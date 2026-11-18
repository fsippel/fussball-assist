"""Datenbestand je Wettbewerb mit Mindestabständen und geteilten Abrufen."""

import asyncio
import dataclasses
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from unittest.mock import ANY
from zoneinfo import ZoneInfo

import pytest

from custom_components.fussball_assist import const
from custom_components.fussball_assist.api import OpenLigaError, RateLimited
from custom_components.fussball_assist.parse import parse_local_datetime, parse_matches
from custom_components.fussball_assist.seasons import COMPETITION_KEYS, load_season
from custom_components.fussball_assist.store import DataStore, match_state, merge_national
from custom_components.fussball_assist.timeutil import local_day

from .conftest import FIXTURES, load_fixture, season_copy

BERLIN = ZoneInfo("Europe/Berlin")
_FIXTURE_CACHE: dict[str, object] = {}


def _fixture(name):
    """Aufgezeichnete Antwort oder None, wenn es keine Aufzeichnung gibt."""
    if name not in _FIXTURE_CACHE:
        _FIXTURE_CACHE[name] = load_fixture(name) if (FIXTURES / f"{name}.json").exists() else None
    return _FIXTURE_CACHE[name]


class Clock:
    """Verstellbare Uhr, startet am Samstag, 21.11.2026, 08:00 Uhr Berlin."""

    def __init__(self):
        self.now = datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN).astimezone(UTC)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)

    def set(self, moment):
        self.now = moment.astimezone(UTC)


class FakeClient:
    """Dieselben Methoden wie OpenLigaClient, Antworten nach den aufgezeichneten Fixtures."""

    def __init__(self):
        self.calls = []
        self.fail = False
        self.error = OpenLigaError("Quelle nicht erreichbar")
        self.rate_limited_after = None
        self.empty_years = set()
        self.leagues_answer = load_fixture("leagues")
        self.changes = {}  # (Kürzel, Jahr, Runde) -> Antwort der Änderungsabfrage
        self.rounds = {}  # (Kürzel, Jahr, Runde) -> Antwort für den Spieltag
        self.recorded_as = {}  # Jahr -> Jahr der Aufzeichnung, die dafür geliefert wird
        # Aufrufe ohne Aufzeichnung; sie liefern wie im HTTP-Mock der übrigen Tests [] bzw. None.
        self.missing = []
        self.clock = Clock()
        self.slot_waits = 0

    async def _answer(self, call, year, produce):
        await asyncio.sleep(0)  # wie ein echter Abruf: anderen Aufgaben Vortritt lassen
        if self.rate_limited_after is not None and len(self.calls) >= self.rate_limited_after:
            raise RateLimited("Obergrenze an Abrufen je Minute erreicht")
        self.calls.append(call)
        await asyncio.sleep(0)
        if self.fail:
            raise self.error
        if year in self.empty_years:
            return [] if call[0] != "last_change" else None
        return produce()

    def requests_left(self):
        return 10

    async def wait_for_slot(self):
        self.slot_waits += 1

    async def leagues(self):
        return await self._answer(("leagues",), None, lambda: self.leagues_answer)

    def _recorded(self, call, template, y):
        """Aufzeichnung zu einem Aufruf; ohne Aufzeichnung [] und ein Eintrag in `missing`."""
        source_year = self.recorded_as.get(y, y)
        raw = _fixture(template.format(year=source_year))
        if raw is None:
            self.missing.append(call)
            return []
        if source_year != y and isinstance(raw, list):
            raw = [dict(entry, leagueSeason=y) if "leagueSeason" in entry else entry for entry in raw]
        return raw

    async def season_matches(self, s, y):
        call = ("season_matches", s, y)
        return await self._answer(call, y, lambda: self._recorded(call, f"{s}_{{year}}", y))

    async def round_matches(self, s, y, o):
        call = ("round_matches", s, y, o)
        if (s, y, o) in self.rounds:
            return await self._answer(call, y, lambda: self.rounds[(s, y, o)])
        return await self._answer(call, y, lambda: self._recorded(call, f"{s}_{{year}}_md{o}", y))

    async def table(self, s, y):
        call = ("table", s, y)
        return await self._answer(call, y, lambda: self._recorded(call, f"{s}_table_{{year}}", y))

    async def scorers(self, s, y):
        call = ("scorers", s, y)
        return await self._answer(call, y, lambda: self._recorded(call, f"{s}_goalgetters_{{year}}", y))

    async def teams(self, s, y):
        call = ("teams", s, y)
        return await self._answer(call, y, lambda: self._recorded(call, f"{s}_teams_{{year}}", y))

    async def last_change(self, s, y, o):
        call = ("last_change", s, y, o)

        def produce():
            if (s, y, o) in self.changes:
                raw = self.changes[(s, y, o)]
                return parse_local_datetime(raw) if isinstance(raw, str) else raw
            raw = self._recorded(call, f"lastchange_{s}_{{year}}_{o}", y)
            return parse_local_datetime(raw) if isinstance(raw, str) else None

        return await self._answer(call, y, produce)


def make_store(client, *, season=None, previous=None, competitions=COMPETITION_KEYS, **kwargs):
    return DataStore(
        client,
        season if season is not None else load_season(2026),
        competitions,
        previous=previous,
        utcnow=client.clock,
        tz=BERLIN,
        **kwargs,
    )


