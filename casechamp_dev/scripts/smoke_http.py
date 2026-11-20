"""Exercise the deployed manual path over HTTP from an empty sandbox."""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(origin):
    milestones = []
    with httpx.Client(base_url=origin + "/api/v1/", timeout=20) as client:
        epoch = None

        def request(method, path, body=None, status=200, **kwargs):
            headers = {"Origin": origin, "Idempotency-Key": str(uuid4())}
            if epoch is not None:
                headers["X-Sandbox-Epoch"] = str(epoch)
            if body is not None:
                kwargs["json"] = body
            response = client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == status, f"{method} {path}: {response.status_code} {response.text}"
            return response.json()["data"]

        health = request("GET", "health/ready")
        assert health["status"] == "ok"
        context = request("POST", "demo/sandboxes", {"fixture_id": "EMPTY_V1"}, status=201)
        epoch = context["sandbox_epoch"]
        assert context["latest_scenario_revision_id"] is None
        imported = request(
            "POST",
            "catalog/imports",
            status=201,
            files={"file": ("catalog.csv", (ROOT / "fixtures/catalog_import.csv").read_bytes(), "text/csv")},
        )
        assert imported["status"] == "VALID"
        catalog = request(
            "POST",
            f"catalog/imports/{imported['import_id']}/commit",
            {"expected_catalog_version": imported["base_catalog_version"]},
        )
        assert len(catalog["items"]) == 17
        milestones.append("EMPTY_V1 → CSV preview → commit")
        media = request(
            "POST",
            "assets",
            status=201,
            data={"kind": "IMAGE"},
            files={
                "file": ("scene.png", (ROOT / "fixtures/replay_assets/scene.png").read_bytes(), "image/png"),
            },
        )
        response = client.get(origin + media["read_url"])
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        scenario = request(
            "POST",
            "scenarios",
            {
                "title": "Сервировка стола",
                "caption": "Проверочный сценарий TCP.",
                "asset_id": media["asset_id"],
            },
            status=201,
        )
        mappings = json.loads((ROOT / "fixtures/snapshot.json").read_text())["template_slots"]
        draft = request(
            "PATCH",
            f"scenarios/{scenario['scenario_id']}",
            {
                "expected_draft_version": scenario["draft_version"],
                "slots": mappings,
            },
        )
        revision = request(
            "POST",
            f"scenarios/{scenario['scenario_id']}/publish",
            {"expected_draft_version": draft["draft_version"]},
            status=201,
        )
        milestones.append("Media → six manual mappings → publication")
        intent = json.loads((ROOT / "fixtures/base_request.json").read_text()) | {
            "scenario_revision_id": revision["scenario_revision_id"],
            "needed_by": None,
            "client_run_id": str(uuid4()),
        }
        quoted = request("POST", "quotes", intent)
        quote = quoted["quote"]
        verification = request("GET", f"quotes/{quote['quote_id']}/verification")
        assert quote["total_minor"] == 440_000 and verification["passed"]
        reserved = request(
            "POST",
            "reservations",
            {"quote_id": quote["quote_id"], "accepted_quote_hash": quote["quote_hash"]},
            status=201,
        )
        assert reserved["status"] == "ACTIVE"
        order = request(
            "POST",
            "orders",
            {"reservation_id": reserved["reservation_id"], "accepted_quote_hash": reserved["quote_hash"]},
            status=201,
        )
        assert order["status"] == "TEST_CONFIRMED" and order["total_minor"] == quote["total_minor"]
        result = request("POST", f"orders/{order['order_id']}/cancel", {"reason": "BUYER_CANCELED"})
        assert result["status"] == "CANCELED"
        restored = request("GET", "catalog")
        assert [row["stock_on_hand_packs"] for row in restored["items"]] == [
            row["stock_on_hand_packs"] for row in catalog["items"]
        ]
        milestones.append("Quote 4400 RUB → verification → reserve → confirm → cancel → stock restored")
        for _ in range(10):
            listing = request("GET", "events")
            if "order.test_canceled" in {event["event_name"] for event in listing["items"]}:
                break
            time.sleep(0.3)
        else:
            raise AssertionError("Worker did not project order cancellation")
        milestones.append("Committed cancellation → outbox worker → events projection")
        for name in ("LAST_STOCK_RACE", "PRICE_CHANGE", "EXPIRED_RESERVATION"):
            simulation = request(
                "POST",
                "demo/simulations",
                {"case_id": name, "scenario_revision_id": revision["scenario_revision_id"]},
            )
            assert simulation["passed"], simulation
            milestones.append(name)
        if context["capabilities"]["ml_mode"] == "REPLAY":
            job = request(
                "POST",
                "ml/intent-jobs",
                {
                    "scenario_revision_id": revision["scenario_revision_id"],
                    "text": "За столом будет 6 гостей.",
                },
                status=202,
            )
            wait_until = time.monotonic() + 32
            while job["status"] in ("QUEUED", "RUNNING") and time.monotonic() < wait_until:
                time.sleep(0.75)
                job = request("GET", f"jobs/{job['job_id']}")
            assert job["status"] == "SUCCEEDED" and job["result"]["intent_draft"] == {"guest_count": 6}, job
            assert job["result"]["provider_mode"] == "REPLAY"
            milestones.append("Recorded intent → asynchronous worker → REPLAY provenance")
        return {"status": "PASS", "quote_total_minor": quote["total_minor"], "milestones": milestones}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://localhost:8080")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = json.dumps(run(args.origin), ensure_ascii=False, indent=2) + "\n"
    print(result, end="")
    if args.output:
        Path(args.output).write_text(result)


if __name__ == "__main__":
    main()
