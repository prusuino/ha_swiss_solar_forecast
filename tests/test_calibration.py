"""The self-calibration: what is learned from the meter and for which part
of the sky, how cautiously, what is skipped, the hours under low
stratus that are learned apart, and what survives a restart or comes from
an earlier version."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.calibration import BINS_BY, Calibration, energy_wh, sun_bin

LAT, LON = 46.9480, 7.4474  # Bern
# The hour 13 to 14 UTC in June: the sun stands in the south-west, above
# 40°, and stays in the same part of the sky for the weeks the tests cover.
DAY = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
MIN_WH, MAX_WH = 200.0, 13000.0
SOUTH_WEST = "225/40"


def _calibration(fingerprint: str = "") -> Calibration:
    return Calibration(fingerprint=fingerprint, lat=LAT, lon=LON)


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


def test_the_part_of_the_sky_of_an_hour() -> None:
    # bearing in steps of 15°, height in bands of 10°, by the lower edges
    assert sun_bin(DAY, LAT, LON) == SOUTH_WEST
    assert sun_bin(datetime(2026, 6, 1, 4, 0, tzinfo=UTC), LAT, LON) == "60/0"  # just risen in the north-east
    assert sun_bin(datetime(2026, 12, 21, 11, 0, tzinfo=UTC), LAT, LON) == "180/10"  # the low winter noon
    # everything above 40° is one band: nothing shades a sun that high
    assert sun_bin(datetime(2026, 6, 21, 11, 0, tzinfo=UTC), LAT, LON).endswith("/40")
    # the same hour of the day is another part of the sky in another season
    assert sun_bin(DAY.replace(month=12), LAT, LON) != SOUTH_WEST
    # at night the sun is in no band of its own
    assert sun_bin(datetime(2026, 6, 1, 23, 0, tzinfo=UTC), LAT, LON).endswith("/0")


def test_fresh_calibration_changes_nothing() -> None:
    cal = _calibration("a")
    assert cal.overall == 1.0 and cal.factor_at(DAY) == 1.0 and cal.factor(SOUTH_WEST) == 1.0
    assert cal.factors() == {} and cal.samples == 0


def test_one_sample_moves_the_factor_only_a_little() -> None:
    cal = _calibration()
    assert _one_hour(cal, DAY, 4000.0, 2000.0)  # half of what was forecast
    assert cal.samples == 1 and cal.last_sample == DAY.isoformat()
    assert cal.bin_of(DAY) == SOUTH_WEST and list(cal.bins) == [SOUTH_WEST]
    assert 0.94 < cal.overall < 0.98
    assert 0.80 < cal.factor_at(DAY) < 0.88
    assert cal.factor_at(DAY + 2 * HOUR) == cal.overall  # a part of the sky without evidence follows the plant


def test_three_weeks_of_evidence_stand_alone() -> None:
    cal = _calibration()
    for day in range(21):
        start = DAY + timedelta(days=day)
        # a tree shades the plant while the sun is in the south-west; the rest of the afternoon is as forecast
        assert _one_hour(cal, start, 4000.0, 1200.0)
        for later in range(1, 4):
            assert _one_hour(cal, start + later * HOUR, 5000.0, 5000.0)
    assert abs(cal.factor_at(DAY) - 0.3) < 0.01
    assert abs(cal.factor_at(DAY + HOUR) - 1.0) < 0.01
    assert 0.83 < cal.overall < 0.90  # energy-weighted over the four hours
    assert cal.factors()[SOUTH_WEST] == round(cal.factor(SOUTH_WEST), 3)
    assert list(cal.factors()) == [SOUTH_WEST, "240/40", "255/30", "270/20"]  # from north over east


def test_what_was_learned_holds_where_the_sun_stands_not_at_the_hour() -> None:
    cal = _calibration()
    morning = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)  # a low sun in the south-east
    for day in range(10):
        assert _one_hour(cal, morning + timedelta(days=day), 2000.0, 600.0)
    shaded = cal.bin_of(morning)
    assert shaded == "135/10" and list(cal.bins) == [shaded] and cal.factor(shaded) < 0.5
    # in February the sun stands there an hour later: the factor goes with it
    later = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
    assert cal.bin_of(later) == shaded and cal.factor_at(later) == cal.factor(shaded)
    # and the hour that was shaded in November has the sun elsewhere by then
    assert cal.bin_of(later - HOUR) != shaded and cal.factor_at(later - HOUR) == cal.overall


def test_old_samples_fade() -> None:
    cal = _calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 2000.0)
    for day in range(30, 60):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 4000.0)  # the tree was felled
    assert list(cal.bins) == [SOUTH_WEST]
    assert cal.factor_at(DAY) > 0.93


def test_factors_stay_within_bounds() -> None:
    cal = _calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 1000.0, 9000.0)
    assert cal.factor_at(DAY) == 1.5
    cal = _calibration()
    for day in range(30):
        _one_hour(cal, DAY + timedelta(days=day), 9000.0, 10.0)
    assert cal.factor_at(DAY) == 0.1


def test_hours_that_teach_nothing_are_skipped() -> None:
    cal = _calibration()
    assert not _one_hour(cal, DAY, 100.0, 80.0)  # next to nothing forecast
    assert not _one_hour(cal, DAY + HOUR, 4000.0, 0.0)  # the meter stood still: outage or snow
    assert not _one_hour(cal, DAY + 2 * HOUR, 4000.0, -30000.0)  # a daily counter went back to zero
    assert not _one_hour(cal, DAY + 3 * HOUR, 4000.0, 20000.0)  # more than the plant can make
    assert cal.samples == 0 and cal.bins == {}
    # no forecast kept for the hour
    cal.meter_read(DAY + 5 * HOUR, 1000.0, MIN_WH, MAX_WH)
    assert not cal.meter_read(DAY + 6 * HOUR, 3000.0, MIN_WH, MAX_WH)


def test_a_gap_in_the_readings_is_no_hour() -> None:
    cal = _calibration()
    cal.record_forecast([(DAY + i * HOUR, 4000.0) for i in range(4)], DAY)
    cal.meter_read(DAY, 1000.0, MIN_WH, MAX_WH)
    assert cal.increase(DAY + 2 * HOUR, 9000.0) is None
    assert not cal.meter_read(DAY + 2 * HOUR, 9000.0, MIN_WH, MAX_WH)  # two hours in one difference
    assert cal.increase(DAY + 3 * HOUR, 12000.0) == 3000.0
    assert cal.meter_read(DAY + 3 * HOUR, 12000.0, MIN_WH, MAX_WH)  # the next full hour counts again
    cal.meter_read(DAY + 4 * HOUR, None, MIN_WH, MAX_WH)  # the meter was unavailable
    cal.record_forecast([(DAY + 4 * HOUR, 4000.0)], DAY + 4 * HOUR)
    assert cal.increase(DAY + 5 * HOUR, 15000.0) is None
    assert not cal.meter_read(DAY + 5 * HOUR, 15000.0, MIN_WH, MAX_WH)
    assert cal.samples == 1


def test_forecast_of_a_finished_hour_is_kept_and_old_ones_go() -> None:
    cal = _calibration()
    cal.record_forecast([(DAY, 4000.0), (DAY + HOUR, 5000.0)], DAY + timedelta(minutes=20))
    cal.record_forecast([(DAY, 1000.0), (DAY + HOUR, 5500.0)], DAY + HOUR + timedelta(minutes=5))  # a newer run, the first hour is over
    assert cal.log[DAY.isoformat()] == 4000.0
    assert cal.log[(DAY + HOUR).isoformat()] == 5500.0
    cal.record_forecast([], DAY + timedelta(days=3))
    assert cal.log == {}


def test_what_was_learned_survives_and_belongs_to_one_plant() -> None:
    cal = _calibration("meter|12|10,180,30")
    for day in range(5):
        _one_hour(cal, DAY + timedelta(days=day), 4000.0, 3000.0)
    kept = cal.as_dict()
    assert kept["bins_by"] == BINS_BY
    again = Calibration.from_dict(kept, "meter|12|10,180,30", LAT, LON)
    assert again.samples == 5 and abs(again.factor_at(DAY) - cal.factor_at(DAY)) < 1e-3 and again.log == cal.log
    assert again.reading_time == cal.reading_time and again.reading_wh == cal.reading_wh
    other = Calibration.from_dict(kept, "meter|12|12,180,30", LAT, LON)  # the plane was changed
    assert other.samples == 0 and other.bins == {} and other.fingerprint == "meter|12|12,180,30"
    assert Calibration.from_dict(None, "x", LAT, LON).fingerprint == "x"
    # what is no part of the sky, or no three figures, is dropped
    odd = {"fingerprint": "x", "bins_by": BINS_BY, "bins": {"ten": [1, 2, 3], "230/40": [1, 2, 3], "225/50": [1, 2, 3], "225/40": [1, 2], "240/40": [1, 2, 3]}}
    assert list(Calibration.from_dict(odd, "x", LAT, LON).bins) == ["240/40"]
    assert Calibration.from_dict({"fingerprint": "x", "bins_by": BINS_BY, "bins": {"225/40": ["a", 2, 3]}}, "x", LAT, LON).bins == {}


def test_factors_learned_per_hour_of_the_day_move_to_the_sky() -> None:
    # an earlier version kept a factor per hour of the day (UTC)
    old = {
        "fingerprint": "x",
        "bins": {"12": [9000.0, 10000.0, 6.0], "13": [3000.0, 10000.0, 6.0], "14": [5000.0, 10000.0, 4.0], "99": [1.0, 1.0, 1.0]},
        "log": {DAY.isoformat(): 4000.0},
        "reading_time": DAY.isoformat(),
        "reading_wh": 50000.0,
        "samples": 16,
        "last_sample": DAY.isoformat(),
    }
    cal = Calibration.from_dict(old, "x", LAT, LON)
    assert cal.samples == 16 and cal.log == {DAY.isoformat(): 4000.0} and cal.reading_wh == 50000.0
    # each hour went to where the sun stood in it on the day of the last sample
    assert cal.bins[SOUTH_WEST] == [3000.0, 10000.0, 6.0]
    assert cal.bins[sun_bin(DAY - HOUR, LAT, LON)][0] == 9000.0 and cal.bins["240/40"] == [5000.0, 10000.0, 4.0]
    assert len(cal.bins) == 3
    assert cal.factor_at(DAY) < cal.factor_at(DAY - HOUR)
    assert cal.as_dict()["bins_by"] == BINS_BY
    # without a last sample there is no telling where the sun stood
    assert Calibration.from_dict({**old, "last_sample": ""}, "x", LAT, LON).bins == {}


# An hour before a November noon, the season of the low stratus.
GREY = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def _grey_hour(cal: Calibration, start: datetime, forecast_wh: float, measured_wh: float, meter_wh: float = 50000.0) -> bool:
    """Like `_one_hour`, for an hour the forecast sees under low stratus."""
    cal.record_forecast([(start, forecast_wh)], start, stratus={start})
    cal.meter_read(start, meter_wh, MIN_WH, MAX_WH)
    return cal.meter_read(start + HOUR, meter_wh + measured_wh, MIN_WH, MAX_WH)


def test_hours_under_low_stratus_are_learned_apart_from_the_sky() -> None:
    cal = _calibration()
    assert not cal.stratus_learned and cal.stratus_factor == 1.0 and cal.factor_at(GREY, True) == 1.0
    for day in range(10):
        assert _grey_hour(cal, GREY + timedelta(days=day), 1000.0, 600.0)
    # they count as hours learned, but no part of the sky and not the plant's factor has heard of them
    assert cal.samples == 10 and cal.bins == {} and cal.overall == 1.0 and cal.factors() == {}
    assert cal.stratus_learned and 9.0 < cal.stratus[2] < 10.0
    # ten hours are thin evidence: between what was counted and the plant's factor
    assert 0.7 < cal.stratus_factor < 0.9
    assert cal.factor_at(GREY, True) == cal.stratus_factor and cal.factor_at(GREY) == 1.0
    # a grey season later they stand alone
    for day in range(10, 60):
        _grey_hour(cal, GREY + timedelta(days=day), 1000.0, 600.0)
    assert abs(cal.stratus_factor - 0.6) < 0.01 and cal.overall == 1.0


def test_the_sky_and_the_low_stratus_each_keep_their_own() -> None:
    cal = _calibration()
    for day in range(21):
        start = DAY + timedelta(days=day)
        assert _one_hour(cal, start, 4000.0, 4000.0)  # in the sun the model fits
        assert _grey_hour(cal, start + 2 * HOUR, 1000.0, 500.0)  # under the lid it promises twice too much
    assert cal.samples == 42 and cal.bins and abs(cal.overall - 1.0) < 0.01
    assert abs(cal.factor_at(DAY) - 1.0) < 0.01
    assert 0.55 < cal.stratus_factor < 0.75
    # an hour under the lid takes the factor of the lid, wherever the sun stands behind it
    assert cal.factor_at(DAY, True) == cal.stratus_factor
    # without evidence of its own the lid follows the plant
    other = _calibration()
    for day in range(30):
        _one_hour(other, DAY + timedelta(days=day), 4000.0, 3000.0)
    assert not other.stratus_learned and other.stratus_factor == other.overall < 0.9


def test_the_mark_of_an_hour_follows_the_forecast_until_the_hour_is_over() -> None:
    cal = _calibration()
    first, second = GREY.isoformat(), (GREY + HOUR).isoformat()
    cal.record_forecast([(GREY, 1000.0), (GREY + HOUR, 1200.0)], GREY - HOUR, stratus={GREY, GREY + HOUR})
    assert cal.stratus_log == {first, second}
    # a newer run sees the lid open an hour earlier
    cal.record_forecast([(GREY, 1000.0), (GREY + HOUR, 2500.0)], GREY, stratus={GREY})
    assert cal.stratus_log == {first}
    # once the hour is over its mark stays, whatever a later run says of it
    cal.record_forecast([(GREY, 3000.0), (GREY + HOUR, 2500.0)], GREY + HOUR)
    assert cal.stratus_log == {first} and cal.log[first] == 1000.0
    # an hour with snow on the modules is not learned at all, lid or no lid
    cal.record_forecast([(GREY + HOUR, 2500.0)], GREY + HOUR, skip={GREY + HOUR}, stratus={GREY + HOUR})
    assert second not in cal.stratus_log and second not in cal.log
    # the marks go with the forecasts they belong to
    cal.record_forecast([], GREY + timedelta(days=3))
    assert cal.stratus_log == set() and cal.log == {}


def test_what_was_learned_under_low_stratus_survives() -> None:
    cal = _calibration("x")
    for day in range(5):
        _grey_hour(cal, GREY + timedelta(days=day), 1000.0, 700.0)
    ahead = GREY + timedelta(days=5)
    cal.record_forecast([(ahead, 900.0)], ahead - 4 * HOUR, stratus={ahead})
    kept = cal.as_dict()
    assert len(kept["stratus"]) == 3 and kept["stratus_hours"] == sorted(cal.stratus_log) and ahead.isoformat() in kept["stratus_hours"]
    again = Calibration.from_dict(kept, "x", LAT, LON)
    assert again.samples == 5 and again.stratus_log == cal.stratus_log and abs(again.stratus_factor - cal.stratus_factor) < 1e-3
    # what an earlier version kept knows no low stratus: nothing learned, nothing marked, the rest as it was
    old = {key: value for key, value in kept.items() if key not in ("stratus", "stratus_hours")}
    earlier = Calibration.from_dict(old, "x", LAT, LON)
    assert earlier.stratus == [0.0, 0.0, 0.0] and earlier.stratus_log == set() and not earlier.stratus_learned
    assert earlier.samples == 5 and earlier.log == cal.log
    # sums that are no three figures are dropped
    assert Calibration.from_dict({**kept, "stratus": [1.0, 2.0]}, "x", LAT, LON).stratus == [0.0, 0.0, 0.0]
