"""Antwort "Spiele": alle Spiele eines Tages, Wochenendes oder Spieltags (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from .models import Match
from .results import (
    CATCH_UP,
    SCORERS_MISSING,
    STARTED,
    UNFINISHED,
    ErrorKind,
    Settings,
    ToolError,
    available,
    favorite_ids,
    given,
    guard,
    live_hints,
    loading_error,
    loading_hints,
    match_entry,
    match_hints,
    round_matches,
    round_text,
    same_round_matches,
    season_hints,
    stand_phrase,
    unique,
)
from .seasons import Competition, Kind
from .store import LIVE, SCHEDULED, Snapshot, match_state
from .teams import TeamIndex, normalize
from .timeutil import PERIODS, day_phrase, local_day, parse_day, range_phrase, relative_phrase, resolve_period

MAX_DETAILED = 12  # bei mehr Spielen entfallen Tore und Stand nach 90 Minuten
MAX_PER_COMPETITION = 9  # bei mehr Spielen als MAX_DETAILED bleiben je Wettbewerb höchstens so viele
MAX_SHORTENED = 30  # und insgesamt höchstens so viele (Lieblingsvereine zuerst), damit die Antwort klein bleibt
SPAN_MARGIN = timedelta(days=7)  # so weit vor dem ersten und nach dem letzten Spiel gilt ein Tag noch als abgedeckt
ROUND_PERIODS = ("letzter_spieltag", "naechster_spieltag")
VALID_PERIODS = PERIODS + ROUND_PERIODS
ALL = "alle"
BREAK = "Länderspielpause"

_DETAILS = ("tore", "nach_90_minuten")  # Felder, die bei vielen Spielen entfallen
_ROUND_WORD = {Kind.CUP: "Runde", Kind.EUROPE: "Runde"}  # sonst "Spieltag"
TIMES_FIXED = "Die Uhrzeiten stehen fest, außer bei den Spielen, bei denen ausdrücklich steht, dass die Uhrzeit noch nicht fest ist."
_TODAY = "heute"
_NO_ROUND = {  # Art des Wettbewerbs -> (noch keine gespielt, keine weitere eingetragen)
    Kind.CUP: ("Runde", "keine weitere Runde"),
    Kind.NATIONAL: ("Länderspiel", "kein weiteres Länderspiel"),
}


def _check_period(value: Any) -> str:
    text = given(value)
    if text is None:
        return _TODAY
    if isinstance(text, str) and text.casefold() in VALID_PERIODS:
        return text.casefold()
    raise ToolError(ErrorKind.INVALID_INPUT, f"Den Zeitraum „{value}“ gibt es nicht. Gültig sind: {', '.join(VALID_PERIODS)}.")


def _check_day(value: Any) -> date | None:
    text = given(value)
    if text is None:
        return None
    day = parse_day(text)
    if day is None:
        raise ToolError(
            ErrorKind.INVALID_INPUT,
            f"Das Datum „{value}“ geht nicht. Gültig ist ein Tag in der Form JJJJ-MM-TT, zum Beispiel 2026-11-21.",
        )
    return day


def _scope(snapshot: Snapshot, settings: Settings, text: Any) -> tuple[str, ...]:
    """Die gefragten unter den gewählten Wettbewerben (in der Reihenfolge der Saison), Frauen-Bundesliga eingeschlossen."""
    competitions = snapshot.season.competitions
    chosen = tuple(k for k in competitions if k in settings.competitions)
    wanted = given(text)
    if wanted is None or normalize(str(wanted)) == ALL:
        return chosen
    valid = f"Gültig sind: {', '.join((ALL, *chosen))}."
    lookup = {normalize(k): k for k in competitions} | {normalize(c.name): k for k, c in competitions.items()}
    key = lookup.get(normalize(str(wanted)))
    if key is None:
        raise ToolError(ErrorKind.INVALID_INPUT, f"Den Wettbewerb „{wanted}“ gibt es nicht. {valid}")
    if key not in chosen:
        raise ToolError(
            ErrorKind.INVALID_INPUT, f"Der Wettbewerb „{competitions[key].name}“ ist nicht ausgewählt. {valid}"
        )
    return (key,)


def _next_day(snapshot: Snapshot, keys: Sequence[str], after: date) -> date | None:
    """Erster Ortstag nach `after` mit einem Spiel, das noch nicht angepfiffen ist oder läuft."""
    tz = snapshot.tz
    days = (
        local_day(m.kickoff, tz)
        for key in keys
        for m in snapshot.matches.get(key, ())
        if match_state(m, snapshot.now) in (SCHEDULED, LIVE)
    )
    return min((d for d in days if d > after), default=None)


def _span(snapshot: Snapshot, keys: Sequence[str]) -> tuple[date, date] | None:
    """Erster und letzter Ortstag mit einem Spiel in den Daten der Wettbewerbe; None ohne Spiele."""
    days = [local_day(m.kickoff, snapshot.tz) for key in keys for m in snapshot.matches.get(key, ())]
    return (min(days), max(days)) if days else None


def _is_favorite(match: Match, favorites: frozenset[int]) -> bool:
    return bool({match.home.id, match.away.id} & favorites)


def answer(
    snapshot: Snapshot,
    settings: Settings,
    *,
    wettbewerb: str | None,
    zeitraum: str | None,
    datum: str | None,
    index: TeamIndex | None = None,
) -> dict[str, Any]:
    """Spiele eines Tages, Wochenendes oder Spieltags, nach Wettbewerben gruppiert; wirft nur `ToolError`.

    `datum` ersetzt `zeitraum`. `letzter_spieltag` und `naechster_spieltag` brauchen einen einzelnen
    Wettbewerb und kommen aus den Spieldaten. Der Vereinsindex (`index`, sonst aus der Momentaufnahme
    gebaut) liefert alle Kennungen der Lieblingsvereine. Ohne Spiele nennt das Ergebnis den nächsten Tag
    mit Spielen und, wenn eine Länderspielpause dazwischenliegt, diese als `grund`; beides nur, wenn
    alle gefragten Wettbewerbe vollständig geladen sind, sonst steht ein Hinweis.
    """
    period = _check_period(zeitraum)
    day = _check_day(datum)
    scope = _scope(snapshot, settings, wettbewerb)
    by_round = day is None and period in ROUND_PERIODS
    if by_round and len(scope) > 1:
        raise ToolError(
            ErrorKind.INVALID_INPUT,
            f"Den {'letzten' if period == 'letzter_spieltag' else 'nächsten'} Spieltag kann ich nur für einen einzelnen"
            f" Wettbewerb nennen. Gültig sind: {', '.join(scope)}.",
        )
    guard(snapshot, scope)
    loaded = available(snapshot, scope)
    if not loaded:  # noch nichts geladen: "keine Spiele" wäre geraten
        raise loading_error(snapshot, scope, "die Spiele") or ToolError(
            ErrorKind.UNREACHABLE, "Die Spiele sind noch nicht geladen. Versuch es gleich noch einmal."
        )
    complete = len(loaded) == len(scope)

    competitions = snapshot.season.competitions
    today = local_day(snapshot.now, snapshot.tz)
    year = today.year
    if day is not None:
        span = _span(snapshot, loaded)
        if span is None or not span[0] - SPAN_MARGIN <= day <= span[1] + SPAN_MARGIN:
            return _no_data(snapshot, scope, loaded, day, span)
    picked: dict[str, list[Match]] = {}
    note = ""
    if by_round:
        key = loaded[0]
        chosen, note = round_matches(snapshot, key, period)
        if chosen:
            picked[key] = chosen
            days = [local_day(m.kickoff, snapshot.tz) for m in chosen]
            first, last = min(days), max(days)
            name = "" if competitions[key].kind is Kind.NATIONAL else round_text(min(chosen, key=lambda m: (m.kickoff, m.id)).round_name)
            dates = range_phrase(first, last, current_year=year)
            label = f"{name} ({dates})" if name else dates
            end = last
        else:
            label = "letzter Spieltag" if period == "letzter_spieltag" else "nächster Spieltag"
            end = today - timedelta(days=1)  # gesucht wird ab heute
    else:
        first, end = (day, day) if day is not None else resolve_period(period, snapshot.now, snapshot.tz)
        label = range_phrase(first, end, current_year=year)
        for key in loaded:
            found = [m for m in snapshot.matches.get(key, ()) if first <= local_day(m.kickoff, snapshot.tz) <= end]
            if found:
                picked[key] = found

    total = sum(len(v) for v in picked.values())
    shortened = total > MAX_DETAILED
    favorites = favorite_ids(snapshot, settings, index) if shortened else frozenset()

    def rank(match: Match) -> tuple[bool, bool, Any, int]:
        """Laufende Spiele zuerst, bei gekürzter Antwort dann die der Lieblingsvereine, dann der Anstoß."""
        return (
            match_state(match, snapshot.now) != LIVE,
            not (shortened and _is_favorite(match, favorites)),
            match.kickoff,
            match.id,
        )

    kept = {key: sorted(matches, key=rank) for key, matches in picked.items()}
    if shortened:  # je Wettbewerb höchstens neun, insgesamt höchstens 30 (nach demselben Rang)
        kept = {key: matches[:MAX_PER_COMPETITION] for key, matches in kept.items()}
        allowed = {m.id for m in sorted((m for v in kept.values() for m in v), key=rank)[:MAX_SHORTENED]}
        kept = {key: [m for m in matches if m.id in allowed] for key, matches in kept.items()}

    groups: list[tuple[Competition, list[tuple[Match, dict[str, Any]]]]] = []
    for key in loaded:
        if not kept.get(key):
            continue
        competition, every = competitions[key], snapshot.matches.get(key, ())
        entries = [
            (m, match_entry(m, snapshot, team=None, competition=competition, same_round=same_round_matches(m, every)))
            for m in kept[key]
        ]
        groups.append((competition, entries))
    if shortened:  # Gruppen mit einem Spiel der Lieblingsvereine zuerst (stabil, sonst Reihenfolge der Saison)
        groups.sort(key=lambda g: not any(_is_favorite(m, favorites) for m, _ in g[1]))

    hints: list[str] = []
    dropped, removed, times_dropped = [], False, False
    for _, entries in groups:
        for match, entry in entries:
            hints += (h for h in match_hints(match, entry) if not (shortened and h == SCORERS_MISSING))
    for key in loaded:  # auch ein Wettbewerb, der ganz entfallen ist, wird genannt
        count, shown_count = len(picked.get(key, ())), len(kept.get(key, ()))
        if count > shown_count:
            of = f"ich nenne nur {shown_count} davon" if shown_count else "ich nenne davon keins"
            dropped.append(f"Im Wettbewerb {competitions[key].name} gibt es {count} Spiele; {of}.")
    hints += live_hints(snapshot, [(m, entry, competition) for competition, entries in groups for m, entry in entries])
    shown = [entry for _, entries in groups for _, entry in entries]
    for entry in shown:
        entry.pop("wettbewerb", None)  # steht schon an der Gruppe
        if shortened:
            for field in _DETAILS:
                removed |= entry.pop(field, None) is not None
            if entry.get("uhrzeit_fest") is True:  # "false" bleibt stehen: nur das Abweichende wird genannt
                del entry["uhrzeit_fest"]
                times_dropped = True
    if removed:
        hints.append("Weil es so viele Spiele sind, nenne ich keine Torschützen und keine Stände nach 90 Minuten.")
    if times_dropped:
        hints.append(TIMES_FIXED)
    if note and by_round:
        word = _ROUND_WORD.get(competitions[loaded[0]].kind, "Spieltag")
        article = ("dieser", "der") if word == "Runde" else ("diesem", "des")
        if note == UNFINISHED:
            hints.append(f"Von {article[0]} {word} sind noch nicht alle Spiele beendet; ich nenne nur die beendeten.")
        elif note == STARTED:
            hints.append(f"Von {article[0]} {word} sind schon Spiele gespielt; ich nenne nur die, die noch ausstehen.")
        else:
            plural = len(shown) > 1
            hints.append(
                f"Das {'sind Nachholspiele' if plural else 'ist ein Nachholspiel'}: Die übrigen Spiele {article[1]} {word}"
                f"{'s' if word == 'Spieltag' else ''} sind schon gespielt."
            )
    hints += dropped

    grouped = []
    for competition, entries in groups:
        spiele = [entry for _, entry in entries]
        group: dict[str, Any] = {"wettbewerb": competition.name}
        rounds = {entry.get("runde") for entry in spiele}
        if len(rounds) == 1 and None not in rounds:  # dieselbe Runde für alle: einmal an der Gruppe
            group["runde"] = rounds.pop()
            for entry in spiele:
                del entry["runde"]
        group["spiele"] = spiele
        grouped.append(group)

    result: dict[str, Any] = {
        "zeitraum": label,
        "stand": stand_phrase(snapshot, loaded),
        "wettbewerbe": grouped,
        "gekuerzt": bool(dropped) or removed or times_dropped,
    }
    if not picked:
        hints += _nothing_found(snapshot, settings, result, scope, loaded, end, complete, label, by_round, period)
    hints += season_hints(snapshot, loaded, upcoming=by_round and period == "naechster_spieltag")
    hints += loading_hints(snapshot, scope)
    result["hinweise"] = unique(hints)
    return result


def _no_data(
    snapshot: Snapshot, scope: Sequence[str], loaded: Sequence[str], day: date, span: tuple[date, date] | None
) -> dict[str, Any]:
    """Ein Tag außerhalb der geladenen Saisondaten: Dafür gibt es keine Daten, nicht "keine Spiele"."""
    year = local_day(snapshot.now, snapshot.tz).year
    label = day_phrase(day, current_year=year)
    text = f"Für {label} habe ich keine Daten."
    if span is not None:
        # Beide Enden immer mit Jahr: Die Spanne reicht über einen Jahreswechsel, und ohne Jahr ergänzt ein
        # Sprachmodell das Jahr des gefragten Tages.
        first, last = (f"{day_phrase(d)} {d.year}" for d in span)
        text += f" Ich kenne nur Spiele von {first} bis {last}."
    return {
        "zeitraum": label,
        "stand": stand_phrase(snapshot, loaded),
        "wettbewerbe": [],
        "gekuerzt": False,
        "hinweise": unique([text, *season_hints(snapshot, loaded), *loading_hints(snapshot, scope)]),
    }


def _nothing_found(
    snapshot: Snapshot,
    settings: Settings,
    result: dict[str, Any],
    scope: Sequence[str],
    loaded: Sequence[str],
    end: date,
    complete: bool,
    label: str,
    by_round: bool,
    period: str,
) -> list[str]:
    """Nächster Tag mit Spielen und Grund in `result`; liefert die Sätze, die das Fehlen erklären.

    "Keine Spiele", der nächste Tag und der Grund gelten nur als sicher, wenn alle gefragten Wettbewerbe
    vollständig geladen sind; sonst steht stattdessen, was die geladenen Daten sagen.
    """
    competitions = snapshot.season.competitions
    where = f"im Wettbewerb {competitions[scope[0]].name}" if len(scope) == 1 else "in den ausgewählten Wettbewerben"
    hints: list[str] = []
    if by_round:
        kind = competitions[loaded[0]].kind
        past, later = _NO_ROUND.get(kind, ("Spieltag", "kein weiterer Spieltag"))
        name = competitions[loaded[0]].name
        hints.append(
            f"Im Wettbewerb {name} ist noch kein {past} gespielt."
            if period == "letzter_spieltag"
            else f"Im Wettbewerb {name} ist {later} eingetragen."
        )
    elif complete:
        hints.append(f"Für {label} sind {where} keine Spiele eingetragen.")
    else:
        hints.append(f"In den bisher geladenen Daten sind für {label} keine Spiele eingetragen.")

    following = _next_day(snapshot, loaded, end)
    if following is None:
        if complete and not by_round:
            hints.append("In den Daten ist danach kein weiteres Spiel eingetragen.")
        return hints
    today = local_day(snapshot.now, snapshot.tz)
    phrase = f"{day_phrase(following, current_year=today.year)}, also {relative_phrase(following, today)}"
    if not complete:
        hints.append(f"In den bisher geladenen Daten ist der nächste Tag mit Spielen {phrase}.")
        return hints
    result["naechster_tag_mit_spielen"] = phrase
    reason, note = _break(snapshot, settings, scope, today, following)
    if reason:
        result["grund"] = reason
    if note:
        hints.append(note)
    return hints


def _break(
    snapshot: Snapshot, settings: Settings, scope: Sequence[str], today: date, following: date
) -> tuple[str | None, str | None]:
    """Länderspielpause, wenn zwischen heute und dem nächsten Tag mit Spielen ein Länderspiel liegt.

    Nur mit gewählter Nationalmannschaft, die nicht selbst gefragt ist (dann wäre ihr Spiel der nächste Tag).
    Fehlen ihre Daten, steht stattdessen ein Hinweis, dass der Grund nicht feststeht.
    """
    competitions = snapshot.season.competitions
    national = [k for k, c in competitions.items() if c.kind is Kind.NATIONAL and k in settings.competitions]
    if not national or any(k in scope for k in national):
        return None, None
    usable = available(snapshot, national)
    if not usable:
        return None, "Ob ein Länderspiel der Grund ist, kann ich nicht sagen, weil die Daten der Nationalmannschaft fehlen."
    for key in usable:
        if any(today <= local_day(m.kickoff, snapshot.tz) < following for m in snapshot.matches.get(key, ())):
            return BREAK, None
    return None, None
