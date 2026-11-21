"""Expiry, local idempotent outbox projection, and dependency-ordered retention."""

import logging
import time
from datetime import timedelta

from app.db import Clock, make_engine
from app.settings import Settings
from app.v2 import checkout
from app.v2.core import Context, Store
from app.v2.service import lock_sandbox

LOGGER = logging.getLogger("casechamp.v2.worker")
PURGE_ORDER = (
    "event_delivery",
    "outbox",
    "events",
    "orders",
    "reservation_lines",
    "reservations",
    "quotes",
    "bundle_solutions",
    "offer_versions",
    "offers",
    "bundle_templates",
    "catalog_skus",
    "role_links",
    "sessions",
    "actors",
)


class Worker:
    def __init__(self, settings, *, engine=None, clock=None):
        self.engine = engine if engine is not None else make_engine(settings.database_url)
        self.maintenance = (
            make_engine(settings.maintenance_database_url) if settings.maintenance_database_url else None
        )
        self.clock = clock if clock is not None else Clock()

    def heartbeat(self):
        with self.engine.begin() as con:
            db = Store(con)
            db.insert(
                "worker_heartbeat",
                {"id": True, "seen_at": self.clock.now(db)},
                conflict="ON CONFLICT (id) DO UPDATE SET seen_at=EXCLUDED.seen_at",
            )

    def expire(self):
        with self.engine.begin() as con:
            db = Store(con)
            targets = db.all(
                """SELECT DISTINCT sandbox_id,generation FROM reservations
                   WHERE state='ACTIVE' AND expires_at<=%s ORDER BY sandbox_id,generation LIMIT 100""",
                self.clock.now(db),
            )
        for target in targets:
            with self.engine.begin() as con:
                db = Store(con)
                if lock_sandbox(db, target["sandbox_id"]) is not None:
                    checkout.sweep(
                        Context(db, target | {"role": "SYSTEM", "actor_id": None}, self.clock.now(db))
                    )

    def publish(self, db, row, now):
        db.insert(
            "event_delivery",
            {
                "sandbox_id": row["sandbox_id"],
                "generation": row["generation"],
                "event_id": row["event_id"],
                "delivered_at": now,
            },
            conflict="ON CONFLICT DO NOTHING",
        )

    def deliver(self):
        with self.engine.begin() as con:
            db = Store(con)
            now = self.clock.now(db)
            rows = db.all(
                """SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt_at<=%s
                   ORDER BY next_attempt_at,id LIMIT 100 FOR UPDATE SKIP LOCKED""",
                now,
            )
            for row in rows:
                attempts = row["attempts"] + 1
                try:
                    with con.begin_nested():
                        self.publish(db, row, now)
                except Exception as exc:
                    delay = 2 ** (attempts - 1) if attempts <= 6 else 60
                    changes = {"attempts": attempts, "next_attempt_at": now + timedelta(seconds=delay)}
                    if attempts >= 10:
                        LOGGER.error(
                            "outbox retry threshold reached id=%s type=%s",
                            row["event_id"],
                            type(exc).__name__,
                        )
                else:
                    changes = {"attempts": attempts, "delivered_at": now}
                db.update("outbox", changes, {"id": row["id"]})

    def cleanup(self):
        engine = self.maintenance if self.maintenance is not None else self.engine
        with engine.begin() as con:
            db = Store(con)
            now = self.clock.now(db)
            db.run("DELETE FROM idempotency_receipts WHERE expires_at<=%s", now)
            db.run("DELETE FROM rate_windows WHERE starts_at<=%s", now - timedelta(hours=1))
            if self.maintenance is None:
                return
            targets = db.all(
                """SELECT id FROM sandboxes b WHERE b.expires_at<=%s AND NOT EXISTS (
                    SELECT 1 FROM idempotency_receipts r WHERE r.sandbox_id=b.id
                    OR r.scope_kind='BOOTSTRAP' AND r.response_data->>'sandbox_id'=b.id::text)
                    ORDER BY b.id LIMIT 100""",
                now,
            )
        for target in targets:
            identity = target["id"]
            with engine.begin() as con:
                db = Store(con)
                if lock_sandbox(db, identity) is None:
                    continue
                db.run("SET CONSTRAINTS published_offer_version_exists DEFERRED")
                for table in PURGE_ORDER:
                    db.run(f"DELETE FROM {table} WHERE sandbox_id=%s", identity)
                db.run("DELETE FROM sandboxes WHERE id=%s", identity)
        with engine.begin() as con:
            db = Store(con)
            db.run(
                """DELETE FROM bootstraps b WHERE b.expires_at<=%s
                   AND NOT EXISTS (SELECT 1 FROM role_links l WHERE l.consumed_by_bootstrap=b.id)
                   AND NOT EXISTS (SELECT 1 FROM idempotency_receipts r
                                   WHERE r.scope_kind='BOOTSTRAP' AND r.scope_owner_id=b.id)""",
                now,
            )

    def serve(self):
        cleanup_at = 0.0
        while True:
            started = time.monotonic()
            try:
                self.heartbeat()
                self.expire()
                self.deliver()
                if started >= cleanup_at:
                    self.cleanup()
                    cleanup_at = started + 60
            except Exception as exc:
                LOGGER.error("worker cycle failed class=%s", type(exc).__name__)
            time.sleep(max(0.0, 1 - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Worker(Settings()).serve()
