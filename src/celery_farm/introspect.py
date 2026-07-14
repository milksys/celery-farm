"""Discover registered Celery tasks and describe their call signatures."""

from __future__ import annotations

import ast
import fnmatch
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from celery import Celery
    from celery.app.task import Task

#: Parameter kinds we can expose as keyword arguments in a request body.
_KEYWORD_KINDS = (
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)


@dataclass(frozen=True)
class TaskParam:
    """A single call parameter of a task."""

    name: str
    annotation: Any
    default: Any
    required: bool


@dataclass
class TaskSpec:
    """Introspected description of a registered Celery task."""

    name: str
    task: Task
    params: list[TaskParam] = field(default_factory=list)
    doc: str | None = None
    return_annotation: Any = inspect.Signature.empty
    #: Explicit OpenAPI overrides, set via task decorator kwargs, e.g.
    #: ``@app.task(summary=..., description=..., tags=[...], deprecated=True)``.
    summary: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    deprecated: bool | None = None
    #: Arbitrary extra OpenAPI operation fields (externalDocs, x-* extensions, ...).
    openapi_extra: dict[str, Any] | None = None


def finalize_app(celery_app: Celery) -> None:
    """Import the app's task modules so every task is registered before we read
    ``celery_app.tasks``.

    Task routes are baked from a one-time snapshot of ``celery_app.tasks``, but a
    web process (unlike a worker) never imports the task modules listed in the
    ``imports``/``include`` config or picked up by ``autodiscover_tasks`` — so
    those tasks are missing at build time. This runs the same discovery a worker
    does at startup (:meth:`~celery.loaders.base.BaseLoader.import_default_modules`,
    which imports ``imports``/``include`` and fires the ``import_modules`` signal
    used by autodiscovery), then finalizes the task registry.

    Safe to call repeatedly — module imports are cached in ``sys.modules``.
    """
    celery_app.loader.import_default_modules()
    celery_app.finalize()


def _is_builtin(name: str) -> bool:
    return name.startswith("celery.")


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) or name == p for p in patterns)


def _type_comment_hints(func: Any) -> dict[str, Any]:
    """Recover types from PEP 484 type comments as a fallback for tasks without
    annotations, e.g.::

        def add(x, y):
            # type: (int, int) -> int
            ...

    Returns a ``name -> type`` map (plus ``"return"``) for whatever it can parse
    and resolve; unresolvable entries are simply omitted.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return {}
    try:
        # ``type_comments=True`` requires Python 3.8+; older versions raise.
        tree = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError, TypeError):
        return {}
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if node is None:
        return {}

    globalns = getattr(func, "__globals__", {})

    def _resolve(expr: ast.expr) -> Any:
        try:
            value = eval(  # noqa: S307 - resolving type names against the func's module
                compile(ast.Expression(expr), "<type_comment>", "eval"), globalns
            )
        except Exception:
            return None
        return None if value is Ellipsis else value

    hints: dict[str, Any] = {}
    args = node.args.args

    # Whole-signature comment: ``# type: (int, int) -> int``.
    if node.type_comment:
        try:
            func_type = ast.parse(node.type_comment, mode="func_type")
        except (SyntaxError, ValueError):
            func_type = None
        if func_type is not None:
            argtypes = func_type.argtypes
            # ``# type: (...) -> X`` means "arguments unspecified" (see per-arg
            # comments) — parsed as a lone Ellipsis; take only the return type.
            if (
                len(argtypes) == 1
                and isinstance(argtypes[0], ast.Constant)
                and argtypes[0].value is Ellipsis
            ):
                argtypes = []
            # ``self``/``cls`` is usually omitted from the comment; align to the
            # trailing parameters when the counts differ.
            offset = max(len(args) - len(argtypes), 0)
            for i, expr in enumerate(argtypes):
                idx = i + offset
                if 0 <= idx < len(args):
                    resolved = _resolve(expr)
                    if resolved is not None:
                        hints[args[idx].arg] = resolved
            ret = _resolve(func_type.returns)
            if ret is not None:
                hints["return"] = ret

    # Per-argument comments: ``x,  # type: int``.
    for arg in args:
        comment = getattr(arg, "type_comment", None)
        if arg.arg not in hints and comment:
            try:
                resolved = eval(comment, globalns)  # noqa: S307
            except Exception:
                resolved = None
            if resolved is not None:
                hints[arg.arg] = resolved

    return hints


def build_task_spec(name: str, task: Task) -> TaskSpec:
    """Build a :class:`TaskSpec` from a registered task instance."""
    run = getattr(task, "run", task)
    try:
        signature = inspect.signature(run)
    except (TypeError, ValueError):
        signature = inspect.Signature()

    # Resolve string annotations (present when the task's module uses
    # ``from __future__ import annotations``) to real types. Without this,
    # pydantic receives forward references it cannot evaluate.
    try:
        hints = get_type_hints(run, include_extras=True)
    except Exception:
        hints = {}

    # Fall back to PEP 484 type comments for anything still untyped (params with
    # no annotation and no resolved hint, or a missing return type).
    keyword_params = [
        p
        for p in signature.parameters.values()
        if p.name != "self" and p.kind in _KEYWORD_KINDS
    ]
    needs_fallback = "return" not in hints and (
        signature.return_annotation is inspect.Signature.empty
    )
    needs_fallback = needs_fallback or any(
        p.name not in hints and p.annotation is inspect.Parameter.empty
        for p in keyword_params
    )
    if needs_fallback:
        for key, value in _type_comment_hints(run).items():
            hints.setdefault(key, value)

    params: list[TaskParam] = []
    for param in signature.parameters.values():
        # ``self`` appears for ``bind=True`` tasks; skip *args/**kwargs.
        if param.name == "self":
            continue
        if param.kind not in _KEYWORD_KINDS:
            continue
        required = param.default is inspect.Parameter.empty
        annotation = hints.get(param.name, param.annotation)
        params.append(
            TaskParam(
                name=param.name,
                annotation=annotation,
                default=None if required else param.default,
                required=required,
            )
        )

    doc = inspect.getdoc(run)
    return TaskSpec(
        name=name,
        task=task,
        params=params,
        doc=doc,
        return_annotation=hints.get("return", signature.return_annotation),
        # Read optional overrides passed to the task decorator, e.g.
        # ``@app.task(summary="...", tags=[...])``. Celery stores unknown
        # decorator kwargs as task attributes.
        summary=getattr(task, "summary", None),
        description=getattr(task, "description", None),
        tags=getattr(task, "tags", None),
        deprecated=getattr(task, "deprecated", None),
        openapi_extra=getattr(task, "openapi_extra", None),
    )


def iter_tasks(
    celery_app: Celery,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[TaskSpec]:
    """Return specs for user-registered tasks on ``celery_app``.

    Built-in ``celery.*`` tasks are always excluded. ``include``/``exclude`` are
    lists of task names or glob patterns (e.g. ``"myproj.tasks.*"``).
    """
    specs: list[TaskSpec] = []
    for name, task in sorted(celery_app.tasks.items()):
        if _is_builtin(name):
            continue
        if include and not _matches_any(name, include):
            continue
        if exclude and _matches_any(name, exclude):
            continue
        specs.append(build_task_spec(name, task))
    return specs
