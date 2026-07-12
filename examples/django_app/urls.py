"""URL config mounting celery_farm under /celery/."""

from __future__ import annotations

from django.urls import include, path

from celery_farm.integrations.django import get_urlpatterns

from .celery_app import celery_app

urlpatterns = [
    path("celery/", include(get_urlpatterns(celery_app, title="celery_farm demo"))),
]
