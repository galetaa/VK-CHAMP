"""Catalog revisions and publication of consent-bearing offer snapshots."""

from uuid import uuid4

from app.common import digest, iso
from app.v2.core import LIMITS, SEED, Failure, check_revision, emit, issue

TERM_FIELDS = ("sku", "price_minor", "active", "ready_in_days", "units_per_pack", "terms_revision")
SKU_FIELDS = (
    "sku",
    "title",
    "price_minor",
    "stock_on_hand",
    "stock_reserved",
    "active",
    "ready_in_days",
    "units_per_pack",
    "revision",
    "terms_revision",
)


def seed(ctx):
    for sku in SEED["catalog"]:
        ctx.insert("catalog_skus", **{key: sku[key] for key in SKU_FIELDS}, updated_at=ctx.now)
    if ctx.actor["bundle_enabled"]:
        identity = str(uuid4())
        template = SEED["bundle_template"] | {"id": identity}
        ctx.insert(
            "bundle_templates",
            id=identity,
            version_no=template["version_no"],
            code=template["code"],
            snapshot=template,
            created_at=ctx.now,
        )


def available(sku):
    return sku["stock_on_hand"] - sku["stock_reserved"]


def sku_view(row):
    return {key: row[key] for key in SKU_FIELDS} | {"stock_available": available(row)}


def catalog(ctx, skus):
    if not skus:
        return {}
    rows = ctx.db.all(
        """SELECT * FROM catalog_skus WHERE sandbox_id=%s AND generation=%s AND sku=ANY(%s)
           ORDER BY sku COLLATE "C" """,
        *ctx.scope.values(),
        sorted(set(skus)),
    )
    return {row["sku"]: row for row in rows}


def count(ctx, table):
    return ctx.db.first(
        f"SELECT count(*) AS n FROM {table} WHERE sandbox_id=%s AND generation=%s", *ctx.scope.values()
    )["n"]


def create_sku(ctx, body):
    if ctx.db.find("catalog_skus", **ctx.scope, sku=body["sku"]):
        raise Failure("VALIDATION_ERROR", fields=[issue("/sku", "DUPLICATE")])
    if count(ctx, "catalog_skus") >= LIMITS["max_catalog_skus"]:
        raise Failure("VALIDATION_ERROR", fields=[issue("/sku", "RANGE")])
    row = body | {"stock_reserved": 0, "revision": 1, "terms_revision": 1}
    ctx.insert("catalog_skus", **row, updated_at=ctx.now)
    emit(ctx, "catalog_sku_changed", entity_revision=1)
    return sku_view(row)


def returnable(ctx, sku):
    return ctx.db.first(
        """SELECT COALESCE(sum((item->>'quantity')::integer),0) AS n FROM orders o
           CROSS JOIN LATERAL jsonb_array_elements(o.snapshot->'lines') item
           WHERE sandbox_id=%s AND generation=%s AND status IN ('CREATED','ACCEPTED') AND item->>'sku'=%s""",
        *ctx.scope.values(),
        sku,
    )["n"]


def update_sku(ctx, identity, body):
    row = ctx.get("catalog_skus", sku=identity)
    check_revision(row["revision"], body["expected_revision"])
    changes = {key: value for key, value in body.items() if key != "expected_revision"}
    stock = changes["stock_on_hand"]
    if stock < row["stock_reserved"]:
        raise Failure("STOCK_BELOW_RESERVED", fields=[issue("/stock_on_hand", "RANGE")])
    if stock + returnable(ctx, identity) > 99999:
        raise Failure("STOCK_CAPACITY_CONFLICT", fields=[issue("/stock_on_hand", "RANGE")])
    changed = any(row[key] != changes[key] for key in ("price_minor", "active", "ready_in_days"))
    changes |= {
        "revision": row["revision"] + 1,
        "terms_revision": row["terms_revision"] + int(changed),
        "updated_at": ctx.now,
    }
    ctx.update("catalog_skus", changes, sku=identity)
    emit(ctx, "catalog_sku_changed", entity_revision=changes["revision"])
    return sku_view(row | changes)


def terms_hash(ctx, rows):
    return digest(
        {
            "sandbox_id": str(ctx.actor["sandbox_id"]),
            "generation": ctx.actor["generation"],
            "terms": [{key: rows[sku][key] for key in TERM_FIELDS} for sku in sorted(rows)],
        }
    )


def priced(row, quantity):
    return {
        "sku": row["sku"],
        "title": row["title"],
        "quantity": quantity,
        "unit_price_minor": row["price_minor"],
        "line_total_minor": row["price_minor"] * quantity,
    }


def resolve_version(ctx, offer, version=None):
    number = version if version is not None else offer["published_version_no"]
    if number is None:
        return None
    return ctx.get("offer_versions", offer_id=offer["id"], version_no=number)["snapshot"]


def latest_terms(ctx, version):
    return catalog(ctx, [row["sku"] for row in version["catalog_terms"]])


def needs_review(ctx, version):
    return (
        version is not None and terms_hash(ctx, latest_terms(ctx, version)) != version["approved_terms_hash"]
    )


