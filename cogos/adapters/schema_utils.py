"""JSON-schema sanitising for provider structured-output constraints.

Anthropic structured outputs disallow numeric bounds, string length bounds,
and ``additionalProperties`` other than ``false``; ``$ref``/``$defs`` are
allowed. Pydantic emits bounds for ``Field(ge=..., le=...)`` so we strip them
and enforce bounds ourselves after parsing.
"""

from __future__ import annotations

import json
from typing import Any

_STRIP_KEYS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "multipleOf",
    "format",
}


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in _STRIP_KEYS:
                    continue
                if k == "default":
                    continue  # defaults are applied by pydantic after parsing
                out[k] = walk(v)
            if out.get("type") == "object" or "properties" in out:
                props = out.get("properties")
                if isinstance(props, dict):
                    # structured outputs require every property to be listed as required
                    out["required"] = list(props.keys())
                    out["additionalProperties"] = False
                elif out.get("additionalProperties") not in (False,):
                    # free-form map: represent as object with no declared keys
                    out["additionalProperties"] = False
                    out.setdefault("properties", {})
                    out.setdefault("required", [])
            if "anyOf" in out:
                out["anyOf"] = [walk(x) for x in out["anyOf"]]
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(json.loads(json.dumps(schema)))


def schema_for(model_cls: Any) -> dict[str, Any]:
    return sanitize_schema(model_cls.model_json_schema())
