from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine

from app.common import Problem, business_date, canonical, truncate


def adapt(value):
    return (
        Jsonb(value, dumps=lambda obj: canonical(obj).decode()) if isinstance(value, (dict, list)) else value
    )


class DB:
    def __init__(self, connection):
        self.raw = connection.connection.driver_connection

    def all(self, query, *params):
        with self.raw.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params or None)
            return cursor.fetchall()

    def first(self, query, *params):
        with self.raw.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params or None)
            return cursor.fetchone()

    def run(self, query, *params):
        return self.raw.execute(query, params or None).rowcount

    def insert(self, table, values, *, conflict=""):
        names = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        return self.run(
            f"INSERT INTO vk_demo.{table} ({names}) VALUES ({placeholders}) {conflict}",
            *(adapt(value) for value in values.values()),
        )

    def update(self, table, values, where):
        setters = ", ".join(f"{key}=%s" for key in values)
        conditions = " AND ".join(f"{key}=%s" for key in where)
        return self.run(
            f"UPDATE vk_demo.{table} SET {setters} WHERE {conditions}",
            *(adapt(value) for value in (*values.values(), *where.values())),
        )


class Clock:
    def now(self, db: DB) -> datetime:
        return truncate(db.first("SELECT clock_timestamp() AS now")["now"])


def make_engine(url):
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        pool_timeout=5,
        connect_args={"connect_timeout": 3},
    )


@dataclass
class Scope:
    db: DB
    sandbox: dict
    epoch: int
    now: datetime
    request_id: str
    client_run_id: str | None = None

    @property
    def sandbox_id(self):
        return self.sandbox["sandbox_id"]

    @property
    def keys(self):
        return {"sandbox_id": self.sandbox_id, "epoch": self.epoch}

    @property
    def date(self):
        return business_date(self.now)

    def scoped(self, table, **keys):
        filters = self.keys | keys
        predicates = " AND ".join(f"{name}=%s" for name in filters)
        return self.db.first(f"SELECT * FROM vk_demo.{table} WHERE {predicates}", *filters.values())

    def require(self, table, **keys):
        row = self.scoped(table, **keys)
        if row is None:
            raise Problem("RESOURCE_NOT_FOUND")
        return row

    def insert(self, table, **values):
        return self.db.insert(table, self.keys | values)

    def update(self, table, values, **keys):
        return self.db.update(table, values, self.keys | keys)


def lock_timeouts(db):
    db.run("SET LOCAL lock_timeout = '2s'")
    db.run("SET LOCAL statement_timeout = '5s'")


def sandbox_for_session(db, token_hash, *, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    row = db.first("SELECT * FROM vk_demo.sandboxes WHERE session_token_hash=%s" + suffix, token_hash)
    if row is None:
        raise Problem("DEMO_SESSION_REQUIRED")
    return row


def verify_session(sandbox, now):
    if sandbox["status"] != "ACTIVE" or now >= sandbox["expires_at"]:
        raise Problem("DEMO_SESSION_EXPIRED")


def rate_limit(db, now, scope, *, per_minute, burst):
    db.insert(
        "rate_limits", {"scope": scope, "tokens": burst, "sampled_at": now}, conflict="ON CONFLICT DO NOTHING"
    )
    row = db.first("SELECT * FROM vk_demo.rate_limits WHERE scope=%s FOR UPDATE", scope)
    elapsed = max(0, (now - row["sampled_at"]).total_seconds())
    tokens = min(burst, row["tokens"] + elapsed * per_minute / 60)
    if tokens < 1:
        wait = timedelta(minutes=(1 - tokens) / per_minute)
        raise Problem("RATE_LIMITED", retry=True, retry_ms=max(1, int(wait.total_seconds() * 1000) + 1))
    db.update(
        "rate_limits", {"tokens": tokens - 1, "sampled_at": max(now, row["sampled_at"])}, {"scope": scope}
    )
