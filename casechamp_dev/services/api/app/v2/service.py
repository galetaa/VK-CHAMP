"""Operation runner. Authorization, replay and housekeeping are shared across routes."""

import logging
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from app.common import iso
from app.db import Clock, make_engine
from app.v2 import bundle, checkout, offers
from app.v2.core import LIMITS, RULES, TTL, Context, Failure, Store, check_revision, emit, issue
from app.v2.security import Reply, Security, add_actor, limit, lookup_receipt, receipt_scope, save_receipt

LOGGER = logging.getLogger("casechamp.v2")
PATH_RESOURCES = {
    "updateSku": ("catalog_skus", "sku", "sku"),
    **{
        name: ("offers", "id", "offer_id")
        for name in (
            "getOffer",
            "updateOfferDraft",
            "previewOffer",
            "publishOffer",
            "pauseOffer",
            "archiveOffer",
            "getBuyerOffer",
        )
    },
    "getQuote": ("quotes", "id", "quote_id"),
    **{
        name: ("reservations", "id", "reservation_id")
        for name in (
            "getReservation",
            "confirmReservation",
            "cancelReservation",
        )
    },
    "getOrder": ("orders", "id", "order_id"),
    "transitionOrder": ("orders", "id", "order_id"),
    "getBundleTemplate": ("bundle_templates", "id", "template_id"),
    "getBundleSolution": ("bundle_solutions", "id", "solution_id"),
}
BUNDLE_OPERATIONS = {"getBundleTemplate", "listBundleTemplates", "solveBundle", "getBundleSolution"}


def lock_sandbox(db, identity):
    return db.first("SELECT * FROM sandboxes WHERE id=%s FOR UPDATE", identity)


def require_role(command, actor):
    if actor["role"] not in command.roles:
        raise Failure("FORBIDDEN")


def verify_visible(ctx, command):
    if command.name in BUNDLE_OPERATIONS:
        offers.enabled(ctx)
    if command.name in PATH_RESOURCES:
        table, column, parameter = PATH_RESOURCES[command.name]
        buyer_scoped = table in ("quotes", "reservations", "orders", "bundle_solutions")
        ctx.get(table, buyer=buyer_scoped, **{column: command.parameters[parameter]})
    if command.name == "createReservation":
        ctx.get("quotes", id=command.body["quote_id"], buyer=True)
    if command.name == "solveBundle" or (command.name == "createQuote" and command.body["kind"] == "OFFER"):
        ctx.get("offers", id=command.body["offer_id"])
    if command.name == "createQuote" and command.body["kind"] == "BUNDLE":
        offers.enabled(ctx)
        ctx.get("bundle_solutions", id=command.body["solution_id"], buyer=True)


def reset(ctx, expected):
    check_revision(ctx.actor["generation"], expected)
    checkout.release(ctx, where="TRUE", state="CANCELLED", reason="OFFER_UNAVAILABLE")
    generation = expected + 1
    sandbox = ctx.actor["sandbox_id"]
    ctx.db.run(
        """UPDATE sessions SET revoked_at=%s WHERE sandbox_id=%s AND revoked_at IS NULL AND actor_id<>%s""",
        ctx.now,
        sandbox,
        ctx.identity,
    )
    ctx.db.run(
        "UPDATE actors SET revoked_at=%s WHERE sandbox_id=%s AND revoked_at IS NULL AND id<>%s",
        ctx.now,
        sandbox,
        ctx.identity,
    )
    ctx.db.update("actors", {"generation": generation}, {"sandbox_id": sandbox, "id": ctx.identity})
    ctx.db.update("sandboxes", {"generation": generation}, {"id": sandbox})
    new = Context(ctx.db, ctx.actor | {"generation": generation}, ctx.now)
    offers.seed(new)
    emit(new, "sandbox_reset", entity_revision=generation)
    return {
        "sandbox_id": str(sandbox),
        "generation": generation,
        "seed": "CORE-001",
        "owner_actor_id": str(ctx.identity),
        "is_test": True,
    }


def dashboard(ctx):
    keys = tuple(ctx.scope.values())
    offer_counts = ctx.db.first(
        """SELECT count(*) FILTER (WHERE state='ACTIVE') active_offers,
           count(*) FILTER (WHERE published_version_no IS NOT NULL) published_offers
           FROM offers WHERE sandbox_id=%s AND generation=%s""",
        *keys,
    )
    counts = ctx.db.first(
        """SELECT count(*) orders_created, count(*) FILTER (WHERE status='ACCEPTED') orders_accepted,
           count(*) FILTER (WHERE status='FULFILLED') orders_fulfilled,
           count(*) FILTER (WHERE status='CANCELLED') orders_cancelled,
           COALESCE(sum(total_minor),0)::bigint order_value_minor FROM orders WHERE sandbox_id=%s AND generation=%s""",
        *keys,
    )
    due = ctx.db.first(
        "SELECT count(*) n FROM reservations WHERE sandbox_id=%s AND generation=%s AND state='ACTIVE'",
        *keys,
    )
    return (
        offer_counts
        | counts
        | {
            "as_of": iso(ctx.now),
            "generation": ctx.actor["generation"],
            "quotes_created": offers.count(ctx, "quotes"),
            "active_reservations": due["n"],
            "is_test": True,
            "native_orders_count": "NOT_MEASURED",
        }
    )


