"""Bausteine für die Antworten aller Werkzeuge (ohne Home-Assistant-Bezug).

Ergebnisse sind flache, kleine Wörterbücher ohne Kennungen, Links und Wappen. Fehlt ein Feld,
erklärt ein ausgeschriebener Satz in `hinweise` den Grund. Jedes Ergebnis mit Daten trägt
`stand`; Rückfragen (`question_payload`) tragen keinen, weil sie keine Daten enthalten.
Fehler werden als `ToolError` geworfen und erst im Werkzeug in `error_payload` verwandelt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
import re
from typing import Any

from . import const
from .models import Goal, Match, Phase, Score, TableRow, Team
from .seasons import Competition, Kind
from .store import FINISHED, LIVE, NOT_FINISHED, SCHEDULED, Snapshot, match_state
from .teams import Resolution, TeamIndex, normalize
from .timeutil import (
    MONTHS,
    clock_phrase,
    countdown_phrase,
    local_day,
    relative_phrase,
    time_confirmed,
    when_phrase,
)

MAX_CANDIDATES = 5
DEFAULT_LEAGUE = "bundesliga"

STATE_TEXT = {
    SCHEDULED: "angesetzt",
    LIVE: "läuft",
    FINISHED: "beendet",
    NOT_FINISHED: "laut Daten noch nicht beendet",
}

_EXTRA_TIME_TEXT = "nach Verlängerung"
_PENALTIES_TEXT = "im Elfmeterschießen"
_COUNTDOWN_LIMIT = timedelta(hours=24)
_ROUND_SPACE = re.compile(r"^([0-9]+)\.(?=\S)")
_FINAL_PREFIXES = ("finale", "endspiel")

STARTED = "begonnen"  # Vermerk zu `round_matches`: die Runde läuft schon, es fehlen die schon gespielten Spiele
UNFINISHED = "unvollständig"  # Vermerk zu `round_matches`: von der letzten Runde fehlen noch nicht beendete Spiele
CATCH_UP = "nachgeholt"  # Vermerk zu `round_matches`: verlegte Spiele einer früheren Runde

NOT_UNIQUE = "Das Ergebnis ist in den Daten nicht eindeutig."
NO_GOALS_WHEN_UNCLEAR = "Wegen des unklaren Ergebnisses nenne ich auch keine Tore."
LIVE_LAG = "Der Stand kann einige Minuten hinterherhängen."
TIME_NOT_FIXED = "Uhrzeit noch nicht fest: Bei diesen Spielen ist nur der Tag sicher."
SCORERS_MISSING = "Torschützen nicht eingetragen: Für einige Spiele fehlen sie in den Daten."
NOT_FINISHED_NOTE = "Ein Spiel ist laut Daten noch nicht beendet; sein Endergebnis ist noch nicht eingetragen."


class ErrorKind(StrEnum):
    """Unterscheidbare Fehlerarten; der Wert steht im Feld `art` des Fehlerergebnisses."""

    UNREACHABLE = "quelle_nicht_erreichbar"
    UNSUPPORTED = "nicht_unterstuetzt"
    UNKNOWN_TEAM = "verein_unbekannt"
    NOTHING_ENTERED = "nichts_eingetragen"
    SEASON_MISSING = "saison_fehlt"
    INVALID_INPUT = "eingabe_ungueltig"


@dataclass(frozen=True, slots=True)
class Settings:
    """Einstellungen der Integration, soweit die Antworten sie brauchen."""

    competitions: tuple[str, ...]
    main_team: int | None
    favorite_teams: tuple[int, ...]


class ToolError(Exception):
    """Fehler eines Werkzeugs mit Art und ganzem deutschem Satz; das Werkzeug gibt ihn als Daten zurück."""

    def __init__(self, kind: ErrorKind, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


def error_payload(kind: ErrorKind, text: str) -> dict[str, Any]:
    """Fehlerergebnis: der Satz steht in den Daten, die Art daneben."""
    return {"fehler": text, "art": kind.value}


def question_payload(text: str, candidates: Sequence[str]) -> dict[str, Any]:
    """Rückfrage mit höchstens fünf Kandidaten; bei mehr bittet der Text um den genauen Namen."""
    names = list(candidates)
    if len(names) > MAX_CANDIDATES:
        names = names[:MAX_CANDIDATES]
        text = "Ich kenne mehrere Vereine mit diesem Namen. Sag bitte den genauen Namen."
    return {"rueckfrage": text, "kandidaten": names}


def season_label(year: int) -> str:
    """Zum Beispiel 2026 -> "2026/27"."""
    return f"{year}/{(year + 1) % 100:02d}"


def join_names(names: Iterable[str]) -> str:
    """"A", "A und B", "A, B und C"."""
    items = list(names)
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} und {items[-1]}"


def unique(items: Iterable[str]) -> list[str]:
    """Reihenfolge behalten, Wiederholungen entfernen (Hinweise erscheinen einmal je Ergebnis)."""
    return list(dict.fromkeys(items))


def given(value: Any) -> Any:
    """Leere Zeichenketten und None gelten als nicht angegeben; Zeichenketten werden von Leerraum befreit."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value.strip() if isinstance(value, str) else value


