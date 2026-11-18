"""Saison, Wettbewerbe und Tabellenzonen aus der Datendatei."""

import json
from datetime import date

import pytest

from custom_components.fussball_assist import seasons
from custom_components.fussball_assist.seasons import (
    COMPETITION_KEYS,
    Kind,
    Zone,
    load_season,
    season_for,
    zone_of,
)

from .conftest import load_fixture

REAL_DATA_DIR = seasons.DATA_DIR


@pytest.mark.parametrize(
    ("day", "year"),
    [
        (date(2026, 11, 17), 2026),
        (date(2027, 6, 30), 2026),
        (date(2027, 7, 1), 2027),
        (date(2026, 7, 1), 2026),
    ],
)
def test_season_for(day, year):
    assert season_for(day) == year


def test_load_season_2026():
    season = load_season(2026)
    assert season.label == "2026/27" and tuple(season.competitions) == COMPETITION_KEYS
    assert season.competitions["nationalmannschaft"].shortcuts == ("nla", "DFBN")
    assert season.competitions["dfb_pokal"].has_table is False


def test_missing_season_file_is_none():
    assert load_season(2031) is None


def test_zones_second_division():
    comp = load_season(2026).competitions["2_bundesliga"]
    assert zone_of(comp, 1) == "Aufstiegsplatz"
    assert zone_of(comp, 9) is None
    assert zone_of(comp, 18) == "Abstiegsplatz"


def test_every_shortcut_exists_in_the_league_list():
    known = {(l["leagueShortcut"], l["leagueSeason"]) for l in load_fixture("leagues")}
    for comp in load_season(2026).competitions.values():
        assert all((s, "2026") in known for s in comp.shortcuts)


def _zones(key):
    comp = load_season(2026).competitions[key]
    return [zone_of(comp, place) for place in range(1, 21)]


def test_zones_bundesliga():
    zones = _zones("bundesliga")
    assert zones[:15] == [None] * 15
    assert zones[15] == "Relegationsplatz gegen den Abstieg"
    assert zones[16:18] == ["Abstiegsplatz"] * 2
    assert zones[18:] == [None, None]  # Plätze 19 und 20 gibt es nicht


def test_zones_second_division_in_full():
    zones = _zones("2_bundesliga")
    assert zones[:2] == ["Aufstiegsplatz"] * 2
    assert zones[2] == "Relegationsplatz für den Aufstieg"
    assert zones[3:15] == [None] * 12
    assert zones[15] == "Relegationsplatz gegen den Abstieg"
    assert zones[16:18] == ["Abstiegsplatz"] * 2


def test_zones_third_division():
    zones = _zones("3_liga")
    assert zones[:2] == ["Aufstiegsplatz"] * 2
    assert zones[2] == "Relegationsplatz für den Aufstieg"
    assert zones[3:16] == [None] * 13
    assert zones[16:20] == ["Abstiegsplatz"] * 4


@pytest.mark.parametrize("key", COMPETITION_KEYS)
def test_zone_labels_are_unique_per_competition(key):
    labels = [zone.label for zone in load_season(2026).competitions[key].zones]
    assert len(labels) == len(set(labels))
    assert set(labels) <= {
        "Aufstiegsplatz",
        "Relegationsplatz für den Aufstieg",
        "Relegationsplatz gegen den Abstieg",
        "Abstiegsplatz",
    }


def test_zones_womens_bundesliga():
    zones = _zones("frauen_bundesliga")
    assert zones[:12] == [None] * 12
    assert zones[12:14] == ["Abstiegsplatz"] * 2


@pytest.mark.parametrize("key", ["dfb_pokal", "champions_league", "europa_league", "conference_league", "nationalmannschaft"])
def test_no_zones_outside_the_german_leagues(key):
    comp = load_season(2026).competitions[key]
    assert comp.zones == ()
    assert all(zone_of(comp, place) is None for place in range(1, 37))


def test_zone_of_ignores_places_outside_the_table():
    comp = load_season(2026).competitions["3_liga"]
    assert zone_of(comp, 0) is None and zone_of(comp, -1) is None and zone_of(comp, 21) is None


def test_every_german_league_names_its_source():
    raw = json.loads((seasons.DATA_DIR / "saison_2026.json").read_text(encoding="utf-8"))
    for key in ("bundesliga", "2_bundesliga", "3_liga", "frauen_bundesliga"):
        source = raw["competitions"][key]["zonen_quelle"]
        assert "https://" in source and "abgerufen 2026-11-17" in source


def test_kinds_and_flags():
    comps = load_season(2026).competitions
    assert [comps[k].kind for k in COMPETITION_KEYS] == [
        Kind.LEAGUE, Kind.LEAGUE, Kind.LEAGUE, Kind.CUP, Kind.EUROPE,
        Kind.EUROPE, Kind.EUROPE, Kind.NATIONAL, Kind.LEAGUE,
    ]
    assert [k for k in COMPETITION_KEYS if comps[k].has_table] == [
        "bundesliga", "2_bundesliga", "3_liga", "champions_league",
        "europa_league", "conference_league", "frauen_bundesliga",
    ]
    assert [k for k in COMPETITION_KEYS if comps[k].women] == ["frauen_bundesliga"]
    assert comps["frauen_bundesliga"].shortcuts == ("ffb1",)
    assert comps["nationalmannschaft"].national_team_ids == {"nla": 725, "DFBN": 139}
    assert comps["bundesliga"].national_team_ids == {}


def test_scorer_lists_follow_the_measurement():
    comps = load_season(2026).competitions
    assert not comps["nationalmannschaft"].has_scorers
    assert all(comps[k].has_scorers for k in COMPETITION_KEYS if k != "nationalmannschaft")
    assert [k for k in COMPETITION_KEYS if comps[k].scorers_incomplete] == ["3_liga", "conference_league"]


