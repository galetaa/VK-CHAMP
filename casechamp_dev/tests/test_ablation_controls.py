"""Counterfactual checks confined to the disposable PostgreSQL acceptance fixture."""

from uuid import uuid4

from app.db import Scope
from conftest import call
from test_http_core import assert_wire
from test_transactions_acceptance import Checkout


def test_ablation_without_product_version_recheck(controlled_backend, monkeypatch):
    start, _ = controlled_backend
    client, service = start()
    checkout = Checkout(client, service)
    protected, shadow = checkout.quote(), checkout.quote()
    selected = protected["lines"][0]
    sku = selected["sku_id"]
    row = checkout.sku(sku)
    checkout.patch(row, price_minor=row["price_minor"] + 100)
    checkout.reserve(protected, expected="QUOTE_STALE")
    require = Scope.require

    def old_version(scope, table, **keys):
        data = require(scope, table, **keys)
        if table == "products" and keys["sku_id"] == sku:
            return data | {"product_version": selected["product_version"]}
        return data

    monkeypatch.setattr(Scope, "require", old_version)
    assert checkout.reserve(shadow)["status"] == "ACTIVE"


def test_ablation_without_receipts(controlled_backend, monkeypatch):
    start, _ = controlled_backend
    client, service = start()
    checkout = Checkout(client, service)
    base = checkout.data(
        "GET", f"/scenario-revisions/{checkout.context['latest_scenario_revision_id']}", "ScenarioRevision"
    )
    path, body = f"/scenarios/{base['scenario_id']}/publish", {"expected_draft_version": 1}
    key = uuid4()

    def publish(key):
        response = call(client, "POST", path, body=body, epoch=1, key=key)
        return assert_wire(service, response, "ScenarioRevision", 201)

    first = publish(key)
    assert publish(key)["scenario_revision_id"] == first["scenario_revision_id"]
    monkeypatch.setattr(service, "receipt", lambda *args: None)
    monkeypatch.setattr(service, "save_receipt", lambda *args: None)
    alternative = uuid4()
    assert publish(alternative)["scenario_revision_id"] != publish(alternative)["scenario_revision_id"]
