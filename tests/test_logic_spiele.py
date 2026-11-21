"""Antwort "Spiele": Tag, Wochenende und Spieltag aus den Aufzeichnungen."""

import dataclasses
import json
from datetime import datetime
from types import MappingProxyType

import pytest

from custom_components.fussball_assist import logic_spiele
from custom_components.fussball_assist.logic_spiele import MAX_DETAILED, answer
from custom_components.fussball_assist.models import Phase, Score
from custom_components.fussball_assist.parse import parse_matches
from custom_components.fussball_assist.results import LIVE_SCORE_CAVEAT, ErrorKind, Settings, ToolError
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import (
    BERLIN,
    SETTINGS_CLUB,
    assert_speakable,
    fetched_earlier,
    load_fixture,
    make_index,
    make_snapshot,
)

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)  # Dienstag
ALL_NAMES = ["Bundesliga", "2. Bundesliga", "3. Liga", "Frauen-Bundesliga"]


def ask(snapshot=None, settings=SETTINGS_CLUB, with_index=True, **kwargs):
    """Fragt mit den Vorgaben des Werkzeugs; jedes Ergebnis muss vorlesbar sein."""
    snapshot = snapshot or make_snapshot(NOW)
    params = {"wettbewerb": "alle", "zeitraum": "heute", "datum": None}
    params.update(kwargs)
    if with_index:
        params["index"] = make_index(snapshot)
    result = answer(snapshot, settings, **params)
    assert_speakable(result)
    return result


def games(result):
    """Alle Spiele; die Runde, die an der Gruppe steht, ist dabei an jedes Spiel geschrieben."""
    return [
        {"runde": group["runde"], **g} if "runde" in group else g
        for group in result["wettbewerbe"]
        for g in group["spiele"]
    ]


def names(result):
    return [group["wettbewerb"] for group in result["wettbewerbe"]]


def hints(result, text):
    return [h for h in result["hinweise"] if text in h]


def error_of(**kwargs):
    with pytest.raises(ToolError) as caught:
        ask(**kwargs)
    return caught.value


def test_today_without_matches_names_the_next_day():
    result = ask(wettbewerb="alle", zeitraum="heute")
    assert result["zeitraum"] == "Dienstag, 17. November"
    assert result["stand"] == "17. November, 16:40 Uhr"
    assert result["wettbewerbe"] == [] and result["gekuerzt"] is False
    assert result["naechster_tag_mit_spielen"] == "Freitag, 20. November, also in 3 Tagen"
    assert "grund" not in result
    assert hints(result, "Für Dienstag, 17. November sind in den ausgewählten Wettbewerben keine Spiele eingetragen.")


def test_stand_follows_the_competitions_of_the_shown_matches():
    # Am 1. Dezember spielt nur der DFB-Pokal; seine Spieldaten sind frisch, die der übrigen Wettbewerbe vom Vorabend.
    snapshot = fetched_earlier(make_snapshot(NOW), datetime(2026, 11, 16, 20, 47, tzinfo=BERLIN), "dfb_pokal")
    result = ask(snapshot, datum="2026-12-01")
    assert names(result) == ["DFB-Pokal"]
    assert result["stand"] == "17. November, 16:40 Uhr"
    # Ohne gezeigtes Spiel beruht "keine Spiele" auf allen durchsuchten Wettbewerben: der älteste Abruf.
    empty = ask(snapshot, datum="2026-11-30")
    assert empty["wettbewerbe"] == []
    assert empty["stand"] == "16. November, 20:47 Uhr"


def test_break_is_explained_only_with_internationals():
    # Samstag 14.11.: Deutschland spielt am 16.11. (Aufzeichnung nla), die Bundesliga erst am 20.11.
    saturday = make_snapshot(datetime(2026, 11, 14, 12, 0, tzinfo=BERLIN))
    result = ask(saturday, wettbewerb="bundesliga", zeitraum="heute")
    assert result["wettbewerbe"] == []
    assert result["grund"] == "Länderspielpause"
    assert result["naechster_tag_mit_spielen"] == "Freitag, 20. November, also in 6 Tagen"
    # Montag 23.11.: zwischen heute und dem 27.11. liegt kein Länderspiel, also kein Grund.
    monday = make_snapshot(datetime(2026, 11, 23, 12, 0, tzinfo=BERLIN))
    result = ask(monday, wettbewerb="bundesliga", zeitraum="heute")
    assert result["wettbewerbe"] == []
    assert result["naechster_tag_mit_spielen"] == "Freitag, 27. November, also in 4 Tagen"
    assert "grund" not in result


def test_break_needs_the_national_team_to_be_selected_and_loaded():
    saturday = make_snapshot(datetime(2026, 11, 14, 12, 0, tzinfo=BERLIN))
    without = Settings(
        competitions=tuple(k for k in COMPETITION_KEYS if k != "nationalmannschaft"), main_team=79, favorite_teams=()
    )
    result = ask(saturday, without, wettbewerb="bundesliga", zeitraum="heute")
    assert "grund" not in result and result["naechster_tag_mit_spielen"].startswith("Freitag, 20. November")
    # Gewählt, aber noch nicht geladen: kein Grund, dafür der Hinweis, dass er fehlt.
    waiting = dataclasses.replace(saturday, loaded=saturday.loaded - {"nationalmannschaft"})
    result = ask(waiting, wettbewerb="bundesliga", zeitraum="heute")
    assert "grund" not in result and hints(result, "Länderspiel")
    # Fragt man nach der Nationalmannschaft selbst, ist ihr Spiel der nächste Tag: kein Grund.
    result = ask(saturday, wettbewerb="nationalmannschaft", zeitraum="heute")
    assert "grund" not in result and result["naechster_tag_mit_spielen"] == "Montag, 16. November, also übermorgen"


