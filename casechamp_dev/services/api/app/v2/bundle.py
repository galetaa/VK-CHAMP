"""Bounded exact search for the immutable B001 template."""

from datetime import timedelta
from math import prod
from time import monotonic
from uuid import uuid4

from app.common import digest, iso
from app.v2 import checkout, offers
from app.v2.core import LIMITS, TTL, Failure


def snapshot(ctx, request):
    offers.enabled(ctx)
    offer, version, catalog = offers.validate_active(ctx, request["offer_id"], request["offer_version_no"])
    if offer["mode"] != "BUNDLE":
        from app.v2.core import issue

        raise Failure("VALIDATION_ERROR", fields=[issue("/offer_id", "MODE_MISMATCH")])
    fee = checkout.fulfillment(version, request["fulfillment"])
    approved_titles = {row["sku"]: row["title"] for row in version["catalog_terms"]}
    return {
        "template": version["bundle_template"],
        "terms_hash": version["approved_terms_hash"],
        "fee": fee,
        "offer_id": str(offer["id"]),
        "version_no": version["version_no"],
        "catalog": {
            sku: {key: row[key] for key in offers.TERM_FIELDS}
            | {"stock_available": offers.available(row), "title": approved_titles[sku]}
            for sku, row in catalog.items()
        },
    }


def enumerate_bundle(template, catalog, request, fee, deadline):
    groups = []
    for slot in sorted(template["slots"], key=lambda item: item["slot_id"]):
        need = request["guests"] if slot["need_rule"] == "GUESTS" else 1
        if slot["owned_key"] in request["owned"]:
            need = 0
        if need == 0:
            continue
        choices = []
        for candidate in slot["alternatives"]:
            row = catalog[candidate["sku"]]
            pack = candidate["units_per_pack"]
            quantity = (need + pack - 1) // pack
            if not row["active"] or row["ready_in_days"] > request["requested_days"]:
                continue
            if quantity > row["stock_available"]:
                continue
            line = offers.priced(row, quantity)
            choices.append((candidate["preference_rank"], quantity * pack - need, line))
        if not choices:
            return None
        groups.append(choices)
    if prod(map(len, groups)) > LIMITS["bundle_combinations"]:
        raise Failure("COMPUTATION_UNAVAILABLE")
    return choose(groups, min(request["budget_minor"], LIMITS["max_quote_total_minor"]), fee, deadline)


def choose(groups, budget, fee, deadline):
    # Each component is a lower bound on any completion of the prefix. Its use
    # only prunes prefixes that cannot win the same exact lexicographic search.
    minima = [(0, 0, 0, ()) for _ in range(len(groups) + 1)]
    for i in range(len(groups) - 1, -1, -1):
        group = groups[i]
        rank, excess, price, sku = minima[i + 1]
        minima[i] = (
            rank + min(choice[0] for choice in group),
            excess + min(choice[1] for choice in group),
            price + min(choice[2]["line_total_minor"] for choice in group),
            (min(choice[2]["sku"] for choice in group), *sku),
        )
        group.sort(key=lambda choice: (choice[0], choice[1], choice[2]["line_total_minor"], choice[2]["sku"]))

    best = best_lines = None
    active = []

    def visit(index, rank, excess, total, sku):
        nonlocal best, best_lines
        if monotonic() >= deadline:
            raise Failure("COMPUTATION_UNAVAILABLE")
        min_rank, min_excess, min_price, min_sku = minima[index]
        if total + min_price > budget:
            return
        lower = (rank + min_rank, excess + min_excess, total + min_price, sku + min_sku)
        if best is not None and lower >= best:
            return
        if index == len(groups):
            best = rank, excess, total, sku
            best_lines = active.copy()
            return
        for preference, surplus, line in groups[index]:
            active.append(line)
            visit(
                index + 1,
                rank + preference,
                excess + surplus,
                total + line["line_total_minor"],
                (*sku, line["sku"]),
            )
            active.pop()

    visit(0, 0, 0, fee, ())
    if best is None:
        return None
    rank, excess, total, sku = best
    return {
        "lines": sorted(best_lines, key=lambda item: item["sku"]),
        "subtotal_minor": total - fee,
        "fulfillment_fee_minor": fee,
        "total_minor": total,
        "currency": "RUB",
        "objective": {"preference_penalty": rank, "surplus_units": excess, "sku_tiebreak": list(sku)},
    }


def save(ctx, request, snap, outcome):
    identity = str(uuid4())
    expires = ctx.now + timedelta(seconds=TTL["bundle_solution"])
    data = {
        "id": identity,
        "sandbox_id": str(ctx.actor["sandbox_id"]),
        "buyer_id": str(ctx.identity),
        "offer_id": snap["offer_id"],
        "offer_version_no": snap["version_no"],
        "template_id": snap["template"]["id"],
        "template_version_no": snap["template"]["version_no"],
        "request": request,
        "created_at": iso(ctx.now),
        "expires_at": iso(expires),
        "is_test": True,
    }
    solution_hash = None
    if outcome is None:
        data |= {"status": "NO_SOLUTION", "reason": "NO_FEASIBLE_COMBINATION"}
    else:
        data |= outcome | {"status": "SOLVED", "catalog_terms_hash": snap["terms_hash"]}
        keys = (
            "buyer_id",
            "offer_id",
            "offer_version_no",
            "template_id",
            "template_version_no",
            "request",
            "lines",
            "objective",
            "catalog_terms_hash",
            "total_minor",
        )
        solution_hash = digest({key: data[key] for key in keys})
        data["solution_hash"] = solution_hash
    ctx.insert(
        "bundle_solutions",
        id=identity,
        buyer_id=ctx.identity,
        offer_id=snap["offer_id"],
        offer_version_no=snap["version_no"],
        status=data["status"],
        snapshot=data,
        solution_hash=solution_hash,
        created_at=ctx.now,
        expires_at=expires,
    )
    return data
