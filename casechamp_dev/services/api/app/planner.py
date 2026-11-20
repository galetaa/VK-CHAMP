"""Pure planner. Commercial snapshots and transport DTOs are assembled by the API."""

import os
from dataclasses import dataclass
from datetime import date, timedelta
from multiprocessing import get_all_start_methods, get_context, set_forkserver_preload
from time import monotonic

from ortools.sat.python import cp_model

from app.common import REQUIRED, SLOTS, Problem, field_error
from app.hashes import normalize_request

WATCHDOG_SECONDS = 5
PHASE_TIME = 0.2
# The dedicated fork server preloads OR-Tools before forking request workers.
# It never carries a live API database connection or a solved model.
PROCESS_METHOD = "forkserver" if "forkserver" in get_all_start_methods() else "spawn"
if PROCESS_METHOD == "forkserver":
    set_forkserver_preload(["app.planner"])
REASON_ORDER = (
    "NO_APPROVED_SKU",
    "PRIMARY_UNAVAILABLE",
    "COLOR_RESTRICTION",
    "GEOMETRY_RESTRICTION",
    "FULFILLMENT_UNSUPPORTED",
    "DEADLINE_UNMET",
    "INSUFFICIENT_STOCK",
    "PRODUCT_INACTIVE",
)
EXPLANATIONS = {
    "NO_APPROVED_SKU": "Для позиции нет подтверждённого товара.",
    "PRIMARY_UNAVAILABLE": "Основной товар недоступен на заданных условиях.",
    "COLOR_RESTRICTION": "Цвет товара не входит в разрешённый список.",
    "GEOMETRY_RESTRICTION": "Скатерть не подходит по размерам.",
    "FULFILLMENT_UNSUPPORTED": "Способ получения недоступен.",
    "DEADLINE_UNMET": "Товар не успевает к выбранной дате.",
    "INSUFFICIENT_STOCK": "В наличии недостаточно упаковок.",
    "PRODUCT_INACTIVE": "Товар снят с продажи.",
    "BUDGET_TOO_LOW": "Минимальная стоимость набора превышает бюджет.",
}


def requirements(request):
    required = {
        slot["slot_id"]: (1 if slot["slot_id"] == "tablecloth" else request["guest_count"])
        if slot["enabled"]
        else 0
        for slot in request["slots"]
    }
    owned = {
        slot["slot_id"]: min(slot["owned_units"], required[slot["slot_id"]]) for slot in request["slots"]
    }
    return required, owned


def validate_intent(request, today):
    slots = {slot["slot_id"]: slot for slot in request["slots"]}
    if len(slots) != 6 or set(slots) != set(SLOTS):
        raise Problem("VALIDATION_ERROR", fields=[field_error("slots")])
    for i, slot in enumerate(request["slots"]):
        location = f"slots[{i}]"
        if not slot["enabled"]:
            if slot["slot_id"] in REQUIRED:
                raise Problem("VALIDATION_ERROR", fields=[field_error(location + ".enabled")])
            continue
        colors, target = slot["allowed_colors"], slot["preferred_color"]
        if colors and target is not None and target not in colors:
            code = "CONTRADICTORY_PREFERENCES"
            raise Problem(
                code,
                fields=[
                    field_error(location + "." + key, code) for key in ("allowed_colors", "preferred_color")
                ],
            )
    if request["needed_by"] is not None:
        due = date.fromisoformat(request["needed_by"])
        if not today <= due <= today + timedelta(days=30):
            raise Problem("VALIDATION_ERROR", fields=[field_error("needed_by")])
    required, owned = requirements(request)
    if required == owned:
        return
    fulfillment = request["fulfillment"]
    if fulfillment["mode"] == "PICKUP" and fulfillment["zone"] is not None:
        raise Problem("VALIDATION_ERROR", fields=[field_error("fulfillment.zone")])
    if fulfillment["mode"] == "LOCAL_DELIVERY":
        if fulfillment["zone"] is None:
            raise Problem("VALIDATION_ERROR", fields=[field_error("fulfillment.zone")])
        if fulfillment["zone"] not in ("CENTER", "NORTH"):
            raise Problem("UNSUPPORTED_ZONE", fields=[field_error("fulfillment.zone", "UNSUPPORTED_ZONE")])
    if required["tablecloth"] > owned["tablecloth"]:
        if request["table_dimensions"] is None:
            raise Problem("VALIDATION_ERROR", fields=[field_error("table_dimensions")])


