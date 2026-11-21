"""Quotes, inventory holds and order state transitions."""

from datetime import timedelta
from uuid import uuid4

from app.common import iso
from app.v2 import offers
from app.v2.core import FEES, LIMITS, RULES, TTL, Failure, check_revision, emit, issue


def fulfillment(version, method):
    if method not in version["fulfillment_methods"]:
        raise Failure("VALIDATION_ERROR", fields=[issue("/fulfillment", "MODE_MISMATCH")])
    return FEES[method]


def verify_lines(rows, lines, days, fee):
    for line in lines:
        sku = rows.get(line["sku"])
        if sku is None or not sku["active"] or sku["ready_in_days"] > days:
            raise Failure("SKU_UNAVAILABLE")
    subtotal = sum(line["line_total_minor"] for line in lines)
    total = subtotal + fee
    if sum(line["quantity"] for line in lines) > LIMITS["max_total_units"]:
        raise Failure("VALIDATION_ERROR", fields=[issue("/lines", "TOTAL_UNITS_LIMIT")])
    if total > LIMITS["max_quote_total_minor"]:
        raise Failure("VALIDATION_ERROR", fields=[issue("/total_minor", "MONEY_LIMIT")])
    for line in lines:
        amount = offers.available(rows[line["sku"]])
        if line["quantity"] > amount:
            raise Failure("INSUFFICIENT_STOCK", available_quantity=amount)
    return subtotal, total


def from_solution(ctx, identity):
    offers.enabled(ctx)
    row = ctx.get("bundle_solutions", id=identity, buyer=True)
    snapshot = row["snapshot"]
    if row["status"] != "SOLVED":
        raise Failure("BUNDLE_NOT_SOLVED")
    if ctx.now >= row["expires_at"]:
        raise Failure("BUNDLE_SOLUTION_EXPIRED")
    request = snapshot["request"]
    offer, version, rows = offers.validate_active(
        ctx,
        snapshot["offer_id"],
        snapshot["offer_version_no"],
        mismatch="BUNDLE_SOLUTION_STALE",
        terms="BUNDLE_SOLUTION_STALE",
        unavailable="BUNDLE_SOLUTION_STALE",
    )
    template = version["bundle_template"]
    if (
        not template
        or template["id"] != snapshot["template_id"]
        or template["version_no"] != snapshot["template_version_no"]
        or snapshot["catalog_terms_hash"] != version["approved_terms_hash"]
    ):
        raise Failure("BUNDLE_SOLUTION_STALE")
    return offer, version, rows, snapshot["lines"], request["fulfillment"], request["requested_days"]


def new_quote(ctx, body):
    direct = body["kind"] == "OFFER"
    if direct:
        offer, version, rows = offers.validate_active(ctx, body["offer_id"], body["offer_version_no"])
        if offer["mode"] != "FIXED":
            raise Failure("VALIDATION_ERROR", fields=[issue("/kind", "MODE_MISMATCH")])
        copies, method, days = body["copies"], body["fulfillment"], body["requested_days"]
        lines = [
            line
            | {"quantity": line["quantity"] * copies, "line_total_minor": line["line_total_minor"] * copies}
            for line in version["lines"]
        ]
    else:
        offer, version, rows, lines, method, days = from_solution(ctx, body["solution_id"])
        copies = 1
    fee = fulfillment(version, method)
    subtotal, total = verify_lines(rows, lines, days, fee)
    expires = ctx.now + timedelta(seconds=TTL["quote"])
    identity = str(uuid4())
    solution_id = body.get("solution_id")
    snapshot = {
        "id": identity,
        "sandbox_id": str(ctx.actor["sandbox_id"]),
        "buyer_id": str(ctx.identity),
        "kind": body["kind"],
        "offer_id": str(offer["id"]),
        "offer_version_no": version["version_no"],
        "solution_id": solution_id,
        "template_version_no": None if direct else version["bundle_template"]["version_no"],
        "copies": copies,
        "fulfillment": method,
        "requested_days": days,
        "lines": lines,
        "subtotal_minor": subtotal,
        "fulfillment_fee_minor": fee,
        "total_minor": total,
        "currency": "RUB",
        "catalog_terms_hash": version["approved_terms_hash"],
        "created_at": iso(ctx.now),
        "expires_at": iso(expires),
        "is_test": True,
    }
    ctx.insert(
        "quotes",
        id=identity,
        buyer_id=ctx.identity,
        offer_id=offer["id"],
        offer_version_no=version["version_no"],
        kind=body["kind"],
        solution_id=solution_id,
        snapshot=snapshot,
        catalog_terms_hash=version["approved_terms_hash"],
        total_minor=total,
        created_at=ctx.now,
        expires_at=expires,
    )
    emit(
        ctx,
        "quote_created",
        offer_id=str(offer["id"]),
        offer_version_no=version["version_no"],
        quote_id=identity,
    )
    return snapshot


