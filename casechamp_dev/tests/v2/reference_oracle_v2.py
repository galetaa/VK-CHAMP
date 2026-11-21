"""Independent exact reference for specification fixtures, not a web backend."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from itertools import product

FEES = {"PICKUP": 0, "COURIER_ZONE_A": 30000}


def integer(value, minimum=0, maximum=100_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("INTEGER_REQUIRED")
    return value


def canonical_bytes(value):
    def check(x):
        if isinstance(x, float):
            raise ValueError("FLOAT_NOT_ALLOWED")
        if isinstance(x, dict):
            if not all(type(k) is str for k in x):
                raise ValueError("STRING_KEYS_REQUIRED")
            for v in x.values():
                check(v)
        elif isinstance(x, list):
            for v in x:
                check(v)

    check(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def payload_hash(value):
    return sha256(canonical_bytes(value)).hexdigest()


def core_price(catalog, lines, copies, fulfillment):
    integer(copies, 1, 10)
    if fulfillment not in FEES:
        raise ValueError("FULFILLMENT_INVALID")
    if not 1 <= len(lines) <= 20 or len({x["sku"] for x in lines}) != len(lines):
        raise ValueError("LINES_INVALID")
    by_sku = {x["sku"]: x for x in catalog}
    result = []
    for line in sorted(lines, key=lambda x: x["sku"]):
        qty = integer(line["qty"], 1, 99) * copies
        item = by_sku.get(line["sku"])
        if item is None or not item["active"]:
            raise ValueError("SKU_UNAVAILABLE")
        price = integer(item["unit_price_minor"], 1)
        result.append(
            {"sku": item["sku"], "qty": qty, "unit_price_minor": price, "line_total_minor": qty * price}
        )
    if sum(x["qty"] for x in result) > 990:
        raise ValueError("TOTAL_QUANTITY_LIMIT")
    subtotal = sum(x["line_total_minor"] for x in result)
    if subtotal + FEES[fulfillment] > 100_000_000:
        raise ValueError("TOTAL_PRICE_LIMIT")
    return {
        "lines": result,
        "subtotal_minor": subtotal,
        "fulfillment_fee_minor": FEES[fulfillment],
        "total_minor": subtotal + FEES[fulfillment],
        "currency": "RUB",
    }


def bundle_solve(catalog, template, request):
    guests = integer(request["guests"], 1, 12)
    budget = integer(request["budget_minor"], 1, 100_000_000)
    days = integer(request["requested_days"], 1, 30)
    mode = request["fulfillment"]
    if mode not in FEES:
        raise ValueError("FULFILLMENT_INVALID")
    owned = request["owned"]
    if not isinstance(owned, list) or len(owned) != len(set(owned)) or any(x != "TABLECLOTH" for x in owned):
        raise ValueError("OWNED_INVALID")
    slots = sorted(template["slots"], key=lambda x: x["slot_id"])
    if not 1 <= len(slots) <= 5:
        raise ValueError("SLOT_LIMIT")
    if len({x["slot_id"] for x in slots}) != len(slots):
        raise ValueError("DUPLICATE_SLOT")
    all_skus = [sku for slot in slots for sku in slot["alternatives"]]
    if len(all_skus) != len(set(all_skus)):
        raise ValueError("DUPLICATE_SKU")
    by_sku = {x["sku"]: x for x in catalog}
    alternatives = []
    for slot in slots:
        if not 1 <= len(slot["alternatives"]) <= 8:
            raise ValueError("ALTERNATIVE_LIMIT")
        if slot["slot_id"] == "TABLECLOTH" and "TABLECLOTH" in owned:
            continue
        units = guests if slot["need"] == "PER_GUEST" else integer(slot["fixed_units"], 1, 99)
        candidates = []
        for sku in sorted(slot["alternatives"]):
            item = by_sku[sku]
            pack = integer(item["units_per_pack"], 1, 100)
            qty = (units + pack - 1) // pack
            if (
                not item["active"]
                or item["ready_in_days"] > days
                or item["stock_on_hand"] - item.get("stock_reserved", 0) < qty
            ):
                continue
            candidates.append(
                {
                    "slot_id": slot["slot_id"],
                    "sku": sku,
                    "qty": qty,
                    "required_units": units,
                    "units_per_pack": pack,
                    "surplus_units": qty * pack - units,
                    "preference_rank": integer(item["preference_rank"], 0, 100),
                    "unit_price_minor": integer(item["unit_price_minor"], 1),
                    "line_total_minor": qty * item["unit_price_minor"],
                }
            )
        alternatives.append(candidates)
    if not alternatives or any(not x for x in alternatives):
        return {"status": "NO_SOLUTION", "reason": "NO_FEASIBLE_COMBINATION"}
    feasible = []
    enumerated = 0
    for composition in product(*alternatives):
        enumerated += 1
        subtotal = sum(x["line_total_minor"] for x in composition)
        total = subtotal + FEES[mode]
        if total > budget:
            continue
        objective = (
            sum(x["preference_rank"] for x in composition),
            sum(x["surplus_units"] for x in composition),
            total,
            tuple(x["sku"] for x in composition),
        )
        feasible.append((objective, composition, subtotal))
    if not feasible:
        return {"status": "NO_SOLUTION", "reason": "NO_FEASIBLE_COMBINATION"}
    objective, composition, subtotal = min(feasible, key=lambda x: x[0])
    return {
        "status": "SOLVED",
        "lines": list(composition),
        "subtotal_minor": subtotal,
        "fulfillment_fee_minor": FEES[mode],
        "total_minor": objective[2],
        "preference_penalty": objective[0],
        "surplus_units": objective[1],
        "sku_tiebreak": list(objective[3]),
        "cheapest_feasible_minor": min(x[0][2] for x in feasible),
        "enumerated_compositions": enumerated,
        "currency": "RUB",
    }


def is_expired(now, expires_at):
    def dt(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    return dt(now) >= dt(expires_at)


def ledger_transition(stock, lines, action):
    """Serial stock arithmetic only: no DB transactions or race execution."""
    out = deepcopy(stock)
    for line in lines:
        row = out[line["sku"]]
        qty = integer(line["qty"], 1, 990)
        if action == "reserve":
            if row["on_hand"] - row["reserved"] < qty:
                raise ValueError("STOCK_INSUFFICIENT")
            row["reserved"] += qty
        elif action in ("expire", "cancel_reservation"):
            if row["reserved"] < qty:
                raise ValueError("INVALID_LEDGER")
            row["reserved"] -= qty
        elif action == "confirm":
            if row["reserved"] < qty or row["on_hand"] < qty:
                raise ValueError("INVALID_LEDGER")
            row["reserved"] -= qty
            row["on_hand"] -= qty
        elif action == "cancel_order":
            row["on_hand"] += qty
        else:
            raise ValueError("ACTION_INVALID")
        if not 0 <= row["reserved"] <= row["on_hand"]:
            raise ValueError("INVALID_LEDGER")
    return out