def normalized_intent(request, today):
    validate_intent(request, today)
    return normalize_request(request) | {"client_run_id": request["client_run_id"]}


def cloth_fit(product, dimensions):
    cloth_length, cloth_width = product["length_cm"], product["width_cm"]
    if dimensions is None or cloth_length is None or cloth_width is None:
        return None
    for orientation, length, width in (
        ("NORMAL", cloth_length, cloth_width),
        ("ROTATED", cloth_width, cloth_length),
    ):
        dl = length - dimensions["length_cm"]
        dw = width - dimensions["width_cm"]
        if 30 <= dl <= 60 and 30 <= dw <= 60:
            return {
                "orientation": orientation,
                "drop_length_mm": dl * 5,
                "drop_width_mm": dw * 5,
                "cloth_length_cm": cloth_length,
                "cloth_width_cm": cloth_width,
                "table_dimensions": dimensions,
            }
    return None


@dataclass
class Plan:
    result: dict
    reasons: list
    phases: list
    error: str | None = None


def reason(code, slot_id=None, sku_id=None):
    return {"code": code, "slot_id": slot_id, "sku_id": sku_id, "message": EXPLANATIONS[code]}


def pick_options(snapshot, request, required, owned, today, transit):
    products = {product["sku_id"]: product for product in snapshot["catalog"]}
    templates = {slot["slot_id"]: slot for slot in snapshot["template_slots"]}
    preferences = {slot["slot_id"]: slot for slot in request["slots"]}
    deadline = date.fromisoformat(request["needed_by"]) if request["needed_by"] else None
    options = {}
    diagnostics = []
    for slot in SLOTS:
        missing = required[slot] - owned[slot]
        if not missing:
            continue
        template, preference = templates[slot], preferences[slot]
        primary = template["primary_sku_id"]
        approved = {primary} if primary else set()
        if preference["selection_mode"] == "APPROVED_ALTERNATIVES":
            approved.update(template["alternative_sku_ids"])
        candidates = []
        rejected = []
        if not approved:
            rejected.append(reason("NO_APPROVED_SKU", slot))
        for sku in sorted(approved):
            product = products.get(sku)
            if product is None or product["slot_id"] != slot:
                rejected.append(
                    reason("PRIMARY_UNAVAILABLE" if sku == primary else "NO_APPROVED_SKU", slot, sku)
                )
                continue
            packs = (missing + product["pack_units"] - 1) // product["pack_units"]
            violations = []
            if not product["active"]:
                violations.append("PRODUCT_INACTIVE")
            if packs > product["available_packs"]:
                violations.append("INSUFFICIENT_STOCK")
            if preference["allowed_colors"] and product["color"] not in preference["allowed_colors"]:
                violations.append("COLOR_RESTRICTION")
            if slot == "tablecloth" and not cloth_fit(product, request["table_dimensions"]):
                violations.append("GEOMETRY_RESTRICTION")
            if deadline and today + timedelta(days=product["handling_days"] + transit) > deadline:
                violations.append("DEADLINE_UNMET")
            if violations:
                if sku == primary:
                    violations.append("PRIMARY_UNAVAILABLE")
                rejected.extend(reason(code, slot, sku) for code in violations)
                continue
            units = packs * product["pack_units"]
            candidates.append(
                {
                    "slot_id": slot,
                    "sku_id": sku,
                    "pack_count": packs,
                    "pack_units": product["pack_units"],
                    "delivered_units": units,
                    "required_units": required[slot],
                    "effective_owned_units": owned[slot],
                    "excess_units": units - missing,
                    "unit_pack_price_minor": product["price_minor"],
                    "line_total_minor": packs * product["price_minor"],
                    "handling_days": product["handling_days"],
                    "substitution": sku != primary,
                    "soft_color_mismatch": preference["preferred_color"] is not None
                    and preference["preferred_color"] != product["color"],
                }
            )
        options[slot] = candidates
        if not candidates:
            diagnostics.extend(
                sorted(rejected, key=lambda item: (REASON_ORDER.index(item["code"]), item["sku_id"] or ""))
            )
    return options, diagnostics


def infeasible(code, slot, minimum):
    return {"outcome": "INFEASIBLE", "reason_code": code, "slot_id": slot, "minimum_total_minor": minimum}


