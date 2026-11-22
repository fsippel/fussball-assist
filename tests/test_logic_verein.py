"""Antwort "Verein": letzte und nächste Spiele eines Vereins aus den Aufzeichnungen."""

import dataclasses
from datetime import datetime, timedelta
from types import MappingProxyType

import pytest

from custom_components.fussball_assist.logic_verein import answer
from custom_components.fussball_assist.parse import parse_matches
from custom_components.fussball_assist.results import LIVE_SCORE_CAVEAT, ErrorKind, Settings, ToolError
from custom_components.fussball_assist.seasons import COMPETITION_KEYS, load_season

from .conftest import (
    BERLIN,
    SETTINGS_CLUB,
    assert_speakable,
    fetched_earlier,
    load_fixture,
    make_index,
    make_snapshot,
    replace_match,
)

NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)


MAIN_TEAM_NOTE = (
    "Ohne Vereinsangabe gilt der Hauptverein 1. FC Nürnberg. Für einen anderen Verein frag mit dessen Namen im Feld verein."
)


def ask(snapshot=None, settings=SETTINGS_CLUB, index_from=None, **kwargs):
    """Fragt mit den Vorgaben des Werkzeugs; jedes Ergebnis muss vorlesbar sein.

    `index_from`: Momentaufnahme, aus der der Vereinsindex gebaut wird (Vorgabe: die gefragte).
    """
    snapshot = snapshot or make_snapshot(NOW)
    params = {
        "verein": None,
        "richtung": "beides",
        "anzahl": 1,
        "gegner": None,
        "wettbewerb": None,
        "nur_heimspiele": False,
    }
    params.update(kwargs)
    result = answer(snapshot, make_index(index_from or snapshot), settings, **params)
    assert_speakable(result)
    return result


def hints(result, text):
    return [h for h in result["hinweise"] if text in h]


def live_snapshot():
    """Bayern gegen Köln läuft, 1:1 (Aufzeichnung live_synthetisch), Donnerstag 21:45 Uhr."""
    now = datetime(2026, 11, 20, 21, 45, tzinfo=BERLIN)
    base = make_snapshot(now)
    live = parse_matches(load_fixture("live_synthetisch"))[0]
    matches = {
        key: tuple(live if m.id == live.id else m for m in value) for key, value in base.matches.items()
    }
    return dataclasses.replace(base, matches=MappingProxyType(matches))


def test_last_and_next_match_of_the_main_team():
    result = ask()
    assert result["verein"] == "1. FC Nürnberg"
    assert result["stand"] == "17. November, 16:40 Uhr"
    (last,), (nxt,) = result["letzte"], result["naechste"]
    assert (last["heim"], last["gast"], last["ergebnis"]) == ("1. FC Nürnberg", "SV Darmstadt 98", "2:0")
    assert last["wann"] == "Samstag, 7. November, 13:00 Uhr"
    assert last["bezug"] == "vor 10 Tagen" and last["zustand"] == "beendet"
    assert (nxt["heim"], nxt["gast"], nxt["gegner"]) == ("1. FC Nürnberg", "Eintracht Braunschweig", "Eintracht Braunschweig")
    assert nxt["wann"] == "Samstag, 21. November, 13:00 Uhr" and nxt["bezug"] == "in 4 Tagen"
    assert nxt["ort"] == "heim" and nxt["uhrzeit_fest"] is True and nxt["zustand"] == "angesetzt"
    assert "ergebnis" not in nxt and "ausgang" not in nxt


def test_stand_follows_the_competitions_of_the_shown_matches():
    # Die Saisonliste des DFB-Pokals und der übrigen Wettbewerbe ist vom Vorabend, die 2. Bundesliga frisch.
    snapshot = fetched_earlier(make_snapshot(NOW), datetime(2026, 11, 16, 20, 47, tzinfo=BERLIN), "2_bundesliga")
    result = ask(snapshot, richtung="letzte")
    assert [e["wettbewerb"] for e in result["letzte"]] == ["2. Bundesliga"]
    assert result["stand"] == "17. November, 16:40 Uhr"
    # Ohne gezeigtes Spiel beruht "kein Spiel" auf allen durchsuchten Wettbewerben: der älteste Abruf.
    empty = ask(snapshot, gegner="Dortmund")
    assert empty["letzte"] == [] and empty["naechste"] == []
    assert empty["stand"] == "16. November, 20:47 Uhr"


def test_nickname_and_count():
    result = ask(verein="Glubb", richtung="letzte", anzahl=5)
    assert result["verein"] == "1. FC Nürnberg" and "naechste" not in result
    opponents = [e["gegner"] for e in result["letzte"]]
    assert opponents == ["SV Darmstadt 98", "FC St. Pauli", "Bayer 04 Leverkusen", "1. FC Kaiserslautern", "Energie Cottbus"]
    assert [e["ergebnis"] for e in result["letzte"]] == ["2:0", "1:3", "5:2", "0:1", "1:1"]
    assert [e["ort"] for e in result["letzte"]] == ["heim", "auswärts", "auswärts", "heim", "auswärts"]
    assert [e["ausgang"] for e in result["letzte"]] == ["2:0-Sieg", "3:1-Sieg", "2:5-Niederlage", "0:1-Niederlage", "1:1-Unentschieden"]
    assert [e["wettbewerb"] for e in result["letzte"]] == ["2. Bundesliga", "2. Bundesliga", "DFB-Pokal", "2. Bundesliga", "2. Bundesliga"]


