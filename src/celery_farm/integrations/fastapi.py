"""FastAPI integration: mount REST endpoints for Celery tasks.

Because each task route carries a real pydantic request model, FastAPI's native
``/openapi.json`` and Swagger UI (``/docs``) describe every task automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from .. import _compat
from ..beat import beat_entry_deprecated, beat_entry_doc, iter_beat_schedule
from ..introspect import iter_tasks
from ..invoke import dispatch, dispatch_by_name, get_result
from ..models import build_request_model, unwrap_param
from ..schemas import (
    BeatEntryInfo,
    TaskDispatchResponse,
    TaskInfo,
    TaskResultResponse,
    build_task_info,
    payload_to_dict,
    return_str,
    split_doc,
)

if TYPE_CHECKING:
    from celery import Celery
    from fastapi import APIRouter, FastAPI


class FastAPIOptions(TypedDict, total=False):
    """Commonly-used ``FastAPI(...)`` options forwarded by the app builders.

    Curated subset (PEP 692); pass ``docs_url=None`` / ``openapi_url=None`` to
    disable the docs & schema. Any FastAPI option not listed here still works at
    runtime — a type checker will just flag it as unexpected.
    """

    description: str
    version: str
    openapi_url: str | None
    docs_url: str | None
    redoc_url: str | None
    swagger_ui_oauth2_redirect_url: str | None
    swagger_ui_parameters: dict[str, Any]
    terms_of_service: str
    openapi_tags: list[dict[str, Any]]
    servers: list[dict[str, Any]]
    root_path: str
    debug: bool


def _build_task_router(
    celery_app: Celery,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
) -> APIRouter:
    """Internal: build the task-route ``APIRouter``. The public entry point is
    :func:`create_task_app`, which wraps this in its own mountable FastAPI app.

    - ``POST /tasks/{name}`` queues a task and returns its ``task_id``.
    - ``GET  /tasks`` lists tasks and their parameters.
    - ``GET  /results/{task_id}`` reports task status/result.
    """
    from fastapi import APIRouter

    # Tags are applied per-route (not on the router) so that a task's own tags
    # fully replace the default instead of merging with it.
    default_tags = tags or ["celery-farm"]
    router = APIRouter()
    specs = iter_tasks(celery_app, include=include, exclude=exclude)

    def make_endpoint(task, body_type: Any, kwarg_name: str | None):
        # ``kwarg_name`` set  -> unwrapped: the body IS the single argument, so
        #                        forward it as kwargs={kwarg_name: body}.
        # ``kwarg_name`` None -> wrapped: the body is a model whose fields are
        #                        the task's keyword arguments.
        async def endpoint(payload):
            if kwarg_name is not None:
                kwargs = {kwarg_name: payload_to_dict(payload)}
            else:
                kwargs = _compat.dump(payload)
            result = dispatch(task, kwargs)
            return TaskDispatchResponse(task_id=result.id, status=result.status)

        # Set the annotation to the concrete type object (not a string) so
        # FastAPI derives the request body / OpenAPI schema. This module uses
        # ``from __future__ import annotations``, so an inline ``payload: Model``
        # annotation would be stored as an unresolvable forward reference.
        endpoint.__annotations__ = {
            "payload": body_type,
            "return": TaskDispatchResponse,
        }
        return endpoint

    for spec in specs:
        sole = unwrap_param(spec)
        if sole is not None:
            body_type: Any = sole.annotation
            kwarg_name: str | None = sole.name
        else:
            body_type = build_request_model(spec)
            kwarg_name = None
        doc_summary, doc_description = split_doc(spec.doc)
        # Explicit @app.task(summary=..., description=...) overrides the docstring.
        summary = spec.summary or doc_summary
        description = spec.description or doc_description
        # Document what the task ultimately returns (fetched via GET /results).
        returns = return_str(spec.return_annotation)
        if returns:
            note = f"Eventual result (via `GET /results/{{task_id}}`): `{returns}`"
            description = f"{description}\n\n{note}" if description else note
        route_kwargs: dict[str, Any] = {}
        if spec.openapi_extra is not None:
            route_kwargs["openapi_extra"] = spec.openapi_extra
        if spec.deprecated is not None:
            route_kwargs["deprecated"] = spec.deprecated
        router.add_api_route(
            f"/tasks/{spec.name}",
            make_endpoint(spec.task, body_type, kwarg_name),
            methods=["POST"],
            response_model=TaskDispatchResponse,
            name=f"call_{spec.name}",
            # Per-task tags override the router default when provided.
            tags=spec.tags if spec.tags is not None else default_tags,
            summary=summary or f"Call task {spec.name}",
            description=description,
            **route_kwargs,
        )

    @router.get(
        "/tasks", response_model=list[TaskInfo], name="list_tasks", tags=default_tags
    )
    async def list_tasks() -> list[TaskInfo]:
        return [
            build_task_info(spec)
            for spec in iter_tasks(celery_app, include=include, exclude=exclude)
        ]

    @router.get(
        "/results/{task_id}",
        response_model=TaskResultResponse,
        name="get_result",
        tags=default_tags,
    )
    async def get_task_result(task_id: str) -> TaskResultResponse:
        return TaskResultResponse(**get_result(celery_app, task_id))

    return router


def create_task_app(
    celery_app: Celery,
    *,
    title: str = "celery_farm",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    dependencies: list[Any] | None = None,
    **fastapi_kwargs: Unpack[FastAPIOptions],
) -> FastAPI:
    """Build a standalone FastAPI app for the task routes, ready to ``mount``.

    Gives the task API its own Swagger UI, isolated at the mount point —
    symmetric with :func:`create_beat_app`. ``dependencies`` (FastAPI ``Depends``)
    apply to every route, e.g. for authentication::

        app.mount("/celery", create_task_app(celery_app, dependencies=[Depends(auth)]))
        app.mount("/beat", create_beat_app(celery_app))
        # tasks: /celery/docs, /celery/tasks/{name}, ...
        # beat:  /beat/docs, /beat/schedule, ...

    Any extra keyword is forwarded to the underlying ``FastAPI(...)``, so you can
    disable the docs/schema or customize Swagger UI (FastAPI renders it natively;
    use its own ``swagger_ui_parameters=``), e.g.::

        create_task_app(celery_app, docs_url=None, redoc_url=None, openapi_url=None)
        create_task_app(celery_app, swagger_ui_parameters={"docExpansion": "none"})
    """
    from fastapi import FastAPI

    task_app = FastAPI(title=title, dependencies=dependencies, **fastapi_kwargs)
    task_app.include_router(
        _build_task_router(celery_app, include=include, exclude=exclude, tags=tags)
    )
    return task_app


def _build_beat_router(
    celery_app: Celery,
    *,
    tags: list[str] | None = None,
    beat_meta: dict[str, dict[str, Any]] | None = None,
) -> APIRouter:
    """Internal: build the beat-schedule ``APIRouter``. The public entry point is
    :func:`create_beat_app`, which wraps this in its own mountable FastAPI app.

    - ``GET  /schedule`` lists every scheduled entry (read-only introspection).
    - ``POST /schedule/{entry}`` runs one entry now — dispatches its task with the
      configured args/kwargs and returns a ``task_id``.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter()
    beat_tags = tags or ["celery-farm-beat"]
    meta = beat_meta or {}

    @router.get(
        "/schedule",
        response_model=list[BeatEntryInfo],
        name="list_beat",
        tags=beat_tags,
        summary="All scheduled entries",
    )
    async def list_beat() -> list[BeatEntryInfo]:
        return [
            BeatEntryInfo(**vars(entry)) for entry in iter_beat_schedule(celery_app)
        ]

    def make_run_handler(entry_name: str):
        async def run_entry() -> TaskDispatchResponse:
            entries = {e.name: e for e in iter_beat_schedule(celery_app)}
            entry = entries.get(entry_name)
            if entry is None:
                raise HTTPException(404, f"Unknown beat entry: {entry_name}")
            result = dispatch_by_name(celery_app, entry.task, entry.args, entry.kwargs)
            if result is None:
                raise HTTPException(404, f"Task not registered: {entry.task}")
            return TaskDispatchResponse(task_id=result.id, status=result.status)

        return run_entry

    for entry in iter_beat_schedule(celery_app):
        entry_meta = meta.get(entry.name) or {}
        summary, description = beat_entry_doc(entry, celery_app, entry_meta)
        entry_tags = entry_meta.get("tags") or beat_tags
        deprecated = beat_entry_deprecated(entry, celery_app, entry_meta)
        route_kwargs: dict[str, Any] = {}
        if deprecated is not None:
            route_kwargs["deprecated"] = deprecated
        if entry_meta.get("openapi_extra"):
            route_kwargs["openapi_extra"] = entry_meta["openapi_extra"]
        # POST runs the entry now: dispatch its task with the configured
        # args/kwargs (without waiting for the beat scheduler).
        router.add_api_route(
            f"/schedule/{entry.name}",
            make_run_handler(entry.name),
            methods=["POST"],
            response_model=TaskDispatchResponse,
            name=f"beat_run_{entry.name}",
            tags=entry_tags,
            summary=summary,
            description=description,
            **route_kwargs,
        )

    return router


