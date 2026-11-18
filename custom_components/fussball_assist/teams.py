"""Vereinsnamen, Spitznamen und die Nationalmannschaft auflösen (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from .models import Match, Team
from .seasons import DATA_DIR, Kind, Season

NATIONAL_TEAM = "nationalmannschaft"  # Kennzeichen statt eines Vereins

_LOGGER = logging.getLogger(__name__)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe"})
_ARTICLES = ("der ", "die ", "das ", "den ")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")

# Erkennung des Begriffs "Nationalmannschaft" (nicht eines Vereins), schon normalisiert.
_NATIONAL_PHRASES = frozenset(
    {
        "deutschland",
        "nationalmannschaft",
        "deutsche nationalmannschaft",
        "nationalelf",
        "nationalteam",
        "dfb elf",
        "dfb team",
        "dfb auswahl",
        "dfb nationalmannschaft",
        "mannschaft",
    }
)


def normalize(text: str) -> str:
    """Klein, ä→ae, ö→oe, ü→ue, ß→ss, æ→ae, ø→oe, Satzzeichen weg, Artikel am Anfang weg; Ziffern bleiben."""
    if not isinstance(text, str):
        return ""
    value = unicodedata.normalize("NFC", text).casefold().strip().translate(_UMLAUTS)
    value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    value = value.replace("'", "").replace("’", "")
    value = _NOT_ALNUM.sub(" ", value).strip()
    for article in _ARTICLES:
        if value.startswith(article) and value[len(article) :].strip():
            return value[len(article) :].strip()
    return value


@dataclass(frozen=True, slots=True)
class Resolution:
    team: Team | None  # gefunden
    national: bool  # Nationalmannschaft gemeint
    candidates: tuple[Team, ...]  # mehrdeutig, sonst leer
    foreign: bool  # nur aus einem Europapokal bekannt


_NOT_FOUND = Resolution(None, False, (), False)


def load_aliases() -> Mapping[str, tuple[str, ...]]:
    """Liest data/spitznamen.json (Schlüssel = teamId als Text). Fehlende oder kaputte Datei ergibt {}."""
    path = DATA_DIR / "spitznamen.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LOGGER.warning("Spitznamen %s nicht gefunden", path)
        return MappingProxyType({})
    except (OSError, ValueError) as err:
        _LOGGER.warning("Spitznamen %s sind nicht lesbar: %s: %s", path, type(err).__name__, err)
        return MappingProxyType({})
    if not isinstance(raw, dict):
        _LOGGER.warning("Spitznamen %s sind ungültig: Objekt erwartet", path)
        return MappingProxyType({})
    aliases: dict[str, tuple[str, ...]] = {}
    for key, names in raw.items():
        if isinstance(names, list) and names and all(isinstance(n, str) and n for n in names):
            aliases[str(key)] = tuple(names)
        else:
            _LOGGER.warning("Spitznamen für %r in %s ungültig und übergangen", key, path)
    return MappingProxyType(aliases)


class TeamIndex:
    """Findet Vereine über Namen, Kurznamen, Kürzel und Spitznamen.

    Ein Verein kann in mehreren Wettbewerben unter verschiedenen Kennungen auftreten
    (z. B. Liga und Pokal). Teams mit gleichem normalisiertem Namen gelten als ein Verein;
    kanonisch ist das zuerst gefundene Team.
    """

    def __init__(
        self,
        teams: Mapping[int, Team],
        variants: Mapping[int, frozenset[int]],
        foreign: frozenset[int],
        exact: Mapping[str, frozenset[int]],
        words: Mapping[int, frozenset[str]],
    ):
        self._teams = teams  # kanonische Kennung -> kanonisches Team
        self._variants = variants  # kanonische Kennung -> alle Kennungen des Vereins
        self._canonical = {i: c for c, ids in variants.items() for i in ids}
        self._foreign = foreign  # kanonische Kennungen
        self._exact = exact
        self._words = words

    @classmethod
    def build(
        cls,
        matches: Mapping[str, Sequence[Match]],
        season: Season,
        aliases: Mapping[str, Sequence[str]],
    ) -> TeamIndex:
        """Frauen-Wettbewerbe und Nationalmannschaften kommen nicht in den Index."""
        teams: list[Team] = []
        domestic: set[int] = set()
        for key, competition in season.competitions.items():  # feste Reihenfolge, Ligen vor Europa
            if competition.women or competition.kind is Kind.NATIONAL:
                continue
            for match in matches.get(key, ()):
                for team in (match.home, match.away):
                    teams.append(team)
                    if competition.kind is not Kind.EUROPE:
                        domestic.add(team.id)
        return cls._from_teams(teams, domestic, aliases)

    @classmethod
    def _from_teams(
        cls, found: Iterable[Team], domestic: set[int], aliases: Mapping[str, Sequence[str]]
    ) -> TeamIndex:
        by_id: dict[int, Team] = {}
        for team in found:
            if team.id not in by_id:
                clean = team.name.strip()
                by_id[team.id] = replace(team, name=clean) if clean != team.name else team
        canonical_of_name: dict[str, int] = {}
        canonical_of: dict[int, int] = {}
        for team_id, team in by_id.items():  # Einfügereihenfolge = Reihenfolge der Wettbewerbe
            canonical_of[team_id] = canonical_of_name.setdefault(normalize(team.name) or str(team_id), team_id)
        teams = {c: by_id[c] for c in set(canonical_of.values())}
        variants: dict[int, set[int]] = {c: set() for c in teams}
        for team_id, canon in canonical_of.items():
            variants[canon].add(team_id)

        exact: dict[str, set[int]] = {}
        words: dict[int, set[str]] = {c: set() for c in teams}

        def add(text: str | None, canon: int) -> None:
            key = normalize(text or "")
            if key:
                exact.setdefault(key, set()).add(canon)

        for team_id, team in by_id.items():
            canon = canonical_of[team_id]
            for text in (team.name, team.short_name, team.tla):
                add(text, canon)
            for text in (team.name, team.short_name):
                words[canon].update(normalize(text).split())
        for raw_id, names in aliases.items():
            try:
                canon = canonical_of.get(int(raw_id))
            except (TypeError, ValueError):
                continue
            if canon is not None:
                for name in names:
                    add(name, canon)
        return cls(
            MappingProxyType(teams),
            {c: frozenset(ids) for c, ids in variants.items()},
            frozenset(c for c, ids in variants.items() if not ids & domestic),
            {key: frozenset(ids) for key, ids in exact.items()},
            {c: frozenset(w) for c, w in words.items()},
        )

    def by_id(self, team_id: int) -> Team | None:
        """Der Verein zu einer beliebigen seiner Kennungen (immer das kanonische Team)."""
        canon = self._canonical.get(team_id)
        return self._teams[canon] if canon is not None else None

    def ids_of(self, team: Team) -> frozenset[int]:
        """Alle Kennungen, unter denen dieser Verein vorkommt (leer, wenn unbekannt)."""
        canon = self._canonical.get(team.id)
        return self._variants[canon] if canon is not None else frozenset()

    def selectable(self) -> tuple[Team, ...]:
        """Alle Vereine (jeder einmal) für den Einrichtungsdialog, nach Namen sortiert."""
        return tuple(sorted(self._teams.values(), key=lambda t: (t.name, t.id)))

    def resolve(self, text: str) -> Resolution:
        """Löst einen Namen auf. Unbekannte oder leere Eingabe ergibt ein leeres Ergebnis, nie eine Ausnahme."""
        key = normalize(text)
        if not key:
            return _NOT_FOUND
        if key in _NATIONAL_PHRASES:
            return Resolution(None, True, (), False)
        ids = self._exact.get(key)
        if not ids:
            wanted = set(key.split())
            ids = frozenset(team_id for team_id, words in self._words.items() if wanted <= words)
        if not ids:
            return _NOT_FOUND
        found = sorted((self._teams[i] for i in ids), key=lambda t: (t.name, t.id))
        if len(found) > 1:
            return Resolution(None, False, tuple(found), False)
        return Resolution(found[0], False, (), found[0].id in self._foreign)
