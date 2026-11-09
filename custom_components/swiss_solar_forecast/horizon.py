"""The terrain horizon of a place, from the swisstopo elevation model.

A plant in a valley loses the direct sun earlier than the weather model
knows: the model sees the terrain in cells of a kilometre or more, the
ridge behind the house it does not. The federal geo API (api3.geo.admin.ch,
open government data, no key) returns the terrain height along a line; a
fan of such lines from the place outwards, one per compass direction the
sun can stand in, gives the angle under which the terrain is seen in each
direction. An hour's direct beam counts only while the sun stands above
that angle; the diffuse light of the sky is not touched.

The elevation model is the bare terrain (swissALTI3D, and a coarser model
beyond the border): mountains, hills and valley flanks. Trees, buildings
and the neighbour's roof are not in it.

The fan is read once per place with a handful of requests (several
directions share one request) and kept; the terrain does not change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from .const import USER_AGENT

_LOGGER = logging.getLogger(__name__)

PROFILE_URL = "https://api3.geo.admin.ch/rest/services/profile.json"
ATTRIBUTION_TERRAIN = "Source: swisstopo (elevation model, open government data)"
_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10)
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# The sun never stands north of about 53° / 307° in Switzerland, so the fan
# covers north-east over south to north-west.
AZIMUTH_FROM = 45.0
AZIMUTH_TO = 315.0
AZIMUTH_STEP = 7.5
RANGE_M = 25000.0  # beyond this even a high ridge stays under two or three degrees
SPACING_M = 50.0  # distance between two heights along a line
MAX_POINTS = 5000  # the limit of the service per request
DIRECTIONS_PER_REQUEST = 8
OBSERVER_HEIGHT_M = 5.0  # the modules sit on a roof, not on the ground
MIN_DISTANCE_M = 30.0  # the plot itself is no horizon
_EARTH_RADIUS_M = 6371000.0
_REFRACTION = 0.13  # the air bends the line of sight: the earth seems a little flatter
_BEARING_TOLERANCE = 0.5  # degrees: a height belongs to a direction when it lies on its line


class HorizonError(Exception):
    """The terrain could not be read."""


def wgs84_to_lv95(lat: float, lon: float) -> tuple[float, float]:
    """Swiss plane coordinates (LV95 east, north) of a WGS84 position, after
    the approximate formulas published by swisstopo (good to about a metre)."""
    phi = (lat * 3600.0 - 169028.66) / 10000.0
    lam = (lon * 3600.0 - 26782.5) / 10000.0
    east = 2600072.37 + 211455.93 * lam - 10938.51 * lam * phi - 0.36 * lam * phi**2 - 44.54 * lam**3
    north = 1200147.07 + 308807.95 * phi + 3745.25 * lam**2 + 76.63 * phi**2 - 194.56 * lam**2 * phi + 119.79 * phi**3
    return east, north


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    """The position a distance away in a compass direction."""
    lat_r, lon_r, brg = math.radians(lat), math.radians(lon), math.radians(bearing)
    ang = distance_m / _EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat_r) * math.cos(ang) + math.cos(lat_r) * math.sin(ang) * math.cos(brg))
    lon2 = lon_r + math.atan2(math.sin(brg) * math.sin(ang) * math.cos(lat_r), math.cos(ang) - math.sin(lat_r) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def directions() -> list[float]:
    """The compass directions of the fan."""
    count = int(round((AZIMUTH_TO - AZIMUTH_FROM) / AZIMUTH_STEP)) + 1
    return [AZIMUTH_FROM + i * AZIMUTH_STEP for i in range(count)]


def elevation_angle(distance_m: float, height_m: float, site_height_m: float) -> float:
    """The angle under which a terrain point is seen from the modules, with
    the curvature of the earth (and the bending of the light) taken off."""
    drop = distance_m**2 / (2.0 * _EARTH_RADIUS_M) * (1.0 - _REFRACTION)
    return math.degrees(math.atan2(height_m - drop - (site_height_m + OBSERVER_HEIGHT_M), distance_m))


@dataclass(frozen=True)
class Horizon:
    """The elevation angle of the terrain per compass direction of the fan."""

    lat: float
    lon: float
    site_height: float  # terrain height at the place, m above sea level
    elevations: tuple[float, ...]  # degrees, one per direction of the fan, never negative
    fetched: str = ""  # ISO time of the read

    def elevation_at(self, azimuth: float) -> float:
        """The horizon in a compass direction, interpolated between the two
        neighbouring directions of the fan; zero outside the fan."""
        azimuth %= 360.0
        if not self.elevations or azimuth < AZIMUTH_FROM or azimuth > AZIMUTH_TO:
            return 0.0
        pos = (azimuth - AZIMUTH_FROM) / AZIMUTH_STEP
        low = min(int(pos), len(self.elevations) - 1)
        high = min(low + 1, len(self.elevations) - 1)
        frac = pos - low
        return self.elevations[low] * (1.0 - frac) + self.elevations[high] * frac

    @property
    def highest(self) -> tuple[float, float]:
        """(elevation, azimuth) of the highest point of the horizon."""
        if not self.elevations:
            return 0.0, 180.0
        index = max(range(len(self.elevations)), key=lambda i: self.elevations[i])
        return self.elevations[index], AZIMUTH_FROM + index * AZIMUTH_STEP

    def profile(self) -> dict[str, float]:
        """Azimuth -> elevation, for the attributes."""
        return {f"{AZIMUTH_FROM + i * AZIMUTH_STEP:g}": e for i, e in enumerate(self.elevations)}

    def matches(self, lat: float, lon: float) -> bool:
        """Whether this horizon was read for that place (to about a metre)."""
        return abs(self.lat - lat) < 1e-5 and abs(self.lon - lon) < 1e-5

    def as_dict(self) -> dict:
        return {"lat": self.lat, "lon": self.lon, "site_height": self.site_height, "elevations": list(self.elevations), "fetched": self.fetched}

    @classmethod
    def from_dict(cls, data: object) -> Horizon | None:
        if not isinstance(data, dict):
            return None
        try:
            elevations = tuple(float(v) for v in data["elevations"])
            if len(elevations) != len(directions()):
                return None
            return cls(lat=float(data["lat"]), lon=float(data["lon"]), site_height=float(data["site_height"]), elevations=elevations, fetched=str(data.get("fetched") or ""))
        except (KeyError, TypeError, ValueError):
            return None


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _path(centre: tuple[float, float], ends: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """One line through several directions: out along the first, across to
    the end of the second, back to the place, and so on. Almost every metre
    of it lies on one of the directions."""
    path = [centre]
    for i in range(0, len(ends), 2):
        path.append(ends[i])
        if i + 1 < len(ends):
            path.append(ends[i + 1])
            path.append(centre)
    return path


def _length(path: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def horizon_from_samples(
    lat: float, lon: float, centre: tuple[float, float], ends: list[tuple[float, float]], samples: list[tuple[float, float, float]], fetched: str = ""
) -> Horizon:
    """The horizon from terrain heights (east, north, height): every height
    is assigned to the direction whose line it lies on, and the steepest
    view per direction is the horizon there. `ends` holds the far end of
    every direction of the fan, in the order of the fan."""
    bearings = [math.degrees(math.atan2(e - centre[0], n - centre[1])) % 360.0 for e, n in ends]
    site = None
    for east, north, height in samples:
        if math.hypot(east - centre[0], north - centre[1]) < 1.0:
            site = height
            break
    if site is None:
        raise HorizonError("no terrain height at the place itself")
    best = [0.0] * len(ends)
    for east, north, height in samples:
        de, dn = east - centre[0], north - centre[1]
        dist = math.hypot(de, dn)
        if dist < MIN_DISTANCE_M:
            continue
        bearing = math.degrees(math.atan2(de, dn)) % 360.0
        for index, target in enumerate(bearings):
            if abs((bearing - target + 180.0) % 360.0 - 180.0) <= _BEARING_TOLERANCE:
                best[index] = max(best[index], elevation_angle(dist, height, site))
                break
    return Horizon(lat=lat, lon=lon, site_height=round(site, 1), elevations=tuple(round(v, 2) for v in best), fetched=fetched)


def _samples(raw: bytes) -> list[tuple[float, float, float]]:
    """The answer of the profile service as (east, north, height)."""
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise HorizonError("unreadable answer from the geo API") from err
    if not isinstance(data, list):
        raise HorizonError("unexpected answer from the geo API")
    out: list[tuple[float, float, float]] = []
    for item in data:
        try:
            alts = item.get("alts") or {}
            height = alts.get("COMB")
            if height is None:
                height = alts.get("DTM2")
            if height is None:
                height = alts.get("DTM25")
            if height is None:
                continue  # outside the elevation model
            out.append((float(item["easting"]), float(item["northing"]), float(height)))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return out


async def _async_profile(session: aiohttp.ClientSession, path: list[tuple[float, float]]) -> tuple[list[tuple[float, float, float]], int]:
    """The terrain heights along a line: (east, north, height) and the bytes read."""
    geom = {"type": "LineString", "coordinates": [[round(e, 1), round(n, 1)] for e, n in path]}
    points = max(2, min(MAX_POINTS, math.ceil(_length(path) / SPACING_M)))
    params = {"geom": json.dumps(geom, separators=(",", ":")), "sr": "2056", "nb_points": str(points), "distinct_points": "true"}
    try:
        async with session.get(PROFILE_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT) as resp:
            # 203: the service returned another number of points than asked for (it drops doubles at
            # the corners of the line); the heights are the same
            if resp.status not in (200, 203):
                raise HorizonError(f"HTTP {resp.status} from the geo API")
            raw = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HorizonError(f"{type(err).__name__} from the geo API") from err
    # some thousand heights per answer: parsed off the event loop
    return await asyncio.get_running_loop().run_in_executor(None, _samples, raw), len(raw)


async def async_fetch_horizon(session: aiohttp.ClientSession, lat: float, lon: float, now: datetime | None = None) -> tuple[Horizon, int, int]:
    """The horizon of a place: (horizon, requests made, bytes read)."""
    centre = wgs84_to_lv95(lat, lon)
    centre = (round(centre[0], 1), round(centre[1], 1))
    ends = [wgs84_to_lv95(*destination(lat, lon, azimuth, RANGE_M)) for azimuth in directions()]
    ends = [(round(e, 1), round(n, 1)) for e, n in ends]
    samples: list[tuple[float, float, float]] = []
    requests = nbytes = 0
    for chunk in _chunks(ends, DIRECTIONS_PER_REQUEST):
        part, size = await _async_profile(session, _path(centre, chunk))
        samples += part
        requests += 1
        nbytes += size
    fetched = now.isoformat() if now else ""
    horizon = await asyncio.get_running_loop().run_in_executor(None, horizon_from_samples, lat, lon, centre, ends, samples, fetched)
    _LOGGER.debug("Terrain horizon read: %d requests, %d bytes, highest %.1f° at %.0f°", requests, nbytes, *horizon.highest)
    return horizon, requests, nbytes
