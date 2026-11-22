"""Antwort "Tabelle": Zonen und fertig berechnete Abstände aus den Aufzeichnungen (2. Bundesliga, Bundesliga, Champions League)."""

import dataclasses
from datetime import datetime
from types import MappingProxyType

import pytest

from custom_components.fussball_assist.logic_tabelle import answer
from custom_components.fussball_assist.results import ErrorKind, Settings, ToolError
from custom_components.fussball_assist.seasons import COMPETITION_KEYS, Zone

from .conftest import BERLIN, SETTINGS_CLUB, assert_speakable, fetched_earlier, make_index, make_snapshot

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)  # Dienstag
ENDSTAND = "Endstand der Saison 2026/27 in 2. Bundesliga."
ZONES = "Bei Plätzen ohne Zonenangabe geht es weder um Aufstieg noch um Relegation oder Abstieg."
LEAGUE_PHASE = "Für die Plätze der Ligaphase nenne ich keine Zonen; welche Plätze weiterkommen, ist in dieser Version nicht enthalten."
UNEQUAL = "Die Vereine haben unterschiedlich viele Spiele; die Abstände zählen nur die Punkte."


def ask(snapshot=None, settings=SETTINGS_CLUB, **kwargs):
    """Fragt mit den Vorgaben des Werkzeugs; jedes Ergebnis muss vorlesbar sein."""
    snapshot = snapshot or make_snapshot(NOW)
    params = {"wettbewerb": None, "verein": None, "ausschnitt": "spitze"}
    params.update(kwargs)
    result = answer(snapshot, make_index(snapshot), settings, **params)
    assert_speakable(result)
    return result


def error_of(**kwargs):
    with pytest.raises(ToolError) as caught:
        ask(**kwargs)
    return caught.value


def places(result):
    return [row["platz"] for row in result["zeilen"]]


def teams(result):
    return [row["verein"] for row in result["zeilen"]]


def test_default_is_the_league_of_the_main_team():
    result = ask()
    assert result["wettbewerb"] == "2. Bundesliga"
    assert result["stand"] == "17. November, 16:40 Uhr"
    assert places(result) == [1, 2, 3, 4, 5]
    assert teams(result) == ["Hertha BSC", "1. FC Nürnberg", "1. FC Kaiserslautern", "1. FC Magdeburg", "VfL Wolfsburg"]
    assert result["zeilen"][1] == {
        "platz": 2, "verein": "1. FC Nürnberg", "spiele": 11, "punkte": 24, "tore": 26, "gegentore": 13,
        "tordifferenz": 13, "siege": 7, "unentschieden": 3, "niederlagen": 1, "zone": "Aufstiegsplatz",
    }
    assert [row.get("zone") for row in result["zeilen"]] == [
        "Aufstiegsplatz", "Aufstiegsplatz", "Relegationsplatz für den Aufstieg", None, None,
    ]
    assert "abstaende" not in result  # nur mit Verein
    assert result["hinweise"] == [ZONES]


def test_section_around_a_team():
    result = ask(verein="Club")
    assert result["wettbewerb"] == "2. Bundesliga" and result["verein"] == "1. FC Nürnberg"
    assert places(result) == [1, 2, 3, 4]  # zwei darüber und darunter, soweit vorhanden
    assert result["abstaende"] == [
        "7 Punkte hinter Platz 1",
        "3 Punkte vor dem Relegationsplatz für den Aufstieg (Platz 3)",
    ]


def test_distances_are_computed_not_left_to_the_model():
    # Wolfsburg, Platz 5 mit 19 Punkten: Hertha 31, Kaiserslautern (Platz 3) 21, Magdeburg 20, Bielefeld 17, Karlsruhe (Platz 16) 11.
    result = ask(verein="Wolfsburg")
    assert places(result) == [3, 4, 5, 6, 7]
    assert result["abstaende"] == [
        "12 Punkte hinter Platz 1",
        "2 Punkte hinter dem Relegationsplatz für den Aufstieg (Platz 3)",
        "1 Punkt hinter Platz 4",
        "2 Punkte vor Platz 6",
        "8 Punkte vor dem Relegationsplatz gegen den Abstieg (Platz 16)",
    ]
    # Fürth, Platz 10 mit 12 Punkten: gleichauf mit Platz 9 (St. Pauli) und Platz 11 (Hannover).
    result = ask(verein="Fürth")
    assert result["abstaende"] == [
        "19 Punkte hinter Platz 1",
        "9 Punkte hinter dem Relegationsplatz für den Aufstieg (Platz 3)",
        "punktgleich mit Platz 9",
        "punktgleich mit Platz 11",
        "1 Punkt vor dem Relegationsplatz gegen den Abstieg (Platz 16)",
    ]


