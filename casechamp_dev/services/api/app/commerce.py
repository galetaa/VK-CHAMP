from datetime import date, timedelta
from uuid import uuid4

from app.catalog import bump_catalog, change_inventory, list_products, lock_inventory, update_product
from app.common import Problem, digest, expiry, field_error, iso
from app.hashes import canonical_snapshot, quote_payload, request_payload, solution_payload
from app.planner import cloth_fit, normalized_intent
from app.verifier import verify_solution

QUOTE_TTL = 900
RESERVATION_TTL = 600
COPY_FIELDS = (
    "lines",
    "currency",
    "subtotal_minor",
    "shipping_minor",
    "total_minor",
    "earliest_date",
    "needed_by",
)


def latest_fulfillment_version(scope):
    return scope.require("sandbox_epochs")["fulfillment_version"]


def read_snapshot(scope, request):
    intent = normalized_intent(request, scope.date)
    revision = scope.require("scenario_revisions", scenario_revision_id=intent["scenario_revision_id"])
    if revision["status"] != "PUBLISHED":
        raise Problem("TEMPLATE_WITHDRAWN")
    revision_items = revision["immutable_snapshot"]["slots"]
    snapshot = canonical_snapshot(
        {
            "snapshot_id": str(uuid4()),
            "business_date": scope.date.isoformat(),
            "currency": "RUB",
            "catalog": list_products(scope, as_of=scope.now),
            "template_slots": revision_items,
            "fulfillment_version": latest_fulfillment_version(scope),
        }
    )
    return intent, snapshot


def lead_days(snapshot):
    transit = int(snapshot["purchase_request"]["fulfillment"]["mode"] == "LOCAL_DELIVERY")
    return transit + max(line["handling_days"] for line in snapshot["lines"])


def commercial_lines(snapshot, result, intent):
    products = {product["sku_id"]: product for product in snapshot["catalog"]}
    lines = []
    for line in result["lines"]:
        product = products[line["sku_id"]]
        fit = cloth_fit(product, intent["table_dimensions"]) if line["slot_id"] == "tablecloth" else None
        lines.append(
            line
            | {name: product[name] for name in ("title", "color", "image_asset_id", "product_version")}
            | {"cloth_fit": fit}
        )
    return lines


