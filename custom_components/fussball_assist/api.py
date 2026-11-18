"""Abrufe bei OpenLigaDB mit Zeitlimit und Obergrenze je Minute."""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp.hdrs import USER_AGENT
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.hass_dict import HassKey

from . import const
from .parse import parse_local_datetime

_LOGGER = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


class OpenLigaError(Exception):
    """Quelle nicht erreichbar, Zeitlimit, HTTP-Fehler, kein JSON oder unerwartete Form."""


class RateLimited(OpenLigaError):
    """Eigene Obergrenze je Minute erreicht, es wurde nichts abgerufen."""


def _segment(value: str | int) -> str:
    return quote(str(value), safe="")


class OpenLigaClient:
    """Dünner Client für die Adressen von api.openligadb.de."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        version: str,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._headers = {USER_AGENT: f"fussball-assist/{version} (Home Assistant)"}
        self._now = now
        self._started: deque[float] = deque()

    def _prune(self) -> None:
        now = self._now()
        while self._started and now - self._started[0] >= WINDOW_SECONDS:
            self._started.popleft()

    def requests_left(self) -> int:
        """Freie Abrufe in der laufenden Minute."""
        self._prune()
        return max(0, const.MAX_REQUESTS_PER_MINUTE - len(self._started))

    async def wait_for_slot(self) -> None:
        """Schläft, bis wieder ein Abruf frei ist (für das Laden im Hintergrund)."""
        while self.requests_left() <= 0:
            if not self._started:
                return
            delay = self._started[0] + WINDOW_SECONDS - self._now()
            await asyncio.sleep(max(delay, 0.0) + 0.001)

    async def _get(self, path: str, *, allow_not_found: bool = False) -> str | None:
        """Ruft eine Adresse ab und gibt den Text zurück; None nur bei erlaubtem 404."""
        if self.requests_left() <= 0:
            raise RateLimited("Obergrenze an Abrufen je Minute erreicht")
        self._started.append(self._now())
        url = f"{const.API_BASE}{path}"
        try:
            async with asyncio.timeout(const.REQUEST_TIMEOUT):
                async with self._session.get(url, headers=self._headers) as response:
                    if allow_not_found and response.status == HTTPStatus.NOT_FOUND:
                        return None
                    if response.status >= HTTPStatus.BAD_REQUEST:
                        raise OpenLigaError(f"HTTP {response.status} bei {path}")
                    return await response.text()
        except TimeoutError as err:
            raise OpenLigaError(f"Zeitlimit bei {path}") from err
        except aiohttp.ClientError as err:
            raise OpenLigaError(f"Abruf von {path} fehlgeschlagen: {type(err).__name__}") from err

    async def _get_list(self, path: str) -> list[dict[str, Any]]:
        text = await self._get(path)
        try:
            data = json.loads(text or "")
        except ValueError as err:
            raise OpenLigaError(f"Antwort auf {path} ist kein JSON") from err
        if not isinstance(data, list):
            raise OpenLigaError(f"Antwort auf {path} ist keine Liste")
        return data

    async def leagues(self) -> list[dict[str, Any]]:
        return await self._get_list("/getavailableleagues")

    async def season_matches(self, shortcut: str, year: int) -> list[dict[str, Any]]:
        return await self._get_list(f"/getmatchdata/{_segment(shortcut)}/{_segment(year)}")

    async def round_matches(self, shortcut: str, year: int, order: int) -> list[dict[str, Any]]:
        return await self._get_list(f"/getmatchdata/{_segment(shortcut)}/{_segment(year)}/{_segment(order)}")

    async def table(self, shortcut: str, year: int) -> list[dict[str, Any]]:
        return await self._get_list(f"/getbltable/{_segment(shortcut)}/{_segment(year)}")

    async def scorers(self, shortcut: str, year: int) -> list[dict[str, Any]]:
        return await self._get_list(f"/getgoalgetters/{_segment(shortcut)}/{_segment(year)}")

    async def teams(self, shortcut: str, year: int) -> list[dict[str, Any]]:
        return await self._get_list(f"/getavailableteams/{_segment(shortcut)}/{_segment(year)}")

    async def last_change(self, shortcut: str, year: int, order: int) -> datetime | None:
        """Zeitpunkt der letzten Änderung des Spieltags; None bei 404 oder unlesbarer Antwort."""
        path = f"/getlastchangedate/{_segment(shortcut)}/{_segment(year)}/{_segment(order)}"
        text = await self._get(path, allow_not_found=True)
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            _LOGGER.debug("Änderungsabfrage %s lieferte kein JSON", path)
            return None
        if not isinstance(value, str):
            return None
        return parse_local_datetime(value)


CLIENT_KEY: HassKey[OpenLigaClient] = HassKey(f"{const.DOMAIN}_client")


async def async_get_client(hass: HomeAssistant) -> OpenLigaClient:
    """Der einzige Client je Home Assistant (Einrichtung, Optionen und Datenbestand teilen sich die Obergrenze).

    Er wird beim ersten Aufruf angelegt und bleibt über Entladen und Neuladen des Eintrags stehen.
    """
    if (client := hass.data.get(CLIENT_KEY)) is not None:
        return client
    integration = await loader.async_get_integration(hass, const.DOMAIN)
    # Während des Wartens kann ein anderer Aufruf den Client angelegt haben.
    return hass.data.setdefault(
        CLIENT_KEY,
        OpenLigaClient(async_get_clientsession(hass), str(integration.version or "unbekannt")),
    )
