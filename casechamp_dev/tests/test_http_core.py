import json
from pathlib import Path
from uuid import uuid4

from conftest import bootstrap, call

ROOT = Path(__file__).resolve().parents[1]


def assert_wire(service, result, schema, status):
    assert result.status_code == status, result.text
    body = result.json()
    issues = service.contract.dto("Meta", body["meta"]) + service.contract.dto(schema, body["data"])
    assert not issues, issues
    return body["data"]


def intent(context):
    request = json.loads((ROOT / "fixtures/base_request.json").read_text())
    return request | {
        "scenario_revision_id": context["latest_scenario_revision_id"],
        "needed_by": None,
        "client_run_id": str(uuid4()),
    }


def test_manual_checkout_and_order_cancel(backend):
    client, service = backend
    context = bootstrap(client)
    epoch = context["sandbox_epoch"]
    catalog = assert_wire(service, call(client, "GET", "/catalog", epoch=epoch), "Catalog", 200)
    key = uuid4()
    created = call(client, "POST", "/quotes", body=intent(context), epoch=epoch, key=key)
    quote = assert_wire(service, created, "SolveResult", 200)["quote"]
    assert quote["total_minor"] == 440000
    verify = call(client, "GET", f"/quotes/{quote['quote_id']}/verification", epoch=epoch)
    assert assert_wire(service, verify, "Verification", 200)["passed"]
    reserve_body = {"quote_id": quote["quote_id"], "accepted_quote_hash": quote["quote_hash"]}
    reservation = assert_wire(
        service, call(client, "POST", "/reservations", body=reserve_body, epoch=epoch), "Reservation", 201
    )
    confirm_body = {
        "reservation_id": reservation["reservation_id"],
        "accepted_quote_hash": quote["quote_hash"],
    }
    key = uuid4()
    order_response = call(client, "POST", "/orders", body=confirm_body, epoch=epoch, key=key)
    order = assert_wire(service, order_response, "Order", 201)
    assert order["total_minor"] == quote["total_minor"]
    replay = call(client, "POST", "/orders", body=confirm_body, epoch=epoch, key=key)
    assert replay.json()["meta"]["idempotency_replayed"] and replay.json()["data"] == order
    cancel_url = f"/orders/{order['order_id']}/cancel"
    canceled = assert_wire(
        service,
        call(client, "POST", cancel_url, body={"reason": "BUYER_CANCELED"}, epoch=epoch),
        "Order",
        200,
    )
    assert canceled["status"] == "CANCELED"
    assert call(client, "POST", "/orders", body=confirm_body, epoch=epoch).json()["data"] == canceled
    restored = assert_wire(service, call(client, "GET", "/catalog", epoch=epoch), "Catalog", 200)
    assert [row["stock_on_hand_packs"] for row in catalog["items"]] == [
        row["stock_on_hand_packs"] for row in restored["items"]
    ]
    assert all(row["reserved_packs"] == 0 for row in restored["items"])
    metrics = assert_wire(service, call(client, "GET", "/metrics", epoch=epoch), "Metrics", 200)
    assert metrics["test_orders_confirmed"] == metrics["test_orders_canceled"] == 1


def test_reset_replay_and_epoch(backend):
    client, service = backend
    before = bootstrap(client, "EMPTY_V1")
    assert before["sandbox_epoch"] == 1
    key = uuid4()
    for expected_replay in (False, True):
        response = call(client, "POST", "/demo/reset", body={"expected_epoch": 1}, epoch=1, key=key)
        new = assert_wire(service, response, "Context", 200)
        assert new["sandbox_epoch"] == response.json()["meta"]["sandbox_epoch"] == 2
        assert response.json()["meta"]["idempotency_replayed"] == expected_replay
        assert new["latest_scenario_revision_id"] is None
    expired = call(client, "GET", "/catalog", epoch=1)
    assert expired.status_code == 409 and expired.json()["error"]["code"] == "SANDBOX_EPOCH_MISMATCH"
