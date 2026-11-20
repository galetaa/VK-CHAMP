import csv
import json
import re
from dataclasses import dataclass
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker, validators
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request

from app.common import Problem, field_error

JSON_LIMIT = 256 * 1024
MIB = 1024 * 1024

StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, value: type(value) is int),
)


@dataclass
class Command:
    operation: str
    method: str
    path: str
    body: dict
    path_values: dict
    query: dict
    epoch: int | None
    key: str | None
    token: str | None
    request_id: str
    peer: str


def unique_keys(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def no_special(_):
    raise ValueError("Non-finite number")


def validate_json_strings(value):
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("NUL not supported by the database")
        value.encode("utf-8")
    elif isinstance(value, list):
        for item in value:
            validate_json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_json_strings(key)
            validate_json_strings(item)


async def bounded_body(request, limit):
    length = request.headers.get("content-length")
    if length and length.isdecimal() and int(length) > limit:
        raise Problem("PAYLOAD_TOO_LARGE")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise Problem("PAYLOAD_TOO_LARGE")
        data.extend(chunk)
    return bytes(data)


def report_path(parts):
    path = ""
    for key in parts:
        path += f"[{key}]" if isinstance(key, int) else ("." if path else "") + key
    return path or "body"


class Contract:
    def __init__(self, root):
        self.openapi = json.loads((root / "openapi.json").read_text())
        with (root / "error_codes.csv").open(encoding="utf-8-sig") as stream:
            self.statuses = {row["code"]: int(row["http_status"]) for row in csv.DictReader(stream)}
        self.validators = {}
        self.operations = {}
        for path, methods in self.openapi["paths"].items():
            for method, operation in methods.items():
                name = operation["operationId"]
                self.operations[name] = (path, method.upper(), operation)

    def issues(self, value, schema):
        key = json.dumps(schema, sort_keys=True)
        if key not in self.validators:
            context = {"components": self.openapi["components"]} | schema
            self.validators[key] = StrictValidator(context, format_checker=FormatChecker())
        validator = self.validators[key]
        return [field_error(report_path(error.absolute_path)) for error in validator.iter_errors(value)][:100]

    def dto(self, schema, value):
        return self.issues(value, {"$ref": f"#/components/schemas/{schema}"})

    async def parse(self, request, operation, request_id, settings, *, cookie_name):
        route, method, operation_schema = self.operations[operation]
        if method != "GET":
            origins = request.headers.getlist("origin")
            if origins != [settings.web_origin]:
                raise Problem("ORIGIN_NOT_ALLOWED")
        token = request.cookies.get(cookie_name)
        if not token or len(token) != 43 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            token = None
        if operation not in ("healthLive", "healthReady", "createSandbox") and not token:
            raise Problem("DEMO_SESSION_REQUIRED")

        path_values, query, headers = {}, {}, {}
        for parameter in operation_schema.get("parameters", []):
            place, name, schema = parameter["in"], parameter["name"], parameter["schema"]
            raw = {"path": request.path_params, "header": request.headers, "query": request.query_params}[
                place
            ].get(name)
            if raw is None:
                if parameter.get("required"):
                    raise Problem("VALIDATION_ERROR", fields=[field_error(name)])
                continue
            value = raw
            if schema.get("type") == "integer" and re.fullmatch(r"[1-9][0-9]{0,9}", raw):
                value = int(raw)
            if self.issues(value, schema):
                raise Problem("VALIDATION_ERROR", fields=[field_error(name)])
            if schema.get("format") == "uuid":
                value = str(UUID(value))
            {"path": path_values, "header": headers, "query": query}[place][name] = value
        if set(request.query_params) - set(query):
            raise Problem("VALIDATION_ERROR", fields=[field_error("query")])

        body = {}
        content = operation_schema.get("requestBody", {}).get("content", {})
        kind = request.headers.get("content-type", "").lower().split(";", 1)[0].strip()
        if "application/json" in content:
            if kind != "application/json":
                raise Problem("UNSUPPORTED_MEDIA_TYPE")
            data = await bounded_body(request, JSON_LIMIT)
            try:
                body = json.loads(
                    data.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=no_special
                )
                validate_json_strings(body)
            except (ValueError, UnicodeError, RecursionError):
                raise Problem("MALFORMED_JSON") from None
            errors = self.issues(body, content["application/json"]["schema"])
            if errors:
                raise Problem("VALIDATION_ERROR", fields=errors)
        elif "multipart/form-data" in content:
            if kind != "multipart/form-data":
                raise Problem("UNSUPPORTED_MEDIA_TYPE")
            limit = (2 if operation == "previewCatalogImport" else 50) * MIB
            data = await bounded_body(request, limit + 65536)

            async def receive():
                return {"type": "http.request", "body": data, "more_body": False}

            buffered = Request(request.scope, receive)
            try:
                async with buffered.form(max_files=1, max_fields=1, max_part_size=4096) as form:
                    expected = {"file"} if operation == "previewCatalogImport" else {"kind", "file"}
                    if len(form.multi_items()) != len(expected) or set(form) != expected:
                        raise Problem("VALIDATION_ERROR", fields=[field_error("file")])
                    if not isinstance(form["file"], UploadFile):
                        raise Problem("VALIDATION_ERROR", fields=[field_error("file")])
                    file = await form["file"].read(limit + 1)
                    if len(file) > limit:
                        raise Problem("PAYLOAD_TOO_LARGE")
                    body = {"file": file}
                    if "kind" in expected:
                        if not isinstance(form["kind"], str):
                            raise Problem("VALIDATION_ERROR", fields=[field_error("kind")])
                        body["kind"] = form["kind"]
            except MultiPartException:
                raise Problem("MALFORMED_JSON") from None
            except Exception as exc:
                # Starlette wraps multipart errors when an application is present in the scope.
                if getattr(exc, "status_code", None) != 400:
                    raise
                raise Problem("VALIDATION_ERROR", fields=[field_error("file")]) from None
        actual_path = route
        for name, value in path_values.items():
            actual_path = actual_path.replace("{" + name + "}", str(value))
        peer = request.client.host if request.client else "unknown"
        if settings.trusted_proxy and peer == settings.trusted_proxy:
            peer = request.headers.get("x-real-ip", peer)
        return Command(
            operation,
            method,
            "/api/v1" + actual_path,
            body,
            path_values,
            query,
            headers.get("X-Sandbox-Epoch"),
            headers.get("Idempotency-Key"),
            token,
            request_id,
            peer,
        )
