"""From irradiance on the horizontal to the power of a tilted PV plane.

MeteoSwiss forecasts the global and the diffuse radiation on a horizontal
surface for every hour. For a module plane the three parts are treated
separately: the direct beam is projected with the angle between sun and
module normal, the diffuse share with the isotropic sky model (the plane
sees a part of the sky dome that depends on its tilt) and the ground
reflection with the albedo. The cell temperature follows the NOCT
convention and lowers the output on warm days; system losses and an
optional inverter limit give the AC power. Pure arithmetic, no external
library, so every step can be tested with known numbers.

Sun position after the standard formulas (Spencer's declination and
equation of time), accurate to a fraction of a degree, which is far
below the uncertainty of the forecast itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .const import ALBEDO, NOCT, TEMPERATURE_COEFFICIENT


@dataclass(frozen=True)
class Plane:
    """One module plane: what the user configures."""

    kwp: float
    azimuth: float  # compass bearing the modules face: 0 north, 90 east, 180 south, 270 west
    tilt: float  # from horizontal
    losses: float = 14.0  # percent
    inverter_kw: float = 0.0  # 0 = no clipping
    damping: float = 1.0


@dataclass(frozen=True)
class SunPosition:
    elevation: float  # degrees above the horizon
    azimuth: float  # compass bearing, 0 north, 90 east

    @property
    def zenith(self) -> float:
        return 90.0 - self.elevation


def sun_position(when: datetime, lat: float, lon: float) -> SunPosition:
    """The sun's elevation and azimuth at a moment, for a place."""
    when = when.astimezone(UTC)
    day_of_year = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60 + when.second / 3600
    # fractional year, radians
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    time_offset = eqtime + 4 * lon  # minutes, UTC-based
    true_solar_time = hours * 60 + time_offset
    hour_angle = math.radians(true_solar_time / 4 - 180)
    lat_r = math.radians(lat)
    cos_zenith = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)
    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-6 or abs(math.cos(lat_r)) < 1e-6:
        azimuth = 180.0
    else:
        # bearing from north, 0..180 on the eastern side (morning), mirrored in the afternoon
        cos_az = (math.sin(decl) - math.sin(lat_r) * cos_zenith) / (math.cos(lat_r) * sin_zenith)
        cos_az = max(-1.0, min(1.0, cos_az))
        bearing = math.degrees(math.acos(cos_az))
        azimuth = bearing if hour_angle <= 0 else 360.0 - bearing
    return SunPosition(elevation=elevation, azimuth=azimuth % 360.0)


def plane_irradiance(ghi: float, dhi: float | None, sun: SunPosition, plane: Plane) -> float:
    """Irradiance on the module plane in W/m² from the global (and, when
    forecast, the diffuse) irradiance on the horizontal."""
    ghi = max(0.0, ghi)
    if ghi <= 0:
        return 0.0
    if dhi is None or dhi < 0 or dhi > ghi:
        dhi = _diffuse_estimate(ghi, sun)
    dhi = min(ghi, max(0.0, dhi))
    tilt_r = math.radians(plane.tilt)
    beam_h = ghi - dhi
    beam = 0.0
    if sun.elevation > 1.0 and beam_h > 0:
        cos_zenith = math.cos(math.radians(sun.zenith))
        cos_aoi = _cos_incidence(sun, plane)
        if cos_aoi > 0 and cos_zenith > 0.05:
            beam = beam_h / cos_zenith * cos_aoi
            # a low sun on the horizontal file cannot produce more on the plane than a plausible normal irradiance
            beam = min(beam, 1100.0 * cos_aoi)
    diffuse = dhi * (1 + math.cos(tilt_r)) / 2
    reflected = ghi * ALBEDO * (1 - math.cos(tilt_r)) / 2
    return beam + diffuse + reflected


def _cos_incidence(sun: SunPosition, plane: Plane) -> float:
    """Cosine of the angle between the sun and the module normal."""
    zen = math.radians(sun.zenith)
    tilt = math.radians(plane.tilt)
    return math.cos(zen) * math.cos(tilt) + math.sin(zen) * math.sin(tilt) * math.cos(math.radians(sun.azimuth - plane.azimuth))


def _diffuse_estimate(ghi: float, sun: SunPosition) -> float:
    """Diffuse share when the forecast has none: Erbs' correlation on the
    clearness index (global over extraterrestrial irradiance)."""
    if sun.elevation <= 0:
        return ghi
    extra = 1361.0 * math.sin(math.radians(sun.elevation))
    if extra <= 0:
        return ghi
    kt = min(1.0, ghi / extra)
    if kt <= 0.22:
        fraction = 1 - 0.09 * kt
    elif kt <= 0.8:
        fraction = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        fraction = 0.165
    return ghi * max(0.0, min(1.0, fraction))


def plane_power_kw(poa: float, air_temperature: float | None, plane: Plane) -> float:
    """AC power of the plane in kW for an irradiance on the plane."""
    if poa <= 0 or plane.kwp <= 0:
        return 0.0
    temp = 20.0 if air_temperature is None else air_temperature
    cell = temp + (NOCT - 20.0) / 800.0 * poa
    dc = plane.kwp * poa / 1000.0 * (1 + TEMPERATURE_COEFFICIENT * (cell - 25.0))
    ac = max(0.0, dc) * (1 - plane.losses / 100.0)
    if plane.inverter_kw > 0:
        ac = min(ac, plane.inverter_kw)
    return ac * plane.damping


@dataclass(frozen=True)
class HourForecast:
    """One forecast hour: the period it covers starts at `start`."""

    start: datetime
    ghi: float
    dhi: float | None
    temperature: float | None
    poa: float
    power_kw: float

    @property
    def energy_wh(self) -> float:
        return self.power_kw * 1000.0


def forecast_hours(
    rows: list[tuple[datetime, float | None, float | None, float | None]], lat: float, lon: float, plane: Plane
) -> list[HourForecast]:
    """The hourly plane output from rows of (timestamp, ghi, dhi,
    temperature). A MeteoSwiss hourly mean covers the hour that ends at its
    timestamp; the sun is taken at the middle of that hour."""
    out: list[HourForecast] = []
    for ts, ghi, dhi, temp in rows:
        if ghi is None:
            continue
        start = ts - timedelta(hours=1)
        sun = sun_position(start + timedelta(minutes=30), lat, lon)
        poa = plane_irradiance(ghi, dhi, sun, plane)
        out.append(HourForecast(start=start, ghi=ghi, dhi=dhi, temperature=temp, poa=poa, power_kw=plane_power_kw(poa, temp, plane)))
    return out