def kickoff_of(match_id):
    return next(m.kickoff for m in parse_matches(_fixture("bl1_2026")) if m.id == match_id)


def last_update_text(fixture, order):
    """Neueste lastUpdateDateTime einer Runde, so wie die Quelle sie schreibt (Ortszeit ohne Offset)."""
    entries = [raw for raw in _fixture(fixture) if raw["group"]["groupOrderID"] == order]
    return max((raw["lastUpdateDateTime"] for raw in entries), key=parse_local_datetime)


@pytest.fixture(autouse=True)
def no_warm_up_pause(monkeypatch):
    monkeypatch.setattr(const, "WARM_UP_PAUSE", 0.0)


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def clock(client):
    return client.clock


@pytest.fixture
def store(client):
    return make_store(client)


@pytest.fixture
def cold_store(client):
    return make_store(client)


# --- Mindestabstände, geteilte Abrufe und Fehler der Quelle ---


async def test_repeated_questions_do_not_cause_repeated_requests(store, client, clock):
    await store.async_warm_up()
    before = len(client.calls)
    for _ in range(20):
        await store.async_snapshot(["2_bundesliga"])
    assert len(client.calls) == before


async def test_concurrent_questions_share_one_request(cold_store, client):
    await asyncio.gather(*(cold_store.async_snapshot(["2_bundesliga"]) for _ in range(8)))
    assert client.calls.count(("season_matches", "bl2", 2026)) == 1


async def test_season_list_is_fetched_at_most_once_a_day(store, client, clock):
    await store.async_warm_up()
    clock.advance(hours=23)
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("season_matches", "bl1", 2026)) == 1
    clock.advance(hours=2)
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("season_matches", "bl1", 2026)) == 2


async def test_live_round_is_refreshed_once_a_minute(store, client, clock):
    await store.async_warm_up()
    clock.set(kickoff_of(83237) + timedelta(minutes=10))
    for _ in range(5):
        await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == 1
    clock.advance(seconds=61)
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == 2


async def test_idle_round_needs_a_change_before_it_is_fetched(store, client, clock):
    await store.async_warm_up()
    clock.advance(minutes=16)
    await store.async_snapshot(["bundesliga"])
    assert ("last_change", "bl1", 2026, ANY) in client.calls
    assert not any(c[0] == "round_matches" for c in client.calls)


@pytest.mark.parametrize(("answer", "fetched"), [("same", False), ("newer", True)])
async def test_idle_round_compares_last_change_with_the_round(store, client, clock, answer, fetched):
    # Die Quelle meldet als Änderungszeit die neueste lastUpdateDateTime der Runde
    # (belegt: lastchange_bl1_2026_9 gleich dem Höchstwert in bl1_2026_md9).
    same = last_update_text("bl1_2026", 10)
    client.changes[("bl1", 2026, 10)] = same if answer == "same" else "2026-11-21T08:10:00"
    await store.async_warm_up()
    clock.advance(minutes=16)
    snap = await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("last_change", "bl1", 2026, 10)) == 1
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == (1 if fetched else 0)
    assert client.missing == []  # alle Antworten kamen aus Aufzeichnungen oder gezielten Vorgaben
    assert snap.fetched_at["bundesliga"] == (clock.now if fetched else clock.now - timedelta(minutes=16))


async def test_unreachable_source_serves_cache_with_its_age(store, client, clock):
    await store.async_warm_up()
    client.fail = True
    clock.advance(hours=30)
    snap = await store.async_snapshot(["bundesliga"])
    assert snap.matches["bundesliga"] and "bundesliga" not in snap.unreachable
    assert snap.fetched_at["bundesliga"] == clock.now - timedelta(hours=30)
    clock.advance(hours=20)  # jetzt älter als STALE_LIMIT
    assert "bundesliga" in (await store.async_snapshot(["bundesliga"])).unreachable


async def test_cold_cache_and_no_internet(cold_store, client):
    client.fail = True
    snap = await cold_store.async_snapshot(["bundesliga"])
    assert snap.unreachable == {"bundesliga"} and snap.invalid_shortcuts == frozenset()


async def test_rate_limit_serves_what_is_there(cold_store, client):
    client.rate_limited_after = 3
    snap = await cold_store.async_snapshot(COMPETITION_KEYS)
    assert 0 < len(snap.loaded) < len(COMPETITION_KEYS) and not snap.unreachable


async def test_errors_are_not_retried_for_a_minute(cold_store, client, clock):
    client.fail = True
    for _ in range(3):
        await cold_store.async_snapshot(["bundesliga"])
    assert len(client.calls) == 1
    clock.advance(seconds=61)
    await cold_store.async_snapshot(["bundesliga"])
    assert len(client.calls) == 2


async def test_unknown_shortcut_is_reported(client):
    client.leagues_answer = [l for l in load_fixture("leagues") if l["leagueShortcut"] != "uel2026"]
    store = make_store(client)
    await store.async_warm_up()
    assert (await store.async_snapshot(["europa_league"])).invalid_shortcuts == {"uel2026"}


