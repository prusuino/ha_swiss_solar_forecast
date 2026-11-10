"""How well the forecast did: the figures over the last days, with known numbers."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from custom_components.swiss_solar_forecast.accuracy import Accuracy

HOUR = timedelta(hours=1)
FIRST = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def _utc_date(when: datetime) -> date:
    return when.date()


def _sun(hour: int, wh: float) -> float:
    return wh if 8 <= hour < 18 else 0.0


def _day(acc: Accuracy, day: datetime, ahead_wh: float, last_wh: float, measured_wh: float, hours: int = 24, shift: int = 0) -> None:
    """One day: forecast at noon the day before, forecast again at midnight, then the meter hour by hour."""
    starts = [day + i * HOUR for i in range(24)]
    acc.record([(start, _sun(start.hour, ahead_wh)) for start in starts], day - 12 * HOUR, _utc_date)
    acc.record([(start, _sun(start.hour + shift, last_wh)) for start in starts], day, _utc_date)
    for start in starts[:hours]:
        acc.measured(start, _sun(start.hour, measured_wh), _utc_date)


def test_no_days_no_figures() -> None:
    figures = Accuracy().figures(date(2026, 6, 10), 7)
    assert figures["day_ahead_accuracy"] is None and figures["same_day_accuracy"] is None and figures["bias"] is None
    assert figures["days"] == 0 and figures["measured_kwh"] == 0.0


def test_a_week_of_days_ten_percent_off() -> None:
    acc = Accuracy(fingerprint="a")
    for i in range(8):
        _day(acc, FIRST + timedelta(days=i), ahead_wh=1100.0, last_wh=1050.0, measured_wh=1000.0)
    today = (FIRST + timedelta(days=8)).date()
    week = acc.figures(today, 7)
    assert week == {
        "day_ahead_accuracy": 90.0,
        "same_day_accuracy": 95.0,
        "hourly_accuracy": 95.0,
        "bias": 5.0,
        "days": 7,
        "days_day_ahead": 7,
        "forecast_kwh": 73.5,
        "measured_kwh": 70.0,
    }
    assert acc.figures(today, 30)["days"] == 8
    assert not acc.pending and not acc.ahead  # every hour was closed
    # the running day does not count
    assert acc.figures(today - timedelta(days=1), 7)["days"] == 7


def test_errors_of_different_days_do_not_cancel_out() -> None:
    acc = Accuracy()
    _day(acc, FIRST, ahead_wh=1200.0, last_wh=1200.0, measured_wh=1000.0)
    _day(acc, FIRST + timedelta(days=1), ahead_wh=800.0, last_wh=800.0, measured_wh=1000.0)
    figures = acc.figures((FIRST + timedelta(days=2)).date(), 7)
    assert figures["day_ahead_accuracy"] == 80.0 and figures["bias"] == 0.0


def test_the_hourly_figure_is_stricter() -> None:
    acc = Accuracy()
    # the right energy, an hour late
    _day(acc, FIRST, ahead_wh=1000.0, last_wh=1000.0, measured_wh=1000.0, shift=-1)
    figures = acc.figures((FIRST + timedelta(days=1)).date(), 7)
    assert figures["same_day_accuracy"] == 100.0
    assert figures["hourly_accuracy"] == 80.0  # two of ten hours missed


def test_a_day_with_a_gap_does_not_count() -> None:
    acc = Accuracy()
    _day(acc, FIRST, ahead_wh=1000.0, last_wh=1000.0, measured_wh=1000.0, hours=12)
    assert acc.figures((FIRST + timedelta(days=1)).date(), 7)["days"] == 0
    assert acc.measured(FIRST - HOUR, 500.0, _utc_date) is False  # an hour nobody forecast


def test_what_was_kept_belongs_to_one_plant() -> None:
    acc = Accuracy(fingerprint="a")
    _day(acc, FIRST, ahead_wh=1100.0, last_wh=1050.0, measured_wh=1000.0)
    acc.record([(FIRST + timedelta(days=2), 500.0)], FIRST + timedelta(days=1), _utc_date)
    back = Accuracy.from_dict(acc.as_dict(), "a")
    assert back == acc and back.pending and back.ahead
    assert Accuracy.from_dict(acc.as_dict(), "b") == Accuracy(fingerprint="b")
    for broken in (None, "x", {"fingerprint": "a", "days": {"2026-06-01": ["x"]}}, {"fingerprint": "a", "pending": 5}):
        assert Accuracy.from_dict(broken, "a") == Accuracy(fingerprint="a")


def test_old_days_and_forgotten_hours_go() -> None:
    acc = Accuracy()
    for i in range(45):
        _day(acc, FIRST + timedelta(days=i), ahead_wh=1000.0, last_wh=1000.0, measured_wh=1000.0)
    assert len(acc.days) == 41
    # hours the meter never closed are dropped after two days
    acc.record([(FIRST + timedelta(days=50), 500.0)], FIRST + timedelta(days=49), _utc_date)
    acc.record([(FIRST + timedelta(days=60), 500.0)], FIRST + timedelta(days=59), _utc_date)
    assert list(acc.pending) == [(FIRST + timedelta(days=60)).isoformat()] and list(acc.ahead) == list(acc.pending)
