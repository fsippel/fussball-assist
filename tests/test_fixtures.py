"""Nagelt die Annahmen über die Datenformen von OpenLigaDB an den aufgezeichneten Antworten fest."""

from .conftest import load_fixture


def test_cup_match_decided_on_penalties():
    match = next(m for m in load_fixture("dfb_2026") if m["matchID"] == 81850)
    kinds = {r["resultTypeKind"]: (r["pointsTeam1"], r["pointsTeam2"]) for r in match["matchResults"]}
    assert kinds["After90Minutes"] == (1, 1)
    assert kinds["AfterPenalties"] == (5, 6)


def test_cup_match_decided_after_extra_time():
    match = next(m for m in load_fixture("dfb_2026") if m["matchID"] == 81843)
    kinds = {r["resultTypeKind"]: (r["pointsTeam1"], r["pointsTeam2"]) for r in match["matchResults"]}
    assert kinds["After90Minutes"] == (2, 2) and kinds["AfterExtraTime"] == (2, 5)


def test_placeholder_matchday_shares_one_kickoff():
    matches = [m for m in load_fixture("bl2_2026") if m["group"]["groupOrderID"] == 19]
    assert len(matches) == 9 and len({m["matchDateTimeUTC"] for m in matches}) == 1


def test_national_team_has_two_ids():
    assert any(139 in (m["team1"]["teamId"], m["team2"]["teamId"]) for m in load_fixture("DFBN_2026"))
    assert any(725 in (m["team1"]["teamId"], m["team2"]["teamId"]) for m in load_fixture("nla_2026"))


def test_reference_points_used_by_later_tasks():
    assert next(m for m in load_fixture("bl1_2026") if m["matchID"] == 83237)["group"]["groupOrderID"] == 10
    assert not any(m["matchIsFinished"] for m in load_fixture("dfb_2026") if m["group"]["groupOrderID"] == 3)


def test_unknown_shortcut_is_empty_list():
    assert load_fixture("empty") == []
