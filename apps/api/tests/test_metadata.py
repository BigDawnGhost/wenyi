"""API metadata follows the installed distribution and application settings."""

import runpy
from unittest.mock import patch

import wenyi_api
from wenyi_api import main


def test_package_version_reads_installed_distribution():
    assert wenyi_api.__file__ is not None
    with patch("importlib.metadata.version", return_value="0.9.0.dev3") as version:
        namespace = runpy.run_path(wenyi_api.__file__)
    assert namespace["__version__"] == "0.9.0.dev3"
    version.assert_called_once_with("wenyi-api")


def test_new_app_uses_package_version(monkeypatch):
    monkeypatch.setattr(main, "__version__", "0.9.0.dev3", raising=False)
    application = main.create_app()
    assert application.version == "0.9.0.dev3"
    assert application.openapi()["info"]["version"] == application.version


def test_openapi_uses_application_metadata():
    application = main.create_app()
    application.title = "Wenyi API metadata fixture"
    application.version = "0.9.1"
    info = application.openapi()["info"]
    assert info["title"] == application.title
    assert info["version"] == application.version
    assert info["description"] == application.description
