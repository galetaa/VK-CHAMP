import copy
import json
import random
from datetime import date
from pathlib import Path

import pytest
from app.common import Problem, digest
from app.hashes import solution_payload
from app.planner import cloth_fit, normalized_intent, run_planner, solve
from app.verifier import verify_solution
from app.wire import Contract
from ortools.sat.python import cp_model
from reference_oracle import solve as oracle

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Contract(ROOT / "packages/contracts")
BASE = json.loads((ROOT / "fixtures/backend_catalog.json").read_text())["cases"][0]


def preset():
    return copy.deepcopy(BASE["snapshot"]), copy.deepcopy(BASE["request"])


def item(snapshot, sku):
    return next(row for row in snapshot["catalog"] if row["sku_id"] == sku)


def intent_slot(request, slot):
    return next(row for row in request["slots"] if row["slot_id"] == slot)


def set_stock(snapshot, sku, packs):
    item(snapshot, sku).update(stock_on_hand_packs=packs, reserved_packs=0, available_packs=packs)


def evaluate(snapshot, request):
    intent = normalized_intent(request, date.fromisoformat(snapshot["business_date"]))
    plan = solve(snapshot, intent)
    if plan.result.get("outcome") == "QUOTED":
        assert verify_solution(snapshot, intent, plan.result) == []
    return plan


def modify(case, snapshot, request):
    dinner, cloth = (intent_slot(request, slot) for slot in ("dinner_plate", "tablecloth"))
    if case in (2, 4):
        request["budget_minor"] = 240000
    if case == 3:
        request["budget_minor"] = 230000
    if case in (4, 12):
        dinner["selection_mode"] = "EXACT_ONLY"
    if case == 5:
        dinner["allowed_colors"] = ["BLUE"]
    if case in (6, 7, 23):
        dinner["preferred_color"] = "BLUE"
    if case in (7, 8):
        set_stock(snapshot, "D-A", 0)
    if case in (9, 10, 12):
        dinner["owned_units"] = {9: 4, 10: 99, 12: 2}[case]
    if case == 11:
        for slot in request["slots"]:
            if slot["enabled"]:
                slot["owned_units"] = 4
    if case in (13, 14, 20, 21):
        request["fulfillment"] = {"mode": "LOCAL_DELIVERY", "zone": "OUTSIDE" if case == 14 else "CENTER"}
    if case in (15, 16, 17, 18, 19):
        cloth["enabled"] = True
        request["table_dimensions"] = {"length_cm": 180, "width_cm": 90}
    if case in (16, 17, 18):
        length, width = {16: (130, 220), 17: (210, 120), 18: (209, 120)}[case]
        item(snapshot, "T-A").update(length_cm=length, width_cm=width)
    if case == 19:
        cloth["owned_units"] = 1
        request["table_dimensions"] = None
    if case in (20, 21, 22):
        item(snapshot, "G-A")["handling_days"] = 3
    if case in (20, 21):
        request["needed_by"] = "2026-11-23" if case == 20 else "2026-11-24"
    if case == 23:
        dinner["allowed_colors"] = ["WHITE"]
    if case == 24:
        snapshot["catalog"].append(item(snapshot, "D-A") | {"sku_id": "CHEAP", "price_minor": 1})


