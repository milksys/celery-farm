"""Framework-neutral OpenAPI document builder.

FastAPI generates its own OpenAPI natively; this module produces an equivalent
document for integrations (e.g. Django) that have no built-in OpenAPI support,
and for standalone use (just extract the spec for any Celery app).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import _compat
from .beat import beat_entry_deprecated, beat_entry_doc, iter_beat_schedule
from .introspect import iter_tasks
from .models import build_request_model, unwrap_param
from .schemas import (
    BeatEntryInfo,
    TaskDispatchResponse,
    TaskInfo,
    TaskResultResponse,
    return_str,
    split_doc,
)

if TYPE_CHECKING:
    from celery import Celery

_REF_TEMPLATE = "#/components/schemas/{model}"

#: Default CDN base URL for the Swagger UI assets (CSS + JS bundle).
SWAGGER_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"

#: Default ``SwaggerUIBundle`` options merged into every rendered page. Callers
#: override/extend these via ``swagger_ui_options`` (``url`` is always set from
#: ``openapi_url`` and takes precedence).
SWAGGER_UI_OPTIONS: dict[str, Any] = {
    "dom_id": "#swagger-ui",
    "deepLinking": True,
}

_SWAGGER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <link rel="stylesheet" href="{cdn}/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{cdn}/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({options});
  </script>
</body>
</html>"""


def swagger_ui_html(
    title: str = "celery_farm",
    openapi_url: str = "openapi.json",
    *,
    cdn: str = SWAGGER_CDN,
    swagger_ui_options: dict[str, Any] | None = None,
) -> str:
    """Return a self-contained Swagger UI page (assets loaded from a CDN) that
    renders the OpenAPI document at ``openapi_url`` (relative by default).

    - ``cdn`` overrides the base URL the ``swagger-ui.css`` / ``swagger-ui-bundle.js``
      assets are loaded from (e.g. to pin a version or self-host).
    - ``swagger_ui_options`` is merged over :data:`SWAGGER_UI_OPTIONS` and passed
      straight to ``SwaggerUIBundle({...})`` (e.g. ``{"docExpansion": "none",
      "tryItOutEnabled": True}``). Values must be JSON-serializable; ``url`` is
      always taken from ``openapi_url``.
    """
    options: dict[str, Any] = {**SWAGGER_UI_OPTIONS}
    if swagger_ui_options:
        options.update(swagger_ui_options)
    options["url"] = openapi_url
    return _SWAGGER_TEMPLATE.format(
        title=title,
        cdn=cdn,
        options=json.dumps(options, indent=6),
    )


#: Response models grouped by section, registered under components/schemas.
_TASK_RESPONSE_MODELS = [TaskDispatchResponse, TaskResultResponse, TaskInfo]
_BEAT_RESPONSE_MODELS = [BeatEntryInfo, TaskDispatchResponse]


def _ref(name: str) -> dict[str, str]:
    return {"$ref": _REF_TEMPLATE.format(model=name)}


