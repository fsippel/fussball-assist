"""Konstanten der Integration Fußball-Assist."""

from datetime import timedelta

DOMAIN = "fussball_assist"
TOOL_PREFIX = "fussball_assist__"

# Schlüssel der Optionen des Eintrags.
CONF_COMPETITIONS = "wettbewerbe"
CONF_MAIN_TEAM = "hauptverein"
CONF_FAVORITE_TEAMS = "lieblingsvereine"

# Kennungen der sichtbaren Meldungen (Reparaturen) in Home Assistant.
ISSUE_SEASON_MISSING = "saison_fehlt"
ISSUE_SHORTCUT_UNKNOWN = "kuerzel_unbekannt"

API_BASE = "https://api.openligadb.de"
REQUEST_TIMEOUT = 10
MAX_REQUESTS_PER_MINUTE = 10

# Mindestabstände der Abrufe, aus Rücksicht auf die ehrenamtlich betriebene Quelle; strenger ist erlaubt, lockerer nicht.
SEASON_LIST_MIN_AGE = timedelta(days=1)
ROUND_MIN_AGE_LIVE = timedelta(seconds=60)
ROUND_MIN_AGE_IDLE = timedelta(minutes=15)
TABLE_MAX_AGE = timedelta(days=1)
# Älter darf der Zwischenspeicher nicht sein, wenn die Quelle nicht antwortet.
STALE_LIMIT = timedelta(hours=48)
# So lange nach dem Anstoß gilt ein nicht beendetes Spiel als laufend.
LIVE_WINDOW = timedelta(hours=3)
# Höchstens so alt dürfen die Spieldaten sein (und nach dem Anstoß abgerufen), damit ein laufendes Spiel einen Spielstand bekommt.
LIVE_SCORE_MAX_AGE = timedelta(minutes=10)
# Sekunden zwischen zwei Abrufen beim Laden im Hintergrund.
WARM_UP_PAUSE = 6.0
# Rückzug nach einem Fehler der Quelle, verdoppelt sich je weiterem Fehler.
RETRY_AFTER_ERROR = timedelta(seconds=60)
RETRY_AFTER_ERROR_MAX = timedelta(minutes=15)
