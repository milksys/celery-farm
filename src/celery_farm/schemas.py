"""Framework-neutral response models and helpers shared by all integrations."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from . import _compat
from .models import build_request_model, result_json_schema, unwrap_param

if TYPE_CHECKING:
    from .introspect import TaskSpec


class TaskParamInfo(BaseModel):
    name: str
    annotation: str
    required: bool
    default: Any = None


class TaskInfo(BaseModel):
    name: str
    doc: str | None = None
    params: list[TaskParamInfo] = Field(default_factory=list)
    #: Readable return type (e.g. "int") and its JSON schema, when annotated.
    returns: str | None = None
    result_schema: dict[str, Any] | None = None


class TaskDispatchResponse(BaseModel):
    task_id: str
    status: str


class TaskResultResponse(BaseModel):
    task_id: str
    status: str
    ready: bool
    result: Any = None
    error: str | None = None


class BeatEntryInfo(BaseModel):
    name: str
    task: str
    schedule: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


def annotation_str(annotation: Any) -> str:
    if annotation is None:
        return "Any"
    return getattr(annotation, "__name__", None) or str(annotation)


def return_str(annotation: Any) -> str | None:
    """Readable return type, or ``None`` when the task has no return annotation."""
    if annotation is inspect.Signature.empty or annotation is None:
        return None
    return annotation_str(annotation)


def split_doc(doc: str | None) -> tuple[str | None, str | None]:
    """Split a docstring into (summary, description).

    Follows the common convention: the first line is the summary and the
    remaining text (if any) is the longer description.
    """
    if not doc:
        return None, None
    lines = doc.strip().splitlines()
    summary = lines[0].strip() or None
    description = "\n".join(lines[1:]).strip() or None
    return summary, description


def payload_to_dict(payload: Any) -> Any:
    """Normalise a validated unwrapped body into a JSON-serialisable value for
    Celery. Pydantic models / dataclasses become dicts; TypedDict/dict pass through."""
    if isinstance(payload, BaseModel):
        return _compat.dump(payload)
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    return payload


def make_validator(spec: TaskSpec) -> Callable[[Any], dict[str, Any]]:
    """Return ``body -> kwargs``: validate a JSON request body and map it to the
    task's keyword arguments, handling single-object unwrap. Raises
    ``_compat.ValidationError`` on invalid input. Used by non-FastAPI integrations
    (Django, Flask) that validate bodies themselves.
    """
    sole = unwrap_param(spec)
    if sole is not None:

        def validate(body: Any) -> dict[str, Any]:
            return {
                sole.name: payload_to_dict(_compat.validate_as(sole.annotation, body))
            }

        return validate

    model = build_request_model(spec)

    def validate(body: Any) -> dict[str, Any]:
        return _compat.dump(_compat.validate_model(model, body))

    return validate


def build_task_info(spec: TaskSpec) -> TaskInfo:
    return TaskInfo(
        name=spec.name,
        doc=spec.doc,
        params=[
            TaskParamInfo(
                name=p.name,
                annotation=annotation_str(p.annotation),
                required=p.required,
                default=p.default,
            )
            for p in spec.params
        ],
        returns=return_str(spec.return_annotation),
        result_schema=result_json_schema(spec),
    )
