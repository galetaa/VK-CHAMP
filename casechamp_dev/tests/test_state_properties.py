from uuid import uuid4

from app.planner import solve
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_http_core import intent
from test_transactions_acceptance import Checkout


@given(
    st.lists(
        st.sampled_from(["quote", "reserve", "confirm", "cancel_reservation", "cancel_order", "time"]),
        min_size=4,
        max_size=12,
    )
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_stock_ledger_under_generated_sequences(controlled_backend, operations):
    start, clock = controlled_backend
    clock.move("2026-11-20T10:00:00Z")
    client, service = start()
    service.plan = solve
    checkout = Checkout(client, service)
    quote = reservation = order = None
    reserve_receipt = None
    for action in operations:
        if action == "quote":
            outcome = checkout.data("POST", "/quotes", "SolveResult", intent(checkout.context))
            assert outcome["outcome"] in ("QUOTED", "INFEASIBLE")
            quote = outcome["quote"]
            reserve_receipt = uuid4()
        elif action == "reserve" and quote:
            result = checkout.send(
                "POST",
                "/reservations",
                {
                    "quote_id": quote["quote_id"],
                    "accepted_quote_hash": quote["quote_hash"],
                },
                key=reserve_receipt,
            )
            if result.status_code == 201:
                reservation = result.json()["data"]
            else:
                assert result.json()["error"]["code"] in ("QUOTE_EXPIRED", "STOCK_CHANGED")
        elif action == "confirm" and reservation:
            result = checkout.send(
                "POST",
                "/orders",
                {
                    "reservation_id": reservation["reservation_id"],
                    "accepted_quote_hash": reservation["quote_hash"],
                },
            )
            if result.status_code == 201:
                order = result.json()["data"]
                assert order["reservation_id"] == reservation["reservation_id"]
            else:
                assert result.json()["error"]["code"] in ("RESERVATION_EXPIRED", "INVALID_STATE")
        elif action == "cancel_reservation" and reservation:
            result = checkout.send(
                "POST", f"/reservations/{reservation['reservation_id']}/cancel", {"reason": "BUYER_CANCELED"}
            )
            if result.status_code != 200:
                assert result.json()["error"]["code"] == "INVALID_STATE"
        elif action == "cancel_order" and order:
            assert checkout.cancel(order, "order")["status"] == "CANCELED"
        elif action == "time":
            clock.advance(250)
        clock.advance(2)  # Observe the published solve rate limit while generating independent steps.
        checkout.invariant()
        assert (
            checkout.query(
                """SELECT quote_id FROM vk_demo.reservations WHERE sandbox_id=%s AND epoch=%s GROUP BY quote_id
               HAVING count(*)>1"""
            )
            == []
        )
        assert (
            checkout.query(
                """SELECT reservation_id FROM vk_demo.orders WHERE sandbox_id=%s AND epoch=%s GROUP BY reservation_id
               HAVING count(*)>1"""
            )
            == []
        )
