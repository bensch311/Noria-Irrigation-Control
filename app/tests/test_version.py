# tests/test_version.py
"""
Tests für app/version.py und die Integration in den Health-Endpunkt.

Getestet werden:
  - Korrekte Typen und Format von __version__ und __version_info__
  - Konsistenz zwischen String und Tupel
  - APP_NAME nicht leer
  - Health-Endpoint liefert app_version als SemVer-String
"""

import re

import pytest

from version import APP_NAME, __version__, __version_info__


# ─────────────────────────────────────────────────────────────────────────────
# version.py – Einheitentests
# ─────────────────────────────────────────────────────────────────────────────

def test_version_string_is_str():
    assert isinstance(__version__, str)


def test_version_info_is_tuple_of_three_ints():
    assert isinstance(__version_info__, tuple)
    assert len(__version_info__) == 3
    assert all(isinstance(x, int) for x in __version_info__)


def test_version_string_matches_semver_format():
    """__version__ muss dem Format MAJOR.MINOR.PATCH entsprechen."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        f"__version__ '{__version__}' entspricht nicht dem SemVer-Format X.Y.Z"
    )


def test_version_string_consistent_with_version_info():
    """__version__ muss exakt aus __version_info__ ableitbar sein."""
    expected = ".".join(str(x) for x in __version_info__)
    assert __version__ == expected, (
        f"__version__ '{__version__}' stimmt nicht mit "
        f"__version_info__ {__version_info__} überein"
    )


def test_app_name_is_nonempty_string():
    assert isinstance(APP_NAME, str)
    assert len(APP_NAME) > 0


def test_version_parts_non_negative():
    assert all(x >= 0 for x in __version_info__)


# ─────────────────────────────────────────────────────────────────────────────
# Integration: Health-Endpoint liefert app_version
# ─────────────────────────────────────────────────────────────────────────────

def test_health_contains_app_version(client):
    """GET /health muss das Feld 'app_version' mit dem aktuellen Versionsstring liefern."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "app_version" in data, "Feld 'app_version' fehlt in /health Response"


def test_health_app_version_matches_version_module(client):
    """Der app_version-Wert in /health muss exakt __version__ aus version.py entsprechen."""
    resp = client.get("/health")
    assert resp.json()["app_version"] == __version__


def test_health_app_version_is_semver(client):
    """app_version in /health muss SemVer-Format haben."""
    app_version = client.get("/health").json()["app_version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", app_version), (
        f"app_version '{app_version}' in /health entspricht nicht dem SemVer-Format"
    )


def test_health_api_version_field_unchanged(client):
    """Das bestehende 'version'-Feld (API-Version, Integer) darf nicht verändert worden sein."""
    data = client.get("/health").json()
    assert "version" in data
    assert isinstance(data["version"], int)
    assert data["version"] == 1
