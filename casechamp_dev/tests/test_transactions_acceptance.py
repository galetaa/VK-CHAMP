from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from app.db import DB
from app.worker import Worker
from conftest import bootstrap, call
from test_http_core import assert_wire, intent

CASE_COUNT = 41


class Checkout:
    def __init__(self, client, service, *, epoch=1, context=None):
        self.client, self.service = client, service
        self.context = context or bootstrap(client)
        self.epoch = epoch

    def send(self, method, path, body=None, *, epoch=None, key=None):
        return call(
            self.client, method, path, body=body, epoch=self.epoch if epoch is None else epoch, key=key
        )

    def data(self, method, path, name, body=None, *, status=200, **options):
        return assert_wire(self.service, self.send(method, path, body, **options), name, status)

    def quote(self, **changes):
        request = intent(self.context) | changes
        result = self.data("POST", "/quotes", "SolveResult", request)
        assert result["outcome"] == "QUOTED", result
        return result["quote"]

    def reserve(self, quote, *, key=None, expected=None):
        body = {"quote_id": quote["quote_id"], "accepted_quote_hash": quote["quote_hash"]}
        result = self.send("POST", "/reservations", body, key=key)
        return (
            assert_error(self.service, result, expected)
            if expected
            else assert_wire(
                self.service,
                result,
                "Reservation",
                201,
            )
        )

    def confirm(self, reservation, *, key=None, expected=None):
        body = {
            "reservation_id": reservation["reservation_id"],
            "accepted_quote_hash": reservation["quote_hash"],
        }
        result = self.send("POST", "/orders", body, key=key)
        return (
            assert_error(self.service, result, expected)
            if expected
            else assert_wire(
                self.service,
                result,
                "Order",
                201,
            )
        )

    def cancel(self, resource, kind, *, expected=None):
        result = self.send("POST", f"/{kind}s/{resource[f'{kind}_id']}/cancel", {"reason": "BUYER_CANCELED"})
        return (
            assert_error(self.service, result, expected)
            if expected
            else assert_wire(self.service, result, kind.title(), 200)
        )

    def get_catalog(self):
        return self.data("GET", "/catalog", "Catalog")

    def sku(self, sku):
        return next(row for row in self.get_catalog()["items"] if row["sku_id"] == sku)

    def patch(self, row, *, expected=None, **patch):
        body = {"expected_product_version": row["product_version"]} | patch
        result = self.send("PATCH", f"/catalog/items/{row['sku_id']}", body)
        return (
            assert_error(self.service, result, expected)
            if expected
            else assert_wire(self.service, result, "Sku", 200)
        )

    def query(self, query, *parameters):
        with self.service.engine.connect() as connection:
            return DB(connection).all(query, self.context["sandbox_id"], self.epoch, *parameters)

    def inventory(self):
        rows = self.query(
            """SELECT sku_id,stock_on_hand_packs AS on_hand,reserved_packs AS reserved,
               returnable_packs AS liability,inventory_version AS version
               FROM vk_demo.inventory WHERE sandbox_id=%s AND epoch=%s"""
        )
        return {row["sku_id"]: row for row in rows}

    def invariant(self):
        discrepancies = self.query(
            """SELECT i.* FROM vk_demo.inventory i WHERE i.sandbox_id=%s AND i.epoch=%s AND (
                i.reserved_packs<0 OR i.reserved_packs>i.stock_on_hand_packs OR i.returnable_packs<0
                OR i.stock_on_hand_packs+i.returnable_packs>10000 OR i.reserved_packs<>
                (SELECT COALESCE(sum(l.pack_count),0) FROM vk_demo.reservation_lines l
                 JOIN vk_demo.reservations r USING(sandbox_id,epoch,reservation_id)
                 WHERE l.sandbox_id=i.sandbox_id AND l.epoch=i.epoch AND l.sku_id=i.sku_id AND r.status='ACTIVE')
                OR i.returnable_packs<>
                (SELECT COALESCE(sum(l.pack_count),0) FROM vk_demo.order_lines l JOIN vk_demo.orders o
                 USING(sandbox_id,epoch,order_id) WHERE l.sandbox_id=i.sandbox_id AND l.epoch=i.epoch
                 AND l.sku_id=i.sku_id AND o.status='TEST_CONFIRMED'))"""
        )
        assert discrepancies == []


