"""Antwort "Die Woche": feste Regeln aus den Aufzeichnungen."""

import dataclasses
import json
import logging
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from custom_components.fussball_assist import logic_woche
from custom_components.fussball_assist.logic_woche import MAX_MATCHES, answer, load_derbies
from custom_components.fussball_assist.models import Score, TableRow, Team
from custom_components.fussball_assist.results import ErrorKind, Settings, ToolError
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import (
    BERLIN,
    SETTINGS_CLUB,
    assert_speakable,
    fetched_earlier,
    make_index,
    make_snapshot,
    replace_match,
)

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)  # Dienstag; die Woche reicht bis Sonntag, 22. November
NO_CLUBS = Settings(competitions=COMPETITION_KEYS, main_team=None, favorite_teams=())
MEN_LEAGUES = Settings(competitions=("bundesliga", "2_bundesliga"), main_team=None, favorite_teams=())

BAYERN = Team(40, "FC Bayern München", "Bayern", None)
DORTMUND = Team(7, "Borussia Dortmund", "Dortmund", None)
FREIBURG = Team(112, "SC Freiburg", "Freiburg", None)
HOFFENHEIM = Team(175, "TSG Hoffenheim", "Hoffenheim", None)
MADRID = Team(1133, "Real Madrid", "Real", None)
INTER = Team(733, "Inter Mailand", "Inter", None)
VILLA = Team(438, "Aston Villa", "Villa", None)


