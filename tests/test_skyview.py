"""The part of the sky a station sees the sun in: what its record tells
about a tree beside the sensor, and what it must not take for one (a
cloud, a foggy morning, a grey day)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from custom_components.swiss_solar_forecast.skyview import ABOVE_SHADE, BAND, FLOOR, SkyView
from custom_components.swiss_solar_forecast.solar import SunPosition, sun_position

from .conftest import station_day

LAT, LON = 47.3769, 8.5417
STEP = timedelta(minutes=10)


def _tree(when: datetime, sun: SunPosition) -> float:
    """A tree south-west of the sensor: behind it the sensor gets the sky's light only."""
    return 0.15 if 210 <= sun.azimuth < 232 and sun.elevation < 30 else 1.0


def _view() -> SkyView:
    return SkyView(station="ZZZ", lat=LAT, lon=LON)


def _days(first: date, count: int, share=_tree) -> list[list[tuple[datetime, float]]]:
    return [station_day(first + timedelta(days=n), LAT, LON, share) for n in range(count)]


def _when(day: date, azimuth: float) -> datetime:
    """The moment of a day the sun stands at a compass bearing."""
    when = datetime(day.year, day.month, day.day, 3, 0, tzinfo=UTC)
    while sun_position(when, LAT, LON).azimuth < azimuth:
        when += timedelta(minutes=1)
    return when


def test_a_tree_is_found_on_the_second_sunny_day() -> None:
    view = _view()
    first, second = _days(date(2026, 11, 1), 2)
    behind = _when(date(2026, 11, 2), 220.0)

    assert view.add_day(first) is True
    # one day proves nothing: it could have been a cloud
    assert view.sees(behind) and view.profile() == {} and view.usable(second) == second

    assert view.add_day(second) is True
    assert view.days == 2 and view.sunny == 2
    assert not view.sees(behind) and len(view.usable(second)) < len(second)
    # the rest of the sky is free: the morning, noon, and the evening beyond the tree
    assert view.sees(_when(date(2026, 11, 2), 150.0)) and view.sees(_when(date(2026, 11, 2), 180.0)) and view.sees(_when(date(2026, 11, 2), 200.0))
    shade = view.profile()
    assert shade and set(shade) <= {205, 210, 215, 220, 225, 230} and {215, 220, 225} <= set(shade)
    # up to the tree's top, and the few degrees above it nothing was measured in
    assert all(FLOOR < height <= 30.0 + BAND * ABOVE_SHADE for height in shade.values())


def test_values_from_the_shade_are_left_out() -> None:
    view = _view()
    for day in _days(date(2026, 11, 1), 2):
        view.add_day(day)
    third = station_day(date(2026, 11, 3), LAT, LON, _tree)
    used = view.usable(third)
    left = [(end, value) for end, value in third if (end, value) not in used]
    in_shade = [end for end, _value in third if _tree(end - STEP / 2, sun_position(end - STEP / 2, LAT, LON)) < 1]
    # every value the tree darkened is gone, and hardly anything else: the ten minutes on either side
    assert in_shade and set(in_shade) <= {end for end, _value in left}
    assert len(left) <= len(in_shade) + 4
    # the night and the open sky stay
    assert len(used) > 100 and all(_tree(end - STEP / 2, sun_position(end - STEP / 2, LAT, LON)) == 1 for end, _value in used)
    # a view that knows nothing leaves everything
    assert _view().usable(third) == third


def test_one_cloud_is_no_tree() -> None:
    def cloud(when: datetime, sun: SunPosition) -> float:
        return 0.3 if when.date() == date(2026, 11, 1) and 12 <= when.hour < 13 else 1.0

    view = _view()
    cloudy, clear = _days(date(2026, 11, 1), 2, cloud)
    assert view.add_day(cloudy) is True  # still a sunny day
    assert view.profile() == {} and view.usable(cloudy) == cloudy
    assert view.add_day(clear) is True
    assert view.profile() == {} and view.usable(clear) == clear


def test_a_grey_morning_is_no_shade() -> None:
    def fog(when: datetime, sun: SunPosition) -> float:
        return 0.3 if when.hour < 11 else 1.0

    view = _view()
    for day in _days(date(2026, 11, 1), 6, fog):
        assert view.add_day(day) is True  # the afternoons are sunny
    # the mornings under fog teach nothing: the station is asked there as before
    assert view.profile() == {}
    morning = station_day(date(2026, 11, 7), LAT, LON, fog)
    assert view.usable(morning) == morning


def test_grey_days_say_nothing() -> None:
    view = _view()
    for day in _days(date(2026, 11, 1), 5, lambda when, sun: 0.25):
        assert view.add_day(day) is False
    assert view.days == 5 and view.sunny == 0 and view.cells == {} and view.profile() == {}


def test_changing_cloud_is_no_sunny_day() -> None:
    def broken(when: datetime, sun: SunPosition) -> float:
        return 0.4 if when.minute // 10 % 3 == 0 else 1.0

    view = _view()
    for day in _days(date(2026, 11, 1), 4, broken):
        assert view.add_day(day) is False
    # full sun still proves the view where it was seen; shade is learned from none of it
    assert view.cells and view.profile() == {}


def test_a_felled_tree_is_forgotten_after_a_sunny_day() -> None:
    view = _view()
    for day in _days(date(2026, 11, 1), 3):
        view.add_day(day)
    open_sky = station_day(date(2026, 11, 4), LAT, LON)
    assert len(view.usable(open_sky)) < len(open_sky)
    # one day of full sun where the tree stood, and the station is asked there again
    view.add_day(open_sky)
    assert view.usable(open_sky) == open_sky


