"""Deutsche Zeitangaben, Zeiträume und Platzhalter-Uhrzeiten (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
import re

from .models import Match

PERIODS = ("heute", "gestern", "morgen", "letztes_wochenende", "kommendes_wochenende")

# Eigene Listen, damit die Ausgabe nicht von der Sprache des Systems abhängt.
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)  # fmt: skip

_FRIDAY = 4
_DAY_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# time_confirmed: Schwellen
_CONFIRMED_WITHIN = timedelta(days=14)
_STALE_UPDATE = timedelta(days=90)
_PLACEHOLDER_HORIZON = timedelta(days=28)
_PLACEHOLDER_MIN_MATCHES = 4


def day_phrase(day: date, *, current_year: int | None = None) -> str:
    """Zum Beispiel "Samstag, 21. November"; mit `current_year` steht ein anderes Jahr dabei ("… 2025")."""
    year = f" {day.year}" if current_year is not None and day.year != current_year else ""
    return f"{WEEKDAYS[day.weekday()]}, {day.day}. {MONTHS[day.month - 1]}{year}"


def range_phrase(first: date, last: date, *, current_year: int | None = None) -> str:
    """Ein Tag oder "Samstag, 21. November bis Sonntag, 22. November" (Jahr wie bei `day_phrase`)."""
    start, end = (day_phrase(d, current_year=current_year) for d in (first, last))
    return start if first == last else f"{start} bis {end}"


def clock_phrase(moment: datetime, tz: tzinfo) -> str:
    """Uhrzeit in der Ortszeit, zum Beispiel "16:12 Uhr"."""
    return f"{moment.astimezone(tz):%H:%M} Uhr"


def when_phrase(moment: datetime, tz: tzinfo, *, with_time: bool, current_year: int | None = None) -> str:
    """Zum Beispiel "Samstag, 21. November, 13:00 Uhr" (Tag und Uhrzeit in Ortszeit, Jahr wie bei `day_phrase`)."""
    text = day_phrase(local_day(moment, tz), current_year=current_year)
    return f"{text}, {clock_phrase(moment, tz)}" if with_time else text


def local_day(moment: datetime, tz: tzinfo) -> date:
    """Kalendertag des Zeitpunkts in der Ortszeit (nicht in UTC)."""
    return moment.astimezone(tz).date()


def relative_phrase(day: date, today: date) -> str:
    """Abstand eines Tages zu heute in Worten, zum Beispiel "morgen" oder "vor 9 Tagen"."""
    delta = (day - today).days
    named = {0: "heute", 1: "morgen", 2: "übermorgen", -1: "gestern", -2: "vorgestern"}
    if delta in named:
        return named[delta]
    return f"in {delta} Tagen" if delta > 0 else f"vor {-delta} Tagen"


def _unit(count: int, singular: str, plural: str) -> str:
    return f"in {count} {singular if count == 1 else plural}"


def countdown_phrase(moment: datetime, now: datetime) -> str:
    """Restzeit bis zum Zeitpunkt, abgerundet auf Tage, Stunden oder Minuten.

    Liegt der Zeitpunkt in der Vergangenheit oder weniger als eine Minute voraus,
    lautet die Antwort "jetzt".
    """
    # Nach UTC normalisieren: gleiche tzinfo-Objekte würden sonst Wandzeit subtrahieren.
    seconds = int((moment.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
    if seconds < 60:
        return "jetzt"
    if seconds < 3600:
        return _unit(seconds // 60, "Minute", "Minuten")
    if seconds < 86400:
        return _unit(seconds // 3600, "Stunde", "Stunden")
    return _unit(seconds // 86400, "Tag", "Tagen")


def resolve_period(period: str, now: datetime, tz: tzinfo) -> tuple[date, date]:
    """Erster und letzter Tag (beide eingeschlossen) eines Zeitraums in Ortszeit.

    Ein Wochenende reicht von Freitag bis Sonntag. Von Montag bis Donnerstag ist das
    kommende Wochenende das nächste, von Freitag bis Sonntag das laufende.
    """
    today = local_day(now, tz)
    if period == "heute":
        return today, today
    if period == "gestern":
        day = today - timedelta(days=1)
        return day, day
    if period == "morgen":
        day = today + timedelta(days=1)
        return day, day
    if period in ("kommendes_wochenende", "letztes_wochenende"):
        coming = today + timedelta(days=(_FRIDAY - today.weekday()) % 7)
        if today.weekday() > _FRIDAY:  # Samstag, Sonntag: das laufende Wochenende
            coming = today - timedelta(days=today.weekday() - _FRIDAY)
        start = coming if period == "kommendes_wochenende" else coming - timedelta(days=7)
        return start, start + timedelta(days=2)
    raise ValueError(f"Unbekannter Zeitraum {period!r}. Gültig sind: {', '.join(PERIODS)}.")


def parse_day(text: str) -> date | None:
    """Liest streng "JJJJ-MM-TT"; alles andere ergibt None."""
    if not isinstance(text, str) or not _DAY_PATTERN.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def time_confirmed(match: Match, same_round: Sequence[Match], now: datetime) -> bool:
    """Ist die Anstoßzeit fest oder nur ein Platzhalter der Liga?

    Regeln in dieser Reihenfolge:
    1. Anstoß in höchstens 14 Tagen: fest.
    2. Letzte Änderung fehlt oder liegt mehr als 90 Tage vor dem Anstoß: nicht fest.
    3. Mindestens vier Spiele derselben Runde haben genau dieselbe Anstoßzeit und der
       Anstoß liegt mehr als 28 Tage entfernt: nicht fest.
    4. Sonst fest.
    """
    kickoff = match.kickoff.astimezone(UTC)  # UTC-Differenzen, nicht Wandzeit
    distance = kickoff - now.astimezone(UTC)
    if distance <= _CONFIRMED_WITHIN:
        return True
    if match.last_update is None or kickoff - match.last_update.astimezone(UTC) > _STALE_UPDATE:
        return False
    same_time = sum(1 for other in same_round if other.kickoff == kickoff)
    return not (same_time >= _PLACEHOLDER_MIN_MATCHES and distance > _PLACEHOLDER_HORIZON)