def adjust_stock(ctx, on_hand=None, reserved=None):
    on_hand, reserved = on_hand or {}, reserved or {}
    for sku in sorted(on_hand.keys() | reserved.keys()):
        if not on_hand.get(sku) and not reserved.get(sku):
            continue
        ctx.db.run(
            """UPDATE catalog_skus SET stock_on_hand=stock_on_hand+%s,stock_reserved=stock_reserved+%s,
               revision=revision+1,updated_at=%s WHERE sandbox_id=%s AND generation=%s AND sku=%s""",
            on_hand.get(sku, 0),
            reserved.get(sku, 0),
            ctx.now,
            *ctx.scope.values(),
            sku,
        )


def quantities(lines, direction=1):
    return {line["sku"]: direction * line["quantity"] for line in lines}


def reservation_view(ctx, row):
    quote = ctx.get("quotes", id=row["quote_id"])["snapshot"]
    order = ctx.db.find("orders", **ctx.scope, reservation_id=row["id"])
    return {
        "id": str(row["id"]),
        "sandbox_id": str(row["sandbox_id"]),
        "buyer_id": str(row["buyer_id"]),
        "offer_id": str(row["offer_id"]),
        "offer_version_no": row["offer_version_no"],
        "quote_id": str(row["quote_id"]),
        "state": row["state"],
        "revision": row["revision"],
        "created_at": iso(row["created_at"]),
        "expires_at": iso(row["expires_at"]),
        "closed_at": iso(row["closed_at"]),
        "cancel_reason": row["cancel_reason"],
        "order_id": str(order["id"]) if order else None,
        "quote": quote,
        "is_test": True,
    }


def new_reservation(ctx, quote_id):
    quote = ctx.get("quotes", id=quote_id, buyer=True)
    if ctx.now >= quote["expires_at"]:
        raise Failure("QUOTE_EXPIRED")
    if ctx.db.find("reservations", **ctx.scope, quote_id=quote_id):
        raise Failure("QUOTE_ALREADY_USED")
    offer, version, rows = offers.validate_active(
        ctx,
        quote["offer_id"],
        quote["offer_version_no"],
        terms="CATALOG_CHANGED",
    )
    snapshot = quote["snapshot"]
    if version["approved_terms_hash"] != quote["catalog_terms_hash"]:
        raise Failure("CATALOG_CHANGED")
    fee = fulfillment(version, snapshot["fulfillment"])
    verify_lines(rows, snapshot["lines"], snapshot["requested_days"], fee)
    count = ctx.db.first(
        """SELECT count(*) n, min(expires_at) next_expiry FROM reservations
           WHERE sandbox_id=%s AND generation=%s AND buyer_id=%s AND state='ACTIVE'""",
        *ctx.scope.values(),
        ctx.identity,
    )
    if count["n"] >= LIMITS["max_active_reservations_per_buyer"]:
        retry = max(1, int((count["next_expiry"] - ctx.now).total_seconds()) + 1)
        raise Failure("RATE_LIMITED", retry_after_seconds=retry)
    identity = str(uuid4())
    row = {
        "id": identity,
        "quote_id": quote_id,
        "buyer_id": ctx.identity,
        "offer_id": offer["id"],
        "offer_version_no": version["version_no"],
        "state": "ACTIVE",
        "revision": 1,
        "created_at": ctx.now,
        "expires_at": ctx.now + timedelta(seconds=TTL["reservation"]),
        "closed_at": None,
        "cancel_reason": None,
    }
    adjust_stock(ctx, reserved=quantities(snapshot["lines"]))
    ctx.insert("reservations", **row)
    for line in snapshot["lines"]:
        ctx.insert("reservation_lines", reservation_id=identity, sku=line["sku"], quantity=line["quantity"])
    emit(
        ctx,
        "reservation_created",
        offer_id=str(offer["id"]),
        offer_version_no=version["version_no"],
        quote_id=quote_id,
        reservation_id=identity,
        to_state="ACTIVE",
        entity_revision=1,
    )
    return reservation_view(ctx, ctx.scope | row)


