import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from app.settings import ROOT, Settings
from app.v2.core import Store
from app.v2.main import create_app
from app.v2.worker import Worker
from conftest import ControlledClock
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:8000"


@pytest.fixture(scope="session")
def migrated_v2(postgres_url):
    with pytest.MonkeyPatch.context() as env:
        env.setenv("MIGRATION_DATABASE_URL", postgres_url)
        cfg = Config(str(ROOT / "alembic.v2.ini"))
        command.upgrade(cfg, "head")
    return postgres_url


class Browser:
    def __init__(self, app):
        self.client = TestClient(app, base_url=ORIGIN)
        self.service = app.state.service
        self.session = None
        self.csrf = None

    def request(self, method, path, body=None, *, key=None, headers=None):
        headers = headers or {}
        if method != "GET":
            headers = {
                "Origin": ORIGIN,
                "Idempotency-Key": str(key or uuid4()),
                "X-CSRF-Token": self.csrf or "",
            } | headers
        return self.client.request(method, "/api/v2" + path, json=body, headers=headers)

    def wire(self, response, expected=200):
        assert response.status_code == expected, response.text
        data = response.json()
        assert self.service.wire.dto("Meta", data["meta"]) == []
        if expected >= 400:
            assert self.service.wire.dto("ErrorEnvelope", data) == [], data
            return data["error"]
        url = response.request.url.path.removeprefix("/api/v2")
        import re

        for path, method, description in self.service.wire.operations.values():
            pattern = re.sub(r"{[^}]+}", r"[^/]+", path)
            if method == response.request.method and re.fullmatch(pattern, url):
                schema = description["responses"][str(expected)]["content"]["application/json"]["schema"]
                issues = self.service.wire.issues(data, schema)
                assert issues == [], (issues, data)
                return data["data"]
        raise AssertionError(url)

    def call(self, method, path, body=None, *, status=200, key=None):
        return self.wire(self.request(method, path, body, key=key), status)

    def probe(self, method, path, body, code, key=None):
        error = self.call(method, path, body, status=self.service.wire.statuses[code], key=key)
        assert error["code"] == code, error
        return error["details"]

    def bootstrap(self):
        data = self.call("GET", "/demo/bootstrap")
        self.csrf = data["csrf_token"]
        return data

    def use_session(self, data):
        self.session = data
        self.csrf = data["csrf_token"]
        return data

    def create(self, bundle=False):
        self.bootstrap()
        return self.use_session(
            self.call("POST", "/demo/sandboxes", {"seed": "CORE-001", "enable_bundle": bundle}, status=201)
        )

    def invite(self, browser, role):
        link = self.call("POST", "/demo/role-links", {"role": role}, status=201)
        browser.bootstrap()
        browser.use_session(browser.call("POST", "/demo/sessions", {"token": link["url"].split("=")[1]}))
        return browser

    def offer(self, mode="FIXED", amount=1, other=2, template=None):
        offer = self.call("POST", "/offers", {"mode": mode}, status=201)
        key = offer["id"]
        data = offer["draft"] | {
            "title": "Предложение",
            "description": "Описание",
            "fulfillment_methods": [
                "PICKUP",
                "COURIER_ZONE_A",
            ],
        }
        if mode == "FIXED":
            data["lines"] = [{"sku": "MUG", "quantity": amount}]
            if other:
                data["lines"].append({"sku": "TEA", "quantity": other})
        else:
            data["bundle_template_id"] = template or self.call("GET", "/bundle-templates")["items"][0]["id"]
        saved = self.call(
            "PATCH", f"/offers/{key}/draft", {"draft": data, "expected_revision": offer["revision"]}
        )
        preview = self.call("POST", f"/offers/{key}/preview", {"expected_revision": saved["revision"]})
        assert preview["valid"]
        return self.call("POST", f"/offers/{key}/publish", {"expected_revision": saved["revision"]})

    def quote(self, offer, **changes):
        return self.call("POST", "/quotes", offer_request(offer) | changes, status=201)

    def reserve(self, quote):
        return self.call("POST", "/reservations", {"quote_id": quote["id"]}, status=201)

    def confirm(self, reservation, key=None):
        return self.call(
            "POST",
            f"/reservations/{reservation['id']}/confirm",
            {"expected_revision": reservation["revision"]},
            status=201,
            key=key,
        )

    def sku(self, name, **changes):
        item = next(row for row in self.call("GET", "/catalog")["items"] if row["sku"] == name)
        if not changes:
            return item
        body = {
            key: item[key] for key in ("title", "price_minor", "stock_on_hand", "active", "ready_in_days")
        }
        return self.call(
            "PATCH", "/catalog/skus/" + name, body | changes | {"expected_revision": item["revision"]}
        )


@pytest.fixture()
def stack(migrated_v2):
    clock = ControlledClock()
    settings = Settings(
        database_url=migrated_v2, app_mode="test", web_origin=ORIGIN, session_secret=uuid4().hex
    )
    app = create_app(settings, clock=clock)
    worker = Worker(settings, engine=app.state.service.engine, clock=clock)
    worker.heartbeat()
    yield lambda: Browser(app), clock, worker
    app.state.service.engine.dispose()


