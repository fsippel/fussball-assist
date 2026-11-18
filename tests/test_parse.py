"""Lesen der Rohdaten von OpenLigaDB an aufgezeichneten Antworten."""

from datetime import UTC, datetime

import pytest

from custom_components.fussball_assist.models import Phase, Score
from custom_components.fussball_assist.parse import (
    parse_local_datetime,
    parse_match,
    parse_matches,
    parse_scorers,
    parse_table,
)

from .conftest import load_fixture


def _match(fixture, match_id):
    return parse_match(next(m for m in load_fixture(fixture) if m["matchID"] == match_id))


def test_league_match():
    m = _match("bl1_2026_md9", 83234)
    assert (m.final, m.final_phase, m.halftime) == (Score(3, 1), Phase.REGULAR, Score(1, 1))
    assert m.finished and not m.result_unclear and m.kickoff.tzinfo is not None


def test_match_fields_come_from_the_record():
    m = _match("bl1_2026_md9", 83234)
    assert (m.id, m.shortcut, m.season, m.round_name, m.round_order) == (83234, "bl1", 2026, "9. Spieltag", 9)
    assert m.kickoff == datetime(2026, 11, 6, 19, 30, tzinfo=UTC)
    assert m.last_update == datetime(2026, 11, 10, 21, 41, 58, 830000, tzinfo=UTC)
    assert (m.home.id, m.home.name, m.home.short_name, m.home.tla) == (100, "Hamburger SV", "HSV", None)
    assert m.away.id == 87
    assert [(g.minute, g.scorer, g.team_id) for g in m.goals][0] == (11, "M. Muheim", 100)


def test_empty_short_name_falls_back_to_name():
    m = _match("dfb_2026", 81835)
    team = m.home if m.home.id == 1071 else m.away
    assert team.short_name == team.name == "Eintracht Trier"


def test_cup_after_extra_time():
    m = _match("dfb_2026", 81843)
    assert (m.final, m.final_phase, m.regular) == (Score(2, 5), Phase.EXTRA_TIME, Score(2, 2))


def test_cup_on_penalties():
    m = _match("dfb_2026", 81850)
    assert (m.final, m.final_phase, m.regular) == (Score(5, 6), Phase.PENALTIES, Score(1, 1))
    assert all(not g.shootout for g in m.goals) and len(m.goals) == 2


def test_duplicate_result_entries_follow_the_goal_list():
    m = parse_match(load_fixture("match_87778"))  # der Abruf nach Kennung liefert ein einzelnes Objekt
    assert m.final == Score(2, 1) and not m.result_unclear


def test_unfinished_match_has_no_result():
    m = _match("bl1_2026_md10", 83237)
    assert not m.finished and m.final is None and m.goals == ()


def test_running_match_reads_score_from_goals():
    m = next(m for m in parse_matches(load_fixture("live_synthetisch")) if m.id == 83237)
    assert not m.finished and m.final is None
    assert (m.goals[-1].home, m.goals[-1].away) == (1, 1)


def test_unfinished_match_ignores_interim_results():
    m = _match("nla_2026", 85259)  # lief während der Aufzeichnung, matchResults trägt schon Zwischenstände
    assert not m.finished and (m.final, m.final_phase, m.regular) == (None, None, None)
    assert m.halftime == Score(2, 2) and not m.result_unclear
    assert (m.goals[-1].home, m.goals[-1].away) == (2, 3)


def test_conflicting_halftime_does_not_make_result_unclear():
    m = _match("ucl_2026", 87412)  # Halbzeit 2:0 und 0:0, Ende 3:3
    assert (m.final, m.final_phase, m.halftime) == (Score(3, 3), Phase.REGULAR, None)
    assert not m.result_unclear


def test_goals_are_ordered_by_score_not_by_position():
    m = _match("dfb_2026", 81850)
    assert [(g.home, g.away) for g in m.goals] == sorted((g.home, g.away) for g in m.goals)


def test_missing_scorers_are_none():
    m = _match("uecl2026_2026_md1", 88295)
    assert m.final == Score(3, 2) and all(g.scorer is None for g in m.goals)