def ui_event(ctx, body):
    offer_id, version_no, step = (body[key] for key in ("offer_id", "offer_version_no", "ui_step"))
    if (offer_id is None) != (version_no is None):
        raise Failure("VALIDATION_ERROR", fields=[issue("/offer_version_no", "REQUIRED")])
    if (body["name"] == "wizard_step") != (step is not None):
        raise Failure("VALIDATION_ERROR", fields=[issue("/ui_step", "FORMAT")])
    if offer_id:
        row = ctx.get("offers", id=offer_id)
        if ctx.actor["role"] == "BUYER" and (
            row["state"] != "ACTIVE" or version_no != row["published_version_no"]
        ):
            raise Failure("NOT_FOUND")
        offers.resolve_version(ctx, row, version_no)
    event_id = body["event_id"]
    previous = ctx.db.find("events", **ctx.scope, event_id=event_id)
    if previous:
        payload = previous["payload"]
        if (
            previous["origin"] != "UI"
            or payload["actor_id"] != str(ctx.identity)
            or any(payload[key] != body[key] for key in ("name", "offer_id", "offer_version_no", "ui_step"))
        ):
            raise Failure("IDEMPOTENCY_CONFLICT")
    else:
        emit(
            ctx,
            body["name"],
            event_id=event_id,
            origin="UI",
            offer_id=offer_id,
            offer_version_no=version_no,
            ui_step=step,
        )
    return {"event_id": event_id, "accepted": True, "duplicate": previous is not None}


