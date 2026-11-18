"""Datenbestand je Wettbewerb mit Mindestabständen und geteilten Abrufen.

Alle Werkzeuge antworten aus einer Momentaufnahme (`Snapshot`) dieses Bestands. Abgerufen
wird nur bei Bedarf und nur, wenn der Zwischenspeicher älter ist als der Mindestabstand aus
`const`:

- Saisonliste je Kürzel höchstens einmal am Tag (`SEASON_LIST_MIN_AGE`).
- Läuft in einem Kürzel ein Spiel, wird nur die Runde dieser Spiele aufgefrischt, höchstens
  einmal je Minute (`ROUND_MIN_AGE_LIVE`).
- Sonst wird für jede Runde mit Spielen von gestern bis morgen (Ortstage) höchstens alle
  15 Minuten die Änderungsabfrage gestellt (`ROUND_MIN_AGE_IDLE`); abgerufen wird die Runde
  nur, wenn die Abfrage einen neueren Zeitpunkt meldet als den bekannten Stand der Runde.
- Tabelle und Torjägerliste nur auf Wunsch je Wettbewerb, und nur wenn sich seit ihrem letzten
  Abruf Spieldaten des Kürzels geändert haben oder `TABLE_MAX_AGE` überschritten ist. Sie kommen
  erst, wenn die Spieldaten aller angefragten Wettbewerbe aufgefrischt sind: Ein Fehler oder die
  Obergrenze bei einer Tabelle hält so keine Spieldaten auf.
- Listen der abgelaufenen Saison (Vorsaison und Rückgriff bei leerer neuer Saison) werden je
  Kürzel höchstens einmal geladen.

Eine leere Saisonliste oder Runde, die bekannte Spiele ersetzen würde, und eine Antwort, deren
Einträge zur Mehrheit unlesbar sind, werden verworfen: Der Zwischenspeicher bleibt, und die Ligaliste
wird noch im selben Aufruf neu geprüft (vielleicht hat sich ein Kürzel geändert). Nur beim allerersten
Abruf darf eine Saisonliste leer sein (eine neue Saison ohne Spiele). Eine verworfene Antwort ist kein
Ausfall der Quelle (sie hat geantwortet), sondern ein Problem dieser einen Liste oder Runde: Nur sie
wird bis zum Ende ihres eigenen Rückzugs nicht abgerufen (Wartezeit wie unten, je Verwerfen verdoppelt),
das Auffrischen geht mit den übrigen Wettbewerben weiter, und eine angenommene Antwort setzt ihren
Rückzug zurück. Solange die letzte Antwort einer Liste oder Runde eines Wettbewerbs verworfen ist,
zählt der Wettbewerb in der Momentaufnahme wie bei einem Fehler der Quelle.

Nach einem Fehler der Quelle (keine Antwort, HTTP-Fehler, Zeitüberschreitung) endet das Auffrischen
des laufenden Aufrufs, und für die ganze Quelle (nicht je Kürzel) wird bis zum Ende des Rückzugs nichts
abgerufen; die Wartezeit verdoppelt sich je Fehler bis `RETRY_AFTER_ERROR_MAX`. Der Rückzug gilt für die
ganze Quelle, weil ein Fehler fast immer die Verbindung betrifft und die Quelle sonst je Kürzel erneut
angefragt würde. `RateLimited` (eigene Obergrenze je Minute) beendet nur das Auffrischen
des laufenden Aufrufs. Nichts hier wirft an die Werkzeuge weiter.

Ein Lock je Kürzel sorgt dafür, dass gleichzeitige Fragen sich einen Abruf teilen: Wer den
Lock bekommt, prüft das Alter erneut. Locks werden nie verschachtelt gehalten.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from types import MappingProxyType
from typing import Any

from . import const
from .api import OpenLigaClient, OpenLigaError, RateLimited
from .models import Match, Scorer, TableRow
from .parse import SOURCE_TIME_ZONE, parse_matches, parse_scorers, parse_table
from .seasons import Competition, Kind, Season
from .timeutil import local_day

_LOGGER = logging.getLogger(__name__)

SCHEDULED = "angesetzt"
LIVE = "laeuft"
FINISHED = "beendet"
NOT_FINISHED = "laut_daten_nicht_beendet"

_ONE_DAY = timedelta(days=1)


def match_state(match: Match, now: datetime) -> str:
    """Zustand eines Spiels aus Anstoß, Angabe "beendet" und Uhr: angesetzt, läuft, beendet oder laut Daten nicht beendet."""
    if now < match.kickoff:
        return SCHEDULED
    if match.finished:
        return FINISHED
    if now - match.kickoff <= const.LIVE_WINDOW:
        return LIVE
    return NOT_FINISHED


def _sorted(matches: Iterable[Match]) -> tuple[Match, ...]:
    return tuple(sorted(matches, key=lambda m: (m.kickoff, m.id)))


def merge_national(by_shortcut: Mapping[str, Sequence[Match]], ids: Mapping[str, int]) -> tuple[Match, ...]:
    """Spiele der Nationalmannschaft aus mehreren Datensträngen, jede Partie einmal.

    Aus jedem Strang zählen nur Spiele mit der dortigen Kennung der Mannschaft. Schlüssel
    einer Partie ist der Ortstag des Anstoßes (Europe/Berlin, die Zeitzone der Quelle) mit der
    Rolle der Mannschaft (Heim oder Gast), nicht Gegner und nicht Uhrzeit, weil beide je Strang
    abweichen. Steht eine Partie in mehreren Strängen, gilt der Strang, der in `ids` zuerst
    steht (Reihenfolge der Saisondatei, 2026/27: `nla`). Stränge ohne Kennung entfallen.
    """
    merged: dict[tuple[date, bool], Match] = {}
    for shortcut, team_id in ids.items():
        for match in by_shortcut.get(shortcut, ()):
            if team_id not in (match.home.id, match.away.id):
                continue
            key = (local_day(match.kickoff, SOURCE_TIME_ZONE), match.home.id == team_id)
            merged.setdefault(key, match)
    return _sorted(merged.values())


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Unveränderliche Momentaufnahme des Bestands für die angefragten Wettbewerbe.

    Jeder angefragte und eingestellte Wettbewerb steht in genau einer der Gruppen `loaded`
    (Daten vorhanden), `unreachable` (keine brauchbaren Daten, weil die Quelle nicht antwortet)
    oder in keiner von beiden (noch nicht geladen, etwa wegen der Obergrenze je Minute).

    Zeitstempel je Wettbewerb, jeweils der letzte erfolgreiche Abruf:
    - `fetched_at`: Spieldaten (Saisonliste oder Runde). Läuft ein Spiel, ist es der älteste Abruf
      unter den Runden mit laufenden Spielen; nur daran lässt sich ablesen, ob ein Spielstand frisch ist.
    - `tables_at`, `scorers_at`: Tabelle und Torjägerliste, unabhängig von den Spieldaten.
    `stale_tables` und `stale_scorers` nennen die Wettbewerbe, deren Spieldaten sich seit dem Abruf von
    Tabelle oder Torjägerliste geändert haben, ohne dass diese aufgefrischt werden konnten.
    """

    now: datetime
    tz: tzinfo
    season: Season
    matches: Mapping[str, tuple[Match, ...]]
    tables: Mapping[str, tuple[TableRow, ...]]
    scorers: Mapping[str, tuple[Scorer, ...]]
    previous_matches: Mapping[str, tuple[Match, ...]]
    loaded: frozenset[str]
    unreachable: frozenset[str]
    fetched_at: Mapping[str, datetime]
    tables_at: Mapping[str, datetime]
    scorers_at: Mapping[str, datetime]
    stale_tables: frozenset[str]
    stale_scorers: frozenset[str]
    invalid_shortcuts: frozenset[str]
    ended_season: Mapping[str, Season]


