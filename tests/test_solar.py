"""The model with known numbers: sun position, transposition, the loss at
the glass, temperature derating with wind, clipping, damping, the hourly
list, the terrain horizon in front of the sun and of the sky, and snow on
the ground."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.swiss_solar_forecast.solar import (
    Plane,
    SunPosition,
    beam_fraction,
    forecast_hours,
    incidence_modifier,
    plane_irradiance,
    plane_power_kw,
    sky_view_factor,
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
    # on the flat the plane sees the horizontal irradiance, less what the glass reflects of the flat light
    assert 340.0 <= flat <= 395.0


def test_glass_reflects_grazing_light() -> None:
    assert incidence_modifier(1.0) == 1.0
    assert 0.99 < incidence_modifier(0.87)  # 30° off the normal: nothing to speak of
    assert 0.85 < incidence_modifier(0.34) < 0.92  # 70°
    assert 0.40 < incidence_modifier(0.087) < 0.46  # 85°
    assert incidence_modifier(0.0) == 0.0 and incidence_modifier(-0.5) == 0.0
    # high sun on a plane that faces it: no loss
    sun = SunPosition(elevation=60.0, azimuth=180.0)
    # the whole beam of 1000 W/m² and a little light from the ground
    assert 1000.0 < plane_irradiance(866.0, 0.0, sun, SOUTH_30) < 1015.0


def test_plane_facing_away_from_the_sun_gets_diffuse_only() -> None:
    sun = SunPosition(elevation=30.0, azimuth=180.0)
    north = Plane(kwp=10.0, azimuth=0.0, tilt=60.0)
    poa = plane_irradiance(600.0, 150.0, sun, north)
    # no beam; of the diffuse light the bright ring around the sun is behind the plane too,
    # what is left is the dim rest of the sky and a little ground reflection
    assert 50 <= poa <= 90
    # under an overcast sky all planes get about the same
    overcast = plane_irradiance(150.0, 150.0, sun, north)
    assert 95 <= overcast <= 125 and abs(overcast - plane_irradiance(150.0, 150.0, sun, Plane(kwp=10.0, azimuth=180.0, tilt=60.0))) < 1e-9


def test_snow_on_the_ground_brightens_steep_planes() -> None:
    sun = SunPosition(elevation=20.0, azimuth=180.0)
    wall = Plane(kwp=10.0, azimuth=180.0, tilt=90.0)
    bare = plane_irradiance(400.0, 100.0, sun, wall)
    snowy = plane_irradiance(400.0, 100.0, sun, wall, albedo=0.6)
    assert 60 < snowy - bare < 80  # 40 % more of the global light, half of the ground in view
    flat = Plane(kwp=10.0, azimuth=180.0, tilt=0.0)
    assert plane_irradiance(400.0, 100.0, sun, flat, albedo=0.6) == plane_irradiance(400.0, 100.0, sun, flat)
    # per hour, where the coordinator says so
    ts = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    rows = [(ts, 300.0, 100.0, -3.0), (ts + timedelta(hours=1), 300.0, 100.0, -3.0)]
    plain = forecast_hours(rows, *BERN, wall)
    white = forecast_hours(rows, *BERN, wall, None, {ts - timedelta(hours=1): 0.6})
    assert white[0].poa > plain[0].poa and white[1].poa == plain[1].poa


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


def test_wind_cools_the_modules() -> None:
    calm = plane_power_kw(1000.0, 20.0, SOUTH_30, wind=0.0)
    usual = plane_power_kw(1000.0, 20.0, SOUTH_30)
    breeze = plane_power_kw(1000.0, 20.0, SOUTH_30, wind=5.0)
    assert calm < usual < breeze
    assert 0.02 < (breeze - usual) / usual < 0.06  # a few percent, not more
    assert plane_power_kw(1000.0, 20.0, SOUTH_30, wind=2.0) == usual  # no forecast wind counts as a light breeze
    # the wind comes with the hourly rows
    ts = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    still, windy = forecast_hours([(ts, 800.0, 250.0, 22.0, 0.0)], *BERN, SOUTH_30)[0], forecast_hours([(ts, 800.0, 250.0, 22.0, 8.0)], *BERN, SOUTH_30)[0]
    assert windy.power_kw > still.power_kw and windy.wind == 8.0 and still.poa == windy.poa


def test_snow_on_a_part_of_the_modules() -> None:
    free = plane_power_kw(600.0, -3.0, SOUTH_30)
    assert abs(plane_power_kw(600.0, -3.0, SOUTH_30, share=0.25) - free / 4) < 1e-9
    assert plane_power_kw(600.0, -3.0, SOUTH_30, share=0.0) == 0.0


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


def test_beam_fraction_of_an_hour() -> None:
    noon = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    sunrise = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)  # the sun rose in Bern at about 03:40 UTC and climbs from 3 to 12 degrees in this hour
    assert beam_fraction(noon, *BERN, None) == 1.0
    assert beam_fraction(noon, *BERN, lambda azimuth: 5.0) == 1.0
    assert beam_fraction(noon, *BERN, lambda azimuth: 89.0) == 0.0
    assert beam_fraction(sunrise, *BERN, lambda azimuth: 0.0) == 1.0  # a flat horizon takes nothing
    assert beam_fraction(sunrise, *BERN, lambda azimuth: 20.0) == 0.0
    part = beam_fraction(sunrise, *BERN, lambda azimuth: 8.0)  # the sun climbs past 8 degrees within this hour
    assert 0.0 < part < 1.0
    assert beam_fraction(datetime(2026, 6, 21, 0, 0, tzinfo=UTC), *BERN, lambda azimuth: 10.0) == 1.0  # night: nothing to take


def test_a_mountain_in_front_of_the_sun_leaves_the_diffuse_light() -> None:
    rows = [(datetime(2026, 6, 21, 12, 0, tzinfo=UTC), 800.0, 250.0, 22.0)]
    free = forecast_hours(rows, *BERN, SOUTH_30)[0]
    flat = forecast_hours(rows, *BERN, SOUTH_30, lambda azimuth: 0.0)[0]
    wall = forecast_hours(rows, *BERN, SOUTH_30, lambda azimuth: 89.0)[0]
    assert free.beam_fraction == 1.0 and flat.power_kw == free.power_kw
    assert wall.beam_fraction == 0.0
    assert 0 < wall.power_kw < 0.45 * free.power_kw
    # a wall only in the east leaves the noon sun alone and takes a little of the sky's light
    east = forecast_hours(rows, *BERN, SOUTH_30, lambda azimuth: 60.0 if azimuth < 120 else 0.0)[0]
    assert east.beam_fraction == 1.0
    assert 0.95 * free.power_kw < east.power_kw < free.power_kw


def test_terrain_hides_a_part_of_the_sky() -> None:
    wall = Plane(kwp=10.0, azimuth=180.0, tilt=90.0)
    flat = Plane(kwp=10.0, azimuth=180.0, tilt=0.0)
    assert sky_view_factor(SOUTH_30, None) == 1.0
    assert sky_view_factor(SOUTH_30, lambda azimuth: 0.0) == 1.0
    ring = lambda azimuth: 20.0  # noqa: E731  mountains of 20° all round
    assert 0.5 < sky_view_factor(wall, ring) < sky_view_factor(SOUTH_30, ring) < sky_view_factor(flat, ring) < 0.95
    # a mountain behind the plane takes nothing from it
    assert sky_view_factor(wall, lambda azimuth: 40.0 if azimuth < 90 or azimuth > 270 else 0.0) == 1.0