def build_openapi(
    celery_app: Celery,
    *,
    title: str = "celery_farm",
    version: str = "0.1.0",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    servers: list[dict[str, Any]] | None = None,
    tasks: bool = True,
    beat: bool = True,
    beat_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI document describing a Celery app's tasks and/or beat.

    ``tasks``/``beat`` select which sections to include, so callers can render a
    tasks-only and a beat-only document into separate Swagger UIs.

    Emits OpenAPI 3.1 under pydantic v2 and 3.0 under pydantic v1, matching the
    JSON Schema dialect each version produces.
    """
    default_tags = tags or ["celery-farm"]
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}

    response_models: list[Any] = []
    if tasks:
        response_models += _TASK_RESPONSE_MODELS
    if beat:
        response_models += _BEAT_RESPONSE_MODELS
    if response_models:
        components.update(_compat.models_schema(response_models, _REF_TEMPLATE))

    if tasks:
        for spec in iter_tasks(celery_app, include=include, exclude=exclude):
            body_defs = _add_task_operation(paths, spec, default_tags)
            components.update(body_defs)
        _add_task_paths(paths, default_tags)

    if beat:
        _add_beat_paths(paths, default_tags, celery_app, beat_meta or {})

    document: dict[str, Any] = {
        "openapi": _compat.OPENAPI_VERSION,
        "info": {"title": title, "version": version},
        "paths": paths,
        "components": {"schemas": components},
    }
    if servers:
        document["servers"] = servers
    return document


def _add_task_operation(paths, spec, default_tags) -> dict[str, Any]:
    """Add the ``POST /tasks/{name}`` operation; return its collected ``$defs``."""
    # Request body schema (unwrapped single object arg, or wrapped model).
    sole = unwrap_param(spec)
    body_type = sole.annotation if sole is not None else build_request_model(spec)
    body_schema, body_defs = _compat.json_schema_of(body_type, _REF_TEMPLATE)

    doc_summary, doc_description = split_doc(spec.doc)
    summary = spec.summary or doc_summary or f"Call task {spec.name}"
    description = spec.description or doc_description
    returns = return_str(spec.return_annotation)
    if returns:
        note = f"Eventual result (via `GET /results/{{task_id}}`): `{returns}`"
        description = f"{description}\n\n{note}" if description else note

    operation: dict[str, Any] = {
        "tags": spec.tags if spec.tags is not None else default_tags,
        "summary": summary,
        "operationId": f"call_{spec.name}",
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": body_schema}},
        },
        "responses": {
            "200": {
                "description": "Successful Response",
                "content": {
                    "application/json": {"schema": _ref("TaskDispatchResponse")}
                },
            }
        },
    }
    if description:
        operation["description"] = description
    if spec.deprecated is not None:
        operation["deprecated"] = spec.deprecated
    if spec.openapi_extra:
        operation.update(spec.openapi_extra)

    paths[f"/tasks/{spec.name}"] = {"post": operation}
    return body_defs


def _list_of(name: str) -> dict[str, Any]:
    return {"type": "array", "items": _ref(name)}


def _get_op(tags: list[str], summary: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "get": {
            "tags": tags,
            "summary": summary,
            "responses": {
                "200": {
                    "description": "Successful Response",
                    "content": {"application/json": {"schema": schema}},
                }
            },
        }
    }


def _add_task_paths(paths: dict[str, Any], tags: list[str]) -> None:
    paths["/tasks"] = _get_op(tags, "List registered tasks", _list_of("TaskInfo"))
    results = _get_op(tags, "Fetch a task result", _ref("TaskResultResponse"))
    results["get"]["parameters"] = [
        {
            "name": "task_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    paths["/results/{task_id}"] = results


def _add_beat_paths(
    paths: dict[str, Any],
    tags: list[str],
    celery_app,
    beat_meta: dict[str, dict[str, Any]],
) -> None:
    # Index: the full schedule as a list.
    paths["/schedule"] = _get_op(
        tags, "All scheduled entries", _list_of("BeatEntryInfo")
    )
    # One operation per scheduled entry, so each shows up in Swagger with its
    # task, cadence, and args.
    for entry in iter_beat_schedule(celery_app):
        entry_meta = beat_meta.get(entry.name) or {}
        summary, description = beat_entry_doc(entry, celery_app, entry_meta)
        entry_tags = entry_meta.get("tags") or tags
        # POST runs the entry now: dispatch its task with configured args/kwargs.
        operation: dict[str, Any] = {
            "tags": entry_tags,
            "summary": summary,
            "operationId": f"beat_run_{entry.name}",
            "description": description,
            "responses": {
                "200": {
                    "description": "Successful Response",
                    "content": {
                        "application/json": {"schema": _ref("TaskDispatchResponse")}
                    },
                }
            },
        }
        deprecated = beat_entry_deprecated(entry, celery_app, entry_meta)
        if deprecated is not None:
            operation["deprecated"] = deprecated
        if entry_meta.get("openapi_extra"):
            operation.update(entry_meta["openapi_extra"])
        paths[f"/schedule/{entry.name}"] = {"post": operation}
