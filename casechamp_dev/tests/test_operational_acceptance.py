from pathlib import Path
from uuid import uuid4

import pytest
from app import events
from app.common import Problem
from app.db import DB
from app.worker import Worker
from conftest import bootstrap, call
from test_http_core import assert_wire, intent
from test_transactions_acceptance import Checkout, assert_error, pair

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def isolated_jobs(worker, checkout):
    db_candidates = worker.job_candidates

    def in_sandbox(*, expiry_only):
        return [
            row
            for row in db_candidates(expiry_only=expiry_only)
            if str(row["sandbox_id"]) == checkout.context["sandbox_id"]
        ]

    worker.job_candidates = in_sandbox
    return worker


def job_request(checkout, text="За столом будет 6 гостей."):
    return checkout.send(
        "POST",
        "/ml/intent-jobs",
        {
            "scenario_revision_id": checkout.context["latest_scenario_revision_id"],
            "text": text,
        },
    )


def upload(client, service, name):
    response = call(
        client,
        "POST",
        "/assets",
        epoch=1,
        data={"kind": "PRODUCT_IMAGE" if name == "plate" else "IMAGE"},
        files={
            "file": (f"{name}.png", (FIXTURES / "replay_assets" / f"{name}.png").read_bytes(), "image/png")
        },
    )
    return assert_wire(service, response, "Asset", 201)


@pytest.mark.parametrize("case", range(1, 7), ids=[f"SEC-{i:02d}" for i in range(1, 7)])
def test_isolation(case, controlled_backend, caplog):
    start, clock = controlled_backend
    client, service = start(ml_mode="REPLAY" if case == 6 else "DISABLED")
    a = Checkout(client, service)
    if case in (1, 4):
        other_client, other_service = start()
        b = Checkout(other_client, other_service)
        original_a, original_b = a.inventory(), b.inventory()
        quote = a.quote()
        assert_error(other_service, b.send("GET", f"/quotes/{quote['quote_id']}"), "RESOURCE_NOT_FOUND")
        b.reserve(quote, expected="RESOURCE_NOT_FOUND")
        assert (a.inventory(), b.inventory()) == (original_a, original_b)
        if case == 4:
            first, second = a.quote(client_run_id=str(uuid4())), a.quote(client_run_id=str(uuid4()))
            sku = first["lines"][0]["sku_id"]
            row = a.sku(sku)
            a.patch(
                row,
                stock_on_hand_packs=first["lines"][0]["pack_count"],
                expected_inventory_version=row["inventory_version"],
            )
            cmds = [
                lambda q=q: a.send(
                    "POST",
                    "/reservations",
                    {"quote_id": q["quote_id"], "accepted_quote_hash": q["quote_hash"]},
                )
                for q in (first, second)
            ]
            assert sorted(response.status_code for response in pair(cmds)) == [201, 409]
            assert b.inventory() == original_b
    if case == 2:
        body = intent(a.context) | {"sandbox_id": str(uuid4()), "epoch": 99}
        assert_error(service, a.send("POST", "/quotes", body), "VALIDATION_ERROR")
        assert a.data("GET", "/context", "Context")["sandbox_id"] == a.context["sandbox_id"]
    if case == 3:
        clock.move(a.context["expires_at"])
        assert_error(service, a.send("GET", "/catalog"), "DEMO_SESSION_EXPIRED")
        assert_error(service, a.send("POST", "/quotes", intent(a.context)), "DEMO_SESSION_EXPIRED")
        assert bootstrap(client)["sandbox_id"] != a.context["sandbox_id"]
    if case == 5:
        for origin in ("https://invalid.example", "null", None):
            headers = {"X-Sandbox-Epoch": "1", "Idempotency-Key": str(uuid4())}
            if origin is not None:
                headers["Origin"] = origin
            response = client.post("/api/v1/quotes", headers=headers, json=intent(a.context))
            assert_error(service, response, "ORIGIN_NOT_ALLOWED")
        token = client.cookies.get(service.cookie_name)
        assert token not in caplog.text
        assert a.query("SELECT quote_id FROM vk_demo.quotes WHERE sandbox_id=%s AND epoch=%s") == []
    if case == 6:
        reference = upload(client, service, "plate")
        scene = upload(client, service, "scene")
        catalog_before = a.get_catalog()
        a.patch(a.sku("PLATE_WHITE1"), image_asset_id=reference["asset_id"])
        anonymous = a.data(
            "POST",
            "/scenarios",
            "Scenario",
            {
                "title": "Проверка",
                "caption": "Демонстрационная белая тарелка.",
                "asset_id": scene["asset_id"],
            },
            status=201,
        )
        ident = anonymous["scenario_id"]
        job = a.data(
            "POST", f"/scenarios/{ident}/analysis-jobs", "Job", {"expected_draft_version": 1}, status=202
        )
        worker = isolated_jobs(Worker(service.settings, engine=service.engine, clock=clock), a)
        assert worker.process_one_job()
        view = a.data("GET", f"/jobs/{job['job_id']}", "Job")
        assert view["status"] == "SUCCEEDED" and view["result"]["provider_mode"] == "REPLAY"
        assert view["result"]["mapping_suggestions"][0]["candidates"][0]["sku_id"] == "PLATE_WHITE1"
        assert_error(
            service,
            a.send("POST", f"/scenarios/{ident}/publish", {"expected_draft_version": 1}),
            "MAPPING_UNCONFIRMED",
        )
        slots = anonymous["slots"]
        slots[0] |= {
            "primary_sku_id": "FAKE_NOT_IN_CATALOG",
            "mapping_status": "CONFIRMED",
            "mapping_source": "ML_REVIEWED",
            "source_job_id": job["job_id"],
        }
        assert_error(
            service,
            a.send("PATCH", f"/scenarios/{ident}", {"expected_draft_version": 1, "slots": slots}),
            "MAPPING_INVALID",
        )
        assert len(a.get_catalog()["items"]) == len(catalog_before["items"])
        assert all(row["sku_id"] != "FAKE_NOT_IN_CATALOG" for row in a.get_catalog()["items"])


