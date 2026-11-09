"""The terrain horizon: plane coordinates, the fan, the angle under which
terrain is seen, the horizon from heights along the lines, and the read
from the geo API with a faked answer."""
from __future__ import annotations

import json
import math

import pytest

from custom_components.swiss_solar_forecast.horizon import (
    DIRECTIONS_PER_REQUEST,
    RANGE_M,
    Horizon,
    HorizonError,
    async_fetch_horizon,
    destination,
    directions,
    elevation_angle,
    horizon_from_samples,
    wgs84_to_lv95,
)

BERN = (46.948, 7.447)
CENTRE = (2600000.0, 1200000.0)


def _ends(centre=CENTRE):
    return [(centre[0] + RANGE_M * math.sin(math.radians(a)), centre[1] + RANGE_M * math.cos(math.radians(a))) for a in directions()]


def test_plane_coordinates_of_the_reference_point() -> None:
    # the origin of the Swiss grid (old observatory of Bern) is 2 600 000 / 1 200 000
    east, north = wgs84_to_lv95(46.951082877, 7.438632495)
    assert abs(east - 2600000.0) < 1.5
    assert abs(north - 1200000.0) < 1.5


def test_destination_north_and_east() -> None:
    lat, lon = destination(47.0, 8.0, 0.0, 25000.0)
    assert abs(lat - 47.2248) < 0.001 and abs(lon - 8.0) < 1e-6
    lat, lon = destination(47.0, 8.0, 90.0, 25000.0)
    assert abs(lat - 47.0) < 0.002 and 8.32 < lon < 8.34


def test_fan_covers_where_the_sun_can_stand() -> None:
    fan = directions()
    assert len(fan) == 37 and fan[0] == 45.0 and fan[-1] == 315.0 and fan[18] == 180.0


def test_elevation_angle_with_curvature() -> None:
    # a ridge 1000 m above the place, 10 km away: a little under atan(0.1) = 5.71 degrees
    angle = elevation_angle(10000.0, 1500.0, 500.0)
    assert 5.6 < angle < 5.7
    # the same height as the place, far away, lies under the horizontal
    assert elevation_angle(20000.0, 500.0, 500.0) < 0


def test_horizon_interpolates_between_directions() -> None:
    elevations = [0.0] * 37
    elevations[0], elevations[1], elevations[36] = 10.0, 20.0, 8.0
    horizon = Horizon(lat=47.0, lon=8.0, site_height=500.0, elevations=tuple(elevations))
    assert horizon.elevation_at(45.0) == 10.0
    assert abs(horizon.elevation_at(48.75) - 15.0) < 1e-9
    assert horizon.elevation_at(315.0) == 8.0
    assert horizon.elevation_at(0.0) == 0.0 and horizon.elevation_at(330.0) == 0.0  # outside the fan
    assert horizon.elevation_at(405.0) == 10.0  # 405 is 45
    assert horizon.highest == (20.0, 52.5)
    assert horizon.profile()["52.5"] == 20.0 and len(horizon.profile()) == 37


def test_horizon_round_trip_and_place() -> None:
    horizon = Horizon(lat=47.0, lon=8.0, site_height=512.3, elevations=tuple(float(i) for i in range(37)), fetched="2026-06-21T04:00:00+00:00")
    assert Horizon.from_dict(horizon.as_dict()) == horizon
    assert horizon.matches(47.0, 8.0) and not horizon.matches(47.001, 8.0)
    assert Horizon.from_dict(None) is None
    assert Horizon.from_dict({"lat": 47.0, "lon": 8.0, "site_height": 500, "elevations": [1.0, 2.0]}) is None  # another fan
    assert Horizon.from_dict({"lat": "x", "lon": 8.0, "site_height": 500, "elevations": [0.0] * 37}) is None