def test_orphan_duplicates_never_appear():
    snapshot = make_snapshot(NOW)
    season = snapshot.season
    shortcuts = {s for c in season.competitions.values() for s in c.shortcuts}
    assert all(m.shortcut in shortcuts for v in snapshot.matches.values() for m in v)
    result = ask(snapshot, richtung="beides", anzahl=5)
    games = [(e["wann"], e["heim"], e["gast"]) for e in result["letzte"] + result["naechste"]]
    assert len(games) == 10 and len(set(games)) == 10
    assert [e["gast"] for e in result["naechste"]][:2] == ["Eintracht Braunschweig", "1. FC Nürnberg"]
    assert result["naechste"][1]["heim"] == "1. FC Heidenheim 1846"


def test_derby_date_has_no_time():
    result = ask(gegner="Fürth", richtung="naechste")
    (derby,) = result["naechste"]
    assert derby["wann"] == "Sonntag, 31. Januar 2027"  # anderes Jahr: mit Jahr
    assert derby["uhrzeit_fest"] is False
    assert (derby["heim"], derby["gast"], derby["ort"]) == ("1. FC Nürnberg", "SpVgg Greuther Fürth", "heim")
    assert hints(result, "Uhrzeit noch nicht fest")


def test_first_leg_against_opponent():
    result = ask(gegner="Fürth", richtung="letzte", anzahl=5)
    (first_leg,) = result["letzte"]
    assert first_leg["wann"] == "Samstag, 15. August, 13:00 Uhr"
    assert (first_leg["heim"], first_leg["gast"]) == ("SpVgg Greuther Fürth", "1. FC Nürnberg")
    assert first_leg["ergebnis"] == "2:4" and first_leg["ausgang"] == "4:2-Sieg" and first_leg["ort"] == "auswärts"
    assert first_leg["runde"] == "2. Spieltag" and first_leg["bezug"] == "vor 94 Tagen"


def test_cup_result_on_penalties():
    result = ask(wettbewerb="dfb_pokal", richtung="letzte", anzahl=5)
    assert [e["runde"] for e in result["letzte"]] == ["2. Runde", "1. Runde"]
    first_round = result["letzte"][1]
    assert first_round["ergebnis"] == "5:6"
    assert first_round["ergebnis_zusatz"] == "im Elfmeterschießen"
    assert first_round["nach_90_minuten"] == "1:1"
    assert first_round["ausgang"] == "6:5-Sieg" and first_round["gegner"] == "Viktoria Köln"
    second_round = result["letzte"][0]
    assert "ergebnis_zusatz" not in second_round and "nach_90_minuten" not in second_round


def test_cup_lost_and_nothing_scheduled_means_out():
    # Der Club verlor die 2. Runde 2:5 in Leverkusen; das Achtelfinale ist ohne ihn angesetzt.
    result = ask(wettbewerb="dfb_pokal", richtung="naechste")
    assert result["naechste"] == []
    assert result["hinweise"] == [
        "1. FC Nürnberg ist im DFB-Pokal ausgeschieden.",
        MAIN_TEAM_NOTE,
    ]


def test_cup_lost_on_penalties_is_out_too():
    # Hoffenheim verlor die 2. Runde in Kiel im Elfmeterschießen (5:4).
    result = ask(verein="Hoffenheim", wettbewerb="dfb_pokal")
    assert result["letzte"][0]["ausgang"] == "4:5-Niederlage" and result["letzte"][0]["ergebnis_zusatz"] == "im Elfmeterschießen"
    assert hints(result, "TSG Hoffenheim ist im DFB-Pokal ausgeschieden.")


def test_cup_won_and_next_round_not_scheduled():
    # Bayern gewann die 2. Runde; ohne das eingetragene Achtelfinale ist die nächste Runde noch nicht angesetzt.
    full = make_snapshot(NOW)
    matches = {**full.matches, "dfb_pokal": tuple(m for m in full.matches["dfb_pokal"] if m.id != 88752)}
    result = ask(make_snapshot(NOW, matches=MappingProxyType(matches)), verein="Bayern", wettbewerb="dfb_pokal")
    assert result["letzte"][0]["ausgang"] == "6:1-Sieg" and result["naechste"] == []
    assert result["hinweise"] == ["Die nächste Runde ist noch nicht angesetzt."]


def _cup_final_of_bayern(own_goals, other_goals):
    """Bayerns letztes Pokalspiel (2. Runde) als beendetes Finale; das Achtelfinale ist nicht eingetragen."""
    full = make_snapshot(NOW)
    matches = {**full.matches, "dfb_pokal": tuple(m for m in full.matches["dfb_pokal"] if m.id != 88752)}
    snapshot = replace_match(
        make_snapshot(NOW, matches=MappingProxyType(matches)), "dfb_pokal", 87633,
        round_name="Finale",
        final=dataclasses.replace(full.matches["dfb_pokal"][0].final, home=own_goals, away=other_goals),
    )
    return ask(snapshot, verein="Bayern", wettbewerb="dfb_pokal")


