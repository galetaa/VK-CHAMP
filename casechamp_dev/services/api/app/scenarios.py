from uuid import uuid4

from app.common import SLOTS, Problem, digest, field_error, iso


def empty_slots():
    return [
        {
            "slot_id": slot,
            "primary_sku_id": None,
            "alternative_sku_ids": [],
            "mapping_status": "UNCONFIRMED",
            "mapping_source": "MANUAL",
            "source_job_id": None,
        }
        for slot in SLOTS
    ]


def create_scenario(scope, body, slots=None):
    asset = body.get("asset_id")
    if asset:
        scope.require("assets", asset_id=asset)
    row = body | {
        "scenario_id": uuid4(),
        "draft_version": 1,
        "slots": slots or empty_slots(),
        "created_at": scope.now,
        "updated_at": scope.now,
    }
    scope.insert("scenarios", **row)
    return scenario_view(scope, row)


def scenario_view(scope, row):
    latest = scope.db.first(
        """SELECT scenario_revision_id FROM vk_demo.scenario_revisions
           WHERE sandbox_id=%s AND epoch=%s AND scenario_id=%s AND status='PUBLISHED'
           ORDER BY revision_number DESC LIMIT 1""",
        scope.sandbox_id,
        scope.epoch,
        row["scenario_id"],
    )
    return {
        "scenario_id": str(row["scenario_id"]),
        "draft_version": row["draft_version"],
        "title": row["title"],
        "caption": row["caption"],
        "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
        "slots": row["slots"],
        "updated_at": iso(row["updated_at"]),
        "latest_published_revision_id": str(latest["scenario_revision_id"]) if latest else None,
    }


def revision_view(row):
    return row["immutable_snapshot"] | {
        "scenario_revision_id": str(row["scenario_revision_id"]),
        "scenario_id": str(row["scenario_id"]),
        "revision_number": row["revision_number"],
        "status": row["status"],
        "published_at": iso(row["published_at"]),
        "withdrawn_at": iso(row["withdrawn_at"]),
        "template_hash": row["template_hash"],
    }


def check_slots(scope, slots, scenario, *, publish=False):
    by_id = {slot["slot_id"]: slot for slot in slots}
    if len(slots) != 6 or set(by_id) != set(SLOTS):
        raise Problem("MAPPING_INVALID", fields=[field_error("slots", "MAPPING_INVALID")])
    ordered = []
    issues = []
    for i, sid in enumerate(SLOTS):
        slot = by_id[sid]
        primary = slot["primary_sku_id"]
        path = f"slots[{i}]"
        alternatives = slot["alternative_sku_ids"]
        if primary in alternatives or (
            not primary and (alternatives or slot["mapping_status"] == "CONFIRMED")
        ):
            issues.append(field_error(path, "MAPPING_INVALID"))
        for sku in filter(None, (primary, *alternatives)):
            row = scope.scoped("products", sku_id=sku)
            if not row or row["slot_id"] != sid or (publish and not row["active"]):
                issues.append(field_error(path, "MAPPING_INVALID"))
                break
        job_id = slot["source_job_id"]
        if slot["mapping_source"] == "ML_REVIEWED":
            job = scope.scoped("ml_jobs", job_id=job_id) if job_id else None
            prior = next(item for item in scenario["slots"] if item["slot_id"] == sid)
            already_reviewed = all(slot[field] == prior[field] for field in slot)
            if (
                not job
                or job["status"] != "SUCCEEDED"
                or job["scenario_id"] != scenario["scenario_id"]
                or (not already_reviewed and job["source_draft_version"] != scenario["draft_version"])
            ):
                issues.append(field_error(path + ".source_job_id", "MAPPING_INVALID"))
        elif job_id:
            issues.append(field_error(path + ".source_job_id", "MAPPING_INVALID"))
        ordered.append(slot | {"alternative_sku_ids": sorted(alternatives)})
    if issues:
        raise Problem("MAPPING_INVALID", fields=issues)
    if publish:
        missing = [
            field_error(f"slots[{i}].primary_sku_id", "MAPPING_UNCONFIRMED")
            for i, slot in enumerate(ordered)
            if not slot["primary_sku_id"] or slot["mapping_status"] != "CONFIRMED"
        ]
        if missing:
            raise Problem("MAPPING_UNCONFIRMED", fields=missing)
    return ordered


def patch_scenario(scope, scenario_id, body):
    previous = scope.require("scenarios", scenario_id=scenario_id)
    if previous["draft_version"] != body["expected_draft_version"]:
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    changes = {key: body[key] for key in ("title", "caption", "asset_id", "slots") if key in body}
    if changes.get("asset_id"):
        scope.require("assets", asset_id=changes["asset_id"])
    if "slots" in changes:
        changes["slots"] = check_slots(scope, changes["slots"], previous)
    old_asset = str(previous["asset_id"]) if previous["asset_id"] else None
    prior = previous | {"asset_id": old_asset}
    changes = {key: value for key, value in changes.items() if value != prior[key]}
    if changes:
        changes |= {"draft_version": previous["draft_version"] + 1, "updated_at": scope.now}
        scope.update("scenarios", changes, scenario_id=scenario_id)
    return scenario_view(scope, previous | changes)


def publish(scope, scenario_id, expected_version, event, *, simulation=False):
    scenario = scope.require("scenarios", scenario_id=scenario_id)
    if scenario["draft_version"] != expected_version:
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    slots = check_slots(scope, scenario["slots"], scenario, publish=True)
    previous = scope.db.first(
        """SELECT COALESCE(max(revision_number),0) AS revision FROM vk_demo.scenario_revisions
           WHERE sandbox_id=%s AND epoch=%s AND scenario_id=%s""",
        scope.sandbox_id,
        scope.epoch,
        scenario_id,
    )
    revision_id = uuid4()
    snapshot = {
        "title": scenario["title"],
        "caption": scenario["caption"],
        "asset_id": str(scenario["asset_id"]) if scenario["asset_id"] else None,
        "slots": slots,
    }
    number = previous["revision"] + 1
    row = {
        "scenario_revision_id": revision_id,
        "scenario_id": scenario_id,
        "revision_number": number,
        "status": "PUBLISHED",
        "immutable_snapshot": snapshot,
        "template_hash": digest(snapshot),
        "published_at": scope.now,
        "withdrawn_at": None,
    }
    scope.insert("scenario_revisions", **row, is_simulation=simulation)
    for slot in slots:
        skus = [
            (slot["primary_sku_id"], "PRIMARY"),
            *[(sku, "ALTERNATIVE") for sku in slot["alternative_sku_ids"]],
        ]
        for sku, role in skus:
            scope.insert(
                "scenario_revision_items",
                scenario_revision_id=revision_id,
                slot_id=slot["slot_id"],
                sku_id=sku,
                role=role,
            )
    event(scope, "template.published", "template", revision_id, 1, {"revision_number": number})
    return revision_view(row)