def offer_request(offer):
    return {
        "kind": "OFFER",
        "offer_id": offer["id"],
        "offer_version_no": offer["published_version_no"],
        "copies": 1,
        "fulfillment": "PICKUP",
        "requested_days": 1,
    }


def checkout(stack, *, bundle=False):
    browser, clock, worker = stack
    owner = browser()
    owner.create(bundle=bundle)
    return owner, owner.invite(browser(), "BUYER"), clock, worker


def test_full_p0(stack):
    browser, _, worker = stack
    owner = browser()
    owner.create()
    buyer = owner.invite(browser(), "BUYER")
    operator = owner.invite(browser(), "OPERATOR")
    assert owner.call("GET", "/health")["contract_version"] == "2.0.0"
    offer = operator.offer()
    assert buyer.call("GET", f"/public/offers/{offer['id']}")["version"]["version_no"] == 1
    quote = buyer.quote(offer)
    assert quote["total_minor"] == 219000
    assert owner.sku("MUG")["stock_reserved"] == 0
    reservation = buyer.reserve(quote)
    assert owner.sku("MUG")["stock_reserved"] == 1
    order = buyer.confirm(reservation)
    assert order["is_test"] and order["native_vk_order_id"] is None and order["paid_at"] is None
    assert owner.sku("MUG")["stock_on_hand"] == 9
    assert [x["id"] for x in buyer.call("GET", "/orders")["items"]] == [order["id"]]
    path = f"/orders/{order['id']}/transition"
    accepted = operator.call(
        "POST", path, {"expected_revision": 1, "to_status": "ACCEPTED", "cancel_reason": None}
    )
    submitted = operator.call(
        "POST",
        path,
        {"expected_revision": accepted["revision"], "to_status": "FULFILLED", "cancel_reason": None},
    )
    assert submitted["status"] == "FULFILLED"
    assert owner.call("GET", "/dashboard")["orders_fulfilled"] == 1
    worker.deliver()
    with owner.service.engine.begin() as con:
        db = Store(con)
        chain = db.all(
            """SELECT payload FROM events WHERE sandbox_id=%s AND payload->>'offer_id'=%s ORDER BY name""",
            owner.session["sandbox_id"],
            offer["id"],
        )
        assert {row["payload"]["name"] for row in chain} >= {
            "offer_published",
            "quote_created",
            "reservation_created",
            "order_created",
            "order_status_changed",
        }
        assert owner.service.wire.dto("Event", chain[0]["payload"]) == []
        assert (
            db.first(
                "SELECT count(*) n FROM event_delivery WHERE sandbox_id=%s", owner.session["sandbox_id"]
            )["n"]
            >= 7
        )


def test_bundle_end_to_end(stack):
    owner, buyer, _, _ = checkout(stack, bundle=True)
    offer = owner.offer("BUNDLE")
    request = {
        "offer_id": offer["id"],
        "offer_version_no": 1,
        "guests": 4,
        "budget_minor": 500000,
        "owned": ["TABLECLOTH"],
        "fulfillment": "PICKUP",
        "requested_days": 1,
    }
    fitted = buyer.call("POST", "/bundle-solutions", request, status=201)
    assert fitted["status"] == "SOLVED" and fitted["total_minor"] == 440000
    alternate = buyer.call("POST", "/bundle-solutions", request | {"budget_minor": 400000}, status=201)
    assert alternate["total_minor"] == 380000
    quote = buyer.call("POST", "/quotes", {"kind": "BUNDLE", "solution_id": fitted["id"]}, status=201)
    order = buyer.confirm(buyer.reserve(quote))
    assert order["total_minor"] == fitted["total_minor"]
    rejected = buyer.call("POST", "/bundle-solutions", request | {"budget_minor": 327999}, status=201)
    assert rejected["status"] == "NO_SOLUTION"
    buyer.probe("POST", "/quotes", {"kind": "BUNDLE", "solution_id": rejected["id"]}, "BUNDLE_NOT_SOLVED")


def run_together(*actions):
    gate = Barrier(len(actions))

    def invoke(action):
        gate.wait(timeout=3)
        return action()

    with ThreadPoolExecutor(max_workers=len(actions)) as executor:
        return list(executor.map(invoke, actions))


