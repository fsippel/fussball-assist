"""Antwort "Torjäger": Plätze, Spielersuche und Hinweise aus den Aufzeichnungen (Bundesliga, Conference League)."""

import dataclasses
from datetime import datetime
from types import MappingProxyType

import pytest

from custom_components.fussball_assist.logic_torjaeger import answer
from custom_components.fussball_assist.models import Scorer
from custom_components.fussball_assist.results import ErrorKind, Settings, ToolError
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import BERLIN, SETTINGS_CLUB, assert_speakable, make_index, make_snapshot

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)  # Dienstag
NO_CLUB = "Die Liste nennt keinen Verein."
SETTINGS_NO_CLUB = Settings(competitions=COMPETITION_KEYS, main_team=None, favorite_teams=())
LAST_PLACE_SHARED = "Auf dem letzten genannten Platz stehen noch weitere Spieler mit {goals} Toren."
INITIAL_ONLY = (
    "Bei „{name}“ nennt die Liste nur den Anfangsbuchstaben des Vornamens (oder nur den Nachnamen);"
    " ob es derselbe Spieler ist, kann ich nicht sicher sagen."
)
PARTIAL = "Bei „{name}“ passt nur ein Teil des Namens; ob es derselbe Spieler ist, kann ich nicht sicher sagen."
SHOOTOUT = "In einem Pokal können die Zahlen auch Tore aus dem Elfmeterschießen enthalten."


def ask(snapshot=None, settings=SETTINGS_NO_CLUB, **kwargs):
    """Fragt mit den Vorgaben des Werkzeugs; jedes Ergebnis muss vorlesbar sein."""
    snapshot = snapshot or make_snapshot(NOW)
    params = {"wettbewerb": "bundesliga", "spieler": None, "anzahl": 5}
    params.update(kwargs)
    result = answer(snapshot, make_index(snapshot), settings, **params)
    assert_speakable(result)
    return result


def error_of(**kwargs):
    with pytest.raises(ToolError) as caught:
        ask(**kwargs)
    return caught.value


def with_scorers(key, *scorers):
    snapshot = make_snapshot(NOW)
    return dataclasses.replace(
        snapshot, scorers=MappingProxyType(dict(snapshot.scorers) | {key: tuple(Scorer(n, g) for n, g in scorers)})
    )


def places(result):
    return [row["platz"] for row in result["torjaeger"]]


def names(result):
    return [row["name"] for row in result["torjaeger"]]


def test_top_five_by_default():
    result = ask()
    assert result["wettbewerb"] == "Bundesliga" and result["stand"] == "17. November, 16:40 Uhr"
    assert result["torjaeger"] == [
        {"platz": 1, "name": "N. Füllkrug", "tore": 9},
        {"platz": 2, "name": "P. Schick", "tore": 8},
        {"platz": 3, "name": "M. Olise", "tore": 6},
        {"platz": 3, "name": "Younes Ebnoutalib", "tore": 6},
        {"platz": 3, "name": "P. Tietz", "tore": 6},
    ]
    assert result["hinweise"] == [NO_CLUB]


def test_equal_goals_share_the_place_and_the_next_place_skips():
    result = ask(anzahl=10)
    assert places(result) == [1, 2, 3, 3, 3, 6, 6, 6, 6, 6]
    assert [row["tore"] for row in result["torjaeger"]] == [9, 8, 6, 6, 6, 5, 5, 5, 5, 5]
    assert result["hinweise"] == [NO_CLUB, LAST_PLACE_SHARED.format(goals=5)]


def test_cut_through_a_group_of_equals_is_said():
    result = ask(anzahl=4)
    assert places(result) == [1, 2, 3, 3]
    assert result["hinweise"] == [NO_CLUB, LAST_PLACE_SHARED.format(goals=6)]
    result = ask(snapshot=with_scorers("bundesliga", ("A", 3), ("B", 2), ("C", 2), ("D", 1)), anzahl=1)
    assert result["torjaeger"] == [{"platz": 1, "name": "A", "tore": 3}] and result["hinweise"] == [NO_CLUB]
    result = ask(snapshot=with_scorers("bundesliga", ("A", 1), ("B", 1)), anzahl=1)
    assert result["hinweise"] == [NO_CLUB, "Auf dem letzten genannten Platz stehen noch weitere Spieler mit 1 Tor."]