def test_cup_final_won_says_competition_is_over():
    result = _cup_final_of_bayern(6, 1)
    assert result["letzte"][0]["ausgang"] == "6:1-Sieg" and result["naechste"] == []
    assert result["hinweise"] == [
        "FC Bayern München hat im DFB-Pokal das Finale gewonnen; der Wettbewerb ist für diese Saison vorbei."
    ]
    assert not hints(result, "nächste Runde")


def test_cup_final_lost_says_competition_is_over():
    result = _cup_final_of_bayern(1, 6)
    assert result["letzte"][0]["ausgang"] == "1:6-Niederlage" and result["naechste"] == []
    assert result["hinweise"] == [
        "FC Bayern München hat im DFB-Pokal das Finale verloren; der Wettbewerb ist für diese Saison vorbei."
    ]


def _europe_without_trabzonspor_after(last_id, **changes):
    """Trabzonspor in der Conference League bis zum Spiel `last_id` (geändert um `changes`), danach nichts."""
    full = make_snapshot(NOW)
    club = make_index(full).resolve("Trabzonspor").team
    last = next(m for m in full.matches["conference_league"] if m.id == last_id)
    matches = {
        **full.matches,
        "conference_league": tuple(
            m for m in full.matches["conference_league"]
            if not (club.id in (m.home.id, m.away.id) and m.kickoff > last.kickoff)
        ),
    }
    snapshot = make_snapshot(NOW, matches=MappingProxyType(matches))
    snapshot = replace_match(snapshot, "conference_league", last_id, **changes)
    return ask(snapshot, index_from=full, verein="Trabzonspor", wettbewerb="conference_league")


def _europe_final_of_trabzonspor(own_goals, other_goals):
    """Trabzonspors Heimspiel gegen Freiburg (3. Spieltag der Ligaphase) als beendetes Finale."""
    final = next(m for m in make_snapshot(NOW).matches["conference_league"] if m.id == 88323).final
    return _europe_without_trabzonspor_after(
        88323, round_name="Finale", final=dataclasses.replace(final, home=own_goals, away=other_goals)
    )


def test_european_final_won_says_competition_is_over():
    result = _europe_final_of_trabzonspor(2, 1)
    assert result["letzte"][0]["ausgang"] == "2:1-Sieg" and result["naechste"] == []
    assert hints(result, "das Finale") == [
        "Trabzonspor hat in der Conference League das Finale gewonnen; der Wettbewerb ist für diese Saison vorbei."
    ]


def test_european_final_lost_says_competition_is_over():
    result = _europe_final_of_trabzonspor(0, 1)
    assert result["letzte"][0]["ausgang"] == "0:1-Niederlage" and result["naechste"] == []
    assert hints(result, "das Finale") == [
        "Trabzonspor hat in der Conference League das Finale verloren; der Wettbewerb ist für diese Saison vorbei."
    ]
    assert not hints(result, "ausgelost") and not hints(result, "ausgeschieden")


def test_european_final_drawn_in_the_data_stays_uncertain():
    result = _europe_final_of_trabzonspor(1, 1)
    assert not hints(result, "das Finale") and hints(result, "vielleicht noch nicht ausgelost")


def test_won_european_match_that_is_no_final_stays_uncertain():
    # Trabzonspor gewann am 2. Spieltag 2:0 gegen Hearts; danach ist nichts eingetragen.
    result = _europe_without_trabzonspor_after(88306)
    assert result["letzte"][0]["ausgang"] == "2:0-Sieg" and result["naechste"] == []
    assert not hints(result, "das Finale") and not hints(result, "nächste Runde ist noch nicht")
    assert hints(result, "vielleicht noch nicht ausgelost")


def test_club_without_any_match_in_the_competition_does_not_play_there():
    result = ask(wettbewerb="champions_league")
    assert result["letzte"] == [] and result["naechste"] == []
    assert result["hinweise"] == [
        "1. FC Nürnberg spielt in dieser Saison nicht in der Champions League.",
        ELSEWHERE,
        MAIN_TEAM_NOTE,
    ]
    result = ask(verein="Darmstadt", wettbewerb="DFB-Pokal", richtung="letzte")
    assert result["letzte"] != []  # Darmstadt spielt im Pokal: kein solcher Satz
    assert not hints(result, "spielt in dieser Saison nicht")


ELSEWHERE = "Spiele von 1. FC Nürnberg gibt es in den Daten in der 2. Bundesliga und im DFB-Pokal."


def test_competition_without_any_match_of_the_club_names_where_it_plays():
    result = ask(verein="Glubb", wettbewerb="bundesliga")
    assert result["letzte"] == [] and result["naechste"] == []
    assert result["hinweise"] == [
        "Für 1. FC Nürnberg ist im Wettbewerb Bundesliga kein Spiel gespielt.",
        "Für 1. FC Nürnberg ist im Wettbewerb Bundesliga kein Spiel angesetzt.",
        ELSEWHERE,
    ]
    # Nur geladene, erreichbare Wettbewerbe zählen.
    snapshot = make_snapshot(NOW, unreachable=frozenset({"dfb_pokal"}))
    assert hints(ask(snapshot, verein="Glubb", wettbewerb="bundesliga"), "Spiele von") == [
        "Spiele von 1. FC Nürnberg gibt es in den Daten in der 2. Bundesliga."
    ]


