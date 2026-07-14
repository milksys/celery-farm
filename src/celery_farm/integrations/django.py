"""Pure-Django integration: URL patterns exposing Celery tasks as REST endpoints.

No Django REST Framework dependency. Request bodies are validated with pydantic
and the OpenAPI document is produced by :func:`celery_farm.openapi.build_openapi`;
Swagger UI is served from a CDN.

Usage::

    # urls.py
    from django.urls import include, path
    from celery_farm.integrations.django import get_urlpatterns
    from myproj.celery import app as celery_app

    urlpatterns = [path("celery/", include(get_urlpatterns(celery_app)))]
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .. import _compat
from ..beat import iter_beat_schedule
from ..introspect import finalize_app, iter_tasks
from ..invoke import dispatch, dispatch_by_name, get_result
from ..openapi import SWAGGER_CDN, build_openapi, swagger_ui_html
from ..schemas import build_task_info, make_validator

if TYPE_CHECKING:
    from celery import Celery
    from django.http import HttpRequest, HttpResponse


def get_urlpatterns(
    celery_app: Celery,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    title: str = "celery_farm",
    version: str = "0.1.0",
    docs: bool = True,
    beat_meta: dict[str, dict[str, Any]] | None = None,
    finalize: bool = True,
    swagger_cdn: str = SWAGGER_CDN,
    swagger_ui_options: dict[str, Any] | None = None,
) -> list:
    """Build Django URL patterns exposing the Celery app's tasks over REST.

    The task routes are built from a one-time snapshot of ``celery_app.tasks``.
    By default (``finalize=True``) the app's task modules are imported first
    (``imports``/``include`` config plus autodiscovery), matching what a worker
    does at startup, so tasks not yet imported are still exposed. Pass
    ``finalize=False`` to skip (e.g. you import the task modules yourself).

    ``swagger_cdn`` overrides where the Swagger UI assets load from, and
    ``swagger_ui_options`` is merged into the ``SwaggerUIBundle({...})`` call
    (e.g. ``{"docExpansion": "none"}``); both apply to the task and beat docs.
    """
    from django.http import Http404, HttpResponse, JsonResponse
    from django.urls import path
    from django.views.decorators.csrf import csrf_exempt

    if finalize:
        finalize_app(celery_app)
    specs = iter_tasks(celery_app, include=include, exclude=exclude)
    # Precompute per-task (task, validator) so dispatch stays O(1).
    registry = {spec.name: (spec.task, make_validator(spec)) for spec in specs}

    @csrf_exempt
    def call_task(request: HttpRequest, name: str) -> HttpResponse:
        if request.method != "POST":
            return JsonResponse({"detail": "Method not allowed"}, status=405)
        entry = registry.get(name)
        if entry is None:
            raise Http404(f"Unknown task: {name}")
        task, validate = entry
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON body"}, status=400)
        try:
            kwargs = validate(body)
        except _compat.ValidationError as exc:
            return JsonResponse({"detail": exc.errors()}, status=422, safe=False)
        result = dispatch(task, kwargs)
        return JsonResponse({"task_id": result.id, "status": result.status})

    def list_tasks(request: HttpRequest) -> HttpResponse:
        data = [
            _compat.dump(build_task_info(spec))
            for spec in iter_tasks(celery_app, include=include, exclude=exclude)
        ]
        return JsonResponse(data, safe=False)

    def task_result(request: HttpRequest, task_id: str) -> HttpResponse:
        return JsonResponse(get_result(celery_app, task_id))

    def beat_list(request: HttpRequest) -> HttpResponse:
        data = [vars(entry) for entry in iter_beat_schedule(celery_app)]
        return JsonResponse(data, safe=False)

    @csrf_exempt
    def beat_run(request: HttpRequest, name: str) -> HttpResponse:
        # POST runs the entry now (dispatch its task with configured args/kwargs).
        if request.method != "POST":
            return JsonResponse({"detail": "Method not allowed"}, status=405)
        entries = {e.name: e for e in iter_beat_schedule(celery_app)}
        entry = entries.get(name)
        if entry is None:
            raise Http404(f"Unknown beat entry: {name}")
        result = dispatch_by_name(celery_app, entry.task, entry.args, entry.kwargs)
        if result is None:
            raise Http404(f"Task not registered: {entry.task}")
        return JsonResponse({"task_id": result.id, "status": result.status})

    def openapi(request: HttpRequest) -> HttpResponse:
        # Base path (everything up to ".../openapi.json") so Swagger "Try it out"
        # targets the correct URLs regardless of the include() prefix.
        base = request.path.rsplit("/openapi.json", 1)[0] or "/"
        document = build_openapi(
            celery_app,
            title=title,
            version=version,
            include=include,
            exclude=exclude,
            tags=tags,
            servers=[{"url": base}],
            beat=False,  # beat lives in its own Swagger (below)
            finalize=False,  # already discovered at build time (get_urlpatterns)
        )
        return JsonResponse(document)

    def beat_openapi(request: HttpRequest) -> HttpResponse:
        # Beat paths (/schedule, /schedule/{name}) sit under ".../beat", so the
        # server base is everything up to "/openapi.json" (i.e. ".../beat").
        base = request.path.rsplit("/openapi.json", 1)[0] or "/"
        document = build_openapi(
            celery_app,
            title=f"{title} — beat",
            version=version,
            tags=tags,
            servers=[{"url": base}],
            tasks=False,
            beat_meta=beat_meta,
            finalize=False,  # already discovered at build time (get_urlpatterns)
        )
        return JsonResponse(document)

    def swagger(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            swagger_ui_html(
                title, cdn=swagger_cdn, swagger_ui_options=swagger_ui_options
            ),
            content_type="text/html",
        )

    def beat_swagger(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            swagger_ui_html(
                f"{title} — beat",
                cdn=swagger_cdn,
                swagger_ui_options=swagger_ui_options,
            ),
            content_type="text/html",
        )

    patterns = [
        path("tasks", list_tasks, name="celery-farm-tasks"),
        path("tasks/<path:name>", call_task, name="celery-farm-call"),
        path("results/<str:task_id>", task_result, name="celery-farm-result"),
        path("beat/schedule", beat_list, name="celery-farm-beat"),
        # POST runs the entry now (see beat_run); GET list is at beat/schedule.
        path("beat/schedule/<path:name>", beat_run, name="celery-farm-beat-run"),
        path("openapi.json", openapi, name="celery-farm-openapi"),
        path("beat/openapi.json", beat_openapi, name="celery-farm-beat-openapi"),
    ]
    if docs:
        patterns.append(path("docs", swagger, name="celery-farm-docs"))
        patterns.append(path("beat/docs", beat_swagger, name="celery-farm-beat-docs"))
    return patterns