@dataclass(slots=True)
class _ListState:
    """Zwischenspeicher eines Kürzels in einer Saison."""

    matches: dict[int, Match] | None = None  # None: Saisonliste nie geladen
    list_at: datetime | None = None  # letzter Abruf der Saisonliste
    matches_at: datetime | None = None  # letzter erfolgreicher Abruf von Spieldaten (Saisonliste oder Runde)
    changed_at: datetime | None = None  # letzte Änderung der Spieldaten
    round_fetched: dict[int, datetime] = field(default_factory=dict)
    round_checked: dict[int, datetime] = field(default_factory=dict)
    round_mark: dict[int, datetime | None] = field(default_factory=dict)  # bekannter Stand je Runde
    round_pending: dict[int, datetime] = field(default_factory=dict)  # gemeldete Änderung, noch nicht abgerufen
    # Rückzug nach verworfenen Antworten: Anzahl in Folge und frühester neuer Abruf, für die Saisonliste und je Runde
    list_rejected: int = 0
    list_retry_at: datetime | None = None
    round_rejected: dict[int, int] = field(default_factory=dict)
    round_retry_at: dict[int, datetime] = field(default_factory=dict)
    table: tuple[TableRow, ...] | None = None
    table_at: datetime | None = None
    scorers: tuple[Scorer, ...] | None = None
    scorers_at: datetime | None = None


@dataclass(slots=True)
class _Run:
    """Ein Auffrischen; beim Laden im Hintergrund mit Pause und Warten auf einen freien Abruf."""

    warm_up: bool
    fetches: int = 0