def test_leader_and_zone_edges():
    result = ask(verein="Hertha")
    assert places(result) == [1, 2, 3]
    assert result["abstaende"] == [
        "Tabellenführer, 7 Punkte vor Platz 2",
        "10 Punkte vor dem Relegationsplatz für den Aufstieg (Platz 3)",
    ]
    result = ask(verein="Karlsruhe")  # Platz 16, Relegationsplatz gegen den Abstieg
    assert places(result) == [14, 15, 16, 17, 18]
    assert result["abstaende"] == [
        "20 Punkte hinter Platz 1",
        "punktgleich mit Platz 15, dem ersten Platz ohne Relegationsplatz gegen den Abstieg",
        "2 Punkte vor dem Abstiegsplatz (Platz 17)",
    ]
    result = ask(verein="Darmstadt")  # Platz 17, Abstiegsplatz
    assert result["abstaende"] == [
        "22 Punkte hinter Platz 1",
        "2 Punkte hinter dem Relegationsplatz gegen den Abstieg (Platz 16)",
        "1 Punkt vor Platz 18",
    ]


def test_relegation_places():
    result = ask(wettbewerb="2_bundesliga", ausschnitt="ende")
    assert places(result) == [14, 15, 16, 17, 18]
    assert [row.get("zone") for row in result["zeilen"]] == [
        None, None, "Relegationsplatz gegen den Abstieg", "Abstiegsplatz", "Abstiegsplatz",
    ]
    assert result["zeilen"][-1]["verein"] == "Holstein Kiel" and result["zeilen"][-1]["punkte"] == 8


def test_whole_table():
    result = ask(wettbewerb="2_bundesliga", ausschnitt="ganz")
    assert places(result) == list(range(1, 19))


def test_league_of_the_named_team_wins():
    result = ask(verein="Bayern")
    assert result["wettbewerb"] == "Bundesliga" and result["verein"] == "FC Bayern München"
    assert places(result) == [1, 2, 3]
    assert result["abstaende"] == [
        "Tabellenführer, punktgleich mit Platz 2",
        "14 Punkte vor dem Relegationsplatz gegen den Abstieg (Platz 16)",
    ]
    assert [row.get("zone") for row in result["zeilen"]] == [None, None, None]


def test_european_league_phase_has_no_zones():
    result = ask(wettbewerb="champions_league", verein="Bayern")
    assert result["wettbewerb"] == "Champions League"
    assert places(result) == [11, 12, 13, 14, 15]
    assert all("zone" not in row for row in result["zeilen"])
    assert result["hinweise"] == [LEAGUE_PHASE]
    assert result["abstaende"] == ["4 Punkte hinter Platz 1", "1 Punkt hinter Platz 12", "1 Punkt vor Platz 14"]


def test_cup_and_national_team_have_no_table():
    for wettbewerb in ("dfb_pokal", "nationalmannschaft"):
        error = error_of(wettbewerb=wettbewerb)
        assert error.kind is ErrorKind.UNSUPPORTED and "Spiele" in error.text
    error = error_of(verein="Deutschland")
    assert error.kind is ErrorKind.UNSUPPORTED and "Spiele" in error.text


def test_team_not_in_this_table():
    error = error_of(wettbewerb="bundesliga", verein="Club")
    assert error.kind is ErrorKind.NOTHING_ENTERED
    assert "1. FC Nürnberg" in error.text and "Bundesliga" in error.text and "2. Bundesliga" in error.text


