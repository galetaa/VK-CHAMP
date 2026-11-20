import csv
import hashlib
import io
import re
from datetime import timedelta
from uuid import uuid4

from app.common import SLOTS, Problem, digest, field_error, iso

CSV_FIELDS = (
    "sku_id",
    "slot_id",
    "title",
    "color",
    "pack_units",
    "price_minor",
    "stock_on_hand_packs",
    "active",
    "handling_days",
    "length_cm",
    "width_cm",
)
SKU = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,63}\Z")
COLORS = frozenset(
    (
        "WHITE",
        "BEIGE",
        "BLACK",
        "BLUE",
        "GREEN",
        "RED",
        "TRANSPARENT",
        "SILVER",
        "GOLD",
        "MULTICOLOR",
        "UNKNOWN",
    )
)
NUMBERS = {
    "pack_units": (1, 24),
    "price_minor": (1, 10_000_000),
    "stock_on_hand_packs": (0, 10_000),
    "handling_days": (0, 14),
    "length_cm": (1, 400),
    "width_cm": (1, 400),
}
PHYSICAL = ("slot_id", "color", "pack_units", "length_cm", "width_cm")
TERMS = (*PHYSICAL, "title", "price_minor", "handling_days", "image_asset_id", "active", "currency")
SKU_WIRE = ("sku_id", *TERMS, "stock_on_hand_packs", "reserved_packs", "product_version", "inventory_version")
MAX_BYTES = 2 * 1024 * 1024


def catalog_version(scope):
    return scope.require("sandbox_epochs")["catalog_version"]


def bump_catalog(scope):
    version = catalog_version(scope) + 1
    scope.update("sandbox_epochs", {"catalog_version": version})
    return version


def list_products(scope, *, as_of=None):
    if as_of is None:
        reserved = "i.reserved_packs AS reserved_packs"
        args = ()
    else:
        reserved = """(SELECT COALESCE(sum(rl.pack_count),0) FROM vk_demo.reservation_lines rl
            JOIN vk_demo.reservations r USING (sandbox_id,epoch,reservation_id)
            WHERE rl.sandbox_id=i.sandbox_id AND rl.epoch=i.epoch AND rl.sku_id=i.sku_id
            AND r.status='ACTIVE' AND r.expires_at>%s) AS reserved_packs"""
        args = (as_of,)
    rows = scope.db.all(
        f"""SELECT p.*, i.stock_on_hand_packs, i.inventory_version, i.returnable_packs, {reserved}
            FROM vk_demo.products p JOIN vk_demo.inventory i USING(sandbox_id,epoch,sku_id)
            WHERE p.sandbox_id=%s AND p.epoch=%s ORDER BY p.sku_id COLLATE "C" """,
        *args,
        scope.sandbox_id,
        scope.epoch,
    )
    return [wire_sku(row) for row in rows]


def wire_sku(row):
    obj = {name: row[name] for name in SKU_WIRE}
    if obj["image_asset_id"] is not None:
        obj["image_asset_id"] = str(obj["image_asset_id"])
    return obj | {"available_packs": row["stock_on_hand_packs"] - row["reserved_packs"]}


def get_catalog(scope):
    return {"catalog_version": catalog_version(scope), "items": list_products(scope)}


def product_revision(scope, product, version):
    payload = {name: product[name] for name in TERMS}
    scope.insert(
        "product_versions",
        sku_id=product["sku_id"],
        product_version=version,
        product_snapshot=payload,
        snapshot_hash=digest(payload),
        created_at=scope.now,
    )


def new_product(scope, row):
    on_hand = row["stock_on_hand_packs"]
    product = {name: row.get(name) for name in TERMS} | {"sku_id": row["sku_id"], "currency": "RUB"}
    scope.insert("products", **product, product_version=1, created_at=scope.now, updated_at=scope.now)
    product_revision(scope, product, 1)
    scope.insert(
        "inventory",
        sku_id=product["sku_id"],
        stock_on_hand_packs=on_hand,
        reserved_packs=0,
        returnable_packs=0,
        inventory_version=1,
        updated_at=scope.now,
    )


def lock_inventory(scope, sku_ids):
    if not sku_ids:
        return {}
    rows = scope.db.all(
        """SELECT * FROM vk_demo.inventory WHERE sandbox_id=%s AND epoch=%s AND sku_id=ANY(%s)
            ORDER BY sku_id COLLATE "C" FOR UPDATE""",
        scope.sandbox_id,
        scope.epoch,
        sorted(set(sku_ids)),
    )
    return {row["sku_id"]: row for row in rows}


def change_inventory(scope, row, *, on_hand=0, reserved=0, returnable=0):
    if not (on_hand or reserved or returnable):
        return
    values = {
        "stock_on_hand_packs": row["stock_on_hand_packs"] + on_hand,
        "reserved_packs": row["reserved_packs"] + reserved,
        "returnable_packs": row["returnable_packs"] + returnable,
    }
    scope.update(
        "inventory",
        values
        | {
            "inventory_version": row["inventory_version"] + 1,
            "updated_at": scope.now,
        },
        sku_id=row["sku_id"],
    )