def release(ctx, *, where, args=(), state, reason):
    rows = ctx.db.all(
        f"""SELECT * FROM reservations WHERE sandbox_id=%s AND generation=%s AND state='ACTIVE'
            AND {where} ORDER BY id""",
        *ctx.scope.values(),
        *args,
    )
    if not rows:
        return {}
    lines = ctx.db.all(
        """SELECT sku,sum(quantity) AS qty FROM reservation_lines
           WHERE sandbox_id=%s AND generation=%s AND reservation_id=ANY(%s) GROUP BY sku""",
        *ctx.scope.values(),
        [row["id"] for row in rows],
    )
    adjust_stock(ctx, reserved={entry["sku"]: -entry["qty"] for entry in lines})
    changed = {}
    for row in rows:
        identity = row["id"]
        changes = {
            "state": state,
            "revision": row["revision"] + 1,
            "closed_at": ctx.now,
            "cancel_reason": reason if state == "CANCELLED" else None,
        }
        ctx.update("reservations", changes, id=identity)
        emit(
            ctx,
            "reservation_expired" if state == "EXPIRED" else "reservation_cancelled",
            offer_id=str(row["offer_id"]),
            offer_version_no=row["offer_version_no"],
            quote_id=str(row["quote_id"]),
            reservation_id=str(identity),
            from_state="ACTIVE",
            to_state=state,
            reason=reason,
            entity_revision=changes["revision"],
        )
        changed[str(identity)] = row | changes
    return changed


def sweep(ctx):
    release(ctx, where="expires_at<=%s", args=(ctx.now,), state="EXPIRED", reason="EXPIRED")


def stop_offer(ctx, identity, expected, state):
    valid = ("ACTIVE",) if state == "PAUSED" else ("DRAFT", "ACTIVE", "PAUSED")
    row = offers.offer_for_edit(ctx, identity, expected, valid)
    changes = {"state": state, "revision": row["revision"] + 1, "updated_at": ctx.now}
    ctx.update("offers", changes, id=identity)
    emit(
        ctx,
        "offer_state_changed",
        offer_id=identity,
        offer_version_no=row["published_version_no"],
        from_state=row["state"],
        to_state=state,
        entity_revision=changes["revision"],
    )
    release(ctx, where="offer_id=%s", args=(identity,), state="CANCELLED", reason="OFFER_UNAVAILABLE")
    return offers.offer_view(ctx, row | changes)


def active_reservation(ctx, identity, revision):
    row = ctx.get("reservations", id=identity, buyer=True)
    if row["state"] == "EXPIRED":
        raise Failure("RESERVATION_EXPIRED", current_state=row["state"], current_revision=row["revision"])
    if row["state"] != "ACTIVE":
        raise Failure(
            "RESERVATION_STATE_CONFLICT", current_state=row["state"], current_revision=row["revision"]
        )
    check_revision(row["revision"], revision)
    return row