@pytest.mark.parametrize("case", range(1, 32), ids=[f"ALG-{i:02d}" for i in range(1, 32)])
def test_algorithm(case, monkeypatch):
    snapshot, request = preset()
    modify(case, snapshot, request)
    if case in (14, 23):
        with pytest.raises(Problem) as failure:
            evaluate(snapshot, request)
        assert failure.value.code == ("UNSUPPORTED_ZONE" if case == 14 else "CONTRADICTORY_PREFERENCES")
        return
    if case in (26, 27, 28):
        original = cp_model.CpSolver.solve
        count = 0

        def forced(solver, model):
            nonlocal count
            count += 1
            if case == 27 and count < 5:
                return original(solver, model)
            code = {26: cp_model.FEASIBLE, 27: cp_model.UNKNOWN, 28: cp_model.MODEL_INVALID}[case]
            # The genuine response provides timing values. Only the solver status is injected.
            original(solver, model)
            return code

        monkeypatch.setattr(cp_model.CpSolver, "solve", forced)
        plan = evaluate(snapshot, request)
        assert plan.error == ("INTERNAL_INVARIANT_FAILED" if case == 28 else "COMPUTATION_LIMIT")
        assert plan.result == {}
        return
    if case == 29:
        assert run_planner(snapshot, request, timeout=0).error == "COMPUTATION_ABORTED"
        return
    if case == 31:
        assert CONTRACT.dto("PurchaseRequest", request) == []
        invalid = copy.deepcopy(request)
        intent_slot(invalid, "dinner_plate")["allowed_colors"] = None
        assert CONTRACT.dto("PurchaseRequest", invalid)
    plan = evaluate(snapshot, request)
    expected = oracle(snapshot, request)
    assert plan.result == expected
    lines = {row["slot_id"]: row for row in plan.result.get("lines", [])}
    if case == 1:
        assert plan.result["total_minor"] == 252000
        assert [x["pack_count"] for x in lines.values()] == [1, 2, 4, 1]
    if case in (2, 5, 6, 7, 8):
        assert lines["dinner_plate"]["sku_id"] == {2: "D-B", 5: "D-C", 6: "D-A", 7: "D-C", 8: "D-B"}[case]
    if case == 3:
        assert plan.result["minimum_total_minor"] == 232000 and plan.reasons[0]["code"] == "BUDGET_TOO_LOW"
    if case == 4:
        assert plan.result["outcome"] == "INFEASIBLE"
    if case in (9, 10):
        assert "dinner_plate" not in lines and plan.result["total_minor"] == 152000
    if case == 11:
        assert plan.result["total_minor"] == plan.result["shipping_minor"] == 0
        request["fulfillment"] = {"mode": "LOCAL_DELIVERY", "zone": "OUTSIDE"}
        assert evaluate(snapshot, request).result == plan.result
    if case == 12:
        assert lines["dinner_plate"]["pack_count"] == 1 and lines["dinner_plate"]["excess_units"] == 2
    if case == 13:
        assert plan.result["shipping_minor"] == 30000 and plan.result["total_minor"] == 282000
    if case in (15, 16, 17):
        fit = cloth_fit(item(snapshot, "T-A"), request["table_dimensions"])
        assert fit["orientation"] == ("ROTATED" if case == 16 else "NORMAL")
        assert fit["drop_length_mm"] == (150 if case == 17 else 200)
        if case == 17:
            item(snapshot, "T-A").update(length_cm=240, width_cm=150)
            assert evaluate(snapshot, request).result["outcome"] == "QUOTED"
            assert cloth_fit(item(snapshot, "T-A"), request["table_dimensions"])["drop_width_mm"] == 300
    if case in (18, 20):
        code = "GEOMETRY_RESTRICTION" if case == 18 else "DEADLINE_UNMET"
        assert plan.result["outcome"] == "INFEASIBLE" and code in [row["code"] for row in plan.reasons]
    if case == 19:
        assert "tablecloth" not in lines
    if case in (21, 22):
        assert plan.result["earliest_date"] == ("2026-11-24" if case == 21 else "2026-11-23")
    if case == 24:
        assert all(line["sku_id"] != "CHEAP" for line in lines.values())
    if case in (25, 30):
        baseline = digest(
            solution_payload(
                algorithm_version="planner-1.0.0", snapshot=snapshot, request=request, result=plan.result
            )
        )
        for i in range(100 if case == 30 else 1):
            if case == 25:
                snapshot["catalog"].reverse()
                snapshot["template_slots"].reverse()
                request["slots"].reverse()
                request = dict(reversed(request.items()))
            current = evaluate(snapshot, request)
            assert current.result == plan.result, i
            assert (
                digest(
                    solution_payload(
                        algorithm_version="planner-1.0.0",
                        snapshot=snapshot,
                        request=request,
                        result=current.result,
                    )
                )
                == baseline
            )


@pytest.mark.parametrize("seed", range(300), ids=lambda seed: f"seed-{seed:03d}")
def test_seeded_catalogs_against_oracle(seed):
    rng = random.Random(seed + 7112026)
    snapshot, request = preset()
    request["guest_count"] = rng.randint(1, 12)
    request["budget_minor"] = rng.randrange(50000, 700000)
    for slot in snapshot["template_slots"]:
        substitutions = rng.randint(0, 3)
        primary = item(snapshot, slot["primary_sku_id"])
        slot["alternative_sku_ids"] = [
            f"ALT{seed}_{sid}_{slot['slot_id'].upper()}" for sid in range(substitutions)
        ]
        for sku in slot["alternative_sku_ids"]:
            in_stock = rng.randint(0, 12)
            snapshot["catalog"].append(
                primary
                | {
                    "sku_id": sku,
                    "pack_units": rng.randint(1, 8),
                    "price_minor": rng.randint(7000, 80000),
                    "stock_on_hand_packs": in_stock,
                    "available_packs": in_stock,
                    "color": rng.choice(["WHITE", "BEIGE", "BLUE"]),
                }
            )
        slot_input = intent_slot(request, slot["slot_id"])
        slot_input["owned_units"] = rng.randint(0, 5)
    if rng.choice((False, True)):
        request["fulfillment"] = {"mode": "LOCAL_DELIVERY", "zone": "NORTH"}
    rng.shuffle(snapshot["catalog"])
    rng.shuffle(snapshot["template_slots"])
    for slot in snapshot["template_slots"]:
        rng.shuffle(slot["alternative_sku_ids"])
    assert evaluate(snapshot, request).result == oracle(snapshot, request), seed


def test_monotone_constraints_and_unapproved_skus():
    snapshot, request = preset()
    reference = evaluate(snapshot, request).result
    more_budget = request | {"budget_minor": request["budget_minor"] + 1}
    assert evaluate(snapshot, more_budget).result["outcome"] == "QUOTED"
    more_stock = copy.deepcopy(snapshot)
    set_stock(more_stock, "D-A", 10000)
    assert evaluate(more_stock, request).result["outcome"] == "QUOTED"
    variant = copy.deepcopy(snapshot)
    variant["catalog"].append(item(snapshot, "D-A") | {"sku_id": "UNAPPROVED", "price_minor": 1})
    assert evaluate(variant, request).result == reference
    constrained = copy.deepcopy(request)
    intent_slot(constrained, "dinner_plate")["allowed_colors"] = ["BLUE"]
    assert all(
        item["color"] == "BLUE"
        for item in variant["catalog"]
        if item["sku_id"]
        in {
            row["sku_id"]
            for row in evaluate(variant, constrained).result["lines"]
            if row["slot_id"] == "dinner_plate"
        }
    )