def _damaged(**changes):
    return dict(load_fixture("bl1_2026_md9")[0], **changes)


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"matchID": 1},
        {"matchID": 1, "team1": None, "team2": None},
        "kein Objekt",
        None,
        _damaged(team2=None),
        _damaged(matchDateTimeUTC="unsinn"),
        _damaged(matchDateTimeUTC=None),
        _damaged(matchDateTimeUTC="0001-01-01T00:00:00+01:00"),  # vor dem Jahr 1 in UTC
    ],
)
def test_unusable_record_is_skipped(broken):
    good = load_fixture("bl1_2026_md9")
    assert len(parse_matches([broken, *good])) == len(good)


def test_odd_values_keep_the_record():
    good = load_fixture("bl1_2026_md9")
    matches = parse_matches([_damaged(lastUpdateDateTime="0001-01-01T00:00:00"), _damaged(leagueSeason="²"), *good])
    assert len(matches) == len(good) + 2
    assert matches[0].last_update is None and matches[0].kickoff is not None
    assert matches[1].season == 0
    assert parse_local_datetime("0001-01-01T00:00:00") is None


@pytest.mark.parametrize("raw", [None, {}, "unsinn", 3])
def test_non_list_input_gives_nothing(raw):
    assert parse_matches(raw) == () and parse_table(raw) == () and parse_scorers(raw) == ()


def test_null_lists_are_tolerated():
    raw = dict(
        next(m for m in load_fixture("bl1_2026_md9") if m["matchID"] == 83234), matchResults=None, goals=None
    )
    m = parse_match(raw)
    assert m is not None and m.final is None and m.goals == ()
    assert m.finished and m.result_unclear  # beendet, aber ohne Ergebnis
    m = parse_match(dict(raw, matchResults=[]))
    assert m.final is None and m.result_unclear


def _result(result_id, kind, home, away):
    return {"resultID": result_id, "resultName": kind, "pointsTeam1": home, "pointsTeam2": away,
            "resultOrderID": 2, "resultTypeID": 2, "resultTypeKind": kind, "resultDescription": ""}


def test_conflicting_result_without_goals_takes_smallest_id_and_is_unclear():
    raw = next(m for m in load_fixture("bl1_2026_md9") if m["matchID"] == 83234)
    m = parse_match(dict(raw, goals=[], matchResults=[_result(20, "After90Minutes", 1, 0), _result(10, "After90Minutes", 0, 0)]))
    assert m.final == Score(0, 0) and m.result_unclear


def test_conflicting_regular_result_in_extra_time_match_follows_goals_before_extra_time():
    # Gebaut aus 81843 (2:2 nach 90, 2:5 n.V.): eine zweite, falsche 90-Minuten-Angabe mit kleinerer resultID.
    # Tor 91. Minute (isOvertime true) ist Nachspielzeit, die Tore 98/106/117 (isOvertime false) sind Verlängerung.
    raw = next(m for m in load_fixture("dfb_2026") if m["matchID"] == 81843)
    m = parse_match(dict(raw, matchResults=[*raw["matchResults"], _result(1, "After90Minutes", 1, 2)]))
    assert (m.final, m.final_phase, m.regular) == (Score(2, 5), Phase.EXTRA_TIME, Score(2, 2))
    assert not m.result_unclear


def test_table_and_scorers():
    table = parse_table(load_fixture("bl2_table_2026"))
    assert [r.position for r in table] == list(range(1, len(table) + 1)) and len(table) == 18
    scorers = parse_scorers(load_fixture("bl1_goalgetters_2026"))
    assert [s.goals for s in scorers] == sorted((s.goals for s in scorers), reverse=True)


def test_table_row_fields():
    second = parse_table(load_fixture("bl2_table_2026"))[1]
    assert (second.team.id, second.team.name, second.points) == (79, "1. FC Nürnberg", 24)
    assert (second.won, second.draw, second.lost) == (7, 3, 1)
    assert (second.goals_for, second.goals_against, second.goal_diff, second.matches) == (26, 13, 13, 11)


def test_local_datetime_with_fraction():
    assert parse_local_datetime("2026-11-16T22:34:47.89") == datetime(2026, 11, 16, 21, 34, 47, 890000, tzinfo=UTC)
    assert parse_local_datetime(None) is None and parse_local_datetime("unsinn") is None


def test_local_datetime_in_summer_and_without_fraction():
    assert parse_local_datetime("2026-07-03T09:07:07") == datetime(2026, 7, 3, 7, 7, 7, tzinfo=UTC)
    assert parse_local_datetime("2026-09-10T17:29:43.763") == datetime(2026, 9, 10, 15, 29, 43, 763000, tzinfo=UTC)
