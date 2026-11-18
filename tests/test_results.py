"""Ergebnisbausteine: Fehler, Rückfragen, Stand, Hinweise und Spieleinträge."""

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from custom_components.fussball_assist.models import Phase, Score
from custom_components.fussball_assist.results import (
    ambiguous_payload,
    asked_competition,
    available,
    default_league,
    ErrorKind,
    ToolError,
    error_payload,
    favorite_ids,
    given,
    guard,
    league_of,
    live_hints,
    loading_error,
    loading_hints,
    match_entry,
    match_hints,
    question_payload,
    round_matches,
    round_text,
    same_round_matches,
    searchable,
    scorers_stale_hints,
    season_hints,
    stand_phrase,
    table_stale_hints,
    unknown_team_error,
)
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import BERLIN, SETTINGS_CLUB, assert_speakable, make_index, make_snapshot

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)


def _entry(snapshot, key, match_id, team=None):
    match = next(m for m in snapshot.matches[key] if m.id == match_id)
    competition = snapshot.season.competitions[key]
    same = same_round_matches(match, snapshot.matches[key])
    return match, match_entry(match, snapshot, team=team, competition=competition, same_round=same)


def test_error_payload_and_tool_error():
    assert error_payload(ErrorKind.UNKNOWN_TEAM, "Das kenne ich nicht.") == {
        "fehler": "Das kenne ich nicht.",
        "art": "verein_unbekannt",
    }
    err = ToolError(ErrorKind.UNREACHABLE, "Nicht erreichbar.")
    assert err.kind is ErrorKind.UNREACHABLE and err.text == str(err) == "Nicht erreichbar."


def test_question_payload_lists_at_most_five_candidates():
    few = question_payload("Welchen?", ["A", "B"])
    assert few == {"rueckfrage": "Welchen?", "kandidaten": ["A", "B"]}
    many = question_payload("Welchen?", list("ABCDEFG"))
    assert many["kandidaten"] == list("ABCDE") and "genauen Namen" in many["rueckfrage"]


def test_stand_is_the_oldest_fetch_of_the_used_competitions():
    older = NOW - timedelta(hours=3, minutes=28)
    snapshot = make_snapshot(NOW)
    snapshot = dataclasses.replace(snapshot, fetched_at={**snapshot.fetched_at, "dfb_pokal": older})
    assert stand_phrase(snapshot, ["bundesliga", "dfb_pokal"]) == "17. November, 13:12 Uhr"
    assert stand_phrase(snapshot, ["bundesliga"]) == "17. November, 16:40 Uhr"
    assert stand_phrase(snapshot, []) == "17. November, 16:40 Uhr"


def test_guard_drops_invalid_and_unreachable_competitions():
    snapshot = make_snapshot(NOW, invalid_shortcuts=frozenset({"bl1"}), unreachable=frozenset({"dfb_pokal"}))
    assert guard(snapshot, ["bundesliga", "2_bundesliga", "dfb_pokal", "3_liga"]) == ("2_bundesliga", "3_liga")


def test_guard_error_kinds():
    snapshot = make_snapshot(NOW, invalid_shortcuts=frozenset({"bl1"}), unreachable=frozenset({"dfb_pokal"}))
    with pytest.raises(ToolError) as err:
        guard(snapshot, ["bundesliga"])
    assert err.value.kind is ErrorKind.SEASON_MISSING
    with pytest.raises(ToolError) as err:
        guard(snapshot, ["bundesliga", "dfb_pokal"])
    assert err.value.kind is ErrorKind.UNREACHABLE
    with pytest.raises(ToolError) as err:
        guard(snapshot, [])
    assert err.value.kind is ErrorKind.UNSUPPORTED


def test_season_hints_name_the_ended_season_per_competition():
    snapshot = make_snapshot(NOW)
    snapshot = dataclasses.replace(snapshot, ended_season={"bundesliga": snapshot.season, "3_liga": snapshot.season})
    assert season_hints(snapshot, COMPETITION_KEYS) == [
        "Endstand der Saison 2026/27 in Bundesliga.",
        "Endstand der Saison 2026/27 in 3. Liga.",
    ]
    assert season_hints(snapshot, ["3_liga"], upcoming=True) == [
        "Endstand der Saison 2026/27 in 3. Liga, für die neue Saison ist noch nichts eingetragen."
    ]
    assert season_hints(snapshot, ["dfb_pokal"]) == []