def format_score(score: Score) -> str:
    return f"{score.home}:{score.away}"


def _moment_phrase(snapshot: Snapshot, moment: datetime) -> str:
    """Zum Beispiel "17. November, 16:12 Uhr"; das Jahr nur, wenn es nicht das laufende ist."""
    day, today = local_day(moment, snapshot.tz), local_day(snapshot.now, snapshot.tz)
    year = f" {day.year}" if day.year != today.year else ""
    return f"{day.day}. {MONTHS[day.month - 1]}{year}, {clock_phrase(moment, snapshot.tz)}"


def stand_phrase(
    snapshot: Snapshot, competitions: Iterable[str] = (), *, tables: Iterable[str] = (), scorers: Iterable[str] = ()
) -> str:
    """Der älteste Abruf unter den Daten, auf denen die Antwort beruht, zum Beispiel "17. November, 16:12 Uhr".

    `competitions` sind die Wettbewerbe, deren Spieldaten die Antwort nutzt (`fetched_at`; bei laufenden Spielen
    der älteste Abruf ihrer Runden), `tables` und `scorers` die, deren Tabelle oder Torjägerliste sie nutzt
    (`tables_at`, `scorers_at`). Jede Art hat ihren eigenen Abruf: Eine frisch geladene Spielliste macht eine
    alte Tabelle nicht neuer. Ohne bekannten Abruf gilt der Zeitpunkt der Momentaufnahme.
    """
    stamps = [snapshot.fetched_at[key] for key in competitions if key in snapshot.fetched_at]
    stamps += [snapshot.tables_at[key] for key in tables if key in snapshot.tables_at]
    stamps += [snapshot.scorers_at[key] for key in scorers if key in snapshot.scorers_at]
    return _moment_phrase(snapshot, min(stamps) if stamps else snapshot.now)


def table_stale_hints(snapshot: Snapshot, keys: Iterable[str]) -> list[str]:
    """Je Wettbewerb, dessen Tabelle älter ist als seine Spieldaten, ein Satz dazu."""
    return [
        f"Seit diesem Stand der Tabelle von {_name(snapshot, key)} haben sich die Spieldaten geändert, vielleicht wurden"
        " seither Spiele gespielt; die Tabelle enthält das noch nicht."
        for key in keys
        if key in snapshot.stale_tables
    ]


def scorers_stale_hints(snapshot: Snapshot, keys: Iterable[str]) -> list[str]:
    """Je Wettbewerb, dessen Torjägerliste älter ist als seine Spieldaten, ein Satz dazu."""
    return [
        f"Seit diesem Stand der Torjägerliste von {_name(snapshot, key)} haben sich die Spieldaten geändert, vielleicht"
        " wurden seither Tore geschossen; die Liste enthält das noch nicht."
        for key in keys
        if key in snapshot.stale_scorers
    ]


def _is_invalid(snapshot: Snapshot, key: str) -> bool:
    return any(s in snapshot.invalid_shortcuts for s in snapshot.season.competitions[key].shortcuts)


def _name(snapshot: Snapshot, key: str) -> str:
    return snapshot.season.competitions[key].name


def available(snapshot: Snapshot, competitions: Iterable[str]) -> tuple[str, ...]:
    """Die Wettbewerbe mit vollständig brauchbaren Daten: geladen, erreichbar und mit gültigem Kürzel."""
    return tuple(
        k for k in competitions if k in snapshot.loaded and k not in snapshot.unreachable and not _is_invalid(snapshot, k)
    )


