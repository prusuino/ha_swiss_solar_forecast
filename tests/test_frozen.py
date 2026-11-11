"""The hours that are over: kept as they were forecast when they ended,
what the meter counted beside them, and what survives a restart."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.frozen import KEEP, Frozen

HOUR = timedelta(hours=1)
START = datetime(2026, 6, 21, 10, 0, tzinfo=UTC)  # the hour 10 to 11 UTC


def test_a_running_hour_follows_the_forecast() -> None:
    frozen = Frozen()
    first = [4000.0, 2500.0, 1500.0, 4200.0]
    assert frozen.settle(START, first, START + timedelta(minutes=15)) is first
    # a newer run sees less: the hour is still running and takes it
    second = [3000.0, 1800.0, 1200.0, 3100.0]
    assert frozen.settle(START, second, START + timedelta(minutes=45)) is second
    assert frozen.hours == {START.isoformat(): second}


def test_a_finished_hour_keeps_what_it_ended_with() -> None:
    frozen = Frozen()
    ended = [3000.04, 1800.0, 1200.0, 3100.0]
    frozen.settle(START, ended, START + timedelta(minutes=55))
    # the hour is over: later runs compute it again, with weather that never came
    for minutes, later in ((60, [500.0, 300.0, 200.0, 520.0]), (300, [9000.0, 5000.0, 4000.0, 9100.0])):
        kept = frozen.settle(START, later, START + timedelta(minutes=minutes))
        assert kept == [3000.0, 1800.0, 1200.0, 3100.0] and kept is not later  # as kept, rounded
    # the hours around it are untouched
    following = [100.0, 60.0, 40.0, 100.0]
    assert frozen.settle(START + HOUR, following, START + timedelta(minutes=70)) is following


def test_a_finished_hour_seen_for_the_first_time_takes_what_is_forecast_now() -> None:
    frozen = Frozen()
    values = [3000.0, 3000.0, 3100.0]
    assert frozen.settle(START, values, START + 5 * HOUR) is values
    assert frozen.settle(START, [1.0, 1.0, 1.0], START + 6 * HOUR) == values
    # another number of planes: what was kept no longer fits
    planes = [3000.0, 2000.0, 1000.0, 3100.0]
    assert frozen.settle(START, planes, START + 7 * HOUR) is planes


def test_what_the_meter_counted() -> None:
    frozen = Frozen()
    frozen.measure(START, 2345.67)
    frozen.measure(START + HOUR, 0.0)  # an hour without sun counts too
    assert frozen.measured_hours() == {START: 2345.7, START + HOUR: 0.0}
    # read a little after the full hour, kept under the hour
    frozen.measure(START + timedelta(minutes=3), 2000.0)
    assert frozen.measured_hours()[START] == 2000.0 and len(frozen.measured) == 2


def test_old_hours_go() -> None:
    frozen = Frozen()
    for index in range(72):
        frozen.settle(START + index * HOUR, [1000.0, 1000.0], START)
        frozen.measure(START + index * HOUR, 900.0)
    now = START + 72 * HOUR
    frozen.prune(now)
    assert len(frozen.hours) == 48 and len(frozen.measured) == 48
    assert min(frozen.hours) == (now - KEEP).isoformat() == min(frozen.measured)


def test_what_was_kept_belongs_to_one_plant() -> None:
    frozen = Frozen(fingerprint="a")
    frozen.settle(START, [3000.0, 1800.0, 1200.0, 3100.0], START + HOUR)
    frozen.measure(START, 2500.0)
    back = Frozen.from_dict(frozen.as_dict(), "a")
    assert back == frozen
    assert back.settle(START, [1.0, 1.0, 1.0, 1.0], START + 2 * HOUR) == [3000.0, 1800.0, 1200.0, 3100.0]
    assert Frozen.from_dict(frozen.as_dict(), "b") == Frozen(fingerprint="b")  # the plant was changed
    for broken in (None, "x", {"fingerprint": "a", "hours": {"then": [1.0]}}, {"fingerprint": "a", "hours": 5}, {"fingerprint": "a", "measured": {START.isoformat(): "much"}}):
        assert Frozen.from_dict(broken, "a") == Frozen(fingerprint="a")
