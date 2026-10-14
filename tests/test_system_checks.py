"""Django system checks must pass for the app's models.

``makemigrations --check`` in CI runs with ``--skip-checks`` and pytest-django
does not run system checks, so without this a model error (e.g. an index name
over 30 characters, models.E034) only surfaces in a consumer's
``manage.py check``.  CI also runs ``manage.py check`` on the demo project.
"""

import pytest
from django.apps import apps
from django.core import checks


@pytest.mark.django_db
def test_system_checks_pass():
    # Only issues raised against this app's objects; project-level checks
    # (e.g. admin.E403 about TEMPLATES in the minimal test settings) belong
    # to the host project and are covered by ``manage.py check`` on the demo.
    issues = checks.run_checks(
        app_configs=[apps.get_app_config("healthdatamodel")], databases=["default"]
    )
    ours = [
        issue
        for issue in issues
        if getattr(getattr(issue.obj, "_meta", None), "app_label", None)
        == "healthdatamodel"
    ]
    assert [str(issue) for issue in ours] == []


def test_index_names_fit_portable_limit():
    # models.E034 — Django caps index names at 30 characters for portability.
    for model in apps.get_app_config("healthdatamodel").get_models():
        for index in model._meta.indexes:
            assert len(index.name) <= 30, (model.__name__, index.name)