def guard(snapshot: Snapshot, competitions: Sequence[str]) -> tuple[str, ...]:
    """Die brauchbaren unter den Wettbewerben (Reihenfolge bleibt).

    Wettbewerbe mit ungültigem Kürzel und nicht erreichbare Wettbewerbe fallen aus der Antwort;
    `loading_hints` nennt sie. Geworfen wird nur, wenn keiner brauchbar ist: `SEASON_MISSING`,
    wenn alle ungültig sind, sonst `UNREACHABLE`.
    """
    if not competitions:
        raise ToolError(ErrorKind.UNSUPPORTED, "Es sind keine Wettbewerbe ausgewählt, deshalb kann ich nichts nachsehen.")
    usable = tuple(k for k in competitions if not _is_invalid(snapshot, k) and k not in snapshot.unreachable)
    if usable:
        return usable
    if all(_is_invalid(snapshot, k) for k in competitions):
        raise ToolError(
            ErrorKind.SEASON_MISSING,
            "Für die neue Saison fehlen noch Daten, deshalb kann ich dazu gerade nichts sagen.",
        )
    raise ToolError(
        ErrorKind.UNREACHABLE,
        "Die Fußballdaten sind gerade nicht erreichbar. Versuch es bitte später noch einmal.",
    )


def loading_error(snapshot: Snapshot, keys: Sequence[str], who: str) -> ToolError | None:
    """Fehler, wenn die Wettbewerbe `keys` nicht vollständig brauchbar sind (sonst None).

    Solange Daten fehlen, darf nichts als "nicht vorhanden" gelten: `who` (zum Beispiel "die Spiele"
    oder "„Köln“") könnte in einem nicht erreichbaren, ungültigen (auch wenn schon geladen) oder noch
    nicht geladenen Wettbewerb stecken. Noch nicht geladen ist vorübergehend und heißt wie
    "nicht erreichbar" (`UNREACHABLE`): gleich noch einmal versuchen.
    """
    names = lambda selected: join_names(_name(snapshot, k) for k in selected)  # noqa: E731
    unreachable = [k for k in keys if k in snapshot.unreachable]
    if unreachable:
        return ToolError(
            ErrorKind.UNREACHABLE,
            f"Ich kann {who} gerade nicht suchen, weil die Daten für {names(unreachable)} nicht erreichbar sind."
            " Versuch es bitte später noch einmal.",
        )
    invalid = [k for k in keys if _is_invalid(snapshot, k)]
    if invalid:
        return ToolError(
            ErrorKind.SEASON_MISSING,
            f"Für {names(invalid)} fehlen noch Daten für die neue Saison, deshalb kann ich {who} nicht sicher finden.",
        )
    waiting = [k for k in keys if k not in snapshot.loaded]
    if waiting:
        return ToolError(
            ErrorKind.UNREACHABLE,
            f"Ich finde {who} noch nicht, weil noch nicht alle Wettbewerbe geladen sind (es fehlen: {names(waiting)})."
            " Versuch es gleich noch einmal.",
        )
    return None


def searchable(snapshot: Snapshot, settings: Settings) -> tuple[str, ...]:
    """Die Wettbewerbe, in denen ein Vereinsname vorkommen kann: alle gewählten ohne Frauen und Nationalmannschaft.

    Ob ein Verein auffindbar ist, hängt von allen davon ab, nicht von einem eingeengten Wettbewerb.
    """
    competitions = snapshot.season.competitions
    return tuple(
        k
        for k in settings.competitions
        if k in competitions and not competitions[k].women and competitions[k].kind is not Kind.NATIONAL
    )


def ambiguous_payload(resolution: Resolution, text: str) -> dict[str, Any]:
    """Rückfrage bei einem Namen, der zu mehreren Vereinen passt."""
    return question_payload(
        f"Ich kenne mehrere Vereine mit „{text}“. Welchen meinst du?",
        [team.name for team in resolution.candidates],
    )


def unknown_team_error(snapshot: Snapshot, keys: Sequence[str], text: str) -> ToolError:
    """"Habe ich nicht" nur, wenn alle Wettbewerbe `keys` geladen sind, sonst der Ladefehler."""
    return loading_error(snapshot, keys, f"„{text}“") or ToolError(
        ErrorKind.UNKNOWN_TEAM,
        f"Den Verein „{text}“ habe ich nicht. Ich kenne nur Vereine aus den ausgewählten Wettbewerben.",
    )


def season_hints(snapshot: Snapshot, competitions: Iterable[str], *, upcoming: bool = False) -> list[str]:
    """Je Wettbewerb mit Daten aus der abgelaufenen Saison "Endstand der Saison 2026/27 ...".

    Mit `upcoming` (Frage nach kommenden Spielen) steht dabei, dass für die neue Saison noch nichts
    eingetragen ist.
    """
    hints = []
    for key in competitions:
        ended = snapshot.ended_season.get(key)
        if ended is None:
            continue
        text = f"Endstand der Saison {ended.label} in {_name(snapshot, key)}"
        if upcoming:
            text += ", für die neue Saison ist noch nichts eingetragen"
        hints.append(f"{text}.")
    return hints


