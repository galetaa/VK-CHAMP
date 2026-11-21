"""Credentials issued by the server and scoped replay receipts."""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from app.common import canonical, iso
from app.v2.core import TOKEN, TTL, Failure


@dataclass
class Reply:
    data: dict
    status: int
    replayed: bool = False
    session_id: object = None
    session_expiry: object = None
    bootstrap_id: object = None
    bootstrap_expiry: object = None


def sha(text):
    return hashlib.sha256(text.encode("ascii")).digest()


class Security:
    def __init__(self, settings):
        secret = settings.session_secret
        if secret is None or len(secret.get_secret_value().encode()) < 32:
            raise ValueError("v2 requires SESSION_SECRET with at least 32 bytes")
        self.secret = secret.get_secret_value().encode()
        self.origin = settings.web_origin

    def token(self, domain, identity):
        digest = hmac.digest(
            self.secret, domain.encode("ascii") + b"\0" + str(identity).encode("ascii"), "sha256"
        )
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def matches(self, domain, identity, token):
        return (
            isinstance(token, str)
            and bool(TOKEN.fullmatch(token))
            and hmac.compare_digest(
                self.token(domain, identity),
                token,
            )
        )

    def hashed(self, domain, identity):
        return sha(self.token(domain, identity))

    def bootstrap(self, db, now, raw, *, create=False):
        row = db.find("bootstraps", token_hash=sha(raw)) if raw and TOKEN.fullmatch(raw) else None
        if row and now < row["expires_at"] and self.matches("bootstrap", row["id"], raw):
            return row
        if not create:
            raise Failure("AUTH_REQUIRED")
        identity = uuid4()
        row = {
            "id": identity,
            "token_hash": self.hashed("bootstrap", identity),
            "csrf_hash": self.hashed("csrf", identity),
            "created_at": now,
            "expires_at": now + timedelta(seconds=TTL["bootstrap"]),
        }
        db.insert("bootstraps", row)
        return row

    def csrf(self, row, supplied):
        if not self.matches("csrf", row["id"], supplied) or not hmac.compare_digest(
            row["csrf_hash"], sha(supplied)
        ):
            raise Failure("CSRF_REJECTED")

    def session(self, db, now, raw, *, verify_csrf=False, csrf=None):
        actor = (
            db.first(
                """SELECT s.*, a.role, a.generation AS actor_generation, a.revoked_at AS actor_revoked,
               b.generation, b.expires_at AS sandbox_expires_at, b.bundle_enabled
               FROM sessions s JOIN actors a ON (s.sandbox_id,s.actor_id)=(a.sandbox_id,a.id)
               JOIN sandboxes b ON s.sandbox_id=b.id WHERE s.token_hash=%s""",
                sha(raw),
            )
            if raw and TOKEN.fullmatch(raw)
            else None
        )
        if actor is None or not self.matches("session", actor["id"], raw):
            raise Failure("AUTH_REQUIRED")
        if verify_csrf:
            self.csrf(actor, csrf)
        if now >= actor["sandbox_expires_at"]:
            raise Failure("SANDBOX_EXPIRED")
        if actor["revoked_at"] or actor["actor_revoked"] or actor["actor_generation"] != actor["generation"]:
            raise Failure("SESSION_STALE")
        if now >= actor["expires_at"]:
            raise Failure("AUTH_REQUIRED")
        return actor

    def ensure_no_active(self, db, now, cookie):
        try:
            self.session(db, now, cookie)
        except Failure:
            return
        raise Failure("SESSION_ALREADY_ACTIVE")

    def issue_session(self, db, sandbox, actor, now):
        session_id = uuid4()
        session = {
            "id": session_id,
            "sandbox_id": sandbox["id"],
            "actor_id": actor["id"],
            "token_hash": self.hashed("session", session_id),
            "csrf_hash": self.hashed("csrf", session_id),
            "created_at": now,
            "expires_at": min(sandbox["expires_at"], now + timedelta(seconds=TTL["session"])),
        }
        db.insert("sessions", session)
        return session | {
            "role": actor["role"],
            "generation": sandbox["generation"],
            "bundle_enabled": sandbox["bundle_enabled"],
            "sandbox_expires_at": sandbox["expires_at"],
        }

    def session_view(self, session):
        return {
            "actor_id": str(session["actor_id"]),
            "sandbox_id": str(session["sandbox_id"]),
            "generation": session["generation"],
            "role": session["role"],
            "csrf_token": self.token("csrf", session["id"]),
            "expires_at": iso(session["expires_at"]),
            "sandbox_expires_at": iso(session["sandbox_expires_at"]),
            "features": {"bundle": session["bundle_enabled"]},
            "is_test": True,
        }

    def create_role_link(self, ctx, role):
        identity = uuid4()
        expires = ctx.now + timedelta(seconds=TTL["role_link"])
        ctx.insert(
            "role_links",
            id=identity,
            created_by=ctx.identity,
            role=role,
            token_hash=self.hashed("role_link", identity),
            created_at=ctx.now,
            expires_at=expires,
        )
        return {
            "role": role,
            "url": "/join#token=" + self.token("role_link", identity),
            "expires_at": iso(expires),
        }

    def find_link(self, db, token):
        row = db.find("role_links", token_hash=sha(token))
        if row is None or not self.matches("role_link", row["id"], token):
            raise Failure("NOT_FOUND")
        return row


