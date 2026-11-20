import json
from datetime import date
from pathlib import Path

import pytest
from app.common import Problem
from app.planner import normalized_intent, solve
from app.wire import Contract
from reference_oracle import solve as oracle

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Contract(ROOT / "packages/contracts")
CASES = json.loads((ROOT / "fixtures/golden_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_golden(case):
    snapshot, request, expected = (case[name] for name in ("snapshot", "request", "expected"))
    assert oracle(snapshot, request) == expected
    if expected["outcome"] == "VALIDATION_ERROR":
        if CONTRACT.dto("PurchaseRequest", request):
            assert expected["error_code"] == "VALIDATION_ERROR"
            return
        with pytest.raises(Problem) as caught:
            normalized_intent(request, date.fromisoformat(snapshot["business_date"]))
        assert caught.value.code == expected["error_code"]
    else:
        normalized = normalized_intent(request, date.fromisoformat(snapshot["business_date"]))
        plan = solve(snapshot, normalized)
        assert plan.error is None
        assert plan.result == expected
