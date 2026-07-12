"""Example Flask app wiring celery_farm to an eager Celery app.

Run it with::

    uv run flask --app examples.flask_app.app run

Then open http://localhost:5000/celery/docs for Swagger UI.
"""

from __future__ import annotations

from celery import Celery
from flask import Flask

from celery_farm.integrations.flask import create_blueprint

celery_app = Celery("demo")
# Eager mode runs tasks in-process (no broker needed).
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
    """Add two numbers."""
    return x + y


@celery_app.task(name="demo.legacy_mul", tags=["legacy"])
def legacy_mul(x, y):
    # type: (int, int) -> int
    """Multiply two numbers, typed via a PEP 484 type comment.

    Demonstrates celery_farm's fallback to ``# type:`` comments for tasks that
    have no annotations (e.g. gradually-typed or legacy code).
    """
    return x * y


@celery_app.task(name="demo.ping")
def ping() -> str:
    """Health-check task used by the beat schedule."""
    return "pong"


app = Flask(__name__)
app.register_blueprint(
    create_blueprint(celery_app, title="celery_farm demo"), url_prefix="/celery"
)