def test_no_hint_on_other_competitions_when_the_club_plays_in_the_chosen_one():
    result = ask(verein="Bayern", wettbewerb="bundesliga")
    assert result["letzte"] and result["naechste"]
    assert not hints(result, "Spiele von")
    # Auch wenn die gefragte Richtung leer ist: Nürnberg spielt in der 2. Bundesliga, nur nicht gegen Bayern.
    result = ask(gegner="Bayern", wettbewerb="2_bundesliga")
    assert result["letzte"] == [] and result["naechste"] == []
    assert not hints(result, "Spiele von")
    result = ask(verein="Deutschland", wettbewerb="nationalmannschaft", richtung="naechste")
    assert result["naechste"] == [] and not hints(result, "Spiele von")


def _with_cup(key, keep):
    """Momentaufnahme, in der der Wettbewerb `key` nur die Spiele enthält, für die `keep(spiel)` wahr ist."""
    full = make_snapshot(NOW)
    matches = {**full.matches, key: tuple(m for m in full.matches[key] if keep(m))}
    return make_snapshot(NOW, matches=MappingProxyType(matches))


@pytest.mark.parametrize(
    ("key", "name", "elsewhere"),
    [
        ("champions_league", "in der Champions League", ELSEWHERE),
        ("dfb_pokal", "im DFB-Pokal", "Spiele von 1. FC Nürnberg gibt es in den Daten in der 2. Bundesliga."),
    ],
)
def test_empty_cup_list_says_no_match_yet_instead_of_not_taking_part(key, name, elsewhere):
    result = ask(_with_cup(key, lambda m: False), wettbewerb=key)
    assert result["letzte"] == [] and result["naechste"] == []
    assert result["hinweise"] == [
        f"Für 1. FC Nürnberg ist {name} noch kein Spiel gespielt.",
        f"Für 1. FC Nürnberg ist {name} noch kein Spiel angesetzt.",
        elsewhere,
        MAIN_TEAM_NOTE,
    ]


def test_cup_list_with_only_scheduled_matches_says_no_match_yet():
    snapshot = _with_cup("champions_league", lambda m: m.kickoff > make_snapshot(NOW).now)
    assert snapshot.matches["champions_league"]  # angesetzte Spiele, keins beendet, Nürnberg nicht dabei
    result = ask(snapshot, wettbewerb="champions_league", richtung="naechste")
    assert result["hinweise"] == [
        "Für 1. FC Nürnberg ist in der Champions League noch kein Spiel angesetzt.",
        ELSEWHERE,
        MAIN_TEAM_NOTE,
    ]


def test_european_cup_with_a_club_entering_after_the_first_round_says_no_match_yet():
    # Steigt ein Verein erst nach der ersten Runde der Daten ein (wie nach einer Qualifikation), steht nicht fest,
    # dass alle Teilnehmer eingetragen sind: kein sicheres "spielt nicht".
    full = make_snapshot(NOW)
    late = make_index(full).resolve("Bayern").team
    snapshot = _with_cup("champions_league", lambda m: not (m.round_order == 1 and late.id in (m.home.id, m.away.id)))
    result = ask(snapshot, index_from=full, wettbewerb="champions_league", richtung="naechste")
    assert result["hinweise"] == [
        "Für 1. FC Nürnberg ist in der Champions League noch kein Spiel angesetzt.",
        ELSEWHERE,
        MAIN_TEAM_NOTE,
    ]


def test_cup_status_is_not_claimed_while_data_is_missing():
    result = ask(only_loaded({"champions_league"}), index_from=make_snapshot(NOW), wettbewerb="dfb_pokal", richtung="naechste")
    assert not hints(result, "ausgeschieden") and not hints(result, "spielt in dieser Saison nicht")


def test_one_lost_match_in_the_european_league_phase_says_nothing_about_being_out():
    # Trabzonspor verlor am 5. November 0:1 gegen Freiburg (Ligaphase); ohne eingetragene weitere Spiele bleibt es offen.
    full = make_snapshot(NOW)
    club = make_index(full).resolve("Trabzonspor").team
    matches = {
        **full.matches,
        "conference_league": tuple(
            m for m in full.matches["conference_league"]
            if not (club.id in (m.home.id, m.away.id) and m.kickoff > full.now)
        ),
    }
    result = ask(make_snapshot(NOW, matches=MappingProxyType(matches)), verein="Trabzonspor", wettbewerb="conference_league")
    assert result["letzte"][0]["ausgang"] == "0:1-Niederlage" and result["naechste"] == []
    assert not hints(result, "ist in der Conference League ausgeschieden") and not hints(result, "nächste Runde ist noch nicht")
    assert hints(result, "vielleicht noch nicht ausgelost")


