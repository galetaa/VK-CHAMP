"""v2 transport, store primitives and immutable configuration."""

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from jsonschema import FormatChecker
from pydantic import TypeAdapter, ValidationError

from app.common import iso
from app.db import DB
from app.settings import ROOT
from app.v2.dto import from_openapi
from app.wire import Contract, StrictValidator, bounded_body, no_special, unique_keys

CONTRACT_ROOT = ROOT / "packages/contracts_v2"
FIXTURES = ROOT / "fixtures/v2"
PREFIX = "/api/v2"
RULES = json.loads((CONTRACT_ROOT / "business_rules.json").read_text())
SEED = json.loads((FIXTURES / "wire_seed.json").read_text())
TTL = RULES["ttl_seconds"]
FEES = RULES["fees_minor"]
LIMITS = RULES["limits"]
COOKIE_SESSION = "vk_demo_session"
COOKIE_BOOTSTRAP = "vk_demo_bootstrap"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")

with (CONTRACT_ROOT / "error_codes.csv").open(encoding="utf-8-sig") as stream:
    ERRORS = {row["code"]: (int(row["http_status"]), row["meaning"]) for row in csv.DictReader(stream)}


def pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts) or "/"


def issue(path, code="TYPE", message="Проверьте значение поля."):
    return {"path": path[:160], "code": code, "message": message}


class Failure(Exception):
    def __init__(self, code, *, fields=(), **detail):
        super().__init__(code)
        self.code = code
        self.status, message = ERRORS[code]
        self.error = {
            "code": code,
            "message": message,
            "details": {
                "fields": sorted(fields, key=lambda item: (item["path"], item["code"]))[:50],
                "available_quantity": None,
                "current_revision": None,
                "current_state": None,
                "retry_after_seconds": None,
            }
            | detail,
        }


class Store(DB):
    schema = "vk_demo_v2"

    def __init__(self, connection):
        super().__init__(connection)
        self.run("SET LOCAL search_path TO vk_demo_v2, pg_catalog")
        self.run("SET LOCAL lock_timeout = '3s'")
        self.run("SET LOCAL statement_timeout = '5s'")

    def find(self, table, **where):
        query = " AND ".join(f"{column}=%s" for column in where)
        return self.first(f"SELECT * FROM {table} WHERE {query}", *where.values())


@dataclass
class Context:
    db: Store
    actor: dict
    now: datetime

    @property
    def scope(self):
        return {"sandbox_id": self.actor["sandbox_id"], "generation": self.actor["generation"]}

    @property
    def identity(self):
        return self.actor["actor_id"]

    def get(self, table, *, buyer=False, **where):
        subset = {"buyer_id": self.identity} if buyer and self.actor["role"] == "BUYER" else {}
        row = self.db.find(table, **self.scope, **subset, **where)
        if row is None:
            raise Failure("NOT_FOUND")
        return row

    def insert(self, table, **values):
        self.db.insert(table, self.scope | values)

    def update(self, table, data, **where):
        self.db.update(table, data, self.scope | where)

    def list(self, table, *, key="id", cursor=None, limit=50, buyer=False):
        tail, args = ("", [])
        if buyer and self.actor["role"] == "BUYER":
            tail, args = (" AND buyer_id=%s", [self.identity])
        col = 'sku COLLATE "C"' if key == "sku" else key
        if cursor is not None:
            tail += f" AND {col}>%s"
            args.append(cursor)
        rows = self.db.all(
            f"SELECT * FROM {table} WHERE sandbox_id=%s AND generation=%s {tail} ORDER BY {col} LIMIT %s",
            *self.scope.values(),
            *args,
            limit + 1,
        )
        return rows[:limit], str(rows[limit - 1][key]) if len(rows) > limit else None


def emit(ctx, name, **values):
    event_id = values.pop("event_id", uuid4())
    origin = values.pop("origin", "SERVER")
    system = ctx.actor.get("role") == "SYSTEM"
    payload = {
        "event_id": str(event_id),
        "name": name,
        "origin": origin,
        "occurred_at": iso(ctx.now),
        "sandbox_id": str(ctx.actor["sandbox_id"]),
        "generation": ctx.actor["generation"],
        "actor_id": None if system else str(ctx.identity),
        "actor_role": None if system else ctx.actor["role"],
        "offer_id": None,
        "offer_version_no": None,
        "quote_id": None,
        "reservation_id": None,
        "order_id": None,
        "from_state": None,
        "to_state": None,
        "reason": None,
        "entity_revision": None,
        "ui_step": None,
        "is_test": True,
    } | values
    ctx.insert(
        "events",
        event_id=event_id,
        origin=origin,
        name=name,
        occurred_at=ctx.now,
        payload=payload,
        is_test=True,
    )
    ctx.insert("outbox", event_id=event_id, created_at=ctx.now, next_attempt_at=ctx.now)
    return event_id


def check_revision(actual, expected):
    if actual != expected:
        raise Failure("REVISION_CONFLICT", current_revision=actual)


