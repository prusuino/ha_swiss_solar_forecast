"""The self-calibration: what is learned from the meter, how cautiously,
what is skipped, and what survives a restart."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.calibration import Calibration, energy_wh

DAY = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)  # the hour 10 to 11 UTC
HOUR = timedelta(hours=1)
MIN_WH, MAX_WH = 200.0, 13000.0


def _one_hour(cal: Calibration, start: datetime, forecast_wh: float, measured_wh: float, meter_wh: float = 50000.0) -> bool:
    cal.record_forecast([(start, forecast_wh)], start)
    cal.meter_read(start, meter_wh, MIN_WH, MAX_WH)
    return cal.meter_read(start + HOUR, meter_wh + measured_wh, MIN_WH, MAX_WH)


def test_meter_units() -> None:
    assert energy_wh("1.5", "kWh") == 1500.0
    assert energy_wh("12", "Wh") == 12.0
    assert energy_wh(0.002, "MWh") == 2000.0
    assert energy_wh("unavailable", "kWh") is None
    assert energy_wh("5", "W") is None
    assert energy_wh("nan", "kWh") is None
    assert energy_wh("5", None) is None


def test_fresh_calibration_changes_nothing() -> None:
    cal = Calibration(fingerprint="a")
    assert cal.overall == 1.0 and cal.factor(10) == 1.0 and cal.factors() == {} and cal.samples == 0


def test_one_sample_moves_the_factor_only_a_little() -> None:
    cal = Calibration()
    assert _one_hour(cal, DAY, 4000.0, 2000.0)  # half of what was forecast
    assert cal.samples == 1 and cal.last_sample == DAY.isoformat()
    assert 0.94 < cal.overall < 0.98
    assert 0.80 < cal.factor(10) < 0.88
    assert cal.factor(14) == cal.overall  # an hour without evidence follows the plant


def test_three_weeks_of_evidence_stand_alone() -> None:
    cal = Calibration()
    for day in range(21):
        start = DAY + timedelta(days=day)
        # a tree shades the plant until 11 UTC; the rest of the day is as forecast
        assert _one_hour(cal, start, 4000.0, 1200.0)
        for later in range(1, 6):
            assert _one_hour(cal, start + later * HOUR, 5000.0, 5000.0)
    assert abs(cal.factor(10) - 0.3) < 0.01
    assert abs(cal.factor(12) - 1.0) < 0.01
    assert 0.85 < cal.overall < 0.92  # energy-weighted over the six hours
    assert cal.factors()[10] == round(cal.factor(10), 3) and list(cal.factors()) == [10, 11, 12, 13, 14, 15]


def test_old_samples_fade() -> None:
    cal = Calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 2000.0)
    for day in range(30, 60):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 4000.0)  # the tree was felled
    assert cal.factor(10) > 0.93


def test_factors_stay_within_bounds() -> None:
    cal = Calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 1000.0, 9000.0)
    assert cal.factor(10) == 1.5
    cal = Calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 9000.0, 10.0)
    assert cal.factor(10) == 0.1


def test_hours_that_teach_nothing_are_skipped() -> None:
    cal = Calibration()
    assert not _one_hour(cal, DAY, 100.0, 80.0)  # next to nothing forecast
    assert not _one_hour(cal, DAY + HOUR, 4000.0, 0.0)  # the meter stood still: outage or snow
    assert not _one_hour(cal, DAY + 2 * HOUR, 4000.0, -30000.0)  # a daily counter went back to zero
    assert not _one_hour(cal, DAY + 3 * HOUR, 4000.0, 20000.0)  # more than the plant can make
    assert cal.samples == 0 and cal.bins == {}
    # no forecast kept for the hour
    cal.meter_read(DAY + 5 * HOUR, 1000.0, MIN_WH, MAX_WH)
    assert not cal.meter_read(DAY + 6 * HOUR, 3000.0, MIN_WH, MAX_WH)


def test_a_gap_in_the_readings_is_no_hour() -> None:
    cal = Calibration()
    cal.record_forecast([(DAY + i * HOUR, 4000.0) for i in range(4)], DAY)
    cal.meter_read(DAY, 1000.0, MIN_WH, MAX_WH)
    assert not cal.meter_read(DAY + 2 * HOUR, 9000.0, MIN_WH, MAX_WH)  # two hours in one difference
    assert cal.meter_read(DAY + 3 * HOUR, 12000.0, MIN_WH, MAX_WH)  # the next full hour counts again
    cal.meter_read(DAY + 4 * HOUR, None, MIN_WH, MAX_WH)  # the meter was unavailable
    cal.record_forecast([(DAY + 4 * HOUR, 4000.0)], DAY + 4 * HOUR)
    assert not cal.meter_read(DAY + 5 * HOUR, 15000.0, MIN_WH, MAX_WH)
    assert cal.samples == 1


def test_forecast_of_a_finished_hour_is_kept_and_old_ones_go() -> None:
    cal = Calibration()
    cal.record_forecast([(DAY, 4000.0), (DAY + HOUR, 5000.0)], DAY + timedelta(minutes=20))
    cal.record_forecast([(DAY, 1000.0), (DAY + HOUR, 5500.0)], DAY + HOUR + timedelta(minutes=5))  # a newer run, the first hour is over
    assert cal.log[DAY.isoformat()] == 4000.0
    assert cal.log[(DAY + HOUR).isoformat()] == 5500.0
    cal.record_forecast([], DAY + timedelta(days=3))
    assert cal.log == {}


def test_what_was_learned_survives_and_belongs_to_one_plant() -> None:
    cal = Calibration(fingerprint="meter|12|10,180,30")
    for day in range(5):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 3000.0)
    kept = cal.as_dict()
    again = Calibration.from_dict(kept, "meter|12|10,180,30")
    assert again.samples == 5 and abs(again.factor(10) - cal.factor(10)) < 1e-3 and again.log == cal.log
    assert again.reading_time == cal.reading_time and again.reading_wh == cal.reading_wh
    other = Calibration.from_dict(kept, "meter|12|12,180,30")  # the plane was changed
    assert other.samples == 0 and other.bins == {} and other.fingerprint == "meter|12|12,180,30"
    assert Calibration.from_dict(None, "x").fingerprint == "x"
    assert Calibration.from_dict({"fingerprint": "x", "bins": {"ten": [1, 2, 3]}}, "x").bins == {}
