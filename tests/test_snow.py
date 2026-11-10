"""The snow on the modules: what falls as snow, how it slides and melts,
and how the state is carried from one forecast run to the next."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.snow import SnowState, snowfall, step_ground, step_plane

T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _hours(count: int, precipitation=lambda i: 0.0, temperature=lambda i: -3.0, ghi=lambda i: 0.0, planes: int = 1) -> list:
    return [(T0 + i * HOUR, precipitation(i), temperature(i), ghi(i), [ghi(i)] * planes) for i in range(count)]


def test_precipitation_falls_as_snow_when_it_is_cold() -> None:
    assert snowfall(1.0, -1.0) == 1.0
    assert snowfall(1.0, 0.5) == 1.0
    assert snowfall(1.0, 3.0) == 0.0
    assert abs(snowfall(1.0, 1.25) - 0.5) < 1e-9  # sleet: half of it
    assert snowfall(None, -1.0) == 0.0 and snowfall(1.0, None) == 0.0 and snowfall(0.0, -5.0) == 0.0


def test_fresh_snow_covers_the_modules_and_stays_while_it_is_cold_and_dark() -> None:
    load, cover = step_plane(0.0, 0.0, 2.0, -3.0, 0.0, 30.0)
    assert (load, cover) == (2.0, 1.0)
    assert step_plane(load, cover, 0.0, -3.0, 0.0, 30.0) == (2.0, 1.0)
    # a dusting covers only a part
    assert step_plane(0.0, 0.0, 0.3, -3.0, 0.0, 30.0)[1] == 0.3
    # nothing lies, nothing happens
    assert step_plane(0.0, 0.0, 0.0, 5.0, 500.0, 30.0) == (0.0, 0.0)


def test_snow_slides_off_in_the_sun_the_faster_the_steeper() -> None:
    load, cover = step_plane(2.0, 1.0, 0.0, -3.0, 400.0, 30.0)
    assert abs(cover - 0.9015) < 0.001 and 1.4 < load < 1.6  # a tenth slid off, a little melted
    steep = step_plane(2.0, 1.0, 0.0, -3.0, 400.0, 60.0)[1]
    flat = step_plane(2.0, 1.0, 0.0, -3.0, 400.0, 0.0)[1]
    assert steep < cover < flat == 1.0
    # weak light in the cold moves nothing
    assert step_plane(2.0, 1.0, 0.0, -3.0, 100.0, 30.0)[1] == 1.0


def test_warm_air_melts_the_snow_on_a_flat_roof() -> None:
    load, cover = 1.0, 1.0
    hours = 0
    while cover > 0 and hours < 24:
        load, cover = step_plane(load, cover, 0.0, 5.0, 0.0, 0.0)
        hours += 1
    assert hours == 2  # 0.625 mm an hour at 5 °C
    # rain on snow adds nothing
    assert step_plane(1.0, 1.0, 3.0, 5.0, 0.0, 0.0)[0] < 1.0


def test_ground_snow_gathers_and_melts() -> None:
    ground = 0.0
    for _ in range(4):
        ground = step_ground(ground, 1.0, -2.0, 0.0)
    assert ground == 4.0
    assert step_ground(ground, 0.0, -10.0, 300.0) == 4.0  # deep frost: the sun melts nothing
    assert step_ground(ground, 0.0, 4.0, 0.0) == 3.5
    assert step_ground(0.2, 0.0, 6.0, 0.0) == 0.0


def test_finished_hours_are_kept_and_not_worked_out_again() -> None:
    state = SnowState()
    snowy = _hours(8, precipitation=lambda i: 1.0 if i < 2 else 0.0)
    now = T0 + 3 * HOUR + timedelta(minutes=15)
    covers = state.advance(snowy, [30.0], now)
    assert len(covers) == 8
    assert covers[T0][0] == 0.5 and covers[T0 + HOUR][0] == 1.0  # the mean of the hour in which the snow fell
    assert covers[T0 + 7 * HOUR][0] == 1.0  # a cold night: it stays
    assert state.time == (T0 + 3 * HOUR).isoformat()
    assert state.ground == 2.0 and state.planes == [[2.0, 1.0]] and state.covered
    assert list(state.history) == [(T0 + i * HOUR).isoformat() for i in range(3)]

    # the next run says it never snowed: what is over stays, the rest continues from the kept snow
    kept = state.as_dict()
    dry = _hours(8)
    assert state.ground_track(dry)[T0 + 5 * HOUR] == 2.0
    assert state.as_dict() == kept  # looking at the ground keeps nothing
    again = state.advance(dry, [30.0], now)
    assert again == covers

    # an hour later one more hour is over
    state.advance(dry, [30.0], now + HOUR)
    assert state.time == (T0 + 4 * HOUR).isoformat() and len(state.history) == 4


def test_the_state_survives_a_restart_and_fits_the_planes() -> None:
    state = SnowState()
    now = T0 + 3 * HOUR
    state.advance(_hours(6, precipitation=lambda i: 1.0), [30.0], now)
    back = SnowState.from_dict(state.as_dict(), 1)
    assert back == state
    # another number of planes: the ground keeps its snow, the modules start free
    other = SnowState.from_dict(state.as_dict(), 2)
    assert other.ground == state.ground and other.planes == [[0.0, 0.0], [0.0, 0.0]] and other.history == {}
    covers = other.advance(_hours(6, planes=2), [30.0, 10.0], now)
    assert covers[T0 + 4 * HOUR] == [0.0, 0.0]
    for broken in (None, "x", {}, {"time": "never"}, {"time": state.time, "planes": [["a", 1]]}):
        fresh = SnowState.from_dict(broken, 2)
        assert fresh.planes == [[0.0, 0.0], [0.0, 0.0]] and fresh.time == "" and not fresh.covered


def test_after_a_long_gap_the_snow_starts_at_none() -> None:
    state = SnowState()
    state.advance(_hours(4, precipitation=lambda i: 1.0), [30.0], T0 + 4 * HOUR)
    assert state.covered
    later = [(T0 + timedelta(days=3) + i * HOUR, 0.0, -3.0, 0.0, [0.0]) for i in range(4)]
    covers = state.advance(later, [30.0], T0 + timedelta(days=3, hours=1))
    assert all(cover == [0.0] for cover in covers.values())
    assert not state.covered and state.ground == 0.0
    assert len(state.history) == 1  # what was older than two days went


def test_the_meter_can_set_the_modules_free() -> None:
    state = SnowState()
    state.advance(_hours(4, precipitation=lambda i: 1.0), [30.0], T0 + 4 * HOUR)
    state.clear_modules()
    assert not state.covered and state.ground == 4.0
    covers = state.advance(_hours(8), [30.0], T0 + 4 * HOUR)
    assert covers[T0 + 3 * HOUR][0] > 0.9  # the past is not rewritten
    assert covers[T0 + 4 * HOUR] == [0.0]