def at(day, hour, minute=0, month=11, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def ask(snapshot=None, settings=SETTINGS_CLUB, derbies=None, woche="diese"):
    """Fragt mit den mitgelieferten Derbys, wenn keine gegeben sind; jedes Ergebnis muss vorlesbar sein."""
    snapshot = snapshot or make_snapshot(NOW)
    result = answer(
        snapshot, make_index(snapshot), settings, load_derbies() if derbies is None else derbies, woche=woche
    )
    assert_speakable(result)
    return result


def reasons(result):
    return [g["grund"] for g in result["spiele"]]


def pairs(result):
    return [(g["heim"], g["gast"]) for g in result["spiele"]]


def hints(result, text):
    return [h for h in result["hinweise"] if text in h]


def moved(snapshot, competition, match_id, kickoff, **changes):
    """Das aufgezeichnete Spiel mit neuem Anstoß, noch nicht beendet, ohne Ergebnis."""
    return replace_match(
        snapshot,
        competition,
        match_id,
        kickoff=kickoff,
        finished=False,
        final=None,
        final_phase=None,
        regular=None,
        halftime=None,
        goals=(),
        **changes,
    )


def test_derbies_come_from_the_data_file():
    derbies = load_derbies()
    assert (79, 115, "Frankenderby") in derbies
    assert len(derbies) >= 2
    assert all(isinstance(a, int) and isinstance(b, int) and a != b and name for a, b, name in derbies)
    assert len({frozenset((a, b)) for a, b, _ in derbies}) == len(derbies)  # jedes Paar einmal


def test_derbies_missing_or_broken_file_is_logged_and_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(logic_woche, "DATA_DIR", tmp_path)
    with caplog.at_level(logging.WARNING):
        assert load_derbies() == ()
    assert "nicht gefunden" in caplog.text
    (tmp_path / "derbys.json").write_text("{kaputt", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert load_derbies() == ()
    assert "nicht lesbar" in caplog.text
    (tmp_path / "derbys.json").write_text(json.dumps([[1, 2, "Gut"], [1, "zwei", "Schlecht"], "nichts", [1, 1, "Selbst"]]), encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert load_derbies() == ((1, 2, "Gut"),)  # ungültige Einträge entfallen einzeln
    assert caplog.text.count("ungültig") == 3
    (tmp_path / "derbys.json").write_text('{"a": 1}', encoding="utf-8")
    assert load_derbies() == ()


def test_favourite_team_comes_first():
    result = ask()
    assert result["zeitraum"] == "Dienstag, 17. November bis Sonntag, 22. November"
    assert result["stand"] == "17. November, 16:40 Uhr"
    first = result["spiele"][0]
    assert (first["heim"], first["gast"]) == ("1. FC Nürnberg", "Eintracht Braunschweig")
    assert first["wann"] == "Samstag, 21. November, 13:00 Uhr"
    assert first["wettbewerb"] == "2. Bundesliga" and first["grund"] == "Dein Verein spielt"
    # Fürth (Lieblingsverein) spielt zur selben Zeit; der Hauptverein steht bei gleichem Anstoß vorn.
    assert pairs(result) == [
        ("1. FC Nürnberg", "Eintracht Braunschweig"),
        ("Hannover 96", "SpVgg Greuther Fürth"),
        ("SV Werder Bremen", "Hamburger SV"),
    ]
    assert reasons(result) == ["Dein Verein spielt", "Dein Verein spielt", "Derby: Nordderby"]


def test_no_favourite_team():
    result = ask(settings=NO_CLUBS)
    assert "Dein Verein spielt" not in reasons(result)
    assert pairs(result) == [("SV Werder Bremen", "Hamburger SV")]  # nur das Derby bleibt


def test_entries_are_flat_and_small():
    entry = ask()["spiele"][0]
    assert set(entry) == {"wettbewerb", "runde", "wann", "bezug", "uhrzeit_fest", "heim", "gast", "zustand", "grund"}
    assert entry["runde"] == "12. Spieltag" and entry["bezug"] == "in 4 Tagen" and entry["zustand"] == "angesetzt"


def test_at_most_five_matches():
    snapshot = make_snapshot(NOW)
    # Jedes Spiel der Bundesliga als Derby: dazu kommen die beiden Spiele der Lieblingsvereine.
    derbies = tuple((m.home.id, m.away.id, "Testderby") for m in snapshot.matches["bundesliga"])
    result = ask(snapshot, derbies=derbies)
    assert len(result["spiele"]) == MAX_MATCHES == 5
    assert reasons(result) == ["Dein Verein spielt"] * 2 + ["Derby: Testderby"] * 3
    # innerhalb einer Regel nach Anstoß: Freitag 20:30, dann Samstag 15:30 (die drei frühesten Kennungen)
    assert [g["wann"] for g in result["spiele"][2:]] == [
        "Freitag, 20. November, 20:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
    ]
    assert hints(result, "Es gibt 11 besondere Spiele; ich nenne nur 5.")


def test_rule_order_then_kickoff():
    snapshot = make_snapshot(NOW)
    snapshot = moved(snapshot, "nationalmannschaft", 85234, at(21, 20, 45))  # Länderspiel
    snapshot = moved(snapshot, "champions_league", 87388, at(20, 21), home=BAYERN, round_order=9, round_name="Playoff")
    snapshot = moved(snapshot, "bundesliga", 83238, at(19, 20), home=DORTMUND, away=FREIBURG)  # Platz 2 gegen 3
    settings = Settings(competitions=COMPETITION_KEYS, main_team=79, favorite_teams=())
    result = ask(snapshot, settings)
    # Der Anstoß der Spitzenspiels (Donnerstag) liegt vor dem des K.-o.-Spiels (Freitag): die Regel entscheidet.
    assert reasons(result) == [
        "Dein Verein spielt",
        "Länderspiel",
        "K.-o.-Spiel in der Champions League",
        "Spitzenspiel: Platz 2 gegen Platz 3",
        "Derby: Nordderby",
    ]
    assert [g["wettbewerb"] for g in result["spiele"]] == [
        "2. Bundesliga",
        "Nationalmannschaft",
        "Champions League",
        "Bundesliga",
        "Bundesliga",
    ]


def test_a_match_for_several_rules_appears_once_with_the_first_reason():
    snapshot = make_snapshot(NOW)
    # Nordderby mit Lieblingsverein Bremen und mit beiden in den ersten Vier: Regel 1 gewinnt, einmal.
    settings = Settings(competitions=COMPETITION_KEYS, main_team=134, favorite_teams=())
    result = ask(snapshot, settings)
    nord = [g for g in result["spiele"] if g["heim"] == "SV Werder Bremen"]
    assert [g["grund"] for g in nord] == ["Dein Verein spielt"]
    # Pokalspiel zwischen Platz 1 und Platz 2 der Bundesliga, K.-o. mit deutschem Verein: Regel 3 vor Regel 4.
    cup = moved(snapshot, "dfb_pokal", 88752, at(18, 20, 45), home=BAYERN, away=DORTMUND)
    result = ask(cup, NO_CLUBS, derbies=())
    assert reasons(result) == ["K.-o.-Spiel im DFB-Pokal"]


def test_top_match_needs_both_in_the_top_four():
    snapshot = make_snapshot(NOW)
    snapshot = moved(snapshot, "bundesliga", 83238, at(19, 20), home=DORTMUND, away=FREIBURG)  # Platz 2 gegen 3
    snapshot = moved(snapshot, "bundesliga", 83239, at(19, 20, 30), home=BAYERN, away=HOFFENHEIM)  # Platz 1 gegen 9
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert pairs(result) == [("Borussia Dortmund", "SC Freiburg")]
    assert reasons(result) == ["Spitzenspiel: Platz 2 gegen Platz 3"]
    # Ein Spiel zweier Mannschaften aus verschiedenen Ligen ist kein Spitzenspiel (Hertha: Platz 1 der 2. Liga).
    mixed = moved(snapshot, "bundesliga", 83238, at(19, 20), home=DORTMUND, away=Team(54, "Hertha BSC", "Hertha", None))
    assert ask(mixed, NO_CLUBS, derbies=())["spiele"] == []


def test_top_match_from_an_outdated_table_names_its_stand_and_says_so():
    snapshot = moved(make_snapshot(NOW), "bundesliga", 83238, at(19, 20), home=DORTMUND, away=FREIBURG)
    yesterday = datetime(2026, 11, 16, 9, 15, tzinfo=BERLIN)
    snapshot = dataclasses.replace(
        snapshot, tables_at={**snapshot.tables_at, "bundesliga": yesterday}, stale_tables=frozenset({"bundesliga"})
    )
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert reasons(result) == ["Spitzenspiel: Platz 2 gegen Platz 3"]
    assert result["stand"] == "16. November, 09:15 Uhr"
    assert hints(result, "Tabelle von Bundesliga") and hints(result, "die Tabelle enthält das noch nicht")
    # Ohne Spitzenspiel aus dieser Tabelle kein Hinweis darauf, der Stand nennt die Tabelle trotzdem.
    quiet = ask(dataclasses.replace(make_snapshot(NOW), tables_at=snapshot.tables_at, stale_tables=snapshot.stale_tables),
                NO_CLUBS, derbies=())
    assert not hints(quiet, "Tabelle von Bundesliga") and quiet["stand"] == "16. November, 09:15 Uhr"


def test_top_match_in_the_second_league_names_the_places_in_that_league():
    snapshot = moved(make_snapshot(NOW), "2_bundesliga", 83698, at(21, 13, 30), home=Team(54, "Hertha BSC", "H", None),
                     away=Team(78, "1. FC Magdeburg", "M", None))
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert reasons(result) == ["Spitzenspiel: Platz 1 gegen Platz 4"]


def test_top_match_cannot_fire_without_the_table():
    snapshot = moved(make_snapshot(NOW), "bundesliga", 83238, at(19, 20), home=DORTMUND, away=FREIBURG)
    without = dataclasses.replace(snapshot, tables=MappingProxyType({"2_bundesliga": snapshot.tables["2_bundesliga"]}))
    result = ask(without, MEN_LEAGUES, derbies=())
    assert result["spiele"] == []
    assert hints(result, "Spitzenspiele") and hints(result, "Bundesliga")
    assert hints(result, "In den bisher geladenen Daten steht in dieser Woche nichts Besonderes an.")  # nie mit Gewissheit


def test_knockout_needs_a_german_club():
    snapshot = make_snapshot(NOW)
    snapshot = moved(snapshot, "champions_league", 87388, at(18, 21), home=BAYERN, away=VILLA, round_order=9)
    snapshot = moved(snapshot, "champions_league", 87390, at(18, 21), home=MADRID, away=INTER, round_order=9)
    # Ein Spiel der Ligaphase (Runde 8) mit einem deutschen Verein zählt nie.
    snapshot = moved(snapshot, "champions_league", 87392, at(18, 21), home=DORTMUND, away=VILLA, round_order=8)
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert pairs(result) == [("FC Bayern München", "Aston Villa")]
    assert reasons(result) == ["K.-o.-Spiel in der Champions League"]
    # In der Conference League gilt die Ligaphase von 6 Runden.
    conference = moved(make_snapshot(NOW), "conference_league", 88333, at(19, 18), round_order=7)
    assert ask(conference, NO_CLUBS, derbies=())["spiele"] == []  # zwei ausländische Vereine
    german = moved(make_snapshot(NOW), "conference_league", 88350, at(19, 18), round_order=7)
    assert reasons(ask(german, NO_CLUBS, derbies=())) == ["K.-o.-Spiel in der Conference League"]


def test_cup_match_needs_a_club_of_a_chosen_league():
    snapshot = moved(make_snapshot(NOW), "dfb_pokal", 88750, at(18, 18))  # Stuttgart gegen Dresden
    assert reasons(ask(snapshot, NO_CLUBS, derbies=())) == ["K.-o.-Spiel im DFB-Pokal"]
    # Ist keine deutsche Liga gewählt, bleibt kein Verein aus einer gewählten Liga: kein K.-o.-Spiel.
    cup_only = Settings(competitions=("dfb_pokal",), main_team=None, favorite_teams=())
    assert ask(snapshot, cup_only, derbies=())["spiele"] == []


def test_derby_from_the_list():
    snapshot = make_snapshot(datetime(2027, 1, 26, 12, 0, tzinfo=BERLIN))  # Woche von Spiel 83874
    result = ask(snapshot, NO_CLUBS)
    assert pairs(result) == [("1. FC Nürnberg", "SpVgg Greuther Fürth")]
    assert reasons(result) == ["Derby: Frankenderby"]
    assert result["zeitraum"] == "Dienstag, 26. Januar bis Sonntag, 31. Januar"
    again = ask(snapshot, SETTINGS_CLUB)
    assert pairs(again) == [("1. FC Nürnberg", "SpVgg Greuther Fürth")] and reasons(again) == ["Dein Verein spielt"]
    # Auch in umgekehrter Reihenfolge der Kennungen.
    assert reasons(ask(snapshot, NO_CLUBS, derbies=((115, 79, "Frankenderby"),))) == ["Derby: Frankenderby"]


def test_derby_matches_any_id_of_a_club():
    snapshot = make_snapshot(datetime(2027, 1, 26, 12, 0, tzinfo=BERLIN))
    # Fürth tritt hier unter einer zweiten Kennung auf (zum Beispiel im Pokal): der Verein bleibt derselbe.
    fuerth = Team(99115, "SpVgg Greuther Fürth", "Fürth", None)
    snapshot = replace_match(snapshot, "2_bundesliga", 83874, away=fuerth)
    assert make_index(snapshot).ids_of(fuerth) == frozenset({115, 99115})
    assert reasons(ask(snapshot, NO_CLUBS)) == ["Derby: Frankenderby"]


def test_international_match():
    snapshot = make_snapshot(datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, SETTINGS_CLUB)
    assert result["zeitraum"] == "Dienstag, 10. November bis Sonntag, 15. November"
    länder = [g for g in result["spiele"] if g["grund"] == "Länderspiel"]
    assert [(g["heim"], g["gast"], g["wettbewerb"]) for g in länder] == [("Serbien", "Deutschland", "Nationalmannschaft")]
    assert länder[0]["wann"] == "Freitag, 13. November, 20:45 Uhr"
    # Ohne gewählte Nationalmannschaft kein Länderspiel.
    settings = Settings(competitions=("bundesliga", "2_bundesliga"), main_team=79, favorite_teams=())
    assert "Länderspiel" not in reasons(ask(snapshot, settings))


def test_next_week():
    result = ask(woche="naechste")
    assert result["zeitraum"] == "Montag, 23. November bis Sonntag, 29. November"
    assert result["spiele"]
    assert all(g["wann"].split(",")[1].strip().split(".")[0] in {str(d) for d in range(23, 30)} for g in result["spiele"])
    assert reasons(result)[0] == "Dein Verein spielt"
    assert (result["spiele"][0]["heim"], result["spiele"][0]["gast"]) == ("1. FC Heidenheim 1846", "1. FC Nürnberg")
    # Am Sonntag selbst beginnt die nächste Woche am folgenden Montag.
    sunday = make_snapshot(datetime(2026, 11, 22, 10, 0, tzinfo=BERLIN))
    assert ask(sunday, woche="naechste")["zeitraum"] == "Montag, 23. November bis Sonntag, 29. November"


def test_this_week_starts_today_and_skips_finished_matches():
    sunday = make_snapshot(datetime(2026, 11, 22, 10, 0, tzinfo=BERLIN))
    result = ask(sunday)
    assert result["zeitraum"] == "Sonntag, 22. November"  # nur ein Tag
    assert pairs(result) == [("SV Werder Bremen", "Hamburger SV")]
    saturday = make_snapshot(datetime(2026, 11, 21, 14, 0, tzinfo=BERLIN))
    # Nürnberg läuft (13:00 angepfiffen), Fürth ist beendet: beendete Spiele erscheinen nicht, laufende schon.
    saturday = replace_match(saturday, "2_bundesliga", 83694, finished=True, final=Score(1, 0))
    result = ask(saturday)
    assert pairs(result)[0] == ("1. FC Nürnberg", "Eintracht Braunschweig")
    assert result["spiele"][0]["zustand"] == "läuft" and result["spiele"][0]["ergebnis"] == "0:0"
    assert ("Hannover 96", "SpVgg Greuther Fürth") not in pairs(result)
    assert result["spiele"][0]["stand_von"] == "14:00 Uhr" and "hinterherhängen" in result["spiele"][0]["vorbehalt"]
    assert not hints(result, "hinterherhängen")


def test_stand_follows_the_competitions_of_the_shown_matches():
    # Nur die Spieldaten der 2. Bundesliga sind frisch, die der übrigen Wettbewerbe vom Vorabend; Tabellen sind frisch.
    snapshot = fetched_earlier(make_snapshot(NOW), datetime(2026, 11, 16, 20, 47, tzinfo=BERLIN), "2_bundesliga")
    result = ask(snapshot, derbies=())
    assert {g["wettbewerb"] for g in result["spiele"]} == {"2. Bundesliga"}
    assert result["stand"] == "17. November, 16:40 Uhr"
    # Ohne gezeigtes Spiel beruht "nichts Besonderes" auf allen durchsuchten Wettbewerben: der älteste Abruf.
    empty = ask(snapshot, MEN_LEAGUES, derbies=())
    assert empty["spiele"] == []
    assert empty["stand"] == "16. November, 20:47 Uhr"


def test_quiet_week_names_the_next_round():
    result = ask(settings=MEN_LEAGUES, derbies=())
    assert result["spiele"] == []
    assert hints(result, "In dieser Woche steht nichts Besonderes an.")
    assert hints(result, "Der nächste Spieltag im Wettbewerb Bundesliga ist der 10. Spieltag (Freitag, 20. November bis Sonntag, 22. November).")
    assert result["stand"] == "17. November, 16:40 Uhr"
    second = Settings(competitions=("2_bundesliga",), main_team=None, favorite_teams=())
    nextweek = ask(settings=second, derbies=(), woche="naechste")  # 23. bis 29. November ohne Spitzenspiel
    assert nextweek["spiele"] == [] and nextweek["zeitraum"] == "Montag, 23. November bis Sonntag, 29. November"
    assert hints(nextweek, "In der nächsten Woche steht nichts Besonderes an.")
    # der erste Spieltag ab Montag der gefragten Woche: der 13. (27. bis 29.), nicht der 12. (20. bis 22. November)
    assert hints(nextweek, "Der nächste Spieltag im Wettbewerb 2. Bundesliga ist der 13. Spieltag (Freitag, 27. November bis Sonntag, 29. November).")


def test_quiet_week_follows_the_league_of_the_main_team():
    settings = Settings(competitions=("bundesliga", "2_bundesliga"), main_team=36, favorite_teams=())  # Osnabrück
    # Sonntagabend: die Spiele des Tages sind angepfiffen oder laut Daten nicht beendet, also nichts mehr ansteht.
    snapshot = make_snapshot(datetime(2026, 11, 22, 20, 0, tzinfo=BERLIN))
    result = ask(snapshot, settings, derbies=())
    assert result["spiele"] == []
    assert hints(result, "Der nächste Spieltag im Wettbewerb 2. Bundesliga ist der 13. Spieltag (Freitag, 27. November bis Sonntag, 29. November).")


def test_quiet_week_is_not_certain_when_data_is_missing():
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"2_bundesliga"})
    result = ask(waiting, MEN_LEAGUES, derbies=())
    assert result["spiele"] == []
    assert hints(result, "In den bisher geladenen Daten steht in dieser Woche nichts Besonderes an.")
    assert not hints(result, "In dieser Woche steht nichts Besonderes an.")
    assert hints(result, "noch nicht alle Wettbewerbe geladen") and hints(result, "2. Bundesliga")
    unreachable = dataclasses.replace(snapshot, unreachable=frozenset({"2_bundesliga"}))
    result = ask(unreachable, MEN_LEAGUES, derbies=())
    assert hints(result, "In den bisher geladenen Daten") and hints(result, "nicht erreichbar")
    # Fehlt die Tabelle einer gewählten Liga, ist das Fehlen von Spitzenspielen nicht sicher.
    no_table = dataclasses.replace(snapshot, tables=MappingProxyType({"bundesliga": snapshot.tables["bundesliga"]}))
    result = ask(no_table, MEN_LEAGUES, derbies=())
    assert hints(result, "In den bisher geladenen Daten") and hints(result, "fehlt die Tabelle von 2. Bundesliga")


def test_nothing_loaded_is_an_error_not_a_quiet_week():
    snapshot = dataclasses.replace(make_snapshot(NOW), loaded=frozenset())
    with pytest.raises(ToolError) as caught:
        ask(snapshot)
    assert caught.value.kind is ErrorKind.UNREACHABLE and "Versuch es gleich noch einmal" in caught.value.text


def test_no_competition_chosen_is_unsupported():
    with pytest.raises(ToolError) as caught:
        ask(settings=Settings(competitions=(), main_team=None, favorite_teams=()))
    assert caught.value.kind is ErrorKind.UNSUPPORTED


def test_unreachable_competition_is_named_and_leaves_the_answer_out():
    snapshot = dataclasses.replace(make_snapshot(NOW), unreachable=frozenset({"2_bundesliga"}))
    result = ask(snapshot)
    assert ("1. FC Nürnberg", "Eintracht Braunschweig") not in pairs(result)
    assert hints(result, "2. Bundesliga") and hints(result, "nicht erreichbar")


def test_invalid_week():
    with pytest.raises(ToolError) as caught:
        ask(woche="letzte")
    assert caught.value.kind is ErrorKind.INVALID_INPUT
    assert "„letzte“" in caught.value.text and "diese, naechste" in caught.value.text


def test_week_defaults_to_this_week_and_ignores_case():
    assert ask(woche=None)["zeitraum"] == ask(woche="  Diese ")["zeitraum"] == ask(woche="")["zeitraum"]
    assert ask(woche="NAECHSTE")["zeitraum"].startswith("Montag, 23. November")


def women_table(snapshot):
    """Synthetische Tabelle der Frauen-Bundesliga (aufgezeichnet gibt es keine): Frankfurt Platz 1, Hamburg Platz 3."""
    teams = {m.home.id: m.home for m in snapshot.matches["frauen_bundesliga"]} | {
        m.away.id: m.away for m in snapshot.matches["frauen_bundesliga"]
    }
    order = [6073, 6062, 6324, *[i for i in teams if i not in (6073, 6062, 6324)]]
    rows = tuple(TableRow(n, teams[i], 10, 30 - n, 20, 10, 10, 5, 2, 3) for n, i in enumerate(order, start=1))
    return dataclasses.replace(snapshot, tables=MappingProxyType({**snapshot.tables, "frauen_bundesliga": rows}))


def test_top_match_of_the_women_bundesliga():
    snapshot = women_table(make_snapshot(NOW))  # Frankfurt Frauen gegen Hamburg Frauen am 21.11.
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert pairs(result) == [("Eintracht Frankfurt Frauen", "Hamburger SV Frauen")]
    assert reasons(result) == ["Spitzenspiel: Platz 1 gegen Platz 3"]
    assert result["spiele"][0]["wettbewerb"] == "Frauen-Bundesliga"
    # Freiburg (Platz 2) gegen Stuttgart (Platz 8) spielt in derselben Woche und erscheint nicht.
    freiburg_stuttgart = ("SC Freiburg Frauen", "VfB Stuttgart Frauen")
    assert freiburg_stuttgart in [(m.home.name, m.away.name) for m in snapshot.matches["frauen_bundesliga"]]
    assert freiburg_stuttgart not in pairs(result)
    # Platz 2 gegen Platz 1 (Heim zuerst) erscheint zusätzlich.
    freiburg = next(m.home for m in snapshot.matches["frauen_bundesliga"] if m.home.id == 6062)
    frankfurt = next(m.home for m in snapshot.matches["frauen_bundesliga"] if m.home.id == 6073)
    both = moved(snapshot, "frauen_bundesliga", 85788, at(22, 14), home=freiburg, away=frankfurt)
    assert sorted(reasons(ask(both, NO_CLUBS, derbies=()))) == [
        "Spitzenspiel: Platz 1 gegen Platz 3",
        "Spitzenspiel: Platz 2 gegen Platz 1",
    ]


def test_men_and_women_tables_are_kept_apart():
    # Dortmund (Männer, Platz 2) gegen Frankfurt Frauen (Frauen, Platz 1): jeder steht nur in seiner Tabelle.
    snapshot = women_table(make_snapshot(NOW))
    frankfurt_women = next(m.home for m in snapshot.matches["frauen_bundesliga"] if m.home.id == 6073)
    snapshot = moved(snapshot, "bundesliga", 83238, at(19, 20), home=DORTMUND, away=frankfurt_women)
    result = ask(snapshot, NO_CLUBS, derbies=())
    assert ("Borussia Dortmund", "Eintracht Frankfurt Frauen") not in pairs(result)


def test_only_the_women_bundesliga_chosen_is_a_normal_answer():
    only = Settings(competitions=("frauen_bundesliga",), main_team=None, favorite_teams=())
    snapshot = women_table(make_snapshot(NOW))
    result = ask(snapshot, only, derbies=())
    assert reasons(result) == ["Spitzenspiel: Platz 1 gegen Platz 3"]
    assert result["stand"] == "17. November, 16:40 Uhr"
    # Ohne Tabelle kann das Spitzenspiel nicht erkannt werden, und das steht da, statt "nichts Besonderes" zu behaupten.
    result = ask(make_snapshot(NOW), only, derbies=())
    assert result["spiele"] == []
    assert hints(result, "fehlt die Tabelle von Frauen-Bundesliga")
    assert hints(result, "In den bisher geladenen Daten steht in dieser Woche nichts Besonderes an.")
    assert hints(result, "Der nächste Spieltag im Wettbewerb Frauen-Bundesliga ist der 11. Spieltag")


def test_knockout_reason_says_in_the_cup_and_in_the_european_competition():
    cup = moved(make_snapshot(NOW), "dfb_pokal", 88750, at(18, 18))
    assert reasons(ask(cup, NO_CLUBS, derbies=())) == ["K.-o.-Spiel im DFB-Pokal"]
    europe = moved(make_snapshot(NOW), "europa_league", 87760, at(19, 21), round_order=9)  # Lyon gegen Leverkusen
    assert reasons(ask(europe, NO_CLUBS, derbies=())) == ["K.-o.-Spiel in der Europa League"]


def test_quiet_next_week_names_the_first_round_from_the_start_of_that_week():
    # Montag 14.12.: die nächste Woche (21. bis 27.12.) ist Winterpause. Der 14. Spieltag (18.-20.12.) liegt davor
    # und ist nicht gemeint, sondern der 15. Spieltag im Januar.
    snapshot = make_snapshot(datetime(2026, 12, 14, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, MEN_LEAGUES, derbies=(), woche="naechste")
    assert result["zeitraum"] == "Montag, 21. Dezember bis Sonntag, 27. Dezember" and result["spiele"] == []
    assert hints(result, "In der nächsten Woche steht nichts Besonderes an.")
    assert hints(result, "Der nächste Spieltag im Wettbewerb Bundesliga ist der 15. Spieltag (Freitag, 8. Januar 2027 bis Sonntag, 10. Januar 2027).")
