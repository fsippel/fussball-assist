"""Stand der Antworten aus dem Datenbestand: laufende Spiele, Tabellen und Torjägerlisten."""

import copy
import json
import re
from datetime import datetime

import aiohttp
from homeassistant.helpers import llm

from custom_components.fussball_assist import logic_spiele, logic_tabelle, logic_torjaeger, logic_verein
from custom_components.fussball_assist.api import RateLimited
from custom_components.fussball_assist.const import API_BASE, CONF_COMPETITIONS, CONF_FAVORITE_TEAMS, CONF_MAIN_TEAM
from custom_components.fussball_assist.results import LIVE_SCORE_CAVEAT
from custom_components.fussball_assist.seasons import COMPETITION_KEYS
from custom_components.fussball_assist.teams import TeamIndex, load_aliases

from .conftest import BERLIN, SETTINGS_CLUB, assert_speakable, load_fixture, register_openliga, setup_entry
from .test_store import FakeClient, _fixture, _round_answer, make_store

SATURDAY_MORNING = datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN)
CLUB_RUNNING = datetime(2026, 11, 21, 13, 40, tzinfo=BERLIN)  # Nürnberg gegen Braunschweig seit 13:00 Uhr


def _index(snapshot):
    return TeamIndex.build(snapshot.matches, snapshot.season, load_aliases())


def _club(snapshot, **args):
    args = {"verein": None, "richtung": "letzte", "anzahl": 1, "gegner": None, "wettbewerb": None, "nur_heimspiele": False} | args
    return logic_verein.answer(snapshot, _index(snapshot), SETTINGS_CLUB, **args)


async def _warmed_store():
    client = FakeClient()
    store = make_store(client)
    await store.async_warm_up()  # Samstag, 08:00 Uhr
    return client, store


async def test_source_gone_after_the_warm_up_running_match_has_no_score():
    client, store = await _warmed_store()
    client.fail = True
    for moment in (CLUB_RUNNING, CLUB_RUNNING.replace(hour=15, minute=50)):  # während und nach dem Spiel
        client.clock.set(moment)
        snapshot = await store.async_snapshot(COMPETITION_KEYS)
        result = _club(snapshot)
        (game,) = result["letzte"]
        assert game["zustand"] == "läuft" and "ergebnis" not in game and "tore" not in game
        assert "stand_von" not in game and "vorbehalt" not in game
        assert not any("hinterherhängen" in h for h in result["hinweise"])
        assert (
            "Das Spiel 1. FC Nürnberg gegen Eintracht Braunschweig läuft; mein letzter Stand ist von 08:00 Uhr,"
            " deshalb kann ich keinen Spielstand sagen."
        ) in result["hinweise"]
        assert result["stand"] == "21. November, 08:00 Uhr"
        assert_speakable(result)
        day = logic_spiele.answer(snapshot, SETTINGS_CLUB, wettbewerb="2_bundesliga", zeitraum="heute", datum=None)
        running = [s for g in day["wettbewerbe"] for s in g["spiele"] if s["zustand"] == "läuft"]
        assert len(running) == 3 and not any("ergebnis" in s or "vorbehalt" in s for s in running)
        assert any(h.startswith("3 Spiele laufen; mein letzter Stand ist von 08:00 Uhr") for h in day["hinweise"])
        assert not any("hinterherhängen" in h for h in day["hinweise"])


