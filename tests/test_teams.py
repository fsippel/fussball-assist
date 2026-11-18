"""Vereinsnamen, Spitznamen und Nationalmannschaft auflösen."""

import dataclasses
import json
import logging

import pytest

from custom_components.fussball_assist import teams
from custom_components.fussball_assist.parse import parse_matches
from custom_components.fussball_assist.seasons import load_season
from custom_components.fussball_assist.teams import (
    NATIONAL_TEAM,
    Resolution,
    TeamIndex,
    load_aliases,
    normalize,
)

from .conftest import load_fixture


@pytest.fixture
def index():
    season = load_season(2026)
    matches = {
        "bundesliga": parse_matches(load_fixture("bl1_2026_md9")),
        "2_bundesliga": parse_matches(load_fixture("bl2_2026")),
        "champions_league": parse_matches(load_fixture("ucl_2026")),
        "frauen_bundesliga": parse_matches(load_fixture("ffb1_2026_md10")),
    }
    return TeamIndex.build(matches, season, load_aliases())


@pytest.mark.parametrize(
    "text", ["Club", "der Club", "Glubb", "FCN", "Nürnberg", "nuernberg", "1. FC Nürnberg", "  club  "]
)
def test_club_resolves_to_nuremberg(index, text):
    assert index.resolve(text).team.id == 79


def test_club_name_means_the_mens_team(index):
    assert index.resolve("Bayern").team.id == 40 and index.resolve("Nürnberg").team.id == 79
    assert all(t.id not in (6063, 6066) for t in index.selectable())


def test_ambiguous_name_lists_candidates(index):
    res = index.resolve("Borussia")
    assert res.team is None and len(res.candidates) >= 2


def test_candidates_are_sorted_by_name_and_distinct(index):
    res = index.resolve("Borussia")
    names = [t.name for t in res.candidates]
    assert names == sorted(names)
    assert len({t.id for t in res.candidates}) == len(res.candidates)
    assert {7, 87} <= {t.id for t in res.candidates}


@pytest.mark.parametrize("text", ["Sechzig", "TSV 1860 München", "xyz", ""])
def test_unknown_team(index, text):
    res = index.resolve(text)
    assert res.team is None and not res.candidates and not res.national


@pytest.mark.parametrize("text", [None, "   ", "...", 42])
def test_garbage_input_never_raises(index, text):
    res = index.resolve(text)
    assert res.team is None and not res.candidates and not res.national


@pytest.mark.parametrize(
    "text",
    [
        "Deutschland",
        "Nationalmannschaft",
        "die Nationalmannschaft",
        "DFB-Elf",
        "DFB Team",
        "die Mannschaft",
        "Deutsche Nationalmannschaft",
    ],
)
def test_national_team(index, text):
    res = index.resolve(text)
    assert res.national and res.team is None and not res.candidates and not res.foreign


def test_national_marker():
    assert NATIONAL_TEAM == "nationalmannschaft"


def test_foreign_club_is_flagged(index):
    foreign = next(t for t in index.selectable() if index.resolve(t.name).foreign)
    assert index.resolve(foreign.name).team.id == foreign.id


def test_foreign_only_for_european_only_clubs(index):
    assert index.resolve("Liverpool").foreign
    assert not index.resolve("Dortmund").foreign  # spielt Bundesliga und Champions League
    assert not index.resolve("Club").foreign
    assert not index.resolve("Borussia").foreign


def test_selectable_sorted_by_name_without_national_or_women(index):
    sel = index.selectable()
    names = [t.name for t in sel]
    assert names == sorted(names)
    ids = [t.id for t in sel]
    assert len(set(ids)) == len(ids)
    assert not any("Frauen" in n for n in names)
    assert {40, 79, 370} <= set(ids)


def test_by_id(index):
    assert index.by_id(79).name == "1. FC Nürnberg"
    assert index.by_id(6063) is None
    assert index.by_id(-1) is None


def test_word_match_unique_and_exact_beats_words(index):
    assert index.resolve("Liverpool").team.id == 370  # Wort im Namen
    assert index.resolve("Dortmund").team.id == 7
    assert index.resolve("Werkself").team.id == 6
    assert index.resolve("Gladbach").team.id == 87
    assert index.resolve("Kleeblatt").team.id == 115
    assert index.resolve("BVB").team.id == 7
    assert index.resolve("Fohlen").team.id == 87


def test_eintracht_is_ambiguous(index):
    res = index.resolve("Eintracht")
    assert res.team is None
    assert {91, 74} <= {t.id for t in res.candidates}


def test_alias_matching_two_clubs_gives_candidates():
    from custom_components.fussball_assist.models import Team

    a, b = Team(1, "Alpha FC", "Alpha", None), Team(2, "Beta FC", "Beta", None)
    idx = TeamIndex._from_teams([a, b], set(), {"1": ["Doppel"], "2": ["Doppel"]})
    res = idx.resolve("doppel")
    assert res.team is None and res.candidates == (a, b)


