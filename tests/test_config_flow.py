"""Config flow: setup by location and by search, the duplicate rule (same
point, orientation and tilt), the options and the reconfigure flow."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_solar_forecast.const import (
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_KWP,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE,
    CONF_TILT,
    DOMAIN,
)

PLANE = {"name": "Roof south", "kwp": 9.6, "azimuth": 180, "tilt": 30, "losses": 14, "inverter_kw": 8.0, "damping": 1.0}


async def test_setup_by_location(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 47.37, "longitude": 8.54}, "search": "", **PLANE}
    )
    assert result["step_id"] == "place"
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"point": "2:800100"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roof south"
    assert result["data"][CONF_POINT_ID] == 800100
    assert result["data"][CONF_POINT_TYPE] == 2
    assert result["data"][CONF_POINT_NAME] == "Zürich"
    assert result["data"][CONF_KWP] == 9.6
    assert result["data"][CONF_AZIMUTH] == 180.0
    assert result["result"].unique_id == "2-800100-180-30"


async def test_setup_by_search_and_duplicate(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 47.0, "longitude": 8.0}, "search": "3011 Bern", **PLANE}
    )
    assert result["step_id"] == "search"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"point": "2:301100"})
    assert result["step_id"] == "place"
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"point": "2:301100"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINT_NAME] == "Bern"

    # the same plane again is refused; a different tilt on the same roof is another entry
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 46.95, "longitude": 7.45}, "search": "", **PLANE}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"point": "2:301100"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_search_without_match_shows_error(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 47.0, "longitude": 8.0}, "search": "Atlantis", **PLANE}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"search": "no_match"}


async def test_options_change_the_plane(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-800100-180-30",
        title="Roof south",
        data={CONF_POINT_TYPE: 2, CONF_POINT_ID: 800100, CONF_POINT_NAME: "Zürich", "point_lat": 47.37, "point_lon": 8.54, "point_height": 408.0, **PLANE},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"kwp": 9.6, "azimuth": 180, "tilt": 30, "losses": 12, "inverter_kw": 8.0, "damping": 0.9}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DAMPING] == 0.9
    assert entry.options["losses"] == 12.0


async def test_reconfigure_moves_the_plant(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-800100-180-30",
        title="Roof south",
        data={CONF_POINT_TYPE: 2, CONF_POINT_ID: 800100, CONF_POINT_NAME: "Zürich", "point_lat": 47.37, "point_lon": 8.54, "point_height": 408.0, **PLANE},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"location": {"latitude": 46.95, "longitude": 7.45}, "search": ""}
        )
        assert result["step_id"] == "reconfigure_place"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"point": "2:301100"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_POINT_ID] == 301100
    assert entry.data[CONF_POINT_NAME] == "Bern"
    assert entry.data[CONF_TILT] == 30
    assert entry.unique_id == "2-301100-180-30"
