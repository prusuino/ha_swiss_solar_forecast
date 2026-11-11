"""The short-term correction: which station is asked, what its last hour
says about the forecast, how far that reaches, and what it leaves alone."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast import nowcast
from custom_components.swiss_solar_forecast.nowcast import Correction

LAT, LON = 46.9480, 7.4474  # Bern
HOUR = timedelta(hours=1)
STEP = timedelta(minutes=10)
NOON = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
NOW = NOON + timedelta(minutes=14)
# the forecast's hourly means by the hour's end, as the files carry them
FORECAST = {NOON + index * HOUR: 800.0 for index in range(-4, 6)}


@dataclass(frozen=True)
class Place:
    abbr: str
    lat: float
    lon: float
    height: float | None


def _flat_km(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    """Distance on a flat map with 100 km to the degree: enough to order stations."""
    return (((other_lat - lat) * 100.0) ** 2 + ((other_lon - lon) * 100.0) ** 2) ** 0.5


def _measured(share: float, newest: datetime = NOON + STEP, count: int = 6) -> list[tuple[datetime, float]]:
    """A station that received `share` of what was forecast, in ten-minute values up to `newest`."""
    ends = [newest - index * STEP for index in range(count)]
    return sorted((end, share * nowcast.expected_at(FORECAST, end, LAT, LON)) for end in ends)


def test_trust_falls_with_the_distance() -> None:
    assert nowcast.trust(0.0) == 1.0
    assert nowcast.trust(15.0) == 0.5
    assert nowcast.trust(30.0) == 0.0 and nowcast.trust(80.0) == 0.0


def test_the_stations_worth_asking() -> None:
    stations = [
        Place("FAR", LAT + 0.40, LON, 550.0),  # 40 km: under another sky
        Place("HIGH", LAT + 0.02, LON, 1600.0),  # above the fog while the plant is not
        Place("B", LAT + 0.10, LON, 600.0),
        Place("A", LAT, LON + 0.03, 520.0),
        Place("C", LAT - 0.15, LON, 480.0),
        Place("D", LAT, LON - 0.20, None),  # no height known: no reason to leave it out
        Place("E", LAT + 0.24, LON, 540.0),
    ]
    found = nowcast.candidates(stations, LAT, LON, 542.0, _flat_km)
    assert [station.abbr for _km, station in found] == ["A", "B", "C", "D"]  # the nearest four
    assert [round(km) for km, _station in found] == [3, 10, 15, 20]
    # a plant whose height is not known takes the nearest, whatever their height
    assert [station.abbr for _km, station in nowcast.candidates(stations, LAT, LON, None, _flat_km)][0] == "HIGH"
    assert nowcast.candidates(stations[:2], LAT, LON, 542.0, _flat_km) == []


def test_the_forecast_of_ten_minutes_follows_the_sun_within_the_hour() -> None:
    morning = datetime(2026, 6, 21, 6, 0, tzinfo=UTC)
    forecast = {morning: 300.0}
    steps = [nowcast.expected_at(forecast, morning - HOUR + index * STEP, LAT, LON) for index in range(1, 7)]
    assert steps == sorted(steps) and steps[0] < 300.0 < steps[-1]  # the sun climbs
    assert abs(sum(steps) / 6 - 300.0) < 1e-6  # the hour's mean is kept
    assert nowcast.expected_at(forecast, morning + STEP, LAT, LON) is None  # an hour the forecast does not cover
    night = datetime(2026, 6, 21, 23, 0, tzinfo=UTC)
    assert nowcast.expected_at({night: 0.0}, night, LAT, LON) is None  # no sun to shape the hour by


def test_a_station_under_cloud_lowers_the_hours_ahead() -> None:
    found = nowcast.correction(_measured(0.5), FORECAST, NOW, LAT, LON, 6.0)
    assert found is not None
    assert found.ratio == 0.5 and found.trust == 0.8 and found.measured_at == NOON + STEP
    assert abs(found.measured / found.expected - 0.5) < 0.01 and 700 < found.expected < 900
    assert found.fresh(NOW) and found.fresh(NOW + HOUR) and not found.fresh(NOW + 2 * HOUR)
    # the running hour takes most of it, three hours on nothing is left
    weights = [found.weight(NOON + index * HOUR) for index in range(5)]
    assert weights == sorted(weights, reverse=True)
    assert abs(weights[0] - 0.8 * (1 - 20 / 180)) < 1e-9 and weights[3] == 0.0 and weights[4] == 0.0
    assert found.weight(NOON - HOUR) == 0.8  # never more than the station is trusted
    assert abs(found.factor(NOON) - (1 - 0.5 * weights[0])) < 1e-9 and found.factor(NOON + 4 * HOUR) == 1.0


def test_a_brighter_sky_raises_them_and_the_ratio_is_limited() -> None:
    assert nowcast.correction(_measured(1.2), FORECAST, NOW, LAT, LON, 0.0).ratio == 1.2
    assert nowcast.correction(_measured(3.0), FORECAST, NOW, LAT, LON, 0.0).ratio == nowcast.RATIO_MAX
    assert nowcast.correction(_measured(0.05), FORECAST, NOW, LAT, LON, 0.0).ratio == nowcast.RATIO_MIN
    assert nowcast.correction(_measured(1.0), FORECAST, NOW, LAT, LON, 0.0).factor(NOON) == 1.0


def test_nothing_sound_to_compare_is_no_correction() -> None:
    assert nowcast.correction([], FORECAST, NOW, LAT, LON, 6.0) is None
    assert nowcast.correction(_measured(0.5, count=2), FORECAST, NOW, LAT, LON, 6.0) is None  # twenty minutes say little
    assert nowcast.correction(_measured(0.5, newest=NOON - HOUR), FORECAST, NOW, LAT, LON, 6.0) is None  # the station fell silent
    assert nowcast.correction(_measured(0.5), FORECAST, NOW, LAT, LON, 30.0) is None  # too far to be believed
    assert nowcast.correction(_measured(0.5), {}, NOW, LAT, LON, 6.0) is None  # no forecast for the time
    dull = {end: 30.0 for end in FORECAST}
    values = [(end, 10.0) for end, _value in _measured(0.5)]
    assert nowcast.correction(values, dull, NOW, LAT, LON, 6.0) is None  # too little forecast for a ratio
    # a sun just over the horizon is behind hills and haze
    dawn = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)
    low = {dawn + index * HOUR: 120.0 for index in range(-3, 3)}
    values = [(dawn - index * STEP, 20.0) for index in range(6)]
    assert nowcast.correction(values, low, dawn + timedelta(minutes=5), LAT, LON, 6.0) is None


def test_only_the_last_hour_counts_and_nothing_from_the_future() -> None:
    # sunny until an hour ago, under cloud since
    values = _measured(1.0, newest=NOON - 5 * STEP, count=12) + _measured(0.5)
    assert nowcast.correction(values, FORECAST, NOW, LAT, LON, 0.0).ratio == 0.5
    # a value stamped later than the clock allows is left out
    ahead = [*_measured(0.5), (NOW + HOUR, 5000.0)]
    found = nowcast.correction(ahead, FORECAST, NOW, LAT, LON, 0.0)
    assert found.ratio == 0.5 and found.measured_at == NOON + STEP
    # a sensor's small negative reading at the edge of the day is none
    assert nowcast.correction([(end, -3.0) for end, _value in _measured(0.5)], FORECAST, NOW, LAT, LON, 0.0).measured == 0.0


def test_the_correction_on_the_forecast_rows() -> None:
    found = Correction(ratio=0.5, measured=400.0, expected=800.0, measured_at=NOON + STEP, trust=1.0)
    rows = [(NOON + index * HOUR, 800.0, 300.0, 20.0, 2.0) for index in range(-1, 6)]
    rows.append((NOON + 6 * HOUR, None, None, 18.0, 1.0))
    out = nowcast.corrected(rows, found, NOW)
    assert len(out) == len(rows)
    assert out[0] is rows[0] and out[1] is rows[1]  # the hours that are over
    running = out[2]
    assert running[0] == NOON + HOUR and abs(running[1] - 800.0 * found.factor(NOON)) < 1e-9
    assert running[2] == 300.0 and running[3:] == (20.0, 2.0)  # what is taken is direct light
    assert [round(row[1]) for row in out[2:7]] == [444, 578, 711, 800, 800]  # fading out over three hours
    assert out[5] is rows[5] and out[7] is rows[7]  # out of reach, and an hour without radiation
    # deep under cloud the diffuse light cannot exceed what arrives at all
    deep = Correction(ratio=0.3, measured=240.0, expected=800.0, measured_at=NOON + STEP, trust=1.0)
    grey = nowcast.corrected([(NOON + HOUR, 800.0, 600.0, 20.0, 2.0)], deep, NOW)[0]
    assert grey[2] == grey[1] < 600.0
    # no correction, or one grown old, changes nothing
    assert nowcast.corrected(rows, None, NOW) is rows
    assert nowcast.corrected(rows, found, NOW + 2 * HOUR) is rows