def test_a_new_building_takes_some_sunny_days() -> None:
    # the same path of the sun day after day, as around the solstice: first open sky, then a building
    open_sky = station_day(date(2026, 12, 20), LAT, LON)
    built = station_day(date(2026, 12, 20), LAT, LON, _tree)
    view = _view()
    for _day in range(5):
        view.add_day(open_sky)
    needed = 0
    while len(view.usable(built)) == len(built) and needed < 30:
        view.add_day(built)
        needed += 1
    # the days in the sun count several times: not after two days, but within two weeks
    assert 2 < needed <= 14


def test_the_fields_below_the_shade_go_with_it() -> None:
    view = _view()
    for day in _days(date(2026, 10, 20), 2):
        view.add_day(day)
    # in December the sun passes the same bearing far lower, where no value was ever measured
    assert sun_position(_when(date(2026, 12, 21), 220.0), LAT, LON).elevation < 12
    assert not view.sees(_when(date(2026, 12, 21), 220.0))
    # in summer it passes above the tree
    assert sun_position(_when(date(2026, 8, 20), 220.0), LAT, LON).elevation > 35
    assert view.sees(_when(date(2026, 8, 20), 220.0))


def test_only_finished_days_are_taken_and_only_once() -> None:
    view = _view()
    record = [value for day in _days(date(2026, 11, 1), 3) for value in day]
    assert view.take(record, date(2026, 11, 3)) == 2
    assert view.covered == "2026-11-02" and view.days == 2 and view.known
    # the same record again brings nothing; a day later it brings the third day
    assert view.take(record, date(2026, 11, 3)) == 0 and view.days == 2
    assert view.take(record, date(2026, 11, 4)) == 1 and view.covered == "2026-11-03"
    assert not _view().known


def test_a_record_with_holes() -> None:
    view = _view()
    day = station_day(date(2026, 11, 1), LAT, LON, _tree)
    # the hours around noon are missing: too little sun left to call the day sunny
    holed = [(end, value) for end, value in day if not 8 <= end.hour < 13]
    assert view.add_day(holed) is False
    assert view.add_day([]) is False and view.days == 2


def test_what_is_kept_comes_back() -> None:
    view = _view()
    for day in _days(date(2026, 11, 1), 3):
        view.add_day(day)
    view.covered, view.settled = "2026-11-03", True
    kept = view.as_dict()
    back = SkyView.from_dict(kept, "ZZZ", LAT, LON)
    assert back.profile() == view.profile() and back.covered == "2026-11-03" and back.settled is True
    assert (back.days, back.sunny) == (3, 3) and back.as_dict() == kept
    # another station, another place and nonsense start afresh
    assert not SkyView.from_dict(kept, "OTH", LAT, LON).known
    assert not SkyView.from_dict(kept, "ZZZ", LAT + 0.1, LON).known
    for nonsense in (None, [], "x", {"station": "ZZZ", "at": [round(LAT, 3), round(LON, 3)], "cells": {"210/3": ["a", 1]}}, {**kept, "covered": "soon"}):
        fresh = SkyView.from_dict(nonsense, "ZZZ", LAT, LON)
        assert not fresh.known and fresh.cells == {} and (fresh.station, fresh.lat) == ("ZZZ", LAT)
    # fields that cannot be are dropped
    odd = SkyView.from_dict({**kept, "cells": {"210/3": [0.0, 2.0], "212/3": [0.0, 2.0], "210/99": [0.0, 2.0], "x/1": [0.0, 2.0], "215/3": [1.0]}}, "ZZZ", LAT, LON)
    assert set(odd.cells) == {"210/3"}


def _day_with_height(first: date, low: float, high: float) -> date:
    """The first day from `first` on that has the sun between two heights at the tree's bearing."""
    day = first
    while not low < sun_position(_when(day, 220.0), LAT, LON).elevation < high:
        day += timedelta(days=1)
    return day


def test_the_first_degrees_above_the_shade_go_with_it_until_the_sun_is_seen_there() -> None:
    view = _view()
    solstice = station_day(date(2026, 12, 20), LAT, LON, _tree)
    for _day in range(2):
        view.add_day(solstice)
    known = sun_position(_when(date(2026, 12, 20), 220.0), LAT, LON).elevation
    assert 7.5 < known < 10 and not view.sees(_when(date(2026, 12, 20), 220.0))
    # weeks later the sun passes the bearing a few degrees higher, where nothing was ever measured: still shade
    higher = _day_with_height(date(2027, 1, 10), 11.0, 14.0)
    assert not view.sees(_when(higher, 220.0))
    # far above what was seen the station is taken at its word
    far = _day_with_height(date(2027, 1, 10), 18.0, 25.0)
    assert view.sees(_when(far, 220.0))
    # one value of full sun there (the tree is no more) and the field is free
    moment = _when(higher, 220.0).replace(second=0, microsecond=0)
    end = moment - timedelta(minutes=moment.minute % 10) + timedelta(minutes=10)
    open_sky = [value for value in station_day(higher, LAT, LON) if abs(value[0] - end) <= timedelta(minutes=10)]
    assert view.add_day(open_sky) is False  # three values make no sunny day
    assert view.sees(end - timedelta(minutes=5))