def test_invalid_section():
    error = error_of(ausschnitt="mitte")
    assert error.kind is ErrorKind.INVALID_INPUT and "spitze, ende, ganz" in error.text
    assert error_of(ausschnitt="mitte", verein="Club").kind is ErrorKind.INVALID_INPUT  # auch mit Verein geprüft


def test_section_of_a_team_ignores_the_given_section():
    assert places(ask(verein="Club", ausschnitt="ganz")) == [1, 2, 3, 4]
    assert places(ask(verein="Club", ausschnitt="ende")) == [1, 2, 3, 4]


def test_table_of_the_finished_season_says_so():
    snapshot = make_snapshot(NOW)
    ended = dataclasses.replace(snapshot, ended_season=MappingProxyType({"2_bundesliga": snapshot.season}))
    result = ask(ended)
    assert places(result) == [1, 2, 3, 4, 5]
    assert ENDSTAND in result["hinweise"]


def test_empty_table_without_a_finished_season():
    snapshot = make_snapshot(NOW)
    empty = dataclasses.replace(snapshot, tables=MappingProxyType({**snapshot.tables, "2_bundesliga": ()}))
    error = error_of(snapshot=empty, wettbewerb="2_bundesliga")
    assert error.kind is ErrorKind.NOTHING_ENTERED and "2. Bundesliga" in error.text
    # Mitten in der Saison (Spiele sind beendet) hat die Saison begonnen: kein Raten darüber.
    assert "nicht begonnen" not in error.text
    before = make_snapshot(datetime(2026, 8, 1, 12, 0, tzinfo=BERLIN))  # vor dem ersten Spiel
    early = dataclasses.replace(before, tables=MappingProxyType({**before.tables, "2_bundesliga": ()}))
    assert "die Saison hat vielleicht noch nicht begonnen" in error_of(snapshot=early, wettbewerb="2_bundesliga").text


def test_missing_data_is_a_loading_error_not_a_certainty():
    snapshot = make_snapshot(NOW)
    no_table = dataclasses.replace(snapshot, tables=MappingProxyType({"bundesliga": snapshot.tables["bundesliga"]}))
    error = error_of(snapshot=no_table, wettbewerb="2_bundesliga")
    assert error.kind is ErrorKind.UNREACHABLE and "Versuch es gleich noch einmal" in error.text
    unreachable = dataclasses.replace(snapshot, unreachable=frozenset({"2_bundesliga"}))
    assert error_of(snapshot=unreachable, wettbewerb="2_bundesliga").kind is ErrorKind.UNREACHABLE
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"2_bundesliga"})
    assert error_of(snapshot=waiting, wettbewerb="2_bundesliga").kind is ErrorKind.UNREACHABLE
    invalid = dataclasses.replace(snapshot, invalid_shortcuts=frozenset({"bl2"}))
    assert error_of(snapshot=invalid, wettbewerb="2_bundesliga").kind is ErrorKind.SEASON_MISSING


def test_league_that_cannot_be_determined_is_not_guessed():
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"2_bundesliga"})
    # Der Club spielt in der 2. Bundesliga, deren Tabelle fehlt: keine Vorgabe auf die Bundesliga, kein "steht nirgends".
    for kwargs in ({"verein": "Club"}, {}):
        error = error_of(snapshot=waiting, **kwargs)
        assert error.kind is ErrorKind.UNREACHABLE and "2. Bundesliga" in error.text
    # Bayern steht in der Bundesliga-Tabelle; die fehlende 2. Bundesliga stört nicht.
    assert ask(waiting, verein="Bayern")["wettbewerb"] == "Bundesliga"
    # Tabelle der Bundesliga fehlt, der Verein ist dort nicht zu finden: nicht sicher "nirgends"
    no_table = dataclasses.replace(snapshot, tables=MappingProxyType({"2_bundesliga": snapshot.tables["2_bundesliga"]}))
    assert error_of(snapshot=no_table, verein="Dortmund").kind is ErrorKind.UNREACHABLE