def create_beat_app(
    celery_app: Celery,
    *,
    title: str = "celery_farm — beat",
    tags: list[str] | None = None,
    beat_meta: dict[str, dict[str, Any]] | None = None,
    dependencies: list[Any] | None = None,
    **fastapi_kwargs: Unpack[FastAPIOptions],
) -> FastAPI:
    """Build a standalone FastAPI app for the beat schedule.

    ``GET /schedule`` lists the entries; each entry also gets a ``POST
    /schedule/{entry}`` that runs it now (dispatches its task with the configured
    args/kwargs). Mount it to give beat its own Swagger UI, separate from tasks::

        app.mount("/beat", create_beat_app(celery_app))
        # -> GET /beat/schedule, POST /beat/schedule/{entry}, /beat/docs

    ``beat_meta`` overrides per-entry OpenAPI fields — ``summary``, ``description``,
    and ``tags`` (a Celery ``beat_schedule`` entry can't carry these itself)::

        create_beat_app(celery_app, beat_meta={
            "ping-every-30s": {"summary": "Liveness", "tags": ["health"]},
        })

    Any extra keyword is forwarded to the underlying ``FastAPI(...)`` — e.g.
    ``docs_url=None, openapi_url=None`` to disable the docs/schema.
    """
    from fastapi import FastAPI

    beat_app = FastAPI(title=title, dependencies=dependencies, **fastapi_kwargs)
    beat_app.include_router(
        _build_beat_router(celery_app, tags=tags, beat_meta=beat_meta)
    )
    return beat_app
