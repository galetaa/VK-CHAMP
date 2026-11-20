import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from time import monotonic
from uuid import UUID

from psycopg.errors import LockNotAvailable, QueryCanceled
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app import __version__, catalog, commerce, events, media, scenarios, session
from app.common import Problem, Result, canonical, digest, iso
from app.db import (
    DB,
    Clock,
    Scope,
    lock_timeouts,
    make_engine,
    rate_limit,
    sandbox_for_session,
    verify_session,
)
from app.hashes import idempotency_payload
from app.planner import run_planner

LOGGER = logging.getLogger("casechamp")


def cmd_has_receipt(scope, command):
    return (
        command.key
        and scope.scoped("idempotency_records", operation_id=command.operation, idempotency_key=command.key)
        is not None
    )


def identify(result):
    if not isinstance(result, dict):
        return None
    for name in (
        "order_id",
        "reservation_id",
        "quote_id",
        "scenario_revision_id",
        "scenario_id",
        "job_id",
        "asset_id",
        "import_id",
        "sandbox_id",
        "resource_id",
        "test_execution_id",
    ):
        if result.get(name):
            try:
                return UUID(str(result[name]))
            except ValueError:
                continue
    if result.get("quote"):
        return UUID(result["quote"]["quote_id"])
    return None


class Service:
    def __init__(self, settings, contract, *, engine=None, clock=None, plan=None):
        self.settings, self.contract = settings, contract
        self.engine = engine if engine is not None else make_engine(settings.database_url)
        self.clock = clock if clock is not None else Clock()
        self.plan = plan if plan is not None else run_planner
        self.rate_secret = (
            settings.session_secret.get_secret_value().encode()
            if settings.session_secret
            else secrets.token_bytes(32)
        )

    @property
    def cookie_name(self):
        return "vk_demo_session" if self.settings.web_origin.startswith("https://") else "vk_demo_session_dev"

    def authenticate(self, db, command, *, lock=False, require_epoch=True, check_simulation=True):
        if command.token is None:
            raise Problem("DEMO_SESSION_REQUIRED")
        if lock:
            lock_timeouts(db)
        started = monotonic()
        sandbox = sandbox_for_session(db, session.token_hash(command.token), lock=lock)
        db_now = self.clock.now(db)
        verify_session(sandbox, db_now)
        current = sandbox["current_epoch"]
        scope = Scope(
            db,
            sandbox,
            command.epoch or current,
            db_now,
            command.request_id,
            command.body.get("client_run_id"),
        )
        if require_epoch and current != scope.epoch:
            raise Problem("SANDBOX_EPOCH_MISMATCH", refresh=True)
        if lock and check_simulation and self.simulation_active(scope):
            raise Problem("STORE_BUSY", reasons=["SIMULATION_RUNNING"], retry=True)
        return scope, round((monotonic() - started) * 1000, 2)

    def simulation_active(self, scope):
        row = scope.db.first(
            "SELECT execution_id FROM vk_demo.simulation_locks WHERE sandbox_id=%s AND expires_at>%s",
            scope.sandbox_id,
            scope.now,
        )
        return row is not None

    def admit(self, command):
        with self.engine.begin() as con:
            db = DB(con)
            lock_timeouts(db)
            scope, _ = self.authenticate(db, command, require_epoch=False)
            rate_limit(db, scope.now, f"{scope.sandbox_id}:general", per_minute=120, burst=30)
            limits = {
                "createQuote": (30, 5, "quote"),
                "uploadAsset": (10, 2, "file"),
                "previewCatalogImport": (10, 2, "file"),
                "analyzeScenario": (2, 2, "ml"),
                "parseIntent": (2, 2, "ml"),
            }
            if command.operation in ("analyzeScenario", "parseIntent"):
                pending = db.first(
                    """SELECT count(*) AS count FROM vk_demo.ml_jobs WHERE sandbox_id=%s AND epoch=%s
                       AND status IN ('QUEUED','RUNNING')""",
                    scope.sandbox_id,
                    scope.sandbox["current_epoch"],
                )["count"]
                if pending >= 2 and not cmd_has_receipt(scope, command):
                    raise Problem("RATE_LIMITED", reasons=["MAX_UNFINISHED_JOBS"], retry=True, retry_ms=1000)
            if command.operation in limits and not cmd_has_receipt(scope, command):
                amount, burst, group = limits[command.operation]
                rate_limit(db, scope.now, f"{scope.sandbox_id}:{group}", per_minute=amount, burst=burst)
            return scope.now, scope.sandbox["current_epoch"]

    def receipt_hash(self, cmd):
        return digest(
            idempotency_payload(operation_id=cmd.operation, method=cmd.method, path=cmd.path, body=cmd.body)
        )

    def receipt(self, scope, cmd):
        if not cmd.key:
            return None
        row = scope.scoped("idempotency_records", operation_id=cmd.operation, idempotency_key=cmd.key)
        if row is None:
            return None
        if row["request_hash"] != self.receipt_hash(cmd):
            raise Problem("IDEMPOTENCY_KEY_REUSED")
        return Result(row["response_body"], row["response_status"], row["resource_id"], replay=True)

    def save_receipt(self, scope, cmd, result):
        if not cmd.key or result.status >= 500 or result.status == 429:
            return
        if result.status == 422 and result.body["code"] in (
            "VALIDATION_ERROR",
            "CONTRADICTORY_PREFERENCES",
            "UNSUPPORTED_ZONE",
            "MAPPING_INVALID",
        ):
            if "STOCK_RETURN_CAPACITY" not in result.body["details"]["reason_codes"]:
                return
        scope.insert(
            "idempotency_records",
            operation_id=cmd.operation,
            idempotency_key=cmd.key,
            request_hash=self.receipt_hash(cmd),
            response_status=result.status,
            response_body=result.body,
            resource_id=result.resource_id,
            created_at=scope.now,
        )

    def bootstrap(self, cmd):
        with self.engine.begin() as con:
            db = DB(con)
            now = self.clock.now(db)
            source = hmac.new(self.rate_secret, cmd.peer.encode(), hashlib.sha256).hexdigest()
            rate_limit(db, now, f"bootstrap:{source}", per_minute=5, burst=5)
            sandbox, token = session.prepare_sandbox(db, now, cmd.body["fixture_id"])
            scope = Scope(db, sandbox, 1, now, cmd.request_id)
            session.seed(scope, self.settings, events.emit)
            outcome = session.context(scope, self.settings)
            events.audit(scope, cmd.operation, "CREATED", sandbox["sandbox_id"])
        return Result(outcome, 201, sandbox["sandbox_id"]), now, 1, token

    def health(self, command):
        try:
            with self.engine.connect() as con:
                db = DB(con)
                now = self.clock.now(db)
                schema = db.first("SELECT version_num FROM public.alembic_version")
                heartbeat = db.first("SELECT max(seen_at) AS seen_at FROM vk_demo.worker_heartbeat")[
                    "seen_at"
                ]
            is_ready = (
                schema
                and schema["version_num"] == "0004_csv_source"
                and heartbeat
                and now - heartbeat <= timedelta(seconds=5)
            )
            if command.operation == "healthReady" and not is_ready:
                return (
                    self.error(Problem("STORE_BUSY", reasons=["SERVICE_NOT_READY"], retry=True)),
                    now,
                    None,
                    None,
                )
            return (
                Result(
                    {
                        "status": "ok",
                        "version": __version__,
                        "database": "ok",
                        "worker_last_heartbeat": iso(heartbeat),
                    }
                ),
                now,
                None,
                None,
            )
        except Exception:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            if command.operation == "healthReady":
                return (
                    self.error(Problem("STORE_BUSY", reasons=["SERVICE_NOT_READY"], retry=True)),
                    now,
                    None,
                    None,
                )
            return (
                Result(
                    {
                        "status": "ok",
                        "version": __version__,
                        "database": "unavailable",
                        "worker_last_heartbeat": None,
                    }
                ),
                now,
                None,
                None,
            )

    def error(self, problem):
        return Result.error(problem, self.contract.statuses)

    def dispatch(self, cmd):
        from datetime import datetime, timezone

        started = monotonic()
        now, current, sandbox_id, wait = datetime.now(timezone.utc), None, None, None
        try:
            if cmd.operation in ("healthLive", "healthReady"):
                return self.health(cmd)
            if cmd.operation == "createSandbox":
                return self.bootstrap(cmd)
            now, current = self.admit(cmd)
            if cmd.operation == "createQuote":
                result, scope, wait = self.create_quote(cmd)
            elif cmd.operation == "runSimulation":
                from app.simulations import run

                result, scope, wait = run(self, cmd)
            elif cmd.operation == "downloadAsset":
                with self.engine.connect() as connection:
                    scope, wait = self.authenticate(DB(connection), cmd)
                    asset = scope.require("assets", **cmd.path_values)
                    result = Result(asset)
            elif cmd.method == "GET" and cmd.operation not in ("getReservation", "verifyQuote"):
                with self.engine.connect() as connection:
                    scope, wait = self.authenticate(DB(connection), cmd)
                    result = Result(self.read(scope, cmd))
            else:
                result, scope, wait = self.mutate(cmd)
            now, current, sandbox_id = scope.now, scope.sandbox["current_epoch"], scope.sandbox_id
            if cmd.operation == "resetSandbox" and result.status < 400:
                current = result.body["sandbox_epoch"]
        except Problem as exc:
            result = self.error(exc)
        except (LockNotAvailable, QueryCanceled, PoolTimeout):
            result = self.error(Problem("STORE_BUSY", retry=True))
        except Exception as exc:
            LOGGER.error("operation_exception %s", type(exc).__name__, extra={"request_id": cmd.request_id})
            if self.settings.app_mode == "test":
                raise
            result = self.error(Problem("INTERNAL_INVARIANT_FAILED"))
        outcome = result.body.get("code", result.body.get("outcome", str(result.status)))
        LOGGER.info(
            canonical(
                {
                    "request_id": cmd.request_id,
                    "client_run_id": cmd.body.get("client_run_id"),
                    "sandbox_id": sandbox_id,
                    "epoch": current,
                    "operationId": cmd.operation,
                    "outcome": outcome,
                    "resource_id": result.resource_id,
                    "duration_ms": round((monotonic() - started) * 1000, 2),
                    "lock_wait_ms": wait,
                    "algorithm_version": self.settings.solver_config_version
                    if cmd.operation == "createQuote"
                    else None,
                }
            ).decode()
        )
        return result, now, current, None

    def mutate(self, cmd):
        prepared = None
        if cmd.operation == "previewCatalogImport":
            prepared = catalog.parse_csv(cmd.body["file"])
        elif cmd.operation == "uploadAsset":
            prepared = media.prepare(cmd.body["file"], cmd.body["kind"])
            prepared = media.place_file(self.settings.media_root, *prepared)
        keep_file = False
        try:
            with self.engine.begin() as con:
                db = DB(con)
                scope, wait = self.authenticate(db, cmd, lock=True, require_epoch=False)
                if scope.epoch != scope.sandbox["current_epoch"]:
                    if cmd.operation == "resetSandbox":
                        replay = self.receipt(scope, cmd)
                        if replay:
                            return replay, scope, wait
                    raise Problem("SANDBOX_EPOCH_MISMATCH", refresh=True)
                commerce.sweep(scope, events.emit)
                replay = self.receipt(scope, cmd)
                if replay:
                    return replay, scope, wait
                result = self.execute(scope, cmd, prepared)
            keep_file = result.status < 400
            return result, scope, wait
        finally:
            if cmd.operation == "uploadAsset" and prepared and not keep_file:
                (self.settings.media_root / prepared["storage_key"]).unlink(missing_ok=True)

    def execute(self, scope, command, prepared=None):
        try:
            data = self.write(scope, command, prepared)
            new = command.operation in (
                "createScenario",
                "publishScenario",
                "previewCatalogImport",
                "uploadAsset",
                "reserveQuote",
                "confirmTestOrder",
            )
            queued = command.operation in ("analyzeScenario", "parseIntent")
            result = Result(data, 202 if queued else 201 if new else 200, identify(data))
        except Problem as problem:
            if problem.code == "INTERNAL_INVARIANT_FAILED":
                raise
            result = self.error(problem)
        self.save_receipt(scope, command, result)
        events.audit(
            scope, command.operation, result.body.get("code", str(result.status)), result.resource_id
        )
        return result

    def create_quote(self, cmd):
        with self.engine.begin() as con:
            db = DB(con)
            db.run("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            scope, _ = self.authenticate(db, cmd)
            replay = self.receipt(scope, cmd)
            if replay:
                return replay, scope, None
            intent, snapshot = commerce.read_snapshot(scope, cmd.body)
            started = scope.now

        plan = self.plan(snapshot, intent)
        with self.engine.begin() as con:
            scope, wait = self.authenticate(DB(con), cmd, lock=True)
            commerce.sweep(scope, events.emit)
            replay = self.receipt(scope, cmd)
            if replay:
                return replay, scope, wait
            try:
                data = commerce.save_solve(
                    scope, intent, snapshot, plan, started, self.settings.solver_config_version, events.emit
                )
                result = Result(data, 200, identify(data))
            except commerce.RecordedSolveFailure as error:
                result = self.error(error)
            except Problem as error:
                if error.code == "INTERNAL_INVARIANT_FAILED":
                    raise
                result = self.error(error)
            self.save_receipt(scope, cmd, result)
            events.audit(
                scope, cmd.operation, result.body.get("code", result.body.get("outcome")), result.resource_id
            )
        return result, scope, wait

    def read(self, scope, cmd):
        match cmd.operation:
            case "getContext":
                return session.context(scope, self.settings)
            case "getCatalog":
                return catalog.get_catalog(scope)
            case "getImport":
                return catalog.import_view(scope.require("catalog_imports", **cmd.path_values), scope.now)
            case "getAssetMetadata":
                return media.asset_view(scope.require("assets", **cmd.path_values))
            case "getScenario":
                return scenarios.scenario_view(scope, scope.require("scenarios", **cmd.path_values))
            case "getScenarioRevision":
                return scenarios.revision_view(
                    scope.require("scenario_revisions", scenario_revision_id=cmd.path_values["revision_id"])
                )
            case "getQuote":
                return commerce.quote_view(scope, scope.require("quotes", **cmd.path_values))
            case "getOrder":
                return commerce.order_view(scope.require("orders", **cmd.path_values))
            case "getJob":
                from app.ml import job_view

                return job_view(scope.require("ml_jobs", **cmd.path_values))
            case "getEvents":
                return events.list_events(scope, cmd.query.get("limit", 100), cmd.query.get("cursor"))
            case "getMetrics":
                return events.metrics(scope)
            case _:
                raise Problem("INTERNAL_INVARIANT_FAILED")

    def write(self, scope, cmd, prepared):
        path, body, event = cmd.path_values, cmd.body, events.emit
        match cmd.operation:
            case "resetSandbox":
                return session.reset(scope, self.settings, body, event)
            case "previewCatalogImport":
                return catalog.preview_import(scope, body["file"], prepared)
            case "commitImport":
                return catalog.commit_import(scope, path["import_id"], body, event)
            case "patchSku":
                return catalog.patch_sku(scope, path["sku_id"], body, event)
            case "withdrawSku":
                return commerce.withdraw_product(scope, path["sku_id"], body, event)
            case "uploadAsset":
                return media.save_asset(scope, prepared)
            case "createScenario":
                return scenarios.create_scenario(scope, body)
            case "patchScenario":
                return scenarios.patch_scenario(scope, path["scenario_id"], body)
            case "publishScenario":
                return scenarios.publish(scope, path["scenario_id"], body["expected_draft_version"], event)
            case "withdrawScenarioRevision":
                return commerce.withdraw_revision(scope, path["revision_id"], body, event)
            case "reserveQuote":
                return commerce.reserve(scope, body, event)
            case "getReservation":
                return commerce.reservation_view(scope, scope.require("reservations", **path))
            case "cancelReservation":
                return commerce.cancel_reservation(scope, path["reservation_id"], event)
            case "confirmTestOrder":
                return commerce.confirm(scope, body, event)
            case "cancelTestOrder":
                return commerce.cancel_order(scope, path["order_id"], event)
            case "verifyQuote":
                return commerce.verify_quote(scope, path["quote_id"])
            case "recordClientEvents":
                return events.record(scope, body["events"])
            case "analyzeScenario" | "parseIntent":
                from app.ml import enqueue

                return enqueue(scope, self.settings, cmd)
            case _:
                raise Problem("INTERNAL_INVARIANT_FAILED")
