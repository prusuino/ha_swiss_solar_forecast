"""What the weather model gets wrong by the kind of sky: how an hour's sky
is told, which forecast an hour is measured against, what the station's
hours teach, when a factor applies and what it leaves alone."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast import bias
from custom_components.swiss_solar_forecast.bias import CLEAR, GREY, MIXED, SkyBias
from custom_components.swiss_solar_forecast.nowcast import Correction
from custom_components.swiss_solar_forecast.solar import sun_position

LAT, LON = 46.9480, 7.4474  # Bern
HOUR = timedelta(hours=1)
STEP = timedelta(minutes=10)


def _june(hour: int, minute: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC)


def _station(share: float, forecast: dict[datetime, float], values: int = 6) -> list[tuple[datetime, float]]:
    """A station that received `share` of what was forecast for every hour in `forecast` (by the hour's start)."""
    return sorted((start + STEP * (index + 1), share * ghi) for start, ghi in forecast.items() for index in range(values))


def _taught(share: float, ghi: float, hours: int) -> SkyBias:
    """A place where the station received `share` of a forecast of `ghi` W/m², for `hours` hours around noon."""
    learned = SkyBias(lat=LAT, lon=LON)
    day = 1
    while learned.samples < hours:
        forecast = {_june(hour, day=day): ghi for hour in range(9, 14)[: hours - learned.samples]}
        learned.record_forecast(forecast.items(), _june(4, day=day))
        learned.learn(_station(share, forecast))
        day += 1
    return learned


def test_the_sky_a_forecast_expects() -> None:
    # around noon in June a cloudless sky gives some 900 W/m² on the Plateau
    assert 850 < bias.clear_sky_hour(_june(11), LAT, LON) < 1000
    assert bias.clear_sky(_june(1), LAT, LON) == 0.0
    assert bias.sky_of(800.0, _june(11), LAT, LON) == CLEAR
    assert bias.sky_of(500.0, _june(11), LAT, LON) == MIXED
    assert bias.sky_of(200.0, _june(11), LAT, LON) == GREY
    assert bias.sky_of(0.0, _june(11), LAT, LON) == GREY
    # the same radiation is a clear sky while the sun stands low
    assert bias.sky_of(200.0, _june(5), LAT, LON) == CLEAR
    # no sky at night, none without a forecast
    assert bias.sky_of(0.0, _june(1), LAT, LON) is None
    assert bias.sky_of(None, _june(11), LAT, LON) is None


def test_an_hour_is_measured_against_the_forecast_three_hours_ahead() -> None:
    learned = SkyBias(lat=LAT, lon=LON)
    learned.record_forecast([(_june(10), 600.0), (_june(6), 300.0), (_june(5), 100.0)], _june(6, 5))
    assert learned.log == {_june(10).isoformat(): 600.0, _june(6).isoformat(): 300.0}  # the hour that is over is not kept
    learned.record_forecast([(_june(10), 650.0), (_june(6), 350.0)], _june(7))
    assert learned.log[_june(10).isoformat()] == 650.0  # three hours ahead: the newer run counts
    learned.record_forecast([(_june(10), 700.0), (_june(9), 500.0)], _june(8))
    assert learned.log[_june(10).isoformat()] == 650.0  # nearer than that it stays
    assert learned.log[_june(9).isoformat()] == 500.0  # first seen late: what was first seen
    learned.record_forecast([(_june(9), 550.0), (_june(10), None)], _june(9, 30))
    assert learned.log[_june(9).isoformat()] == 500.0 and learned.log[_june(10).isoformat()] == 650.0
    # only the day ahead is kept, and what is long over goes
    learned.record_forecast([(_june(12, day=22), 400.0), (_june(12, day=23), 400.0)], _june(10, day=22))
    assert _june(12, day=22).isoformat() in learned.log and _june(12, day=23).isoformat() not in learned.log
    assert _june(10).isoformat() in learned.log and _june(6).isoformat() in learned.log
    learned.record_forecast([], _june(23, day=22))
    assert _june(10).isoformat() not in learned.log and _june(12, day=22).isoformat() in learned.log


def test_the_stations_hours_are_learned_once_and_by_their_sky() -> None:
    learned = SkyBias(lat=LAT, lon=LON)
    forecast = {_june(9): 800.0, _june(10): 500.0, _june(11): 200.0, _june(12): 800.0}
    learned.record_forecast(forecast.items(), _june(4))
    values = _station(0.8, forecast)
    # the station is through with the first two hours
    assert learned.learn([(end, value) for end, value in values if end <= _june(11, 20)]) == 2
    assert learned.sums[CLEAR] == [640.0, 800.0, 1.0] and learned.sums[MIXED] == [400.0, 500.0, 1.0]
    assert learned.samples == 2 and learned.last_sample == _june(10).isoformat()
    # the next read brings the same hours again and two new ones
    assert learned.learn(values) == 2
    assert learned.sums[GREY] == [160.0, 200.0, 1.0]
    measured, expected, count = learned.sums[CLEAR]
    assert abs(measured - (640.0 * bias.DECAY + 640.0)) < 1e-6 and abs(expected - (800.0 * bias.DECAY + 800.0)) < 1e-6
    assert abs(count - (bias.DECAY + 1.0)) < 1e-9
    assert learned.learn(values) == 0 and learned.samples == 4
    assert learned.hours() == {CLEAR: 2.0, MIXED: 1.0, GREY: 1.0}


def test_what_teaches_nothing() -> None:
    learned = SkyBias(lat=LAT, lon=LON)
    forecast = {_june(9): 800.0, _june(10): 800.0, _june(11): 30.0, _june(3): 80.0}
    learned.record_forecast(forecast.items(), _june(2))
    values = (
        _station(0.5, {_june(3): 80.0})  # the sun barely up
        + _station(0.5, {_june(9): 800.0}, values=4)  # the station was silent for a third of the hour
        + _station(0.5, {_june(11): 30.0, _june(12): 800.0})  # next to nothing forecast; no forecast kept
        + _station(0.5, {_june(13): 800.0}, values=3)  # the hour still runs
    )
    assert learned.learn(values) == 0
    assert learned.sums == {} and learned.samples == 0 and not learned.in_force
    # a winter morning: the sun is up, but too low when the hour begins
    winter = datetime(2026, 12, 21, 8, 0, tzinfo=UTC)
    assert 0 < sun_position(winter, LAT, LON).elevation < bias.MIN_ELEVATION < sun_position(winter + HOUR, LAT, LON).elevation
    low = SkyBias(lat=LAT, lon=LON)
    low.record_forecast([(winter, 60.0), (winter + HOUR, 120.0)], winter - 4 * HOUR)
    assert low.learn(_station(0.5, {winter: 60.0})) == 0 and low.learn(_station(0.5, {winter + HOUR: 120.0})) == 1
    # an hour that was passed over stays passed over, a full one after it counts
    assert learned.learn(values + _station(0.5, {_june(10): 800.0})) == 0
    learned.record_forecast([(_june(10, day=22), 800.0)], _june(4, day=22))
    assert learned.learn(_station(0.5, {_june(10, day=22): 800.0})) == 1


def test_a_factor_needs_hours_and_stays_cautious() -> None:
    few = _taught(0.6, 800.0, 5)
    assert few.factor(CLEAR) == 1.0 and few.factors() == {CLEAR: None, MIXED: None, GREY: None} and not few.in_force
    six = _taught(0.6, 800.0, 6)
    assert six.in_force and six.factors()[MIXED] is None and six.factor(MIXED) == 1.0
    assert 0.8 < six.factor(CLEAR) < 0.9  # held near 1 while the hours are few
    many = _taught(0.6, 800.0, 40)
    assert abs(many.factor(CLEAR) - 0.6) < 0.001  # then it stands on the measurements
    assert six.factor(CLEAR) > _taught(0.6, 800.0, 12).factor(CLEAR) > many.factor(CLEAR)
    assert many.hours()[CLEAR] < 40  # earlier hours fade
    # limited on both sides
    assert _taught(0.1, 800.0, 40).factor(CLEAR) == bias.FACTOR_MIN
    assert _taught(3.0, 200.0, 40).factor(GREY) == bias.FACTOR_MAX
    assert many.factor_at(_june(11), 800.0) == many.factor(CLEAR) and many.factor_at(_june(11), 300.0) == 1.0
    assert many.factor(None) == 1.0 and many.factor_at(_june(1), 0.0) == 1.0
    # one odd hour moves next to nothing
    odd = _taught(1.0, 800.0, 39)
    odd.record_forecast([(_june(10, day=30), 800.0)], _june(4, day=30))
    assert odd.learn(_station(0.2, {_june(10, day=30): 800.0})) == 1
    assert 0.96 < odd.factor(CLEAR) < 1.0


def test_the_hours_ahead_take_the_factor_of_their_sky() -> None:
    learned = _taught(0.6, 800.0, 40)
    now = _june(8, 20, day=30)
    rows = [(_june(hour, day=30), ghi, dhi, 20.0, 1.0) for hour, ghi, dhi in ((8, 800.0, 200.0), (9, 800.0, 200.0), (12, 800.0, 600.0), (13, 400.0, 150.0), (14, None, None), (23, 0.0, 0.0))]
    out = bias.corrected(rows, learned, None, now, 1.0)
    assert out[0] == rows[0]  # the hour that is over
    assert abs(out[1][1] - 480.0) < 1.0 and out[1][2] == 200.0 and out[1][3:] == (20.0, 1.0)  # the running hour, the diffuse light stays
    assert abs(out[2][1] - 480.0) < 1.0 and abs(out[2][2] - out[2][1]) < 1e-9  # no more diffuse light than light
    assert out[3] == rows[3] and out[4] == rows[4] and out[5] == rows[5]  # a sky without a factor, no forecast, the night
    # the farther the station, the less of the factor
    half = bias.corrected(rows, learned, None, now, 0.5)
    assert abs(half[1][1] - 640.0) < 1.0
    # nothing learned, no station in reach, switched off: the rows as they came
    assert bias.corrected(rows, SkyBias(lat=LAT, lon=LON), None, now, 1.0) is rows
    assert bias.corrected(rows, learned, None, now, 0.0) is rows
    assert bias.corrected(rows, None, None, now, 1.0) is rows


def test_the_last_hours_measurement_speaks_first() -> None:
    learned = _taught(0.6, 800.0, 40)
    now = _june(8, 20, day=30)
    found = Correction(ratio=0.9, measured=720.0, expected=800.0, measured_at=_june(8, 10, day=30), trust=1.0)
    rows = [(_june(hour, day=30), 800.0, 200.0) for hour in (9, 10, 11, 12, 13)]
    out = bias.corrected(rows, learned, found, now, 1.0)
    shares = [(800.0 - row[1]) / 320.0 for row in out]
    assert shares[0] < 0.15  # the running hour is the measurement's
    assert shares[0] < shares[1] < shares[2] < shares[3]  # it takes over as the measurement fades
    assert abs(shares[3] - 1.0) < 1e-9 and abs(shares[4] - 1.0) < 1e-9  # beyond the reach it stands alone
    for row, share in zip(rows, shares):
        assert abs(share - (1.0 - found.reach(row[0] - HOUR))) < 1e-9
    # a measurement grown old counts no more
    late = _june(9, 40, day=30)
    assert not found.fresh(late)
    assert abs(bias.corrected(rows, learned, found, late, 1.0)[1][1] - 480.0) < 1.0


def test_what_was_learned_is_kept_for_the_place() -> None:
    learned = _taught(0.6, 800.0, 8)
    learned.record_forecast([(_june(10, day=30), 700.0)], _june(4, day=30))
    kept = learned.as_dict()
    again = SkyBias.from_dict(kept, LAT, LON)
    assert again.factors() == learned.factors() and again.samples == 8 and again.done == learned.done
    assert again.log == learned.log and again.last_sample == learned.last_sample
    assert again.learn(_station(0.6, {_june(12, day=2): 800.0})) == 0  # looked at before
    # another place, nothing kept, something broken: a fresh start
    assert SkyBias.from_dict(kept, LAT + 0.1, LON).samples == 0
    assert SkyBias.from_dict(None, LAT, LON) == SkyBias(lat=LAT, lon=LON)
    assert SkyBias.from_dict({**kept, "sums": {CLEAR: ["x", 1, 2]}}, LAT, LON).sums == {}
    assert SkyBias.from_dict({**kept, "sums": {"purple": [1.0, 1.0, 9.0], CLEAR: [1.0, 2.0]}}, LAT, LON).sums == {}