class _Stop(Exception):
    """Beendet das Auffrischen des laufenden Aufrufs (Fehler oder Rückzug der Quelle, Obergrenze)."""


def _unusable(raw: Any, parsed: Sequence[Match], *, known: int) -> str | None:
    """Grund, warum eine Antwort mit Spielen nicht taugt, sonst None.

    Unbrauchbar ist eine Antwort, deren Einträge zur Mehrheit unlesbar sind, und eine leere, wo `known`
    Spiele bekannt sind (sie würde sie ersetzen oder als frisch gelten lassen).
    """
    total = len(raw) if isinstance(raw, list) else 0
    if total == 0:
        return "leer" if known else None
    if (total - len(parsed)) * 2 > total:
        return f"unlesbar (nur {len(parsed)} von {total} Einträgen lesbar)"
    return None


@dataclass(frozen=True, slots=True)
class _Resolved:
    matches: tuple[Match, ...]
    table: tuple[TableRow, ...] | None
    scorers: tuple[Scorer, ...] | None
    ended: Season | None
    fetched_at: datetime | None  # Spieldaten
    live_at: datetime | None  # ältester Abruf der Runden mit laufenden Spielen; None ohne laufendes Spiel
    table_at: datetime | None
    scorers_at: datetime | None
    table_stale: bool
    scorers_stale: bool


