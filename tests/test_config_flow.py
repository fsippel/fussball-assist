"""Einrichtung und Optionen: Wettbewerbe, Hauptverein, weitere Lieblingsvereine."""

import re

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from custom_components.fussball_assist.api import CLIENT_KEY, OpenLigaClient, RateLimited, async_get_client
from custom_components.fussball_assist.const import (
    API_BASE,
    CONF_COMPETITIONS,
    CONF_FAVORITE_TEAMS,
    CONF_MAIN_TEAM,
    DOMAIN,
)
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

from .conftest import load_fixture, register_openliga, setup_entry

ALL = list(COMPETITION_KEYS)


def _field(result, key):
    """Feld des Formulars (Schlüssel mit Beschreibung) und sein Selektor."""
    for marker, selector in result["data_schema"].schema.items():
        if marker.schema == key:
            return marker, selector
    raise AssertionError(f"Feld {key} fehlt im Formular")


def _suggested(result, key):
    marker, _ = _field(result, key)
    return (marker.description or {}).get("suggested_value")


def _option_values(result, key):
    return [option["value"] for option in _field(result, key)[1].config["options"]]


def _team_calls(aioclient_mock):
    return [str(call[1]) for call in aioclient_mock.mock_calls if "/getavailableteams/" in str(call[1])]


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def _to_clubs(hass, competitions=None):
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMPETITIONS: ALL if competitions is None else competitions}
    )


async def _to_clubs_again(hass, competitions):
    """Ein weiterer Durchlauf (der erste Fluss bleibt offen, ein zweiter ist erlaubt, solange kein Eintrag besteht)."""
    for flow in hass.config_entries.flow.async_progress():
        hass.config_entries.flow.async_abort(flow["flow_id"])
    return await _to_clubs(hass, competitions)


async def _fail_team_lists(aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(".*getavailableteams.*"), exc=aiohttp.ClientError())
    register_openliga(aioclient_mock)


