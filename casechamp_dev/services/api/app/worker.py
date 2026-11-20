import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from app import commerce, events
from app.db import DB, Clock, Scope, lock_timeouts, make_engine
from app.ml import run_provider
from app.settings import Settings

LOGGER = logging.getLogger("casechamp.worker")
POLL_SECONDS = 1
TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELED")

CLEANUP_TABLES = (
    "verification_runs",
    "audit_log",
    "client_events",
    "event_projection",
    "outbox_events",
    "idempotency_records",
    "order_lines",
    "orders",
    "reservation_lines",
    "reservations",
    "quote_lines",
    "quotes",
    "solve_runs",
    "ml_jobs",
    "scenario_revision_items",
    "scenario_revisions",
    "scenarios",
    "catalog_imports",
    "inventory",
    "product_versions",
    "products",
    "assets",
    "fulfillment_terms",
)


class Worker:
    def __init__(self, settings, *, engine=None, clock=None, provider=None):
        self.settings = settings
        self.engine = engine if engine is not None else make_engine(settings.database_url)
        self.maintenance = (
            make_engine(settings.maintenance_database_url) if settings.maintenance_database_url else None
        )
        self.clock = clock if clock is not None else Clock()
        self.provider = provider if provider is not None else run_provider
        self.name = f"worker-{uuid4()}"

    def heartbeat(self):
        with self.engine.begin() as conn:
            db = DB(conn)
            db.insert(
                "worker_heartbeat",
                {"worker_name": self.name, "seen_at": self.clock.now(db)},
                conflict="ON CONFLICT (worker_name) DO UPDATE SET seen_at=EXCLUDED.seen_at",
            )

    def lock_sandbox(self, db, sandbox_id, epoch):
        lock_timeouts(db)
        row = db.first("SELECT * FROM vk_demo.sandboxes WHERE sandbox_id=%s FOR UPDATE", sandbox_id)
        if row is None:
            return None
        return Scope(db, row, epoch, self.clock.now(db), str(uuid4()))

    def expire(self):
        with self.engine.connect() as con:
            db = DB(con)
            now = self.clock.now(db)
            identities = db.all(
                """SELECT DISTINCT sandbox_id,epoch FROM vk_demo.reservations WHERE status='ACTIVE'
                   AND expires_at<=%s ORDER BY sandbox_id,epoch LIMIT 100""",
                now,
            )
        for item in identities:
            with self.engine.begin() as con:
                scope = self.lock_sandbox(DB(con), item["sandbox_id"], item["epoch"])
                if scope:
                    commerce.sweep(scope, events.emit)

    def claim_outbox(self, size=30):
        with self.engine.begin() as con:
            db = DB(con)
            now = self.clock.now(db)
            rows = db.all(
                """SELECT * FROM vk_demo.outbox_events
                   WHERE (publish_status='PENDING' AND next_attempt_at<=%s)
                      OR (publish_status='CLAIMED' AND lease_until<=%s)
                   ORDER BY next_attempt_at,event_id FOR UPDATE SKIP LOCKED LIMIT %s""",
                now,
                now,
                size,
            )
            claimed = []
            for row in rows:
                lease = uuid4()
                identity = {key: row[key] for key in ("sandbox_id", "epoch", "event_id")}
                db.update(
                    "outbox_events",
                    {
                        "publish_status": "CLAIMED",
                        "attempt_count": row["attempt_count"] + 1,
                        "lease_token": lease,
                        "lease_until": now + timedelta(seconds=15),
                    },
                    identity,
                )
                claimed.append((row, identity, lease))
        return claimed

    def project_outbox(self, row, identity):
        with self.engine.begin() as con:
            db = DB(con)
            db.insert(
                "event_projection",
                identity
                | {
                    "event_name": row["event_name"],
                    "occurred_at": row["occurred_at"],
                    "projected_at": self.clock.now(db),
                    "payload": row["payload"],
                },
                conflict="ON CONFLICT DO NOTHING",
            )

    def acknowledge_outbox(self, identity, lease, *, retry_delay=None):
        with self.engine.begin() as con:
            db = DB(con)
            now = self.clock.now(db)
            outcome = {"publish_status": "PUBLISHED", "published_at": now}
            if retry_delay is not None:
                outcome = {
                    "publish_status": "PENDING",
                    "next_attempt_at": now + timedelta(seconds=retry_delay),
                }
            db.update(
                "outbox_events",
                outcome | {"lease_token": None, "lease_until": None},
                identity | {"lease_token": lease},
            )

    def deliver_outbox(self):
        for row, identity, lease in self.claim_outbox():
            try:
                self.project_outbox(row, identity)
            except Exception as exc:
                LOGGER.warning("outbox retry id=%s class=%s", row["event_id"], type(exc).__name__)
                self.acknowledge_outbox(
                    identity, lease, retry_delay=min(60, 2 ** min(6, row["attempt_count"]))
                )
            else:
                self.acknowledge_outbox(identity, lease)

    def job_candidates(self, *, expiry_only):
        with self.engine.connect() as con:
            db = DB(con)
            now = self.clock.now(db)
            if expiry_only:
                return db.all(
                    """SELECT sandbox_id,epoch,job_id FROM vk_demo.ml_jobs
                       WHERE (status='RUNNING' AND deadline_at<=%s)
                          OR (status='QUEUED' AND created_at<=%s)
                       ORDER BY created_at,job_id LIMIT 100""",
                    now,
                    now - timedelta(seconds=60),
                )
            return db.all(
                """SELECT sandbox_id,epoch,job_id FROM vk_demo.ml_jobs WHERE status='QUEUED'
                   ORDER BY created_at,job_id LIMIT 30""",
            )

    def job_guard(self, scope, row):
        if row["status"] in TERMINAL:
            return False
        if scope.sandbox["current_epoch"] != scope.epoch or scope.sandbox["expires_at"] <= scope.now:
            status, code = (
                "CANCELED",
                "SANDBOX_EXPIRED" if scope.sandbox["expires_at"] <= scope.now else "RESET",
            )
        elif row["status"] == "QUEUED" and scope.now >= row["created_at"] + timedelta(seconds=60):
            status, code = "TIMED_OUT", "QUEUE_TIMEOUT"
        elif row["status"] == "RUNNING" and scope.now >= row["deadline_at"]:
            status, code = "TIMED_OUT", "RUN_TIMEOUT"
        else:
            return True
        self.finish(scope, row, status, failure=code)
        return False

    def finish(self, scope, row, status, *, result=None, failure=None, source=None):
        scope.update(
            "ml_jobs",
            {"status": status, "finished_at": scope.now, "result": result, "failure_code": failure},
            job_id=row["job_id"],
        )
        events.audit(
            scope,
            "mlJobFinish",
            failure or status,
            row["job_id"],
            {
                "provider_mode": row["input_snapshot"]["provider_mode"],
                "source": source or row["input_snapshot"]["provider_mode"],
                "model_revision": result["model_revision"] if result else None,
                "raw_output_sha256": result["raw_output_sha256"] if result else None,
                "input_sha256": row["input_snapshot"]["input_sha256"],
                "schema_valid": result["schema_valid"] if result else False,
                "duration_ms": int((scope.now - row["started_at"]).total_seconds() * 1000)
                if row["started_at"]
                else None,
            },
        )

    def expire_jobs(self):
        for row in self.job_candidates(expiry_only=True):
            with self.engine.begin() as con:
                scope = self.lock_sandbox(DB(con), row["sandbox_id"], row["epoch"])
                actual = scope.scoped("ml_jobs", job_id=row["job_id"]) if scope else None
                if actual:
                    self.job_guard(scope, actual)

    def claim_job(self):
        for candidate in self.job_candidates(expiry_only=False):
            with self.engine.begin() as con:
                scope = self.lock_sandbox(DB(con), candidate["sandbox_id"], candidate["epoch"])
                row = scope.scoped("ml_jobs", job_id=candidate["job_id"]) if scope else None
                if not row or row["status"] != "QUEUED" or not self.job_guard(scope, row):
                    continue
                limit = 120 if row["kind"] == "ANALYZE_SCENARIO" else 30
                changes = {
                    "status": "RUNNING",
                    "started_at": scope.now,
                    "deadline_at": min(row["deadline_at"], scope.now + timedelta(seconds=limit)),
                }
                scope.update("ml_jobs", changes, job_id=row["job_id"])
                return row | changes
        return None

    def cached_result(self, job):
        snapshot = job["input_snapshot"]
        if snapshot["provider_mode"] != "LOCAL":
            return None
        try:
            manifest = json.loads((self.settings.model_root / "model-manifest.json").read_text())
            revision = manifest["qwen_revision"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        with self.engine.connect() as con:
            row = DB(con).first(
                """SELECT result FROM vk_demo.ml_jobs WHERE sandbox_id=%s AND epoch=%s AND job_id<>%s
                   AND status='SUCCEEDED' AND input_snapshot->>'input_sha256'=%s
                   AND input_snapshot->>'provider_mode'='LOCAL' AND result->>'model_revision'=%s
                   ORDER BY finished_at DESC LIMIT 1""",
                job["sandbox_id"],
                job["epoch"],
                job["job_id"],
                snapshot["input_sha256"],
                revision,
            )
        if not row:
            return None
        result = row["result"]
        if snapshot["asset"]:
            for mapping in result["mapping_suggestions"]:
                for candidate in mapping["candidates"]:
                    candidate["source_asset_id"] = snapshot["asset"]["asset_id"]
        return result

    def process_one_job(self):
        claimed = self.claim_job()
        if claimed is None:
            return False
        source = None
        try:
            cached = self.cached_result(claimed)
            if cached is None:
                remaining = (claimed["deadline_at"] - claimed["started_at"]).total_seconds()
                result, failed = self.provider(self.settings, claimed["input_snapshot"], remaining)
            else:
                result, failed, source = cached, None, "CACHE"
        except Exception as exc:
            LOGGER.warning("model inference failed class=%s", type(exc).__name__)
            result, failed = None, "MODEL_FAILED"
        with self.engine.begin() as con:
            scope = self.lock_sandbox(DB(con), claimed["sandbox_id"], claimed["epoch"])
            row = scope.scoped("ml_jobs", job_id=claimed["job_id"]) if scope else None
            if row and row["status"] == "RUNNING" and self.job_guard(scope, row):
                status = "TIMED_OUT" if failed == "RUN_TIMEOUT" else "FAILED" if failed else "SUCCEEDED"
                self.finish(scope, row, status, result=result, failure=failed, source=source)
        return True

    def cleanup(self):
        if not self.maintenance:
            return
        with self.maintenance.connect() as con:
            db = DB(con)
            now = self.clock.now(db)
            expired = db.all("SELECT sandbox_id FROM vk_demo.sandboxes WHERE expires_at<=%s LIMIT 100", now)
        for row in expired:
            sandbox_id = row["sandbox_id"]
            with self.maintenance.begin() as con:
                db = DB(con)
                sandbox = db.first(
                    "SELECT * FROM vk_demo.sandboxes WHERE sandbox_id=%s FOR UPDATE", sandbox_id
                )
                if not sandbox or sandbox["expires_at"] > self.clock.now(db):
                    continue
                assets = db.all("SELECT storage_key FROM vk_demo.assets WHERE sandbox_id=%s", sandbox_id)
                for table in CLEANUP_TABLES:
                    db.run(f"DELETE FROM vk_demo.{table} WHERE sandbox_id=%s", sandbox_id)
                db.run("DELETE FROM vk_demo.sandboxes WHERE sandbox_id=%s", sandbox_id)
            for item in assets:
                key = item["storage_key"]
                if re.fullmatch(r"[0-9a-f]{32}", key):
                    (self.settings.media_root / key).unlink(missing_ok=True)
        with self.maintenance.begin() as con:
            db = DB(con)
            db.run("DELETE FROM vk_demo.rate_limits WHERE sampled_at<%s", now - timedelta(hours=1))
            db.run("DELETE FROM vk_demo.worker_heartbeat WHERE seen_at<%s", now - timedelta(hours=1))

    def recover_simulations(self):
        from app.simulations import recover

        with self.engine.connect() as con:
            db = DB(con)
            leases = db.all(
                "SELECT sandbox_id FROM vk_demo.simulation_locks WHERE expires_at<=%s AND recovery<>'{}'::jsonb",
                self.clock.now(db),
            )
        for row in leases:
            with self.engine.begin() as con:
                db = DB(con)
                lock_timeouts(db)
                sandbox = db.first(
                    "SELECT * FROM vk_demo.sandboxes WHERE sandbox_id=%s FOR UPDATE", row["sandbox_id"]
                )
                if sandbox is None:
                    continue
                scope = Scope(db, sandbox, sandbox["current_epoch"], self.clock.now(db), str(uuid4()))
                lease = db.first(
                    "SELECT * FROM vk_demo.simulation_locks WHERE sandbox_id=%s", row["sandbox_id"]
                )
                if lease:
                    recover(scope, lease)

    def tick(self):
        self.heartbeat()
        self.expire()
        self.expire_jobs()
        self.recover_simulations()
        self.deliver_outbox()

    def serve(self):
        next_cleanup = 0.0
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml-job") as tasks:
            future = None
            while True:
                cycle = time.monotonic()
                try:
                    self.tick()
                    if cycle >= next_cleanup:
                        self.cleanup()
                        next_cleanup = cycle + 60
                    if future is None or future.done():
                        if future:
                            future.result()
                        future = tasks.submit(self.process_one_job)
                except Exception as exc:
                    LOGGER.error("worker tick failed class=%s", type(exc).__name__)
                    if future and future.done():
                        future = None
                time.sleep(max(0, POLL_SECONDS - (time.monotonic() - cycle)))


def main():
    logging.basicConfig(level=logging.INFO)
    Worker(Settings()).serve()


if __name__ == "__main__":
    main()
