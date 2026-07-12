"""pydantic v1/v2 compatibility shim.

celery_farm requires pydantic and supports both 1.x and 2.x. Every
version-specific pydantic call goes through this module so the rest of the
package stays version-agnostic.

Note: Celery's ``@task(pydantic=True)`` model reconstruction requires pydantic v2;
under v1 a task annotated with a model receives a plain dict instead.
"""

from __future__ import annotations

from typing import Any

import pydantic
from pydantic import ValidationError  # same import path in v1 and v2

PYDANTIC_V2 = pydantic.VERSION.startswith("2")

#: OpenAPI dialect that matches the JSON Schema each pydantic version emits.
OPENAPI_VERSION = "3.1.0" if PYDANTIC_V2 else "3.0.3"

__all__ = [
    "PYDANTIC_V2",
    "OPENAPI_VERSION",
    "ValidationError",
    "dump",
    "validate_model",
    "validate_as",
    "type_json_schema",
    "json_schema_of",
    "models_schema",
]


def dump(instance: Any) -> Any:
    """Serialise a model instance to a dict."""
    if PYDANTIC_V2:
        return instance.model_dump()
    return instance.dict()


def validate_model(model_cls: Any, data: Any) -> Any:
    """Validate ``data`` against a BaseModel subclass, returning an instance."""
    if PYDANTIC_V2:
        return model_cls.model_validate(data)
    return model_cls.parse_obj(data)


def validate_as(tp: Any, data: Any) -> Any:
    """Validate ``data`` against an arbitrary type annotation."""
    if PYDANTIC_V2:
        from pydantic import TypeAdapter

        return TypeAdapter(tp).validate_python(data)
    from pydantic import parse_obj_as

    return parse_obj_as(tp, data)


def type_json_schema(tp: Any) -> dict[str, Any]:
    """Self-contained JSON schema for a type (native dialect, defs embedded)."""
    if PYDANTIC_V2:
        from pydantic import TypeAdapter

        return TypeAdapter(tp).json_schema()
    from pydantic import schema_of

    return schema_of(tp)


def json_schema_of(tp: Any, ref_template: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """JSON schema for ``tp`` split into ``(top_schema, defs)``.

    ``defs`` is a ``name -> schema`` map to hoist into ``components/schemas``;
    ``top_schema`` is the (possibly inline, possibly ``$ref``) request-body schema.
    """
    if PYDANTIC_V2:
        from pydantic import TypeAdapter

        schema = TypeAdapter(tp).json_schema(ref_template=ref_template)
        return schema, schema.pop("$defs", {})
    from pydantic import schema_of

    schema = schema_of(tp, ref_template=ref_template)
    return schema, schema.pop("definitions", {})


def models_schema(models: list[Any], ref_template: str) -> dict[str, Any]:
    """Return a ``name -> schema`` map of definitions for the given models."""
    if PYDANTIC_V2:
        from pydantic.json_schema import models_json_schema

        _, defs = models_json_schema(
            [(m, "serialization") for m in models], ref_template=ref_template
        )
        return defs.get("$defs", defs)
    from pydantic.schema import schema as _v1_schema

    top = _v1_schema(models, ref_template=ref_template)
    return top.get("definitions", {})