def test_list_never_names_a_club():
    result = ask(anzahl=10)
    assert all(set(row) == {"platz", "name", "tore"} for row in result["torjaeger"])
    assert NO_CLUB in result["hinweise"]


def test_player_search_handles_abbreviated_first_names():
    expected = [{"platz": 1, "name": "N. Füllkrug", "tore": 9}]
    for text in ("Füllkrug", "Niclas Füllkrug", "füllkrug", "N. Füllkrug", "N Fuellkrug", "  Füllkrug  "):
        assert ask(spieler=text)["torjaeger"] == expected, text


def test_player_search_keeps_the_place_of_the_whole_list():
    result = ask(spieler="Tietz")
    assert result["torjaeger"] == [{"platz": 3, "name": "P. Tietz", "tore": 6}]
    assert result["hinweise"] == [NO_CLUB]
    assert ask(spieler="Younes Ebnoutalib")["torjaeger"] == [{"platz": 3, "name": "Younes Ebnoutalib", "tore": 6}]
    assert ask(spieler="Luis Diaz")["torjaeger"] == [{"platz": 12, "name": "Luis Díaz", "tore": 4}]


def test_player_search_distinguishes_first_names():
    snapshot = with_scorers("bundesliga", ("N. Müller", 4), ("Nico Müller", 3), ("Thomas Müller", 3), ("T. Müller", 1), ("Romulo", 2))
    assert names(ask(snapshot=snapshot, spieler="Müller")) == ["N. Müller", "Nico Müller", "Thomas Müller", "T. Müller"]
    assert names(ask(snapshot=snapshot, spieler="Thomas Müller")) == ["Thomas Müller", "T. Müller"]
    assert names(ask(snapshot=snapshot, spieler="Niclas Müller")) == ["N. Müller"]  # Nico ist ein anderer Vorname
    assert names(ask(snapshot=snapshot, spieler="Nico Müller")) == ["N. Müller", "Nico Müller"]
    assert names(ask(snapshot=snapshot, spieler="Romulo")) == ["Romulo"]
    assert names(ask(snapshot=snapshot, spieler="Muller")) == names(ask(snapshot=snapshot, spieler="Müller"))


def test_several_matches_are_all_listed_regardless_of_the_count():
    snapshot = with_scorers("bundesliga", *[(f"{c}. Meier", 12 - i) for i, c in enumerate("ABCDEFGH")])
    result = ask(snapshot=snapshot, spieler="Meier", anzahl=1)
    assert len(result["torjaeger"]) == 8 and places(result) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert names(ask(snapshot=snapshot, spieler="C. Meier", anzahl=1)) == ["C. Meier"]


def test_names_with_comma_and_blanks_and_hyphens():
    snapshot = with_scorers("bundesliga", (" Poulsen, Yussuf", 3), ("Camara ", 2), ("Han-Noah Massengo", 2))
    result = ask(snapshot=snapshot, anzahl=10)
    assert names(result) == ["Yussuf Poulsen", "Camara", "Han-Noah Massengo"]
    assert names(ask(snapshot=snapshot, spieler="Yussuf Poulsen")) == ["Yussuf Poulsen"]
    assert names(ask(snapshot=snapshot, spieler="Poulsen")) == ["Yussuf Poulsen"]
    assert names(ask(snapshot=snapshot, spieler="Han-Noah Massengo")) == ["Han-Noah Massengo"]
    assert names(ask(snapshot=snapshot, spieler="Massengo")) == ["Han-Noah Massengo"]
    assert names(ask(snapshot=snapshot, spieler="Camara")) == ["Camara"]