async def test_empty_new_season_falls_back_to_the_finished_one(client):
    client.empty_years = {2027}  # jede Liste der Saison 2027 ist []
    store = make_store(client, season=season_copy(2027), previous=load_season(2026))
    await store.async_warm_up()
    snap = await store.async_snapshot(["2_bundesliga"], tables=["2_bundesliga"])
    assert snap.ended_season["2_bundesliga"].label == "2026/27" and len(snap.tables["2_bundesliga"]) == 18


def test_merge_national_keeps_each_match_once():
    merged = merge_national(
        {"nla": parse_matches(load_fixture("nla_2026")), "DFBN": parse_matches(load_fixture("DFBN_2026"))},
        {"nla": 725, "DFBN": 139},
    )
    assert len(merged) == 6
    greece_home = next(m for m in merged if local_day(m.kickoff, BERLIN) == date(2026, 9, 27))
    assert greece_home.kickoff == datetime(2026, 9, 27, 18, 45, tzinfo=UTC) and greece_home.shortcut == "nla"


@pytest.mark.parametrize(
    ("offset", "finished", "state"),
    [
        (timedelta(hours=-2), False, "angesetzt"),
        (timedelta(minutes=30), False, "laeuft"),
        (timedelta(hours=4), False, "laut_daten_nicht_beendet"),
        (timedelta(hours=4), True, "beendet"),
    ],
)
def test_match_state(offset, finished, state):
    match = next(m for m in parse_matches(load_fixture("bl1_2026_md10")) if m.id == 83237)
    match = dataclasses.replace(match, finished=finished)
    assert match_state(match, match.kickoff + offset) == state


# --- Weitere Regeln ---


def test_match_state_boundaries():
    match = next(m for m in parse_matches(load_fixture("bl1_2026_md10")) if m.id == 83237)
    assert match_state(match, match.kickoff) == "laeuft"
    assert match_state(match, match.kickoff + const.LIVE_WINDOW) == "laeuft"
    assert match_state(match, match.kickoff + const.LIVE_WINDOW + timedelta(seconds=1)) == "laut_daten_nicht_beendet"


def test_merge_national_ignores_other_nations_and_keeps_order():
    merged = merge_national({"nla": parse_matches(load_fixture("nla_2026"))}, {"nla": 725})
    assert merged and all(725 in (m.home.id, m.away.id) for m in merged)
    assert list(merged) == sorted(merged, key=lambda m: (m.kickoff, m.id))


def test_merge_national_first_shortcut_in_ids_wins():
    merged = merge_national(
        {"nla": parse_matches(load_fixture("nla_2026")), "DFBN": parse_matches(load_fixture("DFBN_2026"))},
        {"DFBN": 139, "nla": 725},
    )
    assert len(merged) == 6 and {m.shortcut for m in merged} == {"DFBN"}


async def test_national_team_snapshot_is_merged(store):
    await store.async_warm_up()
    snap = await store.async_snapshot(["nationalmannschaft"])
    assert len(snap.matches["nationalmannschaft"]) == 6


async def test_warm_up_loads_each_shortcut_once_with_slots_and_pauses(client, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args):
        if delay:
            sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(const, "WARM_UP_PAUSE", 6.0)
    monkeypatch.setattr("custom_components.fussball_assist.store.asyncio.sleep", fake_sleep)
    store = make_store(client)
    await store.async_warm_up()
    shortcuts = [s for c in load_season(2026).competitions.values() for s in c.shortcuts]
    assert client.calls == [("leagues",)] + [("season_matches", s, 2026) for s in shortcuts]
    assert client.slot_waits == len(client.calls)
    assert sleeps == [6.0] * (len(client.calls) - 1)
    snap = await store.async_snapshot(COMPETITION_KEYS)
    assert snap.loaded == frozenset(COMPETITION_KEYS) and len(client.calls) == 11


async def test_warm_up_never_raises(client):
    client.fail = True
    store = make_store(client)
    await store.async_warm_up()
    assert client.calls == [("leagues",)]
    client.error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "kaputt")
    client.clock.advance(minutes=2)
    await store.async_warm_up()


async def test_unexpected_client_exception_is_treated_like_a_source_error(cold_store, client, clock):
    client.fail = True
    client.error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "kaputt")
    snap = await cold_store.async_snapshot(["bundesliga"])
    assert snap.unreachable == {"bundesliga"}
    await cold_store.async_snapshot(["bundesliga"])
    assert len(client.calls) == 1


async def test_backoff_doubles_up_to_the_maximum_and_resets(cold_store, client, clock):
    client.fail = True
    waits = []
    for _ in range(7):
        await cold_store.async_snapshot(["bundesliga"])
        before = len(client.calls)
        elapsed = timedelta()
        while len(client.calls) == before:
            clock.advance(seconds=1)
            elapsed += timedelta(seconds=1)
            await cold_store.async_snapshot(["bundesliga"])
        waits.append(elapsed)
    assert waits == [timedelta(seconds=s) for s in (60, 120, 240, 480, 900, 900, 900)]
    client.fail = False
    clock.advance(minutes=15)
    snap = await cold_store.async_snapshot(["bundesliga"])
    assert "bundesliga" in snap.loaded and not snap.unreachable
    client.fail = True
    clock.advance(days=1, seconds=1)
    await cold_store.async_snapshot(["bundesliga"])
    before = len(client.calls)
    clock.advance(seconds=61)
    await cold_store.async_snapshot(["bundesliga"])
    assert len(client.calls) == before + 1  # nach Erfolg wieder 60 Sekunden


