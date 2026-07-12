"""celery_farm: OpenAPI/Swagger and REST endpoints for registered Celery tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .beat import BeatEntry, iter_beat_schedule
from .introspect import TaskParam, TaskSpec, build_task_spec, iter_tasks
from .invoke import dispatch, get_result
from .models import build_request_model
from .openapi import build_openapi

if TYPE_CHECKING:
    from .integrations.django import get_urlpatterns
    from .integrations.fastapi import create_beat_app, create_task_app
    from .integrations.flask import create_blueprint

try:
    # Written at build time by hatch-vcs from the git tag (see pyproject.toml).
    from ._version import __version__
except ImportError:  # running from a source tree that was never built
    __version__ = "0.0.0+unknown"

__all__ = [
    "BeatEntry",
    "TaskParam",
    "TaskSpec",
    "build_openapi",
    "build_request_model",
    "build_task_spec",
    "create_beat_app",
    "create_blueprint",
    "create_task_app",
    "dispatch",
    "get_result",
    "get_urlpatterns",
    "iter_beat_schedule",
    "iter_tasks",
]


def __getattr__(name: str) -> Any:
    # Lazily import framework integrations so the core stays usable without
    # FastAPI / Django / Flask installed.
    if name == "create_beat_app":
        from .integrations.fastapi import create_beat_app

        return create_beat_app
    if name == "create_task_app":
        from .integrations.fastapi import create_task_app

        return create_task_app
    if name == "get_urlpatterns":
        from .integrations.django import get_urlpatterns

        return get_urlpatterns
    if name == "create_blueprint":
        from .integrations.flask import create_blueprint

        return create_blueprint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
