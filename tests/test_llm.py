"""Die fünf Werkzeuge über die Assist-API von Home Assistant."""

import asyncio
import dataclasses
import json
import re
from types import MappingProxyType
from unittest.mock import Mock

import aiohttp
import probatio
import pytest
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from custom_components.fussball_assist import llm as llm_platform
from custom_components.fussball_assist import logic_tabelle
from custom_components.fussball_assist.const import (
    CONF_COMPETITIONS,
    CONF_FAVORITE_TEAMS,
    CONF_MAIN_TEAM,
    DOMAIN,
)
from custom_components.fussball_assist.llm import PROMPT, TOOL_NAMES
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import assert_speakable, make_snapshot, setup_entry

_OPTIONS = {CONF_COMPETITIONS: list(COMPETITION_KEYS), CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: ["115"]}
_TOOLS = ("verein", "spiele", "tabelle", "torjaeger", "woche")


async def call(hass, llm_context, tool, args):
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, llm_context)
    return await api.async_call_tool(llm.ToolInput(tool_name=f"fussball_assist__{tool}", tool_args=args))


async def _our_tools(hass, llm_context):
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, llm_context)
    return api, {t.name.removeprefix("fussball_assist__"): t for t in api.tools if t.name.startswith("fussball_assist__")}


async def test_five_tools_and_the_prompt(hass, config_entry, llm_context):
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, llm_context)
    ours = [t for t in api.tools if t.name.startswith("fussball_assist__")]
    assert sorted(t.name for t in ours) == sorted(TOOL_NAMES) and len(TOOL_NAMES) == 5
    assert all(t.integration == DOMAIN and t.title and t.description and t.annotations.read_only for t in ours)
    assert PROMPT in api.api_prompt and "Sag, dass du das nicht weißt" in PROMPT
    # Auch Wissen aus dem Training über Spieler und Vereine nicht als sicher ausgeben.
    assert "bei welchem Verein ein Spieler spielt" in PROMPT and "wenn du es zu wissen glaubst" in PROMPT


async def test_annotations_say_read_only_and_internet(hass, config_entry, llm_context):
    _, tools = await _our_tools(hass, llm_context)
    assert {t.annotations for t in tools.values()} == {
        llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=True)
    }


async def test_nothing_for_other_apis(hass, config_entry, llm_context):
    assert llm_platform.async_get_tools(hass, llm_context, "homeassistant") is None


async def test_nothing_when_the_entry_is_not_loaded(hass, llm_context):
    assert llm_platform.async_get_tools(hass, llm_context, llm.LLM_API_ASSIST) is None


async def test_getting_the_tools_never_fetches(hass, config_entry, llm_context, aioclient_mock):
    before = aioclient_mock.call_count
    assert llm_platform.async_get_tools(hass, llm_context, llm.LLM_API_ASSIST) is not None
    assert aioclient_mock.call_count == before


async def test_getting_the_tools_never_raises(hass, config_entry, llm_context, monkeypatch, caplog):
    monkeypatch.setattr(llm_platform._Choices, "build", Mock(side_effect=RuntimeError("kaputt")))  # noqa: SLF001
    assert llm_platform.async_get_tools(hass, llm_context, llm.LLM_API_ASSIST) is None
    assert "kaputt" in caplog.text


_EXPECTED = {  # Parameter -> JSON-Typ nach der Umwandlung, wie sie die Agenten machen
    "verein": {"verein": "string", "richtung": "string", "anzahl": "integer", "gegner": "string",
               "wettbewerb": "string", "nur_heimspiele": "boolean"},
    "spiele": {"wettbewerb": "string", "zeitraum": "string", "datum": "string"},
    "tabelle": {"wettbewerb": "string", "verein": "string", "ausschnitt": "string"},
    "torjaeger": {"wettbewerb": "string", "spieler": "string", "anzahl": "integer"},
    "woche": {"woche": "string"},
}