async def test_idle_round_is_fetched_after_a_reported_change(store, client, clock):
    await store.async_warm_up()
    clock.advance(minutes=16)
    client.changes[("bl1", 2026, 10)] = clock.now - timedelta(minutes=1)
    changed = [dict(raw, matchIsFinished=True) if raw["matchID"] == 83237 else raw for raw in _fixture("bl1_2026_md10")]
    client.rounds[("bl1", 2026, 10)] = changed
    snap = await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == 1
    assert next(m for m in snap.matches["bundesliga"] if m.id == 83237).finished
    assert len(snap.matches["bundesliga"]) == 306
    assert snap.fetched_at["bundesliga"] == clock.now
    # gleiche Änderungsmeldung: nichts Neues, kein zweiter Abruf; Änderungsabfrage erst nach 15 Minuten
    clock.advance(minutes=5)
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("last_change", "bl1", 2026, 10)) == 1
    clock.advance(minutes=11)
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("last_change", "bl1", 2026, 10)) == 2
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == 1


async def test_older_change_than_the_data_does_not_fetch_the_round(store, client, clock):
    await store.async_warm_up()
    clock.advance(minutes=16)
    client.changes[("bl1", 2026, 10)] = datetime(2026, 9, 1, tzinfo=UTC)  # älter als lastUpdate der Runde
    await store.async_snapshot(["bundesliga"])
    assert not any(c[0] == "round_matches" for c in client.calls)


async def test_table_only_after_round_change(store, client, clock):
    await store.async_warm_up()
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert client.calls.count(("table", "bl1", 2026)) == 1 and client.calls.count(("scorers", "bl1", 2026)) == 1
    clock.advance(hours=1)
    snap = await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert client.calls.count(("table", "bl1", 2026)) == 1
    assert len(snap.tables["bundesliga"]) == 18 and snap.scorers["bundesliga"]
    # Spieltag ändert sich: Tabelle und Torjäger werden beim nächsten Wunsch neu abgerufen
    client.changes[("bl1", 2026, 10)] = clock.now
    client.rounds[("bl1", 2026, 10)] = [
        dict(raw, matchIsFinished=True) if raw["matchID"] == 83237 else raw for raw in _fixture("bl1_2026_md10")
    ]
    clock.advance(minutes=16)
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert client.calls.count(("round_matches", "bl1", 2026, 10)) == 1
    assert client.calls.count(("table", "bl1", 2026)) == 2 and client.calls.count(("scorers", "bl1", 2026)) == 2
    clock.advance(minutes=1)
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert client.calls.count(("table", "bl1", 2026)) == 2


async def test_table_without_change_at_most_once_a_day(store, client, clock):
    await store.async_warm_up()
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"])
    clock.advance(hours=23)
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"])
    assert client.calls.count(("table", "bl1", 2026)) == 1
    clock.advance(hours=1, seconds=1)  # Saisonliste neu, aber unverändert; Tabelle wegen ihres Alters
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"])
    assert client.calls.count(("season_matches", "bl1", 2026)) == 2
    assert client.calls.count(("table", "bl1", 2026)) == 2


async def test_table_not_fetched_unless_asked_or_without_table(store, client):
    await store.async_warm_up()
    snap = await store.async_snapshot(["bundesliga", "dfb_pokal", "nationalmannschaft"])
    assert not any(c[0] in ("table", "scorers") for c in client.calls) and not snap.tables
    keys = ["dfb_pokal", "nationalmannschaft"]
    await store.async_snapshot(keys, tables=keys, scorers=keys)
    assert [c for c in client.calls if c[0] in ("table", "scorers")] == [("scorers", "dfb", 2026)]


async def test_previous_season_is_loaded_once_and_only_on_request(client):
    client.recorded_as = {2025: 2026}  # Vorsaison: die Aufzeichnung von 2026 mit Jahr 2025
    store = make_store(client, previous=season_copy(2025))
    await store.async_warm_up()
    assert not any(c[2] == 2025 for c in client.calls if len(c) > 2)
    snap = await store.async_snapshot(["bundesliga"])
    assert snap.previous_matches == {}
    for _ in range(3):
        client.clock.advance(days=2)
        snap = await store.async_snapshot(["bundesliga"], previous=True)
    assert client.calls.count(("season_matches", "bl1", 2025)) == 1
    assert ("season_matches", "bl1", 2025) not in client.missing
    older = snap.previous_matches["bundesliga"]
    assert len(older) == 306 and {m.season for m in older} == {2025} and not snap.ended_season
    assert {m.season for m in snap.matches["bundesliga"]} == {2026}


async def test_without_previous_season_nothing_is_loaded(store, client):
    await store.async_warm_up()
    snap = await store.async_snapshot(["bundesliga"], previous=True)
    assert snap.previous_matches == {} and not any(c[2] == 2025 for c in client.calls if len(c) > 2)