def _with_third_league_table(snapshot):
    """Eine 3.-Liga-Tabelle (aufgezeichnet gibt es keine): die der 2. Bundesliga mit anderen Kennungen."""
    rows = tuple(
        dataclasses.replace(r, team=dataclasses.replace(r.team, id=r.team.id + 100000))
        for r in snapshot.tables["2_bundesliga"]
    )
    return dataclasses.replace(snapshot, tables=MappingProxyType({**snapshot.tables, "3_liga": rows}))


def test_team_in_no_table_of_the_leagues():
    snapshot = _with_third_league_table(make_snapshot(NOW))
    error = error_of(snapshot=snapshot, verein="Real Madrid")  # nur aus der Champions League bekannt
    assert error.kind is ErrorKind.NOTHING_ENTERED and "Real Madrid" in error.text and "Europapokal" in error.text
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"3_liga"})
    assert error_of(snapshot=waiting, verein="Real Madrid").kind is ErrorKind.UNREACHABLE


def test_unknown_and_ambiguous_team():
    error = error_of(verein="Sechzig")
    assert error.kind is ErrorKind.UNKNOWN_TEAM and "Sechzig" in error.text
    result = ask(verein="Borussia")
    assert "rueckfrage" in result and result["kandidaten"] == ["Borussia Dortmund", "Borussia Mönchengladbach"]
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"3_liga"})
    assert error_of(snapshot=waiting, verein="Sechzig").kind is ErrorKind.UNREACHABLE


def test_main_team_not_set_or_not_found_falls_back_to_the_bundesliga():
    result = ask(settings=Settings(COMPETITION_KEYS, None, ()))
    assert result["wettbewerb"] == "Bundesliga" and places(result) == [1, 2, 3, 4, 5]
    unknown = ask(settings=Settings(COMPETITION_KEYS, 999999, ()))
    assert unknown["wettbewerb"] == "Bundesliga"
    assert any("Hauptverein" in hint for hint in unknown["hinweise"])
    snapshot = make_snapshot(NOW)
    waiting = dataclasses.replace(snapshot, loaded=snapshot.loaded - {"3_liga"})
    assert error_of(snapshot=waiting, settings=Settings(COMPETITION_KEYS, 999999, ())).kind is ErrorKind.UNREACHABLE


def test_competition_by_name_and_validation():
    assert ask(wettbewerb="2. Bundesliga", ausschnitt="ende")["wettbewerb"] == "2. Bundesliga"
    assert ask(wettbewerb=" Bundesliga ")["wettbewerb"] == "Bundesliga"
    assert ask(wettbewerb="", verein="  ")["wettbewerb"] == "2. Bundesliga"  # leer gilt als nicht angegeben
    error = error_of(wettbewerb="Kreisliga")
    assert error.kind is ErrorKind.INVALID_INPUT and "bundesliga" in error.text and "dfb_pokal" not in error.text
    few = Settings(("bundesliga", "2_bundesliga"), 79, ())
    error = error_of(settings=few, wettbewerb="3_liga")
    assert error.kind is ErrorKind.INVALID_INPUT and "nicht ausgewählt" in error.text


def test_women_only_when_asked_and_never_by_club_name():
    snapshot = make_snapshot(NOW)
    women = dataclasses.replace(
        snapshot, tables=MappingProxyType({**snapshot.tables, "frauen_bundesliga": snapshot.tables["bundesliga"]})
    )
    result = ask(women, wettbewerb="frauen_bundesliga", ausschnitt="ende")
    assert result["wettbewerb"] == "Frauen-Bundesliga" and places(result) == [14, 15, 16, 17, 18]
    # Die Zonen der Frauen-Bundesliga (Abstieg auf den Plätzen 13 und 14) kommen aus der Saisondatei.
    assert [row.get("zone") for row in result["zeilen"]] == ["Abstiegsplatz", None, None, None, None]
    error = error_of(snapshot=women, wettbewerb="frauen_bundesliga", verein="Bayern")
    assert error.kind is ErrorKind.UNSUPPORTED and "Frauen" in error.text
    assert ask(women, verein="Bayern")["wettbewerb"] == "Bundesliga"  # die Frauen-Tabelle zählt bei der Vorgabe nicht


