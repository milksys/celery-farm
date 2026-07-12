"""Eager Celery app + example tasks shared by the Django example."""

from __future__ import annotations

from enum import StrEnum

from celery import Celery
from pydantic import BaseModel

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
}


@celery_app.task(name="demo.add")
def add(x: int, y: int) -> int:
    """Add two numbers.

    Returns the integer sum of ``x`` and ``y``.
    """
    return x + y


class Priority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


@celery_app.task(name="demo.enqueue_job", tags=["jobs"])
def enqueue_job(job: str, priority: Priority = Priority.normal) -> dict:
    """Enqueue a named job at a given priority."""
    return {"job": job, "priority": priority.value, "queued": True}


class Order(BaseModel):
    order_id: str
    items: list[str]
    total: float


@celery_app.task(name="demo.place_order", tags=["orders"])
def place_order(order: Order) -> dict:
    """Place an order (single object argument -> unwrapped request body).

    The ``Order`` annotation drives the unwrapped request-body schema. Celery
    serialises args to dicts over the wire, so ``order`` arrives as a dict here
    (using ``pydantic=True`` to rebuild the model would require pydantic v2).
    """
    return {"order_id": order["order_id"], "item_count": len(order["items"])}


@celery_app.task(name="demo.ping")
def ping() -> str:
    """Health-check task used by the beat schedule."""
    return "pong"
