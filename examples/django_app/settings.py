"""Minimal Django settings for the celery_farm example.

Run it with::

    DJANGO_SETTINGS_MODULE=examples.django_app.settings \\
        uv run django-admin runserver
"""

SECRET_KEY = "celery-farm-example-not-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "examples.django_app.urls"
INSTALLED_APPS: list[str] = []
MIDDLEWARE: list[str] = []
USE_TZ = True