def test_unequal_number_of_games_is_explained():
    snapshot = make_snapshot(NOW)
    rows = list(snapshot.tables["2_bundesliga"])
    rows[2] = dataclasses.replace(rows[2], matches=10)  # Kaiserslautern hat ein Spiel weniger
    changed = dataclasses.replace(snapshot, tables=MappingProxyType({**snapshot.tables, "2_bundesliga": tuple(rows)}))
    assert UNEQUAL in ask(changed, verein="Club")["hinweise"]
    assert UNEQUAL not in ask(verein="Club")["hinweise"]
    assert UNEQUAL in ask(changed, verein="Hertha")["hinweise"]  # Platz 3 kommt in einem Satz vor
    assert UNEQUAL not in ask(changed, verein="Darmstadt")["hinweise"]  # kein Satz mit Platz 3


def _with_zones(snapshot, *zones):
    competitions = dict(snapshot.season.competitions)
    competitions["2_bundesliga"] = dataclasses.replace(competitions["2_bundesliga"], zones=zones)
    season = dataclasses.replace(snapshot.season, competitions=MappingProxyType(competitions))
    return dataclasses.replace(snapshot, season=season)


def test_zone_at_the_edge_of_the_table_is_named_once():
    snapshot = _with_zones(make_snapshot(NOW), Zone("Meisterplatz", 1, 1))
    assert ask(snapshot, verein="Kaiserslautern")["abstaende"] == [
        "10 Punkte hinter dem Meisterplatz (Platz 1)",
        "3 Punkte hinter Platz 2",
        "1 Punkt vor Platz 4",
    ]
    snapshot = _with_zones(make_snapshot(NOW), Zone("Pokalplatz", 2, 3))
    assert ask(snapshot, verein="Hertha")["abstaende"] == ["Tabellenführer, 7 Punkte vor dem Pokalplatz (Platz 2)"]
    assert ask(snapshot, verein="Magdeburg")["abstaende"] == [  # Platz 4 direkt unter der Zone
        "11 Punkte hinter Platz 1",
        "1 Punkt hinter dem Pokalplatz (Platz 3)",
        "1 Punkt vor Platz 5",
    ]
    assert ask(snapshot, verein="Club")["abstaende"] == [
        "7 Punkte hinter Platz 1, dem ersten Platz ohne Pokalplatz",
        "3 Punkte vor Platz 3",
        "4 Punkte vor Platz 4, dem ersten Platz ohne Pokalplatz",
    ]


def test_table_that_is_not_ordered_by_points_makes_no_false_claim():
    snapshot = make_snapshot(NOW)
    rows = list(snapshot.tables["2_bundesliga"])
    rows[2] = dataclasses.replace(rows[2], points=25)  # Platz 3 hat mehr Punkte als Platz 2
    changed = dataclasses.replace(snapshot, tables=MappingProxyType({**snapshot.tables, "2_bundesliga": tuple(rows)}))
    assert ask(changed, verein="Club")["abstaende"] == ["7 Punkte hinter Platz 1"]


def test_single_row_table_has_only_a_leader():
    snapshot = make_snapshot(NOW)
    one = dataclasses.replace(
        snapshot, tables=MappingProxyType({**snapshot.tables, "2_bundesliga": snapshot.tables["2_bundesliga"][:1]})
    )
    result = ask(one, verein="Hertha")
    assert places(result) == [1] and result["abstaende"] == ["Tabellenführer"]


RUNNING_IN_TABLE = (
    "Die Tabelle rechnet laufende Spiele mit dem aktuellen Stand ein; bis zum Abpfiff kann sie sich noch ändern."
)
SATURDAY_RUNNING = datetime(2026, 11, 21, 13, 40, tzinfo=BERLIN)  # 2. Bundesliga: drei Spiele seit 13:00 Uhr


def test_table_with_a_running_match_says_it_counts_the_current_score():
    assert RUNNING_IN_TABLE in ask(make_snapshot(SATURDAY_RUNNING), wettbewerb="2_bundesliga")["hinweise"]
    # Dieselbe Liga am Samstag früh (noch kein Anstoß): kein Satz.
    before = make_snapshot(datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN))
    assert RUNNING_IN_TABLE not in ask(before, wettbewerb="2_bundesliga")["hinweise"]
    # In der Bundesliga läuft zur selben Zeit kein Spiel (Anstoß dort erst um 15:30 Uhr): kein Satz.
    assert RUNNING_IN_TABLE not in ask(make_snapshot(SATURDAY_RUNNING), wettbewerb="bundesliga")["hinweise"]