def save_solve(scope, intent, snapshot, plan, started, version, event):
    calculation = plan.result
    outcome = plan.error or calculation["outcome"]
    if outcome == "QUOTED":
        if scope.date.isoformat() != snapshot["business_date"]:
            raise Problem("CONCURRENT_VERSION_CHANGE", reasons=["BUSINESS_DATE_ROLLOVER"], refresh=True)
        revision = scope.require("scenario_revisions", scenario_revision_id=intent["scenario_revision_id"])
        if revision["status"] != "PUBLISHED":
            raise Problem("TEMPLATE_WITHDRAWN")
        if snapshot["fulfillment_version"] != latest_fulfillment_version(scope):
            raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
        lines = commercial_lines(snapshot, calculation, intent)
        for line in lines:
            product = scope.require("products", sku_id=line["sku_id"])
            if line["product_version"] != product["product_version"] or not product["active"]:
                raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
        errors = verify_solution(snapshot, intent, calculation)
        if errors:
            outcome = "INTERNAL_INVARIANT_FAILED"
    else:
        errors = []
    reason_list = plan.reasons[:30]
    input_hash = digest(request_payload(intent))
    snapshot_hash = digest(snapshot)
    run_id = uuid4()
    scope.insert(
        "solve_runs",
        solve_run_id=run_id,
        scenario_revision_id=intent["scenario_revision_id"],
        client_run_id=intent["client_run_id"],
        algorithm_version=version,
        request_hash=input_hash,
        snapshot_hash=snapshot_hash,
        business_date=date.fromisoformat(snapshot["business_date"]),
        immutable_input={"snapshot": snapshot, "purchase_request": intent},
        result_code=outcome,
        diagnostics={
            "phases": plan.phases,
            "result": calculation,
            "reasons": plan.reasons,
            "verification_errors": errors,
        },
        created_at=started,
        finished_at=scope.now,
    )
    if outcome in ("COMPUTATION_LIMIT", "COMPUTATION_ABORTED", "INTERNAL_INVARIANT_FAILED"):
        raise RecordedSolveFailure(outcome, retry=outcome == "COMPUTATION_ABORTED")

    result = {
        "outcome": outcome,
        "quote": None,
        "reasons": reason_list,
        "minimum_total_minor": calculation.get("minimum_total_minor"),
        "verification_passed": True,
    }
    if outcome != "QUOTED":
        return result
    quote_id = uuid4()
    terms = {
        "quote_id": str(quote_id),
        "scenario_revision_id": intent["scenario_revision_id"],
        "request_hash": input_hash,
        "algorithm_version": version,
        "snapshot_hash": snapshot_hash,
        "created_at": iso(scope.now),
        "purchase_request": intent,
        "lines": lines,
        "currency": "RUB",
        "subtotal_minor": calculation["subtotal_minor"],
        "shipping_minor": calculation["shipping_minor"],
        "total_minor": calculation["total_minor"],
        "earliest_date": calculation["earliest_date"],
        "needed_by": intent["needed_by"],
        "fulfillment_version": snapshot["fulfillment_version"],
        "objective": calculation["objective"],
        "reasons": reason_list,
    }
    expires = expiry(scope.now, QUOTE_TTL, intent["needed_by"], lead_days(terms))
    if expires <= scope.now:
        raise Problem("CONCURRENT_VERSION_CHANGE", reasons=["BUSINESS_DATE_ROLLOVER"], refresh=True)
    terms["expires_at"] = iso(expires)
    accepted_hash = digest(
        quote_payload(
            sandbox_id=str(scope.sandbox_id),
            epoch=scope.epoch,
            business_date=scope.date.isoformat(),
            quote=terms,
        )
    )
    solution_hash = digest(
        solution_payload(algorithm_version=version, snapshot=snapshot, request=intent, result=calculation)
    )
    scope.insert(
        "quotes",
        quote_id=quote_id,
        solve_run_id=run_id,
        scenario_revision_id=intent["scenario_revision_id"],
        status="ACTIVE",
        aggregate_version=1,
        request_hash=input_hash,
        quote_hash=accepted_hash,
        solution_hash=solution_hash,
        snapshot_hash=snapshot_hash,
        algorithm_version=version,
        immutable_terms=terms,
        business_date=scope.date,
        earliest_date=date.fromisoformat(terms["earliest_date"]),
        needed_by=date.fromisoformat(intent["needed_by"]) if intent["needed_by"] else None,
        fulfillment_version=snapshot["fulfillment_version"],
        currency="RUB",
        subtotal_minor=terms["subtotal_minor"],
        shipping_minor=terms["shipping_minor"],
        total_minor=terms["total_minor"],
        created_at=scope.now,
        expires_at=expires,
        updated_at=scope.now,
    )
    fields = (
        "slot_id",
        "sku_id",
        "product_version",
        "pack_units",
        "pack_count",
        "required_units",
        "effective_owned_units",
        "delivered_units",
        "excess_units",
        "unit_pack_price_minor",
        "line_total_minor",
    )
    for line in lines:
        scope.insert(
            "quote_lines", quote_id=quote_id, line_snapshot=line, **{name: line[name] for name in fields}
        )
    event(
        scope,
        "quote.created",
        "quote",
        quote_id,
        1,
        {
            "total_minor": terms["total_minor"],
            "scenario_revision_id": intent["scenario_revision_id"],
            "quote_hash": accepted_hash,
            "solution_hash": solution_hash,
            "snapshot_hash": snapshot_hash,
            "product_versions": {line["sku_id"]: line["product_version"] for line in lines},
        },
    )
    result["quote"] = terms | {"quote_hash": accepted_hash, "reservation_id": None, "status": "ACTIVE"}
    return result


class RecordedSolveFailure(Problem):
    """The diagnostic row is committed; a transient/technical receipt is never saved."""


def quote_view(scope, row):
    status = row["status"]
    reservation = scope.scoped("reservations", quote_id=row["quote_id"])
    if (status == "ACTIVE" and scope.now >= row["expires_at"]) or (
        status == "RESERVED" and reservation and scope.now >= reservation["expires_at"]
    ):
        status = "EXPIRED"
    return row["immutable_terms"] | {
        "status": status,
        "quote_hash": row["quote_hash"],
        "reservation_id": str(reservation["reservation_id"]) if reservation else None,
    }