def test_role_access_and_isolation(stack):
    """AC-001, AC-002, AC-003, AC-015, AC-037, AC-046, AC-053."""
    browser, _, _ = stack
    owner = browser()
    owner.create()
    stranger = browser()
    stranger.create()
    first, second, operator = (owner.invite(browser(), role) for role in ("BUYER", "BUYER", "OPERATOR"))
    assert owner.session["sandbox_id"] != stranger.session["sandbox_id"]
    response = operator.request("GET", "/session", headers={"X-Role": "OWNER"})
    assert operator.wire(response)["role"] == "OPERATOR"
    assert operator.call("GET", "/catalog") == owner.call("GET", "/catalog")
    operator.probe("POST", "/catalog/skus", {}, "FORBIDDEN")
    operator.probe("POST", "/demo/role-links", {"role": "OWNER"}, "FORBIDDEN")
    owner.probe("POST", "/demo/role-links", {"role": "OWNER"}, "VALIDATION_ERROR")
    offer = owner.offer()
    buyer_other_shop = stranger.invite(browser(), "BUYER")
    buyer_other_shop.probe("GET", f"/public/offers/{offer['id']}", None, "NOT_FOUND")
    quote = first.quote(offer)
    for candidate in (second, buyer_other_shop):
        candidate.probe("GET", f"/quotes/{quote['id']}", None, "NOT_FOUND")
        candidate.probe("POST", "/reservations", {"quote_id": quote["id"]}, "NOT_FOUND")
    order = first.confirm(first.reserve(quote))
    second.probe("GET", f"/orders/{order['id']}", None, "NOT_FOUND")
    stranger.probe("GET", f"/orders/{order['id']}", None, "NOT_FOUND")
    first.probe(
        "POST",
        f"/orders/{order['id']}/transition",
        {"expected_revision": 1, "to_status": "ACCEPTED", "cancel_reason": None},
        "FORBIDDEN",
    )


def test_draft_competing_updates_and_publication(stack):
    """AC-004, AC-005, AC-006, AC-007, AC-008, AC-055."""
    owner, buyer, _, _ = checkout(stack)
    blank = owner.call("POST", "/offers", {"mode": "FIXED"}, status=201)
    assert blank["state"] == "DRAFT" and blank["published"] is None and not blank["draft"]["lines"]
    oid = blank["id"]
    owner.probe("POST", f"/offers/{oid}/publish", {"expected_revision": 1}, "DRAFT_INCOMPLETE")
    preview = owner.call("POST", f"/offers/{oid}/preview", {"expected_revision": 1})
    assert not preview["valid"] and preview["issues"]
    draft = blank["draft"] | {
        "title": "Café",
        "source": "POST",
        "source_text": "<strong>текст</strong>\n",
        "lines": [{"sku": "MUG", "quantity": 1}],
        "fulfillment_methods": ["PICKUP"],
    }
    path = f"/offers/{oid}/draft"
    requests = [
        lambda title=title: owner.request(
            "PATCH", path, {"expected_revision": 1, "draft": draft | {"title": title}}
        )
        for title in ("Café", "Другая версия")
    ]
    responses = run_together(*requests)
    assert sorted(r.status_code for r in responses) == [200, 409]
    saved = owner.call("GET", f"/offers/{oid}")
    assert saved["draft"]["title"] in ("Café", "Другая версия")
    assert saved["draft"]["source_text"] == draft["source_text"]
    assert saved["draft_revision"] == 2 and saved["published"] is None
    live = owner.call("POST", f"/offers/{oid}/publish", {"expected_revision": 2})
    assert live["published_version_no"] == 1 and live["share_url"] == "/buy/" + oid
    newer_draft = draft | {"title": "Новый черновик"}
    owner.call("PATCH", path, {"expected_revision": live["revision"], "draft": newer_draft})
    public = buyer.call("GET", f"/public/offers/{oid}")
    assert public["version"]["title"] == live["published"]["title"]


def test_quote_approval_and_protected_hold(stack):
    """AC-009, AC-010, AC-016, AC-017, AC-018."""
    owner, buyer, _, _ = checkout(stack)
    old = owner.offer()
    quote = buyer.quote(old)
    hold = buyer.reserve(quote)
    unreserved = buyer.quote(old)
    owner.sku("MUG", title="Кружка")
    buyer.quote(old)
    owner.sku("MUG", price_minor=199000)
    assert buyer.call("GET", f"/public/offers/{old['id']}")["needs_review"]
    buyer.probe("POST", "/quotes", offer_request(old), "OFFER_NEEDS_REVIEW")
    buyer.probe("POST", "/reservations", {"quote_id": unreserved["id"]}, "CATALOG_CHANGED")
    active = owner.call("POST", f"/offers/{old['id']}/publish", {"expected_revision": old["revision"]})
    assert active["published_version_no"] == 2
    buyer.probe("POST", "/quotes", offer_request(old), "OFFER_VERSION_CHANGED")
    buyer.probe("POST", "/reservations", {"quote_id": unreserved["id"]}, "OFFER_VERSION_CHANGED")
    owner.sku("MUG", active=False)
    order = buyer.confirm(hold)
    assert order["total_minor"] == 219000 and order["offer_version_no"] == 1
    assert buyer.call("GET", f"/quotes/{quote['id']}") == quote


