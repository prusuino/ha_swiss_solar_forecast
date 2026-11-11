"""Short-term correction from the radiation a station measures.

A forecast run is some hours old when it is used, and clouds do not keep
to it. A measurement station nearby sees what the sky really does: when
it received less (or more) global radiation in the last hour than the
forecast had for that hour, the hours just ahead are likely off the same
way. So the ratio of the two corrects the forecast's radiation for the
current hour and, fading out, for the next three. Beyond that the model
run knows better than the last hour's sky.

The correction is cautious:

* It needs a station close by and at a similar height; the farther the
  station, the less its ratio counts.
* It needs a sun well above the horizon, a forecast worth comparing and
  at least half an hour of recent measurements.
* The ratio is limited, and hours that are over are never touched.

Pure arithmetic and plain data, so it can be tested without a network.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .solar import sun_position

WINDOW = timedelta(minutes=60)  # the measurements compared with the forecast
MAX_AGE = timedelta(minutes=40)  # of the newest measurement when the ratio is formed
FRESH = timedelta(minutes=75)  # a ratio older than this corrects nothing any more
MIN_VALUES = 3  # ten-minute values in the window
MIN_EXPECTED = 50.0  # W/m² forecast on average: below it a ratio says nothing
MIN_ELEVATION = 5.0  # degrees: a lower sun is behind hills and haze
RATIO_MIN = 0.3
RATIO_MAX = 1.5
REACH_HOURS = 3.0  # the correction fades to nothing over this lead
MAX_DISTANCE_KM = 25.0  # a station farther away is under another sky
TRUST_DISTANCE_KM = 30.0  # the ratio's weight falls to nothing at this distance
MAX_HEIGHT_DIFFERENCE_M = 300.0  # above or below the fog while the plant is not
CANDIDATES = 4  # the nearest stations tried for radiation
_STEP = timedelta(minutes=10)
_HOUR = timedelta(hours=1)
_HALF = timedelta(minutes=30)


@dataclass(frozen=True)
class Correction:
    """What the station measured against what was forecast for the same time."""

    ratio: float  # measured / forecast over the last hour, limited
    measured: float  # W/m², mean of the window
    expected: float  # W/m², the forecast's mean over the same ten-minute steps
    measured_at: datetime  # end of the newest ten minutes
    trust: float  # 0…1, from the station's distance

    def fresh(self, now: datetime) -> bool:
        return now - self.measured_at <= FRESH

    def weight(self, start: datetime) -> float:
        """How much of the ratio applies to the hour that starts at `start`."""
        lead = (start + _HALF - self.measured_at).total_seconds() / 3600.0
        return self.trust * max(0.0, 1.0 - max(0.0, lead) / REACH_HOURS)

    def factor(self, start: datetime) -> float:
        """The factor on the forecast radiation of the hour that starts at `start`."""
        return 1.0 + (self.ratio - 1.0) * self.weight(start)


def trust(distance_km: float) -> float:
    """What a station's ratio counts at a distance."""
    return max(0.0, min(1.0, 1.0 - distance_km / TRUST_DISTANCE_KM))


def candidates(stations: Iterable, lat: float, lon: float, height: float | None, distance_km: Callable[[float, float, float, float], float]) -> list[tuple[float, object]]:
    """The stations worth asking for radiation: the nearest ones within
    reach and at a similar height, each with its distance in km.
    `distance_km` measures between two places."""
    usable = []
    for station in stations:
        km = distance_km(lat, lon, station.lat, station.lon)
        if km > MAX_DISTANCE_KM:
            continue
        if height is not None and station.height is not None and abs(station.height - height) > MAX_HEIGHT_DIFFERENCE_M:
            continue
        usable.append((km, station))
    usable.sort(key=lambda item: item[0])
    return usable[:CANDIDATES]


def expected_at(forecast: dict[datetime, float | None], end: datetime, lat: float, lon: float) -> float | None:
    """The forecast radiation of the ten minutes that end at `end`: the
    hour's mean (kept under the hour's end), shaped by how high the sun
    stands in each sixth of the hour."""
    whole = end.replace(minute=0, second=0, microsecond=0)
    hour_end = whole if end == whole else whole + _HOUR
    mean = forecast.get(hour_end)
    if mean is None:
        return None
    hour_start = hour_end - _HOUR
    heights = [max(0.0, math.sin(math.radians(sun_position(hour_start + _STEP * n + _STEP / 2, lat, lon).elevation))) for n in range(6)]
    total = sum(heights)
    if total <= 0:
        return None
    index = min(5, max(0, round((end - hour_start) / _STEP) - 1))
    return mean * heights[index] * 6.0 / total


def correction(
    measured: list[tuple[datetime, float]], forecast: dict[datetime, float | None], now: datetime, lat: float, lon: float, distance_km: float
) -> Correction | None:
    """The ratio of the station's last hour to the forecast of the same
    time, or None when there is nothing sound to compare. `measured` holds
    (end of the ten minutes, W/m²), `forecast` the hourly means by hour end."""
    known = sorted((end, value) for end, value in measured if end <= now + _STEP)
    if not known:
        return None
    newest = known[-1][0]
    if now - newest > MAX_AGE or sun_position(newest, lat, lon).elevation < MIN_ELEVATION:
        return None
    pairs = []
    for end, value in known:
        if end <= newest - WINDOW:
            continue
        expected = expected_at(forecast, end, lat, lon)
        if expected is not None:
            pairs.append((max(0.0, value), expected))
    if len(pairs) < MIN_VALUES:
        return None
    measured_mean = sum(value for value, _expected in pairs) / len(pairs)
    expected_mean = sum(expected for _value, expected in pairs) / len(pairs)
    weight = trust(distance_km)
    if expected_mean < MIN_EXPECTED or weight <= 0:
        return None
    ratio = min(RATIO_MAX, max(RATIO_MIN, measured_mean / expected_mean))
    return Correction(ratio=round(ratio, 3), measured=round(measured_mean, 1), expected=round(expected_mean, 1), measured_at=newest, trust=round(weight, 3))


def corrected(rows: list[tuple], found: Correction | None, now: datetime) -> list[tuple]:
    """The forecast rows (hour end, global, diffuse, …) with the correction
    on the hours not yet over. What is added or taken is direct light: the
    diffuse share stays, limited to the new global radiation."""
    if found is None or not found.fresh(now):
        return rows
    out = []
    for row in rows:
        end, ghi, dhi = row[0], row[1], row[2]
        factor = found.factor(end - _HOUR)
        if ghi is None or end <= now or factor == 1.0:
            out.append(row)
            continue
        new = ghi * factor
        out.append((end, new, None if dhi is None else min(dhi, new), *row[3:]))
    return out