def test_table_fetched_before_the_kickoff_of_the_running_match_does_not_include_it():
    # Die Spiele laufen seit 13:00 Uhr; die Tabelle ist von 12:59 Uhr, also noch ohne sie.
    snapshot = make_snapshot(SATURDAY_RUNNING)
    early = dataclasses.replace(
        snapshot, tables_at={**snapshot.tables_at, "2_bundesliga": datetime(2026, 11, 21, 12, 59, tzinfo=BERLIN)}
    )
    assert RUNNING_IN_TABLE not in ask(early, wettbewerb="2_bundesliga")["hinweise"]
    # Eine Minute nach dem Anstoß abgerufen: Sie rechnet die laufenden Spiele ein.
    late = dataclasses.replace(
        snapshot, tables_at={**snapshot.tables_at, "2_bundesliga": datetime(2026, 11, 21, 13, 1, tzinfo=BERLIN)}
    )
    assert RUNNING_IN_TABLE in ask(late, wettbewerb="2_bundesliga")["hinweise"]
    # Ohne Abrufzeit der Tabelle: kein Satz.
    unknown = dataclasses.replace(
        snapshot, tables_at={k: v for k, v in snapshot.tables_at.items() if k != "2_bundesliga"}
    )
    assert RUNNING_IN_TABLE not in ask(unknown, wettbewerb="2_bundesliga")["hinweise"]


def _playing(name):
    return f"{name} spielt gerade; den Spielstand liefert fussball_assist__verein."


def test_club_with_a_running_match_points_to_the_club_tool():
    snapshot = make_snapshot(SATURDAY_RUNNING)  # Nürnberg spielt seit 13:00 Uhr, die Spieldaten sind von 13:40 Uhr
    assert _playing("1. FC Nürnberg") in ask(snapshot, verein="Nürnberg")["hinweise"]
    # Ohne Angabe gilt kein Verein hervorgehoben; auch ohne Hauptverein-Bezug kein Satz.
    assert not [h for h in ask(snapshot, wettbewerb="2_bundesliga")["hinweise"] if "spielt gerade" in h]
    # Die Tabelle bleibt unverändert dabei.
    assert RUNNING_IN_TABLE in ask(snapshot, verein="Nürnberg")["hinweise"]


def test_running_match_note_needs_match_data_from_after_the_kickoff():
    snapshot = fetched_earlier(make_snapshot(SATURDAY_RUNNING), datetime(2026, 11, 21, 12, 59, tzinfo=BERLIN))
    assert _playing("1. FC Nürnberg") not in ask(snapshot, verein="Nürnberg")["hinweise"]
    late = fetched_earlier(make_snapshot(SATURDAY_RUNNING), datetime(2026, 11, 21, 13, 1, tzinfo=BERLIN))
    assert _playing("1. FC Nürnberg") in ask(late, verein="Nürnberg")["hinweise"]
    unknown = make_snapshot(SATURDAY_RUNNING)
    unknown = dataclasses.replace(
        unknown, fetched_at=MappingProxyType({k: v for k, v in unknown.fetched_at.items() if k != "2_bundesliga"})
    )
    assert _playing("1. FC Nürnberg") not in ask(unknown, verein="Nürnberg")["hinweise"]


def test_running_match_note_only_for_the_club_that_plays():
    snapshot = make_snapshot(SATURDAY_RUNNING)  # Hertha spielt nicht, andere Vereine der Liga schon
    assert not [h for h in ask(snapshot, verein="Hertha")["hinweise"] if "spielt gerade" in h]
    # Vor dem Anstoß: auch für Nürnberg kein Satz.
    before = make_snapshot(datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN))
    assert not [h for h in ask(before, verein="Nürnberg")["hinweise"] if "spielt gerade" in h]
