"""Antwort "Verein": letzte und nächste Spiele eines Vereins oder der Nationalmannschaft (ohne Home-Assistant-Bezug)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .models import Match, Team
from .results import (
    ErrorKind,
    Settings,
    ToolError,
    ambiguous_payload,
    available,
    given,
    guard,
    join_names,
    live_hints,
    loading_error,
    loading_hints,
    match_entry,
    match_hints,
    neutral_ground,
    question_payload,
    same_round_matches,
    searchable as searchable_keys,
    season_hints,
    season_started,
    season_label,
    stand_phrase,
    unknown_team_error,
    unique,
)
from .seasons import Competition, Kind
from .store import FINISHED, LIVE, NOT_FINISHED, SCHEDULED, Snapshot, match_state
from .teams import TeamIndex, normalize

DIRECTIONS = ("letzte", "naechste", "beides")
MIN_COUNT, MAX_COUNT = 1, 5

_PAST = (LIVE, FINISHED, NOT_FINISHED)


def _check_direction(value: Any) -> str:
    text = given(value)
    if text is None:
        return "beides"
    if isinstance(text, str) and text.casefold() in DIRECTIONS:
        return text.casefold()
    raise ToolError(ErrorKind.INVALID_INPUT, f"Die Richtung „{value}“ gibt es nicht. Gültig sind: {', '.join(DIRECTIONS)}.")


def _check_count(value: Any) -> int:
    if value is None:
        return MIN_COUNT
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and MIN_COUNT <= value <= MAX_COUNT:
        return value
    raise ToolError(
        ErrorKind.INVALID_INPUT,
        f"Die Anzahl „{value}“ geht nicht. Gültig sind ganze Zahlen von {MIN_COUNT} bis {MAX_COUNT}.",
    )


def _scope(snapshot: Snapshot, settings: Settings, text: Any) -> tuple[str, ...]:
    """Gewählte Wettbewerbe ohne Frauen-Wettbewerbe, auf Wunsch nur der genannte."""
    competitions = snapshot.season.competitions
    chosen = tuple(k for k in settings.competitions if k in competitions)
    valid = tuple(k for k in chosen if not competitions[k].women)  # Frauen nur über das Spiele-Werkzeug
    wanted = given(text)
    if wanted is not None:
        names = {normalize(k): k for k in competitions} | {normalize(c.name): k for k, c in competitions.items()}
        key = names.get(normalize(str(wanted)))
        if key is None:
            raise ToolError(
                ErrorKind.INVALID_INPUT,
                f"Den Wettbewerb „{wanted}“ gibt es nicht. Gültig sind: {', '.join(valid)}.",
            )
        if key not in chosen:
            raise ToolError(
                ErrorKind.INVALID_INPUT,
                f"Der Wettbewerb „{competitions[key].name}“ ist nicht ausgewählt. Gültig sind: {', '.join(valid)}.",
            )
        if competitions[key].women:
            raise ToolError(
                ErrorKind.UNSUPPORTED,
                "Frauenteams kann ich über den Vereinsnamen nicht abfragen. "
                "Frag nach den Spielen der Frauen-Bundesliga, dann zeige ich sie dir.",
            )
        return (key,)
    return valid


def _national_team(snapshot: Snapshot, competition: Competition) -> Team:
    """Die Nationalmannschaft als Team, mit dem Namen aus den Spieldaten."""
    ids = set(competition.national_team_ids.values())
    for match in snapshot.matches.get(competition.key, ()):
        for team in (match.home, match.away):
            if team.id in ids:
                return team
    return Team(next(iter(ids), 0), competition.name, competition.name, None)


def _past_order(snapshot: Snapshot, match: Match) -> tuple[int, float]:
    """Laufendes Spiel zuerst, dann das jüngste."""
    running = match_state(match, snapshot.now) == LIVE
    return (0 if running else 1, -match.kickoff.timestamp())


def _opponent_filter(
    snapshot: Snapshot, index: TeamIndex, keys: Sequence[str], national: bool, text: Any, own: frozenset[int]
) -> tuple[Callable[[Match], bool] | None, dict[str, Any] | None, str]:
    """Prüfung "Gegner ist der genannte Verein" oder, bei Mehrdeutigem, die Rückfrage; Unbekanntes wirft.

    Dazu der Name des Gegners für Sätze: der aufgelöste Verein, sonst die Eingabe in Anführungszeichen.
    """

    def other(match: Match) -> Team:
        return match.away if match.home.id in own else match.home

    quoted = f"„{text}“"
    if national:  # Gegner der Nationalmannschaft stehen nicht im Index: Namen vergleichen
        words = set(normalize(str(text)).split())
        return (lambda match: bool(words) and words <= set(normalize(other(match).name).split())), None, quoted
    found = index.resolve(text)
    if found.candidates:
        return None, ambiguous_payload(found, text), quoted
    if found.national:  # kein Verein spielt gegen die Nationalmannschaft
        return (lambda match: False), None, quoted
    if found.team is None:
        raise unknown_team_error(snapshot, keys, text)
    wanted = index.ids_of(found.team)
    return (lambda match: other(match).id in wanted), None, found.team.name


def answer(
    snapshot: Snapshot,
    index: TeamIndex,
    settings: Settings,
    *,
    verein: str | None,
    richtung: str,
    anzahl: int,
    gegner: str | None,
    wettbewerb: str | None,
    nur_heimspiele: bool,
) -> dict[str, Any]:
    """Letzte und/oder nächste Spiele eines Vereins; wirft nur `ToolError`.

    Reihenfolge der Prüfung: Eingaben, Wettbewerbe (`guard`), Verein auflösen. Ist der Verein
    mehrdeutig oder nicht bekannt, kommt eine Rückfrage (ohne `stand`, sie enthält keine Daten)
    oder ein Fehler. Bei `richtung` "letzte" oder "naechste" steht nur die jeweilige Liste im Ergebnis.
    """
    direction = _check_direction(richtung)
    count = _check_count(anzahl)
    scope = _scope(snapshot, settings, wettbewerb)
    guard(snapshot, scope)

    competitions = snapshot.season.competitions
    national_keys = tuple(k for k in scope if competitions[k].kind is Kind.NATIONAL)
    club_keys = tuple(k for k in scope if k not in national_keys)
    # Ob ein Verein auffindbar ist, hängt von allen gewählten Vereinswettbewerben ab, nicht nur vom Ausschnitt.
    searchable = searchable_keys(snapshot, settings)

    text = given(verein)
    national, team, resolution = False, None, None
    if text is None:
        if settings.main_team is None:
            return question_payload("Welchen Verein meinst du? Du hast noch keinen Hauptverein eingestellt.", [])
        team = index.by_id(settings.main_team)
        if team is None:
            raise loading_error(snapshot, searchable, "deinen Hauptverein") or ToolError(
                ErrorKind.UNKNOWN_TEAM,
                "Dein Hauptverein kommt in keinem der ausgewählten Wettbewerbe vor. Prüfe bitte die Einstellungen.",
            )
    else:
        resolution = index.resolve(text)
        if resolution.candidates:
            return ambiguous_payload(resolution, text)
        national, team = resolution.national, resolution.team
        if not national and team is None:
            raise unknown_team_error(snapshot, searchable, text)

    relevant = national_keys if national else club_keys
    if not relevant:
        raise _no_such_games(national, given(wettbewerb) is not None)
    usable = guard(snapshot, relevant)

    if national:
        national_competition = competitions[national_keys[0]]
        subject = _national_team(snapshot, national_competition)
        ids = frozenset(national_competition.national_team_ids.values())
    else:
        subject = team
        ids = index.ids_of(team)

    is_opponent = None
    opponent_text = given(gegner)
    opponent_name = None
    if opponent_text is not None:
        is_opponent, question, opponent_name = _opponent_filter(snapshot, index, searchable, national, opponent_text, ids)
        if question is not None:
            return question

    def pick(source: Mapping[str, tuple[Match, ...]]) -> list[tuple[Match, Competition]]:
        found = []
        for key in usable:
            competition = competitions[key]
            for match in source.get(key, ()):
                if not {match.home.id, match.away.id} & ids:
                    continue
                if is_opponent is not None and not is_opponent(match):
                    continue
                if nur_heimspiele and (neutral_ground(competition, match) or match.home.id not in ids):
                    continue
                found.append((match, competition))
        return found

    now = snapshot.now
    candidates = pick(snapshot.matches)
    past = sorted((c for c in candidates if match_state(c[0], now) in _PAST), key=lambda c: _past_order(snapshot, c[0]))
    coming = sorted(
        (c for c in candidates if match_state(c[0], now) == SCHEDULED), key=lambda c: (c[0].kickoff, c[0].id)
    )

    hints: list[str] = []
    past_source = snapshot.matches
    # Sonst fehlen vielleicht Spiele, und "keins" wäre falsch. Ungültige Wettbewerbe können geladen sein
    # (der Bestand führt sie als geladen), fallen aber aus `usable`.
    complete = set(usable) == set(relevant) and all(k in snapshot.loaded for k in usable)
    if direction != "naechste" and not past and opponent_text is None and complete:
        # Noch kein Spiel der neuen Saison gespielt: das letzte Spiel kommt aus der Vorsaison.
        older = sorted(
            (c for c in pick(snapshot.previous_matches) if match_state(c[0], now) == FINISHED),
            key=lambda c: _past_order(snapshot, c[0]),
        )
        if older:
            past, past_source = older, snapshot.previous_matches
            label = season_label(older[0][0].season) if older[0][0].season else "der Vorsaison"
            hints.append(
                f"Für die neue Saison gibt es noch kein beendetes Spiel; das letzte Spiel ist aus der Saison {label}."
            )

    result: dict[str, Any] = {"verein": subject.name, "stand": stand_phrase(snapshot, usable)}
    shown: list[tuple[Match, dict[str, Any], Competition]] = []
    for key, wanted, chosen, source in (
        ("letzte", "naechste", past, past_source),
        ("naechste", "letzte", coming, snapshot.matches),
    ):
        if direction == wanted:
            continue
        built = [
            (
                match,
                match_entry(
                    match,
                    snapshot,
                    team=subject,
                    competition=competition,
                    same_round=same_round_matches(match, source.get(competition.key, ())),
                    ids=ids,
                ),
                competition,
            )
            for match, competition in chosen[:count]
        ]
        result[key] = [entry for _, entry, _ in built]
        shown += built

    for match, entry, _ in shown:
        hints += match_hints(match, entry)
    hints += live_hints(snapshot, shown)
    hints += _empty_hints(
        snapshot, result, subject, ids, national, usable, opponent_name, wettbewerb, nur_heimspiele, complete, candidates
    )
    hints += season_hints(snapshot, usable, upcoming=direction != "letzte")
    hints += loading_hints(snapshot, relevant)
    # Der Hinweis auf Auslandsvereine stimmt erst, wenn jeder inländische Wettbewerb, in dem ein Verein vorkommen
    # kann, brauchbar geladen ist; ein eingeengter `wettbewerb` ändert daran nichts.
    domestic = [k for k in searchable if competitions[k].kind is not Kind.EUROPE]
    if resolution is not None and resolution.foreign and len(available(snapshot, domestic)) == len(domestic):
        hints.append(f"{subject.name} ist ein ausländischer Verein; von ihm sind nur Europapokalspiele bekannt.")
    result["hinweise"] = unique(hints)
    return result


def _no_such_games(national: bool, competition_given: bool) -> ToolError:
    if not national:
        return ToolError(
            ErrorKind.UNSUPPORTED,
            "Im Wettbewerb Nationalmannschaft spielen keine Vereine. Frag nach „Deutschland“, dann zeige ich die Länderspiele.",
        )
    if competition_given:
        return ToolError(ErrorKind.UNSUPPORTED, "Die Nationalmannschaft spielt nur im Wettbewerb Nationalmannschaft.")
    return ToolError(
        ErrorKind.UNKNOWN_TEAM, "Die Nationalmannschaft habe ich nicht. Sie gehört nicht zu den ausgewählten Wettbewerben."
    )


def _in(competition: Competition) -> str:
    """"im DFB-Pokal", "in der Champions League"."""
    return f"im {competition.name}" if competition.kind is Kind.CUP else f"in der {competition.name}"


def _all_entered(snapshot: Snapshot, key: str, cup: Competition) -> bool:
    """Stehen im Pokal oder Europapokal sicher alle Teilnehmer der Saison in den Spieldaten?

    Zu Saisonbeginn ist die Liste leer oder nur teilweise eingetragen; sicher ist sie erst, wenn ein Spiel
    beendet ist. Im DFB-Pokal spielen alle Vereine in der ersten Runde. Im Europapokal beginnen die Daten mit
    der Ligaphase, in deren erstem Spieltag alle Teilnehmer spielen; zusätzlich muss in der ersten Runde der
    Daten ein Spiel beendet sein und darf kein Verein erst in einer späteren Runde auftauchen (das wäre ein
    Einstieg nach einer Qualifikation, dann steht die Teilnehmerliste nicht fest).
    """
    if not season_started(snapshot, key):
        return False
    if cup.kind is not Kind.EUROPE:
        return True
    matches = snapshot.matches.get(key, ())
    first = min(m.round_order for m in matches)
    opening = [m for m in matches if m.round_order == first]
    if not any(match_state(m, snapshot.now) == FINISHED for m in opening):
        return False
    entered = {team for m in opening for team in (m.home.id, m.away.id)}
    return all(m.home.id in entered and m.away.id in entered for m in matches)


def _cup_status(snapshot: Snapshot, subject: Team, ids: frozenset[int], cup: Competition, matches: Sequence[Match]) -> str:
    """Warum im Pokal oder Europapokal kein nächstes Spiel eingetragen ist (die Daten sind vollständig).

    Im Pokal entscheidet ein Spiel: Ist das letzte beendet und verloren (auch nach Verlängerung oder im
    Elfmeterschießen), ist der Verein ausgeschieden; gewonnen heißt, die nächste Runde ist noch nicht angesetzt.
    Im Europapokal sagt ein einzelnes Ergebnis nichts Sicheres (Ligaphase, Hin- und Rückspiel).
    """
    now = snapshot.now
    uncertain = (
        f"Im Wettbewerb {cup.name} ist für {subject.name} noch nicht angesetzt, wer der nächste Gegner ist:"
        " Die nächste Runde ist vielleicht noch nicht ausgelost, oder der Verein ist ausgeschieden."
    )
    if cup.kind is not Kind.CUP or any(match_state(m, now) in (LIVE, NOT_FINISHED) for m in matches):
        return uncertain
    finished = [m for m in matches if match_state(m, now) == FINISHED]
    if not finished:
        return uncertain
    last = max(finished, key=lambda m: (m.kickoff, m.id))
    if last.result_unclear or last.final is None or (last.home.id in ids) == (last.away.id in ids):
        return uncertain
    own, other = (last.final.home, last.final.away) if last.home.id in ids else (last.final.away, last.final.home)
    if own < other:
        return f"{subject.name} ist {_in(cup)} ausgeschieden."
    if own > other:
        return "Die nächste Runde ist noch nicht angesetzt."
    return uncertain


def _empty_hints(
    snapshot: Snapshot,
    result: Mapping[str, Any],
    subject: Team,
    ids: frozenset[int],
    national: bool,
    usable: Sequence[str],
    opponent: str | None,
    wettbewerb: Any,
    only_home: bool,
    complete: bool,
    candidates: Sequence[tuple[Match, Competition]],
) -> list[str]:
    """Sätze, die erklären, warum eine Liste leer ist.

    Sind nicht alle Wettbewerbe geladen und erreichbar, fehlen vielleicht Spiele; dann steht nur, dass
    in den bisher geladenen Daten keins ist, nie ein sicheres "kein Spiel". `candidates` sind alle Spiele
    des Vereins in `usable` nach den Filtern der Frage; `opponent` ist der Name des gefragten Gegners.
    """
    hints: list[str] = []
    competitions = snapshot.season.competitions
    ended = any(k in snapshot.ended_season for k in usable)
    cups = [competitions[k] for k in usable if competitions[k].kind in (Kind.CUP, Kind.EUROPE)]
    cup = cups[0] if given(wettbewerb) is not None and len(cups) == 1 and len(usable) == 1 else None
    for key, label in (("letzte", "gespielt"), ("naechste", "angesetzt")):
        if key not in result or result[key]:
            continue
        if not complete:
            hints.append(f"In den bisher geladenen Daten ist für {subject.name} kein Spiel {label}.")
        elif only_home:
            hints.append(f"Es ist kein Heimspiel {label}; nur Spiele als Gastgeber zählen als Heimspiel, Endspiele und Länderspiele nicht.")
        elif opponent is not None:
            hints.append(f"Gegen {opponent} ist in dieser Saison kein Spiel {label}.")
        elif key == "naechste" and national:
            hints.append("Für die Nationalmannschaft ist noch kein Termin eingetragen.")
        elif key == "naechste" and ended:
            continue  # season_hints sagt es
        elif cup is not None and not ended and not candidates:
            if _all_entered(snapshot, usable[0], cup):
                hints.append(f"{subject.name} spielt in dieser Saison nicht {_in(cup)}.")
            else:  # Liste noch leer oder nicht vollständig eingetragen: nichts Sicheres
                hints.append(f"Für {subject.name} ist {_in(cup)} noch kein Spiel {label}.")
        elif key == "naechste" and cup is not None:
            hints.append(_cup_status(snapshot, subject, ids, cup, [m for m, _ in candidates]))
        else:
            where = f"im Wettbewerb {join_names(competitions[k].name for k in usable)}" if given(wettbewerb) else "in den ausgewählten Wettbewerben"
            hints.append(f"Für {subject.name} ist {where} kein Spiel {label}.")
    return hints