def test_fixed_totals_and_strict_payload(stack):
    """AC-011, AC-012, AC-013, AC-014, AC-020, AC-044."""
    owner, buyer, _, _ = checkout(stack)
    offer = owner.offer()
    one = buyer.quote(offer)
    two = buyer.quote(offer, copies=2, fulfillment="COURIER_ZONE_A")
    assert (one["subtotal_minor"], one["fulfillment_fee_minor"], one["total_minor"]) == (219000, 0, 219000)
    assert (two["subtotal_minor"], two["fulfillment_fee_minor"], two["total_minor"]) == (
        438000,
        30000,
        468000,
    )
    for invalid in (True, "1", 1.0, 0, 11):
        buyer.probe("POST", "/quotes", offer_request(offer) | {"copies": invalid}, "VALIDATION_ERROR")
    buyer.probe("POST", "/quotes", offer_request(offer) | {"total_minor": 1}, "VALIDATION_ERROR")
    buyer.probe("POST", "/quotes", offer_request(offer) | {"requested_days": 0}, "VALIDATION_ERROR")
    buyer.probe("POST", "/quotes", offer_request(offer) | {"fulfillment": "UNKNOWN"}, "VALIDATION_ERROR")
    headers = {
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "X-CSRF-Token": buyer.csrf,
        "Idempotency-Key": str(uuid4()),
    }
    for raw in (b'{"kind":"OFFER","kind":"OFFER"}', b'{"copies":NaN}', b'{"text":"\xff"}'):
        response = buyer.client.post("/api/v2/quotes", headers=headers, content=raw)
        assert buyer.wire(response, 400)["code"] == "INVALID_JSON"
    assert (
        buyer.wire(
            buyer.client.post(
                "/api/v2/quotes", headers=headers | {"Origin": "null"}, json=offer_request(offer)
            ),
            403,
        )["code"]
        == "CSRF_REJECTED"
    )
    assert (
        buyer.wire(
            buyer.client.post(
                "/api/v2/quotes", headers=headers | {"X-CSRF-Token": ""}, json=offer_request(offer)
            ),
            403,
        )["code"]
        == "CSRF_REJECTED"
    )
    owner.sku("TEA", stock_on_hand=1)
    error = buyer.probe("POST", "/reservations", {"quote_id": one["id"]}, "INSUFFICIENT_STOCK")
    assert error["available_quantity"] == 1
    assert owner.sku("MUG")["stock_reserved"] == owner.sku("TEA")["stock_reserved"] == 0


@pytest.mark.parametrize("mode", ("quote_before", "quote_at", "hold_before", "hold_at", "hold_replay"))
def test_clock_boundaries_and_replay(stack, mode):
    """AC-019, AC-021, AC-022, AC-023, AC-028, AC-039, AC-041."""
    owner, buyer, clock, _ = checkout(stack)
    offer = owner.offer()
    quote = buyer.quote(offer)
    if mode.startswith("quote_"):
        clock.move(quote["expires_at"])
        if mode == "quote_before":
            clock.advance(-0.001)
            assert buyer.reserve(quote)["state"] == "ACTIVE"
        else:
            buyer.probe("POST", "/reservations", {"quote_id": quote["id"]}, "QUOTE_EXPIRED")
            assert buyer.call("GET", f"/quotes/{quote['id']}") == quote
        return
    key = uuid4()
    hold = buyer.call("POST", "/reservations", {"quote_id": quote["id"]}, status=201, key=key)
    assert owner.sku("MUG")["stock_on_hand"] == 10 and owner.sku("MUG")["stock_available"] == 9
    assert owner.sku("TEA")["stock_reserved"] == 2
    clock.move(hold["expires_at"])
    if mode == "hold_before":
        clock.advance(-0.001)
        assert buyer.confirm(hold)["status"] == "CREATED"
        assert owner.sku("MUG")["stock_on_hand"] == 9
    else:
        if mode == "hold_replay":
            replay = buyer.request("POST", "/reservations", {"quote_id": quote["id"]}, key=key)
            assert buyer.wire(replay, 201) == hold and replay.json()["meta"]["replayed"]
        buyer.probe(
            "POST", f"/reservations/{hold['id']}/confirm", {"expected_revision": 1}, "RESERVATION_EXPIRED"
        )
        current = buyer.request("GET", f"/reservations/{hold['id']}")
        assert buyer.wire(current)["state"] == "EXPIRED"
        assert current.json()["meta"]["server_now"] == hold["expires_at"]
        assert owner.sku("MUG")["stock_on_hand"] == 10 and owner.sku("TEA")["stock_reserved"] == 0