def test_home_matches_only():
    result = ask(richtung="beides", anzahl=5, nur_heimspiele=True)
    entries = result["letzte"] + result["naechste"]
    assert len(result["letzte"]) == 5 and len(result["naechste"]) == 5
    assert all(e["ort"] == "heim" and e["heim"] == "1. FC Nürnberg" for e in entries)
    assert result["naechste"][0]["gast"] == "Eintracht Braunschweig"


def test_national_team_is_merged():
    result = ask(verein="Deutschland", richtung="letzte", anzahl=5)
    assert result["verein"] == "Deutschland"
    entries = result["letzte"]
    assert [e["gegner"] for e in entries] == ["Niederlande", "Serbien", "Griechenland", "Serbien", "Griechenland"]
    assert [e["ergebnis"] for e in entries] == ["5:0", "1:3", "0:0", "2:0", "0:1"]
    assert [e["ausgang"] for e in entries] == ["5:0-Sieg", "3:1-Sieg", "0:0-Unentschieden", "2:0-Sieg", "0:1-Niederlage"]
    days = [e["wann"].split(",")[1] for e in entries]
    assert len(set(days)) == 5
    assert all(e["ort"] == "neutral" for e in entries)
    assert all(e["wettbewerb"] == "Nationalmannschaft" for e in entries)


def test_no_next_international_entered():
    result = ask(verein="Deutschland", richtung="naechste")
    assert result["naechste"] == []
    assert hints(result, "noch kein Termin eingetragen")


def test_running_match_shows_score_and_time():
    result = ask(live_snapshot(), verein="Bayern", richtung="letzte", anzahl=2)
    running = result["letzte"][0]
    assert running["zustand"] == "läuft" and running["ergebnis"] == "1:1"
    assert (running["heim"], running["gast"]) == ("FC Bayern München", "1. FC Köln")
    assert running["wann"] == "Freitag, 20. November, 20:30 Uhr"
    assert "ausgang" not in running
    assert running["tore"] == ["23. Minute: M. Olise (FC Bayern München)", "51. Minute: S. El Mala (1. FC Köln)"]
    assert result["stand"] == "20. November, 21:45 Uhr"
    assert running["stand_von"] == "21:45 Uhr"
    assert running["vorbehalt"] == LIVE_SCORE_CAVEAT
    assert not hints(result, "hinterherhängen")
    assert result["letzte"][1]["zustand"] == "beendet"


def test_goal_sentences_name_penalties_and_own_goals():
    base = live_snapshot()
    live = next(m for m in base.matches["bundesliga"] if m.id == 83237)
    first, second = live.goals
    penalty = dataclasses.replace(first, penalty=True)
    # Eigentor: Das Tor zählt für Köln (1:1), der Schütze spielt für Bayern; die Minute fehlt.
    own = dataclasses.replace(second, minute=None, own_goal=True, scorer="J. Kimmich", team_id=65)
    snapshot = dataclasses.replace(
        base,
        matches=MappingProxyType(
            {k: tuple(dataclasses.replace(m, goals=(penalty, own)) if m.id == 83237 else m for m in v) for k, v in base.matches.items()}
        ),
    )
    result = ask(snapshot, verein="Bayern", richtung="letzte")
    assert result["letzte"][0]["tore"] == [
        "23. Minute: M. Olise (FC Bayern München) (Elfmeter)",
        "J. Kimmich (FC Bayern München) (Eigentor)",
    ]


def test_missing_scorers_are_explained():
    result = ask(verein="Freiburg", wettbewerb="conference_league", richtung="letzte", anzahl=3)
    trabzonspor, monaco, jablonec = result["letzte"]
    assert (trabzonspor["heim"], trabzonspor["ergebnis"]) == ("Trabzonspor", "0:1")
    assert "tore" not in trabzonspor and "tore" not in jablonec
    assert monaco["tore"] == [
        "47. Minute: Brunner (AS Monaco)",
        "83. Minute: Camara (AS Monaco)",
        "92. Minute: Ansu Fati (AS Monaco)",
    ]
    assert len(hints(result, "Torschützen nicht eingetragen")) == 1


def test_unknown_team():
    with pytest.raises(ToolError) as err:
        ask(verein="Sechzig")
    assert err.value.kind is ErrorKind.UNKNOWN_TEAM and "Sechzig" in err.value.text


def test_ambiguous_team_asks_back():
    result = ask(verein="Borussia")
    assert "rueckfrage" in result and "stand" not in result
    assert result["kandidaten"] == ["Borussia Dortmund", "Borussia Mönchengladbach"]


def test_ambiguous_team_with_many_candidates_lists_five():
    result = ask(verein="1. FC")
    assert len(result["kandidaten"]) == 5
    assert "genauen Namen" in result["rueckfrage"]


def test_no_main_team_asks_back():
    result = ask(settings=Settings(COMPETITION_KEYS, None, ()))
    assert "rueckfrage" in result and "Hauptverein" in result["rueckfrage"] and result["kandidaten"] == []


def test_foreign_club_is_explained():
    result = ask(verein="AS Monaco", richtung="letzte", anzahl=2)
    assert hints(result, "nur Europapokalspiele bekannt")
    assert [(e["wettbewerb"], e["gegner"], e["ergebnis"]) for e in result["letzte"]] == [
        ("Conference League", "FC Nordsjaelland", "2:0"),
        ("Conference League", "SC Freiburg", "3:0"),
    ]


