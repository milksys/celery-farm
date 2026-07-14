"""End-to-end tests for the FastAPI integration using an eager Celery app."""

from __future__ import annotations

import pytest
from celery import Celery
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

# pydantic v2 requires typing_extensions.TypedDict (not typing.TypedDict) on
# Python < 3.12; typing_extensions works on every supported version.
from typing_extensions import TypedDict

from celery_farm import create_beat_app, create_task_app

# Internal router builder used by create_task_app; imported directly to keep the
# route tests below unprefixed (the public mount API is covered by
# test_mountable_task_app).
from celery_farm.integrations.fastapi import _build_task_router


class AccessView(TypedDict):
    apply_id: int
    name: str | None


class Payload(BaseModel):
    a: int
    b: str = "x"


@pytest.fixture
def client() -> TestClient:
    celery_app = Celery("test")
    celery_app.conf.update(
        task_always_eager=True,
        task_store_eager_result=True,
        result_backend="cache+memory://",
    )

    @celery_app.task(name="test.add")
    def add(x: int, y: int) -> int:
        """Add two numbers.

        Returns the integer sum of x and y.
        """
        return x + y

    @celery_app.task(name="test.greet")
    def greet(name: str, excited: bool = False) -> str:
        return f"Hello, {name}{'!' if excited else '.'}"

    @celery_app.task(name="test.tagged", tags=["custom"], deprecated=True)
    def tagged(x: int) -> int:
        return x

    @celery_app.task(name="test.typeddict")
    def with_typeddict(data: AccessView) -> int:
        return data["apply_id"]

    @celery_app.task(name="test.model", pydantic=True)
    def with_model(p: Payload) -> int:
        return p.a * 10

    @celery_app.task(
        name="test.override",
        summary="explicit summary",
        description="explicit description",
    )
    def override(x: int) -> int:
        """This docstring should be ignored."""
        return x

    celery_app.conf.beat_schedule = {
        "ping": {"task": "test.add", "schedule": 30.0, "kwargs": {"x": 1, "y": 2}},
        "inherited": {"task": "test.add", "schedule": 60.0},
        # references test.tagged, which is @task(deprecated=True) -> inherited.
        "deprecated-job": {"task": "test.tagged", "schedule": 90.0},
    }

    app = FastAPI()
    app.include_router(_build_task_router(celery_app))
    app.mount(
        "/beat",
        create_beat_app(
            celery_app,
            beat_meta={
                "ping": {
                    "summary": "Liveness",
                    "description": "custom desc",
                    "tags": ["health"],
                    "deprecated": True,
                    "openapi_extra": {
                        "externalDocs": {"url": "https://example.com/beat"}
                    },
                }
            },
        ),
    )
    return TestClient(app)


def test_mountable_task_app() -> None:
    # create_task_app wraps the router in its own FastAPI app for `mount`.
    celery_app = Celery("mount")
    celery_app.conf.update(
        task_always_eager=True,
        task_store_eager_result=True,
        result_backend="cache+memory://",
    )

    @celery_app.task(name="mnt.add")
    def add(x: int, y: int) -> int:
        return x + y

    root = FastAPI()
    root.mount("/celery", create_task_app(celery_app))
    client = TestClient(root)

    assert client.get("/celery/docs").status_code == 200
    assert "/tasks/mnt.add" in client.get("/celery/openapi.json").json()["paths"]
    r = client.post("/celery/tasks/mnt.add", json={"x": 2, "y": 3}).json()
    assert client.get(f"/celery/results/{r['task_id']}").json()["result"] == 5


