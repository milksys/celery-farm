"""Dispatch tasks and fetch results by id."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from celery import Celery
    from celery.app.task import Task
    from celery.result import AsyncResult


def dispatch(
    task: Task,
    payload: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> AsyncResult:
    """Queue ``task`` with keyword arguments ``payload`` and return its result handle.

    ``options`` are forwarded to ``apply_async`` (e.g. ``queue``, ``countdown``,
    ``eta``).
    """
    return task.apply_async(kwargs=payload, **(options or {}))


def dispatch_by_name(
    celery_app: Celery,
    name: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> AsyncResult | None:
    """Queue the registered task ``name`` with ``args``/``kwargs``.

    Returns ``None`` if no task with that name is registered. Used to run a beat
    entry on demand (dispatching its ``task`` with the configured ``args``/``kwargs``).
    """
    task = celery_app.tasks.get(name)
    if task is None:
        return None
    return task.apply_async(
        args=list(args or []), kwargs=dict(kwargs or {}), **(options or {})
    )


def get_result(celery_app: Celery, task_id: str) -> dict[str, Any]:
    """Return the current status/result for ``task_id`` as a plain dict."""
    async_result = celery_app.AsyncResult(task_id)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": async_result.status,
        "ready": async_result.ready(),
    }
    if async_result.ready():
        if async_result.successful():
            payload["result"] = async_result.result
        else:
            # ``result`` holds the exception instance on failure.
            payload["error"] = str(async_result.result)
    return payload