def test_foreign_hint_waits_until_domestic_competitions_are_loaded():
    snapshot = make_snapshot(NOW, loaded=frozenset({"champions_league", "europa_league", "conference_league"}))
    result = ask(snapshot, verein="AS Monaco")
    assert not hints(result, "nur Europapokalspiele bekannt")
    assert hints(result, "noch nicht alle Wettbewerbe geladen")


def test_foreign_hint_needs_every_domestic_competition_usable():
    # Ohne die inländischen Wettbewerbe sähe Bayern aus wie ein Verein, der nur im Europapokal spielt.
    domestic = frozenset({"bundesliga", "2_bundesliga", "3_liga", "dfb_pokal"})
    unreachable = only_loaded(set(COMPETITION_KEYS) - domestic, unreachable=domestic)
    result = ask(unreachable, verein="Bayern", richtung="naechste")
    assert result["verein"] == "FC Bayern München"
    assert not hints(result, "ausländischer Verein") and hints(result, "nicht erreichbar")
    loading = only_loaded({"champions_league", "europa_league"})
    result = ask(loading, verein="Bayern", richtung="naechste", wettbewerb="champions_league")
    assert result["naechste"] and not hints(result, "ausländischer Verein")
    assert hints(ask(verein="AS Monaco", wettbewerb="conference_league"), "ausländischer Verein")


def test_last_match_from_previous_season():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=BERLIN)
    base = make_snapshot(now)
    year = timedelta(days=365)
    older = tuple(
        dataclasses.replace(m, kickoff=m.kickoff - year, season=2025)
        for m in base.matches["2_bundesliga"]
        if m.finished
    )
    snapshot = make_snapshot(now, previous_matches=MappingProxyType({"2_bundesliga": older}))
    result = ask(snapshot, richtung="letzte", anzahl=2)
    first, second = result["letzte"]
    assert (first["heim"], first["gast"], first["ergebnis"]) == ("1. FC Nürnberg", "SV Darmstadt 98", "2:0")
    assert second["gegner"] == "FC St. Pauli"
    assert hints(result, "Saison 2025/26")
    assert ask(snapshot, richtung="naechste")["naechste"][0]["gegner"] == "Dynamo Dresden"


def test_unreachable_source():
    snapshot = make_snapshot(NOW, loaded=frozenset(), unreachable=frozenset(COMPETITION_KEYS))
    with pytest.raises(ToolError) as err:
        ask(snapshot)
    assert err.value.kind is ErrorKind.UNREACHABLE


def test_partly_loaded_is_explained():
    snapshot = make_snapshot(NOW, loaded=frozenset({"2_bundesliga", "dfb_pokal"}))
    result = ask(snapshot)
    assert result["letzte"] and hints(result, "noch nicht alle Wettbewerbe geladen")


def test_unclear_result_is_not_announced():
    snapshot = replace_match(make_snapshot(NOW), "2_bundesliga", _darmstadt_id(), result_unclear=True)
    (last,) = ask(snapshot, richtung="letzte")["letzte"]
    assert not {"ergebnis", "ergebnis_zusatz", "nach_90_minuten", "ausgang"} & set(last)
    assert hints(ask(snapshot, richtung="letzte"), "nicht eindeutig")


def _darmstadt_id():
    snapshot = make_snapshot(NOW)
    return next(
        m.id for m in snapshot.matches["2_bundesliga"] if m.round_order == 11 and m.home.id == 79
    )


def test_countdown_on_matchday():
    now = datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN)
    (nxt,) = ask(make_snapshot(now), richtung="naechste")["naechste"]
    assert nxt["bezug"] == "in 5 Stunden" and nxt["wann"] == "Samstag, 21. November, 13:00 Uhr"


def test_final_is_neutral_ground():
    snapshot = make_snapshot(NOW)
    wolfsburg = next(
        m.id for m in snapshot.matches["dfb_pokal"] if m.round_name == "2. Runde" and m.home.name == "SC Freiburg"
    )
    plain = ask(snapshot, verein="Freiburg", wettbewerb="dfb_pokal", richtung="letzte", anzahl=5)
    assert [e["ort"] for e in plain["letzte"]] == ["heim", "auswärts"]
    snapshot = replace_match(snapshot, "dfb_pokal", wolfsburg, round_name="Endspiel")
    final = ask(snapshot, verein="Freiburg", wettbewerb="dfb_pokal", richtung="letzte", anzahl=5)
    assert [e["ort"] for e in final["letzte"]] == ["neutral", "auswärts"]
    home_only = ask(snapshot, verein="Freiburg", wettbewerb="dfb_pokal", richtung="letzte", anzahl=5, nur_heimspiele=True)
    assert home_only["letzte"] == [] and hints(home_only, "Heimspiel")
    semi = replace_match(snapshot, "dfb_pokal", wolfsburg, round_name="Halbfinale")
    assert ask(semi, verein="Freiburg", wettbewerb="dfb_pokal", richtung="letzte")["letzte"][0]["ort"] == "heim"


def test_one_invalid_competition_does_not_block_the_rest():
    snapshot = make_snapshot(NOW, invalid_shortcuts=frozenset({"uel2026"}))
    result = ask(snapshot)
    assert result["letzte"] and hints(result, "Europa League")