def reservation_view(scope, row):
    order = scope.scoped("orders", reservation_id=row["reservation_id"])
    return {
        "reservation_id": str(row["reservation_id"]),
        "quote_id": str(row["quote_id"]),
        "quote_hash": row["accepted_quote_hash"],
        "status": row["status"],
        "created_at": iso(row["created_at"]),
        "expires_at": iso(row["expires_at"]),
        "completed_at": iso(row["completed_at"]),
        "order_id": str(order["order_id"]) if order else None,
        "reason_code": row["reason_code"],
    }


def order_view(row):
    return row["immutable_terms"] | {
        "order_id": str(row["order_id"]),
        "order_number": row["order_number"],
        "status": row["status"],
        "reservation_id": str(row["reservation_id"]),
        "quote_id": str(row["quote_id"]),
        "created_at": iso(row["created_at"]),
        "canceled_at": iso(row["canceled_at"]),
        "is_test": True,
        "payment_status": "NOT_APPLICABLE",
        "native_vk_order_id": None,
    }


def set_quote_status(scope, quote, status):
    if quote["status"] != status:
        scope.update(
            "quotes",
            {"status": status, "aggregate_version": quote["aggregate_version"] + 1, "updated_at": scope.now},
            quote_id=quote["quote_id"],
        )


def release(scope, reservation, status, reason, event):
    if reservation["status"] != "ACTIVE":
        return reservation
    lines = reservation["immutable_terms"]["lines"]
    inventory = lock_inventory(scope, [item["sku_id"] for item in lines])
    for line in sorted(lines, key=lambda item: item["sku_id"]):
        row = inventory[line["sku_id"]]
        if row["reserved_packs"] < line["pack_count"]:
            raise Problem("INTERNAL_INVARIANT_FAILED")
        change_inventory(scope, row, reserved=-line["pack_count"])
    version = reservation["aggregate_version"] + 1
    changes = {
        "status": status,
        "completed_at": scope.now,
        "reason_code": reason,
        "aggregate_version": version,
    }
    scope.update("reservations", changes, reservation_id=reservation["reservation_id"])
    quote = scope.require("quotes", quote_id=reservation["quote_id"])
    set_quote_status(scope, quote, "EXPIRED" if status == "EXPIRED" else "CANCELED")
    event(
        scope,
        f"reservation.{status.lower()}",
        "reservation",
        reservation["reservation_id"],
        version,
        {"reason_code": reason},
    )
    return reservation | changes


def sweep(scope, event):
    reservations = scope.db.all(
        """SELECT * FROM vk_demo.reservations WHERE sandbox_id=%s AND epoch=%s AND status='ACTIVE'
           AND expires_at<=%s ORDER BY expires_at,reservation_id""",
        scope.sandbox_id,
        scope.epoch,
        scope.now,
    )
    for reservation in reservations:
        release(scope, reservation, "EXPIRED", "EXPIRED", event)


