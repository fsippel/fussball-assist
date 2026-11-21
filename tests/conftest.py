"""Gemeinsame Test-Fixtures."""

import dataclasses
import functools
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fussball_assist import const
from custom_components.fussball_assist.const import (
    API_BASE,
    CONF_COMPETITIONS,
    CONF_FAVORITE_TEAMS,
    CONF_MAIN_TEAM,
    DOMAIN,
)
from custom_components.fussball_assist.models import Match
from custom_components.fussball_assist.parse import parse_matches, parse_scorers, parse_table
from custom_components.fussball_assist.results import Settings
from custom_components.fussball_assist.seasons import COMPETITION_KEYS, load_season
from custom_components.fussball_assist.store import Snapshot, merge_national
from custom_components.fussball_assist.teams import TeamIndex, load_aliases

FIXTURES = Path(__file__).parent / "fixtures"
BERLIN = ZoneInfo("Europe/Berlin")
PRODUCTION_LIMIT = const.MAX_REQUESTS_PER_MINUTE  # gelesen, bevor `openliga_defaults` die Obergrenze anhebt

# Hauptverein 1. FC Nürnberg (79), Lieblingsverein SpVgg Greuther Fürth (115).
SETTINGS_CLUB = Settings(competitions=COMPETITION_KEYS, main_team=79, favorite_teams=(115,))

_SEASON_LISTS = {
    "bundesliga": "bl1_2026",
    "2_bundesliga": "bl2_2026",
    "3_liga": "bl3_2026",
    "dfb_pokal": "dfb_2026",
    "champions_league": "ucl_2026",
    "europa_league": "uel2026_2026",
    "conference_league": "uecl2026_2026",
    "frauen_bundesliga": "ffb1_2026",
}
_TABLES = {"bundesliga": "bl1_table_2026", "2_bundesliga": "bl2_table_2026", "champions_league": "ucl_table_2026"}
_SCORERS = {"bundesliga": "bl1_goalgetters_2026", "conference_league": "uecl2026_goalgetters_2026"}


