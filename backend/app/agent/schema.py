"""Turn Pydantic models into tool schemas that the API's strict mode accepts.

Strict tool use guarantees the model's tool input matches the schema, but only
a subset of JSON Schema is supported: every object needs
``additionalProperties: false``, and numeric/length constraints (``minimum``,
``maxLength``, ``maxItems``…) are rejected with a 400. So:

1. generate the schema from the same Pydantic model that validates the input,
2. strip what strict mode can't express (Pydantic still enforces it after),
3. close every object, and
4. pin service-name fields to an ``enum`` of real services, so the model
   physically cannot ask about a service that doesn't exist.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "pattern",
    "title",
}
SERVICE_FIELDS = {"service", "target_service"}
SERVICE_LIST_FIELDS = {"services", "affected_services"}


def strict_schema(model: type[BaseModel], services: Iterable[str]) -> dict[str, Any]:
    names = sorted(services)
    schema = model.model_json_schema()
    _clean(schema, names)
    return schema


def _clean(node: Any, services: list[str], field_name: str | None = None) -> None:
    if isinstance(node, list):
        for item in node:
            _clean(item, services, field_name)
        return
    if not isinstance(node, dict):
        return
    for key in UNSUPPORTED & node.keys():
        del node[key]
    if node.get("minItems", 0) > 1:
        node["minItems"] = 1
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
    if field_name in SERVICE_FIELDS and node.get("type") == "string":
        node["enum"] = services
    if field_name in SERVICE_LIST_FIELDS and node.get("type") == "array":
        node.setdefault("items", {})["enum"] = services
    for name, prop in node.get("properties", {}).items():
        _clean(prop, services, name)
    for key in ("items", "anyOf", "allOf", "$defs", "definitions"):
        if key in node:
            child = node[key]
            if isinstance(child, dict) and key in ("$defs", "definitions"):
                for sub in child.values():
                    _clean(sub, services)
            else:
                _clean(child, services, field_name if key in ("items", "anyOf") else None)