def add_actor(db, sandbox, role, now):
    actor = {
        "id": uuid4(),
        "sandbox_id": sandbox["id"],
        "generation": sandbox["generation"],
        "role": role,
        "created_at": now,
    }
    db.insert("actors", actor)
    return actor


def receipt_scope(command, owner, sandbox=None):
    return {
        "scope_kind": "BOOTSTRAP" if sandbox is None else "ACTOR",
        "scope_owner_id": owner,
        "sandbox_id": sandbox,
        "method": command.method,
        "normalized_path": command.path,
        "key": command.key,
    }


def body_hash(command):
    return hashlib.sha256(canonical(command.body)).digest()


def lookup_receipt(db, now, command, scope, *, remove_expired=False):
    if command.key is None:
        return None
    columns = " AND ".join(f"{name} IS NOT DISTINCT FROM %s" for name in scope)
    row = db.first(f"SELECT * FROM idempotency_receipts WHERE {columns}", *scope.values())
    if not row:
        return None
    if now >= row["expires_at"]:
        if remove_expired:
            db.run("DELETE FROM idempotency_receipts WHERE id=%s", row["id"])
        return None
    if not hmac.compare_digest(row["request_hash"], body_hash(command)):
        raise Failure("IDEMPOTENCY_CONFLICT")
    return Reply(
        row["response_data"],
        row["response_status"],
        replayed=True,
        session_id=row["cookie_session_id"],
        session_expiry=row["cookie_expires_at"],
    )


def save_receipt(db, now, command, scope, reply):
    if command.key is None:
        return
    db.insert(
        "idempotency_receipts",
        scope
        | {
            "request_hash": body_hash(command),
            "response_status": reply.status,
            "response_data": reply.data,
            "cookie_session_id": reply.session_id,
            "cookie_expires_at": reply.session_expiry,
            "created_at": now,
            "expires_at": now + timedelta(seconds=TTL["idempotency"]),
        },
    )


def limit(db, now, scope, count, window):
    start = now.replace(microsecond=0) - timedelta(seconds=int(now.timestamp()) % window)
    accepted = db.first(
        """INSERT INTO rate_windows(scope,starts_at,requests) VALUES (%s,%s,1)
           ON CONFLICT (scope,starts_at) DO UPDATE SET requests=rate_windows.requests+1
           WHERE rate_windows.requests<%s RETURNING requests""",
        scope,
        start,
        count,
    )
    if accepted is None:
        retry = max(1, int((start + timedelta(seconds=window) - now).total_seconds()) + 1)
        raise Failure("RATE_LIMITED", retry_after_seconds=retry)
