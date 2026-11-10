"""From irradiance on the horizontal to the power of a tilted PV plane.

MeteoSwiss forecasts the global and the diffuse radiation on a horizontal
surface for every hour. For a module plane the parts are treated
separately: the direct beam is projected with the angle between sun and
module normal; the diffuse light is split after Hay & Davies into the
bright ring around the sun, which follows the beam, and the rest of the
sky, of which the plane sees the part its tilt and the terrain leave; the
ground reflection follows the albedo, which rises with snow. Each part
loses a little at the module glass, the more the flatter it falls in
(Martin & Ruiz). The cell temperature follows Faiman's model, so wind
cools the modules, and lowers the output on warm days; system losses and
an optional inverter limit give the AC power. Pure arithmetic, no
external library, so every step can be tested with known numbers.

Sun position after the standard formulas (Spencer's declination and
equation of time), accurate to a fraction of a degree, which is far
below the uncertainty of the forecast itself.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .const import ALBEDO, FAIMAN_U0, FAIMAN_U1, INCIDENCE_AR, SOLAR_CONSTANT, TEMPERATURE_COEFFICIENT, WIND_AT_MODULES, WIND_DEFAULT


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


def beam_fraction(start: datetime, lat: float, lon: float, horizon: Callable[[float], float] | None) -> float:
    """The share of an hour's direct beam that clears the terrain: the sun
    is looked at every ten minutes of the hour and counts, weighted with
    its height (a high sun carries more of the hour's beam), when it stands
    above the horizon in its direction. `horizon` gives the terrain's
    elevation angle for a compass bearing; without one nothing is blocked."""
    if horizon is None:
        return 1.0
    total = clear = 0.0
    for minute in range(5, 60, 10):
        sun = sun_position(start + timedelta(minutes=minute), lat, lon)
        weight = math.sin(math.radians(sun.elevation))
        if weight <= 0:
            continue
        total += weight
        if sun.elevation > horizon(sun.azimuth):
            clear += weight
    return clear / total if total > 0 else 1.0


def sky_view_factor(plane: Plane, horizon: Callable[[float], float] | None) -> float:
    """The share of the diffuse sky light a plane still receives behind
    the terrain: the sky dome the plane faces, weighted with the angle
    the light falls in at, counted once above the terrain horizon and once
    above a flat one. 1.0 without terrain; a plane that looks into a
    valley side loses the low part of its sky."""
    if horizon is None:
        return 1.0
    tilt = math.radians(plane.tilt)
    seen = total = 0.0
    for azimuth in range(5, 360, 10):
        ground = max(0.0, horizon(float(azimuth)))
        swing = math.cos(math.radians(azimuth - plane.azimuth))
        for elevation in range(1, 90, 2):
            el = math.radians(elevation)
            cos_aoi = math.sin(el) * math.cos(tilt) + math.cos(el) * math.sin(tilt) * swing
            if cos_aoi <= 0:
                continue
            weight = cos_aoi * math.cos(el)
            total += weight
            if elevation > ground:
                seen += weight
    return seen / total if total > 0 else 1.0


def incidence_modifier(cos_aoi: float) -> float:
    """The share of the light that passes the module glass at an angle of
    incidence (Martin & Ruiz): 1 for light that falls in at a right angle,
    falling off towards grazing light."""
    if cos_aoi <= 0:
        return 0.0
    return (1 - math.exp(-min(1.0, cos_aoi) / INCIDENCE_AR)) / (1 - math.exp(-1 / INCIDENCE_AR))


def _diffuse_modifiers(tilt: float) -> tuple[float, float]:
    """The same for the sky's diffuse light and for the ground reflection,
    which come from all directions: each has an equivalent angle of
    incidence that depends on the tilt only (Brandemuehl & Beckman)."""
    sky = 59.7 - 0.1388 * tilt + 0.001497 * tilt**2
    ground = 90.0 - 0.5788 * tilt + 0.002693 * tilt**2
    return incidence_modifier(math.cos(math.radians(sky))), incidence_modifier(math.cos(math.radians(ground)))


def plane_irradiance(
    ghi: float, dhi: float | None, sun: SunPosition, plane: Plane, beam_factor: float = 1.0, sky_view: float = 1.0, albedo: float = ALBEDO
) -> float:
    """Irradiance that reaches the cells of the module plane in W/m², from
    the global (and, when forecast, the diffuse) irradiance on the
    horizontal; the reflection at the glass is taken off already.

    `beam_factor` is the share of the direct beam that reaches the place
    (terrain horizon); it also holds back the bright ring around the sun.
    `sky_view` is the share of the remaining sky light the plane receives
    behind the terrain, `albedo` the reflectance of the ground."""
    ghi = max(0.0, ghi)
    if ghi <= 0:
        return 0.0
    if dhi is None or dhi < 0 or dhi > ghi:
        dhi = _diffuse_estimate(ghi, sun)
    dhi = min(ghi, max(0.0, dhi))
    tilt_r = math.radians(plane.tilt)
    beam_h = ghi - dhi
    beam = circumsolar = 0.0
    clearness = 0.0  # Hay & Davies: the share of the diffuse light that comes from around the sun
    cos_aoi = 0.0
    if sun.elevation > 1.0:
        cos_zenith = math.cos(math.radians(sun.zenith))
        cos_aoi = max(0.0, _cos_incidence(sun, plane))
        if cos_zenith > 0.05:
            clearness = min(1.0, max(0.0, beam_h / (SOLAR_CONSTANT * cos_zenith)))
            if cos_aoi > 0:
                factor = min(1.0, max(0.0, beam_factor))
                # a low sun on the horizontal file cannot produce more on the plane than a plausible normal irradiance
                beam = min(beam_h / cos_zenith * cos_aoi, 1100.0 * cos_aoi) * factor
                circumsolar = dhi * clearness * cos_aoi / max(cos_zenith, 0.087) * factor
    sky_modifier, ground_modifier = _diffuse_modifiers(plane.tilt)
    sky = dhi * (1 - clearness) * (1 + math.cos(tilt_r)) / 2 * min(1.0, max(0.0, sky_view))
    reflected = ghi * albedo * (1 - math.cos(tilt_r)) / 2
    return (beam + circumsolar) * incidence_modifier(cos_aoi) + sky * sky_modifier + reflected * ground_modifier


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
    extra = SOLAR_CONSTANT * math.sin(math.radians(sun.elevation))
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


def plane_power_kw(poa: float, air_temperature: float | None, plane: Plane, wind: float | None = None, share: float = 1.0) -> float:
    """AC power of the plane in kW for an irradiance on the plane. `wind`
    is the forecast wind in m/s (10 m above ground), `share` the part of
    the modules that is free of snow."""
    if poa <= 0 or plane.kwp <= 0 or share <= 0:
        return 0.0
    temp = 20.0 if air_temperature is None else air_temperature
    at_modules = WIND_DEFAULT if wind is None else max(0.0, wind) * WIND_AT_MODULES
    cell = temp + poa / (FAIMAN_U0 + FAIMAN_U1 * at_modules)
    dc = plane.kwp * poa / 1000.0 * (1 + TEMPERATURE_COEFFICIENT * (cell - 25.0)) * min(1.0, share)
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
    poa: float  # what reaches the cells, W/m²
    power_kw: float
    beam_fraction: float = 1.0  # share of the direct beam that clears the terrain horizon
    wind: float | None = None  # m/s, 10 m above ground
    snow_cover: float = 0.0  # share of the modules under snow, mean of the hour
    snow_loss_kw: float = 0.0  # what the snow takes off the hour's power

    @property
    def energy_wh(self) -> float:
        return self.power_kw * 1000.0


def forecast_hours(
    rows: list[tuple],
    lat: float,
    lon: float,
    plane: Plane,
    horizon: Callable[[float], float] | None = None,
    albedo: Mapping[datetime, float] | None = None,
) -> list[HourForecast]:
    """The hourly plane output from rows of (timestamp, ghi, dhi,
    temperature) and, optionally, the wind in m/s as a fifth value. A
    MeteoSwiss hourly mean covers the hour that ends at its timestamp; the
    sun is taken at the middle of that hour. With a terrain horizon the
    direct beam counts only for the part of the hour in which the sun
    stands above it, and the sky light only from the sky above the
    terrain. `albedo` gives the ground's reflectance for an hour start
    where it differs from the usual one (snow)."""
    out: list[HourForecast] = []
    sky_view = sky_view_factor(plane, horizon)
    for row in rows:
        ts, ghi, dhi, temp = row[:4]
        wind = row[4] if len(row) > 4 else None
        if ghi is None:
            continue
        start = ts - timedelta(hours=1)
        sun = sun_position(start + timedelta(minutes=30), lat, lon)
        fraction = beam_fraction(start, lat, lon, horizon) if ghi > 0 else 1.0
        ground = albedo.get(start, ALBEDO) if albedo else ALBEDO
        poa = plane_irradiance(ghi, dhi, sun, plane, fraction, sky_view, ground)
        out.append(
            HourForecast(start=start, ghi=ghi, dhi=dhi, temperature=temp, poa=poa, power_kw=plane_power_kw(poa, temp, plane, wind), beam_fraction=fraction, wind=wind)
        )
    return out
