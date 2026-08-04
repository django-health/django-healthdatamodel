"""Test settings for running the suite against PostgreSQL.

Used by the ``test-postgres`` CI job (and locally via
``pytest --ds=tests.settings_postgres``) to exercise the legacy activity
read path, which needs PostgreSQL and is skipped on SQLite.
"""

import os

from tests.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "healthdatamodel"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}