async def test_empty_new_season_without_previous_file_stays_empty(client):
    client.empty_years = {2027}
    store = make_store(client, season=season_copy(2027))
    await store.async_warm_up()
    snap = await store.async_snapshot(["2_bundesliga"])
    assert snap.matches["2_bundesliga"] == () and not snap.ended_season and "2_bundesliga" in snap.loaded


async def test_snapshot_is_immutable_and_only_configured_competitions(client):
    store = make_store(client, competitions=["bundesliga"])
    await store.async_warm_up()
    snap = await store.async_snapshot(["bundesliga", "2_bundesliga", "quatsch"])
    assert snap.loaded == {"bundesliga"} and set(snap.matches) == {"bundesliga"}
    assert isinstance(snap.matches, MappingProxyType) and isinstance(snap.matches["bundesliga"], tuple)
    assert snap.now == client.clock.now and snap.tz is BERLIN and snap.season.year == 2026
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.loaded = frozenset()
    with pytest.raises(TypeError):
        snap.matches["x"] = ()
    assert not any(c[1:2] == ("bl2",) for c in client.calls)


async def test_is_live(store, clock):
    await store.async_warm_up()
    assert not store.is_live("bundesliga")
    clock.set(kickoff_of(83237) + timedelta(minutes=10))
    assert store.is_live("bundesliga") and store.is_live("2_bundesliga")
    assert not store.is_live("champions_league") and not store.is_live("quatsch")


async def test_leagues_check_is_done_lazily_when_warm_up_missed_it(cold_store, client, clock):
    client.fail = True
    await cold_store.async_warm_up()
    client.fail = False
    client.leagues_answer = [l for l in load_fixture("leagues") if l["leagueShortcut"] != "bl1"]
    clock.advance(minutes=2)
    snap = await cold_store.async_snapshot(["bundesliga"])
    assert snap.invalid_shortcuts == {"bl1"} and client.calls.count(("leagues",)) == 2
    clock.advance(minutes=2)
    await cold_store.async_snapshot(["bundesliga"])
    assert client.calls.count(("leagues",)) == 2


async def test_invalid_shortcuts_accessor_follows_the_leagues_check(client, clock):
    client.leagues_answer = [l for l in load_fixture("leagues") if l["leagueShortcut"] != "uel2026"]
    store = make_store(client)
    assert store.invalid_shortcuts == frozenset()  # noch nicht geprüft
    await store.async_warm_up()
    assert store.invalid_shortcuts == {"uel2026"}


def _leagues_without(shortcut):
    return [entry for entry in load_fixture("leagues") if entry["leagueShortcut"] != shortcut]


async def test_recheck_leagues_checks_again_with_the_next_snapshot_only(client):
    client.leagues_answer = _leagues_without("bl1")
    store = make_store(client)
    await store.async_warm_up()
    assert store.invalid_shortcuts == {"bl1"}
    client.leagues_answer = load_fixture("leagues")
    store.recheck_leagues()
    assert client.calls.count(("leagues",)) == 1  # kein Abruf ohne Frage
    assert store.invalid_shortcuts == {"bl1"}  # bis zur neuen Prüfung gilt der alte Stand
    assert (await store.async_snapshot(["bundesliga"])).invalid_shortcuts == frozenset()
    assert store.invalid_shortcuts == frozenset() and client.calls.count(("leagues",)) == 2
    await store.async_snapshot(["bundesliga"])
    assert client.calls.count(("leagues",)) == 2


async def test_recheck_leagues_failing_keeps_the_known_invalid_shortcuts(client, clock):
    client.leagues_answer = _leagues_without("bl1")
    store = make_store(client)
    await store.async_warm_up()
    store.recheck_leagues()
    client.fail = True
    snap = await store.async_snapshot(["bundesliga"])
    assert snap.invalid_shortcuts == {"bl1"}
    client.fail = False
    clock.advance(minutes=5)
    client.leagues_answer = load_fixture("leagues")
    assert (await store.async_snapshot(["bundesliga"])).invalid_shortcuts == frozenset()


async def test_callback_gets_the_invalid_shortcuts_after_each_successful_check(client, clock):
    seen = []
    client.leagues_answer = _leagues_without("uel2026")
    store = make_store(client, on_leagues_checked=seen.append)
    await store.async_warm_up()
    assert seen == [frozenset({"uel2026"})]
    store.recheck_leagues()
    client.leagues_answer = load_fixture("leagues")
    await store.async_snapshot(["bundesliga"])
    assert seen == [frozenset({"uel2026"}), frozenset()]


async def test_callback_is_not_called_when_the_check_fails(client):
    seen = []
    client.fail = True
    store = make_store(client, on_leagues_checked=seen.append)
    await store.async_warm_up()
    assert seen == []


async def test_a_failing_callback_is_logged_and_never_raised(client, caplog):
    def boom(invalid):
        raise RuntimeError("Rückruf kaputt")

    client.leagues_answer = _leagues_without("uel2026")
    store = make_store(client, on_leagues_checked=boom)
    snap = await store.async_snapshot(["bundesliga"])
    assert snap.invalid_shortcuts == {"uel2026"} and "bundesliga" in snap.loaded
    assert "Rückruf kaputt" in caplog.text
    await store.async_snapshot(["bundesliga"])  # die Prüfung gilt trotzdem als gemacht
    assert client.calls.count(("leagues",)) == 1