def load_fixture(name: str) -> Any:
    """Liest eine aufgezeichnete Antwort aus tests/fixtures/<name>.json."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@functools.cache
def _season():
    return load_season(2026)


@functools.cache
def _recorded_matches() -> Mapping[str, tuple[Match, ...]]:
    """Spiele je Wettbewerb aus den Aufzeichnungen, geordnet wie im Datenbestand (Anstoß, Kennung)."""
    parsed = {key: parse_matches(load_fixture(name)) for key, name in _SEASON_LISTS.items()}
    national = _season().competitions["nationalmannschaft"]
    parsed["nationalmannschaft"] = merge_national(
        {shortcut: parse_matches(load_fixture(f"{shortcut}_2026")) for shortcut in national.shortcuts},
        national.national_team_ids,
    )
    return {
        key: tuple(sorted(parsed[key], key=lambda m: (m.kickoff, m.id))) for key in COMPETITION_KEYS
    }


def make_snapshot(now: datetime, **overrides: Any) -> Snapshot:
    """Momentaufnahme aus den Aufzeichnungen (Saison 2026/27, alle neun Wettbewerbe), ohne Abrufe.

    Alle Wettbewerbe sind geladen und zum Zeitpunkt `now` abgerufen (Spieldaten, Tabellen und Torjäger);
    Tabellen und Torjäger gibt es, soweit aufgezeichnet, keine gilt als veraltet. Jedes Feld lässt sich
    über `overrides` ersetzen.
    """
    now = now.astimezone(UTC)
    matches = _recorded_matches()
    snapshot = Snapshot(
        now=now,
        tz=BERLIN,
        season=_season(),
        matches=MappingProxyType(dict(matches)),
        tables=MappingProxyType({key: parse_table(load_fixture(name)) for key, name in _TABLES.items()}),
        scorers=MappingProxyType({key: parse_scorers(load_fixture(name)) for key, name in _SCORERS.items()}),
        previous_matches=MappingProxyType({}),
        loaded=frozenset(COMPETITION_KEYS),
        unreachable=frozenset(),
        fetched_at=MappingProxyType(dict.fromkeys(COMPETITION_KEYS, now)),
        tables_at=MappingProxyType(dict.fromkeys(_TABLES, now)),
        scorers_at=MappingProxyType(dict.fromkeys(_SCORERS, now)),
        stale_tables=frozenset(),
        stale_scorers=frozenset(),
        invalid_shortcuts=frozenset(),
        ended_season=MappingProxyType({}),
    )
    return dataclasses.replace(snapshot, **overrides)


def make_index(snapshot: Snapshot) -> TeamIndex:
    """Vereinsindex aus den Spielen der Momentaufnahme mit den mitgelieferten Spitznamen."""
    return TeamIndex.build(snapshot.matches, snapshot.season, load_aliases())


def replace_match(snapshot: Snapshot, competition: str, match_id: int, **changes: Any) -> Snapshot:
    """Momentaufnahme, in der ein Spiel mit `dataclasses.replace` verändert ist."""
    matches = {
        key: tuple(dataclasses.replace(m, **changes) if m.id == match_id else m for m in value)
        for key, value in snapshot.matches.items()
    }
    return dataclasses.replace(snapshot, matches=MappingProxyType(matches))


def fetched_earlier(snapshot: Snapshot, earlier: datetime, *fresh: str) -> Snapshot:
    """Momentaufnahme, in der die Spieldaten aller Wettbewerbe außer `fresh` schon um `earlier` abgerufen sind."""
    stamps = {key: stamp if key in fresh else earlier.astimezone(UTC) for key, stamp in snapshot.fetched_at.items()}
    return dataclasses.replace(snapshot, fetched_at=MappingProxyType(stamps))


_FORBIDDEN_KEYS = frozenset({"id", "teamId", "team_id", "matchId", "match_id", "url", "icon", "crest"})


def _check_speakable(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, sub in value.items():
            assert key not in _FORBIDDEN_KEYS, f"Kennung oder Adresse als Feld: {path}/{key}"
            _check_speakable(sub, f"{path}/{key}")
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            _check_speakable(sub, f"{path}[{index}]")
    elif isinstance(value, str):
        assert "http" not in value, f"Adresse in {path}: {value}"


def assert_speakable(result: Any) -> None:
    """Ergebnis ist ein Wörterbuch, übersteht json.dumps und enthält weder Kennungen noch Adressen."""
    assert isinstance(result, dict)
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    _check_speakable(result, "")


def season_copy(year):
    """Saison 2026/27 mit anderem Jahr, als Ersatz für eine Saisondatei, die es noch nicht gibt."""
    return dataclasses.replace(load_season(2026), year=year)


_PATHS = (  # Dateiname der Aufzeichnung -> Adresse (siehe tests/fixtures/README.md)
    (re.compile(r"^leagues$"), "/getavailableleagues"),
    (re.compile(r"^(?P<s>\w+?)_table_(?P<y>\d{4})$"), "/getbltable/{s}/{y}"),
    (re.compile(r"^(?P<s>\w+?)_goalgetters_(?P<y>\d{4})$"), "/getgoalgetters/{s}/{y}"),
    (re.compile(r"^(?P<s>\w+?)_teams_(?P<y>\d{4})$"), "/getavailableteams/{s}/{y}"),
    (re.compile(r"^lastchange_(?P<s>\w+?)_(?P<y>\d{4})_(?P<o>\d+)$"), "/getlastchangedate/{s}/{y}/{o}"),
    (re.compile(r"^(?P<s>[A-Za-z0-9]+)_(?P<y>\d{4})_md(?P<o>\d+)$"), "/getmatchdata/{s}/{y}/{o}"),
    (re.compile(r"^(?P<s>[A-Za-z0-9]+)_(?P<y>\d{4})$"), "/getmatchdata/{s}/{y}"),
)


@functools.cache
def _recorded_routes() -> tuple[tuple[str, str], ...]:
    """(Adresse, Antworttext) je aufgezeichneter Datei; einmal gelesen, weil jeder Test sie braucht."""
    routes = []
    for path in sorted(FIXTURES.glob("*.json")):
        for pattern, address in _PATHS:
            if match := pattern.match(path.stem):
                routes.append((API_BASE + address.format_map(match.groupdict()), path.read_text(encoding="utf-8")))
                break
    return tuple(routes)


def register_openliga(aioclient_mock, *, leagues: Any = None) -> None:
    """Richtet den HTTP-Mock nach den Aufzeichnungen ein (Abruf vom 17.11.2026, siehe tests/fixtures/README.md).

    Zuerst jede aufgezeichnete Adresse einzeln, zuletzt ein Auffangmuster mit `[]` (der Mock nimmt den
    ersten passenden Eintrag). `leagues` ersetzt die aufgezeichnete Ligaliste.
    """
    leagues_url = f"{API_BASE}/getavailableleagues"
    if leagues is not None:
        aioclient_mock.get(leagues_url, json=leagues)
    for url, text in _recorded_routes():
        aioclient_mock.get(url, text=text)
    aioclient_mock.get(re.compile(".*"), json=[])


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return


@pytest.fixture(autouse=True)
def openliga_defaults(monkeypatch, freezer, aioclient_mock):
    """Keine Pause beim Laden, großzügige Obergrenze, Uhr auf 17.11.2026 16:40 Uhr Berlin, HTTP nach Aufzeichnung.

    Der Mock nimmt den ersten passenden Eintrag: Wer in einem Test eigene `aioclient_mock`-Einträge
    braucht, ruft vorher `aioclient_mock.clear_requests()` auf (und kann `register_openliga` nutzen).
    """
    monkeypatch.setattr(const, "WARM_UP_PAUSE", 0.0)
    monkeypatch.setattr(const, "MAX_REQUESTS_PER_MINUTE", 100)
    freezer.move_to("2026-11-17T16:40:00+01:00")
    register_openliga(aioclient_mock)


@pytest.fixture
def production_limit(monkeypatch):
    """Hebt die großzügige Obergrenze aus `openliga_defaults` auf: Nach dem Aufruf der gelieferten Funktion mit dem
    eingerichteten Eintrag gilt die echte Obergrenze (10 Abrufe je Minute).

    Die Minute des Clients läuft dann mit der eingefrorenen Uhr der Tests (sonst mit der echten Uhr, die `freezer`
    nicht verstellt). Aufrufen erst nach `setup_entry`: Bei eingefrorener Uhr würde das Laden im Hintergrund sonst
    ewig auf einen freien Abruf warten. Danach die Uhr um mehr als eine Minute weiterstellen.
    """

    def apply(entry: MockConfigEntry) -> None:
        monkeypatch.setattr(const, "MAX_REQUESTS_PER_MINUTE", PRODUCTION_LIMIT)
        monkeypatch.setattr(entry.runtime_data.client, "_now", lambda: dt_util.utcnow().timestamp())

    return apply


async def setup_entry(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Richtet einen Eintrag ein und wartet auch auf das Laden im Hintergrund.

    Ohne `wait_background_tasks=True` wartet Home Assistant nicht auf Hintergrundaufgaben.
    """
    await hass.config.async_set_time_zone("Europe/Berlin")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Fußball-Assist", data={}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


@pytest.fixture
async def config_entry(hass):
    return await setup_entry(
        hass,
        {
            CONF_COMPETITIONS: list(COMPETITION_KEYS),
            CONF_MAIN_TEAM: "79",
            CONF_FAVORITE_TEAMS: ["115"],
        },
    )


@pytest.fixture
def llm_context():
    return llm.LLMContext(
        platform="test",
        context=Context(),
        language="de",
        assistant="conversation",
        device_id=None,
    )