def test_finalize_imports_configured_task_modules(tmp_path, monkeypatch) -> None:
    # A task module listed in conf.imports is not registered until something
    # imports it (a worker does at startup; a web process does not). finalize
    # (the default) runs that import so create_task_app exposes the task.
    (tmp_path / "farm_capp.py").write_text(
        "from celery import Celery\n"
        "app = Celery('fin')\n"
        "app.conf.update(task_always_eager=True, imports=['farm_ctasks'])\n"
    )
    (tmp_path / "farm_ctasks.py").write_text(
        "from farm_capp import app\n"
        "@app.task(name='fin.add')\n"
        "def add(x: int, y: int) -> int:\n"
        "    return x + y\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for mod in ("farm_capp", "farm_ctasks"):
        monkeypatch.delitem(__import__("sys").modules, mod, raising=False)

    from farm_capp import app  # type: ignore[import-not-found]

    # The task module hasn't been imported yet -> task absent from the snapshot.
    assert "fin.add" not in app.tasks
    # finalize=False keeps the raw snapshot: the task stays hidden.
    without = create_task_app(app, finalize=False)
    assert (
        "/tasks/fin.add" not in TestClient(without).get("/openapi.json").json()["paths"]
    )

    # The default finalize=True imports conf.imports first, so it's now exposed.
    with_final = create_task_app(app)
    assert "fin.add" in app.tasks
    assert (
        "/tasks/fin.add" in TestClient(with_final).get("/openapi.json").json()["paths"]
    )


def test_mounted_app_dependencies() -> None:
    # dependencies= (e.g. auth) apply to every route in the mounted app.
    from fastapi import Depends, Header, HTTPException

    celery_app = Celery("auth")
    celery_app.conf.update(task_always_eager=True)

    @celery_app.task(name="auth.add")
    def add(x: int, y: int) -> int:
        return x + y

    def require_token(x_token: str = Header(default="")) -> None:
        if x_token != "secret":
            raise HTTPException(status_code=401)

    root = FastAPI()
    root.mount(
        "/celery", create_task_app(celery_app, dependencies=[Depends(require_token)])
    )
    client = TestClient(root)

    assert client.get("/celery/tasks").status_code == 401  # no token
    assert client.get("/celery/tasks", headers={"x-token": "secret"}).status_code == 200


def test_list_tasks(client: TestClient) -> None:
    resp = client.get("/tasks")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {"test.add", "test.greet"} <= names
    assert "celery.chord" not in names  # built-ins excluded


def test_dispatch_and_fetch_result(client: TestClient) -> None:
    resp = client.post("/tasks/test.add", json={"x": 2, "y": 3})
    assert resp.status_code == 200
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] == "SUCCESS"

    result = client.get(f"/results/{task_id}")
    assert result.status_code == 200
    result_body = result.json()
    assert result_body["ready"] is True
    assert result_body["result"] == 5


def test_default_argument_optional(client: TestClient) -> None:
    resp = client.post("/tasks/test.greet", json={"name": "Ada"})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert client.get(f"/results/{task_id}").json()["result"] == "Hello, Ada."


def test_missing_required_field_is_422(client: TestClient) -> None:
    resp = client.post("/tasks/test.add", json={"x": 1})
    assert resp.status_code == 422


