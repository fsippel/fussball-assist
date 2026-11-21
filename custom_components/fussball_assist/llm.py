"""Die fünf Fußball-Werkzeuge und der Hinweistext für die Assist-API von Home Assistant.

Home Assistant prüft die Argumente eines Werkzeugs nicht. Das Schema beschreibt dem LLM die Parameter;
geprüft wird von Hand, damit der Fehlertext die gültigen Werte und die Vorgabe nennen kann. Ein Werkzeug
wirft nie: Jeder Fehler kommt als Ergebnis mit Fehlerkennzeichen zurück, der Satz steht in den Daten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import re
from typing import Any

import probatio

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.util.json import JsonObjectType

from . import RuntimeData, logic_spiele, logic_tabelle, logic_torjaeger, logic_verein, logic_woche
from .const import DOMAIN, TOOL_PREFIX
from .models import Team
from .results import DEFAULT_LEAGUE, ErrorKind, ToolError, error_payload, given, loading_error, searchable
from .seasons import Season
from .store import DataStore, Snapshot
from .teams import TeamIndex, normalize

_LOGGER = logging.getLogger(__name__)

PROMPT = (
    "Für aktuelle Fußballfragen (Ergebnisse, Spieltermine, Tabellen, Torjäger, wichtigste Spiele) nutze immer"
    " die Fußball-Werkzeuge und antworte nur mit dem, was sie liefern. Liefern sie nichts oder einen Hinweis,"
    " sag das ehrlich und rate nicht. Zu Kadern, Trainern, Transfers, Aufstellungen, Karten, Schiedsrichtern,"
    " Zuschauerzahlen, dem Spielort eines bestimmten Spiels, Fernsehübertragungen, Tickets, Spielabsagen,"
    " Auslosungsterminen, Finalterminen und Finalorten und zu aktuellen Titelträgern hast du keine aktuellen"
    " Daten: Sag, dass du das nicht weißt. Sag auch dann nicht, bei welchem Verein ein Spieler spielt, wer"
    " Trainer ist oder wer im Kader steht, wenn du es zu wissen glaubst. Gib keine Prognosen, Wahrscheinlichkeiten oder Tipps ab. Nenne eine"
    " Uhrzeit nur, wenn das Werkzeug sie als feststehend liefert. Steht vor einem Spielernamen nur ein"
    " Anfangsbuchstabe, nenne nur den Nachnamen und ergänze keinen Vornamen. Nennst du den Stand eines laufenden"
    " Spiels, sag immer dazu, von wann er ist und dass er ein paar Minuten hinterherhängen kann. Sag bei laufenden"
    " Spielen nichts über Spielminute oder Halbzeit; das liefern die Werkzeuge nicht."
)

VEREIN, SPIELE, TABELLE, TORJAEGER, WOCHE = (
    f"{TOOL_PREFIX}{name}" for name in ("verein", "spiele", "tabelle", "torjaeger", "woche")
)
TOOL_NAMES: tuple[str, ...] = (VEREIN, SPIELE, TABELLE, TORJAEGER, WOCHE)

UNAVAILABLE = "Das kann ich gerade nicht abrufen."
SEASON_MISSING = "Für die neue Saison fehlen noch Daten, deshalb kann ich dazu gerade nichts sagen."

_TRUE = frozenset({"ja", "true", "wahr", "1"})
_FALSE = frozenset({"nein", "false", "falsch", "0"})
_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]+")

# Vorgaben, die das Werkzeug setzt (und die Beschreibungen nennen).
DEFAULT_DIRECTION = "beides"
DEFAULT_CLUB_COUNT = logic_verein.MIN_COUNT
DEFAULT_COMPETITION = logic_spiele.ALL
DEFAULT_PERIOD = "heute"
DEFAULT_SECTION = "spitze"
DEFAULT_SCORER_COUNT = 5
DEFAULT_WEEK = "diese"


# --- Auswahlwerte und Parameter ---


@dataclass(frozen=True, slots=True)
class _Choices:
    """Gültige Wettbewerbe je Werkzeug, aus den Einstellungen und der Saisondatei (ohne Abruf)."""

    chosen: tuple[str, ...]  # alle gewählten
    clubs: tuple[str, ...]  # für das Vereinswerkzeug: ohne Frauen
    tables: tuple[str, ...]  # mit Tabelle
    scorers: tuple[str, ...]  # mit Torjägerliste

    @classmethod
    def build(cls, runtime: RuntimeData) -> _Choices:
        wanted = runtime.settings.competitions
        season: Season | None = runtime.store.season if runtime.store is not None else None
        if season is None:  # ohne Saisondatei antworten die Werkzeuge ohnehin mit "saison_fehlt"
            return cls(wanted, wanted, wanted, wanted)
        competitions = season.competitions
        chosen = tuple(k for k in competitions if k in wanted)
        return cls(
            chosen=chosen,
            clubs=tuple(k for k in chosen if not competitions[k].women),
            tables=tuple(k for k in chosen if competitions[k].has_table),
            scorers=tuple(k for k in chosen if competitions[k].has_scorers),
        )


@dataclass(frozen=True, slots=True)
class _Param:
    """Ein freiwilliger Parameter; `kind` ist "text", "choice", "number" oder "flag"."""

    name: str
    kind: str
    description: str
    choices: tuple[str, ...] = ()  # nur "choice": gültige Werte, von Hand geprüft
    describe_only: tuple[str, ...] = ()  # Werte für das Schema, geprüft von der Antwort (Wettbewerbe)
    bounds: tuple[int, int] = (0, 0)  # nur "number"
    default: Any = None

    def validator(self) -> Any:
        if self.kind == "number":
            return probatio.Coerce(int)
        if self.kind == "flag":
            return cv.boolean
        values = self.choices or self.describe_only
        if self.kind == "choice" and values:
            return probatio.In(list(values))
        return cv.string


def _schema(params: Sequence[_Param]) -> probatio.Schema:
    return probatio.Schema({probatio.Optional(p.name, description=p.description): p.validator() for p in params})


def _valid(values: Sequence[str]) -> str:
    return ", ".join(values)


def _invalid(text: str) -> ToolError:
    return ToolError(ErrorKind.INVALID_INPUT, text)


def _as_text(param: _Param, value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if param.kind == "choice" and param.choices:
        raise _invalid(
            f"Für „{param.name}“ geht „{value}“ nicht. Gültig sind: {_valid(param.choices)}. Vorgabe: {param.default}."
        )
    raise _invalid(f"„{param.name}“ muss ein Text sein, zum Beispiel ein Name.")


def _as_number(param: _Param, value: Any) -> int:
    number: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and _WHOLE_NUMBER.fullmatch(value.strip()):
        number = int(value.strip())
    low, high = param.bounds
    if number is None or not low <= number <= high:
        raise _invalid(
            f"Für „{param.name}“ geht „{value}“ nicht. Gültig sind ganze Zahlen von {low} bis {high}."
            f" Vorgabe: {param.default}."
        )
    return number


def _as_flag(param: _Param, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise _invalid(
        f"Für „{param.name}“ geht „{value}“ nicht. Gültig sind ja oder nein (true oder false). Vorgabe: nein (false)."
    )


def _read_args(params: Sequence[_Param], raw: Any) -> dict[str, Any]:
    """Säubert und prüft die Argumente; fehlende Werte bekommen die Vorgabe. Wirft nur `ToolError`."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise _invalid(f"Die Angaben müssen benannte Parameter sein. Gültig sind: {_valid([p.name for p in params])}.")
    by_name = {p.name: p for p in params}
    cleaned: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if given(value) is None:  # "" und None gelten als nicht angegeben
            continue
        name = str(key).strip().casefold()
        if name not in by_name:
            unknown.append(str(key))
            continue
        cleaned[name] = value
    if unknown:
        what = "Den Parameter" if len(unknown) == 1 else "Die Parameter"
        raise _invalid(
            f"{what} „{'“, „'.join(unknown)}“ gibt es bei diesem Werkzeug nicht."
            f" Gültig sind: {_valid(list(by_name))}."
        )
    args: dict[str, Any] = {}
    for param in params:
        value = cleaned.get(param.name)
        if value is None:
            args[param.name] = param.default
        elif param.kind == "number":
            args[param.name] = _as_number(param, value)
        elif param.kind == "flag":
            args[param.name] = _as_flag(param, value)
        else:
            text = _as_text(param, value)
            if param.kind == "choice" and param.choices:
                if text.casefold() not in param.choices:
                    raise _invalid(
                        f"Für „{param.name}“ geht „{value}“ nicht. Gültig sind: {_valid(param.choices)}."
                        f" Vorgabe: {param.default}."
                    )
                text = text.casefold()
            args[param.name] = text
    return args


