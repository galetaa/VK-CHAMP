"""Build strict request DTOs from the versioned OpenAPI data model."""

from functools import lru_cache
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, create_model


class RequestDTO(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def from_openapi(schemas):
    def field_type(schema):
        if "$ref" in schema:
            return definition(schema["$ref"].split("/")[-1])
        if "enum" in schema:
            return Literal[tuple(schema["enum"])]
        if "const" in schema:
            return Literal[schema["const"]]
        if choices := schema.get("anyOf") or schema.get("oneOf"):
            return Union[tuple(field_type(branch) for branch in choices)]
        kind = schema.get("type")
        if kind == "array":
            return list[field_type(schema.get("items", {}))]
        if kind == "object":
            return object_type("EmbeddedDTO", schema)
        return {"integer": int, "string": str, "boolean": bool, "null": type(None), "number": float}.get(
            kind, Any
        )

    def object_type(name, schema):
        required = set(schema.get("required", ()))
        return create_model(
            name,
            __base__=RequestDTO,
            **{
                key: (field_type(part), ... if key in required else None)
                for key, part in schema.get("properties", {}).items()
            },
        )

    @lru_cache(maxsize=None)
    def definition(name):
        schema = schemas[name]
        if schema.get("type") == "object":
            return object_type(name, schema)
        return field_type(schema)

    return definition