def test_coming_weekend_bundesliga():
    result = ask(wettbewerb="bundesliga", zeitraum="kommendes_wochenende")
    assert result["zeitraum"] == "Freitag, 20. November bis Sonntag, 22. November"
    assert names(result) == ["Bundesliga"] and result["gekuerzt"] is False
    spiele = games(result)
    assert len(spiele) == 9
    assert all(g["wann"] and g["uhrzeit_fest"] is True and g["zustand"] == "angesetzt" for g in spiele)
    assert (spiele[0]["heim"], spiele[0]["gast"], spiele[0]["wann"]) == (
        "FC Bayern München",
        "1. FC Köln",
        "Freitag, 20. November, 20:30 Uhr",
    )
    assert spiele[-1]["wann"] == "Sonntag, 22. November, 17:30 Uhr"
    assert "naechster_tag_mit_spielen" not in result and "grund" not in result
    assert all("gegner" not in g and "ort" not in g for g in spiele)


def test_matches_of_a_group_come_in_kickoff_order():
    spiele = games(ask(wettbewerb="bundesliga", zeitraum="kommendes_wochenende"))
    assert [g["wann"] for g in spiele] == [
        "Freitag, 20. November, 20:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 15:30 Uhr",
        "Samstag, 21. November, 18:30 Uhr",
        "Sonntag, 22. November, 15:30 Uhr",
        "Sonntag, 22. November, 17:30 Uhr",
    ]