def reserve(scope, body, event):
    quote = scope.require("quotes", quote_id=body["quote_id"])
    if body["accepted_quote_hash"] != quote["quote_hash"]:
        raise Problem("ACCEPTED_QUOTE_HASH_MISMATCH")
    previous = scope.scoped("reservations", quote_id=quote["quote_id"])
    if previous:
        return reservation_view(scope, previous)
    if quote["status"] == "EXPIRED" or (quote["status"] == "ACTIVE" and scope.now >= quote["expires_at"]):
        set_quote_status(scope, quote, "EXPIRED")
        raise Problem("QUOTE_EXPIRED", refresh=True)
    if quote["status"] == "STALE":
        raise Problem("QUOTE_STALE", refresh=True)
    revision = scope.require("scenario_revisions", scenario_revision_id=quote["scenario_revision_id"])
    if revision["status"] != "PUBLISHED":
        raise Problem("TEMPLATE_WITHDRAWN", refresh=True)
    terms = quote["immutable_terms"]
    for line in terms["lines"]:
        product = scope.require("products", sku_id=line["sku_id"])
        if not product["active"]:
            raise Problem("PRODUCT_WITHDRAWN", refresh=True)
        if product["product_version"] != line["product_version"]:
            set_quote_status(scope, quote, "STALE")
            raise Problem("QUOTE_STALE", refresh=True)
    if quote["status"] != "ACTIVE":
        raise Problem("INVALID_STATE")
    if latest_fulfillment_version(scope) != quote["fulfillment_version"]:
        set_quote_status(scope, quote, "STALE")
        raise Problem("QUOTE_STALE", refresh=True)
    lead = lead_days(terms)
    earliest = scope.date + timedelta(days=lead)
    if earliest != quote["earliest_date"] or (quote["needed_by"] and earliest > quote["needed_by"]):
        set_quote_status(scope, quote, "STALE")
        raise Problem("QUOTE_STALE", reasons=["BUSINESS_DATE_ROLLOVER"], refresh=True)
    inventory = lock_inventory(scope, [line["sku_id"] for line in terms["lines"]])
    errors = []
    for i, line in enumerate(terms["lines"]):
        row = inventory[line["sku_id"]]
        if row["stock_on_hand_packs"] - row["reserved_packs"] < line["pack_count"]:
            errors.append(field_error(f"lines[{i}].pack_count", "STOCK_CHANGED"))
    if errors:
        raise Problem("STOCK_CHANGED", fields=errors, refresh=True)
    reservation = {
        "reservation_id": uuid4(),
        "quote_id": quote["quote_id"],
        "accepted_quote_hash": quote["quote_hash"],
        "status": "ACTIVE",
        "aggregate_version": 1,
        "immutable_terms": terms,
        "created_at": scope.now,
        "expires_at": expiry(scope.now, RESERVATION_TTL, terms["needed_by"], lead),
        "completed_at": None,
        "reason_code": None,
    }
    scope.insert("reservations", **reservation)
    for line in sorted(terms["lines"], key=lambda item: item["sku_id"]):
        sku, packs = line["sku_id"], line["pack_count"]
        scope.insert(
            "reservation_lines",
            reservation_id=reservation["reservation_id"],
            quote_id=quote["quote_id"],
            sku_id=sku,
            pack_count=packs,
        )
        change_inventory(scope, inventory[sku], reserved=packs)
    set_quote_status(scope, quote, "RESERVED")
    event(
        scope,
        "reservation.created",
        "reservation",
        reservation["reservation_id"],
        1,
        {
            "quote_id": str(quote["quote_id"]),
            "accepted_quote_hash": quote["quote_hash"],
        },
    )
    return reservation_view(scope, reservation)


def cancel_reservation(scope, reservation_id, event):
    reservation = scope.require("reservations", reservation_id=reservation_id)
    if reservation["status"] == "CONFIRMED":
        raise Problem("INVALID_STATE")
    return reservation_view(scope, release(scope, reservation, "CANCELED", "BUYER_CANCELED", event))


