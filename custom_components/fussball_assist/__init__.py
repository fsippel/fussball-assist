"""Die Integration Fußball-Assist."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .api import OpenLigaClient, async_get_client
from .const import (
    CONF_COMPETITIONS,
    CONF_FAVORITE_TEAMS,
    CONF_MAIN_TEAM,
    DOMAIN,
    ISSUE_SEASON_MISSING,
    ISSUE_SHORTCUT_UNKNOWN,
)
from .logic_woche import load_derbies
from .results import Settings, season_label
from .seasons import COMPETITION_KEYS, Season, load_season, season_for
from .store import DataStore
from .teams import load_aliases

_LOGGER = logging.getLogger(__name__)

# Uhrzeit der täglichen Prüfung auf Saisonwechsel und Kürzel (Ortszeit von Home Assistant).
DAILY_CHECK_HOUR = 4


@dataclass
class RuntimeData:
    """Laufzeitdaten des Eintrags."""

    client: OpenLigaClient
    store: DataStore | None  # None, wenn die Saisondatei fehlt
    settings: Settings
    derbies: tuple[tuple[int, int, str], ...]
    aliases: Mapping[str, tuple[str, ...]]


type FussballConfigEntry = ConfigEntry[RuntimeData]


def _parse_id(value: object) -> int | None:
    """Kennung aus Text (oder Zahl); None bei allem, was keine ganze Zahl ist."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def settings_from_entry(entry: FussballConfigEntry) -> Settings:
    """Einstellungen aus den Optionen: Kennungen aus Text zu Zahlen, fehlende Wettbewerbe = alle.

    Ungültige Kennungen entfallen (mit Protokoll), unbekannte Wettbewerbe ebenso.
    """
    options = entry.options
    selected = options.get(CONF_COMPETITIONS)
    if selected is None:
        competitions = COMPETITION_KEYS
    elif not isinstance(selected, (list, tuple, set)) or not selected:
        _LOGGER.warning("Keine gültige Auswahl der Wettbewerbe in den Optionen, verwende alle")
        competitions = COMPETITION_KEYS
    else:
        wanted = set(selected)
        for key in sorted(wanted - set(COMPETITION_KEYS), key=str):
            _LOGGER.warning("Unbekannter Wettbewerb %r in den Optionen übergangen", key)
        competitions = tuple(key for key in COMPETITION_KEYS if key in wanted)

    main_team = None
    raw_main = options.get(CONF_MAIN_TEAM)
    if raw_main not in (None, ""):
        main_team = _parse_id(raw_main)
        if main_team is None:
            _LOGGER.warning("Hauptverein %r ist keine gültige Kennung und wird übergangen", raw_main)

    favorites: list[int] = []
    raw_favorites = options.get(CONF_FAVORITE_TEAMS)
    for raw in raw_favorites if isinstance(raw_favorites, (list, tuple)) else ():
        team = _parse_id(raw)
        if team is None:
            if raw not in (None, ""):
                _LOGGER.warning("Lieblingsverein %r ist keine gültige Kennung und wird übergangen", raw)
        elif team not in favorites:
            favorites.append(team)
    return Settings(competitions=competitions, main_team=main_team, favorite_teams=tuple(favorites))


def _load_data(
    year: int,
) -> tuple[Season | None, Season | None, Mapping[str, tuple[str, ...]], tuple[tuple[int, int, str], ...]]:
    """Liest die Datendateien (Dateizugriff, daher im Executor)."""
    return load_season(year), load_season(year - 1), load_aliases(), load_derbies()


@callback
def _sync_shortcut_issue(hass: HomeAssistant, season: Season, invalid: frozenset[str]) -> None:
    """Meldung zu Kürzeln ohne Eintrag in der Ligaliste anlegen oder löschen (Rückruf des Datenbestands)."""
    if invalid:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SHORTCUT_UNKNOWN,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SHORTCUT_UNKNOWN,
            translation_placeholders={"kuerzel": ", ".join(sorted(invalid)), "saison": season.label},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SHORTCUT_UNKNOWN)


async def _warm_up(hass: HomeAssistant, store: DataStore) -> None:
    """Lädt im Hintergrund; die Meldung zu Kürzeln kommt über den Rückruf. Wirft nie (außer beim Abbrechen)."""
    try:
        await store.async_warm_up()
    except Exception:
        _LOGGER.exception("Laden im Hintergrund fehlgeschlagen")


async def async_setup_entry(hass: HomeAssistant, entry: FussballConfigEntry) -> bool:
    """Richtet einen Eintrag ein; das Netz wird erst im Hintergrund gebraucht."""
    client = await async_get_client(hass)
    year = season_for(dt_util.now().date())
    season, previous, aliases, derbies = await hass.async_add_executor_job(_load_data, year)

    store: DataStore | None = None
    settings = settings_from_entry(entry)
    if season is None:
        _LOGGER.warning("Für die Saison %s gibt es keine Saisondatei; ein Update der Integration ist nötig", season_label(year))
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SEASON_MISSING,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SEASON_MISSING,
            translation_placeholders={"saison": season_label(year)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SEASON_MISSING)
        store = DataStore(
            client,
            season,
            settings.competitions,
            previous=previous,
            utcnow=dt_util.utcnow,
            tz=dt_util.get_default_time_zone(),
            on_leagues_checked=lambda invalid: _sync_shortcut_issue(hass, season, invalid),
        )
    entry.runtime_data = RuntimeData(
        client=client, store=store, settings=settings, derbies=derbies, aliases=aliases
    )

    if store is not None:
        entry.async_create_background_task(hass, _warm_up(hass, store), "fussball_assist_warm_up")

    @callback
    def _daily_check(now: datetime) -> None:
        # Eine Liga, die OpenLigaDB erst später anlegt, wird mit der nächsten Frage neu geprüft.
        if store is not None and store.invalid_shortcuts:
            store.recheck_leagues()
        if season_for(now.date()) != year:
            _LOGGER.info("Neue Saison %s, lade den Eintrag neu", season_label(season_for(now.date())))
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_track_time_change(hass, _daily_check, hour=DAILY_CHECK_HOUR, minute=0, second=0))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FussballConfigEntry) -> bool:
    """Entlädt einen Eintrag; die Hintergrundaufgabe und den Zeitgeber beendet Home Assistant."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SEASON_MISSING)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SHORTCUT_UNKNOWN)
    return True