def test_idempotency_conflict_cancel_and_single_quote_use(stack):
    """AC-024 through AC-029 and AC-056."""
    owner, buyer, _, _ = checkout(stack)
    offer = owner.offer()
    q1, q2 = buyer.quote(offer), buyer.quote(offer)
    key = uuid4()
    original = buyer.call("POST", "/reservations", {"quote_id": q1["id"]}, status=201, key=key)
    buyer.probe("POST", "/reservations", {"quote_id": q2["id"]}, "IDEMPOTENCY_CONFLICT", key)
    path = f"/reservations/{original['id']}/cancel"
    cancelled = buyer.call("POST", path, {"expected_revision": 1})
    assert cancelled["state"] == "CANCELLED" and cancelled["cancel_reason"] == "BUYER_CANCELLED"
    buyer.probe("POST", path, {"expected_revision": 1}, "RESERVATION_STATE_CONFLICT")
    buyer.probe("POST", "/reservations", {"quote_id": q1["id"]}, "QUOTE_ALREADY_USED")
    assert owner.sku("MUG")["stock_on_hand"] == 10
    assert owner.sku("MUG")["stock_reserved"] == 0
    hold = buyer.reserve(q2)
    path = f"/reservations/{hold['id']}/confirm"
    key = uuid4()
    responses = run_together(
        *[lambda: buyer.request("POST", path, {"expected_revision": 1}, key=key) for _ in range(2)]
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["data"] == responses[1].json()["data"]
    assert sum(response.json()["meta"]["replayed"] for response in responses) == 1
    buyer.probe("POST", path, {"expected_revision": 1}, "RESERVATION_STATE_CONFLICT")
    assert owner.sku("MUG")["stock_on_hand"] == 9
    assert owner.call("GET", "/dashboard")["orders_created"] == 1


def test_atomic_rollback_before_receipt(stack, monkeypatch):
    """AC-030."""
    from app.v2 import checkout as commerce
    from app.v2.core import Failure

    owner, buyer, _, _ = checkout(stack)
    hold = buyer.reserve(buyer.quote(owner.offer()))
    natural = commerce.emit
    key = uuid4()

    def stop_after_stock_change(ctx, name, **values):
        result = natural(ctx, name, **values)
        if name == "order_created":
            raise Failure("INTERNAL_ERROR")
        return result

    with monkeypatch.context() as fault:
        fault.setattr(commerce, "emit", stop_after_stock_change)
        buyer.probe(
            "POST", f"/reservations/{hold['id']}/confirm", {"expected_revision": 1}, "INTERNAL_ERROR", key
        )
    assert owner.sku("MUG")["stock_reserved"] == 1 and owner.sku("MUG")["stock_on_hand"] == 10
    assert owner.call("GET", "/dashboard")["orders_created"] == 0
    assert buyer.confirm(hold, key)["status"] == "CREATED"


@pytest.mark.parametrize("first", ("pause", "confirm", "race"))
def test_pause_and_confirm_serialization(stack, first):
    """AC-032, AC-033."""
    owner, buyer, _, _ = checkout(stack)
    offer = owner.offer()
    hold = buyer.reserve(buyer.quote(offer))

    def pause():
        return owner.request("POST", f"/offers/{offer['id']}/pause", {"expected_revision": offer["revision"]})

    def confirm():
        return buyer.request("POST", f"/reservations/{hold['id']}/confirm", {"expected_revision": 1})

    if first == "race":
        p, o = run_together(pause, confirm)
    elif first == "pause":
        p, o = pause(), confirm()
    else:
        o, p = confirm(), pause()
    assert owner.wire(p)["state"] == "PAUSED"
    if first == "confirm" or o.status_code == 201:
        assert buyer.wire(o, 201)["status"] == "CREATED"
        assert owner.sku("MUG")["stock_on_hand"] == 9
    else:
        assert buyer.wire(o, 409)["code"] == "RESERVATION_STATE_CONFLICT"
        observed = buyer.call("GET", f"/reservations/{hold['id']}")
        assert observed["cancel_reason"] == "OFFER_UNAVAILABLE"
        assert owner.sku("MUG")["stock_on_hand"] == 10
    assert owner.sku("MUG")["stock_reserved"] == 0


def test_last_stock_concurrent_and_max_reservations(stack):
    """AC-031 and the active hold cap."""
    owner, buyer, clock, _ = checkout(stack)
    browser, _, _ = stack
    rival = owner.invite(browser(), "BUYER")
    owner.sku("MUG", stock_on_hand=1)
    offer = owner.offer(other=0)
    rounds = (buyer.quote(offer), rival.quote(offer))
    responses = run_together(
        lambda: buyer.request("POST", "/reservations", {"quote_id": rounds[0]["id"]}),
        lambda: rival.request("POST", "/reservations", {"quote_id": rounds[1]["id"]}),
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert next(r for r in responses if r.status_code == 409).json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert owner.sku("MUG")["stock_reserved"] == 1
    clock.advance(300)
    owner.sku("MUG", stock_on_hand=15)
    for _ in range(5):
        buyer.reserve(buyer.quote(offer))
    q = buyer.quote(offer)
    buyer.probe("POST", "/reservations", {"quote_id": q["id"]}, "RATE_LIMITED")


def test_order_states_capacity_and_cancelled_dashboard(stack):
    """AC-034, AC-035, AC-036, AC-047, AC-054."""
    owner, buyer, _, _ = checkout(stack)
    offer = owner.offer(amount=2, other=0)
    order = buyer.confirm(buyer.reserve(buyer.quote(offer)))
    path = f"/orders/{order['id']}/transition"
    owner.probe(
        "POST",
        path,
        {"expected_revision": 1, "to_status": "FULFILLED", "cancel_reason": None},
        "ORDER_STATE_CONFLICT",
    )
    accepted = owner.call(
        "POST", path, {"expected_revision": 1, "to_status": "ACCEPTED", "cancel_reason": None}
    )
    sku = owner.sku("MUG")
    patch = {
        "expected_revision": sku["revision"],
        "title": sku["title"],
        "price_minor": sku["price_minor"],
        "stock_on_hand": 99998,
        "active": True,
        "ready_in_days": 1,
    }
    fields = owner.probe("PATCH", "/catalog/skus/MUG", patch, "STOCK_CAPACITY_CONFLICT")["fields"]
    assert fields[0]["path"] == "/stock_on_hand"
    owner.call("PATCH", "/catalog/skus/MUG", patch | {"stock_on_hand": 99997})
    key = uuid4()
    change = {"expected_revision": 2, "to_status": "CANCELLED", "cancel_reason": "CUSTOMER_REQUEST"}
    cancelled = owner.call("POST", path, change, key=key)
    assert cancelled["accepted_at"] == accepted["accepted_at"]
    assert owner.call("POST", path, change, key=key) == cancelled
    owner.probe("POST", path, change | {"expected_revision": 3}, "ORDER_STATE_CONFLICT")
    assert owner.sku("MUG")["stock_on_hand"] == 99999
    snapshot = owner.call("GET", "/dashboard")
    assert snapshot["orders_cancelled"] == 1 and snapshot["order_value_minor"] == order["total_minor"]
    assert snapshot["is_test"] and snapshot["native_orders_count"] == "NOT_MEASURED"


def test_role_link_replay_with_browser_loss_and_active_owner(stack):
    """AC-045, AC-051, AC-052."""
    browser, clock, _ = stack
    owner = browser()
    owner.create()
    issued = owner.call("POST", "/demo/role-links", {"role": "BUYER"}, status=201)
    token = issued["url"].split("=")[1]
    owner.bootstrap()
    owner.probe("POST", "/demo/sessions", {"token": token}, "SESSION_ALREADY_ACTIVE")
    owner.probe(
        "POST", "/demo/sandboxes", {"seed": "CORE-001", "enable_bundle": False}, "SESSION_ALREADY_ACTIVE"
    )
    assert owner.call("GET", "/session")["role"] == "OWNER"
    buyer = browser()
    buyer.bootstrap()
    key = uuid4()
    first = buyer.request("POST", "/demo/sessions", {"token": token}, key=key)
    facts = buyer.wire(first)
    assert "HttpOnly" in first.headers["set-cookie"] and "SameSite=strict" in first.headers["set-cookie"]
    first_cookie = buyer.client.cookies.get("vk_demo_session")
    buyer.client.cookies.delete("vk_demo_session")
    clock.advance(1)
    replay = buyer.request("POST", "/demo/sessions", {"token": token}, key=key)
    assert buyer.wire(replay) == facts and replay.json()["meta"]["replayed"]
    assert buyer.client.cookies.get("vk_demo_session") == first_cookie
    another = browser()
    another.bootstrap()
    another.probe("POST", "/demo/sessions", {"token": token}, "ROLE_LINK_CONSUMED")


def test_reset_retains_owner_receipts_and_revokes_previous_buyers(stack):
    """AC-040."""
    owner, buyer, _, _ = checkout(stack)
    browser, _, _ = stack
    key = uuid4()
    offer = owner.call("POST", "/offers", {"mode": "FIXED"}, status=201, key=key)
    link = owner.call("POST", "/demo/role-links", {"role": "OPERATOR"}, status=201)
    reset_key = uuid4()
    result = owner.call("POST", "/demo/reset", {"expected_generation": 1}, key=reset_key)
    assert result["generation"] == 2
    assert owner.call("POST", "/demo/reset", {"expected_generation": 1}, key=reset_key) == result
    owner.probe("POST", "/demo/reset", {"expected_generation": 1}, "REVISION_CONFLICT")
    assert owner.call("GET", "/session")["generation"] == 2
    buyer.probe("GET", "/session", None, "SESSION_STALE")
    assert owner.call("POST", "/offers", {"mode": "FIXED"}, status=201, key=key) == offer
    owner.probe("GET", f"/offers/{offer['id']}", None, "NOT_FOUND")
    old = browser()
    old.bootstrap()
    old.probe("POST", "/demo/sessions", {"token": link["url"].split("=")[1]}, "ROLE_LINK_EXPIRED")


def test_event_origin_and_deduplication(stack):
    """AC-038 and the UI event permit list."""
    owner, buyer, _, worker = checkout(stack)
    offer = owner.offer()
    order = buyer.confirm(buyer.reserve(buyer.quote(offer)))
    body = {
        "event_id": str(uuid4()),
        "name": "offer_view",
        "offer_id": offer["id"],
        "offer_version_no": 1,
        "ui_step": None,
    }
    assert not buyer.call("POST", "/ui-events", body)["duplicate"]
    assert buyer.call("POST", "/ui-events", body)["duplicate"]
    buyer.probe("POST", "/ui-events", body | {"name": "cta_click"}, "IDEMPOTENCY_CONFLICT")
    buyer.probe("POST", "/ui-events", body | {"name": "order_created"}, "VALIDATION_ERROR")
    buyer.probe("POST", "/ui-events", body | {"offer_version_no": None}, "VALIDATION_ERROR")
    for _ in range(5):
        worker.deliver()
    with owner.service.engine.begin() as con:
        db = Store(con)
        scope = owner.session["sandbox_id"]
        record = db.first(
            "SELECT * FROM events WHERE sandbox_id=%s AND payload->>'order_id'=%s AND name='order_created'",
            scope,
            order["id"],
        )
        assert record["origin"] == "SERVER"
        assert (
            db.first("SELECT count(*) n FROM event_delivery WHERE event_id=%s", record["event_id"])["n"] == 1
        )
        db.run("UPDATE outbox SET delivered_at=NULL WHERE event_id=%s", record["event_id"])
    worker.deliver()
    with owner.service.engine.begin() as con:
        db = Store(con)
        assert (
            db.first("SELECT count(*) n FROM event_delivery WHERE event_id=%s", record["event_id"])["n"] == 1
        )
        assert db.first("SELECT count(*) n FROM events WHERE sandbox_id=%s AND origin='UI'", scope)["n"] == 1


def test_offer_archive_releases_old_versions(stack):
    owner, buyer, _, _ = checkout(stack)
    original = owner.offer()
    held = buyer.reserve(buyer.quote(original))
    identity = original["id"]
    latest = owner.call("POST", f"/offers/{identity}/publish", {"expected_revision": original["revision"]})
    fresh = buyer.reserve(buyer.quote(latest))
    archived = owner.call("POST", f"/offers/{identity}/archive", {"expected_revision": latest["revision"]})
    assert archived["state"] == "ARCHIVED"
    for reserve in (held, fresh):
        view = buyer.call("GET", f"/reservations/{reserve['id']}")
        assert view["state"] == "CANCELLED" and view["cancel_reason"] == "OFFER_UNAVAILABLE"
    buyer.probe("GET", f"/public/offers/{identity}", None, "OFFER_UNAVAILABLE")
    assert owner.sku("MUG")["stock_reserved"] == owner.sku("TEA")["stock_reserved"] == 0
    owner.probe(
        "POST",
        f"/offers/{identity}/preview",
        {"expected_revision": archived["revision"]},
        "OFFER_UNAVAILABLE",
    )


def test_catalog_pagination_and_expiry(stack):
    owner, _, clock, _ = checkout(stack)
    items = owner.call("GET", "/catalog")["items"]
    first = owner.call("GET", "/catalog?limit=3")
    second = owner.call("GET", "/catalog?limit=3&cursor=" + first["next_cursor"])
    assert first["items"] + second["items"] == items[:6]
    clock.move(owner.session["sandbox_expires_at"])
    owner.probe("GET", "/session", None, "SANDBOX_EXPIRED")


def test_role_exchange_reset_race(stack):
    owner, _, _, _ = checkout(stack)
    browser, _, _ = stack
    guest = browser()
    guest.bootstrap()
    token = owner.call("POST", "/demo/role-links", {"role": "BUYER"}, status=201)["url"].split("=")[1]
    exchange, reset = run_together(
        lambda: guest.request("POST", "/demo/sessions", {"token": token}),
        lambda: owner.request("POST", "/demo/reset", {"expected_generation": 1}),
    )
    assert owner.wire(reset)["generation"] == 2
    if exchange.status_code == 200:
        guest.probe("GET", "/session", None, "SESSION_STALE")
    else:
        assert guest.wire(exchange, 410)["code"] == "ROLE_LINK_EXPIRED"


def test_bundle_ownership_staleness_and_feature_switch(stack):
    browser, clock, _ = stack
    disabled = browser()
    disabled.create()
    disabled.probe("POST", "/offers", {"mode": "BUNDLE"}, "MODULE_DISABLED")
    disabled.probe("GET", "/bundle-templates", None, "MODULE_DISABLED")
    owner, buyer, _, _ = checkout(stack, bundle=True)
    outsider = owner.invite(browser(), "BUYER")
    published = owner.offer("BUNDLE")
    request = {
        "offer_id": published["id"],
        "offer_version_no": 1,
        "guests": 4,
        "budget_minor": 500000,
        "owned": ["TABLECLOTH"],
        "fulfillment": "PICKUP",
        "requested_days": 1,
    }
    result = buyer.call("POST", "/bundle-solutions", request, status=201)
    identity = result["id"]
    assert buyer.call("GET", f"/bundle-solutions/{identity}") == result
    outsider.probe("GET", f"/bundle-solutions/{identity}", None, "NOT_FOUND")
    outsider.probe("POST", "/quotes", {"kind": "BUNDLE", "solution_id": identity}, "NOT_FOUND")
    saved_quote = buyer.call("POST", "/quotes", {"kind": "BUNDLE", "solution_id": identity}, status=201)
    assert saved_quote["total_minor"] == result["total_minor"]
    owner.sku("PLATE_B", price_minor=55222)
    buyer.probe("POST", "/quotes", {"kind": "BUNDLE", "solution_id": identity}, "BUNDLE_SOLUTION_STALE")
    clock.move(result["expires_at"])
    buyer.probe("POST", "/quotes", {"kind": "BUNDLE", "solution_id": identity}, "BUNDLE_SOLUTION_EXPIRED")


def freeze_input(name):
    return json.loads((ROOT / "fixtures/v2" / name).read_text())


BUNDLES = freeze_input("bundle_golden.json")["cases"]


@pytest.mark.parametrize("case", BUNDLES, ids=lambda case: case["id"])
def test_all_bundle_goldens_against_oracle(stack, case):
    from reference_oracle_v2 import bundle_solve

    owner, buyer, _, _ = checkout(stack, bundle=True)
    override = case.get("catalog_overrides", {})
    for sku, changes in override.items():
        owner.sku(sku, **changes)
    catalog = [sku | override.get(sku["sku"], {}) for sku in freeze_input("bundle_catalog.json")["catalog"]]
    catalog.extend(case.get("catalog_add", []))
    template = freeze_input("bundle_template.json")
    chosen = None
    if case.get("slot_add"):
        extra = {row["sku"]: row for row in case["catalog_add"]}
        for code, item in extra.items():
            owner.call(
                "POST",
                "/catalog/skus",
                {
                    "sku": code,
                    "title": code,
                    "price_minor": item["unit_price_minor"],
                    **{
                        key: item[key]
                        for key in ("active", "ready_in_days", "stock_on_hand", "units_per_pack")
                    },
                },
                status=201,
            )
        chosen = str(uuid4())
        dto = owner.call("GET", "/bundle-templates")["items"][0] | {"id": chosen}
        for slot in dto["slots"]:
            for sku in case["slot_add"].get(slot["slot_id"], []):
                slot["alternatives"].append(
                    {"sku": sku, **{key: extra[sku][key] for key in ("units_per_pack", "preference_rank")}}
                )
        for slot in template["slots"]:
            slot["alternatives"].extend(case["slot_add"].get(slot["slot_id"], []))
        with owner.service.engine.begin() as con:
            db = Store(con)
            db.insert(
                "bundle_templates",
                {
                    "sandbox_id": owner.session["sandbox_id"],
                    "generation": 1,
                    "id": chosen,
                    "version_no": 1,
                    "code": "B001",
                    "snapshot": dto,
                    "created_at": owner.service.clock.now(db),
                },
            )
    published = owner.offer("BUNDLE", template=chosen)
    request = case["request"] | {"offer_id": published["id"], "offer_version_no": 1}
    if "expected_error" in case:
        with pytest.raises(ValueError, match=case["expected_error"]):
            bundle_solve(catalog, template, case["request"])
        buyer.probe("POST", "/bundle-solutions", request, "VALIDATION_ERROR")
        return
    exact = bundle_solve(catalog, template, case["request"])
    actual = buyer.call("POST", "/bundle-solutions", request, status=201)
    expected = case["expected"]
    assert actual["status"] == exact["status"] == expected["status"]
    if actual["status"] == "NO_SOLUTION":
        assert actual["reason"] == exact["reason"] == expected["reason"]
        return
    assert actual["total_minor"] == exact["total_minor"] == expected["total_minor"]
    objective = actual["objective"]
    assert objective["preference_penalty"] == exact["preference_penalty"] == expected["preference_penalty"]
    assert objective["surplus_units"] == exact["surplus_units"] == expected["surplus_units"]
    assert objective["sku_tiebreak"] == exact["sku_tiebreak"]
    assert {line["sku"]: line["quantity"] for line in actual["lines"]} == expected["quantities"]


def test_bundle_compute_watchdog_is_not_no_solution(stack, monkeypatch):
    from app.v2 import bundle

    owner, buyer, _, _ = checkout(stack, bundle=True)
    offer = owner.offer("BUNDLE")
    monkeypatch.setattr(bundle, "monotonic", lambda: float("inf"))
    buyer.probe(
        "POST",
        "/bundle-solutions",
        {
            "offer_id": offer["id"],
            "offer_version_no": 1,
            "guests": 4,
            "budget_minor": 500000,
            "owned": ["TABLECLOTH"],
            "fulfillment": "PICKUP",
            "requested_days": 1,
        },
        "COMPUTATION_UNAVAILABLE",
    )
    with owner.service.engine.begin() as con:
        assert (
            Store(con).first(
                "SELECT count(*) n FROM bundle_solutions WHERE sandbox_id=%s", owner.session["sandbox_id"]
            )["n"]
            == 0
        )


def test_contract_hmac_inputs_and_frozen_hashes(stack):
    from app.common import canonical, digest

    owner, _, _, _ = checkout(stack)
    for vector in freeze_input("hash_vectors.json")["cases"]:
        assert canonical(vector["payload"]) == vector["canonical_utf8"].encode()
        assert digest(vector["payload"]) == vector["sha256"]
    raw = owner.client.cookies.get("vk_demo_session")
    with owner.service.engine.begin() as con:
        db = Store(con)
        value = db.first(
            "SELECT token_hash,csrf_hash FROM sessions WHERE actor_id=%s", owner.session["actor_id"]
        )
        assert bytes(value["token_hash"]) != raw.encode()
        assert bytes(value["token_hash"]) != bytes(value["csrf_hash"])
