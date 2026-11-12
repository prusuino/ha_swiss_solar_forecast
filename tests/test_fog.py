"""Low stratus: which hours of a forecast lie under the grey lid, which of
a day's daylight hours they are, and when the lid is expected to open."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.fog import clears_at, day_hours, is_low_stratus, stratus_hours

T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = T0.date()
DAYLIGHT = [T0 + h * HOUR for h in range(8, 15)]  # the hours that start from 8 to 14 UTC


def _utc(when: datetime) -> datetime:
    return when


def _two_zones_east(when: datetime) -> datetime:
    return when + 2 * HOUR


def test_a_closed_low_layer_under_an_open_sky_is_low_stratus() -> None:
    assert is_low_stratus(0.95, 0.0)
    assert is_low_stratus(0.75, 0.3)  # the thresholds themselves count
    assert not is_low_stratus(0.74, 0.0)  # broken low clouds
    assert not is_low_stratus(0.95, 0.31)  # clouds above them: a front, not a lid
    assert not is_low_stratus(0.0, 0.0)


def test_without_the_cloud_layers_nothing_is_said() -> None:
    assert not is_low_stratus(None, 0.0)
    assert not is_low_stratus(0.95, None)
    assert not is_low_stratus(None, None, 0.0, 100.0, 95.0)


def test_rain_is_weather_and_not_a_lid() -> None:
    assert is_low_stratus(1.0, 0.0, 0.1)  # drizzle falls from stratus too
    assert not is_low_stratus(1.0, 0.0, 0.2)
    assert is_low_stratus(1.0, 0.0, None)  # an unknown precipitation speaks neither way


def test_an_hour_the_forecast_lets_the_sun_through_is_none() -> None:
    assert is_low_stratus(0.9, 0.0, 0.0, 100.0, 75.0)
    assert not is_low_stratus(0.9, 0.0, 0.0, 100.0, 60.0)  # direct light: the lid has holes
    # at night there is no light to judge by, and without the diffuse share none either
    assert is_low_stratus(0.9, 0.0, 0.0, 0.0, 0.0)
    assert is_low_stratus(0.9, 0.0, 0.0, 100.0, None)
    assert is_low_stratus(0.9, 0.0, 0.0, None, 60.0)


def test_the_series_are_kept_under_the_end_of_their_hour() -> None:
    low = {T0 + 9 * HOUR: 0.9, T0 + 10 * HOUR: 0.9, T0 + 11 * HOUR: 0.2, T0 + 12 * HOUR: 0.9, T0 + 13 * HOUR: 0.9, T0 + 14 * HOUR: None}
    mid = {T0 + 9 * HOUR: 0.0, T0 + 10 * HOUR: 0.0, T0 + 11 * HOUR: 0.0, T0 + 12 * HOUR: 0.8}  # nothing for the last two hours
    assert stratus_hours(low, mid) == {T0 + 8 * HOUR, T0 + 9 * HOUR}
    # precipitation and radiation take hours away, they never add one
    assert stratus_hours(low, mid, {T0 + 9 * HOUR: 0.5}) == {T0 + 9 * HOUR}
    assert stratus_hours(low, mid, None, {T0 + 10 * HOUR: 200.0}, {T0 + 10 * HOUR: 50.0}) == {T0 + 8 * HOUR}
    assert stratus_hours({}, mid) == set() and stratus_hours(low, {}) == set()


def test_the_hours_of_a_day_are_its_daylight_hours_in_local_time() -> None:
    stratus = {T0 + h * HOUR for h in (3, 8, 9, 10, 32, 33)}  # one at night, three in the morning, two the day after
    assert day_hours(stratus, DAYLIGHT, DAY, _utc) == [T0 + 8 * HOUR, T0 + 9 * HOUR, T0 + 10 * HOUR]
    assert day_hours(stratus, DAYLIGHT, DAY + timedelta(days=1), _utc) == []
    assert day_hours(set(), DAYLIGHT, DAY, _utc) == []
    # the day is the local one: an hour that starts at 23 UTC belongs to the next day two zones to the east
    late = T0 - HOUR
    assert day_hours({late}, [late], DAY, _two_zones_east) == [late]
    assert day_hours({late}, [late], DAY, _utc) == []


def test_the_lid_opens_at_the_end_of_its_last_hour() -> None:
    morning = {T0 + h * HOUR for h in (8, 9, 10)}
    assert clears_at(morning, DAYLIGHT, DAY, _utc) == T0 + 11 * HOUR
    # a gap in the morning does not open it for good
    assert clears_at({T0 + 8 * HOUR, T0 + 12 * HOUR}, DAYLIGHT, DAY, _utc) == T0 + 13 * HOUR
    # hours of another day and hours without light say nothing about today
    assert clears_at({T0 + 3 * HOUR, T0 + 33 * HOUR}, DAYLIGHT, DAY, _utc) is None


def test_no_opening_without_stratus_or_when_it_stays_until_dusk() -> None:
    assert clears_at(set(), DAYLIGHT, DAY, _utc) is None
    assert clears_at(set(DAYLIGHT), DAYLIGHT, DAY, _utc) is None
    assert clears_at({T0 + 13 * HOUR, T0 + 14 * HOUR}, DAYLIGHT, DAY, _utc) is None  # it closes in the afternoon and stays
    assert clears_at({T0 + 8 * HOUR}, [], DAY, _utc) is None  # a day without light
