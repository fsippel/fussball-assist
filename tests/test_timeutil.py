"""Deutsche Zeitangaben, Zeiträume und Platzhalter-Uhrzeiten."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.fussball_assist.parse import parse_matches
from custom_components.fussball_assist.timeutil import (
    PERIODS,
    clock_phrase,
    countdown_phrase,
    day_phrase,
    local_day,
    parse_day,
    range_phrase,
    relative_phrase,
    resolve_period,
    time_confirmed,
    when_phrase,
)

from .conftest import load_fixture

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 17, 16, 40, tzinfo=BERLIN)  # Dienstag


def test_when_phrase():
    moment = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    assert when_phrase(moment, BERLIN, with_time=True) == "Samstag, 21. November, 13:00 Uhr"


def test_when_phrase_without_time():
    moment = datetime(2027, 1, 31, 12, 30, tzinfo=UTC)
    assert when_phrase(moment, BERLIN, with_time=False) == "Sonntag, 31. Januar"


def test_phrases_have_no_leading_zero_on_the_day_but_on_the_time():
    moment = datetime(2026, 5, 1, 7, 5, tzinfo=UTC)  # 09:05 Uhr Ortszeit
    assert when_phrase(moment, BERLIN, with_time=True) == "Freitag, 1. Mai, 09:05 Uhr"
    assert clock_phrase(moment, BERLIN) == "09:05 Uhr"
    assert day_phrase(date(2026, 11, 21)) == "Samstag, 21. November"


def test_all_weekday_and_month_names():
    days = [day_phrase(date(2026, 11, 16) + timedelta(days=i)).split(",")[0] for i in range(7)]
    assert days == ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    months = [day_phrase(date(2026, m, 1)).split(", ")[1] for m in range(1, 13)]
    assert months == [
        "1. Januar", "1. Februar", "1. März", "1. April", "1. Mai", "1. Juni",
        "1. Juli", "1. August", "1. September", "1. Oktober", "1. November", "1. Dezember",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("day", "phrase"),
    [
        (date(2026, 11, 17), "heute"),
        (date(2026, 11, 18), "morgen"),
        (date(2026, 11, 19), "übermorgen"),
        (date(2026, 11, 16), "gestern"),
        (date(2026, 11, 15), "vorgestern"),
        (date(2026, 11, 21), "in 4 Tagen"),
        (date(2026, 11, 8), "vor 9 Tagen"),
    ],
)
def test_relative_phrase(day, phrase):
    assert relative_phrase(day, NOW.date()) == phrase


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("heute", (date(2026, 11, 17), date(2026, 11, 17))),
        ("gestern", (date(2026, 11, 16), date(2026, 11, 16))),
        ("morgen", (date(2026, 11, 18), date(2026, 11, 18))),
        ("kommendes_wochenende", (date(2026, 11, 20), date(2026, 11, 22))),
        ("letztes_wochenende", (date(2026, 11, 13), date(2026, 11, 15))),
    ],
)
def test_resolve_period_on_a_tuesday(period, expected):
    assert resolve_period(period, NOW, BERLIN) == expected


def test_running_weekend_counts_as_the_coming_one():
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=BERLIN)
    assert resolve_period("kommendes_wochenende", saturday, BERLIN) == (
        date(2026, 11, 20),
        date(2026, 11, 22),
    )
    assert resolve_period("letztes_wochenende", saturday, BERLIN) == (
        date(2026, 11, 13),
        date(2026, 11, 15),
    )


@pytest.mark.parametrize(
    ("today", "coming", "last"),
    [
        (date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 13)),  # Donnerstag
        (date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 13)),  # Freitag
        (date(2026, 11, 22), date(2026, 11, 20), date(2026, 11, 13)),  # Sonntag
        (date(2026, 11, 23), date(2026, 11, 27), date(2026, 11, 20)),  # Montag
    ],
)
def test_weekend_boundaries(today, coming, last):
    now = datetime(today.year, today.month, today.day, 12, 0, tzinfo=BERLIN)
    assert resolve_period("kommendes_wochenende", now, BERLIN)[0] == coming
    assert resolve_period("letztes_wochenende", now, BERLIN)[0] == last
    assert resolve_period("kommendes_wochenende", now, BERLIN)[1] == coming + timedelta(days=2)


def test_day_is_local_not_utc():
    just_after_midnight = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)  # 00:30 Uhr am 21. in Berlin
    assert resolve_period("heute", just_after_midnight, BERLIN) == (
        date(2026, 11, 21),
        date(2026, 11, 21),
    )
    assert local_day(datetime(2026, 11, 20, 19, 30, tzinfo=UTC), BERLIN) == date(2026, 11, 20)
    assert local_day(datetime(2027, 3, 27, 23, 30, tzinfo=UTC), BERLIN) == date(2027, 3, 28)
    assert local_day(datetime(2027, 3, 28, 22, 30, tzinfo=UTC), BERLIN) == date(2027, 3, 29)


def test_unknown_period_raises_value_error():
    with pytest.raises(ValueError) as error:
        resolve_period("am Samstag", NOW, BERLIN)
    assert all(period in str(error.value) for period in PERIODS)


@pytest.mark.parametrize(
    ("delta", "phrase"),
    [
        (timedelta(days=3, hours=2), "in 3 Tagen"),
        (timedelta(hours=5, minutes=10), "in 5 Stunden"),
        (timedelta(minutes=20), "in 20 Minuten"),
        (timedelta(hours=1, minutes=30), "in 1 Stunde"),
        (timedelta(minutes=1, seconds=30), "in 1 Minute"),
        (timedelta(hours=30), "in 1 Tag"),
        (timedelta(seconds=40), "jetzt"),
        (timedelta(minutes=-5), "jetzt"),
    ],
)
def test_countdown_phrase(delta, phrase):
    assert countdown_phrase(NOW + delta, NOW) == phrase


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-08", date(2026, 11, 8)),
        ("morgen", None),
        ("2026-11-8", None),
        ("21.11.2026", None),
        ("2026-13-01", None),
        ("2026-02-30", None),
        ("", None),
        (" 2026-11-08", None),
        ("2026-11-08T10:00", None),
    ],
)
def test_parse_day(text, expected):
    assert parse_day(text) == expected


def test_placeholder_kickoff_is_not_confirmed():
    md19 = [m for m in parse_matches(load_fixture("bl2_2026")) if m.round_order == 19]
    derby = next(m for m in md19 if m.id == 83874)
    assert time_confirmed(derby, md19, NOW) is False


def test_near_kickoff_is_confirmed():
    md10 = parse_matches(load_fixture("bl1_2026_md10"))
    assert all(time_confirmed(m, md10, NOW) for m in md10)


def _match():
    return parse_matches(load_fixture("bl1_2026_md10"))[0]


def test_time_confirmed_rules_in_order():
    base = _match()
    far = NOW.astimezone(UTC) + timedelta(days=60)
    fresh = far - timedelta(days=10)
    same = [replace(base, id=i, kickoff=far, last_update=fresh) for i in range(4)]

    # Regel 1: höchstens 14 Tage entfernt, auch wenn alles andere dagegen spricht
    near = replace(base, kickoff=NOW.astimezone(UTC) + timedelta(days=14), last_update=None)
    assert time_confirmed(near, [near], NOW) is True
    # Regel 2: keine oder alte Änderung
    assert time_confirmed(replace(same[0], last_update=None), same[:1], NOW) is False
    old = replace(same[0], last_update=far - timedelta(days=91))
    assert time_confirmed(old, [old], NOW) is False
    # Regel 3: vier Spiele mit genau derselben Zeit, mehr als 28 Tage entfernt
    assert time_confirmed(same[0], same, NOW) is False
    assert time_confirmed(same[0], same[:3], NOW) is True
    spread = same[:3] + [replace(same[3], kickoff=far + timedelta(hours=2))]
    assert time_confirmed(spread[0], spread, NOW) is True
    # Regel 3 gilt nur ab mehr als 28 Tagen Abstand
    mid = [replace(m, kickoff=NOW.astimezone(UTC) + timedelta(days=20)) for m in same]
    assert time_confirmed(mid[0], mid, NOW) is True


def test_countdown_across_spring_dst_with_same_tzinfo():
    now = datetime(2027, 3, 28, 0, 30, tzinfo=BERLIN)  # MEZ; Anstoß in echt 4 h 30 min später
    kickoff_utc = datetime(2027, 3, 28, 4, 0, tzinfo=UTC)
    assert countdown_phrase(kickoff_utc, now) == "in 4 Stunden"
    assert countdown_phrase(kickoff_utc.astimezone(BERLIN), now) == "in 4 Stunden"


def test_countdown_across_autumn_dst_with_same_tzinfo():
    now = datetime(2026, 10, 25, 0, 30, tzinfo=BERLIN)  # MESZ; Anstoß in echt 4 h 30 min später
    kickoff = datetime(2026, 10, 25, 4, 0, tzinfo=BERLIN)  # MEZ
    assert countdown_phrase(kickoff, now) == "in 4 Stunden"
    assert countdown_phrase(kickoff.astimezone(UTC), now) == "in 4 Stunden"


def test_local_day_in_the_autumn_dst_night():
    assert local_day(datetime(2026, 10, 24, 21, 30, tzinfo=UTC), BERLIN) == date(2026, 10, 24)
    assert local_day(datetime(2026, 10, 24, 22, 30, tzinfo=UTC), BERLIN) == date(2026, 10, 25)
    assert local_day(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), BERLIN) == date(2026, 10, 25)
    assert local_day(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), BERLIN) == date(2026, 10, 25)


def test_time_confirmed_distance_ignores_wall_clock_across_dst():
    now = datetime(2027, 3, 14, 12, 0, tzinfo=BERLIN)  # MEZ
    kickoff = datetime(2027, 3, 28, 12, 30, tzinfo=BERLIN)  # MESZ: in echt 13 Tage 23 h 30 min
    match = replace(_match(), kickoff=kickoff, last_update=None)
    assert time_confirmed(match, [match], now) is True


def test_rule_2_boundary_exactly_90_days_is_confirmed():
    base = _match()
    far = NOW.astimezone(UTC) + timedelta(days=60)
    exact = replace(base, kickoff=far, last_update=far - timedelta(days=90))
    assert time_confirmed(exact, [exact], NOW) is True
    older = replace(base, kickoff=far, last_update=far - timedelta(days=90, seconds=1))
    assert time_confirmed(older, [older], NOW) is False


def test_year_is_named_only_when_it_is_not_the_current_one():
    assert day_phrase(date(2026, 11, 22), current_year=2026) == "Sonntag, 22. November"
    assert day_phrase(date(2025, 11, 22), current_year=2026) == "Samstag, 22. November 2025"
    assert day_phrase(date(2025, 11, 22)) == "Samstag, 22. November"  # ohne Angabe nie mit Jahr
    moment = datetime(2027, 1, 31, 12, 30, tzinfo=UTC)
    assert when_phrase(moment, BERLIN, with_time=True, current_year=2026) == "Sonntag, 31. Januar 2027, 13:30 Uhr"
    assert when_phrase(moment, BERLIN, with_time=False, current_year=2027) == "Sonntag, 31. Januar"
    assert range_phrase(date(2026, 12, 28), date(2027, 1, 3), current_year=2026) == (
        "Montag, 28. Dezember bis Sonntag, 3. Januar 2027"
    )


def test_range_phrase_names_one_day_or_both_ends():
    assert range_phrase(date(2026, 11, 21), date(2026, 11, 21)) == "Samstag, 21. November"
    assert range_phrase(date(2026, 11, 20), date(2026, 11, 22)) == "Freitag, 20. November bis Sonntag, 22. November"
