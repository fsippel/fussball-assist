"""Einrichtung zur Laufzeit: Laden im Hintergrund, sichtbare Meldungen, Saisonwechsel."""

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fussball_assist import load_season, settings_from_entry
from custom_components.fussball_assist.api import CLIENT_KEY
from custom_components.fussball_assist.const import (
    API_BASE,
    CONF_COMPETITIONS,
    CONF_FAVORITE_TEAMS,
    CONF_MAIN_TEAM,
    DOMAIN,
    ISSUE_SEASON_MISSING,
    ISSUE_SHORTCUT_UNKNOWN,
)
from custom_components.fussball_assist.results import Settings
from custom_components.fussball_assist.seasons import COMPETITION_KEYS

_VERSION = json.loads(
    (Path(__file__).parent.parent / "custom_components" / "fussball_assist" / "manifest.json").read_text()
)["version"]
from custom_components.fussball_assist.store import DataStore

from .conftest import load_fixture, register_openliga, season_copy, setup_entry

_REAL_LOAD_SEASON = load_season


def _issue(issue_registry, issue_id):
    return issue_registry.async_get_issue(DOMAIN, issue_id)


def _without_uel(leagues):
    return [entry for entry in leagues if entry["leagueShortcut"] != "uel2026"]


def _with_season_2027(monkeypatch):
    """Es gibt eine Saisondatei für 2027 (Kopie von 2026) und nur dafür."""
    monkeypatch.setattr(
        "custom_components.fussball_assist.load_season",
        lambda year: season_copy(2027) if year == 2027 else _REAL_LOAD_SEASON(year),
    )


def _leagues_for_2027():
    return [dict(entry, leagueSeason="2027") for entry in load_fixture("leagues")]


async def test_setup_warms_up_in_the_background(hass, config_entry):
    assert config_entry.runtime_data.store is not None and config_entry.state is ConfigEntryState.LOADED


async def test_warm_up_fetches_the_season_lists_with_the_user_agent(hass, config_entry, aioclient_mock):
    urls = {str(call[1]) for call in aioclient_mock.mock_calls}
    assert f"{API_BASE}/getavailableleagues" in urls and f"{API_BASE}/getmatchdata/bl1/2026" in urls
    assert {call[3]["User-Agent"] for call in aioclient_mock.mock_calls} == {
        f"fussball-assist/{_VERSION} (Home Assistant)"
    }
    snapshot = await config_entry.runtime_data.store.async_snapshot(["bundesliga"])
    assert "bundesliga" in snapshot.loaded


async def test_client_is_shared_and_survives_a_reload(hass, config_entry):
    client = config_entry.runtime_data.client
    assert hass.data[CLIENT_KEY] is client
    assert CLIENT_KEY == "fussball_assist_client"  # nicht der Schlüssel der Integration selbst
    before = client.requests_left()
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.runtime_data.client is client and hass.data[CLIENT_KEY] is client
    assert client.requests_left() <= before  # das Fenster beginnt nicht von vorn
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert hass.data[CLIENT_KEY] is client


async def test_runtime_data_holds_settings_and_data_files(hass, config_entry):
    data = config_entry.runtime_data
    assert data.settings == Settings(competitions=COMPETITION_KEYS, main_team=79, favorite_teams=(115,))
    assert data.derbies and data.aliases and data.client is not None


async def test_missing_season_file_creates_an_issue(hass, freezer, issue_registry):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.store is None
    issue = _issue(issue_registry, ISSUE_SEASON_MISSING)
    assert issue is not None and issue.translation_placeholders == {"saison": "2031/32"}
    assert not issue.is_fixable and issue.severity is ir.IssueSeverity.WARNING


async def test_season_file_arriving_clears_the_issue(hass, freezer, issue_registry, monkeypatch):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    entry = await setup_entry(hass)
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is not None
    monkeypatch.setattr(
        "custom_components.fussball_assist.load_season",
        lambda year: season_copy(2031) if year == 2031 else _REAL_LOAD_SEASON(year),
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None and entry.runtime_data.store is not None


async def test_unknown_shortcut_creates_an_issue(hass, aioclient_mock, issue_registry):
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock, leagues=_without_uel(load_fixture("leagues")))
    entry = await setup_entry(hass)
    issue = _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN)
    assert issue is not None and entry.state is ConfigEntryState.LOADED
    assert issue.translation_placeholders == {"kuerzel": "uel2026", "saison": "2026/27"}
    assert not issue.is_fixable and issue.severity is ir.IssueSeverity.WARNING
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None


async def test_known_shortcuts_create_no_issue(hass, config_entry, issue_registry):
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is None
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None


async def test_no_internet_at_start_creates_no_season_issue(hass, aioclient_mock, issue_registry):
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(".*"), exc=aiohttp.ClientError())
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is None


async def test_unknown_shortcut_found_after_a_failed_start_shows_up_without_waiting_for_the_daily_check(
    hass, aioclient_mock, issue_registry, freezer
):
    aioclient_mock.clear_requests()
    aioclient_mock.get(re.compile(".*"), exc=aiohttp.ClientError())
    entry = await setup_entry(hass)
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is None
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock, leagues=_without_uel(load_fixture("leagues")))
    freezer.tick(timedelta(minutes=5))  # Rückzug nach dem Fehler ist vorbei
    await entry.runtime_data.store.async_snapshot(["europa_league"])  # die Ligaliste gelingt erst jetzt
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN).translation_placeholders["kuerzel"] == "uel2026"
    assert entry.state is ConfigEntryState.LOADED


