"""Datenklassen für Spiele, Tabellen und Torjäger (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Phase(StrEnum):
    """Spielphase, in der das Ergebnis feststand."""

    REGULAR = "regulaer"
    EXTRA_TIME = "verlaengerung"
    PENALTIES = "elfmeterschiessen"


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    short_name: str
    tla: str | None


@dataclass(frozen=True, slots=True)
class Goal:
    minute: int | None
    scorer: str | None
    team_id: int | None
    home: int  # Stand nach dem Tor
    away: int
    penalty: bool
    own_goal: bool
    shootout: bool


@dataclass(frozen=True, slots=True)
class Score:
    home: int
    away: int


@dataclass(frozen=True, slots=True)
class Match:
    id: int
    shortcut: str
    season: int
    round_name: str
    round_order: int
    kickoff: datetime  # immer mit Zeitzone UTC
    home: Team
    away: Team
    finished: bool
    final: Score | None
    final_phase: Phase | None
    regular: Score | None
    halftime: Score | None
    result_unclear: bool
    goals: tuple[Goal, ...]  # nach Spielstand geordnet, ohne Elfmeterschießen
    last_update: datetime | None  # mit Zeitzone UTC


@dataclass(frozen=True, slots=True)
class TableRow:
    position: int
    team: Team
    matches: int
    points: int
    goals_for: int
    goals_against: int
    goal_diff: int
    won: int
    draw: int
    lost: int


@dataclass(frozen=True, slots=True)
class Scorer:
    name: str
    goals: int