def test_loading_hints_one_sentence_per_kind():
    snapshot = make_snapshot(
        NOW,
        loaded=frozenset(COMPETITION_KEYS) - {"3_liga", "dfb_pokal", "europa_league", "bundesliga"},
        unreachable=frozenset({"bundesliga"}),
        invalid_shortcuts=frozenset({"uel2026"}),
    )
    hints = loading_hints(snapshot, COMPETITION_KEYS)
    assert len(hints) == 3
    assert "Europa League" in hints[0] and "Bundesliga" in hints[1]
    assert hints[2].startswith("Es sind noch nicht alle Wettbewerbe geladen") and "3. Liga und DFB-Pokal" in hints[2]
    assert loading_hints(make_snapshot(NOW), COMPETITION_KEYS) == []


def _running(snapshot, match_id=83237):  # Bayern gegen Köln, 20.11. 20:30 Uhr, hier ohne Ende und ohne Tore
    match = next(m for m in snapshot.matches["bundesliga"] if m.id == match_id)
    return dataclasses.replace(match, finished=False, final=None, final_phase=None, regular=None, goals=())


def test_live_hints_only_for_running_matches():
    snapshot = make_snapshot(datetime(2026, 11, 20, 20, 45, tzinfo=BERLIN))
    competition = snapshot.season.competitions["bundesliga"]
    running = _running(snapshot)
    finished = next(m for m in snapshot.matches["bundesliga"] if m.finished)
    assert live_hints(snapshot, [(finished, {"zustand": "beendet"}, competition)]) == []
    entry = match_entry(running, snapshot, team=None, competition=competition, same_round=[])
    assert live_hints(snapshot, [(running, entry, competition)]) == ["Der Stand kann einige Minuten hinterherhängen."]
    (note,) = live_hints(snapshot, [(running, {"zustand": "laut Daten noch nicht beendet"}, competition)])
    assert "noch nicht beendet" in note


def test_running_match_with_data_from_before_the_kickoff_has_no_score():
    # Stand von 08:00 Uhr, Anstoß 20:30 Uhr, jetzt 20:45 Uhr (zum Beispiel, weil die Quelle seither nicht antwortet).
    base = make_snapshot(datetime(2026, 11, 20, 20, 45, tzinfo=BERLIN))
    morning = datetime(2026, 11, 20, 8, 0, tzinfo=BERLIN)
    snapshot = dataclasses.replace(base, fetched_at={**base.fetched_at, "bundesliga": morning})
    competition = snapshot.season.competitions["bundesliga"]
    match = next(m for m in snapshot.matches["bundesliga"] if m.id == 83237)
    running = dataclasses.replace(_running(snapshot), goals=match.goals[:1])
    entry = match_entry(running, snapshot, team=None, competition=competition, same_round=[])
    assert entry["zustand"] == "läuft" and "ergebnis" not in entry and "tore" not in entry
    assert match_hints(running, entry) == []
    assert live_hints(snapshot, [(running, entry, competition)]) == [
        "Das Spiel FC Bayern München gegen 1. FC Köln läuft; mein letzter Stand ist von 08:00 Uhr,"
        " deshalb kann ich keinen Spielstand sagen."
    ]
    assert stand_phrase(snapshot, ["bundesliga"]) == "20. November, 08:00 Uhr"


@pytest.mark.parametrize(("fetched", "known"), [
    (datetime(2026, 11, 20, 20, 44, tzinfo=BERLIN), True),  # nach dem Anstoß, eine Minute alt
    (datetime(2026, 11, 20, 20, 35, tzinfo=BERLIN), True),  # genau zehn Minuten alt
    (datetime(2026, 11, 20, 20, 34, tzinfo=BERLIN), False),  # elf Minuten alt
    (datetime(2026, 11, 20, 20, 30, tzinfo=BERLIN), False),  # genau beim Anstoß abgerufen
    (datetime(2026, 11, 19, 20, 44, tzinfo=BERLIN), False),  # vom Vortag
])
def test_running_match_gets_a_score_only_from_fresh_data(fetched, known):
    base = make_snapshot(datetime(2026, 11, 20, 20, 45, tzinfo=BERLIN))
    snapshot = dataclasses.replace(base, fetched_at={**base.fetched_at, "bundesliga": fetched})
    competition = snapshot.season.competitions["bundesliga"]
    running = _running(snapshot)
    entry = match_entry(running, snapshot, team=None, competition=competition, same_round=[])
    assert ("ergebnis" in entry) is known
    hints = live_hints(snapshot, [(running, entry, competition)])
    assert ("Der Stand kann einige Minuten hinterherhängen." in hints) is known
    assert any("keinen Spielstand" in h for h in hints) is not known