def test_horizon_from_heights_along_the_lines() -> None:
    samples = [
        (CENTRE[0], CENTRE[1], 500.0),  # the place
        (CENTRE[0], CENTRE[1] - 5000.0, 1500.0),  # a ridge 5 km to the south
        (CENTRE[0], CENTRE[1] - 2000.0, 700.0),  # a lower hill in front of it
        (CENTRE[0] + 20.0, CENTRE[1], 5000.0),  # on the plot itself: no horizon
        (CENTRE[0] - 3000.0, CENTRE[1], 300.0),  # lower terrain to the west
        (CENTRE[0] + 4000.0, CENTRE[1] + 300.0, 4000.0),  # between two directions: on no line
    ]
    horizon = horizon_from_samples(47.0, 8.0, CENTRE, _ends(), samples, fetched="now")
    assert horizon.site_height == 500.0 and horizon.fetched == "now"
    assert 11.1 < horizon.elevation_at(180.0) < 11.4  # atan(995 / 5000)
    assert horizon.elevation_at(270.0) == 0.0  # never negative
    assert horizon.elevation_at(90.0) == 0.0
    assert sum(1 for e in horizon.elevations if e > 0) == 1


def test_horizon_needs_the_height_of_the_place() -> None:
    with pytest.raises(HorizonError):
        horizon_from_samples(47.0, 8.0, CENTRE, _ends(), [(CENTRE[0], CENTRE[1] - 5000.0, 1500.0)])


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self) -> bytes:
        return self._body


class _GeoApi:
    """Answers like the profile service: heights every 50 m along the line.
    The terrain is a plain at 500 m with a wall of 2500 m south of
    `wall_north`."""

    def __init__(self, wall_north: float, status: int = 200) -> None:
        self.wall_north = wall_north
        self.status = status
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self.status not in (200, 203):
            return _Response(self.status, b"")
        line = json.loads(params["geom"])["coordinates"]
        out = [{"alts": {"COMB": None, "DTM2": None, "DTM25": None}, "dist": 0, "easting": 0.0, "northing": 0.0}]  # a hole in the model
        for (e1, n1), (e2, n2) in zip(line, line[1:]):
            steps = max(1, int(math.hypot(e2 - e1, n2 - n1) // 50))
            for i in range(steps + 1):
                east, north = e1 + (e2 - e1) * i / steps, n1 + (n2 - n1) * i / steps
                height = 2500.0 if north < self.wall_north else 500.0
                out.append({"alts": {"COMB": height, "DTM2": height, "DTM25": round(height)}, "dist": 0, "easting": east, "northing": north})
        return _Response(self.status, json.dumps(out).encode())


async def test_fetch_reads_the_fan_with_a_few_requests() -> None:
    centre = wgs84_to_lv95(*BERN)
    api = _GeoApi(wall_north=centre[1] - 10000.0)
    horizon, requests, nbytes = await async_fetch_horizon(api, *BERN)
    assert requests == len(api.calls) == math.ceil(37 / DIRECTIONS_PER_REQUEST) == 5
    assert nbytes > 0
    assert all(call["params"]["sr"] == "2056" and int(call["params"]["nb_points"]) <= 5000 for call in api.calls)
    assert "SwissSolarForecast" in api.calls[0]["headers"]["User-Agent"]
    assert horizon.site_height == 500.0 and horizon.matches(*BERN)
    assert 11.0 < horizon.elevation_at(180.0) < 11.4  # 1995 m above the modules at 10 km
    assert 0 < horizon.elevation_at(135.0) < horizon.elevation_at(180.0)  # the wall is farther away to the south-east
    assert horizon.elevation_at(90.0) == 0.0 and horizon.elevation_at(45.0) == 0.0 and horizon.elevation_at(270.0) == 0.0


async def test_fetch_takes_an_answer_with_another_number_of_points() -> None:
    # the service answers 203 when it returns fewer or more points than asked for
    centre = wgs84_to_lv95(*BERN)
    horizon, requests, _ = await async_fetch_horizon(_GeoApi(wall_north=centre[1] - 10000.0, status=203), *BERN)
    assert requests == 5 and 11.0 < horizon.elevation_at(180.0) < 11.4


async def test_fetch_failure_is_reported() -> None:
    with pytest.raises(HorizonError):
        await async_fetch_horizon(_GeoApi(wall_north=0.0, status=503), *BERN)
