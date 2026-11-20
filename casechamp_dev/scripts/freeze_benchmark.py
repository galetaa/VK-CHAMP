"""Freeze a synthetic evaluation sample and independently computed gold outcomes."""

import copy
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from reference_oracle import solve as oracle  # noqa: E402

SEED = 6112026
DESIGNS = (
    ("AC-EF-01", "unique"),
    ("AC-EF-02", "tie"),
    ("AC-EF-03", "budget"),
    ("AC-EF-04", "packs"),
    ("AC-EF-05", "color"),
    ("AC-EF-06", "cloth"),
    ("AC-EF-07", "owned"),
    ("AC-EF-08", "delivery"),
    ("AC-EF-09", "unavailable"),
)


def freeze():
    template = json.loads((ROOT / "fixtures/backend_catalog.json").read_text())["cases"][0]
    rng = random.Random(SEED)
    cases = []
    for repeat in range(2):
        for family, variant in DESIGNS:
            snapshot, request = copy.deepcopy(template["snapshot"]), copy.deepcopy(template["request"])
            prefix = f"F{repeat}{family[-2:]}_"
            for product in snapshot["catalog"]:
                product["sku_id"] = prefix + product["sku_id"]
                product["price_minor"] += (100 + 20 * repeat) * product["pack_units"]
            for slot in snapshot["template_slots"]:
                slot["primary_sku_id"] = prefix + slot["primary_sku_id"]
                slot["alternative_sku_ids"] = [
                    prefix + candidate for candidate in slot["alternative_sku_ids"]
                ]
            slots = {slot["slot_id"]: slot for slot in request["slots"]}
            products = {product["sku_id"].removeprefix(prefix): product for product in snapshot["catalog"]}
            if variant == "unique":
                for slot in slots.values():
                    slot["selection_mode"] = "EXACT_ONLY"
            elif variant == "tie":
                products["D-A"].update(stock_on_hand_packs=0, available_packs=0)
            elif variant == "budget":
                request["budget_minor"] = 50000 if repeat == 0 else 240000
            elif variant == "packs":
                request["guest_count"] = rng.choice([5, 7, 9])
                slots["dinner_plate"]["owned_units"] = 1
            elif variant == "color":
                slots["dinner_plate"]["allowed_colors"] = ["BLUE"]
            elif variant == "cloth":
                slots["tablecloth"]["enabled"] = True
                request["table_dimensions"] = {"length_cm": 180, "width_cm": 90}
                if repeat:
                    products["T-A"]["length_cm"], products["T-A"]["width_cm"] = 130, 220
            elif variant == "owned":
                for slot in slots.values():
                    if slot["enabled"]:
                        slot["owned_units"] = request["guest_count"]
            elif variant == "delivery":
                request["fulfillment"] = {"mode": "LOCAL_DELIVERY", "zone": "NORTH"}
                request["needed_by"] = "2026-11-21" if repeat == 0 else "2026-11-24"
            elif variant == "unavailable":
                products["D-A"].update(active=False)
                slots["dinner_plate"]["selection_mode"] = "EXACT_ONLY"
            cases.append(
                {
                    "id": f"EVAL-{len(cases) + 1:03d}",
                    "family": family,
                    "scenario": variant,
                    "snapshot": snapshot,
                    "request": request,
                    "expected": oracle(snapshot, request),
                }
            )
    payload = {
        "version": "evaluation-1.0.0",
        "seed": SEED,
        "origin": "controlled synthetic exact core evaluation",
        "provider_model": "qwen2.5:0.5b",
        "provider_attempts": 1,
        "per_method_wall_seconds": 5,
        "provider_num_predict": 160,
        "cases": cases,
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target = ROOT / "fixtures/benchmark_manifest.json"
    with target.open("x", encoding="utf-8") as out:
        out.write(data)
    print(hashlib.sha256(data.encode()).hexdigest())


if __name__ == "__main__":
    freeze()