def test_several_running_matches_without_fresh_data_share_one_sentence():
    base = make_snapshot(datetime(2026, 11, 20, 20, 45, tzinfo=BERLIN))
    snapshot = dataclasses.replace(base, fetched_at={**base.fetched_at, "bundesliga": base.now - timedelta(hours=3)})
    competition = snapshot.season.competitions["bundesliga"]
    running = _running(snapshot)
    entry = match_entry(running, snapshot, team=None, competition=competition, same_round=[])
    assert live_hints(snapshot, [(running, entry, competition)] * 3) == [
        "3 Spiele laufen; mein letzter Stand ist von 17:45 Uhr, deshalb kann ich keinen Spielstand sagen."
    ]


def test_stand_of_tables_and_scorers_is_their_own_fetch():
    snapshot = make_snapshot(NOW)
    yesterday = NOW - timedelta(days=1, minutes=10)
    snapshot = dataclasses.replace(
        snapshot,
        tables_at={**snapshot.tables_at, "2_bundesliga": yesterday},
        scorers_at={**snapshot.scorers_at, "bundesliga": yesterday - timedelta(hours=1)},
    )
    assert stand_phrase(snapshot, ["2_bundesliga"]) == "17. November, 16:40 Uhr"  # Spieldaten frisch
    assert stand_phrase(snapshot, tables=["2_bundesliga"]) == "16. November, 16:30 Uhr"
    assert stand_phrase(snapshot, scorers=["bundesliga"]) == "16. November, 15:30 Uhr"
    assert stand_phrase(snapshot, ["bundesliga"], tables=["2_bundesliga"]) == "16. November, 16:30 Uhr"


def test_stale_table_and_scorer_hints_name_the_competition():
    snapshot = make_snapshot(NOW, stale_tables=frozenset({"2_bundesliga"}), stale_scorers=frozenset({"bundesliga"}))
    (table,) = table_stale_hints(snapshot, ["bundesliga", "2_bundesliga"])
    assert "Tabelle von 2. Bundesliga" in table and "die Tabelle enthält das noch nicht" in table
    (scorers,) = scorers_stale_hints(snapshot, ["bundesliga"])
    assert "Torjägerliste von Bundesliga" in scorers
    assert table_stale_hints(make_snapshot(NOW), COMPETITION_KEYS) == []


def test_scheduled_match_without_team_has_no_result_fields():
    snapshot = make_snapshot(NOW)
    match = next(m for m in snapshot.matches["bundesliga"] if m.id == 83237)  # Bayern gegen Köln, 20.11. 20:30 Uhr
    _, entry = _entry(snapshot, "bundesliga", 83237)
    assert entry == {
        "wettbewerb": "Bundesliga",
        "runde": "10. Spieltag",
        "wann": "Freitag, 20. November, 20:30 Uhr",
        "bezug": "in 3 Tagen",
        "uhrzeit_fest": True,
        "heim": "FC Bayern München",
        "gast": "1. FC Köln",
        "zustand": "angesetzt",
    }
    assert match_hints(match, entry) == []
    assert_speakable(entry)


def test_countdown_replaces_relative_phrase_within_24_hours():
    snapshot = make_snapshot(datetime(2026, 11, 20, 18, 0, tzinfo=BERLIN))
    _, entry = _entry(snapshot, "bundesliga", 83237)
    assert entry["bezug"] == "in 2 Stunden"
    snapshot = make_snapshot(datetime(2026, 11, 20, 12, 0, tzinfo=BERLIN))
    assert _entry(snapshot, "bundesliga", 83237)[1]["bezug"] == "in 8 Stunden"
    snapshot = make_snapshot(datetime(2026, 11, 19, 20, 0, tzinfo=BERLIN))
    assert _entry(snapshot, "bundesliga", 83237)[1]["bezug"] == "morgen"


def test_running_match_without_goals_is_nil_nil_and_not_finished_has_no_result():
    base = make_snapshot(datetime(2026, 11, 20, 20, 45, tzinfo=BERLIN))
    match = next(m for m in base.matches["bundesliga"] if m.id == 83237)
    running = dataclasses.replace(match, finished=False, final=None, final_phase=None, regular=None, goals=())
    entry = match_entry(
        running, base, team=None, competition=base.season.competitions["bundesliga"], same_round=[]
    )
    assert entry["zustand"] == "läuft" and entry["ergebnis"] == "0:0" and "tore" not in entry
    assert "uhrzeit_fest" not in entry and entry["wann"].endswith("20:30 Uhr")
    late = make_snapshot(datetime(2026, 11, 21, 0, 30, tzinfo=BERLIN))
    entry = match_entry(running, late, team=None, competition=late.season.competitions["bundesliga"], same_round=[])
    assert entry["zustand"] == "laut Daten noch nicht beendet" and "ergebnis" not in entry