@pytest.mark.parametrize("case", range(1, 5), ids=[f"EVT-{i:02d}" for i in range(1, 5)])
def test_events(case, controlled_backend, monkeypatch):
    start, clock = controlled_backend
    client, service = start()
    c = Checkout(client, service)
    quote = c.quote()
    reservation = c.reserve(quote)
    worker = Worker(service.settings, engine=service.engine, clock=clock)
    if case == 1:
        real_emit = events.emit

        def abort(scope, name, *payload):
            identity = real_emit(scope, name, *payload)
            if name == "order.test_confirmed":
                raise Problem("INTERNAL_INVARIANT_FAILED")
            return identity

        before = c.inventory()
        monkeypatch.setattr(events, "emit", abort)
        c.confirm(reservation, expected="INTERNAL_INVARIANT_FAILED")
        assert c.inventory() == before
        assert (
            c.query(
                """SELECT event_id FROM vk_demo.outbox_events WHERE sandbox_id=%s AND epoch=%s
               AND event_name='order.test_confirmed'"""
            )
            == []
        )
        assert c.query("SELECT order_id FROM vk_demo.orders WHERE sandbox_id=%s AND epoch=%s") == []
        return
    order = c.confirm(reservation)
    if case == 2:
        assert c.query("SELECT event_id FROM vk_demo.event_projection WHERE sandbox_id=%s AND epoch=%s") == []
        restarted = Worker(service.settings, engine=service.engine, clock=clock)
        for _ in range(15):
            restarted.deliver_outbox()
        ids = c.query(
            """SELECT payload->>'resource_id' order_id FROM vk_demo.event_projection
               WHERE sandbox_id=%s AND epoch=%s AND event_name='order.test_confirmed'"""
        )
        assert ids == [{"order_id": order["order_id"]}]
    if case == 3:
        with service.engine.connect() as connection:
            row = DB(connection).first(
                """SELECT * FROM vk_demo.outbox_events WHERE sandbox_id=%s AND epoch=%s AND aggregate_id=%s""",
                c.context["sandbox_id"],
                c.epoch,
                order["order_id"],
            )
        identity = {key: row[key] for key in ("sandbox_id", "epoch", "event_id")}
        worker.project_outbox(row, identity)
        worker.project_outbox(row, identity)
        assert c.query(
            """SELECT count(*) count FROM vk_demo.event_projection
            WHERE sandbox_id=%s AND epoch=%s AND event_id=%s""",
            row["event_id"],
        ) == [{"count": 1}]
    if case == 4:
        c.cancel(order, "order")
        lineage = {
            row["event_name"]: row["payload"]["facts"]
            for row in c.query(
                "SELECT event_name,payload FROM vk_demo.outbox_events WHERE sandbox_id=%s AND epoch=%s"
            )
        }
        confirmed = lineage["order.test_confirmed"]
        assert confirmed["scenario_revision_id"] == c.context["latest_scenario_revision_id"]
        assert (
            confirmed["quote_id"] == quote["quote_id"]
            and confirmed["accepted_quote_hash"] == quote["quote_hash"]
        )
        assert confirmed["product_versions"] == {x["sku_id"]: x["product_version"] for x in order["lines"]}
        assert confirmed["solution_hash"] == lineage["quote.created"]["solution_hash"]
        assert lineage["reservation.created"]["quote_id"] == quote["quote_id"]
        assert lineage["order.test_canceled"]["returned_packs"] == {
            x["sku_id"]: x["pack_count"] for x in order["lines"]
        }


