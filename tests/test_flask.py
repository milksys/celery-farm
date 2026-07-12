"""End-to-end tests for the Flask integration using an eager Celery app.

Also covers the PEP 484 type-comment introspection fallback.
"""

from __future__ import annotations

import pytest
from celery import Celery
from flask import Flask

from celery_farm.integrations.flask import create_blueprint


@pytest.fixture
def client():
    celery_app = Celery("test")
    celery_app.conf.update(
        task_always_eager=True,
        task_store_eager_result=True,
        result_backend="cache+memory://",
    )

    @celery_app.task(name="flasktest.add")
    def add(x: int, y: int) -> int:
        return x + y

    @celery_app.task(name="flasktest.legacy")
    def legacy(x, y):
        # type: (int, int) -> int
        return x * y

    celery_app.conf.beat_schedule = {
        "ping": {"task": "flasktest.add", "schedule": 30.0, "kwargs": {"x": 2, "y": 5}},
    }

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(celery_app), url_prefix="/celery")
    return app.test_client()


def test_list_tasks(client) -> None:
    resp = client.get("/celery/tasks")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.get_json()}
    assert {"flasktest.add", "flasktest.legacy"} <= names


def test_dispatch_and_fetch_result(client) -> None:
    resp = client.post("/celery/tasks/flasktest.add", json={"x": 2, "y": 3})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "SUCCESS"
    result = client.get(f"/celery/results/{body['task_id']}")
    assert result.get_json()["result"] == 5


def test_type_comment_fallback(client) -> None:
    # The task is typed only via a `# type:` comment, yet its params are typed
    # in the schema and validation still works.
    tasks = {t["name"]: t for t in client.get("/celery/tasks").get_json()}
    params = {p["name"]: p["annotation"] for p in tasks["flasktest.legacy"]["params"]}
    assert params == {"x": "int", "y": "int"}
    assert tasks["flasktest.legacy"]["returns"] == "int"

    # A valid call works...
    r = client.post("/celery/tasks/flasktest.legacy", json={"x": 4, "y": 5})
    assert (
        client.get(f"/celery/results/{r.get_json()['task_id']}").get_json()["result"]
        == 20
    )
    # ...and a type error is rejected (proves the comment type is enforced).
    bad = client.post("/celery/tasks/flasktest.legacy", json={"x": "nope", "y": 5})
    assert bad.status_code == 422


def test_validation_error_is_422(client) -> None:
    resp = client.post("/celery/tasks/flasktest.add", json={"x": 1})
    assert resp.status_code == 422


def test_unknown_task_is_404(client) -> None:
    resp = client.post("/celery/tasks/test.nope", json={})
    assert resp.status_code == 404


def test_openapi_and_docs(client) -> None:
    from celery_farm import _compat

    doc = client.get("/celery/openapi.json").get_json()
    assert doc["openapi"] == _compat.OPENAPI_VERSION
    assert "/tasks/flasktest.add" in doc["paths"]
    assert doc["servers"] == [{"url": "/celery"}]
    assert "/beat" not in doc["paths"]  # beat has its own Swagger

    docs = client.get("/celery/docs")
    assert docs.status_code == 200
    assert b"swagger-ui" in docs.data


def test_beat_has_its_own_swagger(client) -> None:
    assert client.get("/celery/beat/docs").status_code == 200
    beat_doc = client.get("/celery/beat/openapi.json").get_json()
    assert "/schedule" in beat_doc["paths"]
    assert "/schedule/ping" in beat_doc["paths"]  # per-entry operation
    assert "/tasks/flasktest.add" not in beat_doc["paths"]
    assert beat_doc["servers"] == [{"url": "/celery/beat"}]


def test_beat_schedule(client) -> None:
    # The read-only list endpoint carries each entry's detail.
    entries = client.get("/celery/beat/schedule").get_json()
    assert entries[0]["name"] == "ping"
    assert entries[0]["task"] == "flasktest.add"


def test_beat_run_dispatches(client) -> None:
    # Per-entry endpoint is POST (runs the task); no GET detail.
    beat_doc = client.get("/celery/beat/openapi.json").get_json()
    assert "post" in beat_doc["paths"]["/schedule/ping"]
    # POST dispatches the entry's task with its configured kwargs -> 2 + 5 = 7.
    resp = client.post("/celery/beat/schedule/ping")
    assert resp.status_code == 200
    task_id = resp.get_json()["task_id"]
    assert client.get(f"/celery/results/{task_id}").get_json()["result"] == 7
    # Unknown entry -> 404; GET on the entry path is rejected.
    assert client.post("/celery/beat/schedule/nope").status_code == 404
    assert client.get("/celery/beat/schedule/ping").status_code == 405
