# app/version.py
"""
Zentrale Versionsverwaltung für Noria.

Single Source of Truth für die App-Version.
Wird von main.py (FastAPI-App-Metadaten) und routes_health.py (/health)
importiert. Versionierung folgt Semantic Versioning (semver.org).
"""

APP_NAME = "Noria"

# SemVer: MAJOR.MINOR.PATCH
__version_info__: tuple[int, int, int] = (0, 12, 0)
__version__: str = ".".join(str(x) for x in __version_info__)
