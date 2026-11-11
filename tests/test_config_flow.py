"""Config flow: a plant with two cadastre roofs from the marker, an address
with several matches plus a plane by hand, nothing ticked, the duplicate
rule, the options (pick a plane, change it) and the reconfigure flow."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_solar_forecast.const import CONF_ADDRESS, CONF_PLANES, CONF_POINT_ID, CONF_POINT_NAME, DOMAIN
from custom_components.swiss_solar_forecast.sonnendach import Address, Roof

from .conftest import CHUR_STATION, SAENTIS

SHARES = {m: s for m, s in zip(range(1, 13), (3, 5, 8, 10, 12, 13, 13, 12, 9, 7, 4, 4))}
ROOF_SE = Roof(roof_id=5001, building_id=77, azimuth=130.0, tilt=23.0, area=85.0, suitability=4, suitability_text={"de": "Gut", "en": "Good"}, yield_kwh_year=14500.0, irradiation_kwh_m2_year=1180.0, monthly_share=SHARES)
ROOF_NE = Roof(roof_id=5002, building_id=77, azimuth=50.0, tilt=23.0, area=60.0, suitability=2, suitability_text={"de": "Mittel", "en": "Mean"}, yield_kwh_year=8000.0, irradiation_kwh_m2_year=900.0, monthly_share=SHARES)
ADDRESSES = [Address("Musterweg 1 8001 Zürich", 47.3769, 8.5417), Address("Musterweg 1a 8001 Zürich", 47.3770, 8.5418)]
FIGURES = {"losses": 14, "damping": 1.0}


@pytest.fixture
def fake_geo(fake_points):
    with (
        patch("custom_components.swiss_solar_forecast.config_flow.async_geocode", return_value=ADDRESSES),
        patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[ROOF_SE, ROOF_NE]),
    ):
        yield


def _defaults(result) -> dict:
    return {str(k): k.default() for k in result["data_schema"].schema}


async def test_plant_with_two_cadastre_roofs(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Our roof", "location": {"latitude": 47.37, "longitude": 8.54}, "search": ""}
    )
    assert result["step_id"] == "roofs"
    assert _defaults(result)["roofs"] == ["5001"]  # the largest is pre-ticked
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["5001", "5002"]})
    assert result["step_id"] == "plane"
    assert result["description_placeholders"]["index"] == "1" and result["description_placeholders"]["total"] == "2"
    d = _defaults(result)
    assert d["azimuth"] == 130 and d["tilt"] == 23 and d["kwp"] == 17.0 and d["label"] == "SE 130° / 23°"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "South-east", "kwp": 8.0, "azimuth": 140, "tilt": 23, **FIGURES})
    assert result["step_id"] == "plane"
    assert result["description_placeholders"]["index"] == "2"
    assert _defaults(result)["azimuth"] == 50
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 4.45, "azimuth": 50, "tilt": 23, **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Our roof"
    assert result["data"][CONF_POINT_ID] == 800100 and result["data"][CONF_POINT_NAME] == "Zürich"
    planes = result["data"][CONF_PLANES]
    assert len(planes) == 2
    assert planes[0]["label"] == "South-east" and planes[0]["azimuth"] == 140.0 and planes[0]["roof_id"] == 5001 and planes[0]["roof_class"] == 4
    assert planes[1]["label"] == "NE 50° / 23°" and planes[1]["kwp"] == 4.45 and planes[1]["roof_monthly"]["6"] == 13.0
    assert result["result"].unique_id == "47.3700,8.5400:our-roof"


async def test_address_with_matches_and_a_manual_plane(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Solar", "location": {"latitude": 47.0, "longitude": 8.0}, "search": "Musterweg 1 Zürich"}
    )
    assert result["step_id"] == "address"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"search": "Musterweg 1 8001 Zürich"})
    assert result["step_id"] == "roofs"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["5002", "manual"]})
    assert result["step_id"] == "plane"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 4.45, "azimuth": 50, "tilt": 23, **FIGURES})
    assert result["step_id"] == "plane"
    assert result["description_placeholders"]["roof"] == "—"
    assert _defaults(result)["label"] == "Plane 2"
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "Garage", "kwp": 3.0, "azimuth": 180, "tilt": 15, **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == "Musterweg 1 8001 Zürich"
    assert result["data"]["point_lat"] == 47.3769  # the address, not the marker
    planes = result["data"][CONF_PLANES]
    assert planes[1]["label"] == "Garage" and "roof_id" not in planes[1]

    # the same place again is refused
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Solar", "location": {"latitude": 47.0, "longitude": 8.0}, "search": "Musterweg 1 Zürich"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"search": "Musterweg 1 8001 Zürich"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["5001"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 8.0, "azimuth": 130, "tilt": 23, **FIGURES})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_nothing_ticked_and_no_cadastre(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    with patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Field", "location": {"latitude": 46.95, "longitude": 7.45}, "search": ""}
        )
        assert result["step_id"] == "roofs"
        assert _defaults(result)["roofs"] == ["manual"]  # nothing in the cadastre: by hand is pre-ticked
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": []})
        assert result["errors"] == {"roofs": "pick_one"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["manual"]})
        assert result["step_id"] == "plane"
        with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 10, "azimuth": 180, "tilt": 30, **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINT_NAME] == "Bern"
    assert result["data"][CONF_PLANES][0]["label"] == "S 180° / 30°"
    assert result["options"] == {}  # no yield meter named


async def test_the_yield_meter_is_asked_for_at_once(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    with patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert "yield_sensor" in {str(k) for k in result["data_schema"].schema}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Field", "location": {"latitude": 46.95, "longitude": 7.45}, "search": "", "yield_sensor": "sensor.pv_total"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["manual"]})
        with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 10, "azimuth": 180, "tilt": 30, **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # the meter is an option: it can be changed or taken out later without setting the plant up again
    assert result["options"] == {"yield_sensor": "sensor.pv_total"} and "yield_sensor" not in result["data"]


@pytest.mark.parametrize(
    ("place", "name"),
    [
        ({"latitude": 46.8700, "longitude": 9.5300}, "Chur"),  # next to a station: the postal-code centre two kilometres away
        ({"latitude": 47.2490, "longitude": 9.3430}, "Chur"),  # on a summit with a mountain point: the nearest postal code, far as it is
    ],
)
async def test_the_point_is_one_with_radiation(hass: HomeAssistant, enable_custom_integrations, fake_points, place, name) -> None:
    """Stations and mountain points carry no radiation: never the forecast point, however close."""
    with patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Field", "location": place, "search": ""})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["manual"]})
        with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 10, "azimuth": 180, "tilt": 30, **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["point_type"] == 2 and result["data"][CONF_POINT_NAME] == name


async def test_no_postal_code_centre_in_the_list(hass: HomeAssistant, enable_custom_integrations) -> None:
    with (
        patch("custom_components.swiss_solar_forecast.config_flow.async_fetch_points", return_value=[CHUR_STATION, SAENTIS]),
        patch("custom_components.swiss_solar_forecast.config_flow.async_roofs_at", return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Field", "location": {"latitude": 46.87, "longitude": 9.53}, "search": ""}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["manual"]})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "", "kwp": 10, "azimuth": 180, "tilt": 30, **FIGURES})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_address_without_match_shows_error(hass: HomeAssistant, enable_custom_integrations, fake_points) -> None:
    with patch("custom_components.swiss_solar_forecast.config_flow.async_geocode", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Solar", "location": {"latitude": 47.0, "longitude": 8.0}, "search": "Atlantis"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"search": "no_match"}


PLANES = [
    {"label": "South-east", "kwp": 8.0, "azimuth": 140.0, "tilt": 23.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0, "roof_id": 5001, "roof_area": 85.0, "roof_class": 4, "roof_irradiation": 1180.0, "roof_monthly": {str(m): s for m, s in SHARES.items()}},
    {"label": "North-east", "kwp": 4.45, "azimuth": 50.0, "tilt": 23.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0, "roof_id": 5002, "roof_area": 60.0, "roof_class": 2, "roof_irradiation": 900.0, "roof_monthly": {str(m): s for m, s in SHARES.items()}},
]


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="47.3700,8.5400:our-roof",
        title="Our roof",
        data={"point_type": 2, "point_id": 800100, "point_name": "Zürich", "point_lat": 47.37, "point_lon": 8.54, "point_height": 408.0, "name": "Our roof", "address": "", "planes": PLANES},
    )


async def test_options_pick_a_plane_and_change_it(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plane": "plant"})
        assert result["step_id"] == "plant"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plant_inverter_kw": 12.0})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["plant_inverter_kw"] == 12.0
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plane": "1"})
        assert result["step_id"] == "plane"
        assert result["description_placeholders"] == {"plane": "North-east"}
        assert _defaults(result)["kwp"] == 4.45
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"label": "North-east", "kwp": 4.45, "azimuth": 50, "tilt": 23, "losses": 12, "damping": 0.9}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    planes = entry.options[CONF_PLANES]
    assert planes[1]["damping"] == 0.9 and planes[1]["losses"] == 12.0 and planes[1]["roof_id"] == 5002
    assert planes[0] == PLANES[0]


async def test_reconfigure_keeps_known_planes_and_adds_one(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"location": {"latitude": 47.37, "longitude": 8.54}, "search": ""}
        )
        assert result["step_id"] == "reconfigure_roofs"
        assert _defaults(result)["roofs"] == ["5001", "5002"]  # the plant's planes are pre-ticked
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["5001", "manual"]})
        assert result["step_id"] == "reconfigure_plane"
        d = _defaults(result)
        assert d["kwp"] == 8.0 and d["azimuth"] == 140.0 and d["label"] == "South-east"  # the current figures of this roof
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "South-east", "kwp": 8.0, "azimuth": 140, "tilt": 23, **FIGURES})
        assert result["step_id"] == "reconfigure_plane"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "Carport", "kwp": 2.0, "azimuth": 200, "tilt": 10, **FIGURES})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    planes = entry.data[CONF_PLANES]
    assert [p["label"] for p in planes] == ["South-east", "Carport"]
    assert planes[0]["roof_id"] == 5001 and "roof_id" not in planes[1]
    assert CONF_PLANES not in entry.options


async def _reconfigure_at(hass: HomeAssistant, entry: MockConfigEntry, location: dict, search: str = "") -> None:
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"location": location, "search": search})
        if result["step_id"] == "reconfigure_address":
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"search": ADDRESSES[1].label})
        assert result["step_id"] == "reconfigure_roofs"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"roofs": ["5001"]})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"label": "South-east", "kwp": 8.0, "azimuth": 140, "tilt": 23, **FIGURES})
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_keeps_the_address_while_the_marker_stays(hass: HomeAssistant, enable_custom_integrations, fake_geo) -> None:
    """Changing the planes alone leaves the address the place was found
    with; a marker moved elsewhere drops it, a new search replaces it."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ADDRESS: "Musterweg 1 8001 Zürich"})
    await _reconfigure_at(hass, entry, {"latitude": 47.37, "longitude": 8.54})
    assert entry.data[CONF_ADDRESS] == "Musterweg 1 8001 Zürich"
    assert entry.unique_id == "47.3700,8.5400:our-roof"
    await _reconfigure_at(hass, entry, {"latitude": 47.37, "longitude": 8.54}, search="Musterweg 1 Zürich")
    assert entry.data[CONF_ADDRESS] == "Musterweg 1a 8001 Zürich"
    assert entry.unique_id == "47.3770,8.5418:our-roof"
    await _reconfigure_at(hass, entry, {"latitude": 47.39, "longitude": 8.52})
    assert entry.data[CONF_ADDRESS] == ""
    assert entry.unique_id == "47.3900,8.5200:our-roof"


async def test_options_terrain_horizon_and_yield_meter(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.swiss_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plane": "plant"})
        assert result["step_id"] == "plant"
        assert {"plant_inverter_kw", "terrain_horizon", "snow", "station_correction", "yield_sensor"} == {str(k) for k in result["data_schema"].schema}
        switches = {"terrain_horizon": False, "snow": False, "station_correction": False}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"plant_inverter_kw": 12.0, **switches, "yield_sensor": "sensor.pv_total"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {"plant_inverter_kw": 12.0, **switches, "yield_sensor": "sensor.pv_total"}
        # the meter is taken out again, the horizon, the snow and the station are switched back on
        switches = {"terrain_horizon": True, "snow": True, "station_correction": True}
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plane": "plant"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"plant_inverter_kw": 12.0, **switches})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {"plant_inverter_kw": 12.0, **switches}