# --- Laufzeit ---


def _runtime(hass: HomeAssistant) -> RuntimeData | None:
    """Laufzeitdaten des geladenen Eintrags, ohne Abruf; None, wenn keiner geladen ist."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, RuntimeData):
            return runtime
    return None


def _requested(snapshot: Snapshot, runtime: RuntimeData) -> tuple[str, ...]:
    return tuple(k for k in snapshot.season.competitions if k in runtime.settings.competitions)


def _check_reachable(snapshot: Snapshot, runtime: RuntimeData) -> None:
    """Ist kein angeforderter Wettbewerb erreichbar und nichts zwischengespeichert, gilt der kurze Satz."""
    requested = _requested(snapshot, runtime)
    if requested and all(k in snapshot.unreachable for k in requested):
        raise ToolError(ErrorKind.UNREACHABLE, UNAVAILABLE)


def _is_exact(index: TeamIndex, aliases: Mapping[str, Sequence[str]], team: Team, text: str) -> bool:
    """Trifft der Text den Verein genau (Name, Kurzname, Kürzel, Spitzname) statt nur über ein Wort?"""
    key = normalize(text)
    names = [team.name, team.short_name, team.tla]
    for team_id in index.ids_of(team):
        names += aliases.get(str(team_id), ())
    return any(key == normalize(name or "") for name in names)


def _wait_for_partial_names(
    snapshot: Snapshot, index: TeamIndex, runtime: RuntimeData, texts: Sequence[str | None]
) -> None:
    """Solange Vereinswettbewerbe noch laden, könnte ein Wortteil ("Eintracht") nur zufällig eindeutig sein.

    Dann gilt der Ladefehler statt der Antwort für den einzigen bisher geladenen Verein. Genaue Namen und
    Spitznamen gelten weiter. Nicht erreichbare oder ungültige Wettbewerbe bleiben außen vor: Sie sind kein
    vorübergehender Zustand, und die Antwort nennt sie in ihren Hinweisen.
    """
    waiting = [
        k for k in searchable(snapshot, runtime.settings) if k not in snapshot.loaded and k not in snapshot.unreachable
    ]
    if not waiting:
        return
    for text in texts:
        if text is None:
            continue
        found = index.resolve(text)
        if found.team is None or _is_exact(index, runtime.aliases, found.team, text):
            continue
        raise loading_error(snapshot, waiting, f"„{text}“") or ToolError(
            ErrorKind.UNREACHABLE, "Es sind noch nicht alle Wettbewerbe geladen. Versuch es gleich noch einmal."
        )


# --- Werkzeuge ---


class _FootballTool(llm.Tool):
    """Gemeinsamer Ablauf: Eingaben prüfen, Laufzeitdaten holen, Momentaufnahme, Vereinsindex, Antwort."""

    integration = DOMAIN
    annotations = llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=True)

    def __init__(self, choices: _Choices) -> None:
        self._params = self._build_params(choices)
        self.parameters = _schema(self._params)
        self.description = self._build_description()

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        raise NotImplementedError

    def _build_description(self) -> str:
        raise NotImplementedError

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        raise NotImplementedError

    def _wanted(
        self, runtime: RuntimeData, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Die Wettbewerbe, deren Tabelle und Torjägerliste die Antwort braucht (aus den Spiellisten bestimmt)."""
        return (), ()

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        """Wirft nie; jeder Fehler ist ein Ergebnis mit Fehlerkennzeichen und Satz in den Daten."""
        try:
            data = await self._answer(hass, tool_input.tool_args)
        except ToolError as err:
            return llm.ToolResult(data=error_payload(err.kind, err.text), error=True)
        except Exception:
            _LOGGER.exception("Werkzeug %s fehlgeschlagen", self.name)
            return llm.ToolResult(data=error_payload(ErrorKind.UNREACHABLE, UNAVAILABLE), error=True)
        return llm.ToolResult(data=data)

    async def _answer(self, hass: HomeAssistant, raw_args: Any) -> JsonObjectType:
        args = _read_args(self._params, raw_args)
        runtime = _runtime(hass)
        if runtime is None:
            raise ToolError(ErrorKind.UNREACHABLE, UNAVAILABLE)
        store = runtime.store
        if store is None:
            raise ToolError(ErrorKind.SEASON_MISSING, SEASON_MISSING)
        snapshot = await self._snapshot(runtime, store, previous=False)
        index = TeamIndex.build(snapshot.matches, snapshot.season, runtime.aliases)
        tables, scorers = self._wanted(runtime, snapshot, index, args)
        if tables or scorers:
            snapshot = await self._snapshot(runtime, store, previous=False, tables=tables, scorers=scorers)
            index = TeamIndex.build(snapshot.matches, snapshot.season, runtime.aliases)
        return await self._respond(runtime, store, snapshot, index, args)

    async def _snapshot(
        self,
        runtime: RuntimeData,
        store: DataStore,
        *,
        previous: bool,
        tables: Sequence[str] = (),
        scorers: Sequence[str] = (),
    ) -> Snapshot:
        """Spiellisten aller gewählten Wettbewerbe (der Vereinsindex braucht sie), dazu Tabellen und Torjägerlisten
        nur der Wettbewerbe, die die Antwort braucht."""
        options = {"tables": tables, "scorers": scorers, "previous": previous}
        snapshot = await store.async_snapshot(runtime.settings.competitions, **{k: v for k, v in options.items() if v})
        _check_reachable(snapshot, runtime)
        return snapshot