def _nfc(value, path=()):
    if isinstance(value, list):
        return [_nfc(item, (*path, i)) for i, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: _nfc(item, (*path, key)) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    text = unicodedata.normalize("NFC", value)
    if any(
        unicodedata.category(char) in ("Cc", "Cs")
        and not (char == "\n" and path and path[-1] in ("description", "source_text"))
        for char in text
    ):
        raise Failure("VALIDATION_ERROR", fields=[issue(pointer(path), "FORMAT")])
    return text


@dataclass
class Command:
    name: str
    method: str
    path: str
    parameters: dict
    query: dict
    body: dict
    key: str | None
    csrf: str | None
    cookie: str | None
    bootstrap: str | None
    request_id: str
    peer: str
    status: int
    roles: tuple
    sandbox_id: str | None = None


class Wire(Contract):
    def __init__(self):
        super().__init__(CONTRACT_ROOT)
        definitions = from_openapi(self.openapi["components"]["schemas"])
        with (CONTRACT_ROOT / "api_endpoints.csv").open(encoding="utf-8-sig") as stream:
            self.routes = {row["operation_id"]: row for row in csv.DictReader(stream)}
        self.request_models = {
            name: TypeAdapter(definitions(row["request"]))
            for name, row in self.routes.items()
            if row["request"]
        }

    def body_issues(self, name, sample):
        schema = self.openapi["components"]["schemas"][name]
        if "discriminator" in schema and isinstance(sample, dict):
            key = schema["discriminator"]["propertyName"]
            selected = sample.get(key)
            mapping = schema["discriminator"]["mapping"]
            if isinstance(selected, str) and selected in mapping:
                name = mapping[selected].split("/")[-1]
            else:
                return [issue(pointer([key]), "REQUIRED" if key not in sample else "FORMAT")]
        cache_key = "v2.request." + name
        if cache_key not in self.validators:
            self.validators[cache_key] = StrictValidator(
                {"components": self.openapi["components"], "$ref": f"#/components/schemas/{name}"},
                format_checker=FormatChecker(),
            )
        fragments = {}
        for err in self.validators[cache_key].iter_errors(sample):
            path = list(err.absolute_path)
            keyword = err.validator
            if keyword == "required":
                found = [
                    (pointer([*path, key]), "REQUIRED")
                    for key in err.validator_value
                    if key not in err.instance
                ]
            elif keyword == "additionalProperties" and isinstance(err.instance, dict):
                allowed = err.schema.get("properties", {})
                found = [(pointer([*path, key]), "FORMAT") for key in err.instance.keys() - allowed.keys()]
            else:
                suffix = {
                    "enum": "FORMAT",
                    "const": "FORMAT",
                    "format": "FORMAT",
                    "pattern": "FORMAT",
                    "uniqueItems": "DUPLICATE",
                    "minimum": "RANGE",
                    "maximum": "RANGE",
                    "minLength": "RANGE",
                    "maxLength": "RANGE",
                    "minItems": "RANGE",
                    "maxItems": "RANGE",
                }.get(keyword, "TYPE")
                found = [(pointer(path), suffix)]
            for key, rule in found:
                fragments.setdefault((key, rule), issue(key, rule))
        return list(fragments.values())

    async def read(self, req, name, request_id, origin):
        method = req.method
        modifying = method != "GET"
        data = {}
        if modifying:
            if req.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
                raise Failure("INVALID_JSON")
            try:
                raw = await bounded_body(req, LIMITS["max_body_bytes"])
                data = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=no_special
                )
            except (ValueError, UnicodeError, RecursionError):
                raise Failure("INVALID_JSON") from None
            except Exception as exc:
                if getattr(exc, "code", None) != "PAYLOAD_TOO_LARGE":
                    raise
                # v2's published error catalog uses VALIDATION_ERROR for the body-size limit.
                raise Failure("VALIDATION_ERROR", fields=[issue("/body", "RANGE")]) from None
            if req.headers.getlist("origin") != [origin]:
                raise Failure("CSRF_REJECTED")
        route = self.routes[name]
        return Command(
            name,
            method,
            req.url.path,
            dict(req.path_params),
            dict(req.query_params),
            data,
            req.headers.get("idempotency-key"),
            req.headers.get("x-csrf-token"),
            req.cookies.get(COOKIE_SESSION),
            req.cookies.get(COOKIE_BOOTSTRAP),
            request_id,
            req.client.host if req.client else "unknown",
            int(route["status"]),
            tuple(route["roles"].split(",")),
        )

    def validate(self, command):
        _, _, operation = self.operations[command.name]
        issues = []
        for param in operation.get("parameters", ()):
            location, name, schema = (param[key] for key in ("in", "name", "schema"))
            if location == "header":
                if name != "Idempotency-Key":
                    continue
                value = command.key
            else:
                values = command.parameters if location == "path" else command.query
                value = values.get(name)
                if location == "query" and value is not None and schema.get("type") == "integer":
                    if re.fullmatch(r"[0-9]{1,3}", value):
                        value = int(value)
                        values[name] = value
            if value is None and not param.get("required"):
                continue
            if self.issues(value, schema):
                issues.append(issue("/" + name, "REQUIRED" if value is None else "FORMAT"))
        known = {p["name"] for p in operation.get("parameters", ()) if p["in"] == "query"}
        issues.extend(issue("/" + key, "FORMAT") for key in command.query.keys() - known)
        if command.method != "GET":
            command.body = _nfc(command.body)
            request_type = self.routes[command.name]["request"]
            issues.extend(self.body_issues(request_type, command.body))
        if issues:
            raise Failure("VALIDATION_ERROR", fields=issues)
        if command.method != "GET":
            model = self.request_models[command.name]
            try:
                dto = model.validate_python(command.body, strict=True)
            except ValidationError as exc:
                raise Failure(
                    "VALIDATION_ERROR", fields=[issue(pointer(x["loc"])) for x in exc.errors()]
                ) from None
            command.body = dto.model_dump(mode="json")
