"""Tests für die Abrufe bei OpenLigaDB."""

from datetime import UTC, datetime

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.fussball_assist import api, const
from custom_components.fussball_assist.api import OpenLigaClient, OpenLigaError, RateLimited
from custom_components.fussball_assist.const import API_BASE

from .conftest import load_fixture


@pytest.fixture(autouse=True)
def openliga_defaults():
    """Diese Tests richten den HTTP-Mock selbst ein (überschreibt die Fixture aus conftest.py)."""


def _client(hass, **kwargs):
    return OpenLigaClient(async_get_clientsession(hass), "0.1.0", **kwargs)


async def test_round_matches_sends_user_agent(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getmatchdata/bl1/2026/9", json=load_fixture("bl1_2026_md9"))
    assert len(await _client(hass).round_matches("bl1", 2026, 9)) == 9
    assert aioclient_mock.mock_calls[0][3]["User-Agent"] == "fussball-assist/0.1.0 (Home Assistant)"


async def test_all_endpoints_use_documented_paths(hass, aioclient_mock):
    paths = {
        "leagues": ("/getavailableleagues", ()),
        "season_matches": ("/getmatchdata/bl1/2026", ("bl1", 2026)),
        "round_matches": ("/getmatchdata/bl1/2026/9", ("bl1", 2026, 9)),
        "table": ("/getbltable/bl1/2026", ("bl1", 2026)),
        "scorers": ("/getgoalgetters/bl1/2026", ("bl1", 2026)),
        "teams": ("/getavailableteams/bl1/2026", ("bl1", 2026)),
    }
    for path, _ in paths.values():
        aioclient_mock.get(f"{API_BASE}{path}", json=[{"a": 1}])
    for name, (_, args) in paths.items():
        client = _client(hass)
        assert await getattr(client, name)(*args) == [{"a": 1}]
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [f"{API_BASE}{p}" for p, _ in paths.values()]


async def test_path_segments_are_url_encoded(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getbltable/a%2Fb%20c/2026", json=[])
    assert await _client(hass).table("a/b c", 2026) == []


async def test_unknown_shortcut_gives_empty_list(hass, aioclient_mock):
    for path in ("getmatchdata", "getbltable", "getgoalgetters"):
        aioclient_mock.get(f"{API_BASE}/{path}/zzqqxx/2026", json=[])
    client = _client(hass)
    assert await client.season_matches("zzqqxx", 2026) == []
    assert await client.table("zzqqxx", 2026) == []
    assert await client.scorers("zzqqxx", 2026) == []


async def test_last_change_parses_and_tolerates_404(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getlastchangedate/bl1/2026/9", json="2026-11-10T22:41:58.83")
    aioclient_mock.get(f"{API_BASE}/getlastchangedate/zzqqxx/2026/1", status=404)
    client = _client(hass)
    assert await client.last_change("bl1", 2026, 9) == datetime(2026, 11, 10, 21, 41, 58, 830000, tzinfo=UTC)
    assert await client.last_change("zzqqxx", 2026, 1) is None


@pytest.mark.parametrize("answer", [{"text": ""}, {"text": "<html>"}, {"json": None}, {"json": ""}, {"json": "kein Datum"}, {"json": 5}])
async def test_last_change_unparseable_answer_is_none(hass, aioclient_mock, answer):
    aioclient_mock.get(f"{API_BASE}/getlastchangedate/bl1/2026/9", **answer)
    assert await _client(hass).last_change("bl1", 2026, 9) is None


@pytest.mark.parametrize("failure", [{"status": 500}, {"exc": TimeoutError()}, {"exc": aiohttp.ClientError()}])
async def test_last_change_failures_become_open_liga_error(hass, aioclient_mock, failure):
    aioclient_mock.get(f"{API_BASE}/getlastchangedate/bl1/2026/9", **failure)
    with pytest.raises(OpenLigaError):
        await _client(hass).last_change("bl1", 2026, 9)


@pytest.mark.parametrize(
    "failure",
    [{"status": 500}, {"status": 404}, {"exc": TimeoutError()}, {"exc": aiohttp.ClientError()}, {"text": "<html>"}],
)
async def test_failures_become_open_liga_error(hass, aioclient_mock, failure):
    aioclient_mock.get(f"{API_BASE}/getbltable/bl2/2026", **failure)
    with pytest.raises(OpenLigaError):
        await _client(hass).table("bl2", 2026)


async def test_non_list_answer_becomes_open_liga_error(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getmatchdata/bl1/2026", json={"unerwartet": True})
    with pytest.raises(OpenLigaError):
        await _client(hass).season_matches("bl1", 2026)


async def test_eleventh_request_in_a_minute_is_refused(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getbltable/bl2/2026", json=[])
    clock = [0.0]
    client = _client(hass, now=lambda: clock[0])
    for _ in range(10):
        await client.table("bl2", 2026)
    assert client.requests_left() == 0
    with pytest.raises(RateLimited):
        await client.table("bl2", 2026)
    assert aioclient_mock.call_count == 10
    clock[0] = 61.0
    assert client.requests_left() == 10


async def test_failed_requests_count_and_rate_limited_is_an_open_liga_error(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/getbltable/bl2/2026", status=500)
    client = _client(hass, now=lambda: 0.0)
    for _ in range(10):
        with pytest.raises(OpenLigaError):
            await client.table("bl2", 2026)
    assert client.requests_left() == 0
    with pytest.raises(RateLimited) as info:
        await client.table("bl2", 2026)
    assert isinstance(info.value, OpenLigaError)


async def test_limit_is_read_from_const_at_call_time(hass, aioclient_mock, monkeypatch):
    monkeypatch.setattr(const, "MAX_REQUESTS_PER_MINUTE", 2)
    aioclient_mock.get(f"{API_BASE}/getbltable/bl2/2026", json=[])
    client = _client(hass, now=lambda: 0.0)
    await client.table("bl2", 2026)
    await client.table("bl2", 2026)
    assert client.requests_left() == 0
    with pytest.raises(RateLimited):
        await client.table("bl2", 2026)


async def test_wait_for_slot_returns_at_once_when_free(hass, monkeypatch):
    async def fail_sleep(_):
        raise AssertionError("darf nicht schlafen")

    with monkeypatch.context() as scoped:
        scoped.setattr(api.asyncio, "sleep", fail_sleep)
        await _client(hass).wait_for_slot()


async def test_wait_for_slot_sleeps_until_oldest_leaves_window(hass, aioclient_mock, monkeypatch):
    aioclient_mock.get(f"{API_BASE}/getbltable/bl2/2026", json=[])
    clock = [0.0]
    client = _client(hass, now=lambda: clock[0])
    for _ in range(10):
        await client.table("bl2", 2026)
    clock[0] = 20.0
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    with monkeypatch.context() as scoped:
        scoped.setattr(api.asyncio, "sleep", fake_sleep)
        await client.wait_for_slot()
    assert slept and sum(slept) >= 40.0
    assert client.requests_left() == 10