def check_stock(row, new_on_hand):
    if new_on_hand < row["reserved_packs"]:
        raise Problem(
            "STOCK_BELOW_RESERVED", fields=[field_error("stock_on_hand_packs", "STOCK_BELOW_RESERVED")]
        )
    if new_on_hand + row["returnable_packs"] > 10000:
        raise Problem(
            "VALIDATION_ERROR", reasons=["STOCK_RETURN_CAPACITY"], fields=[field_error("stock_on_hand_packs")]
        )


def update_product(scope, product, changes):
    proposed = {
        key: value
        for key, value in changes.items()
        if (str(value) if key == "image_asset_id" and value is not None else value)
        != (str(product[key]) if key == "image_asset_id" and product[key] is not None else product[key])
    }
    if not proposed:
        return False
    version = product["product_version"] + 1
    scope.update(
        "products", proposed | {"product_version": version, "updated_at": scope.now}, sku_id=product["sku_id"]
    )
    product_revision(scope, product | proposed, version)
    return True


def patch_sku(scope, sku_id, patch, event):
    product = scope.require("products", sku_id=sku_id)
    inventory = lock_inventory(scope, [sku_id])[sku_id]
    if (
        patch["expected_product_version"] != product["product_version"]
        or "stock_on_hand_packs" in patch
        and patch["expected_inventory_version"] != inventory["inventory_version"]
    ):
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    proposed = {
        field: patch[field]
        for field in ("title", "price_minor", "handling_days", "image_asset_id")
        if field in patch
    }
    if proposed.get("image_asset_id"):
        asset = scope.require("assets", asset_id=proposed["image_asset_id"])
        if asset["kind"] == "VIDEO":
            raise Problem("VALIDATION_ERROR", fields=[field_error("image_asset_id")])
    on_hand = patch.get("stock_on_hand_packs", inventory["stock_on_hand_packs"])
    check_stock(inventory, on_hand)
    delta = on_hand - inventory["stock_on_hand_packs"]
    changed = update_product(scope, product, proposed)
    change_inventory(scope, inventory, on_hand=delta)
    if changed or delta:
        version = bump_catalog(scope)
        event(scope, "catalog.updated", "catalog", "catalog", version, {"sku_id": sku_id})
    return wire_sku(scope.require("products", sku_id=sku_id) | scope.require("inventory", sku_id=sku_id))


def parse_csv(content):
    errors, rows = [], []
    try:
        source = io.StringIO(content.decode("utf-8-sig"), newline="")
        reader = csv.reader(source, strict=True)
        if tuple(next(reader, ())) != CSV_FIELDS:
            return [], [field_error("header", "IMPORT_VALIDATION_FAILED", "Неверный заголовок CSV.")], 0
        seen = set()
        count = 0
        for index, cells in enumerate(reader, start=2):
            count += 1
            if count > 500:
                errors.append(field_error("rows", "CATALOG_LIMIT_EXCEEDED"))
                break
            path = f"rows[{index}]"
            if len(cells) != len(CSV_FIELDS):
                errors.append(field_error(path, "IMPORT_VALIDATION_FAILED", "Неверное число колонок."))
                continue
            row = dict(zip(CSV_FIELDS, cells, strict=True))
            row["title"] = row["title"].strip()
            problems = set()
            if not SKU.fullmatch(row["sku_id"]) or row["sku_id"] in seen:
                problems.add("sku_id")
            seen.add(row["sku_id"])
            if row["slot_id"] not in SLOTS:
                problems.add("slot_id")
            if row["color"] not in COLORS:
                problems.add("color")
            if not 1 <= len(row["title"]) <= 120 or "\x00" in row["title"] or row["title"][0] in "=+@-":
                problems.add("title")
            if row["active"] not in ("true", "false"):
                problems.add("active")
            row["active"] = row["active"] == "true"
            for name, (low, high) in NUMBERS.items():
                value = row[name]
                dimensions = name.endswith("_cm")
                if dimensions and row["slot_id"] != "tablecloth":
                    if value:
                        problems.add(name)
                    row[name] = None
                elif not re.fullmatch(r"[0-9]{1,12}", value) or not low <= int(value) <= high:
                    problems.add(name)
                else:
                    row[name] = int(value)
            if problems:
                errors.extend(
                    field_error(f"{path}.{key}", "IMPORT_VALIDATION_FAILED") for key in sorted(problems)
                )
            else:
                rows.append(row)
        if not count:
            errors.append(field_error("rows", "IMPORT_VALIDATION_FAILED", "В файле нет строк с товарами."))
        return rows, errors[:1000], min(count, 500)
    except (UnicodeDecodeError, csv.Error):
        return (
            [],
            [field_error("file", "IMPORT_VALIDATION_FAILED", "Файл должен быть корректным UTF-8 CSV.")],
            0,
        )


