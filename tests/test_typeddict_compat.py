"""Regression: tasks may annotate arguments with a stdlib ``typing.TypedDict``.

pydantic v2 refuses ``typing.TypedDict`` on Python < 3.12 (it wants the
``typing_extensions`` backport). celery_farm transparently rewrites such
TypedDicts so user task code needn't change its imports. These tests use the
stdlib ``typing.TypedDict`` on purpose — that is the exact thing being exercised.
"""

from __future__ import annotations

import warnings
from typing import TypedDict

import pytest
from celery import Celery

from celery_farm import build_openapi
from celery_farm._compat import adapt_type, type_json_schema


class Address(TypedDict):
    city: str


class CreateUser(TypedDict, total=False):
    name: str
    age: int


class Order(TypedDict):
    sku: str
    ship_to: Address


@pytest.fixture
def celery_app() -> Celery:
    app = Celery("td")
    app.conf.update(task_always_eager=True, result_backend="cache+memory://")

    @app.task(name="td.create_user")
    def create_user(payload: CreateUser) -> dict:  # single-object body
        return dict(payload)

    @app.task(name="td.make_order")
    def make_order(order: Order, note: str = "") -> dict:  # wrapped + nested
        return {"order": order, "note": note}

    return app


def _body_schema(spec: dict, path: str) -> dict:
    schema = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def test_stdlib_typeddict_body_schema_is_preserved(celery_app: Celery) -> None:
    """A stdlib TypedDict argument yields a detailed object schema, not a crash."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a graceful-degrade fallback would fail here
        spec = build_openapi(celery_app, title="td", version="1")

    user = _body_schema(spec, "/tasks/td.create_user")
    assert set(user.get("properties", {})) == {"name", "age"}
    # total=False -> every key optional (no "required" list, or an empty one).
    assert not user.get("required")

    order = _body_schema(spec, "/tasks/td.make_order")
    assert {"order", "note"} <= set(order.get("properties", {}))
    # The nested Address TypedDict must survive somewhere in the document.
    assert "city" in __import__("json").dumps(spec)


def test_adapt_type_roundtrips_through_pydantic() -> None:
    """adapt_type output is always schema-able by the active pydantic."""
    schema = type_json_schema(adapt_type(Order))
    dumped = __import__("json").dumps(schema)
    assert "sku" in dumped and "city" in dumped


def test_adapt_type_is_identity_for_plain_types() -> None:
    assert adapt_type(int) is int
    assert adapt_type(list[int]) == list[int]
