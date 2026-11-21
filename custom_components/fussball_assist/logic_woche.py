"""Antwort "Die Woche": die wichtigsten Spiele nach festen Regeln (ohne Home-Assistant-Bezug).

Die Regeln gelten in dieser Reihenfolge; ein Spiel steht mit dem Grund der ersten passenden Regel einmal in der
Antwort:

1. "Dein Verein spielt": Haupt- oder Lieblingsverein.
2. "Länderspiel": Spiel der Nationalmannschaft.
3. "K.-o.-Spiel im <Pokal>" oder "K.-o.-Spiel in der <Europapokal-Wettbewerb>": Pokal oder Europapokal nach der
   Ligaphase, mit mindestens einem Verein aus den gewählten Männerligen.
4. "Spitzenspiel": beide Mannschaften stehen in derselben Liga unter den ersten Vier.
5. "Derby": Paar aus `data/derbys.json`.

Regel 4 gilt in jeder gewählten Liga, auch in der Frauen-Bundesliga (mit ihrer Tabelle); Regel 3 zählt nur Vereine
aus den gewählten Männerligen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
import json
import logging
from typing import Any

from .models import Match, Team
from .results import (
    CATCH_UP,
    ErrorKind,
    Settings,
    ToolError,
    available,
    chosen_competitions,
    default_league,
    fallback_league,
    favorite_ids,
    given,
    guard,
    in_table,
    join_names,
    live_hints,
    loading_error,
    loading_hints,
    match_entry,
    match_hints,
    round_matches,
    round_text,
    same_round_matches,
    season_hints,
    shown_stand,
    table_stale_hints,
    unique,
)
from .seasons import DATA_DIR, Kind
from .store import LIVE, SCHEDULED, Snapshot, match_state
from .teams import TeamIndex
from .timeutil import local_day, range_phrase

MAX_MATCHES = 5
TOP_PLACES = 4  # ein Spitzenspiel: beide unter den ersten vier
WEEKS = ("diese", "naechste")
_SUNDAY = 6

OWN_TEAM = "Dein Verein spielt"
INTERNATIONAL = "Länderspiel"
KNOCKOUT_CUP = "K.-o.-Spiel im {}"
KNOCKOUT_EUROPE = "K.-o.-Spiel in der {}"
TOP_MATCH = "Spitzenspiel: Platz {} gegen Platz {}"
DERBY = "Derby: {}"

_LOGGER = logging.getLogger(__name__)
_WEEK_TEXT = {"diese": "dieser Woche", "naechste": "der nächsten Woche"}

Derby = tuple[int, int, str]


def load_derbies() -> tuple[Derby, ...]:
    """Liest data/derbys.json (Liste aus [Kennung, Kennung, Name]).

    Fehlende oder kaputte Datei ergibt (), ungültige Einträge entfallen einzeln; beides wird protokolliert.
    Die Funktion liest eine Datei; in Home Assistant gehört der Aufruf in den Executor.
    """
    path = DATA_DIR / "derbys.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LOGGER.warning("Derbys %s nicht gefunden", path)
        return ()
    except (OSError, ValueError) as err:
        _LOGGER.warning("Derbys %s sind nicht lesbar: %s: %s", path, type(err).__name__, err)
        return ()
    if not isinstance(raw, list):
        _LOGGER.warning("Derbys %s sind ungültig: Liste erwartet", path)
        return ()
    derbies: list[Derby] = []
    for item in raw:
        if _is_derby(item):
            derbies.append((item[0], item[1], item[2]))
        else:
            _LOGGER.warning("Derby %r in %s ungültig und übergangen", item, path)
    return tuple(derbies)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_derby(item: Any) -> bool:
    return (
        isinstance(item, list)
        and len(item) == 3
        and _is_int(item[0])
        and _is_int(item[1])
        and item[0] != item[1]
        and isinstance(item[2], str)
        and bool(item[2].strip())
    )


def _check_week(value: Any) -> str:
    text = given(value)
    if text is None:
        return WEEKS[0]
    if isinstance(text, str) and text.casefold() in WEEKS:
        return text.casefold()
    raise ToolError(ErrorKind.INVALID_INPUT, f"Die Woche „{value}“ gibt es nicht. Gültig sind: {', '.join(WEEKS)}.")


def _period(week: str, today: date) -> tuple[date, date]:
    """Diese Woche: heute bis Sonntag. Nächste Woche: der folgende Montag bis Sonntag."""
    if week == "diese":
        return today, today + timedelta(days=_SUNDAY - today.weekday())
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=_SUNDAY)


def needed_tables(snapshot: Snapshot, settings: Settings) -> tuple[str, ...]:
    """Die Tabellen, die Regel 4 (Spitzenspiel) braucht: die der gewählten Ligen, auch der Frauen-Bundesliga."""
    competitions = snapshot.season.competitions
    return tuple(
        k for k in chosen_competitions(snapshot, settings) if competitions[k].kind is Kind.LEAGUE and competitions[k].has_table
    )


def _ids(index: TeamIndex, team: Team) -> frozenset[int]:
    """Alle Kennungen des Vereins; unbekannte Mannschaften (Länderspiele) nur mit der eigenen."""
    return index.ids_of(team) or frozenset({team.id})


def answer(
    snapshot: Snapshot,
    index: TeamIndex,
    settings: Settings,
    derbies: Sequence[Derby],
    *,
    woche: str | None,
) -> dict[str, Any]:
    """Höchstens fünf besondere Spiele der Woche mit je einem Grund; wirft nur `ToolError`.

    Ob in der Woche nichts Besonderes ansteht, steht nur dann fest, wenn alle gewählten Wettbewerbe
    und die Tabellen aller gewählten Ligen vollständig geladen sind; sonst steht stattdessen, dass es nur
    für die bisher geladenen Daten gilt, und was fehlt. Spitzenspiele erkennt die Antwort nur mit Tabelle.
    `stand` richtet sich nach den Wettbewerben der gezeigten Spiele (ohne gezeigtes Spiel nach allen geladenen)
    und den Tabellen der gewählten Ligen.
    """
    week = _check_week(woche)
    competitions = snapshot.season.competitions
    relevant = chosen_competitions(snapshot, settings)
    guard(snapshot, relevant)
    loaded = available(snapshot, relevant)
    if not loaded:  # noch nichts geladen: "nichts Besonderes" wäre geraten
        raise loading_error(snapshot, relevant, "die Spiele") or ToolError(
            ErrorKind.UNREACHABLE, "Die Spiele sind noch nicht geladen. Versuch es gleich noch einmal."
        )
    first, end = _period(week, local_day(snapshot.now, snapshot.tz))

    leagues = [k for k in loaded if competitions[k].kind is Kind.LEAGUE]
    tables = {k: snapshot.tables[k] for k in leagues if snapshot.tables.get(k)}
    missing_tables = [competitions[k].name for k in leagues if competitions[k].has_table and k not in tables]
    complete = len(loaded) == len(relevant) and not missing_tables

    favorites = favorite_ids(snapshot, settings, index)
    main = favorite_ids(snapshot, replace(settings, favorite_teams=()), index)
    german = frozenset(  # Vereine der gewählten Männerligen
        i
        for k in leagues
        if not competitions[k].women
        for m in snapshot.matches.get(k, ())
        for team in (m.home, m.away)
        for i in _ids(index, team)
    )
    known = tuple(derbies)

    def reason(key: str, match: Match) -> tuple[int, str, str | None] | None:
        """Nummer der ersten passenden Regel, ihr Grund und bei Spitzenspielen die Liga der Tabelle."""
        competition = competitions[key]
        home, away = _ids(index, match.home), _ids(index, match.away)
        if (home | away) & favorites:
            return 1, OWN_TEAM, None
        if competition.kind is Kind.NATIONAL:
            return 2, INTERNATIONAL, None
        knockout = competition.kind is Kind.CUP or (
            competition.kind is Kind.EUROPE
            and competition.league_phase_rounds is not None
            and match.round_order > competition.league_phase_rounds
        )
        if knockout and (home | away) & german:
            return 3, (KNOCKOUT_CUP if competition.kind is Kind.CUP else KNOCKOUT_EUROPE).format(competition.name), None
        for table_key, table in tables.items():
            a, b = in_table(table, home), in_table(table, away)
            if a is not None and b is not None and max(a.position, b.position) <= TOP_PLACES:
                return 4, TOP_MATCH.format(a.position, b.position), table_key
        for one, other, name in known:
            if (one in home and other in away) or (other in home and one in away):
                return 5, DERBY.format(name), None
        return None

    found: list[tuple[tuple[int, Any, bool, int], str, Match, str, str | None]] = []
    for key in loaded:
        for match in snapshot.matches.get(key, ()):
            if not first <= local_day(match.kickoff, snapshot.tz) <= end:
                continue
            if match_state(match, snapshot.now) not in (SCHEDULED, LIVE):
                continue
            hit = reason(key, match)
            if hit is not None:
                # Regel, Anstoß, bei gleichem Anstoß der Hauptverein zuerst, dann die Kennung.
                rank = (hit[0], match.kickoff, not (_ids(index, match.home) | _ids(index, match.away)) & main, match.id)
                found.append((rank, key, match, hit[1], hit[2]))
    found.sort(key=lambda item: item[0])

    entries = []
    shown = []
    hints: list[str] = []
    top_tables: list[str] = []  # Tabellen, aus denen ein genanntes Spitzenspiel stammt
    for _, key, match, why, table_key in found[:MAX_MATCHES]:
        every = snapshot.matches.get(key, ())
        entry = match_entry(
            match, snapshot, team=None, competition=competitions[key], same_round=same_round_matches(match, every)
        )
        entry["grund"] = why
        hints += match_hints(match, entry)
        entries.append(entry)
        shown.append((match, entry, competitions[key]))
        if table_key is not None:
            top_tables.append(table_key)
    hints += live_hints(snapshot, shown)
    hints += table_stale_hints(snapshot, unique(top_tables))
    if len(found) > MAX_MATCHES:
        hints.append(f"Es gibt {len(found)} besondere Spiele; ich nenne nur {MAX_MATCHES}.")
    if missing_tables:
        hints.append(
            f"Für Spitzenspiele fehlt die Tabelle von {join_names(missing_tables)}, deshalb erkenne ich dort keine."
        )
    if not entries:
        if complete:
            hints.append(f"In {_WEEK_TEXT[week]} steht nichts Besonderes an.")
        else:
            hints.append(f"In den bisher geladenen Daten steht in {_WEEK_TEXT[week]} nichts Besonderes an.")
        hints += _next_round(snapshot, index, settings, loaded, from_day=first if week == "naechste" else None)
    elif not complete:
        hints.append("Die Auswahl beruht nur auf den bisher geladenen Daten; es können weitere besondere Spiele dazukommen.")
    hints += season_hints(snapshot, loaded, upcoming=True)
    hints += loading_hints(snapshot, relevant)
    return {
        "zeitraum": range_phrase(first, end, current_year=local_day(snapshot.now, snapshot.tz).year),
        "stand": shown_stand(snapshot, loaded, (competition.key for _, _, competition in shown), tables=tables),
        "spiele": entries,
        "hinweise": unique(hints),
    }


def _next_round(
    snapshot: Snapshot, index: TeamIndex, settings: Settings, loaded: Sequence[str], *, from_day: date | None
) -> list[str]:
    """Der nächste Spieltag der Liga des Hauptvereins, sonst der Bundesliga (der ersten gewählten Liga).

    Mit `from_day` (Beginn der gefragten Woche) der erste Spieltag mit Spielen ab diesem Tag, sonst der nächste ab jetzt.
    """
    try:
        key, _ = default_league(snapshot, index, settings, "Tabelle")
    except ToolError:  # Tabellen fehlen: dann die Vorgabe ohne Tabelle
        try:
            key = fallback_league(snapshot, settings, "Tabelle")
        except ToolError:  # keine Männerliga gewählt: die erste gewählte Liga, auch die Frauen-Bundesliga
            competitions = snapshot.season.competitions
            key = next((k for k in loaded if competitions[k].kind is Kind.LEAGUE), None)
            if key is None:
                return []
    if key not in loaded:
        return []
    name = snapshot.season.competitions[key].name
    chosen, note = round_matches(snapshot, key, "naechster_spieltag", from_day=from_day)
    if not chosen:
        return [f"Im Wettbewerb {name} ist kein weiterer Spieltag eingetragen."]
    days = [local_day(m.kickoff, snapshot.tz) for m in chosen]
    when = range_phrase(min(days), max(days), current_year=local_day(snapshot.now, snapshot.tz).year)
    if note == CATCH_UP:
        return [f"Das nächste Spiel im Wettbewerb {name} ist ein Nachholspiel ({when})."]
    label = round_text(min(chosen, key=lambda m: (m.kickoff, m.id)).round_name)
    return [f"Der nächste Spieltag im Wettbewerb {name} ist {f'der {label} ({when})' if label else when}."]