def _competition_param(description: str, values: Sequence[str], default_text: str) -> _Param:
    return _Param(
        "wettbewerb",
        "choice",
        f"{description} Gültig sind: {_valid(values)}. Vorgabe: {default_text}.",
        describe_only=tuple(values),
    )


class VereinTool(_FootballTool):
    """Letzte und nächste Spiele eines Vereins oder der Nationalmannschaft."""

    name = VEREIN
    title = "Spiele eines Vereins"

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        low, high = logic_verein.MIN_COUNT, logic_verein.MAX_COUNT
        return (
            _Param(
                "verein",
                "text",
                "Name oder Spitzname des Vereins, zum Beispiel „Club“, „BVB“ oder „Bayern“; gemeint ist immer die"
                " Männermannschaft. „Deutschland“ oder „Nationalmannschaft“ meint die Nationalmannschaft der Männer."
                " Vorgabe: der Hauptverein aus den Einstellungen.",
            ),
            _Param(
                "richtung",
                "choice",
                f"Welche Spiele: letzte (schon gespielt oder läuft gerade), naechste (noch nicht angepfiffen) oder"
                f" beides. Gültig sind: {_valid(logic_verein.DIRECTIONS)}. Vorgabe: {DEFAULT_DIRECTION}.",
                choices=logic_verein.DIRECTIONS,
                default=DEFAULT_DIRECTION,
            ),
            _Param(
                "anzahl",
                "number",
                f"Wie viele Spiele je Richtung, ganze Zahl von {low} bis {high}; für die Form die letzten {high}."
                f" Vorgabe: {DEFAULT_CLUB_COUNT}.",
                bounds=(low, high),
                default=DEFAULT_CLUB_COUNT,
            ),
            _Param(
                "gegner",
                "text",
                "Nur Spiele gegen diesen Verein in der laufenden Saison, zum Beispiel für den Termin oder das"
                " Hinspiel eines Duells. Vorgabe: kein Gegner, also alle Spiele.",
            ),
            _competition_param(
                "Nur Spiele in diesem Wettbewerb, zum Beispiel, ob der Verein im Pokal noch dabei ist."
                " Frauenteams gibt es hier nicht (dafür fussball_assist__spiele mit frauen_bundesliga).",
                choices.clubs,
                "alle ausgewählten Wettbewerbe",
            ),
            _Param(
                "nur_heimspiele",
                "flag",
                "Nur Heimspiele: true oder false. Endspiele und Länderspiele zählen nicht als Heimspiel."
                " Vorgabe: false.",
                default=False,
            ),
        )

    def _build_description(self) -> str:
        return (
            "Letzte und nächste Spiele eines bestimmten Vereins oder der Nationalmannschaft: Gegner, Anstoß,"
            " heim oder auswärts, Ergebnis, Ausgang aus Sicht des Vereins (etwa „2:1-Sieg“), Torschützen,"
            " laufender Stand (mit Uhrzeit des Abrufs und Vorbehalt) und Pokalstatus. Ohne Verein"
            " gilt der Hauptverein („wir“, „mein Verein“). Beispiele: „Hat der BVB gestern gewonnen?“,"
            " „Wann spielt Bayern gegen Dortmund?“, „Wann spielt Deutschland wieder?“. Nicht für alle Spiele"
            " eines Tages (fussball_assist__spiele), Tabellenplätze (fussball_assist__tabelle), Torjäger"
            " (fussball_assist__torjaeger) oder die wichtigsten Spiele der Woche (fussball_assist__woche)."
        )

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        _wait_for_partial_names(snapshot, index, runtime, (args["verein"], args["gegner"]))
        result = self._club_answer(runtime, snapshot, index, args)
        wants_past = args["richtung"] != "naechste" and args["gegner"] is None
        if wants_past and result.get("letzte") == [] and not snapshot.previous_matches:
            # Noch kein Spiel der laufenden Saison gespielt: das letzte Spiel kommt aus der Vorsaison.
            snapshot = await self._snapshot(runtime, store, previous=True)
            index = TeamIndex.build(snapshot.matches, snapshot.season, runtime.aliases)
            _wait_for_partial_names(snapshot, index, runtime, (args["verein"], args["gegner"]))
            result = self._club_answer(runtime, snapshot, index, args)
        return result

    @staticmethod
    def _club_answer(runtime: RuntimeData, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]) -> JsonObjectType:
        return logic_verein.answer(
            snapshot,
            index,
            runtime.settings,
            verein=args["verein"],
            richtung=args["richtung"],
            anzahl=args["anzahl"],
            gegner=args["gegner"],
            wettbewerb=args["wettbewerb"],
            nur_heimspiele=args["nur_heimspiele"],
        )


