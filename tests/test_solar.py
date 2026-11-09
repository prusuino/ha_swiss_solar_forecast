"""The model with known numbers: sun position, transposition, temperature
derating, clipping, damping and the hourly list."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.solar import (
    Plane,
    SunPosition,
    forecast_hours,
    plane_irradiance,
    plane_power_kw,
    sun_position,
)

BERN = (46.948, 7.447)
SOUTH_30 = Plane(kwp=10.0, azimuth=180.0, tilt=30.0, losses=14.0)


def test_sun_at_bern_summer_noon_is_high_and_south() -> None:
    # solar noon in Bern on 21 June is about 11:30 UTC
    sun = sun_position(datetime(2026, 6, 21, 11, 30, tzinfo=UTC), *BERN)
    assert 65.5 <= sun.elevation <= 67.0
    assert 175 <= sun.azimuth <= 185


def test_sun_morning_east_afternoon_west_night_below_horizon() -> None:
    morning = sun_position(datetime(2026, 6, 21, 6, 0, tzinfo=UTC), *BERN)
    afternoon = sun_position(datetime(2026, 6, 21, 16, 0, tzinfo=UTC), *BERN)
    night = sun_position(datetime(2026, 6, 21, 0, 0, tzinfo=UTC), *BERN)
    assert 60 <= morning.azimuth <= 110
    assert 250 <= afternoon.azimuth <= 300
    assert night.elevation < 0


def test_sun_winter_noon_is_low() -> None:
    sun = sun_position(datetime(2026, 12, 21, 11, 30, tzinfo=UTC), *BERN)
    assert 18 <= sun.elevation <= 21


def test_south_tilted_plane_beats_horizontal_at_low_sun() -> None:
    # winter noon: a 30° south plane catches more than the horizontal
    sun = SunPosition(elevation=20.0, azimuth=180.0)
    poa = plane_irradiance(400.0, 100.0, sun, SOUTH_30)
    assert poa > 400.0
    flat = plane_irradiance(400.0, 100.0, sun, Plane(kwp=10.0, azimuth=180.0, tilt=0.0))
    assert abs(flat - 400.0) < 1.0  # on the flat the plane sees exactly the horizontal irradiance


def test_plane_facing_away_from_the_sun_gets_diffuse_only() -> None:
    sun = SunPosition(elevation=30.0, azimuth=180.0)
    north = Plane(kwp=10.0, azimuth=0.0, tilt=60.0)
    poa = plane_irradiance(600.0, 150.0, sun, north)
    # diffuse (isotropic, 60° tilt sees 75 % of the sky) plus a little ground reflection, no beam
    assert 110 <= poa <= 160


def test_night_and_missing_diffuse_are_handled() -> None:
    assert plane_irradiance(0.0, 0.0, SunPosition(elevation=-5.0, azimuth=0.0), SOUTH_30) == 0.0
    sun = SunPosition(elevation=40.0, azimuth=180.0)
    with_estimate = plane_irradiance(500.0, None, sun, SOUTH_30)
    assert 450 <= with_estimate <= 650


def test_power_derates_with_temperature_and_losses() -> None:
    cold = plane_power_kw(1000.0, 0.0, SOUTH_30)
    warm = plane_power_kw(1000.0, 30.0, SOUTH_30)
    assert warm < cold
    # 10 kWp at 1000 W/m², 14 % losses, cell at 25 °C would be 8.6 kW; a cold day gives more
    assert 8.0 <= cold <= 9.5
    assert plane_power_kw(0.0, 20.0, SOUTH_30) == 0.0


def test_inverter_limit_and_damping() -> None:
    clipped = plane_power_kw(1000.0, 10.0, Plane(kwp=10.0, azimuth=180.0, tilt=30.0, inverter_kw=5.0))
    assert clipped == 5.0
    damped = plane_power_kw(1000.0, 10.0, Plane(kwp=10.0, azimuth=180.0, tilt=30.0, damping=0.5))
    assert abs(damped - plane_power_kw(1000.0, 10.0, Plane(kwp=10.0, azimuth=180.0, tilt=30.0)) / 2) < 1e-9


def test_forecast_hours_cover_the_hour_before_the_timestamp() -> None:
    ts = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    hours = forecast_hours([(ts, 800.0, 250.0, 22.0), (ts + timedelta(hours=1), None, None, None)], *BERN, SOUTH_30)
    assert len(hours) == 1
    assert hours[0].start == ts - timedelta(hours=1)
    assert hours[0].power_kw > 5.0
    assert hours[0].energy_wh == hours[0].power_kw * 1000
