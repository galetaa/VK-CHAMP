"""Measure the pure checkout path through HTTP on catalogs of 17, 100, and 500 SKUs."""

import argparse
import csv
import io
import json
import platform
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
from app.benchmark import percentile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def enhanced_catalog(size):
    with (FIXTURES / "catalog_import.csv").open(encoding="utf-8-sig") as stream:
        parsed = csv.DictReader(stream)
        fields, items = parsed.fieldnames, list(parsed)
    for i in range(size - len(items)):
        template = items[i % 17]
        if sum(1 for row in items if row["slot_id"] == template["slot_id"]) >= 499:
            raise ValueError("too many candidates")
        items.append(
            template
            | {
                "sku_id": f"PERF_{i:03d}",
                "price_minor": str(int(template["price_minor"]) + (i + 1) * 31),
            }
        )
    buffer = io.StringIO()
    to_csv = csv.DictWriter(buffer, fieldnames=fields)
    to_csv.writeheader()
    to_csv.writerows(items)
    return items, buffer.getvalue()


def measure(origin, sizes):
    records = []
    for size in sizes:
        items, source_file = enhanced_catalog(size)
        with httpx.Client(base_url=origin, timeout=10) as client:

            def request(method, route, body=None, code=200, **options):
                headers = {"Origin": origin, "X-Sandbox-Epoch": "1", "Idempotency-Key": str(uuid4())}
                if body is not None:
                    options["json"] = body
                response = client.request(method, "/api/v1" + route, headers=headers, **options)
                if code is not None:
                    assert response.status_code == code, response.text
                    return response.json()["data"]
                return response

            request("POST", "/demo/sandboxes", {"fixture_id": "EMPTY_V1"}, 201)
            imported = request(
                "POST",
                "/catalog/imports",
                code=201,
                files={
                    "file": ("catalog.csv", source_file.encode(), "text/csv"),
                },
            )
            catalog = request(
                "POST",
                f"/catalog/imports/{imported['import_id']}/commit",
                {"expected_catalog_version": imported["base_catalog_version"]},
            )
            assert len(catalog["items"]) == size
            scenario = request(
                "POST",
                "/scenarios",
                {"title": "Измерение производительности", "caption": "", "asset_id": None},
                201,
            )
            mappings = json.loads((FIXTURES / "snapshot.json").read_text())["template_slots"]
            for mapping in mappings:
                mapping["alternative_sku_ids"].extend(
                    row["sku_id"]
                    for row in items
                    if row["sku_id"].startswith("PERF_") and row["slot_id"] == mapping["slot_id"]
                )
            draft = request(
                "PATCH",
                f"/scenarios/{scenario['scenario_id']}",
                {"expected_draft_version": scenario["draft_version"], "slots": mappings},
            )
            revision = request(
                "POST",
                f"/scenarios/{scenario['scenario_id']}/publish",
                {"expected_draft_version": draft["draft_version"]},
                201,
            )
            base = json.loads((FIXTURES / "base_request.json").read_text()) | {
                "scenario_revision_id": revision["scenario_revision_id"],
                "needed_by": None,
            }

            def solve(phase, barrier=None):
                if barrier:
                    barrier.wait(timeout=5)
                before = time.perf_counter()
                response = request("POST", "/quotes", base | {"client_run_id": str(uuid4())}, code=None)
                data = response.json()
                result = data["data"]["outcome"] if response.status_code == 200 else data["error"]["code"]
                return {
                    "sku_count": size,
                    "phase": phase,
                    "http_status": response.status_code,
                    "outcome": result,
                    "wall_ms": round((time.perf_counter() - before) * 1000, 3),
                }

            records.append(solve("first_quote"))
            records.extend(solve("sequential") for _ in range(3))
            time.sleep(8)
            gate = Barrier(10)
            with ThreadPoolExecutor(max_workers=10) as pool:
                records.extend(pool.map(lambda _: solve("concurrent_10", gate), range(10)))
    summary = {}
    for size in sizes:
        summary[str(size)] = {}
        for phase in ("first_quote", "sequential", "concurrent_10"):
            runs = [row for row in records if row["sku_count"] == size and row["phase"] == phase]
            durations = [row["wall_ms"] for row in runs]
            success = [row["wall_ms"] for row in runs if row["outcome"] == "QUOTED"]
            summary[str(size)][phase] = {
                "attempts": len(runs),
                "p50_ms": percentile(durations, 0.50),
                "p95_ms": percentile(durations, 0.95),
                "max_ms": max(durations),
                "quoted_p95_ms": percentile(success, 0.95),
                "outcomes": dict(Counter(row["outcome"] for row in runs)),
            }
    return {
        "origin": origin,
        "platform": platform.platform(),
        "scope": "API wall time at the declared rate limits; first quote after creating a new sandbox",
        "summary": summary,
        "records": records,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://localhost:8080")
    parser.add_argument("--output", default="reports/performance.json")
    arguments = parser.parse_args()
    output = measure(arguments.origin, (17, 100, 500))
    Path(arguments.output).write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output["summary"], indent=2))