class DataStore:
    """Gemeinsamer Zwischenspeicher je Wettbewerb für alle Werkzeuge."""

    def __init__(
        self,
        client: OpenLigaClient,
        season: Season,
        competitions: Sequence[str],
        *,
        previous: Season | None,
        utcnow: Callable[[], datetime],
        tz: tzinfo,
        on_leagues_checked: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        self.season = season
        self._on_leagues_checked = on_leagues_checked
        self._client = client
        self._previous = previous
        self._utcnow = utcnow
        self._tz = tz
        self._keys = tuple(dict.fromkeys(k for k in competitions if k in season.competitions))
        self._states: dict[tuple[str, int], _ListState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._leagues_lock = asyncio.Lock()
        self._leagues_checked = False
        self._invalid: frozenset[str] = frozenset()
        self._errors = 0
        self._retry_at: datetime | None = None

    # --- öffentlich ---

    @property
    def invalid_shortcuts(self) -> frozenset[str]:
        """Kürzel ohne Eintrag in der Ligaliste; leer, solange die Ligaliste nicht geprüft werden konnte."""
        return self._invalid

    def recheck_leagues(self) -> None:
        """Die nächste Momentaufnahme prüft die Ligaliste erneut (kein Abruf ohne Frage).

        Bis dahin gilt der bisherige Stand von `invalid_shortcuts`; scheitert die neue Prüfung,
        bleibt er bestehen.
        """
        self._leagues_checked = False

    async def async_warm_up(self) -> None:
        """Ligaliste prüfen und je Kürzel einmal die Saisonliste laden, mit Pause dazwischen."""
        run = _Run(warm_up=True)
        try:
            await self._ensure_leagues(run)
            for key in self._keys:
                await self._refresh_matches(run, key, previous=False)
        except _Stop:
            pass
        except Exception:
            _LOGGER.exception("Laden im Hintergrund fehlgeschlagen")

    async def async_snapshot(
        self,
        competitions: Iterable[str],
        *,
        tables: Iterable[str] = (),
        scorers: Iterable[str] = (),
        previous: bool = False,
    ) -> Snapshot:
        """Frischt auf, was fällig ist, und liefert den vorhandenen Stand. Wirft nie.

        `competitions` sind die Wettbewerbe mit Spieldaten; `tables` und `scorers` die unter ihnen, deren
        Tabelle oder Torjägerliste die Antwort braucht. Zuerst werden die Spieldaten aller Wettbewerbe
        aufgefrischt, danach Tabellen und Torjägerlisten, zuletzt die Ligaliste.
        """
        try:
            keys = tuple(dict.fromkeys(k for k in competitions if k in self._keys))
            with_table = frozenset(tables) & frozenset(keys)
            with_scorers = frozenset(scorers) & frozenset(keys)
        except Exception:
            _LOGGER.exception("Ungültige Angabe der Wettbewerbe")
            keys, with_table, with_scorers = (), frozenset(), frozenset()
        run = _Run(warm_up=False)
        try:
            for key in keys:
                await self._refresh_matches(run, key, previous=previous)
            for key in keys:
                await self._refresh_extras(run, key, table=key in with_table, scorers=key in with_scorers)
            # Ligaliste zuletzt: Spieldaten haben Vorrang, falls die Obergrenze erreicht ist.
            await self._ensure_leagues(run)
        except _Stop:
            pass
        except Exception:
            _LOGGER.exception("Auffrischen fehlgeschlagen")
        try:
            return self._build(keys, previous=previous)
        except Exception:
            _LOGGER.exception("Momentaufnahme fehlgeschlagen")
            return self._empty_snapshot()

    def is_live(self, competition: str) -> bool:
        """Läuft im Wettbewerb laut Zwischenspeicher gerade ein Spiel?"""
        try:
            if competition not in self._keys:
                return False
            resolved = self._resolve(competition)
            if resolved is None:
                return False
            now = self._utcnow()
            return any(match_state(m, now) == LIVE for m in resolved.matches)
        except Exception:
            _LOGGER.exception("Prüfung auf laufende Spiele fehlgeschlagen")
            return False

    # --- Abrufe ---

    def _state(self, shortcut: str, year: int) -> _ListState:
        return self._states.setdefault((shortcut, year), _ListState())

    def _lock(self, shortcut: str) -> asyncio.Lock:
        return self._locks.setdefault(shortcut, asyncio.Lock())

    async def _pace(self, run: _Run) -> None:
        """Beim Laden im Hintergrund: Pause zwischen zwei Abrufen und Warten auf einen freien Abruf.

        Läuft außerhalb der Locks, damit eine gleichzeitige Frage nicht auf die Pause wartet.
        """
        if not run.warm_up:
            return
        if run.fetches:
            await asyncio.sleep(const.WARM_UP_PAUSE)
        await self._client.wait_for_slot()

    async def _call[T](self, run: _Run, fetch: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Ein Abruf mit Rückzug nach Fehlern; jeder Fehler beendet das Auffrischen (`_Stop`).

        Jede Antwort zählt als Lebenszeichen der Quelle und beendet ihren Rückzug, auch wenn sie danach
        verworfen wird.
        """
        if self._retry_at is not None and self._utcnow() < self._retry_at:
            raise _Stop
        run.fetches += 1
        try:
            result = await fetch(*args)
        except RateLimited as err:
            _LOGGER.debug("Obergrenze je Minute erreicht, Rest wartet: %s", err)
            raise _Stop from err
        except OpenLigaError as err:
            self._failed(f"{err}")
            raise _Stop from err
        except Exception as err:  # z. B. UnicodeDecodeError aus dem Client: wie ein Fehler der Quelle
            self._failed(f"unerwartet {type(err).__name__}: {err}")
            raise _Stop from err
        if self._errors:
            _LOGGER.info("OpenLigaDB antwortet wieder")
        self._errors = 0
        self._retry_at = None
        return result

    @staticmethod
    def _backoff(count: int) -> timedelta:
        """Wartezeit nach `count` Fehlern oder verworfenen Antworten in Folge: 60 s, 120 s, 240 s … höchstens 15 min."""
        return min(const.RETRY_AFTER_ERROR * 2 ** (count - 1), const.RETRY_AFTER_ERROR_MAX)

    def _rejected(self, what: str, reason: str, count: int) -> datetime:
        """Eine Antwort taugt nicht (leer oder unlesbar); `count` ist die Zahl der Verwerfungen in Folge mit dieser.

        Liefert den frühesten neuen Abruf dieser Liste oder Runde. Der Rückzug der ganzen Quelle bleibt
        unberührt, und die Ligaliste wird neu geprüft.
        """
        delay = self._backoff(count)
        log = _LOGGER.warning if count == 1 else _LOGGER.debug
        log("%s von OpenLigaDB ist %s; der bisherige Stand bleibt, nächster Versuch in %s", what, reason, delay)
        self.recheck_leagues()
        return self._utcnow() + delay

    def _failed(self, reason: str) -> None:
        self._errors += 1
        delay = self._backoff(self._errors)
        self._retry_at = self._utcnow() + delay
        log = _LOGGER.warning if self._errors == 1 else _LOGGER.debug
        log("Abruf bei OpenLigaDB fehlgeschlagen (%s), nächster Versuch in %s", reason, delay)

    async def _ensure_leagues(self, run: _Run) -> None:
        """Prüft die Kürzel gegen die Ligaliste; scheitert das, beim nächsten Aufruf erneut.

        Ohne Ligaliste bleibt `invalid_shortcuts` leer: offline wird kein Kürzel als fehlend
        gemeldet. Wiederholt wird die Prüfung nur auf `recheck_leagues()`. Nach jeder erfolgreichen
        Prüfung bekommt `on_leagues_checked` den neuen Satz ungültiger Kürzel.
        """
        if self._leagues_checked:
            return
        await self._pace(run)
        async with self._leagues_lock:
            if self._leagues_checked:
                return
            leagues = await self._call(run, self._client.leagues)
            known = {
                (entry.get("leagueShortcut"), str(entry.get("leagueSeason")))
                for entry in leagues
                if isinstance(entry, Mapping)
            }
            year = str(self.season.year)
            self._invalid = frozenset(
                shortcut
                for key in self._keys
                for shortcut in self.season.competitions[key].shortcuts
                if (shortcut, year) not in known
            )
            self._leagues_checked = True
            if self._invalid:
                _LOGGER.warning("Kürzel ohne Eintrag in der Ligaliste: %s", ", ".join(sorted(self._invalid)))
            if self._on_leagues_checked is not None:
                try:
                    self._on_leagues_checked(self._invalid)
                except Exception:
                    _LOGGER.exception("Rückruf nach der Prüfung der Ligaliste fehlgeschlagen")

    def _ended_source(self, key: str) -> Competition | None:
        """Der Wettbewerb der abgelaufenen Saison, wenn die laufende (geladen) noch keine Spiele hat."""
        prev = self._previous.competitions.get(key) if self._previous is not None else None
        if prev is None or self._combine(self.season.competitions[key], self.season.year) != ():
            return None
        return prev

    async def _refresh_matches(self, run: _Run, key: str, *, previous: bool) -> None:
        """Saisonlisten und Runden eines Wettbewerbs, dazu bei Bedarf die der abgelaufenen Saison."""
        comp = self.season.competitions[key]
        year = self.season.year
        for shortcut in comp.shortcuts:
            await self._refresh_list(run, comp, shortcut, year, once=False)
        for shortcut in comp.shortcuts:
            await self._refresh_rounds(run, comp, shortcut, year)
        prev = self._previous.competitions.get(key) if self._previous is not None else None
        if prev is not None and (previous or self._ended_source(key) is not None):
            for shortcut in prev.shortcuts:
                await self._refresh_list(run, prev, shortcut, self._previous.year, once=True)

    async def _refresh_extras(self, run: _Run, key: str, *, table: bool, scorers: bool) -> None:
        """Tabelle und Torjägerliste eines Wettbewerbs, aus der Saison, deren Spiele ausgeliefert werden."""
        if not (table or scorers):
            return
        ended = self._ended_source(key)
        if ended is not None:
            source, source_year, once = ended, self._previous.year, True
        else:
            source, source_year, once = self.season.competitions[key], self.season.year, False
        if table and source.has_table:
            await self._refresh_extra(run, source.shortcuts[0], source_year, "table", once=once)
        if scorers and source.has_scorers:
            await self._refresh_extra(run, source.shortcuts[0], source_year, "scorers", once=once)

    def _list_due(self, state: _ListState, *, once: bool) -> bool:
        if state.list_retry_at is not None and self._utcnow() < state.list_retry_at:
            return False
        if state.matches is None or state.list_at is None:
            return True
        return not once and self._utcnow() - state.list_at >= const.SEASON_LIST_MIN_AGE

    async def _refresh_list(self, run: _Run, comp: Competition, shortcut: str, year: int, *, once: bool) -> None:
        state = self._state(shortcut, year)
        if not self._list_due(state, once=once):
            return
        await self._pace(run)
        async with self._lock(shortcut):
            if not self._list_due(state, once=once):
                return
            raw = await self._call(run, self._client.season_matches, shortcut, year)
            parsed = parse_matches(raw)
            if (reason := _unusable(raw, parsed, known=len(state.matches or {}))) is not None:
                state.list_rejected += 1
                state.list_retry_at = self._rejected(f"Saisonliste {shortcut}/{year}", reason, state.list_rejected)
                return
            state.list_rejected, state.list_retry_at = 0, None
            now = self._utcnow()
            matches = {m.id: m for m in parsed}
            if matches != state.matches:
                state.changed_at = now
            state.matches = matches
            state.list_at = state.matches_at = now
            # Die Saisonliste ist der neueste Stand aller Runden, auch solcher mit verworfener Antwort.
            state.round_mark.clear()
            state.round_pending.clear()
            state.round_rejected.clear()
            state.round_retry_at.clear()
            for order in self._live_rounds(comp, shortcut, matches.values(), now):
                state.round_fetched[order] = now

    def _relevant(self, comp: Competition, shortcut: str, matches: Iterable[Match]) -> list[Match]:
        """Spiele, die für den Wettbewerb zählen (Nationalmannschaft: nur mit ihrer Kennung)."""
        if comp.kind is Kind.NATIONAL and shortcut in comp.national_team_ids:
            team_id = comp.national_team_ids[shortcut]
            return [m for m in matches if team_id in (m.home.id, m.away.id)]
        return list(matches)

    def _live_rounds(self, comp: Competition, shortcut: str, matches: Iterable[Match], now: datetime) -> list[int]:
        relevant = self._relevant(comp, shortcut, matches)
        return sorted({m.round_order for m in relevant if match_state(m, now) == LIVE})

    async def _refresh_rounds(self, run: _Run, comp: Competition, shortcut: str, year: int) -> None:
        state = self._state(shortcut, year)
        if state.matches is None:
            return
        now = self._utcnow()
        live = self._live_rounds(comp, shortcut, state.matches.values(), now)
        if live:
            for order in live:
                await self._refresh_live_round(run, shortcut, year, order)
            return
        today = local_day(now, self._tz)
        window = (today - _ONE_DAY, today, today + _ONE_DAY)
        relevant = self._relevant(comp, shortcut, state.matches.values())
        for order in sorted({m.round_order for m in relevant if local_day(m.kickoff, self._tz) in window}):
            await self._check_idle_round(run, shortcut, year, order)
            if order in state.round_pending:
                await self._fetch_pending_round(run, shortcut, year, order)

    def _round_waiting(self, state: _ListState, order: int) -> bool:
        """Wartet die Runde nach einer verworfenen Antwort noch auf ihren nächsten Abruf?"""
        retry_at = state.round_retry_at.get(order)
        return retry_at is not None and self._utcnow() < retry_at

    def _live_round_due(self, state: _ListState, order: int) -> bool:
        if self._round_waiting(state, order):
            return False
        last = state.round_fetched.get(order)
        return last is None or self._utcnow() - last >= const.ROUND_MIN_AGE_LIVE

    async def _refresh_live_round(self, run: _Run, shortcut: str, year: int, order: int) -> None:
        state = self._state(shortcut, year)
        if not self._live_round_due(state, order):
            return
        await self._pace(run)
        async with self._lock(shortcut):
            if not self._live_round_due(state, order):
                return
            await self._fetch_round(run, state, shortcut, year, order)

    def _idle_check_due(self, state: _ListState, order: int) -> bool:
        if order in state.round_pending:
            return False  # Änderung schon gemeldet, nur noch abrufen
        seen = [t for t in (state.round_checked.get(order), state.round_fetched.get(order), state.list_at) if t]
        return not seen or self._utcnow() - max(seen) >= const.ROUND_MIN_AGE_IDLE

    async def _check_idle_round(self, run: _Run, shortcut: str, year: int, order: int) -> None:
        state = self._state(shortcut, year)
        if not self._idle_check_due(state, order):
            return
        await self._pace(run)
        async with self._lock(shortcut):
            if not self._idle_check_due(state, order):
                return
            changed = await self._call(run, self._client.last_change, shortcut, year, order)
            state.round_checked[order] = self._utcnow()
            if order not in state.round_mark:
                state.round_mark[order] = self._round_stand(state, order)
            mark = state.round_mark[order]
            if isinstance(changed, datetime) and (mark is None or changed > mark):
                state.round_pending[order] = changed

    async def _fetch_pending_round(self, run: _Run, shortcut: str, year: int, order: int) -> None:
        state = self._state(shortcut, year)
        if self._round_waiting(state, order):
            return
        await self._pace(run)
        async with self._lock(shortcut):
            if order not in state.round_pending or self._round_waiting(state, order):
                return
            await self._fetch_round(run, state, shortcut, year, order)

    @staticmethod
    def _round_stand(state: _ListState, order: int) -> datetime | None:
        """Neueste Änderung der Spiele einer Runde laut Zwischenspeicher."""
        stamps = [
            m.last_update for m in (state.matches or {}).values() if m.round_order == order and m.last_update
        ]
        return max(stamps, default=None)

    async def _fetch_round(self, run: _Run, state: _ListState, shortcut: str, year: int, order: int) -> None:
        """Ruft eine Runde ab (Lock wird vom Aufrufer gehalten) und ersetzt deren Spiele nach Kennung."""
        raw = await self._call(run, self._client.round_matches, shortcut, year, order)
        fetched = parse_matches(raw)
        known = sum(1 for m in (state.matches or {}).values() if m.round_order == order)
        if (reason := _unusable(raw, fetched, known=known)) is not None:
            count = state.round_rejected[order] = state.round_rejected.get(order, 0) + 1
            state.round_retry_at[order] = self._rejected(f"Runde {shortcut}/{year}/{order}", reason, count)
            return
        state.round_rejected.pop(order, None)
        state.round_retry_at.pop(order, None)
        now = self._utcnow()
        matches = dict(state.matches or {})
        if any(matches.get(m.id) != m for m in fetched):
            state.changed_at = now
        matches.update((m.id, m) for m in fetched)
        state.matches = matches
        state.round_fetched[order] = state.round_checked[order] = state.matches_at = now
        stamps = [t for t in (state.round_pending.pop(order, None), self._round_stand(state, order)) if t]
        state.round_mark[order] = max(stamps, default=None)

    def _extra_due(self, state: _ListState, kind: str, *, once: bool) -> bool:
        at = state.table_at if kind == "table" else state.scorers_at
        if at is None:
            return True
        if once:
            return False
        if self._utcnow() - at >= const.TABLE_MAX_AGE:
            return True
        return state.changed_at is not None and state.changed_at > at

    async def _refresh_extra(self, run: _Run, shortcut: str, year: int, kind: str, *, once: bool) -> None:
        state = self._state(shortcut, year)
        if not self._extra_due(state, kind, once=once):
            return
        await self._pace(run)
        async with self._lock(shortcut):
            if not self._extra_due(state, kind, once=once):
                return
            if kind == "table":
                rows = parse_table(await self._call(run, self._client.table, shortcut, year))
                state.table, state.table_at = rows, self._utcnow()
            else:
                found = parse_scorers(await self._call(run, self._client.scorers, shortcut, year))
                state.scorers, state.scorers_at = found, self._utcnow()

    # --- Momentaufnahme ---

    def _combine(self, comp: Competition, year: int) -> tuple[Match, ...] | None:
        """Spiele eines Wettbewerbs aus allen Kürzeln, Dubletten bereinigt; None, solange eins fehlt."""
        by_shortcut: dict[str, tuple[Match, ...]] = {}
        for shortcut in comp.shortcuts:
            state = self._states.get((shortcut, year))
            if state is None or state.matches is None:
                return None
            by_shortcut[shortcut] = tuple(state.matches.values())
        if comp.kind is Kind.NATIONAL:
            return merge_national(by_shortcut, comp.national_team_ids)
        unique: dict[int, Match] = {}
        for matches in by_shortcut.values():
            for match in matches:
                unique.setdefault(match.id, match)
        return _sorted(unique.values())

    def _fetched_at(self, comp: Competition, year: int) -> datetime | None:
        """Letzter erfolgreicher Abruf von Spieldaten über alle Kürzel des Wettbewerbs."""
        stamps = [
            state.matches_at
            for shortcut in comp.shortcuts
            if (state := self._states.get((shortcut, year))) is not None and state.matches_at is not None
        ]
        return max(stamps, default=None)

    def _live_at(self, comp: Competition, year: int, matches: Iterable[Match]) -> datetime | None:
        """Ältester Stand der Runden mit laufenden Spielen; None, wenn keins läuft.

        Der Stand einer Runde ist ihr letzter Abruf oder, wenn neuer, der der ganzen Saisonliste, jeweils aus
        dem Kürzel, aus dem das Spiel stammt.
        """
        now = self._utcnow()
        stamps: list[datetime] = []
        for match in matches:
            if match_state(match, now) != LIVE:
                continue
            state = next(
                (
                    s
                    for shortcut in comp.shortcuts
                    if (s := self._states.get((shortcut, year))) is not None and match.id in (s.matches or {})
                ),
                None,
            )
            if state is None or state.list_at is None:  # kommt nicht vor: Spiele gibt es erst nach der Saisonliste
                continue
            stamps.append(max(t for t in (state.list_at, state.round_fetched.get(match.round_order)) if t))
        return min(stamps, default=None)

    def _resolve(self, key: str) -> _Resolved | None:
        """Daten, die für einen Wettbewerb ausgeliefert werden; None, solange sie nicht vollständig sind."""
        comp = self.season.competitions[key]
        year = self.season.year
        current = self._combine(comp, year)
        if current is None:
            return None
        fetched = [self._fetched_at(comp, year)]
        source, source_year, ended = comp, year, None
        prev = self._previous.competitions.get(key) if self._previous is not None else None
        if current == () and prev is not None:
            current = self._combine(prev, self._previous.year)
            if current is None:
                return None
            source, source_year, ended = prev, self._previous.year, self._previous
            fetched.append(self._fetched_at(prev, source_year))
        first = self._states.get((source.shortcuts[0], source_year)) or _ListState()
        table = first.table if source.has_table else None
        scorers = first.scorers if source.has_scorers else None
        return _Resolved(
            matches=current,
            table=table,
            scorers=scorers,
            ended=ended,
            fetched_at=max((t for t in fetched if t), default=None),
            live_at=self._live_at(source, source_year, current),
            table_at=first.table_at if table is not None else None,
            scorers_at=first.scorers_at if scorers is not None else None,
            table_stale=table is not None and self._outdated(first, first.table_at),
            scorers_stale=scorers is not None and self._outdated(first, first.scorers_at),
        )

    @staticmethod
    def _outdated(state: _ListState, extra_at: datetime | None) -> bool:
        """Haben sich die Spieldaten seit dem Abruf von Tabelle oder Torjägerliste geändert?"""
        return extra_at is not None and state.changed_at is not None and state.changed_at > extra_at

    def _rejecting(self, key: str) -> bool:
        """Ist die letzte Antwort für eine Saisonliste oder Runde des Wettbewerbs verworfen (laufende oder abgelaufene Saison)?"""
        sources = [(self.season.competitions[key], self.season.year)]
        if self._previous is not None and key in self._previous.competitions:
            sources.append((self._previous.competitions[key], self._previous.year))
        return any(
            state.list_rejected or state.round_rejected
            for comp, year in sources
            for shortcut in comp.shortcuts
            if (state := self._states.get((shortcut, year))) is not None
        )

    def _build(self, keys: Sequence[str], *, previous: bool) -> Snapshot:
        now = self._utcnow()
        matches: dict[str, tuple[Match, ...]] = {}
        tables: dict[str, tuple[TableRow, ...]] = {}
        scorers: dict[str, tuple[Scorer, ...]] = {}
        previous_matches: dict[str, tuple[Match, ...]] = {}
        fetched_at: dict[str, datetime] = {}
        tables_at: dict[str, datetime] = {}
        scorers_at: dict[str, datetime] = {}
        stale_tables: set[str] = set()
        stale_scorers: set[str] = set()
        ended_season: dict[str, Season] = {}
        loaded: set[str] = set()
        unreachable: set[str] = set()
        for key in keys:
            resolved = self._resolve(key)
            stamp = resolved.fetched_at if resolved is not None else self._any_fetched_at(key)
            if stamp is not None:
                fetched_at[key] = stamp
            # Wie bei einem Fehler der Quelle zählt auch eine verworfene Antwort: Ein nie geladener Wettbewerb
            # gilt dann als nicht erreichbar statt ewig als "lädt noch", ein veralteter Stand ebenso.
            failing = self._errors > 0 or self._rejecting(key)
            if failing and (resolved is None or stamp is None or now - stamp > const.STALE_LIMIT):
                unreachable.add(key)
                continue
            if resolved is None:
                continue
            loaded.add(key)
            matches[key] = resolved.matches
            if resolved.live_at is not None:
                fetched_at[key] = resolved.live_at
            if resolved.table is not None:
                tables[key] = resolved.table
                if resolved.table_at is not None:
                    tables_at[key] = resolved.table_at
                if resolved.table_stale:
                    stale_tables.add(key)
            if resolved.scorers is not None:
                scorers[key] = resolved.scorers
                if resolved.scorers_at is not None:
                    scorers_at[key] = resolved.scorers_at
                if resolved.scorers_stale:
                    stale_scorers.add(key)
            if resolved.ended is not None:
                ended_season[key] = resolved.ended
            if previous and self._previous is not None and key in self._previous.competitions:
                older = self._combine(self._previous.competitions[key], self._previous.year)
                if older is not None:
                    previous_matches[key] = older
        return Snapshot(
            now=now,
            tz=self._tz,
            season=self.season,
            matches=MappingProxyType(matches),
            tables=MappingProxyType(tables),
            scorers=MappingProxyType(scorers),
            previous_matches=MappingProxyType(previous_matches),
            loaded=frozenset(loaded),
            unreachable=frozenset(unreachable),
            fetched_at=MappingProxyType(fetched_at),
            tables_at=MappingProxyType(tables_at),
            scorers_at=MappingProxyType(scorers_at),
            stale_tables=frozenset(stale_tables),
            stale_scorers=frozenset(stale_scorers),
            invalid_shortcuts=self._invalid,
            ended_season=MappingProxyType(ended_season),
        )

    def _any_fetched_at(self, key: str) -> datetime | None:
        """Letzter erfolgreicher Abruf, auch wenn die Daten noch unvollständig sind."""
        stamps = [self._fetched_at(self.season.competitions[key], self.season.year)]
        if self._previous is not None and key in self._previous.competitions:
            stamps.append(self._fetched_at(self._previous.competitions[key], self._previous.year))
        return max((t for t in stamps if t), default=None)

    def _empty_snapshot(self) -> Snapshot:
        empty: Mapping[str, Any] = MappingProxyType({})
        return Snapshot(
            now=self._utcnow(),
            tz=self._tz,
            season=self.season,
            matches=empty,
            tables=empty,
            scorers=empty,
            previous_matches=empty,
            loaded=frozenset(),
            unreachable=frozenset(),
            fetched_at=empty,
            tables_at=empty,
            scorers_at=empty,
            stale_tables=frozenset(),
            stale_scorers=frozenset(),
            invalid_shortcuts=self._invalid,
            ended_season=empty,
        )