def test_all_competitions_invalid():
    season = load_season(2026)
    every = frozenset(s for c in season.competitions.values() for s in c.shortcuts)
    with pytest.raises(ToolError) as err:
        ask(make_snapshot(NOW, invalid_shortcuts=every))
    assert err.value.kind is ErrorKind.SEASON_MISSING


def test_new_season_has_no_next_match_yet():
    now = datetime(2027, 6, 10, 12, 0, tzinfo=BERLIN)
    base = make_snapshot(now)
    season = base.season
    finished = {k: tuple(m for m in v if m.finished) for k, v in base.matches.items()}
    snapshot = make_snapshot(
        now,
        matches=MappingProxyType(finished),
        ended_season=MappingProxyType(dict.fromkeys(COMPETITION_KEYS, season)),
    )
    result = ask(snapshot, richtung="beides")
    assert result["naechste"] == []
    assert hints(result, "für die neue Saison ist noch nichts eingetragen")
    assert hints(result, "Endstand der Saison 2026/27")
    assert result["letzte"][0]["gegner"] == "SV Darmstadt 98"


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"richtung": "vorne"}, "letzte, naechste, beides"),
        ({"anzahl": 0}, "1 bis 5"),
        ({"anzahl": 6}, "1 bis 5"),
        ({"anzahl": True}, "1 bis 5"),
        ({"wettbewerb": "kreisliga"}, "bundesliga"),
    ],
)
def test_invalid_input_names_valid_values(params, valid):
    with pytest.raises(ToolError) as err:
        ask(**params)
    assert err.value.kind is ErrorKind.INVALID_INPUT and valid in err.value.text


def test_empty_values_count_as_not_given():
    result = ask(verein="", gegner="", wettbewerb="", richtung="")
    assert result["verein"] == "1. FC Nürnberg" and result["letzte"] and result["naechste"]


def test_women_competition_is_not_reachable_by_club_name():
    with pytest.raises(ToolError) as err:
        ask(wettbewerb="frauen_bundesliga")
    assert err.value.kind is ErrorKind.UNSUPPORTED


def only_loaded(keys, **overrides):
    """Momentaufnahme, in der nur die genannten Wettbewerbe Spiele haben und geladen sind (z. B. beim Start)."""
    full = make_snapshot(NOW)
    keys = frozenset(keys)
    return make_snapshot(
        NOW,
        matches=MappingProxyType({k: (v if k in keys else ()) for k, v in full.matches.items()}),
        loaded=keys,
        fetched_at=MappingProxyType({k: v for k, v in full.fetched_at.items() if k in keys}),
        **overrides,
    )


def test_missing_matches_of_unloaded_competitions_are_not_reported_as_no_match():
    snapshot = only_loaded({"dfb_pokal"})
    result = ask(snapshot, index_from=make_snapshot(NOW), richtung="beides", anzahl=2)
    assert result["letzte"] != [] and result["naechste"] == []  # Pokalspiele sind geladen, die Liga nicht
    assert not [h for h in result["hinweise"] if h.startswith("Für 1. FC Nürnberg ist")]
    assert hints(result, "In den bisher geladenen Daten ist für 1. FC Nürnberg kein Spiel angesetzt")
    assert hints(result, "noch nicht alle Wettbewerbe geladen")


def test_unreachable_league_is_not_reported_as_no_match():
    snapshot = only_loaded(set(COMPETITION_KEYS) - {"2_bundesliga"}, unreachable=frozenset({"2_bundesliga"}))
    result = ask(snapshot, index_from=make_snapshot(NOW), richtung="naechste")
    assert result["naechste"] == []
    assert not [h for h in result["hinweise"] if h.startswith("Für 1. FC Nürnberg ist")]
    assert hints(result, "nicht erreichbar") and hints(result, "2. Bundesliga")


def test_unloaded_national_team_data_is_not_reported_as_no_date():
    snapshot = only_loaded(set(COMPETITION_KEYS) - {"nationalmannschaft"})
    result = ask(snapshot, index_from=make_snapshot(NOW), verein="Deutschland", richtung="naechste")
    assert result["naechste"] == []
    assert not hints(result, "noch kein Termin eingetragen")
    assert hints(result, "In den bisher geladenen Daten")


def test_no_previous_season_fallback_while_data_is_missing():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=BERLIN)
    full = make_snapshot(now)
    older = tuple(
        dataclasses.replace(m, kickoff=m.kickoff - timedelta(days=365), season=2025)
        for m in full.matches["2_bundesliga"]
        if m.finished
    )
    keys = frozenset({"dfb_pokal"})
    snapshot = make_snapshot(
        now,
        matches=MappingProxyType({k: (v if k in keys else ()) for k, v in full.matches.items()}),
        loaded=keys,
        previous_matches=MappingProxyType({"2_bundesliga": older}),
    )
    result = ask(snapshot, index_from=full, richtung="letzte")
    assert result["letzte"] == [] and not hints(result, "Saison 2025/26")