async def test_schemas_survive_the_conversion_of_the_agents(hass, config_entry, llm_context):
    api, tools = await _our_tools(hass, llm_context)
    for name, tool in tools.items():
        schema = probatio.to_openapi(tool.parameters, custom_serializer=api.custom_serializer, openapi_version="3.1.0")
        properties = schema["properties"]
        assert {k: v["type"] for k, v in properties.items()} == _EXPECTED[name]
        assert "required" not in schema  # alle Parameter sind freiwillig
        assert all(v["description"] for v in properties.values())
    properties = probatio.to_openapi(tools["verein"].parameters, custom_serializer=api.custom_serializer)["properties"]
    assert properties["richtung"]["enum"] == ["letzte", "naechste", "beides"]
    assert "frauen_bundesliga" not in properties["wettbewerb"]["enum"]
    spiele = probatio.to_openapi(tools["spiele"].parameters, custom_serializer=api.custom_serializer)["properties"]
    assert spiele["wettbewerb"]["enum"][0] == "alle" and "frauen_bundesliga" in spiele["wettbewerb"]["enum"]
    tabelle = probatio.to_openapi(tools["tabelle"].parameters, custom_serializer=api.custom_serializer)["properties"]
    assert "dfb_pokal" not in tabelle["wettbewerb"]["enum"] and "bundesliga" in tabelle["wettbewerb"]["enum"]


@pytest.mark.parametrize(("tool", "param", "words"), [
    ("verein", "richtung", ("letzte", "naechste", "beides", "Vorgabe: beides")),
    ("verein", "anzahl", ("1 bis 5", "Vorgabe: 1")),
    ("verein", "nur_heimspiele", ("Vorgabe: false",)),
    ("verein", "verein", ("Deutschland", "Vorgabe: der Hauptverein")),
    ("spiele", "wettbewerb", ("alle", "bundesliga", "Vorgabe: alle")),
    ("spiele", "zeitraum", ("heute", "kommendes_wochenende", "naechster_spieltag", "Vorgabe: heute")),
    ("spiele", "datum", ("JJJJ-MM-TT",)),
    ("tabelle", "ausschnitt", ("spitze", "ende", "ganz", "Vorgabe: spitze", "Offensive", "Abwehr")),
    ("tabelle", "wettbewerb", ("Liga des Hauptvereins",)),
    ("torjaeger", "anzahl", ("1 bis 10", "Vorgabe: 5")),
    ("woche", "woche", ("diese", "naechste", "Vorgabe: diese")),
])
async def test_descriptions_name_valid_values_and_defaults(hass, config_entry, llm_context, tool, param, words):
    _, tools = await _our_tools(hass, llm_context)
    description = next(
        key.description for key in tools[tool].parameters.schema if str(key) == param
    )
    assert all(word in description for word in words), description


async def test_each_tool_description_has_examples_and_points_to_the_others(hass, config_entry, llm_context):
    _, tools = await _our_tools(hass, llm_context)
    for name, tool in tools.items():
        assert tool.description.count("„") >= 2, name  # zwei Beispielfragen
        others = [f"fussball_assist__{other}" for other in _TOOLS if other != name]
        assert all(other in tool.description for other in others), name


@pytest.mark.parametrize(("tool", "args", "key"), [
    ("verein", {}, "letzte"), ("verein", {"verein": "Glubb", "richtung": "naechste"}, "naechste"),
    ("spiele", {}, "wettbewerbe"), ("spiele", {"wettbewerb": "bundesliga", "zeitraum": "kommendes_wochenende"}, "wettbewerbe"),
    ("tabelle", {}, "zeilen"), ("tabelle", {"verein": "Club"}, "abstaende"),
    ("torjaeger", {"wettbewerb": "bundesliga"}, "torjaeger"), ("woche", {}, "spiele")])
async def test_each_tool_answers(hass, config_entry, llm_context, tool, args, key):
    result = await call(hass, llm_context, tool, args)
    assert result.error is False and key in result.data and "stand" in result.data
    assert_speakable(result.data)


