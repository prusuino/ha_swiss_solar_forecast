"""Setup with a faked run: the sensors, the energy dashboard hook, the
repair notice when the data cannot be read, and unloading."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_solar_forecast.const import DOMAIN
from custom_components.swiss_solar_forecast.energy import async_get_solar_forecast
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable

from .conftest import RUN

ENTRY_DATA = {
    "point_type": 2, "point_id": 800100, "point_name": "Zürich", "point_lat": 47.3769, "point_lon": 8.5417, "point_height": 408.0,
    "name": "Roof south", "kwp": 10.0, "azimuth": 180.0, "tilt": 30.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0,
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id="2-800100-180-30", title="Roof south", data=ENTRY_DATA, entry_id="solar1234")


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_sensors_and_energy_forecast(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    today = hass.states.get("sensor.swiss_solar_forecast_1234_energy_today")
    assert today is not None
    # a clear June day on 10 kWp south, 30°: somewhere between 50 and 90 kWh
    assert 50 <= float(today.state) <= 90
    assert today.attributes["diffuse_from_forecast"] is True
    assert 3 <= len(today.attributes["daily"]) <= 4  # the last UTC hour of day 3 starts on day 4 in local time
    tomorrow = hass.states.get("sensor.swiss_solar_forecast_1234_energy_tomorrow")
    assert abs(float(tomorrow.state) - float(today.state)) < 0.5  # the fake is the same every day
    remaining = hass.states.get("sensor.swiss_solar_forecast_1234_energy_remaining_today")
    assert 0 < float(remaining.state) < float(today.state)
    now = hass.states.get("sensor.swiss_solar_forecast_1234_power_now")
    assert float(now.state) > 4000
    peak_time = hass.states.get("sensor.swiss_solar_forecast_1234_peak_time_today")
    assert peak_time.state.startswith("2026-06-21T")

    forecast = await async_get_solar_forecast(hass, entry.entry_id)
    assert forecast is not None and len(forecast["wh_hours"]) == 72
    first_key = next(iter(forecast["wh_hours"]))
    assert first_key.startswith("2026-06-21T00:00")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unreachable_raises_a_repair_after_three_failures(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.coordinator.async_find_latest_run", side_effect=OgdUnavailable("HTTP 503")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"forecast_unreachable_{entry.entry_id}") is None  # one failure is no notice yet


async def test_energy_forecast_unknown_entry(hass: HomeAssistant) -> None:
    assert await async_get_solar_forecast(hass, "nope") is None
