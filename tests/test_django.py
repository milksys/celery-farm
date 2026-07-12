"""End-to-end tests for the pure-Django integration using an eager Celery app.

Django settings are configured manually (no pytest-django), pointing ROOT_URLCONF
at the example app's urls module.
"""

from __future__ import annotations

import django
import pytest
from django.conf import settings
from django.test import Client


@pytest.fixture(scope="module", autouse=True)
def _django_setup():
    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY="test",
            ALLOWED_HOSTS=["*"],
            ROOT_URLCONF="examples.django_app.urls",
            INSTALLED_APPS=[],
            MIDDLEWARE=[],
            USE_TZ=True,
        )
        django.setup()


@pytest.fixture
def client() -> Client:
    return Client()


def test_list_tasks(client: Client) -> None:
    resp = client.get("/celery/tasks")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {"demo.add", "demo.enqueue_job", "demo.place_order"} <= names


def test_dispatch_and_fetch_result(client: Client) -> None:
    resp = client.post(
        "/celery/tasks/demo.add",
        data={"x": 2, "y": 3},
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"

    result = client.get(f"/celery/results/{body['task_id']}")
    assert result.json()["result"] == 5


def test_single_object_arg_is_unwrapped(client: Client) -> None:
    # Body is the Order object directly, not wrapped under "order".
    resp = client.post(
        "/celery/tasks/demo.place_order",
        data={"order_id": "A1", "items": ["x", "y"], "total": 9.9},
        content_type="application/json",
    )
    assert resp.status_code == 200
    result = client.get(f"/celery/results/{resp.json()['task_id']}")
    assert result.json()["result"] == {"order_id": "A1", "item_count": 2}


def test_validation_error_is_422(client: Client) -> None:
    resp = client.post(
        "/celery/tasks/demo.add",
        data={"x": 1},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_unknown_task_is_404(client: Client) -> None:
    resp = client.post(
        "/celery/tasks/demo.nope", data={}, content_type="application/json"
    )
    assert resp.status_code == 404


def test_openapi_document(client: Client) -> None:
    from celery_farm import _compat

    doc = client.get("/celery/openapi.json").json()
    # 3.1 under pydantic v2, 3.0 under v1 (matching the JSON Schema dialect).
    assert doc["openapi"] == _compat.OPENAPI_VERSION
    assert "/tasks/demo.add" in doc["paths"]
    assert "TaskDispatchResponse" in doc["components"]["schemas"]
    # servers reflects the include() prefix so "Try it out" hits the right URL.
    assert doc["servers"] == [{"url": "/celery"}]
    # beat is NOT in the main Swagger (it has its own).
    assert "/beat" not in doc["paths"]


def test_beat_has_its_own_swagger(client: Client) -> None:
    assert client.get("/celery/beat/docs").status_code == 200
    beat_doc = client.get("/celery/beat/openapi.json").json()
    assert "/schedule" in beat_doc["paths"]
    # Each scheduled entry is a separate operation.
    assert "/schedule/ping-every-30s" in beat_doc["paths"]
    assert "/tasks/demo.add" not in beat_doc["paths"]
    # servers points at ".../beat" so entry paths resolve correctly.
    assert beat_doc["servers"] == [{"url": "/celery/beat"}]


def test_beat_schedule(client: Client) -> None:
    # The read-only list endpoint carries each entry's detail.
    entries = client.get("/celery/beat/schedule").json()
    assert entries[0]["name"] == "ping-every-30s"
    assert entries[0]["task"] == "demo.ping"


def test_beat_run_dispatches(client: Client) -> None:
    # Per-entry endpoint is POST (runs the task); no GET detail.
    beat_doc = client.get("/celery/beat/openapi.json").json()
    assert "post" in beat_doc["paths"]["/schedule/ping-every-30s"]
    # POST dispatches the entry's task -> demo.ping() -> "pong".
    resp = client.post("/celery/beat/schedule/ping-every-30s")
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert client.get(f"/celery/results/{task_id}").json()["result"] == "pong"
    # Unknown entry -> 404; GET on the entry path is rejected.
    assert client.post("/celery/beat/schedule/nope").status_code == 404
    assert client.get("/celery/beat/schedule/ping-every-30s").status_code == 405


def test_swagger_docs(client: Client) -> None:
    resp = client.get("/celery/docs")
    assert resp.status_code == 200
    assert b"swagger-ui" in resp.content