@pytest.mark.parametrize(("tool", "args", "must_name"), [
    ("spiele", {"wettbewerb": "1. Bundesliga"}, "bundesliga"), ("spiele", {"zeitraum": "am Samstag"}, "kommendes_wochenende"),
    ("verein", {"anzahl": "drei"}, "1 bis 5"), ("verein", {"anzahl": 99}, "1 bis 5"), ("verein", {"richtung": "zurück"}, "letzte"),
    ("tabelle", {"ausschnitt": "mitte"}, "spitze"), ("torjaeger", {"anzahl": 0}, "1 bis 10"), ("woche", {"woche": "letzte"}, "diese"),
    ("spiele", {"unbekannt": 1}, "wettbewerb")])
async def test_invalid_input_names_the_valid_values(hass, config_entry, llm_context, tool, args, must_name):
    result = await call(hass, llm_context, tool, args)
    assert result.error is True and result.data["art"] == "eingabe_ungueltig" and must_name in result.data["fehler"]


@pytest.mark.parametrize(("tool", "args", "must_name"), [
    ("verein", {"nur_heimspiele": "vielleicht"}, "ja"), ("verein", {"anzahl": True}, "1 bis 5"),
    ("verein", {"verein": ["Club"]}, "Text"), ("tabelle", {"ausschnitt": 3}, "spitze"),
    ("torjaeger", {"anzahl": "2,5"}, "1 bis 10"), ("woche", {"woche": "diese", "verein": "Club"}, "woche")])
async def test_more_invalid_input(hass, config_entry, llm_context, tool, args, must_name):
    result = await call(hass, llm_context, tool, args)
    assert result.error is True and result.data["art"] == "eingabe_ungueltig" and must_name in result.data["fehler"]
    assert result.data["fehler"].endswith(".")


async def test_invalid_input_does_not_fetch(hass, config_entry, llm_context, aioclient_mock):
    before = aioclient_mock.call_count
    await call(hass, llm_context, "verein", {"richtung": "zurück"})
    assert aioclient_mock.call_count == before


@pytest.mark.parametrize(("args", "count"), [({"anzahl": "3"}, 3), ({"anzahl": " 2 "}, 2), ({"anzahl": 4.0}, 4)])
async def test_numbers_as_text_are_valid(hass, config_entry, llm_context, args, count):
    result = await call(hass, llm_context, "verein", {"richtung": "letzte", **args})
    assert result.error is False and len(result.data["letzte"]) == count


@pytest.mark.parametrize("value", ["ja", "true", "True", True, "nein", "false", False])
async def test_yes_and_no_as_text_are_valid(hass, config_entry, llm_context, value):
    result = await call(hass, llm_context, "verein", {"nur_heimspiele": value, "richtung": "naechste", "anzahl": 5})
    assert result.error is False
    places = {entry["ort"] for entry in result.data["naechste"]}
    assert places == {"heim"} if value in ("ja", "true", "True", True) else "auswärts" in places


async def test_choices_ignore_case(hass, config_entry, llm_context):
    result = await call(hass, llm_context, "verein", {"richtung": "Naechste"})
    assert result.error is False and "naechste" in result.data and "letzte" not in result.data


@pytest.mark.parametrize("args", [{"verein": ""}, {"verein": None}, {"gegner": "", "wettbewerb": None}])
async def test_empty_values_count_as_not_given(hass, config_entry, llm_context, args):
    assert (await call(hass, llm_context, "verein", args)).data["verein"] == "1. FC Nürnberg"


async def test_empty_unknown_fields_are_ignored(hass, config_entry, llm_context):
    assert (await call(hass, llm_context, "woche", {"unbekannt": "  "})).error is False