def confirm(scope, body, event):
    reservation = scope.require("reservations", reservation_id=body["reservation_id"])
    if body["accepted_quote_hash"] != reservation["accepted_quote_hash"]:
        raise Problem("ACCEPTED_QUOTE_HASH_MISMATCH")
    status = reservation["status"]
    if status == "CONFIRMED":
        return order_view(scope.require("orders", reservation_id=reservation["reservation_id"]))
    if status == "EXPIRED" or (status == "ACTIVE" and scope.now >= reservation["expires_at"]):
        raise Problem("RESERVATION_EXPIRED", refresh=True)
    if status != "ACTIVE":
        code = reservation["reason_code"]
        if status == "INVALIDATED" and code in ("PRODUCT_WITHDRAWN", "TEMPLATE_WITHDRAWN"):
            raise Problem(code, refresh=True)
        raise Problem("INVALID_STATE")
    terms = {field: reservation["immutable_terms"][field] for field in COPY_FIELDS}
    inventory = lock_inventory(scope, [line["sku_id"] for line in terms["lines"]])
    for line in sorted(terms["lines"], key=lambda item: item["sku_id"]):
        row, count = inventory[line["sku_id"]], line["pack_count"]
        if min(row["reserved_packs"], row["stock_on_hand_packs"]) < count:
            raise Problem("INTERNAL_INVARIANT_FAILED")
        change_inventory(scope, row, on_hand=-count, reserved=-count, returnable=count)
    order_id = uuid4()
    order = {
        "order_id": order_id,
        "order_number": "TEST-" + order_id.hex,
        "reservation_id": reservation["reservation_id"],
        "quote_id": reservation["quote_id"],
        "status": "TEST_CONFIRMED",
        "aggregate_version": 1,
        "immutable_terms": terms,
        "currency": terms["currency"],
        "subtotal_minor": terms["subtotal_minor"],
        "shipping_minor": terms["shipping_minor"],
        "total_minor": terms["total_minor"],
        "created_at": scope.now,
        "canceled_at": None,
    }
    scope.insert("orders", **order)
    for line in terms["lines"]:
        scope.insert(
            "order_lines",
            order_id=order_id,
            quote_id=order["quote_id"],
            sku_id=line["sku_id"],
            pack_count=line["pack_count"],
        )
    scope.update(
        "reservations",
        {
            "status": "CONFIRMED",
            "completed_at": scope.now,
            "aggregate_version": reservation["aggregate_version"] + 1,
        },
        reservation_id=reservation["reservation_id"],
    )
    quote = scope.require("quotes", quote_id=order["quote_id"])
    set_quote_status(scope, quote, "CONFIRMED")
    bump_catalog(scope)
    event(
        scope,
        "order.test_confirmed",
        "order",
        order_id,
        1,
        {
            "total_minor": order["total_minor"],
            "reservation_id": str(reservation["reservation_id"]),
            "quote_id": str(quote["quote_id"]),
            "accepted_quote_hash": reservation["accepted_quote_hash"],
            "solution_hash": quote["solution_hash"],
            "scenario_revision_id": str(quote["scenario_revision_id"]),
            "product_versions": {line["sku_id"]: line["product_version"] for line in terms["lines"]},
        },
    )
    return order_view(order)


def cancel_order(scope, order_id, event):
    order = scope.require("orders", order_id=order_id)
    if order["status"] == "CANCELED":
        return order_view(order)
    lines = order["immutable_terms"]["lines"]
    inventory = lock_inventory(scope, [line["sku_id"] for line in lines])
    for line in sorted(lines, key=lambda item: item["sku_id"]):
        row, packs = inventory[line["sku_id"]], line["pack_count"]
        if row["returnable_packs"] < packs:
            raise Problem("INTERNAL_INVARIANT_FAILED")
        change_inventory(scope, row, on_hand=packs, returnable=-packs)
    version = order["aggregate_version"] + 1
    changes = {"status": "CANCELED", "canceled_at": scope.now, "aggregate_version": version}
    scope.update("orders", changes, order_id=order_id)
    set_quote_status(scope, scope.require("quotes", quote_id=order["quote_id"]), "CANCELED")
    bump_catalog(scope)
    event(
        scope,
        "order.test_canceled",
        "order",
        order_id,
        version,
        {
            "total_minor": order["total_minor"],
            "reservation_id": str(order["reservation_id"]),
            "returned_packs": {line["sku_id"]: line["pack_count"] for line in lines},
        },
    )
    return order_view(order | changes)


def invalidate(scope, quotes, reason, event):
    count = 0
    for quote in quotes:
        reservation = scope.scoped("reservations", quote_id=quote["quote_id"], status="ACTIVE")
        if reservation:
            release(scope, reservation, "INVALIDATED", reason, event)
            count += 1
        elif quote["status"] == "ACTIVE":
            set_quote_status(scope, quote, "EXPIRED" if quote["expires_at"] <= scope.now else "CANCELED")
    return count


def withdraw_product(scope, sku_id, body, event):
    product = scope.require("products", sku_id=sku_id)
    if product["product_version"] != body["expected_version"]:
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    if not product["active"]:
        raise Problem("PRODUCT_WITHDRAWN")
    update_product(scope, product, {"active": False})
    quotes = scope.db.all(
        """SELECT q.* FROM vk_demo.quotes q JOIN vk_demo.quote_lines l USING(sandbox_id,epoch,quote_id)
           WHERE q.sandbox_id=%s AND q.epoch=%s AND l.sku_id=%s AND q.status IN ('ACTIVE','RESERVED')
           ORDER BY q.quote_id""",
        scope.sandbox_id,
        scope.epoch,
        sku_id,
    )
    count = invalidate(scope, quotes, "PRODUCT_WITHDRAWN", event)
    mutation_id = event(
        scope,
        "catalog.updated",
        "catalog",
        "catalog",
        bump_catalog(scope),
        {"sku_id": sku_id, "active": False},
    )
    return {"resource_id": str(mutation_id), "status": "WITHDRAWN", "invalidated_reservations": count}