def test_league_phase_rounds_match_the_recorded_season_lists():
    comps = load_season(2026).competitions
    for key, shortcut in (("champions_league", "ucl"), ("europa_league", "uel2026"), ("conference_league", "uecl2026")):
        rounds = {m["group"]["groupName"] for m in load_fixture(f"{shortcut}_2026") if "Spieltag" in m["group"]["groupName"]}
        assert comps[key].league_phase_rounds == len(rounds)
    assert [comps[k].league_phase_rounds for k in ("champions_league", "europa_league", "conference_league")] == [8, 8, 6]
    assert all(comps[k].league_phase_rounds is None for k in COMPETITION_KEYS if comps[k].kind is not Kind.EUROPE)


def test_season_is_immutable():
    season = load_season(2026)
    comp = season.competitions["nationalmannschaft"]
    with pytest.raises(TypeError):
        season.competitions["x"] = comp
    with pytest.raises(TypeError):
        comp.national_team_ids["x"] = 1
    with pytest.raises(AttributeError):
        comp.name = "anders"
    assert isinstance(season.competitions["3_liga"].zones, tuple)
    assert Zone("Aufstiegsplatz", 1, 2) in season.competitions["3_liga"].zones


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(seasons, "DATA_DIR", tmp_path)
    return tmp_path


def _good():
    return json.loads((REAL_DATA_DIR / "saison_2026.json").read_text(encoding="utf-8"))


def _write(data_dir, content, year=2026):
    (data_dir / f"saison_{year}.json").write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def test_invalid_json_is_none(data_dir):
    _write(data_dir, "{kaputt")
    assert load_season(2026) is None


def test_binary_garbage_is_none(data_dir):
    (data_dir / "saison_2026.json").write_bytes(b"\xff\xfe\x00")
    assert load_season(2026) is None


def test_unreadable_path_is_none(data_dir):
    (data_dir / "saison_2026.json").mkdir()
    assert load_season(2026) is None


@pytest.mark.parametrize("content", ["[]", "null", "42", '"text"'])
def test_wrong_top_level_is_none(data_dir, content):
    _write(data_dir, content)
    assert load_season(2026) is None


def _mutations():
    def drop(key):
        def f(d):
            del d["competitions"][key]
        return f

    def set_comp(key, field, value):
        def f(d):
            d["competitions"][key][field] = value
        return f

    def drop_field(key, field):
        def f(d):
            del d["competitions"][key][field]
        return f

    return {
        "competition_missing": drop("dfb_pokal"),
        "field_missing": drop_field("bundesliga", "zones"),
        "label_wrong_type": lambda d: d.update(label=2026),
        "year_mismatch": lambda d: d.update(year=2027),
        "unknown_kind": set_comp("bundesliga", "kind", "turnier"),
        "shortcuts_empty": set_comp("bundesliga", "shortcuts", []),
        "shortcut_not_text": set_comp("bundesliga", "shortcuts", [1]),
        "flag_not_bool": set_comp("bundesliga", "has_table", "ja"),
        "zone_reversed": set_comp("bundesliga", "zones", [{"label": "Abstiegsplatz", "first": 18, "last": 17}]),
        "zone_zero": set_comp("bundesliga", "zones", [{"label": "Abstiegsplatz", "first": 0, "last": 2}]),
        "zone_overlap": set_comp("bundesliga", "zones", [{"label": "a", "first": 1, "last": 3}, {"label": "b", "first": 3, "last": 4}]),
        "zone_label_empty": set_comp("bundesliga", "zones", [{"label": "", "first": 1, "last": 2}]),
        "zone_bool": set_comp("bundesliga", "zones", [{"label": "a", "first": True, "last": 2}]),
        "rounds_wrong_type": set_comp("champions_league", "league_phase_rounds", "8"),
        "team_ids_wrong": set_comp("nationalmannschaft", "national_team_ids", {"nla": "725"}),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_invalid_content_is_none(data_dir, name):
    data = _good()
    _mutations()[name](data)
    _write(data_dir, data)
    assert load_season(2026) is None


def test_extra_fields_are_ignored(data_dir):
    data = _good()
    data["bemerkung"] = "egal"
    data["competitions"]["bundesliga"]["zonen_quelle"] = "anders"
    data["competitions"]["bundesliga"]["unbekannt"] = 1
    _write(data_dir, data)
    assert load_season(2026).competitions["bundesliga"].name == "Bundesliga"


def test_non_integer_year_is_none():
    assert load_season("2026") is None
    assert load_season(2026.0) is None
    assert load_season(True) is None
    assert load_season(-1) is None


def test_missing_file_is_silent(caplog):
    with caplog.at_level("DEBUG", logger=seasons.__name__):
        assert load_season(2031) is None
    assert caplog.records == []


def test_invalid_file_is_logged_with_path_and_reason(data_dir, caplog):
    _write(data_dir, "{kaputt")
    with caplog.at_level("WARNING", logger=seasons.__name__):
        assert load_season(2026) is None
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert str(data_dir / "saison_2026.json") in record.getMessage()
    assert "Expecting property name" in record.getMessage()


def test_invalid_content_is_logged(data_dir, caplog):
    data = _good()
    data["competitions"]["bundesliga"]["kind"] = "turnier"
    _write(data_dir, data)
    with caplog.at_level("WARNING", logger=seasons.__name__):
        assert load_season(2026) is None
    assert "turnier" in caplog.text


def test_unreadable_file_is_logged(data_dir, caplog):
    (data_dir / "saison_2026.json").mkdir()
    with caplog.at_level("WARNING", logger=seasons.__name__):
        assert load_season(2026) is None
    assert "saison_2026.json" in caplog.text