async def test_full_flow(hass, aioclient_mock):
    await hass.config.async_set_time_zone("Europe/Berlin")
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert _suggested(result, CONF_COMPETITIONS) == ALL
    assert _option_values(result, CONF_COMPETITIONS) == ALL

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "vereine"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: ["115"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "Fußball-Assist"
    assert result["data"] == {}
    assert result["options"] == {CONF_COMPETITIONS: ALL, CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: ["115"]}
    await hass.async_block_till_done(wait_background_tasks=True)
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.settings.main_team == 79


async def test_competitions_are_stored_in_the_fixed_order(hass, aioclient_mock):
    result = await _to_clubs(hass, ["3_liga", "bundesliga"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["options"][CONF_COMPETITIONS] == ["bundesliga", "3_liga"]


async def test_club_list_has_only_mens_club_teams(hass, aioclient_mock):
    result = await _to_clubs(hass)
    assert result["step_id"] == "vereine"
    for key in (CONF_MAIN_TEAM, CONF_FAVORITE_TEAMS):
        values = _option_values(result, key)
        assert "79" in values and "115" in values and "65" in values
        assert "6066" not in values  # Frauenteam
        assert "139" not in values and "725" not in values  # Nationalmannschaften
        assert len(values) == len(set(values))
    assert all("/nla/" not in url and "/DFBN/" not in url and "/ffb1/" not in url for url in _team_calls(aioclient_mock))
    assert len(_team_calls(aioclient_mock)) == 7


async def test_club_list_has_every_club_once(hass, aioclient_mock):
    result = await _to_clubs(hass)
    values = _option_values(result, CONF_MAIN_TEAM)
    assert "417" in values and "3078" not in values  # Saarbrücken: 3. Liga vor DFB-Pokal
    assert "398" in values and "5276" not in values  # Würzburg
    labels = [option["label"] for option in _field(result, CONF_MAIN_TEAM)[1].config["options"]]
    assert "1. FC Nürnberg" in labels and len(labels) == len(set(labels))
    assert _field(result, CONF_MAIN_TEAM)[1].config["sort"] is True
    assert _field(result, CONF_FAVORITE_TEAMS)[1].config["multiple"] is True


async def test_only_selected_competitions_are_fetched(hass, aioclient_mock):
    result = await _to_clubs(hass, ["bundesliga", "nationalmannschaft", "frauen_bundesliga"])
    assert _team_calls(aioclient_mock) == [f"{API_BASE}/getavailableteams/bl1/2026"]
    values = _option_values(result, CONF_MAIN_TEAM)
    assert "65" in values and "79" not in values


async def test_only_national_and_women_need_no_club_list(hass, aioclient_mock):
    result = await _to_clubs(hass, ["nationalmannschaft", "frauen_bundesliga"])
    assert result["step_id"] == "vereine" and _team_calls(aioclient_mock) == []
    assert _option_values(result, CONF_MAIN_TEAM) == []


async def test_main_team_and_favourites_may_stay_empty(hass, aioclient_mock):
    result = await _to_clubs(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_COMPETITIONS: ALL, CONF_FAVORITE_TEAMS: []}
    assert CONF_MAIN_TEAM not in result["options"]


@pytest.mark.parametrize("selection", [[], None])
async def test_at_least_one_competition(hass, aioclient_mock, selection):
    result = await _start(hass)
    user_input = {} if selection is None else {CONF_COMPETITIONS: selection}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == {"base": "keine_wettbewerbe"}
    assert _team_calls(aioclient_mock) == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COMPETITIONS: ["bundesliga"]})
    assert result["step_id"] == "vereine"


async def test_source_unreachable(hass, aioclient_mock):
    await _fail_team_lists(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == {"base": "nicht_erreichbar"}
    assert _suggested(result, CONF_COMPETITIONS) == ALL  # die Auswahl bleibt stehen
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "vereine"
    assert not result.get("errors")


async def test_rate_limit_counts_as_unreachable(hass, monkeypatch):
    async def limited(self, shortcut, year):
        raise RateLimited("zu viele Abrufe")

    monkeypatch.setattr(OpenLigaClient, "teams", limited)
    result = await _to_clubs(hass)
    assert result["step_id"] == "user" and result["errors"] == {"base": "nicht_erreichbar"}


async def test_garbled_team_list_counts_as_unreachable(hass, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/getavailableteams/bl1/2026", text="kein json")
    register_openliga(aioclient_mock)
    result = await _to_clubs(hass, ["bundesliga"])
    assert result["errors"] == {"base": "nicht_erreichbar"}


async def test_each_team_call_waits_for_a_free_slot(hass, monkeypatch):
    order = []
    real_wait, real_teams = OpenLigaClient.wait_for_slot, OpenLigaClient.teams

    async def wait(self):
        order.append("warten")
        await real_wait(self)

    async def teams(self, shortcut, year):
        order.append(shortcut)
        return await real_teams(self, shortcut, year)

    monkeypatch.setattr(OpenLigaClient, "wait_for_slot", wait)
    monkeypatch.setattr(OpenLigaClient, "teams", teams)
    await _to_clubs(hass, ["bundesliga", "2_bundesliga"])
    assert order == ["warten", "bl1", "warten", "bl2"]


async def test_foreign_club_is_offered_with_its_european_competition(hass, aioclient_mock):
    result = await _to_clubs(hass, ["champions_league"])
    values = _option_values(result, CONF_MAIN_TEAM)
    assert "438" in values  # Aston Villa
    assert "79" not in values and "65" not in values
    result = await _to_clubs_again(hass, ["bundesliga"])
    assert "438" not in _option_values(result, CONF_MAIN_TEAM)


async def test_unexpected_error_counts_as_unreachable(hass, monkeypatch, caplog):
    async def broken(self, shortcut, year):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "kaputt")

    monkeypatch.setattr(OpenLigaClient, "teams", broken)
    result = await _to_clubs(hass)
    assert result["step_id"] == "user" and result["errors"] == {"base": "nicht_erreichbar"}
    assert "Unerwarteter Fehler beim Laden der Vereinslisten" in caplog.text


async def test_flow_and_entry_share_one_client_and_one_window(hass, config_entry):
    client = await async_get_client(hass)
    assert client is hass.data[CLIENT_KEY] and client is config_entry.runtime_data.client
    before = client.requests_left()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert client.requests_left() == before - 7  # sieben Abrufe der Vereinslisten im selben Fenster


async def test_flow_without_entry_creates_the_client_once(hass):
    assert CLIENT_KEY not in hass.data
    await _to_clubs(hass, ["bundesliga"])
    first = hass.data[CLIENT_KEY]
    assert await async_get_client(hass) is first


async def test_missing_season_aborts(hass, freezer):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "saison_fehlt"


async def test_single_instance(hass, config_entry):
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"


async def test_options_form_is_prefilled(hass, config_entry):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    assert _suggested(result, CONF_COMPETITIONS) == ALL
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["step_id"] == "vereine"
    assert _suggested(result, CONF_MAIN_TEAM) == "79" and _suggested(result, CONF_FAVORITE_TEAMS) == ["115"]


async def test_options_change_reloads(hass, config_entry, aioclient_mock):
    assert config_entry.runtime_data.settings.main_team == 79
    old = config_entry.runtime_data
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAIN_TEAM: "115", CONF_FAVORITE_TEAMS: ["79"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.options == {CONF_COMPETITIONS: ALL, CONF_MAIN_TEAM: "115", CONF_FAVORITE_TEAMS: ["79"]}
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is not old and config_entry.runtime_data.settings.main_team == 115
    assert config_entry.runtime_data.settings.favorite_teams == (79,)


async def test_options_can_clear_the_main_team(hass, config_entry):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.options == {CONF_COMPETITIONS: ALL, CONF_FAVORITE_TEAMS: []}
    assert config_entry.runtime_data.settings.main_team is None


@pytest.mark.parametrize("stored", [[], ["gibt_es_nicht"], "bundesliga", None])
async def test_options_form_suggests_all_when_nothing_valid_is_stored(hass, stored):
    options = {} if stored is None else {CONF_COMPETITIONS: stored}
    entry = await setup_entry(hass, options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init" and _suggested(result, CONF_COMPETITIONS) == ALL


async def test_options_drop_clubs_of_removed_competitions(hass, config_entry):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ["bundesliga"]})
    assert _suggested(result, CONF_MAIN_TEAM) is None and _suggested(result, CONF_FAVORITE_TEAMS) is None


async def test_options_map_a_club_to_its_other_id(hass, config_entry):
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_MAIN_TEAM: "3078"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert _suggested(result, CONF_MAIN_TEAM) == "417"


async def test_options_need_a_competition_and_a_reachable_source(hass, config_entry, aioclient_mock):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: []})
    assert result["step_id"] == "init" and result["errors"] == {"base": "keine_wettbewerbe"}
    await _fail_team_lists(aioclient_mock)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["step_id"] == "init" and result["errors"] == {"base": "nicht_erreichbar"}
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COMPETITIONS: ALL})
    assert result["step_id"] == "vereine"