def withdraw_revision(scope, revision_id, body, event):
    row = scope.require("scenario_revisions", scenario_revision_id=revision_id)
    if row["revision_number"] != body["expected_version"]:
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    if row["status"] == "WITHDRAWN":
        raise Problem("TEMPLATE_WITHDRAWN")
    scope.update(
        "scenario_revisions",
        {"status": "WITHDRAWN", "withdrawn_at": scope.now},
        scenario_revision_id=revision_id,
    )
    quotes = scope.db.all(
        """SELECT * FROM vk_demo.quotes WHERE sandbox_id=%s AND epoch=%s AND scenario_revision_id=%s
           AND status IN ('ACTIVE','RESERVED') ORDER BY quote_id""",
        scope.sandbox_id,
        scope.epoch,
        revision_id,
    )
    count = invalidate(scope, quotes, "TEMPLATE_WITHDRAWN", event)
    event(scope, "template.withdrawn", "template", revision_id, 2, {"invalidated_reservations": count})
    return {"resource_id": revision_id, "status": "WITHDRAWN", "invalidated_reservations": count}


def verify_quote(scope, quote_id):
    quote = scope.require("quotes", quote_id=quote_id)
    run = scope.require("solve_runs", solve_run_id=quote["solve_run_id"])
    source = run["immutable_input"]
    snapshot, intent = source["snapshot"], source["purchase_request"]
    calculation = run["diagnostics"]["result"]
    terms = quote["immutable_terms"]
    persisted = scope.db.all(
        """SELECT line_snapshot FROM vk_demo.quote_lines WHERE sandbox_id=%s AND epoch=%s AND quote_id=%s""",
        scope.sandbox_id,
        scope.epoch,
        quote_id,
    )
    checks = [
        ("SNAPSHOT_HASH", digest(snapshot) == quote["snapshot_hash"], "Хеш снимка планирования."),
        ("REQUEST_HASH", digest(request_payload(intent)) == quote["request_hash"], "Хеш запроса."),
        (
            "QUOTE_HASH",
            digest(
                quote_payload(
                    sandbox_id=str(scope.sandbox_id),
                    epoch=scope.epoch,
                    business_date=iso(quote["business_date"]),
                    quote=terms,
                )
            )
            == quote["quote_hash"],
            "Хеш согласованных условий.",
        ),
        (
            "SOLUTION_HASH",
            digest(
                solution_payload(
                    algorithm_version=quote["algorithm_version"],
                    snapshot=snapshot,
                    request=intent,
                    result=calculation,
                )
            )
            == quote["solution_hash"],
            "Хеш чистого решения.",
        ),
        (
            "FEASIBILITY",
            not verify_solution(snapshot, intent, calculation),
            "Независимая проверка комплекта.",
        ),
        (
            "LINES",
            {row["line_snapshot"]["slot_id"]: row["line_snapshot"] for row in persisted}
            == {line["slot_id"]: line for line in terms["lines"]}
            and terms["lines"] == commercial_lines(snapshot, calculation, intent),
            "Состав и суммы строк.",
        ),
    ]
    wire = [{"check_id": code, "passed": passed, "message": message} for code, passed, message in checks]
    passed = all(item["passed"] for item in wire)
    scope.insert(
        "verification_runs",
        verification_id=uuid4(),
        quote_id=quote_id,
        snapshot_hash=quote["snapshot_hash"],
        verifier_version="verifier-1.0.0",
        passed=passed,
        checks=wire,
        checked_at=scope.now,
    )
    return {
        "quote_id": quote_id,
        "snapshot_hash": quote["snapshot_hash"],
        "checked_at": iso(scope.now),
        "passed": passed,
        "checks": wire,
    }