def preview(c, content):
    response = call(
        c.client,
        "POST",
        "/catalog/imports",
        epoch=c.epoch,
        files={"file": ("catalog.csv", content, "text/csv")},
    )
    return assert_wire(c.service, response, "CatalogImport", 201)


def commit(c, imported):
    return c.send(
        "POST",
        f"/catalog/imports/{imported['import_id']}/commit",
        {"expected_catalog_version": imported["base_catalog_version"]},
    )


@pytest.mark.parametrize("case", (1, 2, 3), ids=("IMP-01", "IMP-02", "IMP-03"))
def test_csv_import(case, controlled_backend):
    start, clock = controlled_backend
    client, service = start()
    fixture = (FIXTURES / "catalog_import.csv").read_text(encoding="utf-8-sig").splitlines()
    header, first = fixture[:2]
    c = Checkout(client, service, context=bootstrap(client, "EMPTY_V1" if case == 1 else "TABLE_DEMO_V1"))
    before = c.get_catalog()
    if case == 1:
        values = first.split(",")
        values[0], values[5] = "BROKEN", "250.00"
        imported = preview(c, "\n".join((header, first, ",".join(values))))
        assert imported["status"] == "INVALID" and imported["errors"]
        assert_error(service, commit(c, imported), "IMPORT_VALIDATION_FAILED")
        assert c.get_catalog() == before
        valid = preview(c, "\n".join(fixture))
        applied = assert_wire(service, commit(c, valid), "Catalog", 200)
        assert len(applied["items"]) == 17
    if case == 2:
        same = preview(c, "\n".join((header, first, first)))
        assert same["status"] == "INVALID"
        values = first.split(",")
        values[5] = "1e5"
        assert preview(c, "\n".join((header, ",".join(values))))["errors"]
        clock.advance(60)
        lines = []
        for i in range(501):
            copy = first.split(",")
            copy[0] = f"SKU-{i:03d}"
            lines.append(",".join(copy))
        overflow = preview(c, "\n".join((header, *lines)))
        assert "CATALOG_LIMIT_EXCEEDED" in [item["code"] for item in overflow["errors"]]
        row = c.sku("PLATE_WHITE1")
        c.patch(row, expected="RESOURCE_NOT_FOUND", image_asset_id=str(uuid4()))
        assert c.get_catalog() == before
    if case == 3:
        row = first.split(",")
        row[4] = str(int(row[4]) + 1)
        row[7] = "false"
        invalid = preview(c, "\n".join((header, ",".join(row))))
        assert {item["path"].split(".")[-1] for item in invalid["errors"]} >= {"active", "pack_units"}
        assert_error(service, commit(c, invalid), "IMPORT_VALIDATION_FAILED")
        assert c.get_catalog() == before
        row[0] = "NEW_VARIANT"
        valid = preview(c, "\n".join((header, ",".join(row))))
        assert_wire(service, commit(c, valid), "Catalog", 200)
        assert c.sku("NEW_VARIANT")["active"] is False