def cancel_reservation(ctx, identity, revision):
    active_reservation(ctx, identity, revision)
    change = release(ctx, where="id=%s", args=(identity,), state="CANCELLED", reason="BUYER_CANCELLED")
    return reservation_view(ctx, change[identity])


def confirm(ctx, identity, revision):
    row = active_reservation(ctx, identity, revision)
    quote = ctx.get("quotes", id=row["quote_id"])["snapshot"]
    offer = ctx.get("offers", id=row["offer_id"])
    if offer["state"] != "ACTIVE":
        raise Failure("OFFER_UNAVAILABLE", current_state=offer["state"])
    removed = quantities(quote["lines"], -1)
    adjust_stock(ctx, on_hand=removed, reserved=removed)
    ctx.update(
        "reservations", {"state": "CONFIRMED", "revision": revision + 1, "closed_at": ctx.now}, id=identity
    )
    stable = (
        "buyer_id",
        "offer_id",
        "offer_version_no",
        "lines",
        "fulfillment",
        "requested_days",
        "subtotal_minor",
        "fulfillment_fee_minor",
        "total_minor",
        "currency",
        "is_test",
    )
    order = {key: quote[key] for key in stable} | {"quote_id": quote["id"]}
    order_id = str(uuid4())
    record = {
        "id": order_id,
        "reservation_id": identity,
        "buyer_id": ctx.identity,
        "status": "CREATED",
        "revision": 1,
        "snapshot": order,
        "total_minor": quote["total_minor"],
        "order_kind": "DEMO",
        "created_at": ctx.now,
        "native_vk_order_id": None,
        "paid_at": None,
        "accepted_at": None,
        "fulfilled_at": None,
        "cancelled_at": None,
        "cancel_reason": None,
    }
    ctx.insert("orders", **record)
    emit(
        ctx,
        "order_created",
        offer_id=quote["offer_id"],
        offer_version_no=quote["offer_version_no"],
        quote_id=quote["id"],
        reservation_id=identity,
        order_id=order_id,
        to_state="CREATED",
        entity_revision=1,
    )
    return order_view(ctx.scope | record)


def order_view(row):
    return row["snapshot"] | {
        "id": str(row["id"]),
        "sandbox_id": str(row["sandbox_id"]),
        "reservation_id": str(row["reservation_id"]),
        **{
            key: row[key]
            for key in ("status", "revision", "order_kind", "native_vk_order_id", "paid_at", "cancel_reason")
        },
        **{key: iso(row[key]) for key in ("created_at", "accepted_at", "fulfilled_at", "cancelled_at")},
    }


def transition(ctx, identity, body):
    row = ctx.get("orders", id=identity)
    check_revision(row["revision"], body["expected_revision"])
    before, after = row["status"], body["to_status"]
    if after not in RULES["order_transitions"][before]:
        raise Failure("ORDER_STATE_CONFLICT", current_state=before, current_revision=row["revision"])
    if (after == "CANCELLED") != (body["cancel_reason"] is not None):
        raise Failure(
            "VALIDATION_ERROR",
            fields=[issue("/cancel_reason", "REQUIRED" if after == "CANCELLED" else "FORMAT")],
        )
    timestamp = {"ACCEPTED": "accepted_at", "FULFILLED": "fulfilled_at", "CANCELLED": "cancelled_at"}[after]
    changes = {
        "status": after,
        "revision": row["revision"] + 1,
        timestamp: ctx.now,
        "cancel_reason": body["cancel_reason"],
    }
    if after == "CANCELLED":
        adjust_stock(ctx, on_hand=quantities(row["snapshot"]["lines"]))
    ctx.update("orders", changes, id=identity)
    snap = row["snapshot"]
    emit(
        ctx,
        "order_status_changed",
        offer_id=snap["offer_id"],
        offer_version_no=snap["offer_version_no"],
        quote_id=snap["quote_id"],
        reservation_id=str(row["reservation_id"]),
        order_id=identity,
        from_state=before,
        to_state=after,
        reason=changes["cancel_reason"],
        entity_revision=changes["revision"],
    )
    return order_view(row | changes)