def offer_view(ctx, row):
    version = resolve_version(ctx, row)
    return {
        "id": str(row["id"]),
        "sandbox_id": str(row["sandbox_id"]),
        "mode": row["mode"],
        "state": row["state"],
        "revision": row["revision"],
        "draft_revision": row["draft_revision"],
        "draft": row["draft"],
        "published_version_no": row["published_version_no"],
        "published": version,
        "needs_review": needs_review(ctx, version),
        "share_url": "/buy/" + str(row["id"]) if version else None,
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


def enabled(ctx):
    if not ctx.actor["bundle_enabled"]:
        raise Failure("MODULE_DISABLED")


def new_offer(ctx, mode):
    if mode == "BUNDLE":
        enabled(ctx)
    if count(ctx, "offers") >= LIMITS["max_offers_per_generation"]:
        raise Failure("VALIDATION_ERROR", fields=[issue("/mode", "RANGE")])
    row = {
        "id": uuid4(),
        "mode": mode,
        "state": "DRAFT",
        "revision": 1,
        "draft_revision": 1,
        "draft": {
            "title": "",
            "description": "",
            "source": "BUNDLE" if mode == "BUNDLE" else "MANUAL",
            "source_text": "",
            "lines": [],
            "bundle_template_id": None,
            "fulfillment_methods": [],
        },
        "created_by": ctx.identity,
        "created_at": ctx.now,
        "updated_at": ctx.now,
        "published_version_no": None,
    }
    ctx.insert("offers", **row)
    emit(ctx, "offer_draft_created", offer_id=str(row["id"]), to_state="DRAFT", entity_revision=1)
    return offer_view(ctx, ctx.scope | row)


def validate_draft(mode, draft):
    errors = []
    if mode == "FIXED" and (draft["source"] == "BUNDLE" or draft["bundle_template_id"] is not None):
        errors.append(issue("/draft/source", "MODE_MISMATCH"))
    if mode == "BUNDLE":
        if draft["source"] != "BUNDLE":
            errors.append(issue("/draft/source", "MODE_MISMATCH"))
        if draft["lines"]:
            errors.append(issue("/draft/lines", "MODE_MISMATCH"))
    items = [line["sku"] for line in draft["lines"]]
    if len(items) != len(set(items)):
        errors.append(issue("/draft/lines", "DUPLICATE"))
    if errors:
        raise Failure("VALIDATION_ERROR", fields=errors)


def offer_for_edit(ctx, identity, revision, states):
    row = ctx.get("offers", id=identity)
    check_revision(row["revision"], revision)
    if row["state"] not in states:
        raise Failure("OFFER_UNAVAILABLE", current_state=row["state"])
    return row


def update_draft(ctx, identity, body):
    row = offer_for_edit(ctx, identity, body["expected_revision"], ("DRAFT", "ACTIVE", "PAUSED"))
    draft = body["draft"]
    validate_draft(row["mode"], draft)
    changes = {
        "draft": draft,
        "revision": row["revision"] + 1,
        "draft_revision": row["draft_revision"] + 1,
        "updated_at": ctx.now,
    }
    ctx.update("offers", changes, id=identity)
    return offer_view(ctx, row | changes)


def template_view(ctx, identity):
    enabled(ctx)
    return ctx.get("bundle_templates", id=identity)["snapshot"]


def preview(ctx, row):
    draft = row["draft"]
    errors, lines, rows, template, problem = [], [], {}, None, None
    if not draft["title"].strip():
        errors.append(issue("/draft/title", "INCOMPLETE"))
    if not draft["fulfillment_methods"]:
        errors.append(issue("/draft/fulfillment_methods", "INCOMPLETE"))
    if row["mode"] == "FIXED":
        if not draft["lines"]:
            errors.append(issue("/draft/lines", "INCOMPLETE"))
        rows = catalog(ctx, [line["sku"] for line in draft["lines"]])
        for index, line in enumerate(draft["lines"]):
            sku = rows.get(line["sku"])
            path = f"/draft/lines/{index}/sku"
            if not sku or not sku["active"]:
                errors.append(issue(path, "UNKNOWN_SKU" if sku is None else "INCOMPLETE"))
                problem = problem or ("SKU_UNAVAILABLE", {})
            elif available(sku) < line["quantity"]:
                errors.append(issue(path, "RANGE"))
                problem = problem or ("INSUFFICIENT_STOCK", {"available_quantity": available(sku)})
            if sku:
                lines.append(priced(sku, line["quantity"]))
    else:
        identity = draft["bundle_template_id"]
        physical = ctx.db.find("bundle_templates", **ctx.scope, id=identity) if identity else None
        if not physical:
            errors.append(issue("/draft/bundle_template_id", "INCOMPLETE"))
        else:
            template = physical["snapshot"]
            rows = catalog(ctx, [alt["sku"] for slot in template["slots"] for alt in slot["alternatives"]])
            for i, slot in enumerate(template["slots"]):
                alternatives = slot["alternatives"]
                if any(
                    alt["sku"] not in rows or rows[alt["sku"]]["units_per_pack"] != alt["units_per_pack"]
                    for alt in alternatives
                ):
                    errors.append(issue(f"/bundle_template/slots/{i}", "UNKNOWN_SKU"))
                    problem = problem or ("SKU_UNAVAILABLE", {})
                elif not any(
                    rows[alt["sku"]]["active"] and available(rows[alt["sku"]]) >= 1 for alt in alternatives
                ):
                    errors.append(issue(f"/bundle_template/slots/{i}", "RANGE"))
                    problem = problem or ("INSUFFICIENT_STOCK", {"available_quantity": 0})
    subtotal = sum(line["line_total_minor"] for line in lines)
    if subtotal > LIMITS["max_quote_total_minor"]:
        errors.append(issue("/subtotal_minor", "MONEY_LIMIT"))
    if sum(line["quantity"] for line in draft["lines"]) > LIMITS["max_total_units"]:
        errors.append(issue("/draft/lines", "TOTAL_UNITS_LIMIT"))
    lines = sorted(lines, key=lambda line: line["sku"])
    return (
        {
            "revision": row["revision"],
            "valid": not errors,
            "issues": sorted(errors, key=lambda i: (i["path"], i["code"])),
            "priced_lines": [] if subtotal > LIMITS["max_quote_total_minor"] else lines,
            "subtotal_minor": None
            if row["mode"] == "BUNDLE" or subtotal > LIMITS["max_quote_total_minor"]
            else subtotal,
            "currency": "RUB",
            "bundle_template": template,
        },
        rows,
        problem,
    )


def publish(ctx, identity, expected):
    row = offer_for_edit(ctx, identity, expected, ("DRAFT", "ACTIVE", "PAUSED"))
    result, rows, problem = preview(ctx, row)
    if not result["valid"]:
        structural = {"INCOMPLETE", "TOTAL_UNITS_LIMIT", "MONEY_LIMIT"}
        fields = result["issues"]
        if any(
            i["path"]
            in ("/draft/title", "/draft/fulfillment_methods", "/draft/bundle_template_id", "/draft/lines")
            or i["code"] in structural - {"INCOMPLETE"}
            for i in fields
        ):
            raise Failure("DRAFT_INCOMPLETE", fields=fields)
        if problem:
            code, details = problem
            raise Failure(code, fields=fields, **details)
        raise Failure("DRAFT_INCOMPLETE", fields=fields)
    number = (row["published_version_no"] or 0) + 1
    version = {
        key: row["draft"][key]
        for key in ("title", "description", "source", "source_text", "fulfillment_methods")
    }
    version |= {
        "version_no": number,
        "lines": result["priced_lines"],
        "bundle_template": result["bundle_template"],
        "catalog_terms": [{key: sku[key] for key in (*TERM_FIELDS, "title")} for sku in rows.values()],
        "approved_terms_hash": terms_hash(ctx, rows),
        "published_by": str(ctx.identity),
        "published_at": iso(ctx.now),
    }
    ctx.insert(
        "offer_versions",
        offer_id=identity,
        version_no=number,
        snapshot=version,
        approved_terms_hash=version["approved_terms_hash"],
        published_by=ctx.identity,
        published_at=ctx.now,
    )
    changes = {
        "published_version_no": number,
        "revision": row["revision"] + 1,
        "state": "ACTIVE",
        "updated_at": ctx.now,
    }
    ctx.update("offers", changes, id=identity)
    emit(
        ctx,
        "offer_published",
        offer_id=identity,
        offer_version_no=number,
        from_state=row["state"],
        to_state="ACTIVE",
        entity_revision=changes["revision"],
    )
    return offer_view(ctx, row | changes)


def public_offer(ctx, identity):
    offer = ctx.get("offers", id=identity)
    if offer["state"] != "ACTIVE":
        raise Failure("OFFER_UNAVAILABLE", current_state=offer["state"])
    version = resolve_version(ctx, offer)
    rows = latest_terms(ctx, version)
    return {
        "id": str(offer["id"]),
        "mode": offer["mode"],
        "version": version,
        "availability": [
            {"sku": code, "available_quantity": available(row) if row["active"] else 0}
            for code, row in rows.items()
        ],
        "needs_review": terms_hash(ctx, rows) != version["approved_terms_hash"],
        "is_test": True,
    }


def validate_active(
    ctx,
    identity,
    expected,
    *,
    mismatch="OFFER_VERSION_CHANGED",
    terms="OFFER_NEEDS_REVIEW",
    unavailable="OFFER_UNAVAILABLE",
):
    offer = ctx.get("offers", id=identity)
    if offer["state"] != "ACTIVE":
        raise Failure(unavailable, current_state=offer["state"])
    if offer["published_version_no"] != expected:
        raise Failure(mismatch, current_revision=offer["published_version_no"])
    version = resolve_version(ctx, offer)
    rows = latest_terms(ctx, version)
    if terms_hash(ctx, rows) != version["approved_terms_hash"]:
        raise Failure(terms)
    return offer, version, rows