def test_optional_primary_required(controlled_backend):
    """PUB-01"""
    start, _ = controlled_backend
    client, service = start()
    c = Checkout(client, service)
    revision = c.data(
        "GET", f"/scenario-revisions/{c.context['latest_scenario_revision_id']}", "ScenarioRevision"
    )
    created = c.data(
        "POST",
        "/scenarios",
        "Scenario",
        {"title": "Ручная публикация", "caption": "", "asset_id": None},
        status=201,
    )
    scenario = created["scenario_id"]
    partial = list(revision["slots"])
    partial[1] = created["slots"][1]
    draft = c.data(
        "PATCH", f"/scenarios/{scenario}", "Scenario", {"expected_draft_version": 1, "slots": partial}
    )
    reply = c.send(
        "POST", f"/scenarios/{scenario}/publish", {"expected_draft_version": draft["draft_version"]}
    )
    assert_error(service, reply, "MAPPING_UNCONFIRMED")
    row = c.sku(revision["slots"][1]["primary_sku_id"])
    c.patch(row, stock_on_hand_packs=0, expected_inventory_version=row["inventory_version"])
    draft = c.data(
        "PATCH",
        f"/scenarios/{scenario}",
        "Scenario",
        {"expected_draft_version": draft["draft_version"], "slots": revision["slots"]},
    )
    final = c.data(
        "POST",
        f"/scenarios/{scenario}/publish",
        "ScenarioRevision",
        {"expected_draft_version": draft["draft_version"]},
        status=201,
    )
    assert all(slot["mapping_status"] == "CONFIRMED" for slot in final["slots"])


@pytest.mark.parametrize("case", ("LAST_STOCK_RACE", "PRICE_CHANGE", "EXPIRED_RESERVATION"))
def test_demo_simulations(controlled_backend, case):
    start, _ = controlled_backend
    client, service = start()
    c = Checkout(client, service)
    key = uuid4()
    body = {"case_id": case, "scenario_revision_id": c.context["latest_scenario_revision_id"]}
    result = c.data("POST", "/demo/simulations", "SimulationResult", body, key=key)
    assert result["passed"] and all(item["passed"] for item in result["assertions"]), result
    replay = c.send("POST", "/demo/simulations", body, key=key)
    assert replay.json()["meta"]["idempotency_replayed"]
    assert replay.json()["data"] == result
    assert c.data("GET", "/context", "Context")["latest_scenario_revision_id"] == body["scenario_revision_id"]
    c.invariant()


@pytest.mark.parametrize(
    "boundary", ("queue_59999", "queue_60000", "running_deadline", "max_unfinished", "exact_replay")
)
def test_ml_deadlines_and_replay(controlled_backend, boundary):
    start, clock = controlled_backend
    client, service = start(ml_mode="REPLAY")
    c = Checkout(client, service)
    worker = isolated_jobs(Worker(service.settings, engine=service.engine, clock=clock), c)
    queued = assert_wire(service, job_request(c), "Job", 202)
    assert queued["status"] == "QUEUED" and queued["started_at"] is None
    jid = queued["job_id"]
    if boundary == "queue_59999":
        clock.advance(59.999)
        assert worker.claim_job()["status"] == "RUNNING"
        live = c.data("GET", f"/jobs/{jid}", "Job")
        assert live["deadline_at"] == "2026-11-20T10:01:29.999Z"
    if boundary == "queue_60000":
        clock.advance(60)
        assert worker.claim_job() is None
        final = c.data("GET", f"/jobs/{jid}", "Job")
        assert final["status"] == "TIMED_OUT" and final["failure_code"] == "QUEUE_TIMEOUT"
        assert final["started_at"] is None
    if boundary == "running_deadline":

        def delayed(settings, snapshot, timeout):
            clock.advance(timeout)
            worker.expire_jobs()
            return {"fake": "late"}, None

        worker.provider = delayed
        assert worker.process_one_job()
        final = c.data("GET", f"/jobs/{jid}", "Job")
        assert final["status"] == "TIMED_OUT" and final["failure_code"] == "RUN_TIMEOUT"
        assert final["result"] is None
    if boundary == "max_unfinished":
        assert job_request(c).status_code == 202
        error = assert_error(service, job_request(c), "RATE_LIMITED")
        assert error["details"]["reason_codes"] == ["MAX_UNFINISHED_JOBS"]
    if boundary == "exact_replay":
        assert worker.process_one_job()
        final = c.data("GET", f"/jobs/{jid}", "Job")
        assert final["status"] == "SUCCEEDED"
        assert final["result"]["intent_draft"] == {"guest_count": 6}
        assert final["result"]["model_revision"] == "fixture-authored-1.0.0"
        miss = assert_wire(service, job_request(c, "Уже семь гостей."), "Job", 202)
        assert worker.process_one_job()
        final_miss = c.data("GET", f"/jobs/{miss['job_id']}", "Job")
        assert final_miss["status"] == "FAILED" and final_miss["failure_code"] == "REPLAY_MISS"