def assert_error(service, response, code):
    assert response.status_code == service.contract.statuses[code], response.text
    envelope = response.json()
    assert not service.contract.dto("Error", envelope), envelope
    assert envelope["error"]["code"] == code, envelope
    return envelope["error"]


def pair(actions):
    barrier = Barrier(len(actions))

    def call_action(action):
        barrier.wait(timeout=10)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(call_action, actions))


def deadline_request(checkout, clock):
    clock.move("2026-11-20T20:55:00Z")
    # The demo primary goods have different handling times. Set the selected terms to zero before the quote.
    reference = checkout.quote()
    for line in reference["lines"]:
        if line["handling_days"]:
            checkout.patch(checkout.sku(line["sku_id"]), handling_days=0)
    return checkout.quote(needed_by="2026-11-20")


def reset(checkout, key=None):
    return checkout.data("POST", "/demo/reset", "Context", {"expected_epoch": checkout.epoch}, key=key)


@pytest.mark.parametrize(
    "case", range(1, CASE_COUNT + 1), ids=[f"TX-{i:02d}" for i in range(1, CASE_COUNT + 1)]
)
def test_transactions(case, controlled_backend):
    start, clock = controlled_backend
    client, service = start()
    c = Checkout(client, service)
    original = c.inventory()
    if case in (31, 32):
        quote = deadline_request(c, clock)
    elif case in (20, 33, 34):
        clock.move("2026-11-20T20:58:00Z")
        if case == 34:
            solve = service.plan

            def cross_midnight(snapshot, request):
                plan = solve(snapshot, request)
                clock.move("2026-11-20T21:00:00Z")
                return plan

            service.plan = cross_midnight
            error = assert_error(
                service, c.send("POST", "/quotes", intent(c.context)), "CONCURRENT_VERSION_CHANGE"
            )
            assert error["details"]["reason_codes"] == ["BUSINESS_DATE_ROLLOVER"]
            assert c.query("SELECT quote_id FROM vk_demo.quotes WHERE sandbox_id=%s AND epoch=%s") == []
            return
        quote = c.quote()
    else:
        quote = c.quote()

    if case in (17, 18, 23, 32):
        other = c.quote(needed_by="2026-11-20" if case == 32 else None)

    line = quote["lines"][0]
    sku = line["sku_id"]
    if case in (2, 3, 4, 5, 8, 9, 15, 17, 18, 19, 22, 23, 24, 28, 30, 31, 32, 35, 36, 37, 38, 39):
        reservation = c.reserve(quote)

    if case == 1:
        assert c.inventory() == original
    if case == 2:
        current = c.inventory()
        for entry in quote["lines"]:
            assert current[entry["sku_id"]]["reserved"] == entry["pack_count"]
            assert current[entry["sku_id"]]["on_hand"] == original[entry["sku_id"]]["on_hand"]
        assert c.data("GET", f"/quotes/{quote['quote_id']}", "Quote")["status"] == "RESERVED"
    if case in (3, 5, 30, 35, 36, 37, 38, 39):
        order = c.confirm(reservation)
        confirmed = c.inventory()
        assert order["status"] == "TEST_CONFIRMED"
        for entry in quote["lines"]:
            identity, packs = entry["sku_id"], entry["pack_count"]
            assert confirmed[identity]["on_hand"] == original[identity]["on_hand"] - packs
            assert confirmed[identity]["reserved"] == 0 and confirmed[identity]["liability"] == packs
    if case == 4:
        first = c.cancel(reservation, "reservation")
        assert first["status"] == "CANCELED" and c.cancel(reservation, "reservation") == first
        assert c.inventory()[sku]["on_hand"] == original[sku]["on_hand"]
        assert c.inventory()[sku]["version"] == original[sku]["version"] + 2
    if case == 5:
        assert c.confirm(reservation) == order
        assert c.inventory() == confirmed
    if case == 6:
        results = pair([lambda: c.reserve(quote) for _ in range(20)])
        assert len({item["reservation_id"] for item in results}) == 1
        assert c.inventory()[sku]["version"] == original[sku]["version"] + 1
    if case in (7, 14, 23, 26):
        if case == 23:
            c.patch(
                c.sku(sku),
                stock_on_hand_packs=line["pack_count"],
                expected_inventory_version=c.sku(sku)["inventory_version"],
            )
            clock.move(reservation["expires_at"])
            # Quote TTL is 900s and remains valid when its sibling's 600s reserve expires.
            assert c.reserve(other)["status"] == "ACTIVE"
            assert (
                c.data("GET", f"/reservations/{reservation['reservation_id']}", "Reservation")["status"]
                == "EXPIRED"
            )
        else:
            selected = quote["lines"][-1] if case == 14 else line
            target = c.sku(selected["sku_id"])
            if case == 7:
                other = c.quote()
            c.patch(
                target,
                stock_on_hand_packs=selected["pack_count"] if case == 7 else 0,
                expected_inventory_version=target["inventory_version"],
            )
            if case == 7:
                paths = (quote, other)

                def send_reserve(quote_result):
                    return c.send(
                        "POST",
                        "/reservations",
                        {
                            "quote_id": quote_result["quote_id"],
                            "accepted_quote_hash": quote_result["quote_hash"],
                        },
                    )

                results = pair([lambda current=current: send_reserve(current) for current in paths])
                assert sorted(res.status_code for res in results) == [201, 409]
                assert_error(service, next(res for res in results if res.status_code == 409), "STOCK_CHANGED")
                assert c.inventory()[sku]["reserved"] == line["pack_count"]
            elif case == 14:
                c.reserve(quote, expected="STOCK_CHANGED")
                assert all(row["reserved"] == 0 for row in c.inventory().values())
            else:
                key = uuid4()
                first = c.reserve(quote, key=key, expected="STOCK_CHANGED")
                updated = c.sku(sku)
                c.patch(
                    updated, stock_on_hand_packs=10, expected_inventory_version=updated["inventory_version"]
                )
                assert c.reserve(quote, key=key, expected="STOCK_CHANGED") == first
                assert c.reserve(quote)["status"] == "ACTIVE"
    if case == 8:

        def confirm():
            return c.send(
                "POST",
                "/orders",
                {
                    "reservation_id": reservation["reservation_id"],
                    "accepted_quote_hash": reservation["quote_hash"],
                },
            )

        def cancel():
            return c.send(
                "POST", f"/reservations/{reservation['reservation_id']}/cancel", {"reason": "BUYER_CANCELED"}
            )

        results = pair([confirm, cancel])
        assert sorted(result.status_code for result in results) in ([200, 409], [201, 409])
        row = c.data("GET", f"/reservations/{reservation['reservation_id']}", "Reservation")
        assert row["status"] in ("CONFIRMED", "CANCELED")
        assert c.inventory()[sku]["reserved"] == 0
        assert len(c.query("SELECT order_id FROM vk_demo.orders WHERE sandbox_id=%s AND epoch=%s")) == int(
            row["status"] == "CONFIRMED"
        )
    if case in (9, 22):
        clock.move(reservation["expires_at"])
        if case == 22:
            worker = Worker(service.settings, engine=service.engine, clock=clock)
            worker.expire()
            released = c.inventory()
            worker.expire()
            assert c.inventory() == released
            assert c.query(
                """SELECT count(*) count FROM vk_demo.outbox_events
                WHERE sandbox_id=%s AND epoch=%s AND event_name='reservation.expired'"""
            ) == [{"count": 1}]
        c.confirm(reservation, expected="RESERVATION_EXPIRED")
        assert all(row["reserved"] == 0 for row in c.inventory().values())
    if case in (10, 21):
        clock.move(quote["expires_at"])
        if case == 21:
            current = c.sku(sku)
            c.patch(current, price_minor=current["price_minor"] + 1)
            reply = c.send(
                "POST",
                "/reservations",
                {"quote_id": quote["quote_id"], "accepted_quote_hash": quote["quote_hash"], "total_minor": 1},
            )
            assert_error(service, reply, "VALIDATION_ERROR")
        c.reserve(quote, expected="QUOTE_EXPIRED")
        assert c.inventory() == original
    if case in (11, 12, 13, 15):
        selected = sku if case != 12 else "DESSERT_WHITE6"
        row = c.sku(selected)
        if case == 13:
            c.patch(
                row,
                stock_on_hand_packs=row["stock_on_hand_packs"] + 1,
                expected_inventory_version=row["inventory_version"],
            )
        else:
            c.patch(row, price_minor=row["price_minor"] + 100)
        if case == 11:
            c.reserve(quote, expected="QUOTE_STALE")
            saved = c.data("GET", f"/quotes/{quote['quote_id']}", "Quote")
            assert saved == quote | {"status": "STALE"}
        elif case == 15:
            assert c.confirm(reservation)["total_minor"] == quote["total_minor"]
        else:
            assert c.reserve(quote)["status"] == "ACTIVE"
    if case == 16:
        revision = c.data("GET", f"/scenario-revisions/{quote['scenario_revision_id']}", "ScenarioRevision")
        original_draft = c.data("GET", f"/scenarios/{revision['scenario_id']}", "Scenario")
        new = c.data(
            "POST",
            f"/scenarios/{revision['scenario_id']}/publish",
            "ScenarioRevision",
            {"expected_draft_version": original_draft["draft_version"]},
            status=201,
        )
        assert new["scenario_revision_id"] != quote["scenario_revision_id"]
        assert c.reserve(quote)["status"] == "ACTIVE"
    if case in (17, 18):
        if case == 17:
            target, version, code = (
                f"/scenario-revisions/{quote['scenario_revision_id']}",
                1,
                "TEMPLATE_WITHDRAWN",
            )
        else:
            target, version, code = (
                f"/catalog/items/{sku}",
                c.sku(sku)["product_version"],
                "PRODUCT_WITHDRAWN",
            )
        result = c.data(
            "POST", target + "/withdraw", "MutationResult", {"expected_version": version, "reason": "test"}
        )
        assert result["invalidated_reservations"] == 1
        assert c.data("GET", f"/quotes/{other['quote_id']}", "Quote")["status"] == "CANCELED"
        withdrawn = c.data("GET", f"/reservations/{reservation['reservation_id']}", "Reservation")
        assert withdrawn["status"] == "INVALIDATED" and withdrawn["reason_code"] == code
        c.confirm(reservation, expected=code)
        assert all(row["reserved"] == 0 for row in c.inventory().values())
    if case == 19:
        row = c.sku(sku)
        c.patch(
            row,
            expected="STOCK_BELOW_RESERVED",
            stock_on_hand_packs=0,
            expected_inventory_version=row["inventory_version"],
        )
        assert c.inventory()[sku]["on_hand"] == original[sku]["on_hand"]
    if case in (20, 33):
        clock.move("2026-11-20T21:00:00Z")
        error = c.reserve(quote, expected="QUOTE_STALE")
        assert error["details"]["reason_codes"] == ["BUSINESS_DATE_ROLLOVER"]
    if case == 24:
        key = uuid4()
        body = {
            "reservation_id": reservation["reservation_id"],
            "accepted_quote_hash": reservation["quote_hash"],
        }
        initial = c.send("POST", "/orders", body, key=key)
        replay = c.send("POST", "/orders", body, key=key)
        assert replay.json()["meta"]["idempotency_replayed"]
        assert replay.json()["data"] == initial.json()["data"]
        assert len(c.query("SELECT order_id FROM vk_demo.orders WHERE sandbox_id=%s AND epoch=%s")) == 1
    if case == 25:
        key = uuid4()
        c.reserve(quote, key=key)
        other = c.quote()
        c.reserve(other, key=key, expected="IDEMPOTENCY_KEY_REUSED")
        assert (
            len(c.query("SELECT reservation_id FROM vk_demo.reservations WHERE sandbox_id=%s AND epoch=%s"))
            == 1
        )
    if case == 27:
        key = uuid4()
        with service.engine.begin() as connection:
            DB(connection).first(
                "SELECT * FROM vk_demo.sandboxes WHERE sandbox_id=%s FOR UPDATE", c.context["sandbox_id"]
            )
            c.reserve(quote, key=key, expected="STORE_BUSY")
        assert (
            c.query(
                """SELECT * FROM vk_demo.idempotency_records WHERE sandbox_id=%s AND epoch=%s AND idempotency_key=%s""",
                key,
            )
            == []
        )
        assert c.inventory() == original
        assert c.reserve(quote, key=key)["status"] == "ACTIVE"
    if case == 28:
        current = reset(c)
        c.confirm(reservation, expected="SANDBOX_EPOCH_MISMATCH")
        assert current["sandbox_epoch"] == 2
        assert Checkout(client, service, context=current, epoch=2).inventory() == original
    if case == 29:
        key = uuid4()
        first = reset(c, key=key)
        response = c.send("POST", "/demo/reset", {"expected_epoch": 1}, key=key)
        assert response.json()["data"] == first and response.json()["meta"]["idempotency_replayed"]
        assert first["sandbox_epoch"] == 2
    if case == 30:
        c.cancel(reservation, "reservation", expected="INVALID_STATE")
        assert c.inventory() == confirmed
    if case == 31:
        assert quote["expires_at"] == reservation["expires_at"] == "2026-11-20T21:00:00.000Z"
    if case == 32:
        clock.move(quote["expires_at"])
        c.reserve(other, expected="QUOTE_EXPIRED")
        c.confirm(reservation, expected="RESERVATION_EXPIRED")
    if case in (35, 36, 37, 38, 39):
        if case == 38:
            row = c.sku(sku)
            c.data(
                "POST",
                f"/catalog/items/{sku}/withdraw",
                "MutationResult",
                {"expected_version": row["product_version"], "reason": "test"},
            )
            assert c.data("GET", f"/orders/{order['order_id']}", "Order")["status"] == "TEST_CONFIRMED"
        if case == 39:
            row = c.sku(sku)
            error = c.patch(
                row,
                expected="VALIDATION_ERROR",
                stock_on_hand_packs=10000,
                expected_inventory_version=row["inventory_version"],
            )
            assert "STOCK_RETURN_CAPACITY" in error["details"]["reason_codes"]
            assert c.inventory() == confirmed
        outcomes = (
            pair([lambda: c.cancel(order, "order") for _ in range(20)])
            if case == 36
            else [c.cancel(order, "order"), c.cancel(order, "order")]
        )
        assert all(result["status"] == "CANCELED" and result == outcomes[0] for result in outcomes)
        assert c.inventory()[sku]["on_hand"] == original[sku]["on_hand"]
        assert c.inventory()[sku]["liability"] == 0
        assert (
            c.data("GET", f"/reservations/{reservation['reservation_id']}", "Reservation")["status"]
            == "CONFIRMED"
        )
        assert c.query(
            """SELECT count(*) count FROM vk_demo.outbox_events
            WHERE sandbox_id=%s AND epoch=%s AND event_name='order.test_canceled'"""
        ) == [{"count": 1}]
        if case == 37:
            assert c.confirm(reservation) == outcomes[0]
        if case == 38:
            assert c.sku(sku)["active"] is False
    if case == 40:
        row = c.sku(sku)
        unchanged = c.patch(row, title=row["title"])
        assert unchanged == row
        changed = c.patch(row, title=row["title"] + " обновлено")
        assert changed["product_version"] == row["product_version"] + 1
        c.reserve(quote, expected="QUOTE_STALE")
    if case == 41:
        row = c.sku(sku)
        c.patch(row, expected="VALIDATION_ERROR", stock_on_hand_packs=0)
        c.patch(
            row,
            expected="CONCURRENT_VERSION_CHANGE",
            stock_on_hand_packs=0,
            expected_inventory_version=row["inventory_version"] + 1,
        )
        assert c.sku(sku) == row
    c.invariant()