async def test_fresh_round_gives_the_running_score():
    client, store = await _warmed_store()
    goal = {"goalID": 900001, "scoreTeam1": 1, "scoreTeam2": 0, "matchMinute": 23, "goalGetterID": 1,
            "goalGetterName": "R. Lubach", "scoringTeamId": 79, "isPenalty": False, "isOwnGoal": False,
            "isOvertime": False, "comment": None}
    client.rounds[("bl2", 2026, 12)] = [
        dict(raw, goals=[goal]) if raw["matchID"] == 83699 else raw for raw in _round_answer("bl2_2026", 12)
    ]
    client.clock.set(CLUB_RUNNING)
    result = _club(await store.async_snapshot(COMPETITION_KEYS))
    (game,) = result["letzte"]
    assert game["zustand"] == "läuft" and game["ergebnis"] == "1:0"
    assert game["tore"] == ["23. Minute: R. Lubach (1. FC Nürnberg)"]
    assert game["stand_von"] == "13:40 Uhr"
    assert game["vorbehalt"] == LIVE_SCORE_CAVEAT
    assert not any("hinterherhängen" in h for h in result["hinweise"])
    assert not any("keinen Spielstand" in h for h in result["hinweise"])
    # Nur der Wettbewerb des gezeigten Spiels zählt; die übrigen Wettbewerbe (von 08:00 Uhr) überdecken ihn nicht.
    assert result["stand"] == "21. November, 13:40 Uhr"
    assert _club(await store.async_snapshot(COMPETITION_KEYS), wettbewerb="2_bundesliga")["stand"] == "21. November, 13:40 Uhr"
    # Elf Minuten später ohne neuen Abruf (Quelle weg): kein Spielstand mehr.
    client.fail = True
    client.clock.advance(minutes=11)
    result = _club(await store.async_snapshot(COMPETITION_KEYS))
    assert "ergebnis" not in result["letzte"][0]
    assert any("mein letzter Stand ist von 13:40 Uhr" in h for h in result["hinweise"])


async def test_old_table_keeps_its_own_stand_and_says_it_is_outdated():
    client, store = await _warmed_store()
    await store.async_snapshot(COMPETITION_KEYS, tables=COMPETITION_KEYS, scorers=COMPETITION_KEYS)
    table_time = client.clock.now
    # Am nächsten Tag ändert sich die Saisonliste; Tabelle und Torjäger lässt die Obergrenze nicht mehr zu.
    client.clock.advance(days=1, minutes=5)
    finished = {"bl1": 83237, "bl2": 83699}
    original = client.season_matches

    async def season_matches(s, y):
        if s in finished:
            changed = [dict(raw, matchIsFinished=True) if raw["matchID"] == finished[s] else raw for raw in _fixture(f"{s}_2026")]
            return await client._answer(("season_matches", s, y), y, lambda: changed)
        return await original(s, y)

    async def refused(*_):
        raise RateLimited("Obergrenze an Abrufen je Minute erreicht")

    client.season_matches = season_matches
    await store.async_snapshot(COMPETITION_KEYS)  # eine andere Frage lädt die Saisonlisten neu
    client.table = client.scorers = refused
    snapshot = await store.async_snapshot(COMPETITION_KEYS, tables=COMPETITION_KEYS, scorers=COMPETITION_KEYS)
    assert snapshot.fetched_at["2_bundesliga"] == client.clock.now
    assert snapshot.tables_at["2_bundesliga"] == table_time
    result = logic_tabelle.answer(snapshot, _index(snapshot), SETTINGS_CLUB, wettbewerb=None, verein="Club", ausschnitt="spitze")
    assert result["stand"] == "21. November, 08:00 Uhr"
    assert any("Tabelle von 2. Bundesliga" in h and "noch nicht" in h for h in result["hinweise"])
    scorers = logic_torjaeger.answer(snapshot, _index(snapshot), SETTINGS_CLUB, wettbewerb="bundesliga", spieler=None, anzahl=3)
    assert scorers["stand"] == "21. November, 08:00 Uhr"
    assert any("Torjägerliste von Bundesliga" in h for h in scorers["hinweise"])
    assert_speakable(result) and assert_speakable(scorers)


# --- Über die Werkzeuge mit der echten Obergrenze (10 Abrufe je Minute) ---

_OPTIONS = {CONF_COMPETITIONS: list(COMPETITION_KEYS), CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: ["115"]}
_STALE_TABLE = "Seit diesem Stand der Tabelle von {} haben sich die Spieldaten geändert"


async def _call(hass, llm_context, tool, args):
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, llm_context)
    result = await api.async_call_tool(llm.ToolInput(tool_name=f"fussball_assist__{tool}", tool_args=args))
    assert_speakable(result.data)
    return result


async def _saturday_with_production_limit(hass, freezer, production_limit):
    """Eingerichtet und geladen am Samstag, 21.11.2026, 08:00 Uhr; danach gilt die echte Obergrenze."""
    freezer.move_to("2026-11-21T08:00:00+01:00")
    production_limit(await setup_entry(hass, _OPTIONS))
    freezer.move_to("2026-11-21T08:01:01+01:00")  # die Abrufe des Ladens zählen nicht mehr