def test_unclear_result_drops_result_fields_and_goals_and_is_explained():
    snapshot = make_snapshot(NOW)
    finished = next(m for m in snapshot.matches["bundesliga"] if m.finished and m.final is not None and m.goals)
    competition = snapshot.season.competitions["bundesliga"]
    plain = match_entry(finished, snapshot, team=finished.home, competition=competition, same_round=[], ids=frozenset({finished.home.id}))
    assert "tore" in plain  # ohne Unklarheit stehen die Tore da
    unclear = dataclasses.replace(finished, result_unclear=True)
    entry = match_entry(
        unclear, snapshot, team=unclear.home, competition=competition, same_round=[], ids=frozenset({unclear.home.id})
    )
    assert not {"ergebnis", "ausgang", "tore"} & set(entry)
    assert entry["ort"] == "heim" and entry["gegner"] == unclear.away.name
    assert match_hints(unclear, entry) == [
        "Das Ergebnis ist in den Daten nicht eindeutig.",
        "Wegen des unklaren Ergebnisses nenne ich auch keine Tore.",
    ]


def test_outcome_is_told_from_the_view_of_the_club():
    snapshot = make_snapshot(NOW)
    competition = snapshot.season.competitions["bundesliga"]
    finished = next(m for m in snapshot.matches["bundesliga"] if m.finished and m.final is not None)

    def entry_for(match, team):
        return match_entry(match, snapshot, team=team, competition=competition, same_round=[], ids=frozenset({team.id}))

    away_win = dataclasses.replace(finished, final=Score(1, 2), final_phase=Phase.REGULAR, regular=Score(1, 2), result_unclear=False)
    guest = entry_for(away_win, away_win.away)
    assert guest["ort"] == "auswärts" and guest["ergebnis"] == "1:2" and guest["ausgang"] == "2:1-Sieg"
    host = entry_for(away_win, away_win.home)
    assert host["ort"] == "heim" and host["ergebnis"] == "1:2" and host["ausgang"] == "1:2-Niederlage"
    draw = dataclasses.replace(away_win, final=Score(3, 3), regular=Score(3, 3))
    assert entry_for(draw, draw.away)["ausgang"] == "3:3-Unentschieden"
    assert entry_for(draw, draw.home)["ausgang"] == "3:3-Unentschieden"


def test_match_entry_with_team_needs_ids():
    snapshot = make_snapshot(NOW)
    match = snapshot.matches["bundesliga"][0]
    competition = snapshot.season.competitions["bundesliga"]
    with pytest.raises(ValueError, match="ids"):
        match_entry(match, snapshot, team=match.home, competition=competition, same_round=[])


def test_placeholder_time_shows_only_the_day():
    snapshot = make_snapshot(NOW)
    match = next(
        m for m in snapshot.matches["2_bundesliga"] if m.round_order == 19 and m.home.name == "1. FC Nürnberg"
    )
    entry = match_entry(
        match,
        snapshot,
        team=None,
        competition=snapshot.season.competitions["2_bundesliga"],
        same_round=same_round_matches(match, snapshot.matches["2_bundesliga"]),
    )
    assert entry["wann"] == "Sonntag, 31. Januar 2027" and entry["uhrzeit_fest"] is False
    assert "Uhrzeit noch nicht fest" in match_hints(match, entry)[0]


def test_given_treats_empty_values_as_not_given():
    assert given(None) is None and given("") is None and given("  \t") is None
    assert given("  Köln ") == "Köln" and given(0) == 0 and given(False) is False


def test_round_text_adds_the_missing_space():
    assert round_text("1.Spieltag") == "1. Spieltag" and round_text(" 7. Spieltag ") == "7. Spieltag"
    assert round_text("Achtelfinale") == "Achtelfinale" and round_text("") == ""