def solve(snapshot, request, *, prove_sku_ties=True):
    today = date.fromisoformat(snapshot["business_date"])
    required, owned = requirements(request)
    if required == owned:
        return Plan(
            {
                "outcome": "NO_PURCHASE_REQUIRED",
                "lines": [],
                "subtotal_minor": 0,
                "shipping_minor": 0,
                "total_minor": 0,
                "objective": None,
                "earliest_date": None,
                "minimum_total_minor": 0,
            },
            [],
            [],
        )
    transit = int(request["fulfillment"]["mode"] == "LOCAL_DELIVERY")
    shipping = transit * 30000
    options, reasons = pick_options(snapshot, request, required, owned, today, transit)
    for slot, choices in options.items():
        if not choices:
            return Plan(infeasible("NO_CANDIDATE", slot, None), reasons, [])
    minimum = shipping + sum(min(c["line_total_minor"] for c in choices) for choices in options.values())
    if minimum > request["budget_minor"]:
        return Plan(infeasible("BUDGET_TOO_LOW", None, minimum), [reason("BUDGET_TOO_LOW")], [])

    model = cp_model.CpModel()
    variables = {
        slot: [model.new_bool_var(f"{slot}:{c['sku_id']}") for c in choices]
        for slot, choices in options.items()
    }
    for choices in variables.values():
        model.add_exactly_one(choices)

    def weighted(field):
        return sum(
            int(c[field]) * x
            for slot, choices in options.items()
            for c, x in zip(choices, variables[slot], strict=True)
        )

    total_expr = weighted("line_total_minor") + shipping
    model.add(total_expr <= request["budget_minor"])
    objectives = [
        (field, weighted(field)) for field in ("substitution", "soft_color_mismatch", "excess_units")
    ]
    objectives.append(("total_minor", total_expr))
    if prove_sku_ties:
        objectives.extend(
            (f"sku_rank:{slot}", sum(rank * x for rank, x in enumerate(choices)))
            for slot, choices in variables.items()
        )
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.max_deterministic_time = PHASE_TIME
    phases = []
    for index, (name, expression) in enumerate(objectives):
        model.minimize(expression)
        status = solver.solve(model)
        phase = {
            "phase": name,
            "status": solver.status_name(status),
            "deterministic_time": solver.response_proto.deterministic_time,
            "wall_time": solver.wall_time,
        }
        phases.append(phase)
        if status == cp_model.INFEASIBLE and index == 0:
            # The exact minimum above already established feasibility in this model.
            return Plan({}, [], phases, "INTERNAL_INVARIANT_FAILED")
        if status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
            return Plan({}, [], phases, "COMPUTATION_LIMIT")
        if status != cp_model.OPTIMAL:
            return Plan({}, [], phases, "INTERNAL_INVARIANT_FAILED")
        optimum = solver.value(expression)
        phase["optimum"] = optimum
        model.add(expression == optimum)

    lines = [
        c
        for slot, choices in options.items()
        for c, x in zip(choices, variables[slot], strict=True)
        if solver.boolean_value(x)
    ]
    sku_by_slot = {line["slot_id"]: line["sku_id"] for line in lines}
    subtotal = sum(line["line_total_minor"] for line in lines)
    earliest = today + timedelta(days=transit + max(line["handling_days"] for line in lines))
    return Plan(
        {
            "outcome": "QUOTED",
            "lines": lines,
            "subtotal_minor": subtotal,
            "shipping_minor": shipping,
            "total_minor": subtotal + shipping,
            "objective": {
                "substituted_slots": sum(line["substitution"] for line in lines),
                "soft_color_mismatches": sum(line["soft_color_mismatch"] for line in lines),
                "excess_units": sum(line["excess_units"] for line in lines),
                "total_minor": subtotal + shipping,
                "sku_tuple": [sku_by_slot.get(slot, "") for slot in SLOTS],
            },
            "earliest_date": earliest.isoformat(),
            "minimum_total_minor": minimum,
        },
        [],
        phases,
    )


def _child(writer, snapshot, request):
    for name in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "MAINTENANCE_DATABASE_URL", "SESSION_SECRET"):
        os.environ.pop(name, None)
    try:
        writer.send(solve(snapshot, request))
    finally:
        writer.close()


def run_planner(snapshot, request, timeout=WATCHDOG_SECONDS):
    context = get_context(PROCESS_METHOD)
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(writer, snapshot, request), daemon=True)
    started = monotonic()
    process.start()
    writer.close()
    try:
        if reader.poll(max(0, timeout - (monotonic() - started))):
            try:
                return reader.recv()
            except EOFError:
                pass
        return Plan({}, [], [], "COMPUTATION_ABORTED")
    finally:
        reader.close()
        process.join(0.05)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()
