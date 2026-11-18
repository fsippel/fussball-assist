"""Einrichtung und Optionen der Integration Fußball-Assist: Wettbewerbe, Hauptverein, Lieblingsvereine."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryBaseFlow,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import OpenLigaError, async_get_client
from .const import CONF_COMPETITIONS, CONF_FAVORITE_TEAMS, CONF_MAIN_TEAM, DOMAIN
from .seasons import COMPETITION_KEYS, Kind, Season, load_season, season_for
from .teams import normalize

_LOGGER = logging.getLogger(__name__)

ERROR_NO_COMPETITIONS = "keine_wettbewerbe"
ERROR_UNREACHABLE = "nicht_erreichbar"
ABORT_SEASON_MISSING = "saison_fehlt"


def _text_ids(value: object) -> list[str]:
    """Kennungen aus den Optionen als Text; alles, was keine Liste ist, ergibt eine leere Liste."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if isinstance(item, (str, int)) and not isinstance(item, bool)]


class _ClubFlowMixin(ConfigEntryBaseFlow):
    """Gemeinsame Schritte von Einrichtung und Optionen: erst Wettbewerbe, dann Vereine.

    Die Unterklassen nennen die Schritt-Kennung des ersten Schritts und legen am Ende den Eintrag an.
    """

    _first_step_id = "user"
    _season: Season | None = None
    _competitions: tuple[str, ...] = ()
    _club_options: tuple[SelectOptionDict, ...] = ()
    _club_ids: Mapping[str, str] = {}  # jede gesehene Kennung -> Kennung in der Auswahl (bei Doppelungen)

    def _suggested_values(self) -> Mapping[str, Any]:
        """Vorbelegung der Felder (in den Optionen die bisherigen Werte)."""
        return {}

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError

    async def _async_load_season(self) -> Season | None:
        if self._season is None:
            year = season_for(dt_util.now().date())
            self._season = await self.hass.async_add_executor_job(load_season, year)
        return self._season

    async def _async_load_clubs(self, season: Season, selected: Sequence[str]) -> None:
        """Vereinsliste der gewählten Wettbewerbe (Männer, ohne Nationalmannschaft); wirft OpenLigaError."""
        client = await async_get_client(self.hass)
        shortcuts: list[str] = []
        for key in COMPETITION_KEYS:  # feste Reihenfolge: bei zwei Kennungen gilt die des ersten Wettbewerbs
            competition = season.competitions.get(key)
            if key not in selected or competition is None:
                continue
            if competition.women or competition.kind is Kind.NATIONAL:
                continue
            shortcuts.extend(s for s in competition.shortcuts if s not in shortcuts)

        options: list[SelectOptionDict] = []
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for shortcut in shortcuts:
            await client.wait_for_slot()
            for team in await client.teams(shortcut, season.year):
                if not isinstance(team, dict):
                    continue
                team_id, name = team.get("teamId"), team.get("teamName")
                if not isinstance(team_id, int) or isinstance(team_id, bool):
                    continue
                if not isinstance(name, str) or not name.strip():
                    continue
                value = str(team_id)
                if value in by_id:
                    continue
                key = normalize(name) or value
                kept = by_name.setdefault(key, value)
                by_id[value] = kept
                if kept == value:
                    options.append(SelectOptionDict(value=value, label=name.strip()))
        self._club_options = tuple(options)
        self._club_ids = by_id

    def _ordered(self, season: Season, chosen: Iterable[object]) -> tuple[str, ...]:
        wanted = set(chosen)
        return tuple(key for key in COMPETITION_KEYS if key in wanted and key in season.competitions)

    async def _async_competitions_step(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        season = await self._async_load_season()
        if season is None:
            return self.async_abort(reason=ABORT_SEASON_MISSING)
        suggested = self._suggested_values().get(CONF_COMPETITIONS)
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input.get(CONF_COMPETITIONS) or []
            suggested = chosen
            selected = self._ordered(season, chosen)
            if not selected:
                errors["base"] = ERROR_NO_COMPETITIONS
            else:
                try:
                    await self._async_load_clubs(season, selected)
                except OpenLigaError as err:
                    _LOGGER.debug("Vereinsliste nicht abrufbar: %s", err)
                    errors["base"] = ERROR_UNREACHABLE
                except Exception:
                    _LOGGER.exception("Unerwarteter Fehler beim Laden der Vereinslisten")
                    errors["base"] = ERROR_UNREACHABLE
                else:
                    self._competitions = selected
                    return await self.async_step_vereine()
        options = [
            SelectOptionDict(value=key, label=season.competitions[key].name)
            for key in COMPETITION_KEYS
            if key in season.competitions
        ]
        schema = probatio.Schema(
            {
                probatio.Optional(CONF_COMPETITIONS): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True, sort=False)
                )
            }
        )
        valid = {option["value"] for option in options}
        shown = [key for key in _text_ids(suggested) if key in valid]
        if user_input is None and not shown:  # nichts oder nichts Gültiges gespeichert: wie zur Laufzeit alle
            shown = [key for key in COMPETITION_KEYS if key in valid]
        return self.async_show_form(
            step_id=self._first_step_id,
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_COMPETITIONS: shown}),
            errors=errors,
        )

    async def async_step_vereine(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Hauptverein und weitere Lieblingsvereine aus den Vereinen der gewählten Wettbewerbe."""
        if user_input is not None:
            options: dict[str, Any] = {CONF_COMPETITIONS: list(self._competitions)}
            main_team = user_input.get(CONF_MAIN_TEAM)
            if main_team not in (None, ""):
                options[CONF_MAIN_TEAM] = str(main_team)
            options[CONF_FAVORITE_TEAMS] = _text_ids(user_input.get(CONF_FAVORITE_TEAMS))
            return self._finish(options)

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_MAIN_TEAM): SelectSelector(
                    SelectSelectorConfig(options=list(self._club_options), multiple=False, sort=True)
                ),
                probatio.Optional(CONF_FAVORITE_TEAMS): SelectSelector(
                    SelectSelectorConfig(options=list(self._club_options), multiple=True, sort=True)
                ),
            }
        )
        stored = self._suggested_values()
        suggested: dict[str, Any] = {}
        valid = {option["value"] for option in self._club_options}
        main = self._club_ids.get(str(stored.get(CONF_MAIN_TEAM, "")).strip())
        if main in valid:
            suggested[CONF_MAIN_TEAM] = main
        favorites = [self._club_ids.get(raw) for raw in _text_ids(stored.get(CONF_FAVORITE_TEAMS))]
        favorites = [team for index, team in enumerate(favorites) if team in valid and team not in favorites[:index]]
        if favorites:
            suggested[CONF_FAVORITE_TEAMS] = favorites
        return self.async_show_form(
            step_id="vereine",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
        )


class FussballAssistConfigFlow(_ClubFlowMixin, ConfigFlow, domain=DOMAIN):
    """Einrichtung: Wettbewerbe, dann Hauptverein und Lieblingsvereine."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FussballAssistOptionsFlow:
        return FussballAssistOptionsFlow()

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="Fußball-Assist", data={}, options=options)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_competitions_step(user_input)


class FussballAssistOptionsFlow(_ClubFlowMixin, OptionsFlowWithReload):
    """Optionen: dieselben Schritte, vorbelegt mit den bisherigen Werten; lädt den Eintrag neu."""

    _first_step_id = "init"

    def _suggested_values(self) -> Mapping[str, Any]:
        return self.config_entry.options

    def _finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_competitions_step(user_input)