def loading_hints(snapshot: Snapshot, competitions: Iterable[str]) -> list[str]:
    """Je ein Satz für ungültige, nicht erreichbare und noch nicht geladene Wettbewerbe."""
    invalid, unreachable, waiting = [], [], []
    for key in competitions:
        if _is_invalid(snapshot, key):
            invalid.append(_name(snapshot, key))
        elif key in snapshot.unreachable:
            unreachable.append(_name(snapshot, key))
        elif key not in snapshot.loaded:
            waiting.append(_name(snapshot, key))
    hints = []
    if invalid:
        hints.append(f"Für {join_names(invalid)} fehlen noch Daten für die neue Saison, deshalb fehlen sie in dieser Antwort.")
    if unreachable:
        hints.append(f"Die Daten für {join_names(unreachable)} sind gerade nicht erreichbar, deshalb fehlen sie in dieser Antwort.")
    if waiting:
        hints.append(f"Es sind noch nicht alle Wettbewerbe geladen; es fehlen noch: {join_names(waiting)}.")
    return hints


def live_score_known(snapshot: Snapshot, competition: Competition, match: Match) -> bool:
    """Ist der Spielstand eines laufenden Spiels frisch genug, um ihn zu nennen?

    Nur wenn die Spieldaten des Wettbewerbs nach dem Anstoß abgerufen wurden und höchstens
    `LIVE_SCORE_MAX_AGE` alt sind. Sonst stammt der Stand vielleicht von vor dem Anpfiff (zum Beispiel,
    wenn die Quelle seither nicht antwortet), und "0:0" wäre geraten.
    """
    stamp = snapshot.fetched_at.get(competition.key)
    if stamp is None:
        return False
    stamp, now = stamp.astimezone(UTC), snapshot.now.astimezone(UTC)
    return stamp > match.kickoff.astimezone(UTC) and now - stamp <= const.LIVE_SCORE_MAX_AGE


def _stale_live_sentence(snapshot: Snapshot, moment: datetime, entries: Sequence[Mapping[str, Any]]) -> str:
    same_day = local_day(moment, snapshot.tz) == local_day(snapshot.now, snapshot.tz)
    when = f"von {clock_phrase(moment, snapshot.tz)}" if same_day else f"vom {_moment_phrase(snapshot, moment)}"
    if len(entries) == 1:
        subject = f"Das Spiel {entries[0]['heim']} gegen {entries[0]['gast']} läuft"
    else:
        subject = f"{len(entries)} Spiele laufen"
    return f"{subject}; mein letzter Stand ist {when}, deshalb kann ich keinen Spielstand sagen."


def live_hints(snapshot: Snapshot, shown: Iterable[tuple[Match, Mapping[str, Any], Competition]]) -> list[str]:
    """Hinweise zu laufenden und zu laut Daten nicht beendeten Spielen.

    Ein laufendes Spiel mit frischem Stand bekommt den Hinweis zur Verzögerung; eines ohne frischen Stand (siehe
    `live_score_known`, dann fehlen `ergebnis` und `tore`) den Satz, von wann der letzte Stand ist.
    """
    fresh = False
    stale: dict[datetime, list[Mapping[str, Any]]] = {}
    not_finished = False
    for match, entry, competition in shown:
        state = entry.get("zustand")
        if state == STATE_TEXT[LIVE]:
            if live_score_known(snapshot, competition, match):
                fresh = True
            else:
                stamp = snapshot.fetched_at.get(competition.key)
                stale.setdefault(stamp or snapshot.now, []).append(entry)
        elif state == STATE_TEXT[NOT_FINISHED]:
            not_finished = True
    hints = []
    if fresh:
        hints.append(LIVE_LAG)
    hints += [_stale_live_sentence(snapshot, moment, entries) for moment, entries in sorted(stale.items())]
    if not_finished:
        hints.append(NOT_FINISHED_NOTE)
    return hints


def match_hints(match: Match, entry: Mapping[str, Any]) -> list[str]:
    """Sätze, die erklären, warum in `entry` Felder fehlen (je Spiel; mit `unique` zusammenführen)."""
    hints = []
    if entry["zustand"] == STATE_TEXT[SCHEDULED]:
        if entry.get("uhrzeit_fest") is False:
            hints.append(TIME_NOT_FIXED)
        return hints
    if entry["zustand"] == STATE_TEXT[LIVE] and "ergebnis" not in entry:
        return hints  # kein frischer Stand: `live_hints` sagt, von wann der letzte ist
    if _unclear(match):
        hints.append(NOT_UNIQUE)
        if match.goals:
            hints.append(NO_GOALS_WHEN_UNCLEAR)
    elif "tore" not in entry and _scorers_missing(match):
        hints.append(SCORERS_MISSING)
    return hints


