"""Independent arithmetic verifier derived from the specification reference implementation."""

from datetime import date, timedelta

from app.common import SLOTS


def verify_solution(snapshot, request, result):
    """Independent feasibility/metadata verifier. Exact optimum is checked separately."""
    if not isinstance(result, dict) or result.get("outcome") not in {
        "QUOTED",
        "INFEASIBLE",
        "NO_PURCHASE_REQUIRED",
        "VALIDATION_ERROR",
    }:
        return ["MALFORMED_RESULT"]
    if result["outcome"] != "QUOTED":
        return []
    errors = []
    products = {x["sku_id"]: x for x in snapshot["catalog"]}
    requests = {x["slot_id"]: x for x in request["slots"]}
    templates = {x["slot_id"]: x for x in snapshot["template_slots"]}
    if not isinstance(result.get("lines"), list) or not result["lines"]:
        return ["EMPTY_QUOTE"]
    seen = set()
    subtotal = 0
    chosen = {}
    handling = []
    for line in result["lines"]:
        if not isinstance(line, dict):
            errors.append("MALFORMED_LINE")
            continue
        s = line.get("slot_id")
        if s not in requests:
            errors.append("UNKNOWN_SLOT")
            continue
        if s in seen:
            errors.append("DUPLICATE_SLOT")
        seen.add(s)
        p = products.get(line.get("sku_id"))
        q = requests[s]
        t = templates[s]
        if p is None:
            errors.append("UNKNOWN_SKU")
            continue
        if not p["active"] or p["slot_id"] != s:
            errors.append("INVALID_SKU")
        allowed = [t["primary_sku_id"]] + (
            t["alternative_sku_ids"] if q["selection_mode"] == "APPROVED_ALTERNATIVES" else []
        )
        if p["sku_id"] not in allowed:
            errors.append("UNAPPROVED_SKU")
        required = (1 if s == "tablecloth" else request["guest_count"]) if q["enabled"] else 0
        effective_owned = min(q["owned_units"], required)
        remaining = required - effective_owned
        count = (remaining + p["pack_units"] - 1) // p["pack_units"]
        expected = {
            "pack_count": count,
            "pack_units": p["pack_units"],
            "delivered_units": count * p["pack_units"],
            "required_units": required,
            "effective_owned_units": effective_owned,
            "excess_units": count * p["pack_units"] - remaining,
            "unit_pack_price_minor": p["price_minor"],
            "line_total_minor": count * p["price_minor"],
            "handling_days": p["handling_days"],
        }
        for field, val in expected.items():
            if type(line.get(field)) is not int or line[field] != val:
                errors.append("FIELD_" + field.upper())
        if count <= 0:
            errors.append("UNNEEDED_LINE")
        if count > p["available_packs"]:
            errors.append("STOCK")
        if q["allowed_colors"] and p["color"] not in q["allowed_colors"]:
            errors.append("COLOR")
        substitution = p["sku_id"] != t["primary_sku_id"]
        mismatch = q["preferred_color"] is not None and p["color"] != q["preferred_color"]
        if type(line.get("substitution")) is not bool or line["substitution"] != substitution:
            errors.append("SUBSTITUTION")
        if type(line.get("soft_color_mismatch")) is not bool or line["soft_color_mismatch"] != mismatch:
            errors.append("SOFT_COLOR")
        subtotal += expected["line_total_minor"]
        handling.append(p["handling_days"])
        chosen[s] = {
            "sku_id": p["sku_id"],
            "substitution": substitution,
            "soft_color_mismatch": mismatch,
            "excess_units": expected["excess_units"],
        }
        if s == "tablecloth":
            d = request["table_dimensions"]
            a, b = p["length_cm"], p["width_cm"]
            if not d or a is None or b is None:
                errors.append("DIMENSIONS")
            else:
                x, y = d["length_cm"], d["width_cm"]
                if not (
                    (30 <= a - x <= 60 and 30 <= b - y <= 60) or (30 <= b - x <= 60 and 30 <= a - y <= 60)
                ):
                    errors.append("DIMENSIONS")
    for s, q in requests.items():
        n = (1 if s == "tablecloth" else request["guest_count"]) if q["enabled"] else 0
        if n > q["owned_units"] and s not in seen:
            errors.append("MISSING_SLOT")
    if [x.get("slot_id") for x in result["lines"] if isinstance(x, dict)] != [s for s in SLOTS if s in seen]:
        errors.append("LINE_ORDER")
    fee = 0 if request["fulfillment"]["mode"] == "PICKUP" else 30000
    if (
        result.get("subtotal_minor") != subtotal
        or result.get("shipping_minor") != fee
        or result.get("total_minor") != subtotal + fee
    ):
        errors.append("TOTAL")
    if subtotal + fee > request["budget_minor"]:
        errors.append("BUDGET")
    expected_obj = {
        "substituted_slots": sum(x["substitution"] for x in chosen.values()),
        "soft_color_mismatches": sum(x["soft_color_mismatch"] for x in chosen.values()),
        "excess_units": sum(x["excess_units"] for x in chosen.values()),
        "total_minor": subtotal + fee,
        "sku_tuple": [chosen[s]["sku_id"] if s in chosen else "" for s in SLOTS],
    }
    if result.get("objective") != expected_obj:
        errors.append("OBJECTIVE")
    if handling:
        transit = 0 if request["fulfillment"]["mode"] == "PICKUP" else 1
        earliest = date.fromisoformat(snapshot["business_date"]) + timedelta(days=max(handling) + transit)
        if result.get("earliest_date") != earliest.isoformat():
            errors.append("EARLIEST_DATE")
        if request["needed_by"] and earliest > date.fromisoformat(request["needed_by"]):
            errors.append("DEADLINE")
    return sorted(set(errors))