def _source_answers(aioclient_mock, *, season_lists=None, tables=None):
    """Ab jetzt liefert die Quelle diese Saisonlisten und Tabellen, sonst die Aufzeichnungen (der Mock nimmt den ersten Treffer)."""
    aioclient_mock.clear_requests()
    for shortcut, raw in (season_lists or {}).items():
        aioclient_mock.get(f"{API_BASE}/getmatchdata/{shortcut}/2026", text=json.dumps(raw))
    for shortcut, raw in (tables or {}).items():
        aioclient_mock.get(f"{API_BASE}/getbltable/{shortcut}/2026", text=json.dumps(raw))
    register_openliga(aioclient_mock)


def _finished(fixture, match_id):
    return [dict(raw, matchIsFinished=True) if raw["matchID"] == match_id else raw for raw in load_fixture(fixture)]


def _swapped_table(fixture, first, second):
    rows = copy.deepcopy(load_fixture(fixture))
    rows[first], rows[second] = rows[second], rows[first]
    return rows


async def test_table_question_after_the_limit_is_used_up_names_the_old_stand(
    hass, llm_context, freezer, aioclient_mock, production_limit
):
    # Tag 1: Tabelle der 2. Bundesliga im Zwischenspeicher (Club Zweiter).
    await _saturday_with_production_limit(hass, freezer, production_limit)
    first = await _call(hass, llm_context, "tabelle", {"verein": "Club"})
    assert first.error is False and first.data["stand"] == "21. November, 08:01 Uhr"
    # Tag 2: Die Quelle hat ein neues Ergebnis und den Club auf Platz 3.
    _source_answers(
        aioclient_mock,
        season_lists={"bl2": _finished("bl2_2026", 83699)},
        tables={"bl2": _swapped_table("bl2_table_2026", 1, 2)},
    )
    freezer.move_to("2026-11-22T08:06:01+01:00")
    before = aioclient_mock.call_count
    assert (await _call(hass, llm_context, "verein", {})).error is False  # zehn Saisonlisten: Obergrenze erreicht
    assert aioclient_mock.call_count - before == 10
    freezer.tick(30)
    before = aioclient_mock.call_count
    result = await _call(hass, llm_context, "tabelle", {"verein": "Club"})
    assert aioclient_mock.call_count == before  # nichts mehr frei
    assert result.error is False
    assert result.data["stand"] == "21. November, 08:01 Uhr"  # der Stand der Tabelle, nicht der neuen Spielliste
    assert any(h.startswith(_STALE_TABLE.format("2. Bundesliga")) for h in result.data["hinweise"])


async def test_first_table_question_of_the_day_with_the_limit_used_by_the_season_lists(
    hass, llm_context, freezer, aioclient_mock, production_limit
):
    await _saturday_with_production_limit(hass, freezer, production_limit)
    # Für die Europa League gibt es keine aufgezeichnete Tabelle; die der Champions League steht dafür.
    _source_answers(aioclient_mock, tables={"uel2026": load_fixture("ucl_table_2026")})
    first = await _call(hass, llm_context, "tabelle", {"wettbewerb": "europa_league"})
    assert first.error is False and first.data["stand"] == "21. November, 08:01 Uhr"
    _source_answers(
        aioclient_mock,
        season_lists={"uel2026": [dict(raw, lastUpdateDateTime="2026-11-21T23:00:00") if i == 0 else raw
                                  for i, raw in enumerate(load_fixture("uel2026_2026"))]},
        tables={"uel2026": list(reversed(load_fixture("ucl_table_2026")))},
    )
    freezer.move_to("2026-11-22T08:11:06+01:00")
    before = aioclient_mock.call_count
    result = await _call(hass, llm_context, "tabelle", {"wettbewerb": "europa_league"})
    urls = [str(c[1]) for c in aioclient_mock.mock_calls[before:]]
    assert len(urls) == 10 and all("/getmatchdata/" in u for u in urls)  # die Tabelle ist die elfte: verweigert
    assert result.error is False and result.data["stand"] == "21. November, 08:01 Uhr"
    assert result.data["zeilen"][0]["verein"] == first.data["zeilen"][0]["verein"]
    assert any(h.startswith(_STALE_TABLE.format("Europa League")) for h in result.data["hinweise"])