def test_player_not_in_the_list():
    result = ask(spieler="Niemand")
    assert result["torjaeger"] == []
    assert result["hinweise"] == [NO_CLUB, "„Niemand“ steht nicht in der Torjägerliste."]


def test_not_found_in_an_incomplete_list_is_not_certain():
    result = ask(wettbewerb="conference_league", spieler="Niemand")
    assert result["torjaeger"] == []
    assert not any("steht nicht in der Torjägerliste" in hint for hint in result["hinweise"])
    assert any("Niemand" in hint and "nichts Sicheres" in hint for hint in result["hinweise"])
    assert not any("getroffen" in hint for hint in result["hinweise"])


def test_incomplete_list_is_flagged():
    result = ask(wettbewerb="conference_league")
    assert result["wettbewerb"] == "Conference League"
    assert places(result) == [1, 1, 1, 1, 1]
    assert result["hinweise"][0] == NO_CLUB
    assert (
        "Die Torjägerliste von Conference League ist unvollständig und deckt nur einen kleinen Teil der Tore ab."
        in result["hinweise"]
    )
    # auch bei einem Treffer bleibt der Hinweis
    result = ask(wettbewerb="conference_league", spieler="Ansu Fati")
    assert result["torjaeger"] == [{"platz": 1, "name": "Ansu Fati", "tore": 1}]
    assert any("unvollständig" in hint for hint in result["hinweise"])


def test_incomplete_league_list_says_not_all_goals():
    # 3. Liga: unvollständig, aber kein Europapokal
    result = ask(snapshot=with_scorers("3_liga", ("A. Test", 4)), wettbewerb="3_liga")
    assert "Die Torjägerliste von 3. Liga ist unvollständig und deckt nicht alle Tore ab." in result["hinweise"]


def test_cup_list_may_count_shootout_goals():
    result = ask(snapshot=with_scorers("dfb_pokal", ("A. Test", 4), ("B. Test", 2)), wettbewerb="dfb_pokal")
    assert result["wettbewerb"] == "DFB-Pokal" and SHOOTOUT in result["hinweise"]
    assert SHOOTOUT not in ask()["hinweise"]


def test_count_is_limited():
    for value in (11, 0, -1, 2.5, "viele", True):
        error = error_of(anzahl=value)
        assert error.kind is ErrorKind.INVALID_INPUT and "1 bis 10" in error.text, value
    assert len(ask(anzahl=1)["torjaeger"]) == 1
    assert len(ask(anzahl=10)["torjaeger"]) == 10
    assert ask(anzahl=None)["torjaeger"] == ask(anzahl=5)["torjaeger"]
    assert ask(anzahl=3.0)["torjaeger"] == ask(anzahl=3)["torjaeger"]
    assert ask(anzahl="4")["torjaeger"] == ask(anzahl=4)["torjaeger"]
    # auch mit Spieler wird die Anzahl geprüft
    assert error_of(spieler="Füllkrug", anzahl=11).kind is ErrorKind.INVALID_INPUT


def test_competition_without_list():
    error = error_of(wettbewerb="nationalmannschaft")
    assert error.kind is ErrorKind.UNSUPPORTED
    assert "Nationalmannschaft" in error.text and "Torjägerliste" in error.text
    assert "Eine Torjägerliste gibt es für: bundesliga," in error.text
    assert "nationalmannschaft" not in error.text.split("gibt es für:")[1]
    assert "Spielen von Nationalmannschaft" in error.text


def test_unknown_or_unchosen_competition():
    error = error_of(wettbewerb="Kreisliga")
    assert error.kind is ErrorKind.INVALID_INPUT and "Gültig sind: bundesliga," in error.text
    assert "nationalmannschaft" not in error.text
    settings = Settings(competitions=("bundesliga",), main_team=None, favorite_teams=())
    error = error_of(settings=settings, wettbewerb="dfb_pokal")
    assert error.kind is ErrorKind.INVALID_INPUT and "nicht ausgewählt" in error.text