class SpieleTool(_FootballTool):
    """Alle Spiele eines Tages, Wochenendes oder Spieltags."""

    name = SPIELE
    title = "Spiele eines Tages oder Spieltags"

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        return (
            _Param(
                "wettbewerb",
                "choice",
                f"Ein Wettbewerb oder alle; frauen_bundesliga für die Spiele der Frauen."
                f" Gültig sind: {_valid((logic_spiele.ALL, *choices.chosen))}. Vorgabe: {DEFAULT_COMPETITION}.",
                describe_only=(logic_spiele.ALL, *choices.chosen),
                default=DEFAULT_COMPETITION,
            ),
            _Param(
                "zeitraum",
                "choice",
                f"Gültig sind: {_valid(logic_spiele.VALID_PERIODS)}. Ein Wochenende ist Freitag bis Sonntag;"
                f" während eines Wochenendes meint kommendes_wochenende das laufende. letzter_spieltag und"
                f" naechster_spieltag brauchen einen einzelnen Wettbewerb. Vorgabe: {DEFAULT_PERIOD}.",
                choices=logic_spiele.VALID_PERIODS,
                default=DEFAULT_PERIOD,
            ),
            _Param(
                "datum",
                "text",
                "Ein bestimmter Tag in der Form JJJJ-MM-TT, zum Beispiel 2026-11-21; ersetzt zeitraum."
                " Vorgabe: kein Datum, dann gilt zeitraum.",
            ),
        )

    def _build_description(self) -> str:
        return (
            "Alle Spiele eines Tages, Wochenendes oder Spieltags, nach Wettbewerb gruppiert, mit Ergebnissen und"
            " laufenden Ständen; ohne Spiele nennt es den nächsten Tag mit Spielen und eine Länderspielpause als"
            " Grund. Beispiele: „Läuft heute Abend noch irgendwo ein Spiel?“, „Warum ist dieses Wochenende keine"
            " Bundesliga?“. Für die Spiele eines bestimmten Vereins nimm fussball_assist__verein, für Tabellen"
            " fussball_assist__tabelle, für Torjäger fussball_assist__torjaeger, für die wichtigsten Spiele der"
            " Woche fussball_assist__woche."
        )

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        return logic_spiele.answer(
            snapshot,
            runtime.settings,
            wettbewerb=args["wettbewerb"],
            zeitraum=args["zeitraum"],
            datum=args["datum"],
            index=index,
        )


