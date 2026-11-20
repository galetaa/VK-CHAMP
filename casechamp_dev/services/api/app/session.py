import hashlib
import json
import secrets
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from app.catalog import new_product
from app.commerce import invalidate
from app.common import SLOTS, Problem, iso
from app.scenarios import create_scenario, publish


def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).digest()


def context(scope, settings):
    latest = scope.db.first(
        """SELECT scenario_revision_id FROM vk_demo.scenario_revisions WHERE sandbox_id=%s AND epoch=%s
           AND status='PUBLISHED' AND NOT is_simulation
           ORDER BY published_at DESC, scenario_revision_id DESC LIMIT 1""",
        scope.sandbox_id,
        scope.epoch,
    )
    return {
        "sandbox_id": str(scope.sandbox_id),
        "sandbox_epoch": scope.epoch,
        "expires_at": iso(scope.sandbox["expires_at"]),
        "business_date": iso(scope.date),
        "timezone": "Europe/Moscow",
        "capabilities": {
            "mode": "DEMO",
            "ml_mode": settings.ml_mode,
            "native_checkout": False,
            "live_payment": False,
        },
        "latest_scenario_revision_id": str(latest["scenario_revision_id"]) if latest else None,
        "constants": {
            "guest_min": 1,
            "guest_max": 12,
            "budget_min_minor": 50000,
            "budget_max_minor": 5000000,
            "quote_ttl_seconds": 900,
            "reservation_ttl_seconds": 600,
        },
    }


def seed(scope, settings, event):
    scope.insert(
        "sandbox_epochs", catalog_version=1, fulfillment_version=1, created_at=scope.now, closed_at=None
    )
    scope.insert(
        "fulfillment_terms",
        fulfillment_version=1,
        pickup_fee_minor=0,
        local_delivery_fee_minor=30000,
        pickup_transit_days=0,
        local_delivery_transit_days=1,
        timezone="Europe/Moscow",
        terms_snapshot={"delivery_zones": ["CENTER", "NORTH"]},
        created_at=scope.now,
    )
    if scope.sandbox["fixture_id"] == "EMPTY_V1":
        return
    data = json.loads((settings.fixture_root / "snapshot.json").read_text())
    products = data["catalog"]
    for product in products:
        new_product(scope, product | {"image_asset_id": None})
    for slot, original in zip(SLOTS, data["template_slots"], strict=True):
        src = next(product for product in products if product["sku_id"] == original["primary_sku_id"])
        new_product(
            scope,
            src
            | {
                "sku_id": f"SIM_{slot.upper()}",
                "stock_on_hand_packs": 12,
                "image_asset_id": None,
            },
        )
    scenario = create_scenario(
        scope,
        {
            "title": "Сервировка стола",
            "caption": "Демонстрационный набор сервировки на шесть позиций.",
            "asset_id": None,
        },
        data["template_slots"],
    )
    publish(scope, scenario["scenario_id"], scenario["draft_version"], event)


def prepare_sandbox(db, now, fixture_id):
    token = secrets.token_urlsafe(32)
    sandbox = {
        "sandbox_id": uuid4(),
        "session_token_hash": token_hash(token),
        "current_epoch": 1,
        "status": "ACTIVE",
        "fixture_id": fixture_id,
        "created_at": now,
        "expires_at": now + timedelta(hours=24),
    }
    db.insert("sandboxes", sandbox)
    return sandbox, token


def reset(scope, settings, body, event):
    if scope.epoch != body["expected_epoch"]:
        raise Problem("SANDBOX_EPOCH_MISMATCH", refresh=True)
    quotes = scope.db.all(
        """SELECT * FROM vk_demo.quotes WHERE sandbox_id=%s AND epoch=%s AND status IN ('ACTIVE','RESERVED')
           ORDER BY quote_id""",
        scope.sandbox_id,
        scope.epoch,
    )
    invalidate(scope, quotes, "RESET", event)
    scope.db.run(
        """UPDATE vk_demo.ml_jobs SET status='CANCELED',failure_code='RESET',finished_at=%s
           WHERE sandbox_id=%s AND epoch=%s AND status IN ('QUEUED','RUNNING')""",
        scope.now,
        scope.sandbox_id,
        scope.epoch,
    )
    scope.update("sandbox_epochs", {"closed_at": scope.now})
    epoch = scope.epoch + 1
    scope.db.update("sandboxes", {"current_epoch": epoch}, {"sandbox_id": scope.sandbox_id})
    new_scope = replace(scope, epoch=epoch, sandbox=scope.sandbox | {"current_epoch": epoch})
    seed(new_scope, settings, event)
    event(new_scope, "sandbox.reset", "sandbox", scope.sandbox_id, epoch, {"previous_epoch": scope.epoch})
    return context(new_scope, settings)
