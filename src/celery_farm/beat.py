"""Introspect the Celery beat schedule."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from celery import Celery


@dataclass
class BeatEntry:
    """A single periodic-task entry from ``beat_schedule``."""

    name: str
    task: str
    schedule: str
    args: list[Any]
    kwargs: dict[str, Any]


def iter_beat_schedule(celery_app: Celery) -> list[BeatEntry]:
    """Return the configured periodic tasks as :class:`BeatEntry` items."""
    schedule = celery_app.conf.beat_schedule or {}
    entries: list[BeatEntry] = []
    for name, entry in schedule.items():
        entries.append(
            BeatEntry(
                name=name,
                task=entry.get("task", ""),
                schedule=str(entry.get("schedule", "")),
                args=list(entry.get("args", []) or []),
                kwargs=dict(entry.get("kwargs", {}) or {}),
            )
        )
    return entries


def humanize_schedule(schedule: str) -> str:
    """Render a schedule string a bit more readably (numbers → ``every Ns``)."""
    try:
        seconds = float(schedule)
    except (TypeError, ValueError):
        return schedule  # crontab / timedelta repr — already descriptive
    return f"every {seconds:g}s"


def _task_doc(celery_app: Celery, task_name: str) -> tuple[str | None, str | None]:
    """Effective (summary, description) of a task: decorator overrides first,
    else its docstring (first line = summary, rest = description)."""
    task = celery_app.tasks.get(task_name) if task_name else None
    if task is None:
        return None, None
    summary = getattr(task, "summary", None)
    description = getattr(task, "description", None)
    if summary and description:
        return summary, description
    doc = inspect.getdoc(getattr(task, "run", task))
    doc_summary = doc_description = None
    if doc:
        lines = doc.strip().splitlines()
        doc_summary = lines[0].strip() or None
        doc_description = "\n".join(lines[1:]).strip() or None
    return summary or doc_summary, description or doc_description


def beat_entry_doc(
    entry: BeatEntry,
    celery_app: Celery,
    meta: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(summary, description)`` documenting a scheduled entry for OpenAPI.

    Precedence for summary/description: explicit ``meta`` override → the referenced
    task's own summary/description → an auto-generated fallback. The schedule and
    args/kwargs are always appended to the description.
    """
    meta = meta or {}
    task_summary, task_description = _task_doc(celery_app, entry.task)

    summary = (
        meta.get("summary")
        or task_summary
        or f"{entry.task} · {humanize_schedule(entry.schedule)}"
    )

    lines: list[str] = []
    head = meta.get("description") or task_description
    if head:
        lines.append(head)
    lines.append(f"**Task:** `{entry.task}`")
    lines.append(f"**Schedule:** {entry.schedule}")
    if entry.args:
        lines.append(f"**Args:** `{entry.args}`")
    if entry.kwargs:
        lines.append(f"**Kwargs:** `{entry.kwargs}`")
    return summary, "\n\n".join(lines)


def beat_entry_deprecated(
    entry: BeatEntry,
    celery_app: Celery,
    meta: dict[str, Any] | None = None,
) -> bool | None:
    """Whether a scheduled entry's operation is deprecated in OpenAPI.

    Precedence: explicit ``meta['deprecated']`` override → the referenced task's
    own ``deprecated`` flag → ``None`` (not set). Mirrors ``@task(deprecated=...)``.
    """
    meta = meta or {}
    if "deprecated" in meta:
        return meta["deprecated"]
    task = celery_app.tasks.get(entry.task) if entry.task else None
    return getattr(task, "deprecated", None) if task is not None else None