# --- Zeitstempel je Art der Daten ---


async def test_table_fetch_does_not_make_the_match_data_newer(store, client, clock):
    await store.async_warm_up()
    warmed = clock.now
    clock.advance(hours=1)
    snap = await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert ("table", "bl1", 2026) in client.calls and ("scorers", "bl1", 2026) in client.calls
    assert snap.fetched_at["bundesliga"] == warmed
    assert snap.tables_at["bundesliga"] == clock.now and snap.scorers_at["bundesliga"] == clock.now
    assert not snap.stale_tables and not snap.stale_scorers


async def test_table_older_than_changed_match_data_is_flagged_as_stale(store, client, clock):
    await store.async_warm_up()
    await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    table_time = clock.now
    clock.advance(minutes=16)
    client.changes[("bl1", 2026, 10)] = clock.now
    client.rounds[("bl1", 2026, 10)] = [
        dict(raw, matchIsFinished=True) if raw["matchID"] == 83237 else raw for raw in _fixture("bl1_2026_md10")
    ]
    client.rate_limited_after = len(client.calls) + 2  # Änderungsabfrage und Runde ja, Tabelle nicht mehr
    snap = await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert client.calls[-1] == ("round_matches", "bl1", 2026, 10)
    assert snap.fetched_at["bundesliga"] == clock.now
    assert snap.tables_at["bundesliga"] == table_time and snap.scorers_at["bundesliga"] == table_time
    assert snap.stale_tables == {"bundesliga"} and snap.stale_scorers == {"bundesliga"}
    client.rate_limited_after = None
    clock.advance(minutes=1)
    snap = await store.async_snapshot(["bundesliga"], tables=["bundesliga"], scorers=["bundesliga"])
    assert snap.tables_at["bundesliga"] == clock.now and not snap.stale_tables and not snap.stale_scorers


def _round_answer(fixture, order, **changes_by_id):
    """Die Spiele einer Runde aus einer Saisonliste, einzelne Spiele geändert."""
    return [
        dict(raw, **changes_by_id.get(raw["matchID"], {}))
        for raw in _fixture(fixture)
        if raw["group"]["groupOrderID"] == order
    ]


async def test_running_match_stand_is_the_fetch_of_its_round(store, client, clock):
    # 2. Bundesliga, 12. Spieltag: Samstag 13:00 Uhr Nürnberg gegen Braunschweig (und zwei weitere Spiele).
    await store.async_warm_up()  # Samstag, 08:00 Uhr
    warmed = clock.now
    client.fail = True
    clock.set(datetime(2026, 11, 21, 13, 40, tzinfo=BERLIN))
    snap = await store.async_snapshot(["2_bundesliga"])
    assert "2_bundesliga" in snap.loaded and snap.fetched_at["2_bundesliga"] == warmed
    client.fail = False
    client.rounds[("bl2", 2026, 12)] = _round_answer("bl2_2026", 12)
    clock.advance(minutes=2)  # nach dem Rückzug
    snap = await store.async_snapshot(["2_bundesliga"])
    assert client.calls[-1] == ("round_matches", "bl2", 2026, 12)
    assert snap.fetched_at["2_bundesliga"] == clock.now


async def test_without_running_match_stand_is_the_newest_match_data_fetch(store, client, clock):
    await store.async_warm_up()
    clock.set(datetime(2026, 11, 21, 17, 0, tzinfo=BERLIN))  # Spiele von 13:00 Uhr in den Daten "nicht beendet", aber über drei Stunden alt
    clock.advance(hours=1)
    client.fail = True
    snap = await store.async_snapshot(["2_bundesliga"])
    assert snap.fetched_at["2_bundesliga"] == datetime(2026, 11, 21, 8, 0, tzinfo=BERLIN)


# --- Leere oder unlesbare Antworten ---


def _unreadable(fixture):
    """Die Saisonliste mit umbenanntem Feld für die Kennung: kein Spiel ist mehr lesbar."""
    return [{("matchId" if k == "matchID" else k): v for k, v in raw.items()} for raw in _fixture(fixture)]


@pytest.mark.parametrize("answer", ["empty", "unreadable"])
async def test_empty_or_unreadable_season_list_keeps_the_known_matches(store, client, clock, caplog, answer):
    await store.async_warm_up()
    known = len((await store.async_snapshot(["2_bundesliga"])).matches["2_bundesliga"])
    clock.advance(hours=25)
    original = client.season_matches

    async def season_matches(s, y):
        if s == "bl2":
            raw = [] if answer == "empty" else _unreadable("bl2_2026")
            return await client._answer(("season_matches", s, y), y, lambda: raw)
        return await original(s, y)

    client.season_matches = season_matches
    snap = await store.async_snapshot(["2_bundesliga"])
    assert len(snap.matches["2_bundesliga"]) == known and "2_bundesliga" in snap.loaded
    assert snap.fetched_at["2_bundesliga"] == clock.now - timedelta(hours=25)
    assert "Saisonliste bl2/2026" in caplog.text and "WARNING" in caplog.text
    assert client.calls[-1] == ("leagues",)  # die Ligaliste wird im selben Durchgang neu geprüft
    # Rückzug für diese Liste: eine Minute lang kein neuer Abruf
    before = len(client.calls)
    await store.async_snapshot(["2_bundesliga"])
    assert len(client.calls) == before
    clock.advance(seconds=61)
    client.season_matches = original
    snap = await store.async_snapshot(["2_bundesliga"])
    assert client.calls[before:] == [("season_matches", "bl2", 2026)]
    assert len(snap.matches["2_bundesliga"]) == known
    assert snap.fetched_at["2_bundesliga"] == clock.now