async def test_options_without_season_file_abort(hass, freezer):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    entry = await setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "saison_fehlt"


def test_translations_cover_every_step_error_and_abort():
    import json
    from pathlib import Path

    folder = Path(__file__).parent.parent / "custom_components" / "fussball_assist" / "translations"
    for name in ("de", "en"):
        texts = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
        assert {"saison_fehlt", "kuerzel_unbekannt"} <= set(texts["issues"])
        for flow, steps in (("config", ("user", "vereine")), ("options", ("init", "vereine"))):
            block = texts[flow]
            assert set(block["step"]) == set(steps)
            for step in steps:
                assert block["step"][step]["title"] and block["step"][step]["description"]
                assert block["step"][step]["data"] and block["step"][step]["data_description"]
            assert {"keine_wettbewerbe", "nicht_erreichbar"} <= set(block["error"])
            assert "saison_fehlt" in block["abort"]
        assert "single_instance_allowed" in texts["config"]["abort"]
        for flow in ("config", "options"):
            for step in texts[flow]["step"].values():
                assert not re.search(r"[.!?] [a-zäöü]", step["description"]), step["description"]
    english = (folder / "en.json").read_text(encoding="utf-8")
    assert "avourite" not in english
    assert load_fixture("leagues")  # Aufzeichnungen sind vorhanden
