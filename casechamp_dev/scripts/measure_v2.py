"""Measure v2 across 20 concurrent isolated buyer sessions in a dedicated Compose project."""

import argparse
import json
import math
import platform
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
from app.settings import ROOT


def seed_project(env_file, project):
    command = [
        "docker",
        "compose",
        "--env-file",
        env_file,
        "-f",
        "compose.yaml",
        "-f",
        "compose.v2.yaml",
        "-f",
        "compose.performance.yaml",
        "-p",
        project,
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "app.v2.performance_seed",
    ]
    # Authentication material exists only in memory of this local load-test client.
    created = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return json.loads(created.stdout)


def api(client, credential, origin, method, path, body=None):
    headers = {"Cookie": "vk_demo_session=" + credential["cookie"]}
    if method != "GET":
        headers |= {"Origin": origin, "X-CSRF-Token": credential["csrf"], "Idempotency-Key": str(uuid4())}
    began = time.monotonic()
    try:
        response = client.request(method, "/api/v2" + path, headers=headers, json=body)
        payload = response.json()
        return response.status_code, payload, round((time.monotonic() - began) * 1000, 3)
    except (httpx.HTTPError, ValueError):
        return 0, {}, round((time.monotonic() - began) * 1000, 3)


def load(origin, env_file, project, duration, rounds):
    seed = seed_project(env_file, project)
    with httpx.Client(base_url=origin, timeout=6) as warm:
        for _ in range(50):
            status, _, _ = api(warm, seed["owner"], origin, "GET", "/catalog")
            if status != 200:
                raise RuntimeError(f"warmup returned {status}")
    start = time.monotonic() + 0.2

    def load_user(credential):
        log = []
        with httpx.Client(base_url=origin, timeout=8) as client:

            def call(label, method, path, body=None, role=None):
                status, payload, elapsed = api(client, role or credential, origin, method, path, body)
                code = payload.get("error", {}).get("code")
                log.append((label, status, code, elapsed))
                return payload.get("data")

            for i in range(rounds):
                time.sleep(max(0.0, start + i * duration / rounds - time.monotonic()))
                call("catalog", "GET", "/catalog", role=seed["owner"])
                call("offer", "GET", f"/public/offers/{seed['fixed']}")
                quote = call(
                    "quote",
                    "POST",
                    "/quotes",
                    {
                        "kind": "OFFER",
                        "offer_id": seed["fixed"],
                        "offer_version_no": 1,
                        "copies": 1,
                        "fulfillment": "PICKUP",
                        "requested_days": 1,
                    },
                )
                if quote:
                    reservation = call("reserve", "POST", "/reservations", {"quote_id": quote["id"]})
                    if reservation:
                        order = call(
                            "confirm",
                            "POST",
                            f"/reservations/{reservation['id']}/confirm",
                            {"expected_revision": 1},
                        )
                        if order:
                            call("order", "GET", f"/orders/{order['id']}")
                call(
                    "bundle",
                    "POST",
                    "/bundle-solutions",
                    {
                        "offer_id": seed["bundled"],
                        "offer_version_no": 1,
                        "guests": 4,
                        "budget_minor": 500000,
                        "owned": [],
                        "fulfillment": "PICKUP",
                        "requested_days": 1,
                    },
                )
        return log

    with ThreadPoolExecutor(max_workers=20) as executor:
        series = list(executor.map(load_user, seed["buyers"]))
    time.sleep(max(0.0, start + duration - time.monotonic()))
    buckets = defaultdict(list)
    for log in series:
        for name, code, error, milliseconds in log:
            buckets[name].append((code, error, milliseconds))

    def summarize(rows):
        timings = sorted(x[2] for x in rows)
        statuses = dict(Counter(x[0] for x in rows))
        errors = dict(Counter(x[1] for x in rows if x[1]))
        unexpected = sum(s == 0 or (s >= 500 and code != "COMPUTATION_UNAVAILABLE") for s, code, _ in rows)
        return {
            "requests": len(rows),
            "statuses": statuses,
            "errors": errors,
            "unexpected_5xx_or_transport": unexpected,
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": timings[math.ceil(len(timings) * 0.95) - 1],
            "max_ms": timings[-1],
        }

    return {
        "warmup": 50,
        "duration_seconds": round(time.monotonic() - start, 3),
        "concurrent_users": 20,
        "seed": seed["counts"],
        "bundle_max_shape": seed["bundle"],
        "platform": platform.platform(),
        "summary": {name: summarize(rows) for name, rows in sorted(buckets.items())},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--origin", default="http://localhost:18081")
    parser.add_argument("--project", default="casechamp-d2-v2")
    parser.add_argument("--duration", type=int, default=330)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/v2-performance.json")
    args = parser.parse_args()
    report = load(args.origin, args.env_file, args.project, args.duration, args.rounds)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