def test_last_round_comes_from_the_data():
    result = ask(wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert result["zeitraum"] == "9. Spieltag (Freitag, 6. November bis Sonntag, 8. November)"
    spiele = games(result)
    assert len(spiele) == 9 and result["gekuerzt"] is False
    assert {g["runde"] for g in spiele} == {"9. Spieltag"}
    assert {g["zustand"] for g in spiele} == {"beendet"}
    first = spiele[0]
    assert (first["heim"], first["gast"], first["ergebnis"]) == ("Hamburger SV", "Borussia Mönchengladbach", "3:1")
    assert len(first["tore"]) == 4 and first["bezug"] == "vor 11 Tagen"


def test_next_round_comes_from_the_data():
    result = ask(wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert result["zeitraum"] == "10. Spieltag (Freitag, 20. November bis Sonntag, 22. November)"
    spiele = games(result)
    assert len(spiele) == 9
    assert {g["runde"] for g in spiele} == {"10. Spieltag"} and {g["zustand"] for g in spiele} == {"angesetzt"}


def test_round_ignores_a_stale_unfinished_match_of_an_old_round():
    # Ein alter Spieltag, dessen Spiel laut Daten nie beendet wurde, ist nicht der nächste Spieltag.
    snapshot = make_snapshot(NOW)
    stale = next(m for m in snapshot.matches["bundesliga"] if m.round_name == "5. Spieltag")
    matches = {
        k: tuple(dataclasses.replace(m, finished=False) if m.id == stale.id else m for m in v)
        for k, v in snapshot.matches.items()
    }
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    result = ask(snapshot, wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert {g["runde"] for g in games(result)} == {"10. Spieltag"}
    last = ask(snapshot, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert {g["runde"] for g in games(last)} == {"9. Spieltag"}


def test_round_needs_one_competition():
    error = error_of(wettbewerb="alle", zeitraum="letzter_spieltag")
    assert error.kind is ErrorKind.INVALID_INPUT
    for key in COMPETITION_KEYS:
        assert key in error.text
    assert error_of(wettbewerb=None, zeitraum="naechster_spieltag").kind is ErrorKind.INVALID_INPUT
    # Ist nur ein Wettbewerb gewählt, meint "alle" genau ihn.
    single = Settings(competitions=("bundesliga",), main_team=None, favorite_teams=())
    result = ask(settings=single, wettbewerb="alle", zeitraum="letzter_spieltag")
    assert {g["runde"] for g in games(result)} == {"9. Spieltag"}


def test_round_without_a_finished_match_says_so():
    snapshot = make_snapshot(datetime(2026, 8, 1, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert result["wettbewerbe"] == [] and result["zeitraum"] == "letzter Spieltag"
    assert hints(result, "Im Wettbewerb Bundesliga ist noch kein Spieltag gespielt.")
    nxt = ask(snapshot, wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert {g["runde"] for g in games(nxt)} == {"1. Spieltag"}


def test_explicit_day_overrides_period():
    result = ask(zeitraum="morgen", datum="2026-11-08")
    assert result["zeitraum"] == "Sonntag, 8. November"
    spiele = games(result)
    assert len(spiele) == 11 and all(g["wann"].startswith("Sonntag, 8. November") for g in spiele)
    assert names(result) == ALL_NAMES
    assert result["gekuerzt"] is False and any("tore" in g for g in spiele)  # 11 Spiele sind noch nicht zu viele


def test_many_matches_are_shortened_and_say_so():
    result = ask(datum="2026-11-07")  # 17 Spiele in vier Wettbewerben, alle beendet
    spiele = games(result)
    assert len(spiele) == 17 > MAX_DETAILED
    assert result["gekuerzt"] is True
    assert all("tore" not in g and "nach_90_minuten" not in g for g in spiele)
    assert any("ergebnis" in g for g in spiele)
    assert hints(result, "keine Torschützen")
    # Der Verein des Hauptvereins steht zuerst: seine Gruppe vorn, sein Spiel vorn in der Gruppe.
    assert names(result)[0] == "2. Bundesliga" and sorted(names(result)) == sorted(ALL_NAMES)
    assert (spiele[0]["heim"], spiele[0]["gast"]) == ("1. FC Nürnberg", "SV Darmstadt 98")
    assert [g["wann"] for g in result["wettbewerbe"][0]["spiele"]][1:] == sorted(
        g["wann"] for g in result["wettbewerbe"][0]["spiele"][1:]
    )


def test_shortening_keeps_at_most_nine_matches_per_competition():
    result = ask(wettbewerb="dfb_pokal", zeitraum="letzter_spieltag")  # 2. Runde: 16 Spiele
    assert result["zeitraum"] == "2. Runde (Dienstag, 27. Oktober bis Mittwoch, 28. Oktober)"
    spiele = games(result)
    assert len(spiele) == 9 and result["gekuerzt"] is True
    assert {g["runde"] for g in spiele} == {"2. Runde"}
    assert (spiele[0]["heim"], spiele[0]["gast"]) == ("Bayer 04 Leverkusen", "1. FC Nürnberg")  # Hauptverein zuerst
    assert hints(result, "Im Wettbewerb DFB-Pokal gibt es 16 Spiele; ich nenne nur 9 davon.")


def test_details_stay_when_not_shortened():
    result = ask(datum="2026-10-28")  # acht Pokalspiele, eins im Elfmeterschießen
    spiele = games(result)
    assert len(spiele) == 8 and result["gekuerzt"] is False
    penalties = next(g for g in spiele if g["heim"] == "Holstein Kiel")
    assert (penalties["ergebnis"], penalties["ergebnis_zusatz"]) == ("5:4", "im Elfmeterschießen")
    assert penalties["nach_90_minuten"] == "1:1"


def test_running_matches_come_first():
    # Sonntag 18:00: die Spiele um 15:30 und 17:30 laufen; die früheren stehen laut Daten noch nicht als beendet.
    sunday = make_snapshot(datetime(2026, 11, 22, 18, 0, tzinfo=BERLIN))
    result = ask(sunday, wettbewerb="bundesliga", zeitraum="kommendes_wochenende")
    states = [g["zustand"] for g in games(result)]
    assert states[:2] == ["läuft", "läuft"] and set(states[2:]) == {"laut Daten noch nicht beendet"}
    spiele = games(result)
    assert [g["heim"] for g in spiele[:2]] == ["FC Augsburg", "SV Werder Bremen"]
    assert (spiele[2]["heim"], spiele[2]["wann"]) == ("FC Bayern München", "Freitag, 20. November, 20:30 Uhr")
    assert all(g["ergebnis"] == "0:0" and g["vorbehalt"] == LIVE_SCORE_CAVEAT for g in spiele[:2])
    assert all(g["stand_von"] == "18:00 Uhr" for g in spiele[:2])
    assert not any("vorbehalt" in g for g in spiele[2:])
    assert not hints(result, "hinterherhängen") and hints(result, "noch nicht beendet")


def test_running_match_from_the_live_recording():
    base = make_snapshot(datetime(2026, 11, 20, 21, 45, tzinfo=BERLIN))
    live = parse_matches(load_fixture("live_synthetisch"))[0]  # Bayern gegen Köln, 1:1
    matches = {k: tuple(live if m.id == live.id else m for m in v) for k, v in base.matches.items()}
    snapshot = dataclasses.replace(base, matches=MappingProxyType(matches))
    result = ask(snapshot, zeitraum="heute")
    group = result["wettbewerbe"][0]
    assert group["wettbewerb"] == "Bundesliga"
    (bayern,) = group["spiele"]
    assert (bayern["zustand"], bayern["ergebnis"]) == ("läuft", "1:1")
    assert [t.split(":")[0] for t in bayern["tore"]] == ["23. Minute", "51. Minute"]
    assert (bayern["stand_von"], bayern["vorbehalt"]) == ("21:45 Uhr", LIVE_SCORE_CAVEAT)
    assert not hints(result, "hinterherhängen")


def test_invalid_values_name_the_valid_ones():
    period = error_of(zeitraum="am Samstag")
    assert period.kind is ErrorKind.INVALID_INPUT
    for valid in ("heute", "gestern", "morgen", "letztes_wochenende", "kommendes_wochenende", "letzter_spieltag", "naechster_spieltag"):
        assert valid in period.text
    competition = error_of(wettbewerb="1. Bundesliga")
    assert competition.kind is ErrorKind.INVALID_INPUT
    for valid in ("alle", *COMPETITION_KEYS):
        assert valid in competition.text
    for bad in ("morgen", "21.11.2026", "2026-13-45", "2026-11-8"):
        day = error_of(datum=bad)
        assert day.kind is ErrorKind.INVALID_INPUT and "JJJJ-MM-TT" in day.text


def test_period_is_checked_even_when_a_day_is_given():
    assert error_of(zeitraum="am Samstag", datum="2026-11-08").kind is ErrorKind.INVALID_INPUT


def test_empty_values_count_as_not_given():
    result = ask(wettbewerb="  ", zeitraum=None, datum="")
    assert result == ask(wettbewerb="alle", zeitraum="heute", datum=None)


def test_competition_names_are_accepted_like_keys():
    by_name = ask(wettbewerb="2. Bundesliga", zeitraum="kommendes_wochenende")
    assert by_name == ask(wettbewerb="2_bundesliga", zeitraum="kommendes_wochenende")
    assert names(by_name) == ["2. Bundesliga"]


def test_cup_has_rounds_not_matchdays():
    result = ask(wettbewerb="dfb_pokal", zeitraum="letzter_spieltag")
    assert {g["runde"] for g in games(result)} == {"2. Runde"}
    nxt = ask(wettbewerb="dfb_pokal", zeitraum="naechster_spieltag")
    assert {g["runde"] for g in games(nxt)} == {"Achtelfinale"} and len(games(nxt)) == 8


def test_only_selected_competitions():
    two = Settings(competitions=("bundesliga", "2_bundesliga"), main_team=79, favorite_teams=())
    result = ask(settings=two, wettbewerb="alle", zeitraum="kommendes_wochenende")
    assert names(result) == ["2. Bundesliga", "Bundesliga"]  # 18 Spiele: die Gruppe mit dem Hauptverein zuerst
    assert len(games(result)) == 9 + 9 and result["gekuerzt"] is True  # nur die festen Uhrzeiten sind entfallen
    pokal = error_of(settings=two, wettbewerb="dfb_pokal")
    assert pokal.kind is ErrorKind.INVALID_INPUT and "bundesliga" in pokal.text and "dfb_pokal" not in pokal.text.split("Gültig")[1]
    # Auch die Frage nach einem Tag nennt nie einen nicht gewählten Wettbewerb.
    day = ask(settings=two, datum="2026-11-07")
    assert set(names(day)) <= {"Bundesliga", "2. Bundesliga"}


def test_women_are_reachable_here():
    result = ask(wettbewerb="frauen_bundesliga", zeitraum="kommendes_wochenende")
    assert names(result) == ["Frauen-Bundesliga"] and len(games(result)) == 6


def test_favorites_with_several_ids_come_first():
    # Saarbrücken ist im Pokal unter einer anderen Kennung (3078) als in der 3. Liga (417).
    settings = Settings(competitions=COMPETITION_KEYS, main_team=417, favorite_teams=())
    for with_index in (True, False):
        result = ask(settings=settings, datum="2026-08-22", with_index=with_index)
        assert result["gekuerzt"] is True
        assert names(result)[0] == "DFB-Pokal"
        assert games(result)[0]["heim"] == "1. FC Saarbrücken"


def test_favorite_without_a_known_id_is_ignored():
    settings = Settings(competitions=COMPETITION_KEYS, main_team=999999, favorite_teams=(115,))
    result = ask(settings=settings, datum="2026-11-21")  # Hannover gegen Fürth; 18 Spiele
    assert len(games(result)) == 18
    assert names(result)[0] == "2. Bundesliga"
    assert (games(result)[0]["heim"], games(result)[0]["gast"]) == ("Hannover 96", "SpVgg Greuther Fürth")


def test_next_day_is_not_stated_when_data_is_missing():
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"2_bundesliga"})
    result = ask(waiting, wettbewerb="alle", zeitraum="heute")
    assert result["wettbewerbe"] == [] and "naechster_tag_mit_spielen" not in result and "grund" not in result
    assert hints(result, "In den bisher geladenen Daten sind für Dienstag, 17. November keine Spiele eingetragen.")
    assert hints(result, "Freitag, 20. November, also in 3 Tagen")
    assert hints(result, "noch nicht alle Wettbewerbe geladen") and hints(result, "2. Bundesliga")
    assert not hints(result, "in den ausgewählten Wettbewerben keine Spiele")


def test_unreachable_competition_is_named_and_makes_answer_incomplete():
    snapshot = make_snapshot(NOW)
    broken = dataclasses.replace(snapshot, unreachable=frozenset({"bundesliga"}), loaded=snapshot.loaded - {"bundesliga"})
    result = ask(broken, wettbewerb="alle", zeitraum="kommendes_wochenende")
    assert "Bundesliga" not in names(result) and "2. Bundesliga" in names(result)
    assert hints(result, "Die Daten für Bundesliga sind gerade nicht erreichbar")
    empty = ask(broken, wettbewerb="alle", zeitraum="heute")
    assert "naechster_tag_mit_spielen" not in empty
    assert hints(empty, "In den bisher geladenen Daten")


def test_invalid_shortcut_makes_answer_incomplete_even_when_loaded():
    snapshot = make_snapshot(NOW)
    invalid = dataclasses.replace(snapshot, invalid_shortcuts=frozenset({"bl1"}))
    result = ask(invalid, wettbewerb="alle", zeitraum="heute")
    assert "naechster_tag_mit_spielen" not in result
    assert hints(result, "Für Bundesliga fehlen noch Daten für die neue Saison")


def test_nothing_usable_is_an_error_not_an_empty_answer():
    snapshot = make_snapshot(NOW)
    unloaded = dataclasses.replace(snapshot, loaded=frozenset())
    error = error_of(snapshot=unloaded, zeitraum="heute")
    assert error.kind is ErrorKind.UNREACHABLE and "Versuch es gleich noch einmal" in error.text
    gone = dataclasses.replace(snapshot, unreachable=frozenset(COMPETITION_KEYS), loaded=frozenset())
    assert error_of(snapshot=gone).kind is ErrorKind.UNREACHABLE
    invalid = dataclasses.replace(snapshot, invalid_shortcuts=frozenset({"bl1"}))
    assert error_of(snapshot=invalid, wettbewerb="bundesliga").kind is ErrorKind.SEASON_MISSING
    none_selected = Settings(competitions=(), main_team=None, favorite_teams=())
    assert error_of(settings=none_selected).kind is ErrorKind.UNSUPPORTED


def test_round_of_a_competition_that_is_still_loading_is_an_error():
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"bundesliga"})
    error = error_of(snapshot=waiting, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert error.kind is ErrorKind.UNREACHABLE and "Versuch es gleich noch einmal" in error.text


def test_no_further_match_in_the_data_is_a_certain_statement_only_when_complete():
    snapshot = make_snapshot(datetime(2027, 6, 20, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, wettbewerb="bundesliga", zeitraum="heute")
    assert "naechster_tag_mit_spielen" not in result and "grund" not in result
    assert hints(result, "ist danach kein weiteres Spiel eingetragen")
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"bundesliga"})
    assert error_of(snapshot=waiting, wettbewerb="bundesliga").kind is ErrorKind.UNREACHABLE


def test_season_hint_when_the_season_has_ended():
    snapshot = make_snapshot(NOW)
    ended = dataclasses.replace(snapshot, ended_season=MappingProxyType({"bundesliga": snapshot.season}))
    result = ask(ended, wettbewerb="bundesliga", zeitraum="heute")
    assert hints(result, "Endstand der Saison 2026/27 in Bundesliga.")
    assert not hints(result, "für die neue Saison ist noch nichts eingetragen")
    coming = ask(ended, wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert hints(coming, "Endstand der Saison 2026/27 in Bundesliga, für die neue Saison ist noch nichts eingetragen.")
    day = ask(ended, wettbewerb="bundesliga", datum="2026-12-31")  # ein Tag in der Zukunft, aber kein "nächster Spieltag"
    assert not hints(day, "für die neue Saison ist noch nichts eingetragen")


def test_unclear_result_gives_no_goals_and_a_hint():
    snapshot = make_snapshot(NOW)
    target = next(m for m in snapshot.matches["bundesliga"] if m.id == 83234)
    unclear = dataclasses.replace(snapshot, matches=MappingProxyType({
        k: tuple(dataclasses.replace(m, result_unclear=True) if m.id == target.id else m for m in v)
        for k, v in snapshot.matches.items()
    }))
    result = ask(unclear, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    entry = games(result)[0]
    assert "ergebnis" not in entry and "tore" not in entry
    assert hints(result, "nicht eindeutig")


def test_answer_has_no_identifiers_or_links():
    result = ask(zeitraum="letztes_wochenende")
    assert result["wettbewerbe"]
    for game in games(result):
        assert not {"id", "heim_id", "team_id", "url", "icon"} & set(game)


def test_next_round_is_not_a_rescheduled_old_round():
    # Frauen-Bundesliga: Bremen gegen Hoffenheim (7. Spieltag) ist auf Do 29.10. verlegt, nach dem 8. Spieltag.
    for day in (26, 28):
        snapshot = make_snapshot(datetime(2026, 10, day, 12, 0, tzinfo=BERLIN))
        last = ask(snapshot, wettbewerb="frauen_bundesliga", zeitraum="letzter_spieltag")
        assert last["zeitraum"].startswith("8. Spieltag (Freitag, 23. Oktober bis Sonntag, 25. Oktober)")
        result = ask(snapshot, wettbewerb="frauen_bundesliga", zeitraum="naechster_spieltag")
        assert result["zeitraum"] == "9. Spieltag (Freitag, 6. November bis Montag, 9. November)"
        assert {g["runde"] for g in games(result)} == {"9. Spieltag"} and len(games(result)) == 7
        assert not hints(result, "Nachholspiel")


def test_only_a_rescheduled_match_left_is_named_as_such():
    snapshot = make_snapshot(datetime(2026, 10, 28, 12, 0, tzinfo=BERLIN))
    matches = dict(snapshot.matches)
    matches["frauen_bundesliga"] = tuple(m for m in matches["frauen_bundesliga"] if m.round_order <= 8)
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    result = ask(snapshot, wettbewerb="frauen_bundesliga", zeitraum="naechster_spieltag")
    (game,) = games(result)
    assert (game["runde"], game["heim"], game["gast"]) == ("7. Spieltag", "SV Werder Bremen Frauen", "TSG 1899 Hoffenheim Frauen")
    assert game["zustand"] == "angesetzt" and game["wann"] == "Donnerstag, 29. Oktober, 18:00 Uhr"
    assert result["zeitraum"] == "7. Spieltag (Donnerstag, 29. Oktober)"
    assert hints(result, "Nachholspiel")


def test_national_team_round_is_the_last_or_next_match_day():
    result = ask(wettbewerb="nationalmannschaft", zeitraum="letzter_spieltag")
    assert result["zeitraum"] == "Montag, 16. November"
    (game,) = games(result)
    assert (game["heim"], game["gast"], game["zustand"]) == ("Deutschland", "Niederlande", "beendet")
    friday = make_snapshot(datetime(2026, 11, 14, 12, 0, tzinfo=BERLIN))
    last = ask(friday, wettbewerb="nationalmannschaft", zeitraum="letzter_spieltag")
    assert last["zeitraum"] == "Freitag, 13. November" and [g["gast"] for g in games(last)] == ["Deutschland"]
    nxt = ask(friday, wettbewerb="nationalmannschaft", zeitraum="naechster_spieltag")
    assert nxt["zeitraum"] == "Montag, 16. November" and [g["heim"] for g in games(nxt)] == ["Deutschland"]
    none = ask(wettbewerb="nationalmannschaft", zeitraum="naechster_spieltag")
    assert none["wettbewerbe"] == [] and hints(none, "Im Wettbewerb Nationalmannschaft ist kein weiteres Länderspiel eingetragen.")


def test_round_stands_on_the_group_when_it_is_the_same_for_all():
    result = ask(wettbewerb="bundesliga", zeitraum="kommendes_wochenende")
    (group,) = result["wettbewerbe"]
    assert group["runde"] == "10. Spieltag" and all("runde" not in g for g in group["spiele"])


def test_rounds_that_differ_inside_a_group_stay_on_the_matches():
    snapshot = make_snapshot(NOW)
    target = next(m for m in snapshot.matches["bundesliga"] if m.id == 83243)  # Bremen gegen HSV, So 22.11.
    changed = dataclasses.replace(target, round_name="9. Spieltag", round_order=9)
    matches = {k: tuple(changed if m.id == target.id else m for m in v) for k, v in snapshot.matches.items()}
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    (group,) = ask(snapshot, wettbewerb="bundesliga", zeitraum="kommendes_wochenende")["wettbewerbe"]
    assert "runde" not in group
    assert sorted({g["runde"] for g in group["spiele"]}) == ["10. Spieltag", "9. Spieltag"]


def test_shortened_answer_leaves_out_fixed_time_flags_but_keeps_the_open_ones():
    result = ask(datum="2027-01-31")  # 16 Spiele, darunter das Derby; alle Uhrzeiten noch nicht fest
    assert len(games(result)) == 16 > MAX_DETAILED
    flags = [g["uhrzeit_fest"] for g in games(result) if "uhrzeit_fest" in g]
    assert flags and all(f is False for f in flags)
    derby = next(g for g in games(result) if g["heim"] == "1. FC Nürnberg")
    assert derby["gast"] == "SpVgg Greuther Fürth" and derby["uhrzeit_fest"] is False
    assert not hints(result, "Die Uhrzeiten stehen fest")  # nichts weggelassen, also nichts zu erklären
    # Ohne Kürzung bleibt die Angabe an jedem angesetzten Spiel.
    small = ask(wettbewerb="bundesliga", zeitraum="kommendes_wochenende")
    assert all(g["uhrzeit_fest"] is True for g in games(small)) and not hints(small, "Die Uhrzeiten stehen fest")


def test_shortened_answer_has_at_most_thirty_matches_favorites_first():
    result = ask(wettbewerb="alle", zeitraum="kommendes_wochenende")  # 9 + 9 + 10 + 6 = 34 Spiele
    spiele = games(result)
    assert len(spiele) == 30 and result["gekuerzt"] is True
    heim_gast = {(g["heim"], g["gast"]) for g in spiele}
    assert ("1. FC Nürnberg", "Eintracht Braunschweig") in heim_gast
    assert ("Hannover 96", "SpVgg Greuther Fürth") in heim_gast
    assert hints(result, "ich nenne nur")
    assert all("uhrzeit_fest" not in g for g in spiele) and hints(result, "Die Uhrzeiten stehen fest, außer")


@pytest.mark.parametrize("settings", [SETTINGS_CLUB, Settings(competitions=COMPETITION_KEYS, main_team=79, favorite_teams=(115,))])
@pytest.mark.parametrize("now", [NOW, datetime(2026, 12, 13, 16, 0, tzinfo=BERLIN)])  # zweiter: größter Fall der Aufzeichnungen
def test_biggest_answer_stays_small_enough_for_the_tool(settings, now):
    result = ask(make_snapshot(now), settings=settings, wettbewerb="alle", zeitraum="kommendes_wochenende")
    size = len(json.dumps(result, ensure_ascii=False))
    # 8000 seit 0.1.3: Jedes laufende Spiel trägt Uhrzeit und Vorbehalt (`stand_von`, `vorbehalt`, zusammen etwa
    # 125 Zeichen); im zweiten Fall laufen 6 der 30 Spiele. Schlimmster Fall gemessen 7589 Zeichen: ein ganzer
    # Bundesliga-Spieltag läuft in einer gekürzten Antwort mit 30 Spielen (siehe den nächsten Test).
    assert size < 8000, size


def test_answer_with_a_whole_round_running_stays_small_enough_for_the_tool():
    # Schlimmster realistischer Fall: der ganze 10. Spieltag der Bundesliga gleichzeitig um 15:30 Uhr (wie am
    # letzten Spieltag), dazu die laufenden Spiele der anderen Ligen; gekürzte Antwort mit 30 Spielen.
    # Gemessen 7589 Zeichen: 15 der 30 Spiele laufen, jedes mit `stand_von` und `vorbehalt`.
    kickoff = datetime(2026, 11, 21, 15, 30, tzinfo=BERLIN)
    base = make_snapshot(datetime(2026, 11, 21, 16, 10, tzinfo=BERLIN))
    round_ten = tuple(
        dataclasses.replace(m, kickoff=kickoff) if m.round_order == 10 else m for m in base.matches["bundesliga"]
    )
    snapshot = dataclasses.replace(base, matches=MappingProxyType({**base.matches, "bundesliga": round_ten}))
    result = ask(snapshot, wettbewerb="alle", zeitraum="kommendes_wochenende")
    assert result["gekuerzt"] is True and sum(g["zustand"] == "läuft" for g in games(result)) == 15
    size = len(json.dumps(result, ensure_ascii=False))
    assert size < 8000, size


def _finish(snapshot, key, match_id):
    """Das Spiel als beendet mit 1:0 (die Aufzeichnung führt Spiele der Zukunft als nicht beendet)."""
    done = {"finished": True, "final": Score(1, 0), "final_phase": Phase.REGULAR, "regular": Score(1, 0)}
    matches = {
        k: tuple(dataclasses.replace(m, **done) if m.id == match_id else m for m in v) for k, v in snapshot.matches.items()
    }
    return dataclasses.replace(snapshot, matches=MappingProxyType(matches))


def test_a_round_that_has_begun_is_still_the_next_one_for_its_remaining_matches():
    # Samstag 12:00, das Freitagsspiel (Bayern gegen Köln) ist beendet: der 10. Spieltag ist weiter der nächste.
    snapshot = _finish(make_snapshot(datetime(2026, 11, 21, 12, 0, tzinfo=BERLIN)), "bundesliga", 83237)
    last = ask(snapshot, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert last["zeitraum"] == "10. Spieltag (Freitag, 20. November)" and len(games(last)) == 1  # nur das beendete Spiel
    assert hints(last, "Von diesem Spieltag sind noch nicht alle Spiele beendet; ich nenne nur die beendeten.")
    result = ask(snapshot, wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert result["zeitraum"] == "10. Spieltag (Samstag, 21. November bis Sonntag, 22. November)"
    spiele = games(result)
    assert len(spiele) == 8 and {g["zustand"] for g in spiele} == {"angesetzt"} and {g["runde"] for g in spiele} == {"10. Spieltag"}
    assert all(g["gast"] != "1. FC Köln" for g in spiele)
    assert hints(result, "Von diesem Spieltag sind schon Spiele gespielt; ich nenne nur die, die noch ausstehen.")
    assert not hints(result, "Nachholspiel")


def test_cup_round_over_two_days_is_not_a_catch_up():
    # Mittwoch 28.10. 12:00, das Achtelfinale ist noch nicht ausgelost (aus den Daten entfernt).
    snapshot = make_snapshot(datetime(2026, 10, 28, 12, 0, tzinfo=BERLIN))
    matches = dict(snapshot.matches)
    matches["dfb_pokal"] = tuple(m for m in matches["dfb_pokal"] if m.round_name != "Achtelfinale")
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    result = ask(snapshot, wettbewerb="dfb_pokal", zeitraum="naechster_spieltag")
    assert result["zeitraum"] == "2. Runde (Mittwoch, 28. Oktober)"
    assert len(games(result)) == 8 and {g["runde"] for g in games(result)} == {"2. Runde"}
    assert not hints(result, "Nachholspiel")
    assert hints(result, "Von dieser Runde sind schon Spiele gespielt; ich nenne nur die, die noch ausstehen.")


def test_catch_up_hint_says_round_in_cups():
    snapshot = make_snapshot(datetime(2026, 12, 3, 12, 0, tzinfo=BERLIN))
    first_round = next(m for m in snapshot.matches["dfb_pokal"] if m.round_name == "1. Runde")
    open_ = {"finished": False, "final": None, "final_phase": None, "regular": None, "goals": ()}
    changed = []
    for m in snapshot.matches["dfb_pokal"]:
        if m.id == first_round.id:  # ein verlegtes Spiel der 1. Runde, erst am 8.12.
            m = dataclasses.replace(m, kickoff=m.kickoff.replace(month=12, day=8), **open_)
        elif m.round_name == "Achtelfinale":  # die Achtelfinals sind gespielt
            m = dataclasses.replace(m, finished=True, final=Score(1, 0), final_phase=Phase.REGULAR, regular=Score(1, 0))
        changed.append(m)
    matches = dict(snapshot.matches)
    matches["dfb_pokal"] = tuple(sorted(changed, key=lambda m: (m.kickoff, m.id)))
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    result = ask(snapshot, wettbewerb="dfb_pokal", zeitraum="naechster_spieltag")
    (game,) = games(result)
    assert game["runde"] == "1. Runde" and game["zustand"] == "angesetzt"
    assert hints(result, "Das ist ein Nachholspiel: Die übrigen Spiele der Runde sind schon gespielt.")
    assert not hints(result, "Spieltag")


def test_a_competition_removed_entirely_by_the_limit_still_says_so(monkeypatch):
    monkeypatch.setattr(logic_spiele, "MAX_SHORTENED", 2)
    result = ask(wettbewerb="alle", zeitraum="kommendes_wochenende")
    assert names(result) == ["2. Bundesliga"] and len(games(result)) == 2  # nur die beiden Spiele der Lieblingsvereine
    assert result["gekuerzt"] is True
    assert hints(result, "Im Wettbewerb Bundesliga gibt es 9 Spiele; ich nenne davon keins.")
    assert hints(result, "Im Wettbewerb Frauen-Bundesliga gibt es 6 Spiele; ich nenne davon keins.")


def test_running_national_match_counts_for_the_match_day():
    base = make_snapshot(datetime(2026, 11, 16, 21, 0, tzinfo=BERLIN))
    target = next(m for m in base.matches["nationalmannschaft"] if m.kickoff.astimezone(BERLIN).day == 16)
    live = dataclasses.replace(target, finished=False, final=None, final_phase=None, regular=None)
    matches = {k: tuple(live if m.id == target.id else m for m in v) for k, v in base.matches.items()}
    snapshot = dataclasses.replace(base, matches=MappingProxyType(matches))
    for which in ("letzter_spieltag", "naechster_spieltag"):
        result = ask(snapshot, wettbewerb="nationalmannschaft", zeitraum=which)
        assert result["zeitraum"] == "Montag, 16. November"
        (game,) = games(result)
        assert game["zustand"] == "läuft"


@pytest.mark.parametrize("day", [20, 21, 22, 23])
def test_a_rescheduled_match_of_the_last_round_does_not_displace_the_real_next_round(day):
    # ffb1: Bremen gegen Hoffenheim (7. Spieltag) ist auf 29.10. verlegt; der 8. Spieltag beginnt am Freitag 23.10.
    snapshot = make_snapshot(datetime(2026, 10, day, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, wettbewerb="frauen_bundesliga", zeitraum="naechster_spieltag")
    assert result["zeitraum"] == "8. Spieltag (Freitag, 23. Oktober bis Sonntag, 25. Oktober)"
    assert len(games(result)) == 7 and {g["runde"] for g in games(result)} == {"8. Spieltag"}
    assert not hints(result, "Nachholspiel") and not hints(result, "ausstehen")


def test_last_round_shows_only_finished_matches_and_their_dates():
    snapshot = make_snapshot(datetime(2026, 10, 20, 12, 0, tzinfo=BERLIN))
    result = ask(snapshot, wettbewerb="frauen_bundesliga", zeitraum="letzter_spieltag")
    assert result["zeitraum"] == "7. Spieltag (Donnerstag, 15. Oktober bis Montag, 19. Oktober)"
    spiele = games(result)
    assert len(spiele) == 6 and {g["zustand"] for g in spiele} == {"beendet"}
    assert all(g["heim"] != "SV Werder Bremen Frauen" for g in spiele)
    assert hints(result, "Von diesem Spieltag sind noch nicht alle Spiele beendet; ich nenne nur die beendeten.")


def test_a_match_moved_far_into_the_future_does_not_displace_the_next_round():
    snapshot = make_snapshot(NOW)
    target = next(m for m in snapshot.matches["bundesliga"] if m.id == 83234)  # 9. Spieltag, Hamburg gegen Gladbach
    moved = dataclasses.replace(
        target,
        finished=False, final=None, final_phase=None, regular=None, goals=(),
        kickoff=datetime(2026, 12, 16, 20, 30, tzinfo=BERLIN).astimezone(target.kickoff.tzinfo),
    )
    matches = {k: tuple(moved if m.id == target.id else m for m in v) for k, v in snapshot.matches.items()}
    snapshot = dataclasses.replace(snapshot, matches=MappingProxyType(matches))
    nxt = ask(snapshot, wettbewerb="bundesliga", zeitraum="naechster_spieltag")
    assert nxt["zeitraum"] == "10. Spieltag (Freitag, 20. November bis Sonntag, 22. November)" and len(games(nxt)) == 9
    last = ask(snapshot, wettbewerb="bundesliga", zeitraum="letzter_spieltag")
    assert last["zeitraum"] == "9. Spieltag (Samstag, 7. November bis Sonntag, 8. November)"  # ohne das verlegte Spiel
    assert len(games(last)) == 8


@pytest.mark.parametrize(("datum", "label"), [
    ("2025-11-22", "Samstag, 22. November 2025"),  # falsches Jahr
    ("1999-01-01", "Freitag, 1. Januar 1999"),
    ("2027-11-20", "Samstag, 20. November 2027"),
    ("2026-05-10", "Sonntag, 10. Mai"),  # vor der Saison
])
def test_day_outside_the_loaded_season_data_has_no_data_not_no_matches(datum, label):
    result = answer(make_snapshot(NOW), SETTINGS_CLUB, wettbewerb=None, zeitraum=None, datum=datum)
    assert_speakable(result)
    assert result["zeitraum"] == label and result["wettbewerbe"] == []
    assert result["hinweise"][0] == (
        f"Für {label} habe ich keine Daten. Ich kenne nur Spiele von Freitag, 7. August 2026 bis Sonntag, 23. Mai 2027."
    )
    assert not any("keine Spiele eingetragen" in h for h in result["hinweise"])
    assert "naechster_tag_mit_spielen" not in result and "grund" not in result


def test_day_inside_the_season_data_without_matches_still_says_no_matches():
    result = answer(make_snapshot(NOW), SETTINGS_CLUB, wettbewerb="dfb_pokal", zeitraum=None, datum="2026-11-21")
    assert any("keine Spiele eingetragen" in h for h in result["hinweise"])
    later = answer(make_snapshot(NOW), SETTINGS_CLUB, wettbewerb="dfb_pokal", zeitraum=None, datum="2027-03-03")
    assert later["hinweise"][0].startswith("Für Mittwoch, 3. März 2027 habe ich keine Daten.")  # noch nicht ausgelost
