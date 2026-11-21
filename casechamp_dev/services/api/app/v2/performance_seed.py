"""Create a disposable v2 workload, scoped to a new demo sandbox."""

import json
from datetime import timedelta
from uuid import uuid4

from app.db import Clock, make_engine
from app.settings import Settings
from app.v2 import checkout, offers
from app.v2.core import Context, Store
from app.v2.security import Security, add_actor


def prepared_offer(ctx, mode, draft):
    empty = offers.new_offer(ctx, mode)
    saved = offers.update_draft(ctx, empty["id"], {"expected_revision": 1, "draft": empty["draft"] | draft})
    return offers.publish(ctx, saved["id"], saved["revision"])


def run(settings):
    engine = make_engine(settings.database_url)
    security = Security(settings)
    with engine.begin() as con:
        db = Store(con)
        now = Clock().now(db)
        sandbox = {
            "id": uuid4(),
            "seed": "CORE-001",
            "generation": 1,
            "bundle_enabled": True,
            "created_at": now,
            "expires_at": now + timedelta(hours=24),
        }
        db.insert("sandboxes", sandbox)
        owner = security.issue_session(db, sandbox, add_actor(db, sandbox, "OWNER", now), now)
        ctx = Context(db, owner, now)
        offers.seed(ctx)
        # Only this benchmark sandbox has the maximal synthetic template.
        slots = []
        for slot in range(5):
            choices = [
                {"sku": f"LOAD_{slot * 8 + rank:03}", "units_per_pack": 1, "preference_rank": rank}
                for rank in range(8)
            ]
            slots.append(
                {
                    "slot_id": f"LOAD_SLOT_{slot}",
                    "need_rule": "GUESTS",
                    "owned_key": None,
                    "alternatives": choices,
                }
            )
        old = ctx.list("bundle_templates")[0][0]["snapshot"]
        template_id = str(uuid4())
        version = old | {"id": template_id, "title": "Предельный шаблон замера", "slots": slots}
        ctx.insert(
            "bundle_templates", id=template_id, version_no=1, code="B001", snapshot=version, created_at=now
        )
        for i in range(500 - offers.count(ctx, "catalog_skus")):
            offers.create_sku(
                ctx,
                {
                    "sku": f"LOAD_{i:03}",
                    "title": f"Контрольный SKU {i}",
                    "price_minor": 10000 + i,
                    "stock_on_hand": 10000,
                    "active": True,
                    "ready_in_days": 1,
                    "units_per_pack": 1,
                },
            )
        mug = ctx.get("catalog_skus", sku="MUG")
        ctx.update("catalog_skus", {"stock_on_hand": 99999, "revision": mug["revision"] + 1}, sku="MUG")
        fixed = {
            "title": "Контрольное предложение",
            "lines": [{"sku": "MUG", "quantity": 1}],
            "fulfillment_methods": ["PICKUP"],
        }
        primary = prepared_offer(ctx, "FIXED", fixed)
        for i in range(98):
            prepared_offer(ctx, "FIXED", fixed | {"title": f"Контрольное предложение {i + 2}"})
        bundled = prepared_offer(
            ctx,
            "BUNDLE",
            {
                "title": "Предельный комплект",
                "bundle_template_id": template_id,
                "fulfillment_methods": ["PICKUP"],
            },
        )
        buyers = [
            security.issue_session(db, sandbox, add_actor(db, sandbox, "BUYER", now), now) for _ in range(20)
        ]
        request = {
            "kind": "OFFER",
            "offer_id": primary["id"],
            "offer_version_no": 1,
            "copies": 1,
            "fulfillment": "PICKUP",
            "requested_days": 1,
        }
        for i in range(1000):
            buyer = Context(db, buyers[i % 20], now)
            quote = checkout.new_quote(buyer, request)
            hold = checkout.new_reservation(buyer, quote["id"])
            checkout.confirm(buyer, hold["id"], 1)

    def creds(session):
        identity = session["id"]
        return {"cookie": security.token("session", identity), "csrf": security.token("csrf", identity)}

    return {
        "owner": creds(owner),
        "buyers": list(map(creds, buyers)),
        "fixed": primary["id"],
        "bundled": bundled["id"],
        "counts": {"sku": 500, "offers": 100, "initial_orders": 1000},
        "bundle": [5, 8],
    }


if __name__ == "__main__":
    print(json.dumps(run(Settings())))