class Service:
    def __init__(self, settings, wire, *, engine=None, clock=None):
        self.engine = engine if engine is not None else make_engine(settings.database_url)
        self.settings, self.wire = settings, wire
        self.security = Security(settings)
        self.clock = clock if clock is not None else Clock()

    def health(self, db, now):
        version = db.first("SELECT version_num FROM public.alembic_version_v2")
        beat = db.first("SELECT seen_at FROM worker_heartbeat")
        if (
            not version
            or version["version_num"] != "0001_v2"
            or not beat
            or now - beat["seen_at"] > timedelta(seconds=5)
        ):
            raise Failure("COMPUTATION_UNAVAILABLE")
        return Reply({"status": "ok", "contract_version": "2.0.0", "is_test": True}, 200)

    def bootstrap(self, db, now, cmd):
        row = self.security.bootstrap(db, now, cmd.bootstrap, create=True)
        return Reply(
            {"csrf_token": self.security.token("csrf", row["id"]), "expires_at": iso(row["expires_at"])},
            200,
            bootstrap_id=row["id"],
            bootstrap_expiry=row["expires_at"],
        )

    def auth_command(self, con, db, cmd):
        now = self.clock.now(db)
        boot = self.security.bootstrap(db, now, cmd.bootstrap)
        self.security.csrf(boot, cmd.csrf)
        self.wire.validate(cmd)
        scope = receipt_scope(cmd, boot["id"])
        result = lookup_receipt(db, now, cmd, scope)
        if result is not None:
            return result, now
        if cmd.name == "exchangeRoleLink":
            link = self.security.find_link(db, cmd.body["token"])
            sandbox = lock_sandbox(db, link["sandbox_id"])
            if sandbox is None:
                raise Failure("ROLE_LINK_EXPIRED")
        db.first("SELECT id FROM bootstraps WHERE id=%s FOR UPDATE", boot["id"])
        now = self.clock.now(db)
        if now >= boot["expires_at"]:
            raise Failure("AUTH_REQUIRED")
        result = lookup_receipt(db, now, cmd, scope, remove_expired=True)
        if result is not None:
            return result, now
        self.security.ensure_no_active(db, now, cmd.cookie)
        if cmd.name == "createSandbox":
            cap = RULES["rate_limits"]["create_sandbox_per_bootstrap_per_hour"]
            source = self.security.token("rate", cmd.peer)
            limit(db, now, "create-source:" + source, cap * 4, 3600)
            limit(db, now, "create-bootstrap:" + str(boot["id"]), cap, 3600)
            sandbox = {
                "id": uuid4(),
                "generation": 1,
                "seed": cmd.body["seed"],
                "bundle_enabled": cmd.body["enable_bundle"],
                "created_at": now,
                "expires_at": now + timedelta(seconds=TTL["sandbox"]),
            }
            db.insert("sandboxes", sandbox)
            actor = add_actor(db, sandbox, "OWNER", now)
        else:
            link = db.first("SELECT * FROM role_links WHERE id=%s FOR UPDATE", link["id"])
            if sandbox["generation"] != link["generation"] or now >= min(
                sandbox["expires_at"], link["expires_at"]
            ):
                raise Failure("ROLE_LINK_EXPIRED")
            if link["consumed_at"] is not None:
                raise Failure("ROLE_LINK_CONSUMED")
            actor = add_actor(db, sandbox, link["role"], now)
        session = self.security.issue_session(db, sandbox, actor, now)
        if cmd.name == "createSandbox":
            ctx = Context(db, session, now)
            offers.seed(ctx)
            emit(ctx, "sandbox_created")
        else:
            db.update(
                "role_links",
                {
                    "consumed_at": now,
                    "consumed_by_bootstrap": boot["id"],
                    "resulting_session_id": session["id"],
                },
                {"id": link["id"]},
            )
        reply = Reply(
            self.security.session_view(session),
            cmd.status,
            session_id=session["id"],
            session_expiry=session["expires_at"],
        )
        save_receipt(db, now, cmd, scope, reply)
        return reply, now

    def locked_context(self, db, command, actor):
        if lock_sandbox(db, actor["sandbox_id"]) is None:
            raise Failure("AUTH_REQUIRED")
        now = self.clock.now(db)
        actual = self.security.session(db, now, command.cookie)
        ctx = Context(db, actual, now)
        checkout.sweep(ctx)
        return ctx

    def authorize(self, db, cmd):
        now = self.clock.now(db)
        actor = self.security.session(db, now, cmd.cookie, verify_csrf=cmd.method != "GET", csrf=cmd.csrf)
        cmd.sandbox_id = str(actor["sandbox_id"])
        require_role(cmd, actor)
        self.wire.validate(cmd)
        return Context(db, actor, now)

    def admit(self, ctx, cmd):
        if cmd.method == "GET":
            return
        caps = RULES["rate_limits"]
        limit(ctx.db, ctx.now, "mutation:" + str(ctx.actor["id"]), caps["session_mutations_per_minute"], 60)
        if cmd.name == "solveBundle":
            limit(
                ctx.db, ctx.now, "solve:" + str(ctx.identity), caps["bundle_solves_per_buyer_per_minute"], 60
            )
        if cmd.name == "resetSandbox":
            check_revision(ctx.actor["generation"], cmd.body["expected_generation"])
            limit(ctx.db, ctx.now, "reset:" + str(ctx.identity), caps["reset_per_owner_per_minute"], 60)

    def run(self, cmd):
        if cmd.name == "solveBundle":
            return self.solve(cmd)
        with self.engine.begin() as con:
            db = Store(con)
            now = self.clock.now(db)
            if cmd.name == "getHealth":
                return self.health(db, now), now
            if cmd.name == "bootstrapDemo":
                return self.bootstrap(db, now, cmd), now
            if "BOOTSTRAP" in cmd.roles:
                return self.auth_command(con, db, cmd)
            ctx = self.authorize(db, cmd)
            scope = receipt_scope(cmd, ctx.identity, ctx.actor["sandbox_id"])
            result = lookup_receipt(db, ctx.now, cmd, scope)
            if result is not None:
                return result, ctx.now
            verify_visible(ctx, cmd)
        with self.engine.begin() as con:
            db = Store(con)
            current = self.locked_context(db, cmd, ctx.actor)
            try:
                result = lookup_receipt(db, current.now, cmd, scope, remove_expired=True)
                if result is not None:
                    return result, current.now
                verify_visible(current, cmd)
                if cmd.method != "GET":
                    check_revision(current.actor["generation"], ctx.actor["generation"])
                self.admit(current, cmd)
                with con.begin_nested():
                    result = Reply(self.handle(current, cmd), cmd.status)
                    save_receipt(db, current.now, cmd, scope, result)
            except Failure as error:
                if error.status >= 500:
                    raise
                result = error
        return result, current.now

    def solve(self, cmd):
        deadline = monotonic() + 2
        with self.engine.begin() as con:
            read = self.authorize(Store(con), cmd)
            scope = receipt_scope(cmd, read.identity, read.actor["sandbox_id"])
            result = lookup_receipt(read.db, read.now, cmd, scope)
            if result is not None:
                return result, read.now
            verify_visible(read, cmd)
        with self.engine.begin() as con:
            ctx = self.locked_context(Store(con), cmd, read.actor)
            try:
                result = lookup_receipt(ctx.db, ctx.now, cmd, scope, remove_expired=True)
                if result is not None:
                    return result, ctx.now
                self.admit(ctx, cmd)
                captured = bundle.snapshot(ctx, cmd.body)
            except Failure as error:
                if error.status >= 500:
                    raise
                return error, ctx.now
        for attempt in range(2):
            outcome = bundle.enumerate_bundle(
                captured["template"], captured["catalog"], cmd.body, captured["fee"], deadline
            )
            with self.engine.begin() as con:
                ctx = self.locked_context(Store(con), cmd, read.actor)
                try:
                    with con.begin_nested():
                        result = lookup_receipt(ctx.db, ctx.now, cmd, scope, remove_expired=True)
                        if result is not None:
                            return result, ctx.now
                        if monotonic() >= deadline:
                            raise Failure("COMPUTATION_UNAVAILABLE")
                        refreshed = bundle.snapshot(ctx, cmd.body)
                        if captured == refreshed:
                            result = Reply(bundle.save(ctx, cmd.body, captured, outcome), cmd.status)
                            save_receipt(ctx.db, ctx.now, cmd, scope, result)
                            return result, ctx.now
                        if attempt:
                            raise Failure("COMPUTATION_UNAVAILABLE")
                        captured = refreshed
                except Failure as error:
                    if error.status >= 500:
                        raise
                    return error, ctx.now
        raise Failure("COMPUTATION_UNAVAILABLE")

    def handle(self, ctx, cmd):
        body, params, query = cmd.body, cmd.parameters, cmd.query
        revision = body.get("expected_revision")
        name = cmd.name
        if name == "getSession":
            return self.security.session_view(ctx.actor)
        if name == "createRoleLink":
            return self.security.create_role_link(ctx, body["role"])
        if name in ("listCatalog", "listOffers", "listOrders"):
            table, key, view = {
                "listCatalog": ("catalog_skus", "sku", lambda row: offers.sku_view(row)),
                "listOffers": ("offers", "id", lambda row: offers.offer_view(ctx, row)),
                "listOrders": ("orders", "id", checkout.order_view),
            }[name]
            rows, next_cursor = ctx.list(
                table,
                key=key,
                buyer=name == "listOrders",
                limit=query.get("limit", LIMITS["list_limit_default"]),
                cursor=query.get("cursor"),
            )
            return {"items": list(map(view, rows)), "next_cursor": next_cursor}
        if name == "createSku":
            return offers.create_sku(ctx, body)
        if name == "updateSku":
            return offers.update_sku(ctx, params["sku"], body)
        if name == "createOffer":
            return offers.new_offer(ctx, body["mode"])
        if name == "getOffer":
            return offers.offer_view(ctx, ctx.get("offers", id=params["offer_id"]))
        if name == "updateOfferDraft":
            return offers.update_draft(ctx, params["offer_id"], body)
        if name == "previewOffer":
            row = offers.offer_for_edit(ctx, params["offer_id"], revision, ("DRAFT", "ACTIVE", "PAUSED"))
            return offers.preview(ctx, row)[0]
        if name == "publishOffer":
            return offers.publish(ctx, params["offer_id"], revision)
        if name in ("pauseOffer", "archiveOffer"):
            state = "PAUSED" if name == "pauseOffer" else "ARCHIVED"
            return checkout.stop_offer(ctx, params["offer_id"], revision, state)
        if name == "getBuyerOffer":
            return offers.public_offer(ctx, params["offer_id"])
        if name == "createQuote":
            return checkout.new_quote(ctx, body)
        if name == "getQuote":
            return ctx.get("quotes", id=params["quote_id"], buyer=True)["snapshot"]
        if name == "createReservation":
            return checkout.new_reservation(ctx, body["quote_id"])
        if name == "getReservation":
            return checkout.reservation_view(
                ctx, ctx.get("reservations", id=params["reservation_id"], buyer=True)
            )
        if name == "cancelReservation":
            return checkout.cancel_reservation(ctx, params["reservation_id"], revision)
        if name == "confirmReservation":
            return checkout.confirm(ctx, params["reservation_id"], revision)
        if name == "getOrder":
            return checkout.order_view(ctx.get("orders", id=params["order_id"], buyer=True))
        if name == "transitionOrder":
            return checkout.transition(ctx, params["order_id"], body)
        if name == "getDashboard":
            return dashboard(ctx)
        if name == "recordUiEvent":
            return ui_event(ctx, body)
        if name == "listBundleTemplates":
            return {"items": [row["snapshot"] for row in ctx.list("bundle_templates")[0]]}
        if name == "getBundleTemplate":
            return offers.template_view(ctx, params["template_id"])
        if name == "getBundleSolution":
            return ctx.get("bundle_solutions", id=params["solution_id"], buyer=True)["snapshot"]
        if name == "resetSandbox":
            return reset(ctx, body["expected_generation"])
        raise Failure("INTERNAL_ERROR")
