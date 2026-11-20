"""Reproducible exact-core evaluation on the frozen synthetic input."""

import argparse
import hashlib
import json
import math
import platform
from collections import defaultdict
from datetime import date, timedelta
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import ortools

from app.common import SLOTS, canonical
from app.planner import normalized_intent, pick_options, requirements, run_planner, solve
from app.settings import ROOT
from app.verifier import verify_solution


def group_inputs(case):
    snapshot = case["snapshot"]
    today = date.fromisoformat(snapshot["business_date"])
    request = normalized_intent(case["request"], today)
    transit = int(request["fulfillment"]["mode"] == "LOCAL_DELIVERY")
    required, owned = requirements(request)
    options, reasons = pick_options(snapshot, request, required, owned, today, transit)
    return snapshot, request, today, transit, options, reasons


def complete(lines, today, transit):
    if not lines:
        return {"outcome": "NO_PURCHASE_REQUIRED", "lines": []}
    subtotal = sum(item["line_total_minor"] for item in lines)
    total = subtotal + transit * 30000
    earliest = today + timedelta(days=transit + max(item["handling_days"] for item in lines))
    selected = {line["slot_id"]: line["sku_id"] for line in lines}
    return {
        "outcome": "QUOTED",
        "lines": lines,
        "subtotal_minor": subtotal,
        "shipping_minor": transit * 30000,
        "total_minor": total,
        "earliest_date": earliest.isoformat(),
        "objective": {
            "substituted_slots": sum(line["substitution"] for line in lines),
            "soft_color_mismatches": sum(line["soft_color_mismatch"] for line in lines),
            "excess_units": sum(line["excess_units"] for line in lines),
            "total_minor": total,
            "sku_tuple": [selected.get(slot, "") for slot in SLOTS],
        },
    }


def priority(line):
    return (
        line["substitution"],
        line["soft_color_mismatch"],
        line["excess_units"],
        line["line_total_minor"],
        line["sku_id"],
    )


def baseline(request, today, transit, options):
    if any(not rows for rows in options.values()):
        return {"outcome": "INFEASIBLE"}
    lines = [min(choices, key=priority) for choices in options.values()]
    result = complete(lines, today, transit)
    if result.get("total_minor", 0) > request["budget_minor"]:
        return {"outcome": "REFUSAL"}
    return result


def request_json(url, path, *, body=None, timeout=5):
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("The benchmark provider must bind to a local HTTP address.")
    req = Request(
        url.rstrip("/") + path,
        data=canonical(body) if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def llm_baseline(config, url, request, today, transit, options):
    if url is None:
        return {"outcome": "PROVIDER_UNAVAILABLE"}, {}
    model = config["provider_model"]
    tool_catalog = {
        slot: [
            {
                name: item[name]
                for name in (
                    "sku_id",
                    "line_total_minor",
                    "substitution",
                    "soft_color_mismatch",
                    "excess_units",
                    "pack_count",
                )
            }
            for item in choices
        ]
        for slot, choices in options.items()
    }
    prompt = (
        "Select one SKU per slot from the tool output. All candidates passed hard per-slot checks. "
        "Cost plus shipping must be at most budget. Minimize lexicographically substitutions, "
        "soft color misses, excess units, cost, sorted slot-order SKU. An empty options object means no "
        "purchase. If impossible say INFEASIBLE. Return JSON with status (QUOTED, INFEASIBLE, "
        "NO_PURCHASE_REQUIRED) and selected (array of exact SKU strings in slot order, omit unused slots).\n"
        + json.dumps(
            {
                "budget_minor": request["budget_minor"],
                "shipping_minor": transit * 30000,
                "candidate_tool_output": tool_catalog,
            },
            separators=(",", ":"),
        )
    )
    started = perf_counter()
    try:
        answer = request_json(
            url,
            "/api/generate",
            body={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "seed": config["seed"],
                    "num_ctx": 4096,
                    "num_gpu": 0,
                    "num_predict": config["provider_num_predict"],
                },
            },
            timeout=config["per_method_wall_seconds"],
        )
    except (HTTPError, URLError, TimeoutError):
        outcome = (
            "COMPUTATION_LIMIT"
            if perf_counter() - started >= config["per_method_wall_seconds"] - 0.05
            else "PROVIDER_UNAVAILABLE"
        )
        return {"outcome": outcome}, {}
    use = {name: answer.get(name) for name in ("prompt_eval_count", "eval_count", "total_duration")}
    try:
        decoded = json.loads(answer["response"])
        status, selected = decoded["status"], decoded["selected"]
        if status == "INFEASIBLE" and not selected:
            return {"outcome": "INFEASIBLE"}, use
        if status == "NO_PURCHASE_REQUIRED" and not selected:
            return {"outcome": "NO_PURCHASE_REQUIRED"}, use
        if status != "QUOTED" or not isinstance(selected, list) or len(selected) != len(options):
            raise ValueError
        lines = [
            next(item for item in choices if item["sku_id"] == sku)
            for sku, choices in zip(selected, options.values(), strict=True)
        ]
        return complete(lines, today, transit), use
    except (ValueError, KeyError, TypeError, StopIteration):
        return {"outcome": "INVALID_OUTPUT"}, use


