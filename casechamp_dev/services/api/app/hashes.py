"""Точный эталон hash payload. Принимает уже провалидированные доменные объекты.
Не является API и не выполняет авторизацию, решатель или транзакции.
"""

from copy import deepcopy
from datetime import datetime, timezone

from app.common import SLOTS

HASH_VERSION = "1.0.0"
PLANNER_PRODUCT_FIELDS = (
    "sku_id",
    "slot_id",
    "color",
    "pack_units",
    "price_minor",
    "currency",
    "available_packs",
    "active",
    "handling_days",
    "length_cm",
    "width_cm",
)


def utc_milliseconds(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_request(request):
    """Shape/semantic validation precedes this function; invalid input is not repaired."""
    out = deepcopy(request)
    out.pop("client_run_id", None)
    slots = {s["slot_id"]: deepcopy(s) for s in out["slots"]}
    for sid in SLOTS:
        s = slots[sid]
        if not s["enabled"]:
            s.update(
                owned_units=0, selection_mode="APPROVED_ALTERNATIVES", allowed_colors=[], preferred_color=None
            )
        else:
            s["allowed_colors"] = sorted(s["allowed_colors"])
    out["slots"] = [slots[s] for s in SLOTS]
    cloth = slots["tablecloth"]
    if not cloth["enabled"] or cloth["owned_units"] >= 1:
        out["table_dimensions"] = None
    return out


def canonical_snapshot(snapshot):
    out = deepcopy(snapshot)
    out["catalog"] = sorted(out["catalog"], key=lambda p: p["sku_id"])
    mapping = {m["slot_id"]: deepcopy(m) for m in out["template_slots"]}
    for m in mapping.values():
        m["alternative_sku_ids"] = sorted(m["alternative_sku_ids"])
    out["template_slots"] = [mapping[s] for s in SLOTS]
    return out


def request_payload(request):
    return {"hash_version": HASH_VERSION, "request": normalize_request(request)}


def quote_payload(*, sandbox_id, epoch, business_date, quote):
    q = deepcopy(quote)
    for field in ("status", "reservation_id", "quote_hash", "reasons"):
        q.pop(field, None)
    q["purchase_request"] = normalize_request(q["purchase_request"])
    return {
        "hash_version": HASH_VERSION,
        "spec_version": "1.0.0",
        "sandbox_id": sandbox_id,
        "epoch": epoch,
        "business_date": business_date,
        "quote": q,
    }


def solution_payload(*, algorithm_version, snapshot, request, result):
    """Contains only planner facts, no session/resource UUID or storage timestamps."""
    r = normalize_request(request)
    r.pop("scenario_revision_id")
    snap = canonical_snapshot(snapshot)
    catalog = [{k: p[k] for k in PLANNER_PRODUCT_FIELDS} for p in snap["catalog"]]
    mappings = [
        {k: m[k] for k in ("slot_id", "primary_sku_id", "alternative_sku_ids")}
        for m in snap["template_slots"]
    ]
    business = {
        "business_date": snap["business_date"],
        "currency": snap["currency"],
        "catalog": catalog,
        "template_slots": mappings,
        "fulfillment_terms": {
            "pickup_fee_minor": 0,
            "pickup_transit_days": 0,
            "delivery_fee_minor": 30000,
            "delivery_transit_days": 1,
            "delivery_zones": ["CENTER", "NORTH"],
        },
    }
    return {
        "hash_version": HASH_VERSION,
        "algorithm_version": algorithm_version,
        "business_snapshot": business,
        "request": r,
        "result": deepcopy(result),
    }


def idempotency_payload(*, operation_id, method, path, body):
    b = deepcopy(body)
    if isinstance(b, dict):
        b.pop("client_run_id", None)
    return {
        "hash_version": HASH_VERSION,
        "operation_id": operation_id,
        "method": method.upper(),
        "path": path,
        "body": b,
    }
