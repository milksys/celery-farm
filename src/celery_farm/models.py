"""Build pydantic request models from task signatures."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import is_dataclass
from typing import TYPE_CHECKING, Any, get_origin

from pydantic import BaseModel, create_model

# typing_extensions.is_typeddict recognizes TypedDicts from BOTH typing and
# typing_extensions across all versions; typing.is_typeddict misses the latter
# on Python < 3.12.
from typing_extensions import is_typeddict

from . import _compat

if TYPE_CHECKING:
    from .introspect import TaskParam, TaskSpec

_MODEL_CACHE: dict[str, type[BaseModel]] = {}

_INVALID_CHARS = re.compile(r"[^0-9a-zA-Z]+")


def _model_name(task_name: str) -> str:
    """Turn a dotted task name into a CamelCase pydantic model name."""
    parts = _INVALID_CHARS.split(task_name)
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return f"{camel}Request"


def build_request_model(spec: TaskSpec) -> type[BaseModel]:
    """Create (and cache) a pydantic model describing a task's keyword args."""
    cached = _MODEL_CACHE.get(spec.name)
    if cached is not None:
        return cached

    fields: dict[str, tuple[Any, Any]] = {}
    for param in spec.params:
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = Any
        default = ... if param.required else param.default
        fields[param.name] = (annotation, default)

    model = create_model(_model_name(spec.name), **fields)  # type: ignore[call-overload]
    model.__doc__ = spec.doc or f"Request body for task '{spec.name}'."
    _MODEL_CACHE[spec.name] = model
    return model


def is_object_like(annotation: Any) -> bool:
    """True for types that model a JSON object: pydantic model, dataclass,
    TypedDict, or a ``dict``/``Mapping``."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    if is_dataclass(annotation):
        return True
    if is_typeddict(annotation):
        return True
    if annotation is dict or get_origin(annotation) in (dict, Mapping):
        return True
    return False


def unwrap_param(spec: TaskSpec) -> TaskParam | None:
    """Return the sole parameter if the task's request body should be that
    object directly (single object-typed argument); otherwise ``None``."""
    if len(spec.params) == 1 and is_object_like(spec.params[0].annotation):
        return spec.params[0]
    return None


def result_json_schema(spec: TaskSpec) -> dict[str, Any] | None:
    """Return a JSON schema for the task's return type, if one can be derived.

    Works for builtins, pydantic models, dataclasses, and typing generics. Returns
    ``None`` when the task has no return annotation or the type is not resolvable.
    """
    annotation = spec.return_annotation
    if annotation is inspect.Signature.empty or annotation is None:
        return None
    try:
        return _compat.type_json_schema(annotation)
    except Exception:
        return None