class TabelleTool(_FootballTool):
    """Tabelle eines Wettbewerbs, auf Wunsch rund um einen Verein mit Abständen."""

    name = TABELLE
    title = "Tabelle"

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        return (
            _competition_param(
                "Wettbewerb mit Tabelle; Pokal und Nationalmannschaft haben keine.",
                choices.tables,
                f"die Liga des genannten Vereins, sonst die Liga des Hauptvereins, sonst {DEFAULT_LEAGUE}",
            ),
            _Param(
                "verein",
                "text",
                "Zeigt die Zeile dieses Vereins, je zwei Plätze darüber und darunter und fertige Punkteabstände"
                " (zum Ersten, zu den Nachbarn, zur nächsten Zone); ausschnitt gilt dann nicht."
                " Vorgabe: kein Verein.",
            ),
            _Param(
                "ausschnitt",
                "choice",
                f"spitze (die ersten fünf), ende (die letzten fünf, etwa für die Abstiegsplätze) oder ganz (alle"
                f" Plätze; nimm ganz für Fragen nach der besten Offensive, der besten Abwehr oder wer noch"
                f" ungeschlagen ist). Gültig sind: {_valid(logic_tabelle.SECTIONS)}. Vorgabe: {DEFAULT_SECTION}.",
                choices=logic_tabelle.SECTIONS,
                default=DEFAULT_SECTION,
            ),
        )

    def _build_description(self) -> str:
        return (
            "Tabelle einer Liga oder der Ligaphase im Europapokal: Platz, Verein, Spiele, Punkte, Tore,"
            " Gegentore, Siege, Unentschieden, Niederlagen und die Zone (Aufstieg, Relegation, Abstieg); mit"
            " Verein dessen Umfeld und fertige Punkteabstände. Keine Prognosen. Beispiele: „Auf welchem Platz"
            " ist Schalke?“, „Wie viele Punkte fehlen Hertha bis zum Aufstiegsplatz?“, „Welche Mannschaft"
            " hat die meisten Tore geschossen?“. Für Spiele nimm fussball_assist__verein oder"
            " fussball_assist__spiele, für die Tore einzelner Spieler fussball_assist__torjaeger, für die"
            " wichtigsten Spiele der Woche fussball_assist__woche."
        )

    def _wanted(
        self, runtime: RuntimeData, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            _wait_for_partial_names(snapshot, index, runtime, (args["verein"],))
        except ToolError:
            return (), ()  # die Antwort ist der Ladefehler
        key = logic_tabelle.table_key(
            snapshot, index, runtime.settings, wettbewerb=args["wettbewerb"], verein=args["verein"], ausschnitt=args["ausschnitt"]
        )
        return ((key,) if key is not None else ()), ()

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        _wait_for_partial_names(snapshot, index, runtime, (args["verein"],))
        return logic_tabelle.answer(
            snapshot,
            index,
            runtime.settings,
            wettbewerb=args["wettbewerb"],
            verein=args["verein"],
            ausschnitt=args["ausschnitt"],
        )


class TorjaegerTool(_FootballTool):
    """Torjägerliste eines Wettbewerbs oder die Tore eines Spielers."""

    name = TORJAEGER
    title = "Torjäger"

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        low, high = logic_torjaeger.MIN_COUNT, logic_torjaeger.MAX_COUNT
        return (
            _competition_param(
                "Wettbewerb mit Torjägerliste; die Zahlen gelten nur für diesen Wettbewerb.",
                choices.scorers,
                f"die Liga des Hauptvereins, sonst {DEFAULT_LEAGUE}",
            ),
            _Param(
                "spieler",
                "text",
                "Name oder Nachname eines Spielers, zum Beispiel „Kane“; liefert alle passenden Einträge."
                " Vorgabe: kein Spieler, dann kommt die Liste.",
            ),
            _Param(
                "anzahl",
                "number",
                f"Wie viele Plätze der Liste, ganze Zahl von {low} bis {high}. Vorgabe: {DEFAULT_SCORER_COUNT}.",
                bounds=(low, high),
                default=DEFAULT_SCORER_COUNT,
            ),
        )

    def _build_description(self) -> str:
        return (
            "Torjägerliste eines Wettbewerbs oder die Tore eines bestimmten Spielers: Name, Tore und Platz;"
            " die Liste nennt keinen Verein. Beispiele: „Wer führt die Torjägerliste der Bundesliga an?“,"
            " „Wie viele Tore hat der Kane diese Saison schon?“. Nicht für die Tore einer Mannschaft"
            " (fussball_assist__tabelle) und nicht für die Torschützen eines einzelnen Spiels"
            " (fussball_assist__verein oder fussball_assist__spiele); die wichtigsten Spiele der Woche liefert"
            " fussball_assist__woche."
        )

    def _wanted(
        self, runtime: RuntimeData, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        key = logic_torjaeger.list_key(
            snapshot, index, runtime.settings, wettbewerb=args["wettbewerb"], spieler=args["spieler"], anzahl=args["anzahl"]
        )
        return (), ((key,) if key is not None else ())

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        return logic_torjaeger.answer(
            snapshot,
            index,
            runtime.settings,
            wettbewerb=args["wettbewerb"],
            spieler=args["spieler"],
            anzahl=args["anzahl"],
        )


class WocheTool(_FootballTool):
    """Die wichtigsten Spiele dieser oder der nächsten Woche."""

    name = WOCHE
    title = "Wichtigste Spiele der Woche"

    def _build_params(self, choices: _Choices) -> tuple[_Param, ...]:
        return (
            _Param(
                "woche",
                "choice",
                f"diese (heute bis Sonntag) oder naechste (der folgende Montag bis Sonntag)."
                f" Gültig sind: {_valid(logic_woche.WEEKS)}. Vorgabe: {DEFAULT_WEEK}.",
                choices=logic_woche.WEEKS,
                default=DEFAULT_WEEK,
            ),
        )

    def _build_description(self) -> str:
        return (
            "Höchstens fünf besondere Spiele dieser oder der nächsten Woche, jedes mit einem kurzen Grund, nach"
            " festen Regeln: Lieblingsvereine, Länderspiele, K.-o.-Spiele im Pokal und Europapokal,"
            " Spitzenspiele, Derbys. Beispiele: „Was sind die wichtigsten Spiele diese Woche?“, „Welche Spiele"
            " lohnen sich nächste Woche?“. Für alle Spiele eines Tages nimm fussball_assist__spiele, für einen"
            " bestimmten Verein fussball_assist__verein, für Tabellen fussball_assist__tabelle, für Torjäger"
            " fussball_assist__torjaeger."
        )

    def _wanted(
        self, runtime: RuntimeData, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return logic_woche.needed_tables(snapshot, runtime.settings), ()  # Spitzenspiele erkennt die Antwort nur mit Tabelle

    async def _respond(
        self, runtime: RuntimeData, store: DataStore, snapshot: Snapshot, index: TeamIndex, args: dict[str, Any]
    ) -> JsonObjectType:
        return logic_woche.answer(snapshot, index, runtime.settings, runtime.derbies, woche=args["woche"])


_TOOL_CLASSES: tuple[type[_FootballTool], ...] = (VereinTool, SpieleTool, TabelleTool, TorjaegerTool, WocheTool)


@callback
def async_get_tools(hass: HomeAssistant, llm_context: llm.LLMContext, api_id: str) -> LLMTools | None:
    """Die fünf Werkzeuge und der Hinweistext, nur für die Assist-API. Ruft nichts ab und wirft nie."""
    if api_id != llm.LLM_API_ASSIST:
        return None
    try:
        runtime = _runtime(hass)
        if runtime is None:
            return None
        choices = _Choices.build(runtime)
        return LLMTools(tools=[cls(choices) for cls in _TOOL_CLASSES], prompt=PROMPT)
    except Exception:
        _LOGGER.exception("Werkzeuge konnten nicht bereitgestellt werden")
        return None
