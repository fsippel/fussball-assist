"""Antwort "Torjäger": die besten Torschützen eines Wettbewerbs oder die Suche nach Spielern (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

import re
from typing import Any

from .models import Scorer
from .results import (
    ErrorKind,
    Settings,
    ToolError,
    asked_competition,
    chosen_competitions,
    default_league,
    given,
    loading_error,
    scorers_stale_hints,
    season_hints,
    season_started,
    stand_phrase,
    unique,
)
from .seasons import Competition, Kind
from .store import Snapshot
from .teams import TeamIndex, normalize

MIN_COUNT = 1
MAX_COUNT = 10
NO_CLUB = "Die Liste nennt keinen Verein."
UNSURE = "ob es derselbe Spieler ist, kann ich nicht sicher sagen."
SHOOTOUT = "In einem Pokal können die Zahlen auch Tore aus dem Elfmeterschießen enthalten."


def _check_count(value: Any) -> int:
    """Die Anzahl, 1 bis 10; fehlt sie, gelten fünf."""
    text = given(value)
    if text is None:
        return 5
    number: int | None = None
    if isinstance(text, int) and not isinstance(text, bool):
        number = text
    elif isinstance(text, float) and text.is_integer():
        number = int(text)
    elif isinstance(text, str) and text.isdecimal():
        number = int(text)
    if number is None or not MIN_COUNT <= number <= MAX_COUNT:
        raise ToolError(
            ErrorKind.INVALID_INPUT,
            f"Die Anzahl muss eine ganze Zahl von {MIN_COUNT} bis {MAX_COUNT} sein, nicht „{value}“.",
        )
    return number


def _display_name(name: str) -> str:
    """Der Name ohne überzähligen Leerraum; "Nachname, Vorname" wird zu "Vorname Nachname"."""
    parts = [part.strip() for part in name.split(",")]
    if len(parts) == 2 and all(parts):
        name = f"{parts[1]} {parts[0]}"
    return " ".join(name.split())


def _words(text: str) -> list[str]:
    """Die Wörter eines Namens ohne Umlaute, Groß-/Kleinschreibung und Satzzeichen ("N." wird "n").

    Bindestriche trennen wie Leerzeichen. "Müller", "Mueller" und "Muller" ergeben dasselbe Wort, weil gesprochene
    und getippte Namen oft ohne Umlaut ankommen; ebenso "Grønbæk", "Grönbäk" und "Gronbaek".
    """
    words = (word for part in re.split(r"[\s-]+", _display_name(text)) for word in normalize(part).split())
    return [w.replace("ae", "a").replace("oe", "o").replace("ue", "u") for w in words]


SURE, INITIAL, PARTIAL = 0, 1, 2  # wie sicher ein Treffer ist: nur der Anfangsbuchstabe, nur ein Teil des Namens


def _locate(words: list[str], part: list[str]) -> list[int]:
    return [p for p in range(len(words) - len(part) + 1) if words[p : p + len(part)] == part]


def _match_level(search: list[str], name: str) -> int | None:
    """Wie gut ein Eintrag zur Suche passt: `SURE`, `INITIAL`, `PARTIAL` oder None (kein Treffer).

    Die Suche zerfällt in Vornamen und Nachnamenwörter (jede Teilung wird versucht). Die Nachnamenwörter müssen
    zusammenhängend im Eintrag stehen, sicher nur am Ende ("da Costa", "Latte Lath"); stehen sie weiter vorn
    ("Lee" in "Lee Jae-sung"), ist der Treffer `PARTIAL`. Nennt die Suche einen Vornamen, muss er zum ersten
    Wort des Eintrags passen: ausgeschrieben gleich, oder bei einer Abkürzung auf einer Seite der Anfangsbuchstabe.
    Ist nur der Eintrag abgekürzt oder hat er keinen Vornamen, ist der Treffer `INITIAL`, denn dann lässt sich
    nicht sagen, ob es derselbe Spieler ist. Eine abgekürzte Suche gegen einen ausgeschriebenen Namen ist sicher.
    """
    words = _words(name)
    best: int | None = None
    for split in range(len(search)):
        given_names, surname = search[:split], search[split:]
        for position in _locate(words, surname):
            level = SURE if position + len(surname) == len(words) else PARTIAL
            if given_names:
                if position == 0:
                    if len(words) != len(surname):
                        continue  # kein Vorname im Eintrag, aber der Name geht weiter: kein Vergleich möglich
                    level = max(level, INITIAL)
                else:
                    first, entry_first = given_names[0], words[0]
                    if len(first) > 1 and len(entry_first) > 1:
                        if first != entry_first:
                            continue
                    elif first[0] != entry_first[0]:
                        continue
                    elif len(first) > 1:  # ausgeschriebener Vorname gesucht, im Eintrag nur der Anfangsbuchstabe
                        level = max(level, INITIAL)
            best = level if best is None else min(best, level)
    return best


def _ranked(scorers: tuple[Scorer, ...]) -> list[tuple[int, Scorer]]:
    """Alle Einträge mit Platz; gleiche Torzahl teilt den Platz, der nächste Platz entfällt entsprechend (1, 2, 2, 4)."""
    ordered = sorted(scorers, key=lambda s: -s.goals)
    return [(1 + sum(1 for other in ordered if other.goals > s.goals), s) for s in ordered]


def _goals(count: int) -> str:
    return f"{count} Tor" if count == 1 else f"{count} Toren"


def _no_list(snapshot: Snapshot, settings: Settings, competition: Competition) -> ToolError:
    capable = ", ".join(
        k for k in chosen_competitions(snapshot, settings) if snapshot.season.competitions[k].has_scorers
    )
    return ToolError(
        ErrorKind.UNSUPPORTED,
        f"Für {competition.name} gibt es keine Torjägerliste. Eine Torjägerliste gibt es für: {capable}."
        f" Frag nach den Spielen von {competition.name}, dann nenne ich die Torschützen der einzelnen Spiele.",
    )


def _list_hints(competition: Competition) -> list[str]:
    hints = [NO_CLUB]
    if competition.scorers_incomplete:
        if competition.kind is Kind.EUROPE:
            extent = "deckt nur einen kleinen Teil der Tore ab"
        else:
            extent = "deckt nicht alle Tore ab"
        hints.append(f"Die Torjägerliste von {competition.name} ist unvollständig und {extent}.")
    if competition.kind is Kind.CUP:
        hints.append(SHOOTOUT)
    return hints


def _competition(snapshot: Snapshot, index: TeamIndex, settings: Settings, wettbewerb: Any) -> tuple[str, list[str]]:
    """Der Wettbewerb der Frage und Hinweise zur Wahl der Liga; wirft nur `ToolError`."""
    competitions = snapshot.season.competitions
    asked = given(wettbewerb)
    if asked is None:
        return default_league(snapshot, index, settings, "Torjägerliste")
    key = asked_competition(snapshot, settings, asked, lambda c: c.has_scorers)
    if not competitions[key].has_scorers:
        raise _no_list(snapshot, settings, competitions[key])
    return key, []


def _search(spieler: Any) -> tuple[Any, list[str]]:
    """Der gesuchte Name und seine Wörter; ohne Namen (None, []). Wirft `INVALID_INPUT`, wenn nichts übrig bleibt."""
    text = given(spieler)
    if text is None:
        return None, []
    search = _words(str(text))
    if not search:
        raise ToolError(
            ErrorKind.INVALID_INPUT, f"Mit „{text}“ kann ich keinen Spieler suchen. Nenne den Nachnamen, zum Beispiel „Müller“."
        )
    return text, search


def list_key(
    snapshot: Snapshot, index: TeamIndex, settings: Settings, *, wettbewerb: str | None, spieler: str | None, anzahl: int
) -> str | None:
    """Der Wettbewerb, dessen Torjägerliste die Antwort braucht; None, wenn sie ohne Liste endet (Fehler).

    Braucht nur die Spiellisten (die Liga des Hauptvereins kommt aus ihnen), deshalb lässt sich die eine
    nötige Liste anfordern, bevor die Antwort entsteht.
    """
    try:
        _check_count(anzahl)
        key, _ = _competition(snapshot, index, settings, wettbewerb)
        _search(spieler)
    except ToolError:
        return None
    return key


def answer(
    snapshot: Snapshot,
    index: TeamIndex,
    settings: Settings,
    *,
    wettbewerb: str | None,
    spieler: str | None,
    anzahl: int,
) -> dict[str, Any]:
    """Die besten Torschützen eines Wettbewerbs oder die Treffer einer Spielersuche; wirft nur `ToolError`.

    Prüfung in dieser Reihenfolge: `anzahl` (1 bis 10, auch mit Spieler), Wettbewerb, Spielername. Der Wettbewerb
    ohne Angabe ist die Liga des Hauptvereins, sonst die Bundesliga; er braucht eine Torjägerliste, sonst
    `UNSUPPORTED`. Gleiche Torzahl teilt den Platz. Mit `spieler` stehen alle Treffer der Suche im Ergebnis
    (mit ihrem Platz in der ganzen Liste), `anzahl` kürzt sie nicht. Fehlt die Liste (nicht geladen, nicht
    erreichbar, ungültig), wirft die Antwort den Ladefehler; nie steht "nicht in der Liste" auf fehlenden
    Daten. Die Liste nennt keinen Verein, und das sagt ein Hinweis.
    """
    count = _check_count(anzahl)
    competitions = snapshot.season.competitions
    key, hints = _competition(snapshot, index, settings, wettbewerb)
    text, search = _search(spieler)

    competition = competitions[key]
    problem = loading_error(snapshot, [key], "die Torjägerliste")
    if problem is not None:
        raise problem
    if key not in snapshot.scorers:
        raise ToolError(
            ErrorKind.UNREACHABLE,
            f"Die Torjägerliste von {competition.name} ist noch nicht geladen. Versuch es gleich noch einmal.",
        )
    ranked = _ranked(snapshot.scorers[key])
    if not ranked:
        why = (
            "obwohl schon Spiele beendet sind; deshalb kann ich keine Torjäger nennen"
            if season_started(snapshot, key)
            else "die Saison hat vielleicht noch nicht begonnen"
        )
        raise ToolError(ErrorKind.NOTHING_ENTERED, f"In der Torjägerliste von {competition.name} steht noch niemand, {why}.")

    hints = _list_hints(competition) + hints
    if text is not None:
        levels = {id(s): _match_level(search, s.name) for _, s in ranked}
        shown = [(place, s) for place, s in ranked if levels[id(s)] is not None]
        for _, s in shown:
            if levels[id(s)] == PARTIAL:
                hints.append(f"Bei „{_display_name(s.name)}“ passt nur ein Teil des Namens; {UNSURE}")
            elif levels[id(s)] == INITIAL:
                hints.append(
                    f"Bei „{_display_name(s.name)}“ nennt die Liste nur den Anfangsbuchstaben des Vornamens (oder nur den Nachnamen); {UNSURE}"
                )
        if not shown:
            if competition.scorers_incomplete:
                hints.append(
                    f"„{text}“ finde ich in der unvollständigen Torjägerliste nicht; weil sie nicht alle Tore enthält,"
                    " kann ich dazu nichts Sicheres sagen."
                )
            else:
                hints.append(f"„{text}“ steht nicht in der Torjägerliste.")
    else:
        shown = ranked[:count]
        if len(shown) < len(ranked) and ranked[len(shown)][0] == shown[-1][0]:
            hints.append(f"Auf dem letzten genannten Platz stehen noch weitere Spieler mit {_goals(shown[-1][1].goals)}.")

    hints += scorers_stale_hints(snapshot, [key])
    hints += season_hints(snapshot, [key])
    return {
        "wettbewerb": competition.name,
        "stand": stand_phrase(snapshot, scorers=[key]),
        "torjaeger": [{"platz": place, "name": _display_name(s.name), "tore": s.goals} for place, s in shown],
        "hinweise": unique(hints),
    }
