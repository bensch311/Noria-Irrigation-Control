# app/version.py
"""
Zentrale Versionsinformation für Noria.

Diese Datei ist die einzige Quelle der Wahrheit für die Versionsnummer.
Alle anderen Module importieren von hier – niemals hartcodierte Strings.

Versionsschema: Semantic Versioning (SemVer) MAJOR.MINOR.PATCH
  MAJOR – Breaking Changes (inkompatibles Datenformat, API-Bruch)
  MINOR – Neue Features (rückwärtskompatibel)
  PATCH – Bugfixes (keine neuen Features)

Vor jedem Release:
  1. __version__ und __version_info__ hier aktualisieren
  2. CHANGELOG.md (docs/) mit Einträgen ergänzen
  3. git commit -m "chore: bump version to X.Y.Z"
  4. git tag -a vX.Y.Z -m "Noria X.Y.Z"
  5. git push && git push --tags
"""

APP_NAME = "Noria"

# Versionstupel für programmatische Vergleiche (z.B. >= (1, 1, 0))
__version_info__: tuple[int, int, int] = (0, 10, 2)

# Versionsstring für Anzeige, Logging und API-Responses
__version__: str = ".".join(str(x) for x in __version_info__)
