# core/limiter.py
"""
Rate-Limiting-Konfiguration für das Bewässerungs-Backend.

Design:
- Ein Limiter-Singleton, der von Main und allen Route-Modulen importiert wird.
- Globales Limit (120/min): via SlowAPIMiddleware auf alle Routen.
- Mutations-Limit (30/min): via @limiter.limit(MUTATION_LIMIT) auf POST/DELETE-Routen.
- key_func: get_remote_address → Limits gelten pro Client-IP.

Wichtig für Tests:
- Das conftest.py-app-Fixture erstellt pro Test eine NEUE Limiter-Instanz
  und setzt diese als app.state.limiter – damit ist die Storage pro Test
  isoliert und sauber, ohne Akkumulation über mehrere Tests.
- Der Modul-Level-Limiter hier wird ausschließlich für die @limiter.limit()-
  Dekoratoren verwendet (annotiert nur die Route-Funktionen mit _rate_limit_info).
  Die eigentliche Enforcement geht über request.app.state.limiter.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

# Limit-Konstanten – zentral definiert, damit Änderungen an einer Stelle wirken.
GLOBAL_LIMIT = "120/minute"
MUTATION_LIMIT = "30/minute"

# Singleton für Dekoratoren in den Route-Modulen.
# Wird in main.py auch als app.state.limiter gesetzt (Produktion).
# In Tests wird ein frischer Limiter pro Test-App verwendet.
limiter = Limiter(key_func=get_remote_address, default_limits=[GLOBAL_LIMIT])