def same_round_matches(match: Match, competition_matches: Iterable[Match]) -> list[Match]:
    """Spiele derselben Runde (gleiches Kürzel und gleiche Rundennummer), für `time_confirmed`."""
    return [m for m in competition_matches if m.shortcut == match.shortcut and m.round_order == match.round_order]


def is_final_round(competition: Competition, match: Match) -> bool:
    """Endspiel in Pokal oder Europapokal; Halb-, Viertel- und Achtelfinale zählen nicht."""
    if competition.kind not in (Kind.CUP, Kind.EUROPE):
        return False
    return normalize(match.round_name).startswith(_FINAL_PREFIXES)


def neutral_ground(competition: Competition, match: Match) -> bool:
    """Länderspiele und Endspiele gelten als Spiele auf neutralem Platz."""
    return competition.kind is Kind.NATIONAL or is_final_round(competition, match)


def round_text(name: str) -> str:
    """"1.Spieltag" und "1. Spieltag" lauten gleich."""
    return _ROUND_SPACE.sub(r"\1. ", name.strip())


def _unclear(match: Match) -> bool:
    """Ergebnis nicht eindeutig lesbar (auch ein beendetes Spiel ohne Endstand)."""
    return match.result_unclear or (match.finished and match.final is None)


def _running_score(match: Match) -> Score:
    """Höchster Stand der Torliste, ohne Tore 0:0."""
    if not match.goals:
        return Score(0, 0)
    last = match.goals[-1]  # nach Spielstand geordnet
    return Score(last.home, last.away)


def _expected_goals(match: Match) -> int | None:
    """Mindestzahl der Tore laut Ergebnis (im Elfmeterschießen nur die der regulären Spielzeit)."""
    score = match.regular if match.final_phase is Phase.PENALTIES else match.final
    return None if score is None else score.home + score.away


def _scorers_missing(match: Match) -> bool:
    """Fehlt bei einem Tor der Schütze, oder sind weniger Tore eingetragen, als das Ergebnis verlangt?"""
    if any(goal.scorer is None for goal in match.goals):
        return True
    if match.finished and not match.result_unclear:
        expected = _expected_goals(match)
        return expected is not None and len(match.goals) < expected
    return False


def _scoring_side(goal: Goal, before: tuple[int, int], match: Match) -> str | None:
    """"home" oder "away": die Seite, der das Tor zählt (bei Eigentoren die begünstigte)."""
    if goal.team_id == match.home.id:
        return "home"
    if goal.team_id == match.away.id:
        return "away"
    if goal.home > before[0]:
        return "home"
    if goal.away > before[1]:
        return "away"
    return None


def _goal_sentences(match: Match) -> list[str]:
    """Zum Beispiel "23. Minute: M. Olise (FC Bayern München)"; bei Eigentor der Verein des Schützen."""
    sentences, before = [], (0, 0)
    for goal in match.goals:
        side = _scoring_side(goal, before, match)
        before = (goal.home, goal.away)
        club = None
        if side is not None:
            beneficiary = match.home if side == "home" else match.away
            other = match.away if side == "home" else match.home
            club = other.name if goal.own_goal else beneficiary.name
        text = f"{goal.minute}. Minute: " if goal.minute is not None else ""
        text += goal.scorer or ""
        if club is not None:
            text += f" ({club})"
        if goal.own_goal:
            text += " (Eigentor)"
        elif goal.penalty:
            text += " (Elfmeter)"
        sentences.append(text)
    return sentences


