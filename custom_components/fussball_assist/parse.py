"""Liest die Rohdaten von OpenLigaDB in die Datenklassen aus models.py.

Alles hier ist nachsichtig: Unbrauchbare Einträge werden übersprungen, nichts wirft.
Die Datenformen sind an den Aufzeichnungen unter tests/fixtures/ belegt (tests/test_fixtures.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from .models import Goal, Match, Phase, Score, Scorer, TableRow, Team

# Zeitangaben ohne Offset (lastUpdateDateTime, Änderungsabfrage) sind deutsche Ortszeit.
SOURCE_TIME_ZONE = ZoneInfo("Europe/Berlin")

_HALFTIME = "HalfTime"
_REGULAR = "After90Minutes"
_EXTRA_TIME = "AfterExtraTime"
_PENALTIES = "AfterPenalties"
# Phasen, die ein Ergebnis tragen, von der frühesten zur spätesten.
_RESULT_PHASES = {
    _REGULAR: Phase.REGULAR,
    _EXTRA_TIME: Phase.EXTRA_TIME,
    _PENALTIES: Phase.PENALTIES,
}
_KNOWN_KINDS = (_HALFTIME, *_RESULT_PHASES)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_datetime(text: Any, naive_zone: tzinfo) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.strip())
        if value.tzinfo is None:
            value = value.replace(tzinfo=naive_zone)
        return value.astimezone(UTC)
    except (ValueError, OverflowError):  # unlesbar oder außerhalb des darstellbaren Bereichs
        return None


def parse_local_datetime(text: str | None) -> datetime | None:
    """Liest eine Ortszeit der Quelle (Europe/Berlin, 0 bis 3 Nachkommastellen) und gibt UTC zurück."""
    return _parse_datetime(text, SOURCE_TIME_ZONE)


def _parse_team(raw: Any, *id_keys: str) -> Team | None:
    if not isinstance(raw, Mapping):
        return None
    team_id = next((i for i in (_int(raw.get(key)) for key in id_keys) if i is not None), None)
    name = _text(raw.get("teamName"))
    if team_id is None or not name:
        return None
    return Team(
        id=team_id,
        name=name,
        short_name=_text(raw.get("shortName")) or name,
        tla=_text(raw.get("tla")) or None,
    )


def _score_key(score: tuple[int, int]) -> tuple[int, int, int]:
    """Ordnet Spielstände nach Zahl der Tore; so steht der spätere Stand hinten."""
    return (score[0] + score[1], score[0], score[1])


def _collect_results(raw_results: list[Any]) -> dict[str, list[tuple[float, Score]]]:
    """Sammelt die Einträge je Ergebnisart als (resultID, Stand)."""
    entries: dict[str, list[tuple[float, Score]]] = {}
    for raw in raw_results:
        if not isinstance(raw, Mapping) or raw.get("resultTypeKind") not in _KNOWN_KINDS:
            continue
        home, away = _int(raw.get("pointsTeam1")), _int(raw.get("pointsTeam2"))
        if home is None or away is None:
            continue
        result_id = _int(raw.get("resultID"))
        entries.setdefault(raw["resultTypeKind"], []).append(
            (float("inf") if result_id is None else result_id, Score(home, away))
        )
    return entries


def _resolve_results(
    entries: dict[str, list[tuple[float, Score]]], references: dict[str, Score | None]
) -> tuple[dict[str, Score], bool]:
    """Ein Ergebnis je Art; bei widersprüchlichen Dubletten entscheidet der Stand aus der Torliste.

    Eine widersprüchliche Halbzeit entfällt ohne Weiteres; nur Widersprüche ab
    After90Minutes machen das Ergebnis unklar.
    """
    scores: dict[str, Score] = {}
    unclear = False
    for kind, candidates in entries.items():
        reference = references.get(kind)
        if len({score for _, score in candidates}) == 1:
            scores[kind] = candidates[0][1]
        elif kind == _HALFTIME:
            continue
        elif reference is not None and any(score == reference for _, score in candidates):
            scores[kind] = reference
        else:
            scores[kind] = min(candidates, key=lambda entry: entry[0])[1]
            unclear = True
    return scores, unclear


def _is_extra_time_goal(goal: Mapping[str, Any]) -> bool:
    """Tor der Verlängerung: nach der 90. Minute und nicht als Nachspielzeit markiert.

    isOvertime kennzeichnet in den Aufzeichnungen die Nachspielzeit (45.+ und 90.+),
    nicht die Verlängerung; deren Tore haben isOvertime false (dfb 81843, 81862).
    """
    minute = _int(goal.get("matchMinute"))
    return minute is not None and (minute > 120 or (minute > 90 and goal.get("isOvertime") is not True))


def _highest(goals: list[Mapping[str, Any]], start: Score | None) -> Score | None:
    scores = [Score(g["scoreTeam1"], g["scoreTeam2"]) for g in goals]
    if start is not None:
        scores.append(start)
    return max(scores, key=lambda score: _score_key((score.home, score.away)), default=None)


def _references(goals: list[Mapping[str, Any]], kinds: set[str]) -> dict[str, Score | None]:
    """Stand aus der Torliste, an dem jede Ergebnisart gemessen wird."""
    if not goals:
        return {}
    shootout = _PENALTIES in kinds
    in_play = [g for g in goals if not (shootout and g.get("isPenalty") is True and _int(g.get("matchMinute")) is None)]
    references = {_PENALTIES: _highest(goals, None), _EXTRA_TIME: _highest(in_play, Score(0, 0))}
    if _EXTRA_TIME in kinds or shootout:
        if any(_int(g.get("matchMinute")) is None for g in in_play):
            references[_REGULAR] = None  # ohne Minute lässt sich die Verlängerung nicht abtrennen
        else:
            references[_REGULAR] = _highest([g for g in in_play if not _is_extra_time_goal(g)], Score(0, 0))
    else:
        references[_REGULAR] = references[_EXTRA_TIME]
    return references


def _season(value: Any) -> int:
    if isinstance(value, str):
        value = value.strip()
        return int(value) if value.isascii() and value.isdecimal() else 0
    return _int(value) or 0


def parse_match(raw: Mapping[str, Any]) -> Match | None:
    """Liest ein Spiel; gibt None zurück, wenn Kennung, Mannschaften oder Anstoß fehlen. Wirft nie."""
    try:
        return _parse_match(raw)
    except Exception:  # noqa: BLE001 – Rückhalt: ein kaputter Satz darf die Liste nicht kippen
        return None


def _parse_match(raw: Mapping[str, Any]) -> Match | None:
    if not isinstance(raw, Mapping):
        return None
    match_id = _int(raw.get("matchID"))
    home = _parse_team(raw.get("team1"), "teamId")
    away = _parse_team(raw.get("team2"), "teamId")
    kickoff = _parse_datetime(raw.get("matchDateTimeUTC"), UTC)
    if match_id is None or home is None or away is None or kickoff is None:
        return None

    group = raw.get("group")
    if not isinstance(group, Mapping):
        group = {}

    raw_goals = [
        g
        for g in _list(raw.get("goals"))
        if isinstance(g, Mapping) and _int(g.get("scoreTeam1")) is not None and _int(g.get("scoreTeam2")) is not None
    ]
    entries = _collect_results(_list(raw.get("matchResults")))
    scores, unclear = _resolve_results(entries, _references(raw_goals, set(entries)))
    shootout_held = _PENALTIES in entries

    finished = raw.get("matchIsFinished") is True
    if not finished:
        # Laufende Spiele tragen Zwischenstände in matchResults; der Stand kommt aus der Torliste.
        scores = {kind: score for kind, score in scores.items() if kind == _HALFTIME}
        unclear = False

    final_kind = next((kind for kind in reversed(_RESULT_PHASES) if kind in scores), None)
    final_phase = None if final_kind is None else _RESULT_PHASES[final_kind]
    if finished and final_kind is None:
        unclear = True

    goals = []
    for g in raw_goals:
        minute = _int(g.get("matchMinute"))
        penalty = g.get("isPenalty") is True
        if shootout_held and penalty and minute is None:
            continue  # Elfmeterschießen; der Stand danach steht in AfterPenalties
        name = _text(g.get("goalGetterName"))
        goals.append(
            Goal(
                minute=minute,
                scorer=None if not name or g.get("goalGetterID") == 0 else name,
                team_id=_int(g.get("scoringTeamId")),
                home=g["scoreTeam1"],
                away=g["scoreTeam2"],
                penalty=penalty,
                own_goal=g.get("isOwnGoal") is True,
                shootout=False,
            )
        )
    goals.sort(key=lambda goal: _score_key((goal.home, goal.away)))

    return Match(
        id=match_id,
        shortcut=_text(raw.get("leagueShortcut")),
        season=_season(raw.get("leagueSeason")),
        round_name=_text(group.get("groupName")),
        round_order=_int(group.get("groupOrderID")) or 0,
        kickoff=kickoff,
        home=home,
        away=away,
        finished=finished,
        final=None if final_kind is None else scores[final_kind],
        final_phase=final_phase,
        regular=scores.get(_REGULAR),
        halftime=scores.get(_HALFTIME),
        result_unclear=unclear,
        goals=tuple(goals),
        last_update=parse_local_datetime(raw.get("lastUpdateDateTime")),
    )


def parse_matches(raw: Any) -> tuple[Match, ...]:
    """Liest eine Liste von Spielen; unbrauchbare Einträge und Nicht-Listen ergeben nichts."""
    return tuple(match for match in map(parse_match, _list(raw)) if match is not None)


_TABLE_NUMBERS = {
    "matches": "matches",
    "points": "points",
    "goals_for": "goals",
    "goals_against": "opponentGoals",
    "goal_diff": "goalDiff",
    "won": "won",
    "draw": "draw",
    "lost": "lost",
}


def parse_table(raw: Any) -> tuple[TableRow, ...]:
    """Liest eine Tabelle; der Platz ist die Stelle in der Liste der Quelle (Index + 1)."""
    rows = []
    for index, entry in enumerate(_list(raw)):
        team = _parse_team(entry, "teamInfoId", "teamId")
        if team is None:
            continue
        numbers = {field: _int(entry.get(key)) for field, key in _TABLE_NUMBERS.items()}
        if any(value is None for value in numbers.values()):
            continue
        rows.append(TableRow(position=index + 1, team=team, **numbers))
    return tuple(rows)


def parse_scorers(raw: Any) -> tuple[Scorer, ...]:
    """Liest die Torjägerliste, absteigend nach Toren; Einträge ohne Namen entfallen."""
    scorers = []
    for entry in _list(raw):
        if not isinstance(entry, Mapping):
            continue
        name, goals = _text(entry.get("goalGetterName")), _int(entry.get("goalCount"))
        if name and goals is not None:
            scorers.append(Scorer(name=name, goals=goals))
    return tuple(sorted(scorers, key=lambda scorer: -scorer.goals))