async def test_mostly_unreadable_list_is_an_error_even_at_the_first_fetch(cold_store, client):
    original = client.season_matches

    async def season_matches(s, y):
        if s == "bl2":
            raw = _unreadable("bl2_2026")[:200] + _fixture("bl2_2026")[200:]  # 200 von 306 unlesbar
            return await client._answer(("season_matches", s, y), y, lambda: raw)
        return await original(s, y)

    client.season_matches = season_matches
    snap = await cold_store.async_snapshot(["2_bundesliga"])
    assert "2_bundesliga" not in snap.loaded and snap.unreachable == {"2_bundesliga"}


# Von Montag bis Donnerstag kein Spiel der 2. und 3. Liga; Mittwoch ist die Saisonliste vom Dienstag fällig, aber nicht veraltet.
TUESDAY = datetime(2026, 11, 24, 8, 0, tzinfo=BERLIN)
WEDNESDAY = datetime(2026, 11, 25, 10, 0, tzinfo=BERLIN)
_BL2_LIST = ("season_matches", "bl2", 2026)


def _broken_bl2_list(client, answer):
    """Ab jetzt liefert die Saisonliste bl2 nur noch `[]` oder Einträge mit umbenanntem Feld für die Kennung."""
    original = client.season_matches

    async def season_matches(s, y):
        if s == "bl2":
            raw = [] if answer == "empty" else _unreadable("bl2_2026")
            return await client._answer(("season_matches", s, y), y, lambda: raw)
        return await original(s, y)

    client.season_matches = season_matches
    return original


async def _wait_for_next(store, client, clock, keys, call):
    """Stellt die Uhr sekundenweise vor, bis `call` wieder abgerufen wird; liefert die Wartezeit."""
    before = client.calls.count(call)
    elapsed = timedelta()
    while client.calls.count(call) == before:
        clock.advance(seconds=1)
        elapsed += timedelta(seconds=1)
        await store.async_snapshot(keys)
    return elapsed


@pytest.mark.parametrize("answer", ["empty", "unreadable"])
async def test_season_list_rejected_again_and_again_backs_off_for_this_list_only(store, client, clock, caplog, answer):
    keys = ["2_bundesliga", "3_liga"]
    clock.set(TUESDAY)
    await store.async_warm_up()
    warmed = clock.now
    known = len((await store.async_snapshot(keys)).matches["2_bundesliga"])
    original = _broken_bl2_list(client, answer)
    clock.set(WEDNESDAY)
    before = len(client.calls)
    snap = await store.async_snapshot(keys)
    # Die 3. Liga nach der 2. Bundesliga wird im selben Durchgang aufgefrischt, und die Ligaliste wird geprüft.
    assert client.calls[before:] == [_BL2_LIST, ("season_matches", "bl3", 2026), ("leagues",)]
    assert len(snap.matches["2_bundesliga"]) == known and snap.fetched_at["2_bundesliga"] == warmed
    assert snap.fetched_at["3_liga"] == clock.now and not snap.unreachable
    before = len(client.calls)
    caplog.clear()
    waits = [await _wait_for_next(store, client, clock, keys, _BL2_LIST) for _ in range(7)]
    assert waits == [timedelta(seconds=s) for s in (60, 120, 240, 480, 900, 900, 900)]
    assert set(client.calls[before:]) == {_BL2_LIST, ("leagues",)}  # sonst nichts, auch keine Ligaliste ohne Grund
    assert client.calls[before:].count(("leagues",)) == 7
    assert "WARNING" not in caplog.text and "antwortet wieder" not in caplog.text  # nur die erste Warnung
    snap = await store.async_snapshot(keys)
    assert len(snap.matches["2_bundesliga"]) == known and "2_bundesliga" in snap.loaded
    # Erholung: eine gute Liste wird angenommen, danach wieder der normale Takt.
    client.season_matches = original
    clock.advance(minutes=15)
    snap = await store.async_snapshot(keys)
    assert snap.fetched_at["2_bundesliga"] == clock.now
    before = client.calls.count(_BL2_LIST)
    clock.advance(hours=23)
    await store.async_snapshot(keys)
    assert client.calls.count(_BL2_LIST) == before
    # Bei der nächsten verworfenen Liste beginnt der Rückzug wieder bei einer Minute.
    _broken_bl2_list(client, answer)
    clock.advance(hours=1)
    await store.async_snapshot(keys)
    assert client.calls.count(_BL2_LIST) == before + 1
    assert await _wait_for_next(store, client, clock, keys, _BL2_LIST) == timedelta(seconds=60)