def test_openapi_documents_tasks(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/tasks/test.add" in schema["paths"]
    ref = schema["paths"]["/tasks/test.add"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    model_name = ref.split("/")[-1]
    props = schema["components"]["schemas"][model_name]["properties"]
    assert set(props) == {"x", "y"}


def test_docstring_becomes_summary_and_description(client: TestClient) -> None:
    op = client.get("/openapi.json").json()["paths"]["/tasks/test.add"]["post"]
    assert op["summary"] == "Add two numbers."
    assert op["description"].startswith("Returns the integer sum of x and y.")


def test_decorator_overrides_docstring(client: TestClient) -> None:
    op = client.get("/openapi.json").json()["paths"]["/tasks/test.override"]["post"]
    assert op["summary"] == "explicit summary"
    assert op["description"].startswith("explicit description")


def test_per_task_tags_and_deprecated(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    tagged = paths["/tasks/test.tagged"]["post"]
    assert tagged["tags"] == ["custom"]  # replaces the default tag, not merged
    assert tagged["deprecated"] is True
    # A task without explicit tags falls back to the default.
    assert paths["/tasks/test.add"]["post"]["tags"] == ["celery-farm"]


def test_return_type_documented(client: TestClient) -> None:
    tasks = {t["name"]: t for t in client.get("/tasks").json()}
    assert tasks["test.add"]["returns"] == "int"
    # pydantic v1 adds a title to the schema; assert the meaningful part.
    assert tasks["test.add"]["result_schema"]["type"] == "integer"
    # The eventual result type is noted in the POST operation description.
    op = client.get("/openapi.json").json()["paths"]["/tasks/test.add"]["post"]
    assert "Eventual result" in op["description"]
    assert "`int`" in op["description"]


def test_single_object_arg_is_unwrapped(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    # The request body is the object itself, not wrapped under the param name.
    td_ref = schema["paths"]["/tasks/test.typeddict"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"].split("/")[-1]
    assert set(schema["components"]["schemas"][td_ref]["properties"]) == {
        "apply_id",
        "name",
    }

    # And the flat body dispatches correctly (no {"data": {...}} nesting).
    r = client.post("/tasks/test.typeddict", json={"apply_id": 7, "name": None}).json()
    assert client.get(f"/results/{r['task_id']}").json()["result"] == 7

    # Same for a single pydantic-model arg. Celery's pydantic=True model
    # reconstruction requires pydantic v2 AND celery >= 5.6 (earlier celery
    # delivers the argument as a plain dict, so the task can't do `p.a`).
    from celery import __version__ as celery_version

    from celery_farm import _compat

    celery_ver = tuple(int(p) for p in celery_version.split(".")[:2])
    r2 = client.post("/tasks/test.model", json={"a": 5}).json()
    if _compat.PYDANTIC_V2 and celery_ver >= (5, 6):
        assert client.get(f"/results/{r2['task_id']}").json()["result"] == 50


def test_multi_arg_task_stays_wrapped(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    ref = schema["paths"]["/tasks/test.add"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"].split("/")[-1]
    assert set(schema["components"]["schemas"][ref]["properties"]) == {"x", "y"}


def test_beat_has_its_own_swagger(client: TestClient) -> None:
    # Beat lives in a separate sub-app: not in the main OpenAPI...
    main = client.get("/openapi.json").json()
    assert not any("beat" in p for p in main["paths"])
    # ...but served under /beat with its own docs + per-entry operations.
    assert client.get("/beat/docs").status_code == 200
    beat_spec = client.get("/beat/openapi.json").json()
    assert "/schedule" in beat_spec["paths"]
    # Each scheduled entry is its own POST operation (run-now).
    assert "/schedule/ping" in beat_spec["paths"]
    ping_op = beat_spec["paths"]["/schedule/ping"]["post"]
    # beat_meta overrides summary/description/tags/deprecated/openapi_extra.
    assert ping_op["summary"] == "Liveness"
    assert ping_op["description"].startswith("custom desc")
    assert ping_op["tags"] == ["health"]
    assert ping_op["deprecated"] is True
    assert ping_op["externalDocs"]["url"] == "https://example.com/beat"
    # ...while an entry without an override inherits the task's docstring summary.
    inherited_op = beat_spec["paths"]["/schedule/inherited"]["post"]
    assert inherited_op["summary"] == "Add two numbers."
    assert "deprecated" not in inherited_op  # task isn't deprecated -> not set
    # deprecated is inherited from the referenced task (@task(deprecated=True)).
    assert beat_spec["paths"]["/schedule/deprecated-job"]["post"]["deprecated"] is True
    # The read-only list endpoint still shows each entry's detail.
    entries = client.get("/beat/schedule").json()
    ping = next(e for e in entries if e["name"] == "ping")
    assert ping["task"] == "test.add"
    assert ping["kwargs"] == {"x": 1, "y": 2}


def test_beat_run_dispatches(client: TestClient) -> None:
    # The per-entry endpoint is POST (runs the task); there is no GET detail.
    assert client.get("/beat/schedule/ping").status_code == 405
    r = client.post("/beat/schedule/ping")
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    # "ping" -> test.add(x=1, y=2) -> 3, fetched via the task app's /results.
    assert client.get(f"/results/{task_id}").json()["result"] == 3
    # Unknown entry -> 404.
    assert client.post("/beat/schedule/nope").status_code == 404