def test_competition_by_name_and_empty_means_default():
    assert ask(wettbewerb="Bundesliga")["wettbewerb"] == "Bundesliga"
    assert ask(wettbewerb="")["wettbewerb"] == "Bundesliga"  # keine Angabe, Bundesliga ohne Hauptverein
    assert ask(wettbewerb=None, spieler="  ")["torjaeger"][0]["name"] == "N. Füllkrug"


def test_default_is_the_league_of_the_main_team():
    snapshot = with_scorers("2_bundesliga", ("A. Test", 7), ("B. Test", 7), ("C. Test", 5))
    result = ask(snapshot=snapshot, settings=SETTINGS_CLUB, wettbewerb=None)
    assert result["wettbewerb"] == "2. Bundesliga"
    assert places(result) == [1, 1, 3]
    assert result["hinweise"] == [NO_CLUB]


def test_main_team_in_no_table_falls_back_to_the_bundesliga_with_a_reason():
    settings = Settings(competitions=COMPETITION_KEYS, main_team=999999, favorite_teams=())
    result = ask(settings=settings, wettbewerb=None)
    assert result["wettbewerb"] == "Bundesliga"
    assert any("Hauptverein" in hint and "Bundesliga" in hint for hint in result["hinweise"])


def test_scorers_not_loaded_is_a_loading_error_not_a_certain_answer():
    # Hauptverein spielt in der 2. Bundesliga; deren Torjägerliste ist nicht geladen
    error = error_of(settings=SETTINGS_CLUB, wettbewerb=None)
    assert error.kind is ErrorKind.UNREACHABLE and "noch nicht" in error.text and "2. Bundesliga" in error.text
    for spieler in (None, "Niemand"):
        error = error_of(settings=SETTINGS_CLUB, wettbewerb="2_bundesliga", spieler=spieler)
        assert error.kind is ErrorKind.UNREACHABLE and "gleich noch einmal" in error.text


def test_unreachable_and_not_yet_loaded_and_invalid_sources():
    snapshot = make_snapshot(NOW, unreachable=frozenset({"bundesliga"}))
    error = error_of(snapshot=snapshot, spieler="Niemand")
    assert error.kind is ErrorKind.UNREACHABLE and "nicht erreichbar" in error.text
    snapshot = make_snapshot(NOW, loaded=frozenset(COMPETITION_KEYS) - {"bundesliga"})
    error = error_of(snapshot=snapshot, spieler="Niemand")
    assert error.kind is ErrorKind.UNREACHABLE and "noch nicht" in error.text
    snapshot = make_snapshot(NOW, invalid_shortcuts=frozenset({"bl1"}))
    error = error_of(snapshot=snapshot, spieler="Niemand")
    assert error.kind is ErrorKind.SEASON_MISSING


def test_empty_list_of_a_loaded_competition_is_nothing_entered():
    snapshot = with_scorers("bundesliga")
    for spieler in (None, "Füllkrug"):
        error = error_of(snapshot=snapshot, spieler=spieler)
        assert error.kind is ErrorKind.NOTHING_ENTERED and "Bundesliga" in error.text
        assert "nicht begonnen" not in error.text  # es sind schon Spiele beendet
    before = make_snapshot(datetime(2026, 8, 1, 12, 0, tzinfo=BERLIN))  # vor dem ersten Spiel
    early = dataclasses.replace(before, scorers=MappingProxyType({**before.scorers, "bundesliga": ()}))
    assert "die Saison hat vielleicht noch nicht begonnen" in error_of(snapshot=early).text


def test_a_name_without_letters_is_invalid():
    assert error_of(spieler="...").kind is ErrorKind.INVALID_INPUT


def test_ended_season_is_named():
    snapshot = make_snapshot(NOW)
    ended = dataclasses.replace(snapshot, ended_season=MappingProxyType({"bundesliga": snapshot.season}))
    result = ask(snapshot=ended)
    assert any(hint.startswith("Endstand der Saison") and "Bundesliga" in hint for hint in result["hinweise"])


