"""Antwort "Tabelle": Ausschnitt einer Tabelle mit Zonen und fertig berechneten Abständen (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from .models import TableRow, Team
from .results import (
    ErrorKind,
    Settings,
    ToolError,
    ambiguous_payload,
    asked_competition,
    default_league,
    given,
    in_table,
    league_of,
    searchable,
    season_hints,
    season_started,
    stand_phrase,
    table_problem,
    table_stale_hints,
    unique,
    unknown_team_error,
)
from .seasons import Competition, Kind, Zone, zone_of
from .store import LIVE, Snapshot, match_state
from .teams import TeamIndex

SECTIONS = ("spitze", "ende", "ganz")
SECTION_SIZE = 5  # "spitze" und "ende": so viele Zeilen
NEIGHBOURS = 2  # Ausschnitt um einen Verein: so viele Zeilen darüber und darunter
MAX_DISTANCES = 5

ZONES_EXPLAINED = "Bei Plätzen ohne Zonenangabe geht es weder um Aufstieg noch um Relegation oder Abstieg."
LEAGUE_PHASE = (
    "Für die Plätze der Ligaphase nenne ich keine Zonen; welche Plätze weiterkommen, ist in dieser Version nicht enthalten."
)
UNEQUAL_GAMES = "Die Vereine haben unterschiedlich viele Spiele; die Abstände zählen nur die Punkte."
RUNNING_COUNTED = (
    "Die Tabelle rechnet laufende Spiele mit dem aktuellen Stand ein; bis zum Abpfiff kann sie sich noch ändern."
)

PLAYING_NOW = "{name} spielt gerade; den Spielstand liefert fussball_assist__verein."


def _check_section(value: Any) -> str:
    text = given(value)
    if text is None:
        return SECTIONS[0]
    if isinstance(text, str) and text.casefold() in SECTIONS:
        return text.casefold()
    raise ToolError(ErrorKind.INVALID_INPUT, f"Den Ausschnitt „{value}“ gibt es nicht. Gültig sind: {', '.join(SECTIONS)}.")


def _no_table(competition: Competition) -> ToolError:
    return ToolError(
        ErrorKind.UNSUPPORTED,
        f"Im Wettbewerb {competition.name} gibt es keine Tabelle. Frag nach den Spielen von {competition.name}, dann nenne ich sie.",
    )


def _points(count: int) -> str:
    return f"{count} Punkt" if count == 1 else f"{count} Punkte"


def _behind(gap: int, target: str) -> str:
    return f"punktgleich mit {target}" if gap == 0 else f"{_points(gap)} hinter {target}"


def _ahead(gap: int, target: str) -> str:
    return f"punktgleich mit {target}" if gap == 0 else f"{_points(gap)} vor {target}"


def _zone_label(zones: Sequence[Zone], place: int) -> str | None:
    return next((z.label for z in zones if z.first <= place <= z.last), None)


def _target(zones: Sequence[Zone], place: int, leaving: Zone | None) -> str:
    """"dem Aufstiegsplatz (Platz 2)" für einen Platz in einer Zone, sonst "Platz 15", beim Verlassen einer Zone mit Erklärung."""
    label = _zone_label(zones, place)
    if label is not None:
        return f"dem {label} (Platz {place})"
    if leaving is not None:
        return f"Platz {place}, dem ersten Platz ohne {leaving.label}"
    return f"Platz {place}"


def _distances(rows: Sequence[TableRow], zones: Sequence[Zone], club: TableRow) -> tuple[list[str], list[TableRow]]:
    """Abstandssätze des Vereins, aus den Punkten gerechnet, und die Zeilen, auf die sie sich beziehen.

    Bezugsplätze: Platz 1 (als Tabellenführer Platz 2), der Platz darüber und darunter und die Zone-Grenzen.
    Außerhalb einer Zone ist das der nächstgelegene Platz der nächsten Zone darüber und darunter, innerhalb
    einer Zone der erste Platz, der sie nach oben und der sie nach unten verlässt. Jeder Satz nennt die
    Platznummer; ein Platz kommt nur einmal vor, und die Zone geht dem bloßen Platz vor. Reihenfolge von oben
    nach unten. Ein Platz, dessen Punkte nicht zur Ordnung der Tabelle passen, bekommt keinen Satz.
    """
    by_place = {row.position: row for row in rows}
    place, last = club.position, max(by_place)
    zone = next((z for z in zones if z.first <= place <= z.last), None)
    if zone is None:  # der nächstgelegene Platz der nächsten Zone darüber und darunter
        above = max((z for z in zones if z.last < place), key=lambda z: z.last, default=None)
        below = min((z for z in zones if place < z.first <= last), key=lambda z: z.first, default=None)
        up_place = above.last if above is not None else None
        down_place = below.first if below is not None else None
    else:  # die ersten Plätze außerhalb der Zone
        up_place = zone.first - 1 if zone.first > 1 else None
        down_place = zone.last + 1 if zone.last < last else None

    up: dict[int, str] = {}
    if place > 1:
        up[1] = "Platz 1"
        if place > 2:
            up[place - 1] = f"Platz {place - 1}"
    if up_place is not None:
        up[up_place] = _target(zones, up_place, zone)
    down: dict[int, str] = {}
    if place < last:
        down[place + 1] = f"Platz {place + 1}"
    if down_place is not None:
        down[down_place] = _target(zones, down_place, zone)

    sentences: list[str] = []
    used: list[TableRow] = []

    def add(phrase: str, other: int, gap: int) -> None:
        if gap >= 0:  # eine nicht nach Punkten geordnete Tabelle ergibt keinen Satz
            sentences.append(phrase)
            used.append(by_place[other])

    if place == 1:
        target = down.pop(2, None)
        if target is None:
            sentences.append("Tabellenführer")
        else:
            gap = club.points - by_place[2].points
            add(f"Tabellenführer, {_ahead(gap, target)}", 2, gap)
    for other, target in sorted(up.items()):
        gap = by_place[other].points - club.points
        add(_behind(gap, target), other, gap)
    for other, target in sorted(down.items()):
        gap = club.points - by_place[other].points
        add(_ahead(gap, target), other, gap)
    return sentences[:MAX_DISTANCES], used


def _row(row: TableRow, competition: Competition) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "platz": row.position,
        "verein": row.team.name.strip(),
        "spiele": row.matches,
        "punkte": row.points,
        "tore": row.goals_for,
        "gegentore": row.goals_against,
        "tordifferenz": row.goal_diff,
        "siege": row.won,
        "unentschieden": row.draw,
        "niederlagen": row.lost,
    }
    if competition.kind is not Kind.EUROPE and (zone := zone_of(competition, row.position)) is not None:
        entry["zone"] = zone
    return entry


def _section(rows: Sequence[TableRow], section: str, club: TableRow | None) -> list[TableRow]:
    if club is not None:
        return [r for r in rows if abs(r.position - club.position) <= NEIGHBOURS]
    if section == "spitze":
        return list(rows[:SECTION_SIZE])
    if section == "ende":
        return list(rows[-SECTION_SIZE:])
    return list(rows)


def _choose(
    snapshot: Snapshot, index: TeamIndex, settings: Settings, wettbewerb: Any, verein: Any
) -> tuple[str, Team | None, frozenset[int], list[str]] | dict[str, Any]:
    """Wettbewerb, Verein, dessen Kennungen und Hinweise zur Wahl der Liga, oder eine Rückfrage. Wirft nur `ToolError`."""
    competitions = snapshot.season.competitions
    asked = given(wettbewerb)
    key = asked_competition(snapshot, settings, asked, lambda c: c.has_table) if asked is not None else None
    if key is not None and not competitions[key].has_table:
        raise _no_table(competitions[key])

    text = given(verein)
    team: Team | None = None
    ids: frozenset[int] = frozenset()
    hints: list[str] = []
    if text is not None:
        resolution = index.resolve(text)
        if resolution.candidates:
            return ambiguous_payload(resolution, text)
        if resolution.national:
            raise ToolError(
                ErrorKind.UNSUPPORTED,
                "Für die Nationalmannschaft gibt es keine Tabelle. Frag nach den Spielen der Nationalmannschaft, dann nenne ich sie.",
            )
        team = resolution.team
        if team is None:
            raise unknown_team_error(snapshot, searchable(snapshot, settings), text)
        if key is not None and competitions[key].women:
            raise ToolError(
                ErrorKind.UNSUPPORTED,
                "Frauenteams kann ich über den Vereinsnamen nicht abfragen. "
                "Frag nach der Tabelle der Frauen-Bundesliga ohne Verein, dann zeige ich sie dir.",
            )
        ids = index.ids_of(team)
        if key is None:
            key = league_of(snapshot, settings, ids, f"die Liga von „{team.name}“")
            if key is None:
                raise _in_no_league(team, resolution.foreign)
    elif key is None:
        key, hints = default_league(snapshot, index, settings, "Tabelle")
    return key, team, ids, hints


def table_key(
    snapshot: Snapshot, index: TeamIndex, settings: Settings, *, wettbewerb: str | None, verein: str | None, ausschnitt: str
) -> str | None:
    """Der Wettbewerb, dessen Tabelle die Antwort braucht; None, wenn sie ohne Tabelle endet (Rückfrage, Fehler).

    Braucht nur die Spiellisten (der Verein und seine Liga kommen aus ihnen), deshalb lässt sich die eine
    nötige Tabelle anfordern, bevor die Antwort entsteht.
    """
    try:
        _check_section(ausschnitt)
        chosen = _choose(snapshot, index, settings, wettbewerb, verein)
    except ToolError:
        return None
    return None if isinstance(chosen, dict) else chosen[0]


def answer(
    snapshot: Snapshot,
    index: TeamIndex,
    settings: Settings,
    *,
    wettbewerb: str | None,
    verein: str | None,
    ausschnitt: str,
) -> dict[str, Any]:
    """Ausschnitt einer Tabelle, mit `verein` um den Verein herum samt Abständen; wirft nur `ToolError`.

    Prüfung in dieser Reihenfolge: `ausschnitt` (auch wenn ein Verein den Ausschnitt bestimmt), Wettbewerb,
    Verein. Mit `verein` zeigt das Ergebnis die Zeile des Vereins und je zwei darüber und darunter, `ausschnitt`
    gilt dann nicht. Ohne Verein: "spitze" die obersten fünf, "ende" die untersten fünf, "ganz" alle.
    Der Wettbewerb ohne Angabe ist die Liga des genannten Vereins, sonst die des Hauptvereins, sonst die
    Bundesliga; die Liga eines Vereins ist die gewählte Liga (ohne Frauen), in deren Spielliste er steht. Lässt
    sie sich nicht sicher bestimmen, weil Spiele fehlen, wirft die Antwort den Ladefehler. Der Wettbewerb
    muss eine Tabelle haben; Pokal und Nationalmannschaft werfen `UNSUPPORTED`. `abstaende` gibt es nur mit Verein.
    Rechnet die Tabelle ein laufendes Spiel ein (siehe `_counts_running_match`), sagt ein Hinweis das dazu.
    """
    section = _check_section(ausschnitt)
    competitions = snapshot.season.competitions
    chosen = _choose(snapshot, index, settings, wettbewerb, verein)
    if isinstance(chosen, dict):
        return chosen
    key, team, ids, hints = chosen

    competition = competitions[key]
    problem = table_problem(snapshot, [key], "die Tabelle")
    if problem is not None:
        raise problem
    rows = sorted(snapshot.tables[key], key=lambda r: r.position)
    if not rows:
        why = (
            "obwohl schon Spiele beendet sind; deshalb kann ich sie nicht nennen"
            if season_started(snapshot, key)
            else "die Saison hat vielleicht noch nicht begonnen"
        )
        raise ToolError(ErrorKind.NOTHING_ENTERED, f"Für {competition.name} ist noch keine Tabelle eingetragen, {why}.")

    club: TableRow | None = None
    if team is not None:
        club = in_table(rows, ids)
        if club is None:
            raise _not_in_table(snapshot, settings, key, team, ids)

    shown = _section(rows, section, club)
    result: dict[str, Any] = {"wettbewerb": competition.name, "stand": stand_phrase(snapshot, tables=[key])}
    if club is not None:
        result["verein"] = club.team.name.strip()
    result["zeilen"] = [_row(row, competition) for row in shown]
    if club is not None:
        sentences, used = _distances(rows, competition.zones, club)
        result["abstaende"] = sentences
        if any(other.matches != club.matches for other in used):
            hints.append(UNEQUAL_GAMES)

    if competition.kind is Kind.EUROPE:
        hints.append(LEAGUE_PHASE)
    elif competition.zones and any("zone" not in entry for entry in result["zeilen"]):
        hints.append(ZONES_EXPLAINED)
    if _counts_running_match(snapshot, key):
        hints.append(RUNNING_COUNTED)
    if club is not None and _plays_now(snapshot, key, ids):
        hints.append(PLAYING_NOW.format(name=result["verein"]))
    hints += table_stale_hints(snapshot, [key])
    hints += season_hints(snapshot, [key])
    result["hinweise"] = unique(hints)
    return result


def _counts_running_match(snapshot: Snapshot, key: str) -> bool:
    """Läuft im Wettbewerb ein Spiel, das schon vor dem Abruf der Tabelle angepfiffen war?

    OpenLigaDB rechnet laufende Spiele mit dem aktuellen Stand in die Tabelle ein; eine vor dem Anstoß
    abgerufene Tabelle enthält das Spiel aber noch nicht. Ohne Abrufzeit der Tabelle gilt das als nicht sicher.
    """
    stamp = snapshot.tables_at.get(key)
    if stamp is None:
        return False
    stamp = stamp.astimezone(UTC)
    matches = snapshot.matches.get(key, ())
    return any(match_state(m, snapshot.now) == LIVE and m.kickoff.astimezone(UTC) < stamp for m in matches)


def _plays_now(snapshot: Snapshot, key: str, ids: frozenset[int]) -> bool:
    """Spielt der Verein gerade, und sind die Spieldaten des Wettbewerbs nach dem Anstoß abgerufen?

    Ohne Abruf nach dem Anstoß weiß das Werkzeug nicht, ob das Spiel wirklich läuft (wie bei `_counts_running_match`,
    nur mit den Spieldaten statt der Tabelle); ohne Abrufzeit gilt das als nicht sicher.
    """
    stamp = snapshot.fetched_at.get(key)
    if stamp is None:
        return False
    stamp = stamp.astimezone(UTC)
    return any(
        match_state(m, snapshot.now) == LIVE and m.kickoff.astimezone(UTC) < stamp and {m.home.id, m.away.id} & ids
        for m in snapshot.matches.get(key, ())
    )


def _in_no_league(team: Team, foreign: bool) -> ToolError:
    text = f"Den Verein „{team.name}“ finde ich in keiner der ausgewählten Ligen."
    if foreign:
        text += " Er ist nur aus einem Europapokal bekannt; nenne den Wettbewerb, zum Beispiel champions_league."
    return ToolError(ErrorKind.NOTHING_ENTERED, text)


def _not_in_table(snapshot: Snapshot, settings: Settings, key: str, team: Team, ids: frozenset[int]) -> ToolError:
    """Der Verein fehlt in der geladenen, nicht leeren Tabelle; nennt die Liga, in der er spielt, soweit sie bekannt ist."""
    competitions = snapshot.season.competitions
    text = f"„{team.name}“ steht nicht in der Tabelle von {competitions[key].name}."
    try:
        league = league_of(snapshot, settings, ids, "die Liga")
    except ToolError:
        league = None
    if league is not None and league != key:
        text += f" Der Verein spielt in der Liga {competitions[league].name}."
    if key in snapshot.ended_season:
        text += f" Das ist der Endstand der Saison {snapshot.ended_season[key].label}."
    return ToolError(ErrorKind.NOTHING_ENTERED, text)