def match_entry(
    match: Match,
    snapshot: Snapshot,
    *,
    team: Team | None,
    competition: Competition,
    same_round: Sequence[Match],
    ids: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Ein Spiel als flaches, vorlesbares Wörterbuch; Felder ohne Wert fehlen.

    Die Gründe für fehlende Felder liefert `match_hints` (bei laufenden Spielen `live_hints`). Ein
    laufendes Spiel bekommt `ergebnis` und `tore` nur mit frischem Stand (`live_score_known`). Mit `team` kommen `gegner`, `ort` und
    `ausgang` aus Sicht dieses Vereins dazu. `ids` (Pflicht, wenn `team`
    gegeben ist) sind alle Kennungen des Vereins; ein Verein kann in mehreren Wettbewerben unter
    verschiedenen Kennungen auftreten, `team.id` allein würde Spiele übersehen. Bei unklarem
    Ergebnis entfallen auch die Tore, sonst ließe sich der Stand aus der Torliste erraten.
    """
    if team is not None and ids is None:
        raise ValueError("match_entry braucht ids (alle Kennungen des Vereins), wenn team gegeben ist")
    now, tz = snapshot.now, snapshot.tz
    state = match_state(match, now)
    scheduled = state == SCHEDULED
    fixed = time_confirmed(match, same_round, now) if scheduled else True
    entry: dict[str, Any] = {"wettbewerb": competition.name}
    if round_name := round_text(match.round_name):
        entry["runde"] = round_name
    entry["wann"] = when_phrase(match.kickoff, tz, with_time=fixed, current_year=local_day(now, tz).year)
    if scheduled and fixed and match.kickoff.astimezone(UTC) - now.astimezone(UTC) < _COUNTDOWN_LIMIT:
        entry["bezug"] = countdown_phrase(match.kickoff, now)
    else:
        entry["bezug"] = relative_phrase(local_day(match.kickoff, tz), local_day(now, tz))
    if scheduled:
        entry["uhrzeit_fest"] = fixed
    entry["heim"] = match.home.name
    entry["gast"] = match.away.name
    entry["zustand"] = STATE_TEXT[state]

    final: Score | None = None
    unclear = _unclear(match)
    if state == FINISHED and not unclear and match.final is not None:
        final = match.final
        entry["ergebnis"] = format_score(final)
        if match.final_phase is Phase.EXTRA_TIME:
            entry["ergebnis_zusatz"] = _EXTRA_TIME_TEXT
        elif match.final_phase is Phase.PENALTIES:
            entry["ergebnis_zusatz"] = _PENALTIES_TEXT
        if match.final_phase in (Phase.EXTRA_TIME, Phase.PENALTIES) and match.regular is not None:
            entry["nach_90_minuten"] = format_score(match.regular)
    live_known = state == LIVE and live_score_known(snapshot, competition, match)
    if live_known:
        entry["ergebnis"] = format_score(_running_score(match))
    if state not in (SCHEDULED, LIVE) or live_known:
        if match.goals and not unclear and not _scorers_missing(match):
            entry["tore"] = _goal_sentences(match)

    if team is not None:
        home_is, away_is = match.home.id in ids, match.away.id in ids
        if home_is != away_is:
            entry["gegner"] = (match.away if home_is else match.home).name
            entry["ort"] = "neutral" if neutral_ground(competition, match) else ("heim" if home_is else "auswärts")
            if final is not None:
                own, other = (final.home, final.away) if home_is else (final.away, final.home)
                entry["ausgang"] = "Sieg" if own > other else "Niederlage" if own < other else "Unentschieden"
    return entry


def chosen_competitions(snapshot: Snapshot, settings: Settings) -> tuple[str, ...]:
    """Die gewählten Wettbewerbe in der Reihenfolge der Saison."""
    return tuple(k for k in snapshot.season.competitions if k in settings.competitions)


def favorite_ids(snapshot: Snapshot, settings: Settings, index: TeamIndex | None) -> frozenset[int]:
    """Alle Kennungen von Haupt- und Lieblingsvereinen (ein Verein kann je Wettbewerb eine andere haben)."""
    wanted = [t for t in (settings.main_team, *settings.favorite_teams) if t is not None]
    if not wanted:
        return frozenset()
    if index is None:  # nur die Kennungen werden gebraucht, deshalb ohne Spitznamen (und ohne Lesen einer Datei)
        index = TeamIndex.build(snapshot.matches, snapshot.season, {})
    ids: set[int] = set()
    for team_id in wanted:
        team = index.by_id(team_id)
        ids |= index.ids_of(team) if team is not None else {team_id}
    return frozenset(ids)


def round_matches(
    snapshot: Snapshot, key: str, which: str, *, from_day: date | None = None
) -> tuple[list[Match], str]:
    """Die letzte oder nächste Runde des Wettbewerbs aus den Spieldaten und ein Vermerk dazu.

    Letzte Runde: die jüngste (höchste Rundennummer) mit mindestens einem beendeten Spiel, nur mit ihren
    beendeten Spielen (Vermerk `UNFINISHED`, wenn dadurch Spiele fehlen); sonst nennte die Datumsspanne bei
    einem verlegten Spiel einen Tag in der Zukunft.
    Nächste Runde: unter den Runden mit Rundennummer von der der letzten Runde an aufwärts, die offene
    (angesetzte oder laufende) Spiele haben, die mit dem frühesten offenen Anstoß (bei Gleichstand die
    niedrigere Nummer). Ein verlegtes Spiel der letzten Runde verdrängt so nicht die Runde, die vorher
    beginnt. Gezeigt werden ihre offenen Spiele (Vermerk `STARTED`, wenn dadurch schon gespielte fehlen),
    sonst die ganze Runde. Gibt es nur offene Spiele in früheren Runden als der letzten (verlegte Spiele),
    ist die mit dem frühesten offenen Anstoß die nächste, nur mit diesen Spielen (Vermerk `CATCH_UP`).
    Ein Spiel, das laut Daten nie beendet wurde, ist nicht offen.
    Bei der Nationalmannschaft ist die Runde der letzte oder nächste Ortstag mit einem Länderspiel (auch einem
    laufenden), weil die Rundennummern der Datenstränge nicht vergleichbar sind.
    Mit `from_day` zählen für die nächste Runde nur Spiele an Ortstagen ab diesem Tag (zum Beispiel ab dem
    Beginn einer gefragten Woche); die letzte Runde bleibt davon unberührt.
    """
    now, tz = snapshot.now, snapshot.tz
    matches = snapshot.matches.get(key, ())
    if snapshot.season.competitions[key].kind is Kind.NATIONAL:
        wanted = (FINISHED if which == "letzter_spieltag" else SCHEDULED, LIVE)
        days = [local_day(m.kickoff, tz) for m in matches if match_state(m, now) in wanted]
        if which != "letzter_spieltag" and from_day is not None:
            days = [d for d in days if d >= from_day]
        if not days:
            return [], ""
        day = max(days) if which == "letzter_spieltag" else min(days)
        return [m for m in matches if local_day(m.kickoff, tz) == day], ""
    rounds: dict[tuple[str, int], list[Match]] = {}
    for match in matches:
        rounds.setdefault((match.shortcut, match.round_order), []).append(match)
    done = [r for r in rounds.values() if any(match_state(m, now) == FINISHED for m in r)]
    if which == "letzter_spieltag":
        if not done:
            return [], ""
        latest = max(done, key=lambda r: (r[0].round_order, max(m.kickoff for m in r)))
        finished = [m for m in latest if match_state(m, now) == FINISHED]
        return finished, UNFINISHED if len(finished) < len(latest) else ""
    last = max((r[0].round_order for r in done), default=None)

    def open_matches(round_: list[Match]) -> list[Match]:
        return [
            m
            for m in round_
            if match_state(m, now) in (SCHEDULED, LIVE) and (from_day is None or local_day(m.kickoff, tz) >= from_day)
        ]

    def start(round_: list[Match]) -> tuple[Any, int]:
        return min(m.kickoff for m in open_matches(round_)), round_[0].round_order

    opened = [r for r in rounds.values() if open_matches(r)]
    current = [r for r in opened if last is None or r[0].round_order >= last]
    if current:
        chosen = min(current, key=start)
        rest = open_matches(chosen)
        return (rest, STARTED) if len(rest) < len(chosen) else (chosen, "")
    if opened:
        return open_matches(min(opened, key=start)), CATCH_UP
    return [], ""


def asked_competition(
    snapshot: Snapshot, settings: Settings, text: Any, supports: Callable[[Competition], bool]
) -> str:
    """Der genannte Wettbewerb (Kürzel oder Name); die Frauen-Bundesliga nur auf ausdrücklichen Wunsch.

    `supports` wählt die Wettbewerbe, die die Frage überhaupt beantworten können (zum Beispiel mit Tabelle);
    nur sie nennt die Fehlermeldung als gültig.
    """
    competitions = snapshot.season.competitions
    chosen = chosen_competitions(snapshot, settings)
    valid = f"Gültig sind: {', '.join(k for k in chosen if supports(competitions[k]))}."
    lookup = {normalize(k): k for k in competitions} | {normalize(c.name): k for k, c in competitions.items()}
    key = lookup.get(normalize(str(text)))
    if key is None:
        raise ToolError(ErrorKind.INVALID_INPUT, f"Den Wettbewerb „{text}“ gibt es nicht. {valid}")
    if key not in chosen:
        raise ToolError(ErrorKind.INVALID_INPUT, f"Der Wettbewerb „{competitions[key].name}“ ist nicht ausgewählt. {valid}")
    return key


def season_started(snapshot: Snapshot, key: str) -> bool:
    """Ist im Wettbewerb laut Spieldaten schon ein Spiel beendet?"""
    return any(match_state(m, snapshot.now) == FINISHED for m in snapshot.matches.get(key, ()))


def table_problem(snapshot: Snapshot, keys: Sequence[str], who: str) -> ToolError | None:
    """Fehler, wenn die Tabellen der Wettbewerbe nicht vollständig brauchbar sind: nicht erreichbar, ungültig, nicht geladen."""
    error = loading_error(snapshot, keys, who)
    if error is not None:
        return error
    missing = [k for k in keys if k not in snapshot.tables]
    if missing:
        names = join_names(snapshot.season.competitions[k].name for k in missing)
        return ToolError(
            ErrorKind.UNREACHABLE,
            f"Ich finde {who} noch nicht, weil die Tabellen noch nicht alle geladen sind (es fehlen: {names})."
            " Versuch es gleich noch einmal.",
        )
    return None


def in_table(rows: Sequence[TableRow], ids: frozenset[int]) -> TableRow | None:
    return next((row for row in rows if row.team.id in ids), None)


def league_of(snapshot: Snapshot, settings: Settings, ids: frozenset[int], who: str) -> str | None:
    """Die gewählte Liga (ohne Frauen), in deren Spielliste der Verein steht; bei mehreren die erste der Saison.

    Gefunden ist sie, sobald die Spielliste einer brauchbaren Liga ein Spiel mit einer Kennung aus `ids`
    enthält; Tabellen braucht es dafür nicht. Ohne Fund ist `None` nur sicher, wenn alle Ligen brauchbar
    geladen sind und Spiele haben; sonst wirft die Funktion, welche Daten fehlen.
    """
    competitions = snapshot.season.competitions
    leagues = [
        k for k in chosen_competitions(snapshot, settings) if competitions[k].kind is Kind.LEAGUE and not competitions[k].women
    ]
    for key in available(snapshot, leagues):
        if any({m.home.id, m.away.id} & ids for m in snapshot.matches.get(key, ())):
            return key
    error = loading_error(snapshot, leagues, who)
    if error is not None:
        raise error
    empty = [k for k in leagues if not snapshot.matches.get(k)]
    if empty:
        names = join_names(competitions[k].name for k in empty)
        raise ToolError(
            ErrorKind.NOTHING_ENTERED,
            f"Ich kann {who} nicht sagen, weil für {names} noch keine Spiele eingetragen sind.",
        )
    return None


def fallback_league(snapshot: Snapshot, settings: Settings, what: str) -> str:
    """Bundesliga, sonst die erste gewählte Liga (ohne Frauen); `what` ist das Gesuchte, zum Beispiel "Tabelle"."""
    competitions = snapshot.season.competitions
    leagues = [
        k for k in chosen_competitions(snapshot, settings) if competitions[k].kind is Kind.LEAGUE and not competitions[k].women
    ]
    if DEFAULT_LEAGUE in leagues:
        return DEFAULT_LEAGUE
    if leagues:
        return leagues[0]
    raise ToolError(ErrorKind.UNSUPPORTED, f"Es ist keine Liga ausgewählt, deshalb kann ich keine {what} nennen.")


def default_league(snapshot: Snapshot, index: TeamIndex, settings: Settings, what: str) -> tuple[str, list[str]]:
    """Der Wettbewerb ohne Angabe: die Liga des Hauptvereins, sonst die Bundesliga (mit Hinweis, warum).

    Die Liga des Hauptvereins ist die gewählte Liga (ohne Frauen), in deren Spielliste er steht (`league_of`).
    Lässt sie sich nicht sicher bestimmen, weil Spiele fehlen, wirft die Funktion den Ladefehler.
    `what` ist das Gesuchte, zum Beispiel "Tabelle".
    """
    competitions = snapshot.season.competitions
    reason = None
    main = index.by_id(settings.main_team) if settings.main_team is not None else None
    if settings.main_team is not None and main is None:
        error = loading_error(snapshot, searchable(snapshot, settings), "deinen Hauptverein")
        if error is not None:
            raise error
        reason = "Dein Hauptverein kommt in keinem der ausgewählten Wettbewerbe vor"
    key = None
    if main is not None:
        key = league_of(snapshot, settings, index.ids_of(main), "die Liga deines Hauptvereins")
        if key is None:
            reason = f"{main.name} spielt in keiner der ausgewählten Ligen"
    hints: list[str] = []
    if key is None:
        key = fallback_league(snapshot, settings, what)
        if reason is not None:
            hints.append(f"{reason}; ich zeige deshalb die {what} von {competitions[key].name}.")
    return key, hints