def test_loading_error_names_what_is_missing():
    snapshot = make_snapshot(NOW)
    assert loading_error(snapshot, COMPETITION_KEYS, "die Spiele") is None
    unreachable = dataclasses.replace(snapshot, unreachable=frozenset({"bundesliga"}))
    error = loading_error(unreachable, COMPETITION_KEYS, "die Spiele")
    assert error.kind is ErrorKind.UNREACHABLE and "die Spiele" in error.text and "Bundesliga" in error.text
    invalid = dataclasses.replace(snapshot, invalid_shortcuts=frozenset({"bl2"}))  # auch geladene zählen
    error = loading_error(invalid, COMPETITION_KEYS, "die Spiele")
    assert error.kind is ErrorKind.SEASON_MISSING and "2. Bundesliga" in error.text
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"dfb_pokal", "3_liga"})
    error = loading_error(waiting, COMPETITION_KEYS, "die Spiele")
    assert error.kind is ErrorKind.UNREACHABLE and "3. Liga und DFB-Pokal" in error.text
    assert "Versuch es gleich noch einmal" in error.text
    assert loading_error(waiting, ("bundesliga",), "die Spiele") is None


def test_available_needs_loaded_reachable_and_valid_data():
    snapshot = make_snapshot(NOW)
    assert available(snapshot, COMPETITION_KEYS) == COMPETITION_KEYS
    broken = dataclasses.replace(
        snapshot,
        loaded=snapshot.loaded - {"3_liga"},
        unreachable=frozenset({"dfb_pokal"}),
        invalid_shortcuts=frozenset({"bl1"}),  # geladen, aber ungültig
    )
    assert available(broken, COMPETITION_KEYS) == tuple(
        k for k in COMPETITION_KEYS if k not in ("bundesliga", "3_liga", "dfb_pokal")
    )


def test_searchable_leaves_out_women_and_the_national_team():
    snapshot = make_snapshot(NOW)
    assert searchable(snapshot, SETTINGS_CLUB) == (
        "bundesliga", "2_bundesliga", "3_liga", "dfb_pokal", "champions_league", "europa_league", "conference_league",
    )
    chosen = dataclasses.replace(SETTINGS_CLUB, competitions=("2_bundesliga", "frauen_bundesliga", "nationalmannschaft"))
    assert searchable(snapshot, chosen) == ("2_bundesliga",)


def test_unknown_team_error_waits_for_the_data():
    snapshot = make_snapshot(NOW)
    error = unknown_team_error(snapshot, searchable(snapshot, SETTINGS_CLUB), "Xyz")
    assert error.kind is ErrorKind.UNKNOWN_TEAM and "„Xyz“" in error.text
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"3_liga"})
    error = unknown_team_error(waiting, searchable(waiting, SETTINGS_CLUB), "Xyz")
    assert error.kind is ErrorKind.UNREACHABLE and "3. Liga" in error.text


def test_ambiguous_payload_lists_the_candidates():
    snapshot = make_snapshot(NOW)
    resolution = make_index(snapshot).resolve("Borussia")
    assert resolution.candidates
    payload = ambiguous_payload(resolution, "Borussia")
    assert "„Borussia“" in payload["rueckfrage"] and payload["kandidaten"] == [t.name for t in resolution.candidates]


def test_asked_competition_accepts_key_and_name_and_names_the_capable_ones():
    snapshot = make_snapshot(NOW)
    assert asked_competition(snapshot, SETTINGS_CLUB, "Bundesliga", lambda c: c.has_table) == "bundesliga"
    assert asked_competition(snapshot, SETTINGS_CLUB, "dfb_pokal", lambda c: c.has_scorers) == "dfb_pokal"
    with pytest.raises(ToolError) as caught:
        asked_competition(snapshot, SETTINGS_CLUB, "Kreisliga", lambda c: c.has_scorers)
    assert caught.value.kind is ErrorKind.INVALID_INPUT
    assert "nationalmannschaft" not in caught.value.text and "dfb_pokal" in caught.value.text
    narrow = dataclasses.replace(SETTINGS_CLUB, competitions=("bundesliga",))
    with pytest.raises(ToolError) as caught:
        asked_competition(snapshot, narrow, "dfb_pokal", lambda c: c.has_scorers)
    assert "nicht ausgewählt" in caught.value.text


