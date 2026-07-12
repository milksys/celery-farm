"""Example FastAPI app wiring celery_farm to an eager Celery app.

Run it with::

    uv run uvicorn examples.fastapi_app.app:app --reload

Then open http://localhost:8000/celery/docs (tasks) and /beat/docs (schedule).

The tasks below are intentionally varied to show what celery_farm renders into
OpenAPI/Swagger: scalar args + defaults, enums, nested pydantic models,
collections, docstring summary/description, explicit decorator overrides,
type-comment introspection, auto-named tasks, and failures.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from celery import Celery
from fastapi import FastAPI
from pydantic import BaseModel

from celery_farm import create_beat_app, create_task_app

celery_app = Celery("demo")
# Eager mode runs tasks in-process (no broker needed). ``task_store_eager_result``
# + a real result backend make GET /results/{id} work for eager runs too.
celery_app.conf.update(
    task_always_eager=True,
    task_store_eager_result=True,
    result_backend="cache+memory://",
)
celery_app.conf.beat_schedule = {
    "ping-every-30s": {"task": "demo.ping", "schedule": 30.0},
    "report-every-morning": {
        "task": "demo.daily_report",
        "schedule": 86400.0,
        "kwargs": {"recipients": ["ops@example.com"]},
    },
}


# --- Scalars + defaults ----------------------------------------------------
@celery_app.task(name="demo.add")
def add(x: int, y: int) -> int:
    """Add two numbers.

    Returns the integer sum of ``x`` and ``y``. The first docstring line becomes
    the OpenAPI *summary*; the rest becomes the *description*.
    """
    return x + y


@celery_app.task(name="demo.greet")
def greet(name: str, excited: bool = False) -> str:
    """Greet someone, optionally with excitement.

    ``excited`` has a default, so the request body may omit it.
    """
    return f"Hello, {name}{'!' if excited else '.'}"


# --- Decorator summary/description override + auto-generated name -----------
@celery_app.task(
    summary="Send a notification",
    description="The decorator's summary/description override the docstring below.",
)
def notify(user_id: int, message: str) -> dict:
    """This docstring is ignored in OpenAPI because the decorator sets both fields.

    There is also no explicit ``name=``, so Celery derives the task name from the
    module and function (``examples.fastapi_app.app.notify``).
    """
    return {"user_id": user_id, "delivered": message}


# --- PEP 484 type-comment introspection (no annotations) -------------------
@celery_app.task(name="demo.legacy_mul", tags=["legacy"])
def legacy_mul(x, y):
    # type: (int, int) -> int
    """Multiply two numbers, typed via a type comment.

    celery_farm falls back to parsing ``# type:`` comments when a task has no
    annotations, so ``x`` and ``y`` are still typed in the schema.
    """
    return x * y


# --- Enum argument (renders as a dropdown in Swagger) ----------------------
class Priority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


@celery_app.task(name="demo.enqueue_job", tags=["jobs"])
def enqueue_job(job: str, priority: Priority = Priority.normal) -> dict:
    """Enqueue a named job at a given priority.

    ``tags=["jobs"]`` groups this operation under a "jobs" section in Swagger.
    """
    return {"job": job, "priority": priority.value, "queued": True}


# --- Nested pydantic models as arguments -----------------------------------
class Address(BaseModel):
    street: str
    city: str
    zipcode: str


class Order(BaseModel):
    order_id: str
    items: list[str]
    total: float


@celery_app.task(
    name="demo.ship_order",
    pydantic=True,
    tags=["orders"],
    openapi_extra={
        "externalDocs": {
            "description": "Shipping policy",
            "url": "https://example.com/shipping",
        }
    },
)
def ship_order(order: Order, address: Address, express: bool = False) -> dict:
    """Ship an order to an address.

    Nested pydantic models render as a full nested schema in Swagger. The
    ``pydantic=True`` task option tells Celery to rebuild the model instances
    from the incoming (JSON) dicts before running the task.
    """
    return {
        "order_id": order.order_id,
        "shipped_to": f"{address.street}, {address.city} {address.zipcode}",
        "item_count": len(order.items),
        "express": express,
    }


# --- Collections + datetime (also used by the beat schedule) ---------------
@celery_app.task(name="demo.daily_report")
def daily_report(
    recipients: list[str],
    since: datetime | None = None,
    tags: dict[str, str] | None = None,
) -> dict:
    """Build and 'send' a daily report."""
    return {
        "recipients": recipients,
        "since": since.isoformat() if since else None,
        "tags": tags or {},
    }


# --- A task that fails (see the error via GET /results/{id}) ----------------
@celery_app.task(name="demo.divide", deprecated=True)
def divide(x: int, y: int) -> float:
    """Divide ``x`` by ``y``.

    Call with ``y=0`` to see a failure surface as
    ``{"status": "FAILURE", "error": "..."}`` from ``GET /results/{task_id}``.
    """
    return x / y


# --- Health check (used by beat) -------------------------------------------
@celery_app.task(name="demo.ping")
def ping() -> str:
    """Health-check task used by the beat schedule."""
    return "pong"


app = FastAPI(title="celery_farm demo")
# Tasks and beat are each mounted as their own sub-app with isolated Swagger UI.
app.mount("/celery", create_task_app(celery_app))  # /celery/docs, /celery/tasks/{name}
# Each beat entry's summary/description are inherited from the scheduled task's
# own docstring — no extra config needed. (Pass ``beat_meta={entry: {...}}`` only
# when you want to override that per entry, e.g. one task scheduled several ways.)
app.mount("/beat", create_beat_app(celery_app))  # /beat/docs, /beat/schedule
