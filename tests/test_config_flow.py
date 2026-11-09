"""Config flow: setup from the marker with a cadastre roof, from an address
with several matches, by hand without a roof, the duplicate rule, the
options and the reconfigure flow. The geo services are faked."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_solar_forecast.const import (
    CONF_ADDRESS,
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_KWP,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE,
    CONF_ROOF_CLASS,
    CONF_ROOF_ID,
    CONF_ROOF_MONTHLY,
    CONF_TILT,
    DOMAIN,
)
from custom_components.swiss_solar_forecast.sonnendach import Address, Roof

ROOF_SE = Roof(roof_id=5001, building_id=77, azimuth=130.0, tilt=23.0, area=85.0, suitability=4, suitability_text={"de": "Gut", "en": "Good"}, yield_kwh_year=14500.0, irradiation_kwh_m2_year=1180.0, monthly_share={m: s for m, s in zip(range(1, 13), (3, 5, 8, 10, 12, 13, 13, 12, 9, 7, 4, 4))})
ROOF_NE = Roof(roof_id=5002, building_id=77, azimuth=50.0, tilt=23.0, area=60.0, suitability=2, suitability_text={"de": "Mittel", "en": "Mean"}, yield_kwh_year=8000.0, irradiation_kwh_m2_year=900.0, monthly_share=ROOF_SE.monthly_share)
ADDRESSES = [Address("Musterweg 1 8001 Zürich", 47.3769, 8.5417), Address("Musterweg 1a 8001 Zürich", 47.3770, 8.5418)]
PLANE = {"kwp": 9.6, "azimuth": 130, "tilt": 23, "losses": 14, "inverter_kw": 8.0, "damping": 1.0}


@pytest.fixture
def fake_geo(fake_points):
    with (
        patch("custom_components.swiss_solar_forecast.config_flow.async_geocode", return_value=ADDRESSES),
        patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[ROOF_SE, ROOF_NE]),
    ):
        yield


async def test_setup_from_marker_with_cadastre_roof(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Roof south-east", "location": {"latitude": 47.37, "longitude": 8.54}, "search": ""}
    )
    assert result["step_id"] == "roof"
    options = result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roof": "5001"})
    assert result["step_id"] == "plane"
    # the cadastre's orientation and tilt are the suggested values
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["azimuth"] == 130 and defaults["tilt"] == 23 and defaults["kwp"] == 17.0
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**PLANE, "azimuth": 135})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roof south-east"
    assert result["data"][CONF_POINT_ID] == 800100  # nearest forecast point, picked automatically
    assert result["data"][CONF_POINT_NAME] == "Zürich"
    assert result["data"][CONF_ROOF_ID] == 5001
    assert result["data"][CONF_ROOF_CLASS] == 4
    assert result["data"][CONF_ROOF_MONTHLY]["6"] == 13.0
    assert result["data"][CONF_AZIMUTH] == 135.0  # the user's correction wins
    assert result["data"][CONF_KWP] == 9.6
    assert result["result"].unique_id == "roof-5001"


async def test_setup_from_address_with_several_matches_and_duplicate(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Solar", "location": {"latitude": 47.0, "longitude": 8.0}, "search": "Musterweg 1 Zürich"}
    )
    assert result["step_id"] == "address"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"search": "Musterweg 1 8001 Zürich"})
    assert result["step_id"] == "roof"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roof": "5002"})
    assert result["step_id"] == "plane"
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**PLANE, "azimuth": 50})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == "Musterweg 1 8001 Zürich"
    assert result["data"]["point_lat"] == 47.3769  # the address, not the marker
    assert result["result"].unique_id == "roof-5002"

    # the same roof again is refused
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Again", "location": {"latitude": 47.37, "longitude": 8.54}, "search": ""}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roof": "5002"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PLANE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_plane_without_cadastre(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    """No roof in the cadastre (or the service down): the plane is entered by hand."""
    with patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Field", "location": {"latitude": 46.95, "longitude": 7.45}, "search": ""}
        )
        assert result["step_id"] == "roof"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roof": "manual"})
        assert result["step_id"] == "plane"
        with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {**PLANE, "azimuth": 180, "tilt": 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINT_NAME] == "Bern"
    assert CONF_ROOF_ID not in result["data"]
    assert result["result"].unique_id == "2-301100-180-30"


async def test_address_without_match_shows_error(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    with patch("custom_components.swiss_solar_forecast.config_flow.async_geocode", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Solar", "location": {"latitude": 47.0, "longitude": 8.0}, "search": "Atlantis"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"search": "no_match"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="roof-5001",
        title="Roof south-east",
        data={CONF_POINT_TYPE: 2, CONF_POINT_ID: 800100, CONF_POINT_NAME: "Zürich", "point_lat": 47.37, "point_lon": 8.54, "point_height": 408.0, CONF_ROOF_ID: 5001, **PLANE},
    )


async def test_options_change_the_plane(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"kwp": 9.6, "azimuth": 130, "tilt": 23, "losses": 12, "inverter_kw": 8.0, "damping": 0.9}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DAMPING] == 0.9
    assert entry.options["losses"] == 12.0


async def test_reconfigure_moves_to_another_roof(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"location": {"latitude": 46.95, "longitude": 7.45}, "search": ""}
        )
        assert result["step_id"] == "reconfigure_roof"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roof": "5002"})
        assert result["step_id"] == "reconfigure_plane"
        defaults = {str(k): k.default() for k in result["data_schema"].schema}
        assert defaults["azimuth"] == 50 and defaults["kwp"] == 9.6  # roof's orientation, entry's peak power
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**PLANE, "azimuth": 50})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_POINT_ID] == 301100
    assert entry.data[CONF_ROOF_ID] == 5002
    assert entry.data[CONF_AZIMUTH] == 50.0
    assert entry.data[CONF_TILT] == 23.0
    assert entry.unique_id == "roof-5002"
