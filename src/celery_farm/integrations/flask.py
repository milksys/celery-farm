"""Flask integration: a Blueprint exposing Celery tasks as REST endpoints.

Request bodies are validated with pydantic and the OpenAPI document is produced
by :func:`celery_farm.openapi.build_openapi`; Swagger UI is served from a CDN.

Usage::

    from flask import Flask
    from celery_farm.integrations.flask import create_blueprint
    from myproj.celery import app as celery_app

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(celery_app), url_prefix="/celery")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import _compat
from ..beat import iter_beat_schedule
from ..introspect import iter_tasks
from ..invoke import dispatch, dispatch_by_name, get_result
from ..openapi import SWAGGER_CDN, build_openapi, swagger_ui_html
from ..schemas import build_task_info, make_validator

if TYPE_CHECKING:
    from celery import Celery
    from flask import Blueprint


def create_blueprint(
    celery_app: Celery,
    *,
    name: str = "celery_farm",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    title: str = "celery_farm",
    version: str = "0.1.0",
    docs: bool = True,
    beat_meta: dict[str, dict[str, Any]] | None = None,
    swagger_cdn: str = SWAGGER_CDN,
    swagger_ui_options: dict[str, Any] | None = None,
) -> Blueprint:
    """Build a Flask ``Blueprint`` exposing the Celery app's tasks over REST.

    - ``POST /tasks/{name}`` queues a task and returns its ``task_id``.
    - ``GET  /tasks`` lists tasks, parameters, and return types.
    - ``GET  /results/{task_id}`` reports task status/result.
    - ``GET  /beat`` lists the periodic-task schedule.
    - ``GET  /openapi.json`` + ``GET /docs`` expose OpenAPI and Swagger UI.

    ``swagger_cdn`` overrides where the Swagger UI assets load from, and
    ``swagger_ui_options`` is merged into the ``SwaggerUIBundle({...})`` call
    (e.g. ``{"docExpansion": "none"}``); both apply to the task and beat docs.
    """
    from flask import Blueprint, Response, abort, jsonify, request

    bp = Blueprint(name, __name__)
    specs = iter_tasks(celery_app, include=include, exclude=exclude)
    # Precompute per-task (task, validator) so dispatch stays O(1).
    registry = {spec.name: (spec.task, make_validator(spec)) for spec in specs}

    @bp.post("/tasks/<path:name>")
    def call_task(name: str):
        entry = registry.get(name)
        if entry is None:
            abort(404, description=f"Unknown task: {name}")
        task, validate = entry
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        try:
            kwargs = validate(body)
        except _compat.ValidationError as exc:
            return jsonify({"detail": exc.errors()}), 422
        result = dispatch(task, kwargs)
        return jsonify({"task_id": result.id, "status": result.status})

    @bp.get("/tasks")
    def list_tasks():
        return jsonify(
            [
                _compat.dump(build_task_info(spec))
                for spec in iter_tasks(celery_app, include=include, exclude=exclude)
            ]
        )

    @bp.get("/results/<task_id>")
    def task_result(task_id: str):
        return jsonify(get_result(celery_app, task_id))

    @bp.get("/beat/schedule")
    def beat_list():
        return jsonify([vars(entry) for entry in iter_beat_schedule(celery_app)])

    @bp.post("/beat/schedule/<path:name>")
    def beat_run(name: str):
        # POST runs the entry now (dispatch its task with configured args/kwargs).
        entries = {e.name: e for e in iter_beat_schedule(celery_app)}
        entry = entries.get(name)
        if entry is None:
            abort(404, description=f"Unknown beat entry: {name}")
        result = dispatch_by_name(celery_app, entry.task, entry.args, entry.kwargs)
        if result is None:
            abort(404, description=f"Task not registered: {entry.task}")
        return jsonify({"task_id": result.id, "status": result.status})

    @bp.get("/openapi.json")
    def openapi():
        # Base path (everything up to ".../openapi.json") so Swagger "Try it out"
        # targets the correct URLs regardless of the blueprint's url_prefix.
        base = request.path.rsplit("/openapi.json", 1)[0] or "/"
        return jsonify(
            build_openapi(
                celery_app,
                title=title,
                version=version,
                include=include,
                exclude=exclude,
                tags=tags,
                servers=[{"url": base}],
                beat=False,  # beat lives in its own Swagger (below)
            )
        )

    @bp.get("/beat/openapi.json")
    def beat_openapi():
        # Beat paths (/schedule, /schedule/{name}) sit under ".../beat", so the
        # server base is everything up to "/openapi.json" (i.e. ".../beat").
        base = request.path.rsplit("/openapi.json", 1)[0] or "/"
        return jsonify(
            build_openapi(
                celery_app,
                title=f"{title} — beat",
                version=version,
                tags=tags,
                servers=[{"url": base}],
                tasks=False,
                beat_meta=beat_meta,
            )
        )

    if docs:

        @bp.get("/docs")
        def swagger():
            return Response(
                swagger_ui_html(
                    title, cdn=swagger_cdn, swagger_ui_options=swagger_ui_options
                ),
                mimetype="text/html",
            )

        @bp.get("/beat/docs")
        def beat_swagger():
            return Response(
                swagger_ui_html(
                    f"{title} — beat",
                    cdn=swagger_cdn,
                    swagger_ui_options=swagger_ui_options,
                ),
                mimetype="text/html",
            )

    return bp
