"""Recorded P0 and P1 walkthrough against a running v2 HTTP endpoint."""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx


class Browser:
    def __init__(self, client, origin):
        self.client, self.origin = client, origin
        self.csrf = None

    def request(self, path, body=None, *, method=None, status=200, key=None):
        headers = {}
        if body is not None:
            headers = {
                "Origin": self.origin,
                "X-CSRF-Token": self.csrf,
                "Idempotency-Key": str(key or uuid4()),
            }
        response = self.client.request(
            method or ("GET" if body is None else "POST"), "/api/v2" + path, headers=headers, json=body
        )
        payload = response.json()
        if response.status_code != status:
            raise RuntimeError(
                f"{path} returned {response.status_code} {payload.get('error', {}).get('code')}"
            )
        return payload

    def data(self, *args, **kwargs):
        return self.request(*args, **kwargs)["data"]

    def bootstrap(self):
        self.csrf = self.data("/demo/bootstrap")["csrf_token"]

    def invite(self, client, role):
        issued = self.data("/demo/role-links", {"role": role}, status=201)
        invited = Browser(client, self.origin)
        invited.bootstrap()
        grant = invited.data("/demo/sessions", {"token": issued["url"].split("#token=")[1]})
        assert grant["role"] == role
        invited.csrf = grant["csrf_token"]
        return invited

    def offer(self, mode, **draft):
        data = self.data("/offers", {"mode": mode}, status=201)
        path = "/offers/" + data["id"]
        next_draft = (
            data["draft"]
            | {"title": "Демонстрационное предложение", "fulfillment_methods": ["PICKUP"]}
            | draft
        )
        revised = self.data(
            path + "/draft", {"expected_revision": data["revision"], "draft": next_draft}, method="PATCH"
        )
        revision = {"expected_revision": revised["revision"]}
        assert self.data(path + "/preview", revision)["valid"]
        return self.data(path + "/publish", revision)

    def checkout(self, quote):
        hold = self.data("/reservations", {"quote_id": quote["id"]}, status=201)
        key = uuid4()
        path = f"/reservations/{hold['id']}/confirm"
        confirmed = self.data(path, {"expected_revision": hold["revision"]}, status=201, key=key)
        replay = self.request(path, {"expected_revision": hold["revision"]}, status=201, key=key)
        assert replay["meta"]["replayed"] and replay["data"] == confirmed
        return hold, confirmed


def run(origin):
    with ExitStack() as stack:

        def open_context():
            return stack.enter_context(httpx.Client(base_url=origin, timeout=10))

        owner = Browser(open_context(), origin)
        assert owner.data("/health")["contract_version"] == "2.0.0"
        owner.bootstrap()
        session = owner.data("/demo/sandboxes", {"seed": "CORE-001", "enable_bundle": True}, status=201)
        owner.csrf = session["csrf_token"]
        operator = owner.invite(open_context(), "OPERATOR")
        buyer = owner.invite(open_context(), "BUYER")
        assert owner.data("/catalog")["items"]
        offer = owner.offer(
            "FIXED",
            source="POST",
            source_text="Кружка и две упаковки чая",
            lines=[{"sku": "MUG", "quantity": 1}, {"sku": "TEA", "quantity": 2}],
        )
        quote = buyer.data(
            "/quotes",
            {
                "kind": "OFFER",
                "offer_id": offer["id"],
                "offer_version_no": 1,
                "copies": 1,
                "fulfillment": "PICKUP",
                "requested_days": 1,
            },
            status=201,
        )
        hold, order = buyer.checkout(quote)
        assert order["total_minor"] == 219000 and order["is_test"] and order["paid_at"] is None
        order_path = f"/orders/{order['id']}/transition"
        accepted = operator.data(
            order_path,
            {"expected_revision": order["revision"], "to_status": "ACCEPTED", "cancel_reason": None},
        )
        fulfilled = operator.data(
            order_path,
            {"expected_revision": accepted["revision"], "to_status": "FULFILLED", "cancel_reason": None},
        )
        assert fulfilled["status"] == "FULFILLED"
        template = owner.data("/bundle-templates")["items"][0]
        bundle = owner.offer("BUNDLE", bundle_template_id=template["id"])
        request = {
            "offer_id": bundle["id"],
            "offer_version_no": 1,
            "guests": 4,
            "owned": ["TABLECLOTH"],
            "budget_minor": 500000,
            "fulfillment": "PICKUP",
            "requested_days": 1,
        }
        solution = buyer.data("/bundle-solutions", request, status=201)
        cheaper = buyer.data("/bundle-solutions", request | {"budget_minor": 400000}, status=201)
        rejected = buyer.data("/bundle-solutions", request | {"budget_minor": 327999}, status=201)
        assert (solution["total_minor"], cheaper["total_minor"], rejected["status"]) == (
            440000,
            380000,
            "NO_SOLUTION",
        )
        bundle_quote = buyer.data("/quotes", {"kind": "BUNDLE", "solution_id": solution["id"]}, status=201)
        bundle_hold, bundle_order = buyer.checkout(bundle_quote)
        assert bundle_order["total_minor"] == 440000
        summary = owner.data("/dashboard")
        assert summary["orders_created"] == 2 and summary["orders_fulfilled"] == 1
        assert summary["native_orders_count"] == "NOT_MEASURED"
        return {
            "status": "PASS",
            "contract_version": "2.0.0",
            "is_test": True,
            "P0": {
                "offer_id": offer["id"],
                "quote_id": quote["id"],
                "reservation_id": hold["id"],
                "order_id": order["id"],
                "total_minor": order["total_minor"],
                "final_status": fulfilled["status"],
            },
            "P1": {
                "offer_id": bundle["id"],
                "solution_id": solution["id"],
                "quote_id": bundle_quote["id"],
                "reservation_id": bundle_hold["id"],
                "order_id": bundle_order["id"],
                "total_minor": 440000,
                "alternative_minor": cheaper["total_minor"],
                "low_budget_result": rejected["status"],
            },
            "dashboard": summary,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://localhost:18081")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = json.dumps(run(arguments.origin), ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(result)
    print(result, end="")