async def test_league_missing_at_first_check_is_found_later_and_the_issue_goes_away(
    hass, aioclient_mock, issue_registry, freezer
):
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock, leagues=_without_uel(load_fixture("leagues")))
    entry = await setup_entry(hass)
    store = entry.runtime_data.store
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is not None
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock)  # OpenLigaDB hat die Liga inzwischen angelegt
    await store.async_snapshot(["europa_league"])
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is not None  # ohne Prüfung bleibt es beim alten Stand
    freezer.move_to("2026-11-18T04:00:00+01:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is not None  # die tägliche Prüfung ruft selbst nichts ab
    await store.async_snapshot(["europa_league"])
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is None
    assert entry.runtime_data.store is store  # kein Neuladen


async def test_daily_check_does_not_recheck_when_all_shortcuts_are_valid(hass, config_entry, freezer, aioclient_mock):
    calls = len(aioclient_mock.mock_calls)
    freezer.move_to("2026-11-18T04:00:00+01:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await config_entry.runtime_data.store.async_snapshot(["europa_league"])
    assert not any(str(call[1]).endswith("/getavailableleagues") for call in aioclient_mock.mock_calls[calls:])


async def test_season_change_without_a_season_file_schedules_a_reload(hass, freezer, issue_registry, monkeypatch):
    freezer.move_to("2031-08-01T12:00:00+02:00")
    entry = await setup_entry(hass)
    assert entry.runtime_data.store is None
    monkeypatch.setattr(
        "custom_components.fussball_assist.load_season",
        lambda year: season_copy(2032) if year == 2032 else _REAL_LOAD_SEASON(year),
    )
    freezer.move_to("2032-07-01T04:00:00+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.store.season.year == 2032
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None


async def test_new_season_with_file_but_no_matches(hass, freezer, aioclient_mock, issue_registry, monkeypatch):
    _with_season_2027(monkeypatch)
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock, leagues=_leagues_for_2027())
    freezer.move_to("2027-07-01T12:00:00+02:00")
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.store is not None
    assert entry.runtime_data.store.season.year == 2027
    assert _issue(issue_registry, ISSUE_SEASON_MISSING) is None
    assert _issue(issue_registry, ISSUE_SHORTCUT_UNKNOWN) is None


async def test_season_change_while_running_reloads(hass, config_entry, freezer, monkeypatch, aioclient_mock):
    assert config_entry.runtime_data.store.season.year == 2026
    _with_season_2027(monkeypatch)
    aioclient_mock.clear_requests()
    register_openliga(aioclient_mock, leagues=_leagues_for_2027())
    freezer.move_to("2027-07-01T04:00:00+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.store.season.year == 2027


async def test_daily_check_does_not_reload_within_the_season(hass, config_entry, freezer):
    store = config_entry.runtime_data.store
    freezer.move_to("2027-06-30T04:00:00+02:00")  # noch Saison 2026/27
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.runtime_data.store is store


async def test_unload_cancels_the_background_task(hass, config_entry):
    assert await hass.config_entries.async_unload(config_entry.entry_id) and config_entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_cancels_a_running_warm_up_and_the_daily_check(hass, monkeypatch, issue_registry, freezer):
    started = asyncio.Event()
    cancelled = []

    async def never_ends(self):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(DataStore, "async_warm_up", never_ends)
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry = MockConfigEntry(domain=DOMAIN, title="Fußball-Assist", data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await started.wait()
    assert entry.state is ConfigEntryState.LOADED  # Einrichtung wartet nicht auf das Laden
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cancelled and entry.state is ConfigEntryState.NOT_LOADED
    # Der Zeitgeber ist abgemeldet: ein Saisonwechsel danach löst nichts mehr aus.
    freezer.move_to("2027-07-01T04:00:00+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_a_crash_in_the_background_task_is_logged_not_raised(hass, monkeypatch, caplog):
    async def boom(self):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(DataStore, "async_warm_up", boom)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert "Laden im Hintergrund" in caplog.text and "kaputt" in caplog.text


def _entry(options):
    return MockConfigEntry(domain=DOMAIN, data={}, options=options)


def test_settings_from_entry_defaults():
    assert settings_from_entry(_entry({})) == Settings(
        competitions=COMPETITION_KEYS, main_team=None, favorite_teams=()
    )


def test_settings_from_entry_converts_ids_from_text():
    settings = settings_from_entry(
        _entry({CONF_COMPETITIONS: ["3_liga", "bundesliga"], CONF_MAIN_TEAM: "79", CONF_FAVORITE_TEAMS: ["115", "16"]})
    )
    assert settings == Settings(competitions=("bundesliga", "3_liga"), main_team=79, favorite_teams=(115, 16))


def test_settings_from_entry_skips_invalid_ids_and_drops_unknown_competitions(caplog):
    settings = settings_from_entry(
        _entry(
            {
                CONF_COMPETITIONS: ["bundesliga", "eishockey", "bundesliga"],
                CONF_MAIN_TEAM: "abc",
                CONF_FAVORITE_TEAMS: ["115", "", "x1", None, "16", "115"],
            }
        )
    )
    assert settings == Settings(competitions=("bundesliga",), main_team=None, favorite_teams=(115, 16))
    assert "abc" in caplog.text and "x1" in caplog.text


def test_settings_from_entry_empty_main_team_means_none():
    assert settings_from_entry(_entry({CONF_MAIN_TEAM: ""})).main_team is None


@pytest.mark.parametrize("value", [[], "bundesliga", 5, ()])
def test_settings_from_entry_empty_or_invalid_competitions_mean_all(value, caplog):
    settings = settings_from_entry(_entry({CONF_COMPETITIONS: value}))
    assert settings.competitions == COMPETITION_KEYS
    assert "Wettbewerbe" in caplog.text
