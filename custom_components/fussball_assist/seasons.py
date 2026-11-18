"""Saison, Wettbewerbe und Tabellenzonen aus einer Datendatei (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

COMPETITION_KEYS = (
    "bundesliga",
    "2_bundesliga",
    "3_liga",
    "dfb_pokal",
    "champions_league",
    "europa_league",
    "conference_league",
    "nationalmannschaft",
    "frauen_bundesliga",
)

DATA_DIR = Path(__file__).parent / "data"

_LOGGER = logging.getLogger(__name__)


class Kind(StrEnum):
    """Art des Wettbewerbs."""

    LEAGUE = "liga"
    CUP = "pokal"
    EUROPE = "europa"
    NATIONAL = "nationalteam"


@dataclass(frozen=True, slots=True)
class Zone:
    """Tabellenplätze first bis last (einschließlich) mit einer Bezeichnung."""

    label: str
    first: int
    last: int


@dataclass(frozen=True, slots=True)
class Competition:
    key: str
    name: str
    kind: Kind
    shortcuts: tuple[str, ...]  # Tabelle und Torjägerliste kommen vom ersten Kürzel
    has_table: bool
    has_scorers: bool
    scorers_incomplete: bool
    women: bool
    zones: tuple[Zone, ...]
    league_phase_rounds: int | None  # nur EUROPE
    national_team_ids: Mapping[str, int]  # nur NATIONAL: Kürzel -> Kennung


@dataclass(frozen=True, slots=True)
class Season:
    year: int
    label: str  # z. B. "2026/27"
    competitions: Mapping[str, Competition]  # in der Reihenfolge von COMPETITION_KEYS


def season_for(day: date) -> int:
    """Juli bis Dezember gehören zur Saison des Jahres, Januar bis Juni zu der des Vorjahres."""
    return day.year if day.month >= 7 else day.year - 1


def zone_of(competition: Competition, position: int) -> str | None:
    """Bezeichnung der Zone, in der der Tabellenplatz liegt, sonst None."""
    for zone in competition.zones:
        if zone.first <= position <= zone.last:
            return zone.label
    return None


def load_season(year: int) -> Season | None:
    """Liest data/saison_<year>.json. Fehlende oder ungültige Datei ergibt None, nie eine Ausnahme.

    Die Funktion liest eine Datei; in Home Assistant gehört der Aufruf in den Executor.
    """
    if not _is_int(year) or year < 0:
        return None
    path = DATA_DIR / f"saison_{year}.json"
    try:
        return _build_season(json.loads(path.read_text(encoding="utf-8")), year)
    except FileNotFoundError:
        return None  # keine Datei für diese Saison: kein Fehler, nur "noch nicht vorhanden"
    except (OSError, ValueError, KeyError, TypeError) as err:
        _LOGGER.warning("Saisondatei %s ist nicht lesbar oder ungültig: %s: %s", path, type(err).__name__, err)
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Text erwartet")
    return value


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Wahrheitswert erwartet")
    return value


def _build_zones(raw: Any) -> tuple[Zone, ...]:
    if not isinstance(raw, list):
        raise ValueError("Liste von Zonen erwartet")
    zones = []
    for item in raw:
        zone = Zone(_text(item["label"]), item["first"], item["last"])
        if not (_is_int(zone.first) and _is_int(zone.last) and 1 <= zone.first <= zone.last):
            raise ValueError("ungültige Platzgrenzen")
        zones.append(zone)
    ordered = sorted(zones, key=lambda z: z.first)
    if any(a.last >= b.first for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Zonen überschneiden sich")
    return tuple(zones)


def _build_competition(key: str, raw: Any) -> Competition:
    shortcuts = raw["shortcuts"]
    if not isinstance(shortcuts, list) or not shortcuts:
        raise ValueError("Kürzel erwartet")
    rounds = raw["league_phase_rounds"]
    if rounds is not None and not (_is_int(rounds) and rounds > 0):
        raise ValueError("Zahl der Runden erwartet")
    ids = raw["national_team_ids"]
    if not isinstance(ids, dict) or not all(_is_int(v) for v in ids.values()):
        raise ValueError("Kennungen erwartet")
    return Competition(
        key=key,
        name=_text(raw["name"]),
        kind=Kind(raw["kind"]),
        shortcuts=tuple(_text(s) for s in shortcuts),
        has_table=_flag(raw["has_table"]),
        has_scorers=_flag(raw["has_scorers"]),
        scorers_incomplete=_flag(raw["scorers_incomplete"]),
        women=_flag(raw["women"]),
        zones=_build_zones(raw["zones"]),
        league_phase_rounds=rounds,
        national_team_ids=MappingProxyType({_text(k): v for k, v in ids.items()}),
    )


def _build_season(raw: Any, year: int) -> Season:
    if not isinstance(raw, dict) or raw["year"] != year:
        raise ValueError("Jahr passt nicht zur Datei")
    competitions = raw["competitions"]
    if not isinstance(competitions, dict):
        raise ValueError("Wettbewerbe erwartet")
    return Season(
        year=year,
        label=_text(raw["label"]),
        competitions=MappingProxyType(
            {key: _build_competition(key, competitions[key]) for key in COMPETITION_KEYS}
        ),
    )