def compare_import(scope, rows):
    existing = {row["sku_id"]: row for row in list_products(scope)}
    errors, operations = [], {"insert_count": 0, "update_count": 0, "unchanged_count": 0}
    for row in rows:
        sku = row["sku_id"]
        previous = existing.get(sku)
        if previous is None:
            operations["insert_count"] += 1
            continue
        immutable = [name for name in (*PHYSICAL, "active") if row[name] != previous[name]]
        errors.extend(
            field_error(
                f"items.{sku}.{key}", "IMPORT_VALIDATION_FAILED", "Поле существующего SKU неизменяемо."
            )
            for key in immutable
        )
        altered = any(row[key] != previous[key] for key in CSV_FIELDS if key != "sku_id")
        operations["update_count" if altered else "unchanged_count"] += 1
    if len(existing) + operations["insert_count"] > 500:
        errors.append(field_error("file", "CATALOG_LIMIT_EXCEEDED"))
    return errors, operations


def import_view(row, now):
    status = row["status"]
    if now >= row["expires_at"] and status == "VALID":
        status = "EXPIRED"
    return {
        "import_id": str(row["import_id"]),
        "status": status,
        "source_sha256": row["source_sha256"],
        "created_at": iso(row["created_at"]),
        "expires_at": iso(row["expires_at"]),
        "base_catalog_version": row["base_catalog_version"],
        "row_count": row["row_count"],
    } | {key: value for key, value in row["validation_report"].items() if key != "normalized_sha256"}


def preview_import(scope, content, parsed):
    rows, errors, count = parsed
    conflicts, operations = compare_import(scope, rows)
    report = operations | {
        "errors": (errors + conflicts)[:1000],
        "warnings": [],
        "normalized_sha256": digest(rows),
    }
    row = {
        "import_id": uuid4(),
        "status": "INVALID" if report["errors"] else "VALID",
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "source_content": content,
        "base_catalog_version": catalog_version(scope),
        "normalized_rows": rows,
        "validation_report": report,
        "row_count": count,
        "created_at": scope.now,
        "expires_at": scope.now + timedelta(seconds=900),
    }
    scope.insert("catalog_imports", **row)
    return import_view(row, scope.now)


def commit_import(scope, import_id, body, event):
    preview = scope.require("catalog_imports", import_id=import_id)
    if preview["status"] == "COMMITTED":
        raise Problem("IMPORT_NOT_VALID")
    if scope.now >= preview["expires_at"]:
        raise Problem("IMPORT_EXPIRED")
    if preview["status"] != "VALID":
        fields = preview["validation_report"]["errors"]
        if fields:
            if any(error["code"] == "CATALOG_LIMIT_EXCEEDED" for error in fields):
                raise Problem("CATALOG_LIMIT_EXCEEDED", fields=fields[:100])
            raise Problem("IMPORT_VALIDATION_FAILED", fields=fields[:100])
        raise Problem("IMPORT_NOT_VALID")
    if not body["expected_catalog_version"] == preview["base_catalog_version"] == catalog_version(scope):
        raise Problem("CONCURRENT_VERSION_CHANGE", refresh=True)
    source = preview["source_content"]
    if source is not None and hashlib.sha256(source).hexdigest() != preview["source_sha256"]:
        raise Problem("INTERNAL_INVARIANT_FAILED")
    rows = preview["normalized_rows"]
    if digest(rows) != preview["validation_report"]["normalized_sha256"]:
        raise Problem("INTERNAL_INVARIANT_FAILED")
    conflicts, counts = compare_import(scope, rows)
    if conflicts:
        if any(error["code"] == "CATALOG_LIMIT_EXCEEDED" for error in conflicts):
            raise Problem("CATALOG_LIMIT_EXCEEDED")
        raise Problem("IMPORT_VALIDATION_FAILED", fields=conflicts[:100])
    inventories = lock_inventory(scope, [row["sku_id"] for row in rows])
    for row in rows:
        if row["sku_id"] in inventories:
            check_stock(inventories[row["sku_id"]], row["stock_on_hand_packs"])
    for row in sorted(rows, key=lambda item: item["sku_id"]):
        sku = row["sku_id"]
        if sku not in inventories:
            new_product(scope, row)
            continue
        inventory = inventories[sku]
        product = scope.require("products", sku_id=sku)
        update_product(scope, product, {key: row[key] for key in ("title", "price_minor", "handling_days")})
        change_inventory(
            scope, inventory, on_hand=row["stock_on_hand_packs"] - inventory["stock_on_hand_packs"]
        )
    if counts["insert_count"] or counts["update_count"]:
        version = bump_catalog(scope)
        event(scope, "catalog.updated", "catalog", "catalog", version, counts)
    scope.update("catalog_imports", {"status": "COMMITTED", "committed_at": scope.now}, import_id=import_id)
    return get_catalog(scope)