async def test_a_bug_in_the_logic_becomes_an_error_result(hass, config_entry, llm_context, monkeypatch):
    monkeypatch.setattr(logic_tabelle, "answer", Mock(side_effect=RuntimeError("kaputt")))
    result = await call(hass, llm_context, "tabelle", {})
    assert result.error is True and result.data["art"] == "quelle_nicht_erreichbar" and "kaputt" not in result.data["fehler"]
    assert result.data["fehler"] == "Das kann ich gerade nicht abrufen."


async def test_unknown_team_is_an_error_with_a_sentence(hass, config_entry, llm_context):
    result = await call(hass, llm_context, "verein", {"verein": "Sechzig"})
    assert result.error is True and result.data["art"] == "verein_unbekannt" and result.data["fehler"].endswith(".")


async def test_ambiguous_team_is_a_question_not_an_error(hass, config_entry, llm_context):
    result = await call(hass, llm_context, "verein", {"verein": "Borussia"})
    assert result.error is False and "rueckfrage" in result.data and len(result.data["kandidaten"]) == 2


async def test_missing_season_answers_with_season_error(hass, freezer, llm_context):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    await setup_entry(hass, _OPTIONS)
    for tool in _TOOLS:
        result = await call(hass, llm_context, tool, {})
        assert result.error is True and result.data["art"] == "saison_fehlt", tool
        assert result.data["fehler"].endswith("."), tool


async def test_no_internet_answers_with_a_sentence(hass, aioclient_mock, llm_context):
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(".*"), exc=aiohttp.ClientError())
    await setup_entry(hass, _OPTIONS)
    for tool in _TOOLS:
        result = await call(hass, llm_context, tool, {})
        assert result.error is True and result.data["art"] == "quelle_nicht_erreichbar", tool
        assert result.data["fehler"] == "Das kann ich gerade nicht abrufen.", tool


async def test_parallel_calls_do_not_interfere(hass, config_entry, llm_context):
    results = await asyncio.gather(*(call(hass, llm_context, "verein", {}) for _ in range(6)))
    assert all(r.error is False for r in results)
    assert len({json.dumps(r.data, ensure_ascii=False) for r in results}) == 1


async def test_results_are_small(hass, config_entry, llm_context):
    for tool, args in [("spiele", {"wettbewerb": "alle", "zeitraum": "kommendes_wochenende"}), ("tabelle", {"ausschnitt": "ganz"})]:
        assert len(json.dumps((await call(hass, llm_context, tool, args)).data, ensure_ascii=False)) < 6000


def _spy(monkeypatch, store, replacement=None):
    """Zeichnet die Anforderungen an den Datenbestand auf (und ersetzt auf Wunsch die Momentaufnahme)."""
    calls = []
    original = store.async_snapshot

    async def snapshot(competitions, **kwargs):
        calls.append((tuple(competitions), kwargs))
        if replacement is not None:
            return replacement(**kwargs)
        return await original(competitions, **kwargs)

    monkeypatch.setattr(store, "async_snapshot", snapshot)
    return calls


_LEAGUES_WITH_TABLE = ("bundesliga", "2_bundesliga", "3_liga", "frauen_bundesliga")


@pytest.mark.parametrize(("tool", "args", "wanted"), [
    ("verein", {}, []),
    ("spiele", {}, []),
    ("tabelle", {}, [{"tables": ("2_bundesliga",)}]),  # die Liga des Hauptvereins
    ("tabelle", {"verein": "Bayern"}, [{"tables": ("bundesliga",)}]),
    ("tabelle", {"wettbewerb": "champions_league"}, [{"tables": ("champions_league",)}]),
    ("torjaeger", {"wettbewerb": "bundesliga"}, [{"scorers": ("bundesliga",)}]),  # nur diese eine Liste
    ("torjaeger", {}, [{"scorers": ("2_bundesliga",)}]),
    ("woche", {}, [{"tables": _LEAGUES_WITH_TABLE}]),  # Spitzenspiele: die Tabellen der gewählten Ligen
])
async def test_each_tool_asks_for_what_it_needs(hass, config_entry, llm_context, monkeypatch, tool, args, wanted):
    calls = _spy(monkeypatch, config_entry.runtime_data.store)
    await call(hass, llm_context, tool, args)
    # Zuerst die Spiellisten aller gewählten Wettbewerbe (der Vereinsindex braucht sie), dann nur, was die Antwort braucht.
    assert [competitions for competitions, _ in calls] == [COMPETITION_KEYS] * (1 + len(wanted))
    assert [{k: tuple(v) for k, v in kwargs.items() if v} for _, kwargs in calls] == [{}, *wanted]