def evaluate(case, config, url):
    snapshot, request, today, transit, options, _ = group_inputs(case)
    records = []
    for method in ("H", "B0", "B1"):
        started = perf_counter()
        usage = {}
        if method == "H":
            plan = run_planner(snapshot, request, timeout=config["per_method_wall_seconds"])
            result = plan.result if plan.error is None else {"outcome": plan.error}
        elif method == "B0":
            result = baseline(request, today, transit, options)
        else:
            result, usage = llm_baseline(config, url, request, today, transit, options)
        expected = case["expected"]
        outcome = result["outcome"]
        errors = verify_solution(snapshot, request, result) if outcome == "QUOTED" else []
        if outcome == "NO_PURCHASE_REQUIRED" and options:
            errors.append("PURCHASE_NEEDED")
        valid_bundle = outcome == "QUOTED" and not errors
        optimum = valid_bundle and result["objective"] == expected.get("objective")
        correct_class = outcome == expected["outcome"] and not errors
        elapsed = round((perf_counter() - started) * 1000, 3)
        records.append(
            {
                "id": case["id"],
                "family": case["family"],
                "method": method,
                "outcome": outcome,
                "gold": expected["outcome"],
                "valid_bundle": valid_bundle,
                "exact_optimum": optimum,
                "correct_classification": correct_class,
                "verification_errors": errors,
                "wall_ms": elapsed,
                "usage": usage,
                "billed_cost": 0 if method != "B1" or url else None,
            }
        )
    return records


def percentile(data, fraction):
    return sorted(data)[math.ceil(len(data) * fraction) - 1] if data else None


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["method"]].append(row)

    def summary(group):
        feasible = [row for row in group if row["gold"] == "QUOTED"]
        invalid = [
            row
            for row in group
            if row["outcome"] == "INVALID_OUTPUT" or (row["outcome"] == "QUOTED" and not row["valid_bundle"])
        ]
        p95 = percentile([row["wall_ms"] for row in group], 0.95)
        return {
            "attempts": len(group),
            "oracle_feasible": len(feasible),
            "valid_on_feasible": sum(row["valid_bundle"] for row in feasible),
            "exact_optimum": sum(row["exact_optimum"] for row in feasible),
            "false_refusals": sum(not row["valid_bundle"] for row in feasible),
            "invalid_outputs": len(invalid),
            "correct_classification": sum(row["correct_classification"] for row in group),
            "limits": sum(row["outcome"] in ("COMPUTATION_LIMIT", "COMPUTATION_ABORTED") for row in group),
            "unavailable": sum(row["outcome"] == "PROVIDER_UNAVAILABLE" for row in group),
            "p50_ms": percentile([row["wall_ms"] for row in group], 0.50),
            "p95_ms": p95,
            "max_ms": max(row["wall_ms"] for row in group),
            "tokens_prompt": sum(row["usage"].get("prompt_eval_count") or 0 for row in group),
            "tokens_output": sum(row["usage"].get("eval_count") or 0 for row in group),
            "billed_cost": 0 if all(row["billed_cost"] == 0 for row in group) else None,
        }

    return {
        method: summary(group)
        | {
            "families": {
                family: summary([row for row in group if row["family"] == family])
                for family in sorted({row["family"] for row in group})
            }
        }
        for method, group in groups.items()
    }


def ablations(cases):
    rows = []
    for case in cases:
        snapshot, request, _, _, _, _ = group_inputs(case)
        with_tiebreak = solve(snapshot, request)
        without_tiebreak = solve(snapshot, request, prove_sku_ties=False)
        chosen = with_tiebreak.result
        if chosen.get("outcome") == "QUOTED":
            mutated_lines = [chosen["lines"][0] | {"line_total_minor": 1}, *chosen["lines"][1:]]
            mutated = chosen | {"lines": mutated_lines}
            detected = bool(verify_solution(snapshot, request, mutated))
        else:
            detected = None
        rows.append(
            {
                "id": case["id"],
                "canonical_without_tiebreak": without_tiebreak.result == chosen,
                "mutated_quote_rejected_with_verify": detected,
            }
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--output", default=str(ROOT / "reports/benchmark.json"))
    args = parser.parse_args()
    config_path = ROOT / "fixtures/benchmark_manifest.json"
    raw = config_path.read_bytes()
    config = json.loads(raw)
    model_metadata = None
    if args.ollama_url:
        tags = request_json(args.ollama_url, "/api/tags")
        model_metadata = next(
            (item for item in tags["models"] if item["name"] == config["provider_model"]), None
        )
        if model_metadata is None:
            parser.error("The frozen baseline model is missing from the local Ollama server.")
    rows = []
    for case in config["cases"]:
        rows.extend(evaluate(case, config, args.ollama_url))
    output = {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_version": config["version"],
        "seed": config["seed"],
        "python": platform.python_version(),
        "ortools": ortools.__version__,
        "platform": platform.platform(),
        "llm": model_metadata,
        "scope": "synthetic exact-core benchmark",
        "summary": summarize(rows),
        "results": rows,
        "ablations": ablations(config["cases"]),
    }
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                k: {key: value for key, value in v.items() if key != "families"}
                for k, v in output["summary"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