def test_full_first_name_matching_only_an_initial_is_not_stated_as_certain():
    result = ask(spieler="Enzo Fernández")
    assert names(result) == ["E. Fernández"]
    assert result["hinweise"] == [NO_CLUB, INITIAL_ONLY.format(name="E. Fernández")]
    result = ask(spieler="Harry Kane")
    assert names(result) == ["H. Kane"]
    assert result["hinweise"] == [NO_CLUB, INITIAL_ONLY.format(name="H. Kane")]
    for text in ("Kane", "H. Kane", "H Kane"):
        result = ask(spieler=text)
        assert names(result) == ["H. Kane"] and result["hinweise"] == [NO_CLUB], text


def test_entry_without_first_name_is_not_stated_as_certain():
    result = ask(wettbewerb="conference_league", spieler="Fabian Brunner")
    assert names(result) == ["Brunner"]
    assert INITIAL_ONLY.format(name="Brunner") in result["hinweise"]
    result = ask(wettbewerb="conference_league", spieler="Brunner")
    assert names(result) == ["Brunner"]
    assert INITIAL_ONLY.format(name="Brunner") not in result["hinweise"]


def test_wrong_first_name_is_no_match():
    assert names(ask(spieler="Keven Schlotterbeck")) == []
    result = ask(spieler="Nils Schlotterbeck")  # nur der Anfangsbuchstabe passt: Treffer mit Hinweis
    assert names(result) == ["N. Schlotterbeck"]
    assert INITIAL_ONLY.format(name="N. Schlotterbeck") in result["hinweise"]
    assert names(ask(spieler="Müller")) == []


def test_multi_part_surnames():
    assert names(ask(spieler="El Mala")) == ["S. El Mala", "Malek El Mala"]
    assert ask(spieler="El Mala")["hinweise"] == [NO_CLUB]
    assert names(ask(spieler="Malek El Mala")) == ["Malek El Mala"]
    assert names(ask(spieler="Sven El Mala")) == ["S. El Mala"]
    assert names(ask(spieler="Da Costa")) == ["Danny da Costa"]
    assert names(ask(spieler="Danny da Costa")) == ["Danny da Costa"]
    assert names(ask(spieler="Costa")) == ["Danny da Costa"]
    assert names(ask(spieler="Latte Lath")) == ["Emmanuel Latte Lath"]
    assert names(ask(spieler="Emmanuel Latte Lath")) == ["Emmanuel Latte Lath"]
    assert names(ask(spieler="Lath")) == ["Emmanuel Latte Lath"]
    assert names(ask(spieler="Han-Noah Massengo")) == ["Han-Noah Massengo"]
    assert names(ask(spieler="Han Noah Massengo")) == ["Han-Noah Massengo"]
    assert ask(spieler="Latte Lath")["hinweise"] == [NO_CLUB]


def test_scandinavian_letters_and_umlauts():
    for text in ("Grönbäk", "Gronbaek", "Grønbæk", "A. Gronbaek", "Anders Grønbæk"):
        assert names(ask(spieler=text)) == ["A. Grønbæk"], text
    assert ask(spieler="Grönbäk")["hinweise"] == [NO_CLUB]
    assert INITIAL_ONLY.format(name="A. Grønbæk") in ask(spieler="Anders Grønbæk")["hinweise"]


def test_search_by_the_start_of_a_longer_name_is_flagged():
    result = ask(spieler="Lee")
    assert names(result) == ["Lee Jae-sung"]
    assert result["hinweise"] == [NO_CLUB, PARTIAL.format(name="Lee Jae-sung")]
    result = ask(spieler="Mokwa")  # derselbe Spieler steht doppelt in der Liste
    assert names(result) == ["David Mokwa Ntusu", "D. Mokwa"]
    assert result["hinweise"] == [NO_CLUB, PARTIAL.format(name="David Mokwa Ntusu")]
