"""Shared fixtures: a forecast run served from memory and a flat terrain
horizon, so no test touches the MeteoSwiss or the swisstopo servers, and
a store kept in memory, so none writes to disk."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.swiss_solar_forecast.horizon import Horizon, directions
from custom_components.swiss_solar_forecast.ogd import ForecastData, Point

ZURICH = Point(point_type=2, point_id=800100, name="Zürich", postal_code="8001", station_abbr="SMA", height=408.0, lat=47.3769, lon=8.5417)
BERN = Point(point_type=2, point_id=301100, name="Bern", postal_code="3011", station_abbr="BER", height=542.0, lat=46.9480, lon=7.4474)
SAENTIS = Point(point_type=3, point_id=90001, name="Säntis", postal_code="", station_abbr="SAE", height=2502.0, lat=47.2494, lon=9.3433)
POINTS = [ZURICH, BERN, SAENTIS]

RUN = datetime(2026, 6, 21, 4, 0, tzinfo=UTC)


def clear_sky_run(run: datetime = RUN, days: int = 3) -> ForecastData:
    """A fair-weather forecast: a bell of global radiation around noon UTC,
    a third of it diffuse, 20 °C air. Timestamps mark the end of the hour."""
    data = ForecastData(run=run, point=(800100, 2))
    ghi, dhi, temp = [], [], []
    start = run.replace(hour=0)
    for i in range(24 * days):
        ts = start + timedelta(hours=i + 1)
        hour_mid = (ts - timedelta(minutes=30)).hour + (ts - timedelta(minutes=30)).minute / 60
        value = max(0.0, 800.0 * (1 - ((hour_mid - 12.0) / 7.0) ** 2))
        ghi.append((ts, round(value, 1)))
        dhi.append((ts, round(value / 3, 1)))
        temp.append((ts, 20.0))
    data.series = {"gre000h0": ghi, "ods000h0": dhi, "tre200h0": temp}
    return data


@pytest.fixture
def fake_points():
    with patch("custom_components.swiss_solar_forecast.config_flow.async_fetch_points", return_value=POINTS):
        yield


@pytest.fixture
def fake_forecast():
    """The coordinator finds run RUN and reads the clear-sky series."""
    with (
        patch("custom_components.swiss_solar_forecast.coordinator.async_find_latest_run", return_value=RUN),
        patch("custom_components.swiss_solar_forecast.coordinator.async_fetch_forecast", return_value=clear_sky_run()),
    ):
        yield


def horizon_reply(elevation_of=lambda azimuth: 0.0):
    """What the terrain read returns: a horizon made by `elevation_of`, five requests, some bytes."""

    async def _reply(session, lat, lon, now=None):
        elevations = tuple(float(elevation_of(azimuth)) for azimuth in directions())
        return Horizon(lat=lat, lon=lon, site_height=408.0, elevations=elevations, fetched="2026-06-21T10:15:00+00:00"), 5, 2000

    return _reply


@pytest.fixture(autouse=True)
def memory_store(hass_storage):
    """What an entry keeps stays in memory."""
    return hass_storage


@pytest.fixture(autouse=True)
def fake_horizon():
    """The terrain is flat unless a test sets another `side_effect`."""
    with patch("custom_components.swiss_solar_forecast.coordinator.async_fetch_horizon", side_effect=horizon_reply()) as mock:
        yield mock