def test_main_team_is_not_denied_while_data_is_still_loading():
    snapshot = only_loaded(set())  # nichts geladen, der Index ist leer
    with pytest.raises(ToolError) as err:
        ask(snapshot)
    assert err.value.kind is ErrorKind.UNREACHABLE
    assert "noch nicht alle Wettbewerbe geladen" in err.value.text and "Hauptverein eingestellt" not in err.value.text


def test_main_team_with_unreachable_data_names_the_source_problem():
    # Der Club spielt auch im Pokal; ohne Pokal und 2. Liga ist er in den geladenen Daten nicht zu finden.
    snapshot = only_loaded(
        set(COMPETITION_KEYS) - {"2_bundesliga", "dfb_pokal"}, unreachable=frozenset({"2_bundesliga"})
    )
    with pytest.raises(ToolError) as err:
        ask(snapshot)
    assert err.value.kind is ErrorKind.UNREACHABLE and "2. Bundesliga" in err.value.text


def test_named_team_is_not_unknown_while_its_league_is_not_loaded():
    snapshot = only_loaded(set(COMPETITION_KEYS) - {"2_bundesliga", "dfb_pokal"})
    with pytest.raises(ToolError) as err:
        ask(snapshot, verein="Glubb")
    assert err.value.kind is ErrorKind.UNREACHABLE and "noch nicht alle Wettbewerbe geladen" in err.value.text
    with pytest.raises(ToolError) as err:
        ask(snapshot, verein="Bayern", gegner="Glubb")
    assert err.value.kind is ErrorKind.UNREACHABLE


def test_main_team_outside_the_selected_competitions_is_said_so():
    settings = Settings(("bundesliga",), 79, ())
    snapshot = only_loaded({"bundesliga"})
    with pytest.raises(ToolError) as err:
        ask(snapshot, settings=settings)
    assert err.value.kind is ErrorKind.UNKNOWN_TEAM
    assert "Hauptverein" in err.value.text and "ausgewählten Wettbewerbe" in err.value.text
    with pytest.raises(ToolError) as err:
        ask(snapshot, settings=settings, verein="Glubb")
    assert err.value.kind is ErrorKind.UNKNOWN_TEAM and "habe ich nicht" in err.value.text


def test_invalid_competition_text_does_not_offer_the_womens_league():
    with pytest.raises(ToolError) as err:
        ask(wettbewerb="kreisliga")
    assert "frauen_bundesliga" not in err.value.text and "bundesliga" in err.value.text
    with pytest.raises(ToolError) as err:
        ask(wettbewerb="frauen_bundesliga")
    assert err.value.kind is ErrorKind.UNSUPPORTED


def test_narrowed_competition_does_not_make_the_club_unknown():
    snapshot = only_loaded({"champions_league"})  # 2. Liga und Pokal sind nur noch nicht geladen
    with pytest.raises(ToolError) as err:
        ask(snapshot, wettbewerb="champions_league")
    assert err.value.kind is ErrorKind.UNREACHABLE and "noch nicht alle Wettbewerbe geladen" in err.value.text
    with pytest.raises(ToolError) as err:
        ask(snapshot, verein="Glubb", wettbewerb="champions_league")
    assert err.value.kind is ErrorKind.UNREACHABLE
    with pytest.raises(ToolError) as err:
        ask(snapshot, verein="AS Monaco", gegner="Glubb", wettbewerb="champions_league")
    assert err.value.kind is ErrorKind.UNREACHABLE


def test_loaded_but_invalid_competition_is_not_reported_as_no_match():
    full = make_snapshot(NOW)
    snapshot = make_snapshot(
        NOW,
        invalid_shortcuts=frozenset({"bl2"}),
        matches=MappingProxyType({k: (() if k == "2_bundesliga" else v) for k, v in full.matches.items()}),
    )
    assert "2_bundesliga" in snapshot.loaded  # wie im Bestand: ungültig und trotzdem geladen
    result = ask(snapshot, index_from=full, richtung="naechste", wettbewerb="")
    assert result["naechste"] == []
    assert not [h for h in result["hinweise"] if h.startswith("Für 1. FC Nürnberg ist")]
    assert hints(result, "In den bisher geladenen Daten") and hints(result, "2. Bundesliga")
    with pytest.raises(ToolError) as err:
        ask(snapshot, index_from=only_loaded(set()), verein="Glubb")
    assert err.value.kind is ErrorKind.SEASON_MISSING


def test_no_match_against_an_opponent_names_the_resolved_club():
    result = ask(gegner="Köln", richtung="letzte")
    assert result["letzte"] == []
    assert result["hinweise"] == ["Gegen 1. FC Köln ist in dieser Saison kein Spiel gespielt.", MAIN_TEAM_NOTE]


def test_main_team_note_only_without_a_club_given():
    assert MAIN_TEAM_NOTE in ask()["hinweise"]
    assert MAIN_TEAM_NOTE in ask(verein="")["hinweise"]
    for name in ("Club", "Nürnberg", "Hertha"):
        assert not [h for h in ask(verein=name)["hinweise"] if "Ohne Vereinsangabe" in h], name


def test_main_team_note_not_for_a_question_or_an_error():
    result = ask(settings=Settings(COMPETITION_KEYS, None, ()))
    assert "hinweise" not in result