def test_normalize():
    assert normalize("  Ärger Öl Übung Straße  ") == "aerger oel uebung strasse"
    assert normalize("1. FC Nürnberg") == "1 fc nuernberg"
    assert normalize("der Club") == "club"
    assert normalize("Die Bayern") == "bayern"
    assert normalize("Den Fohlen") == "fohlen"
    assert normalize("Das Kleeblatt") == "kleeblatt"
    assert normalize("A. Grønbæk") == "a groenbaek"  # Nordische Buchstaben bleiben Buchstaben
    assert normalize("DFB-Elf") == "dfb elf"
    assert normalize("St. Pauli") == "st pauli"
    assert normalize("Man'City") == "mancity"
    assert normalize("Fenerbahçe") == "fenerbahce"
    assert normalize("die") == "die"
    assert normalize("") == ""
    assert normalize("...") == ""


def test_resolution_is_frozen():
    res = Resolution(None, False, (), False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.national = True


def test_load_aliases_required_entries():
    aliases = load_aliases()
    assert aliases["79"] == ("Club", "Glubb", "FCN", "Nürnberg")
    assert aliases["40"][:3] == ("Bayern", "FCB", "die Bayern")
    assert aliases["7"][:2] == ("BVB", "Dortmund")
    assert aliases["6"][:2] == ("Werkself", "Bayer")
    assert aliases["87"][:2] == ("Fohlen", "Gladbach")
    assert aliases["115"][:2] == ("Kleeblatt", "Fürth")


def test_aliases_cover_every_club_and_no_women():
    aliases = load_aliases()
    for fixture in ("bl1_2026_md9", "bl2_2026"):
        for m in parse_matches(load_fixture(fixture)):
            for team in (m.home, m.away):
                assert str(team.id) in aliases, team.name
    assert "6063" not in aliases and "6066" not in aliases


def test_borussia_is_no_alias():
    flat = {normalize(a) for names in load_aliases().values() for a in names}
    assert "borussia" not in flat and "eintracht" not in flat and "vfl" not in flat


def test_aliases_do_not_collide_between_clubs():
    owners: dict[str, set[str]] = {}
    for team_id, names in load_aliases().items():
        for name in names:
            owners.setdefault(normalize(name), set()).add(team_id)
    assert {k: v for k, v in owners.items() if len(v) > 1} == {}


def test_load_aliases_broken_file_returns_empty(tmp_path, monkeypatch, caplog):
    (tmp_path / "spitznamen.json").write_text("{kaputt", encoding="utf-8")
    monkeypatch.setattr(teams, "DATA_DIR", tmp_path)
    with caplog.at_level(logging.WARNING):
        assert load_aliases() == {}
    assert "nicht lesbar" in caplog.text
    caplog.clear()
    (tmp_path / "spitznamen.json").write_text(json.dumps({"1": "kein Liste", "2": ["ok", 3]}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_aliases() == {}
    assert "ungültig" in caplog.text
    caplog.clear()
    monkeypatch.setattr(teams, "DATA_DIR", tmp_path / "gibtsnicht")
    with caplog.at_level(logging.WARNING):
        assert load_aliases() == {}
    assert "nicht gefunden" in caplog.text


@pytest.fixture
def full_index():
    season = load_season(2026)
    matches = {
        "bundesliga": parse_matches(load_fixture("bl1_2026_md9")),
        "2_bundesliga": parse_matches(load_fixture("bl2_2026")),
        "3_liga": parse_matches(load_fixture("bl3_2026")),
        "dfb_pokal": parse_matches(load_fixture("dfb_2026")),
        "champions_league": parse_matches(load_fixture("ucl_2026")),
    }
    return TeamIndex.build(matches, season, load_aliases())


@pytest.mark.parametrize(
    ("text", "ids"),
    [
        ("1. FC Saarbrücken", {417, 3078}),
        ("Saarbrücken", {417, 3078}),
        ("Würzburger Kickers", {398, 5276}),
        ("Würzburg", {398, 5276}),
        ("Kickers", {398, 5276}),
    ],
)
def test_club_with_two_ids_is_one_club(full_index, text, ids):
    res = full_index.resolve(text)
    assert res.team is not None and not res.candidates
    assert full_index.ids_of(res.team) == frozenset(ids)
    assert res.team.id in ids


def test_canonical_team_is_first_in_competition_order(full_index):
    assert full_index.resolve("Saarbrücken").team.id == 417  # 3. Liga vor DFB-Pokal
    assert full_index.by_id(3078).id == 417
    assert full_index.by_id(417).id == 417
    assert full_index.by_id(5276).id == 398


def test_selectable_has_every_club_once(full_index):
    names = [teams.normalize(t.name) for t in full_index.selectable()]
    assert len(set(names)) == len(names)
    assert names.count("1 fc saarbruecken") == 1
    ids = {t.id for t in full_index.selectable()}
    assert 3078 not in ids and 5276 not in ids


def test_ids_of_single_id_club_and_ambiguity_with_merge(full_index):
    assert full_index.ids_of(full_index.by_id(79)) == frozenset({79})
    res = full_index.resolve("Borussia")
    assert len(res.candidates) == len({teams.normalize(t.name) for t in res.candidates})


def test_merged_club_is_domestic_if_any_id_is(full_index):
    assert not full_index.resolve("Saarbrücken").foreign