async def test_never_loaded_list_rejected_again_and_again_is_unreachable_not_loading(cold_store, client, clock):
    keys = ["2_bundesliga", "3_liga"]
    _broken_bl2_list(client, "unreadable")
    for _ in range(3):
        snap = await cold_store.async_snapshot(keys)
        assert snap.unreachable == {"2_bundesliga"} and "3_liga" in snap.loaded
        clock.advance(minutes=20)
    assert client.calls.count(_BL2_LIST) == 3


async def test_live_round_rejected_again_and_again_backs_off_for_this_round(store, client, clock):
    await store.async_warm_up()
    clock.set(datetime(2026, 11, 21, 13, 5, tzinfo=BERLIN))  # drei Spiele der 2. Bundesliga laufen seit 13:00 Uhr
    client.rounds[("bl2", 2026, 12)] = []
    live = ("round_matches", "bl2", 2026, 12)
    await store.async_snapshot(["2_bundesliga"])
    assert client.calls.count(live) == 1
    waits = [await _wait_for_next(store, client, clock, ["2_bundesliga"], live) for _ in range(3)]
    assert waits == [timedelta(seconds=s) for s in (60, 120, 240)]
    client.rounds[("bl2", 2026, 12)] = _round_answer("bl2_2026", 12)
    clock.advance(minutes=8)
    snap = await store.async_snapshot(["2_bundesliga"])
    assert snap.fetched_at["2_bundesliga"] == clock.now
    assert await _wait_for_next(store, client, clock, ["2_bundesliga"], live) == timedelta(seconds=60)


async def test_a_few_unreadable_entries_are_skipped(cold_store, client):
    original = client.season_matches

    async def season_matches(s, y):
        if s == "bl2":
            raw = _unreadable("bl2_2026")[:3] + _fixture("bl2_2026")[3:]
            return await client._answer(("season_matches", s, y), y, lambda: raw)
        return await original(s, y)

    client.season_matches = season_matches
    snap = await cold_store.async_snapshot(["2_bundesliga"])
    assert len(snap.matches["2_bundesliga"]) == 303


async def test_empty_round_answer_does_not_count_as_a_fresh_stand(store, client, clock):
    await store.async_warm_up()
    warmed = clock.now
    clock.set(datetime(2026, 11, 21, 13, 40, tzinfo=BERLIN))
    client.rounds[("bl2", 2026, 12)] = []
    snap = await store.async_snapshot(["2_bundesliga"])
    assert client.calls[-2:] == [("round_matches", "bl2", 2026, 12), ("leagues",)]
    assert snap.fetched_at["2_bundesliga"] == warmed and len(snap.matches["2_bundesliga"]) == 306
    before = len(client.calls)
    clock.advance(seconds=30)
    await store.async_snapshot(["2_bundesliga"])
    assert len(client.calls) == before  # Rückzug für diese Runde


# --- Reihenfolge: erst alle Spieldaten, dann Tabellen und Torjäger ---


async def test_match_data_of_all_competitions_comes_before_tables_and_scorers(cold_store, client):
    keys = ["bundesliga", "2_bundesliga", "champions_league"]
    await cold_store.async_snapshot(keys, tables=keys, scorers=["bundesliga"])
    kinds = [c[0] for c in client.calls]
    first_extra = min(kinds.index(k) for k in ("table", "scorers"))
    assert all(k not in ("table", "scorers") for k in kinds[:first_extra])
    assert all(k in ("table", "scorers", "leagues") for k in kinds[first_extra:])
    assert [c for c in client.calls if c[0] in ("table", "scorers")] == [
        ("table", "bl1", 2026), ("scorers", "bl1", 2026), ("table", "bl2", 2026), ("table", "ucl", 2026)
    ]


async def test_a_failing_table_does_not_block_the_live_round_of_another_competition(client):
    client.clock.set(datetime(2026, 11, 20, 8, 0, tzinfo=BERLIN))
    store = make_store(client)
    await store.async_warm_up()
    client.clock.set(datetime(2026, 11, 20, 20, 40, tzinfo=BERLIN))  # Bundesliga (20:30) und 2. Bundesliga (18:30) laufen
    client.rounds[("bl2", 2026, 12)] = _round_answer("bl2_2026", 12)

    async def broken(*_):
        raise OpenLigaError("Tabelle kaputt")

    client.table = broken
    snap = await store.async_snapshot(["bundesliga", "2_bundesliga"], tables=["bundesliga"])
    assert ("round_matches", "bl2", 2026, 12) in client.calls and ("round_matches", "bl1", 2026, 10) in client.calls
    assert snap.fetched_at["2_bundesliga"] == client.clock.now and snap.fetched_at["bundesliga"] == client.clock.now


async def test_tables_and_scorers_only_for_the_asked_competitions(store, client):
    await store.async_warm_up()
    snap = await store.async_snapshot(COMPETITION_KEYS, scorers=["bundesliga"])
    assert [c for c in client.calls if c[0] in ("table", "scorers")] == [("scorers", "bl1", 2026)]
    assert set(snap.scorers) == {"bundesliga"} and not snap.tables