async def test_club_without_a_finished_game_gets_the_previous_season(hass, config_entry, llm_context, monkeypatch):
    base = make_snapshot(dt_util.utcnow())
    coming = {k: tuple(m for m in v if m.kickoff > base.now) for k, v in base.matches.items()}

    def replacement(*, previous=False, **_):
        snapshot = dataclasses.replace(base, matches=MappingProxyType(coming))
        if previous:
            snapshot = dataclasses.replace(snapshot, previous_matches=base.matches)
        return snapshot

    calls = _spy(monkeypatch, config_entry.runtime_data.store, replacement)
    result = await call(hass, llm_context, "verein", {"richtung": "letzte"})
    assert [kwargs.get("previous", False) for _, kwargs in calls] == [False, True]
    assert result.error is False and result.data["letzte"]
    assert any("kein beendetes Spiel" in hint for hint in result.data["hinweise"])


async def test_no_second_request_with_an_opponent(hass, config_entry, llm_context, monkeypatch):
    base = make_snapshot(dt_util.utcnow())
    coming = {k: tuple(m for m in v if m.kickoff > base.now) for k, v in base.matches.items()}
    calls = _spy(
        monkeypatch,
        config_entry.runtime_data.store,
        lambda **_: dataclasses.replace(base, matches=MappingProxyType(coming)),
    )
    await call(hass, llm_context, "verein", {"gegner": "Fürth"})
    assert len(calls) == 1


def _partly_loaded(base):
    """Momentaufnahme, in der 2. Bundesliga, 3. Liga und DFB-Pokal noch nicht geladen sind."""
    waiting = {"2_bundesliga", "3_liga", "dfb_pokal"}
    matches = {k: v for k, v in base.matches.items() if k not in waiting}
    return dataclasses.replace(base, matches=MappingProxyType(matches), loaded=base.loaded - waiting)


@pytest.mark.parametrize(("tool", "param"), [("verein", "verein"), ("verein", "gegner"), ("tabelle", "verein")])
async def test_partial_name_waits_while_competitions_are_loading(hass, config_entry, llm_context, monkeypatch, tool, param):
    # "Eintracht" passt auf Frankfurt (Bundesliga), Braunschweig (2. Bundesliga) und Trier (3. Liga); solange
    # nur die Bundesliga geladen ist, sähe es nach Frankfurt aus.
    base = make_snapshot(dt_util.utcnow())
    _spy(monkeypatch, config_entry.runtime_data.store, lambda **_: _partly_loaded(base))
    args = {param: "Eintracht"} if param == "verein" else {"verein": "Bayern", param: "Eintracht"}
    result = await call(hass, llm_context, tool, args)
    assert result.error is True and result.data["art"] == "quelle_nicht_erreichbar"
    assert "noch nicht alle Wettbewerbe geladen" in result.data["fehler"]


@pytest.mark.parametrize("name", ["Eintracht Frankfurt", "Frankfurt", "SGE"])
async def test_exact_name_answers_while_competitions_are_loading(hass, config_entry, llm_context, monkeypatch, name):
    base = make_snapshot(dt_util.utcnow())
    _spy(monkeypatch, config_entry.runtime_data.store, lambda **_: _partly_loaded(base))
    result = await call(hass, llm_context, "verein", {"verein": name})
    assert result.error is False and result.data["verein"] == "Eintracht Frankfurt"
