"""Setup with a faked run: the plant's and the planes' sensors, the
cadastre yardstick, the energy dashboard hook, the repair notice when the
data cannot be read, and unloading."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_solar_forecast.const import DOMAIN
from custom_components.swiss_solar_forecast.energy import async_get_solar_forecast
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable

SHARES = {str(m): s for m, s in zip(range(1, 13), (3, 5, 8, 10, 12, 13, 13, 12, 9, 7, 4, 4))}
PLANES = [
    {"label": "South", "kwp": 10.0, "azimuth": 180.0, "tilt": 30.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0, "roof_id": 5001, "roof_area": 85.0, "roof_class": 4, "roof_irradiation": 1200.0, "roof_monthly": SHARES},
    {"label": "East", "kwp": 5.0, "azimuth": 90.0, "tilt": 30.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0},
]
ENTRY_DATA = {
    "point_type": 2, "point_id": 800100, "point_name": "Zürich", "point_lat": 47.3769, "point_lon": 8.5417, "point_height": 408.0,
    "name": "Our roof", "address": "", "planes": PLANES,
}


def _entry(planes=None) -> MockConfigEntry:
    data = {**ENTRY_DATA, "planes": planes or PLANES}
    return MockConfigEntry(domain=DOMAIN, unique_id="47.3769,8.5417", title="Our roof", data=data, entry_id="solar1234")


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_plant_and_plane_sensors(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    today = hass.states.get("sensor.swiss_solar_forecast_1234_energy_today")
    assert today is not None
    # a clear June day on 15 kWp (10 south, 5 east): between 60 and 130 kWh
    assert 60 <= float(today.state) <= 130
    assert today.attributes["kwp"] == 15.0
    assert set(today.attributes["planes"]) == {"South", "East"}
    assert today.attributes["planes"]["South"] > today.attributes["planes"]["East"]
    south = hass.states.get("sensor.swiss_solar_forecast_1234_plane1_energy_today")
    east = hass.states.get("sensor.swiss_solar_forecast_1234_plane2_energy_today")
    assert abs(float(south.state) + float(east.state) - float(today.state)) < 0.01
    assert south.attributes["kwp"] == 10.0 and south.attributes["roof_suitability_class"] == 4
    assert hass.states.get("sensor.swiss_solar_forecast_1234_plane1_power_now") is not None

    typical = hass.states.get("sensor.swiss_solar_forecast_1234_typical_day_energy")
    assert typical is not None
    assert 30 <= float(typical.state) <= 60  # June expectation of the 10 kWp south plane (the east plane has no roof)
    assert typical.attributes["planes"] == {"South": float(typical.state)}
    ratio = hass.states.get("sensor.swiss_solar_forecast_1234_forecast_vs_typical")
    assert 100 <= float(ratio.state) <= 300  # the fake forecast is a bright day, counted against the one roof's expectation
    registry = er.async_get(hass)
    assert registry.async_get("sensor.swiss_solar_forecast_1234_plane2_typical_day_energy") is None  # no roof, no yardstick

    remaining = hass.states.get("sensor.swiss_solar_forecast_1234_energy_remaining_today")
    assert 0 < float(remaining.state) < float(today.state)
    peak_time = hass.states.get("sensor.swiss_solar_forecast_1234_peak_time_today")
    assert peak_time.state.startswith("2026-06-21T")

    forecast = await async_get_solar_forecast(hass, entry.entry_id)
    assert forecast is not None and len(forecast["wh_hours"]) == 72
    assert next(iter(forecast["wh_hours"])).startswith("2026-06-21T00:00")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_single_plane_has_no_plane_sensors(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    entry = _entry([PLANES[1]])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.swiss_solar_forecast_1234_energy_today") is not None
    assert hass.states.get("sensor.swiss_solar_forecast_1234_plane1_energy_today") is None
    assert hass.states.get("sensor.swiss_solar_forecast_1234_typical_day_energy") is None


async def test_unreachable_is_retried(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.coordinator.async_find_latest_run", side_effect=OgdUnavailable("HTTP 503")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"forecast_unreachable_{entry.entry_id}") is None  # one failure is no notice yet


async def test_energy_forecast_unknown_entry(hass: HomeAssistant) -> None:
    assert await async_get_solar_forecast(hass, "nope") is None