async def test_running_match_while_the_source_is_gone_has_no_score(
    hass, llm_context, freezer, aioclient_mock, production_limit
):
    await _saturday_with_production_limit(hass, freezer, production_limit)
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(".*"), exc=aiohttp.ClientConnectionError())
    freezer.move_to("2026-11-21T13:40:00+01:00")  # Nürnberg gegen Braunschweig läuft seit 13:00 Uhr
    result = await _call(hass, llm_context, "verein", {"richtung": "letzte"})
    assert result.error is False
    (game,) = result.data["letzte"]
    assert game["zustand"] == "läuft" and "ergebnis" not in game and "tore" not in game
    assert "stand_von" not in game and "vorbehalt" not in game
    assert (
        "Das Spiel 1. FC Nürnberg gegen Eintracht Braunschweig läuft; mein letzter Stand ist von 08:00 Uhr,"
        " deshalb kann ich keinen Spielstand sagen."
    ) in result.data["hinweise"]
    assert not any("hinterherhängen" in h for h in result.data["hinweise"])
    # Die Quelle ist wieder da und liefert die laufende Runde: jetzt mit Spielstand.
    goal = {"goalID": 900001, "scoreTeam1": 1, "scoreTeam2": 0, "matchMinute": 23, "goalGetterID": 1,
            "goalGetterName": "R. Lubach", "scoringTeamId": 79, "isPenalty": False, "isOwnGoal": False,
            "isOvertime": False, "comment": None}
    live_round = [
        dict(raw, goals=[goal]) if raw["matchID"] == 83699 else raw
        for raw in load_fixture("bl2_2026")
        if raw["group"]["groupOrderID"] == 12
    ]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/getmatchdata/bl2/2026/12", text=json.dumps(live_round))
    register_openliga(aioclient_mock)
    freezer.move_to("2026-11-21T13:42:00+01:00")  # nach dem Rückzug
    result = await _call(hass, llm_context, "verein", {"richtung": "letzte"})
    (game,) = result.data["letzte"]
    assert game["ergebnis"] == "1:0" and game["tore"] == ["23. Minute: R. Lubach (1. FC Nürnberg)"]
    assert game["stand_von"] == "13:42 Uhr"
    assert game["vorbehalt"] == LIVE_SCORE_CAVEAT
    assert not any("hinterherhängen" in h for h in result.data["hinweise"])


async def test_season_list_empty_again_and_again_under_the_limit_backs_off_and_other_lists_stay_fresh(
    hass, llm_context, freezer, aioclient_mock, production_limit
):
    # Nur 2. Bundesliga und 3. Liga, geladen am Dienstag; die Saisonliste der 2. Bundesliga kommt ab Mittwoch immer leer.
    freezer.move_to("2026-11-24T08:00:00+01:00")
    options = {CONF_COMPETITIONS: ["2_bundesliga", "3_liga"], CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: []}
    production_limit(await setup_entry(hass, options))
    _source_answers(aioclient_mock, season_lists={"bl2": []})
    freezer.move_to("2026-11-25T10:00:00+01:00")  # von Dienstag bis Donnerstag kein Spiel der beiden Ligen
    bl2_list = f"{API_BASE}/getmatchdata/bl2/2026"

    async def ask():
        before = aioclient_mock.call_count
        result = await _call(hass, llm_context, "verein", {"richtung": "letzte"})
        assert result.error is False
        assert result.data["verein"] == "1. FC Nürnberg" and result.data["letzte"]  # der bisherige Stand bleibt
        return [str(c[1]) for c in aioclient_mock.mock_calls[before:]]

    assert await ask() == [bl2_list, f"{API_BASE}/getmatchdata/bl3/2026", f"{API_BASE}/getavailableleagues"]
    for wait in (60, 120, 240, 480, 900, 900):
        freezer.tick(wait - 1)
        assert await ask() == []
        freezer.tick(1)
        assert await ask() == [bl2_list, f"{API_BASE}/getavailableleagues"]