def test_default_league_is_the_league_of_the_main_team_else_the_bundesliga():
    snapshot = make_snapshot(NOW)
    index = make_index(snapshot)
    assert default_league(snapshot, index, SETTINGS_CLUB, "Tabelle") == ("2_bundesliga", [])
    nobody = dataclasses.replace(SETTINGS_CLUB, main_team=None)
    assert default_league(snapshot, index, nobody, "Tabelle") == ("bundesliga", [])
    unknown = dataclasses.replace(SETTINGS_CLUB, main_team=999999)
    key, hints = default_league(snapshot, index, unknown, "Torjägerliste")
    assert key == "bundesliga" and hints == [
        "Dein Hauptverein kommt in keinem der ausgewählten Wettbewerbe vor; ich zeige deshalb die Torjägerliste von Bundesliga."
    ]
    # Die Liga kommt aus den Spiellisten, nicht aus den Tabellen.
    assert default_league(dataclasses.replace(snapshot, tables={}), index, SETTINGS_CLUB, "Tabelle") == ("2_bundesliga", [])
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"2_bundesliga"})
    with pytest.raises(ToolError) as caught:
        default_league(waiting, index, SETTINGS_CLUB, "Tabelle")
    assert caught.value.kind is ErrorKind.UNREACHABLE and "2. Bundesliga" in caught.value.text


def test_league_of_a_club_comes_from_the_match_lists_of_the_chosen_leagues():
    snapshot = dataclasses.replace(make_snapshot(NOW), tables={})
    index = make_index(snapshot)
    club = index.ids_of(index.by_id(79))
    assert league_of(snapshot, SETTINGS_CLUB, club, "die Liga") == "2_bundesliga"
    bayern = index.ids_of(index.resolve("Bayern").team)
    assert league_of(snapshot, SETTINGS_CLUB, bayern, "die Liga") == "bundesliga"
    madrid = index.ids_of(index.resolve("Real Madrid").team)
    assert league_of(snapshot, SETTINGS_CLUB, madrid, "die Liga") is None  # alle Ligen geladen: sicher in keiner
    # Eine Liga ohne eingetragene Spiele: nicht sicher, also kein "in keiner Liga".
    empty = dataclasses.replace(snapshot, matches={**snapshot.matches, "3_liga": ()})
    with pytest.raises(ToolError) as caught:
        league_of(empty, SETTINGS_CLUB, madrid, "die Liga")
    assert caught.value.kind is ErrorKind.NOTHING_ENTERED and "3. Liga" in caught.value.text
    # Gefunden ist gefunden, auch wenn eine andere Liga noch lädt.
    loading = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"3_liga"})
    assert league_of(loading, SETTINGS_CLUB, club, "die Liga") == "2_bundesliga"


def test_favorite_ids_collect_every_id_of_main_and_favourite_teams():
    snapshot = make_snapshot(NOW)
    assert favorite_ids(snapshot, SETTINGS_CLUB, make_index(snapshot)) == {79, 115}
    assert favorite_ids(snapshot, dataclasses.replace(SETTINGS_CLUB, main_team=None, favorite_teams=()), None) == frozenset()
    # ohne Index aus der Momentaufnahme gebaut; eine unbekannte Kennung bleibt, wie sie ist
    assert favorite_ids(snapshot, dataclasses.replace(SETTINGS_CLUB, favorite_teams=(999999,)), None) == {79, 999999}


def test_round_matches_gives_the_next_and_the_last_round():
    snapshot = make_snapshot(NOW)
    chosen, note = round_matches(snapshot, "bundesliga", "naechster_spieltag")
    assert {m.round_name for m in chosen} == {"10. Spieltag"} and len(chosen) == 9 and note == ""
    chosen, note = round_matches(snapshot, "bundesliga", "letzter_spieltag")
    assert {m.round_order for m in chosen} == {9} and note == ""


def test_round_matches_from_a_day_only_counts_matches_from_it():
    snapshot = make_snapshot(NOW)
    chosen, note = round_matches(snapshot, "bundesliga", "naechster_spieltag", from_day=date(2026, 11, 23))
    assert {m.round_name for m in chosen} == {"11. Spieltag"} and note == ""
    # Der Tag selbst zählt mit; der Freitag (20.11.) liegt davor, der Rest der Runde folgt mit dem Vermerk.
    chosen, note = round_matches(snapshot, "bundesliga", "naechster_spieltag", from_day=date(2026, 11, 21))
    assert {m.round_name for m in chosen} == {"10. Spieltag"} and len(chosen) == 8 and note == "begonnen"
    chosen, note = round_matches(snapshot, "bundesliga", "naechster_spieltag", from_day=date(2026, 11, 20))
    assert len(chosen) == 9 and note == ""
    assert round_matches(snapshot, "bundesliga", "naechster_spieltag", from_day=date(2030, 1, 1)) == ([], "")
