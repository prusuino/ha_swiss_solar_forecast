"""Setup with a faked run: the plant's and the planes' sensors, the
cadastre yardstick, the energy dashboard hook, the repair notice when the
data cannot be read, unloading, the terrain horizon and the
self-calibration against a yield meter."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.swiss_solar_forecast.const import DOMAIN
from custom_components.swiss_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.swiss_solar_forecast.energy import async_get_solar_forecast
from custom_components.swiss_solar_forecast.horizon import HorizonError
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable

from .conftest import horizon_reply

SHARES = {str(m): s for m, s in zip(range(1, 13), (3, 5, 8, 10, 12, 13, 13, 12, 9, 7, 4, 4))}
PLANES = [
    {"label": "South", "kwp": 10.0, "azimuth": 180.0, "tilt": 30.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0, "roof_id": 5001, "roof_area": 85.0, "roof_class": 4, "roof_irradiation": 1200.0, "roof_monthly": SHARES},
    {"label": "East", "kwp": 5.0, "azimuth": 90.0, "tilt": 30.0, "losses": 14.0, "inverter_kw": 0.0, "damping": 1.0},
]
ENTRY_DATA = {
    "point_type": 2, "point_id": 800100, "point_name": "Zürich", "point_lat": 47.3769, "point_lon": 8.5417, "point_height": 408.0,
    "name": "Our roof", "address": "", "planes": PLANES,
}


STORE_KEY = "swiss_solar_forecast.solar1234"
NOW = datetime(2026, 6, 21, 10, 15, tzinfo=UTC)
PREFIX = "sensor.swiss_solar_forecast_1234_"


def _entry(planes=None, options=None) -> MockConfigEntry:
    data = {**ENTRY_DATA, "planes": planes or PLANES}
    return MockConfigEntry(domain=DOMAIN, unique_id="47.3769,8.5417", title="Our roof", data=data, options=options or {}, entry_id="solar1234")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


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


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_plant_inverter_limit_clips_the_sum(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="47.3769,8.5417", title="Our roof", data={**ENTRY_DATA, "plant_inverter_kw": 5.0}, entry_id="solar1234")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    peak = hass.states.get("sensor.swiss_solar_forecast_1234_peak_power_today")
    assert float(peak.state) == 5000
    south = hass.states.get("sensor.swiss_solar_forecast_1234_plane1_energy_today")
    today = hass.states.get("sensor.swiss_solar_forecast_1234_energy_today")
    assert float(today.state) < float(south.state) + 20  # the clipped plant sum is well below the free sum
    hourly = today.attributes["hourly"]
    assert max(hourly.values()) == 5000


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


@freeze_time(NOW)
async def test_terrain_horizon_blocks_the_morning_sun(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_horizon, memory_store) -> None:
    await _setup(hass, _entry())
    flat = float(hass.states.get(PREFIX + "energy_today").state)
    assert hass.states.get(PREFIX + "terrain_horizon").state == "0.0"
    await hass.config_entries.async_remove("solar1234")
    await hass.async_block_till_done()
    assert STORE_KEY not in memory_store  # a removed plant leaves nothing behind

    fake_horizon.reset_mock()
    fake_horizon.side_effect = horizon_reply(lambda azimuth: 30.0 if azimuth <= 120 else 0.0)  # a mountain in the east
    entry = _entry()
    await _setup(hass, entry)
    assert fake_horizon.await_count == 1
    sensor = hass.states.get(PREFIX + "terrain_horizon")
    assert sensor.state == "30.0"
    assert sensor.attributes["highest_azimuth"] == 45.0 and sensor.attributes["site_height"] == 408.0
    assert len(sensor.attributes["profile"]) == 37 and sensor.attributes["profile"]["180"] == 0.0
    assert "swisstopo" in sensor.attributes["attribution"]
    today = hass.states.get(PREFIX + "energy_today")
    assert today.attributes["terrain_horizon"] is True and today.attributes["calibrated"] is False
    assert flat - 15 < float(today.state) < flat - 1  # the early hours lose their direct light, the day keeps most of its yield
    plant = entry.runtime_data.data.plant
    early = plant.hour_at(datetime(2026, 6, 21, 5, 30, tzinfo=UTC))  # the sun stands 12 to 21 degrees high in the east
    noon = plant.hour_at(datetime(2026, 6, 21, 11, 30, tzinfo=UTC))
    assert early.beam_fraction == 0.0 and early.power_kw > 0  # diffuse light only
    assert noon.beam_fraction == 1.0
    assert entry.runtime_data.data.requests_today >= 5  # the terrain read is counted with the traffic

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["terrain_horizon"] == {"enabled": True, "read": True, "fetched": "2026-06-21T10:15:00+00:00", "directions": 37, "highest": 30.0, "mean": round(30.0 * 11 / 37, 2)}
    assert diagnostics["calibration"] is None
    assert "profile" not in str(diagnostics)

    # kept over a restart: the terrain is not read again
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["horizon"]["elevations"][0] == 30.0
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_horizon.await_count == 1
    assert hass.states.get(PREFIX + "terrain_horizon").state == "30.0"

    # switched off in the options: no horizon in the forecast, and the sensor goes
    hass.config_entries.async_update_entry(entry, options={"terrain_horizon": False})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "terrain_horizon") is None
    assert er.async_get(hass).async_get(PREFIX + "terrain_horizon") is None
    today = hass.states.get(PREFIX + "energy_today")
    assert today.attributes["terrain_horizon"] is False and abs(float(today.state) - flat) < 0.01
    assert fake_horizon.await_count == 1


@freeze_time(NOW)
async def test_terrain_horizon_switched_off_is_never_read(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_horizon) -> None:
    await _setup(hass, _entry(options={"terrain_horizon": False}))
    assert fake_horizon.await_count == 0
    assert hass.states.get(PREFIX + "terrain_horizon") is None
    assert hass.states.get(PREFIX + "energy_today").attributes["terrain_horizon"] is False


@freeze_time(NOW)
async def test_forecast_works_without_the_terrain(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_horizon) -> None:
    fake_horizon.side_effect = HorizonError("HTTP 503")
    entry = _entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(PREFIX + "terrain_horizon").state == "unknown"
    today = hass.states.get(PREFIX + "energy_today")
    assert 60 <= float(today.state) <= 130 and today.attributes["terrain_horizon"] is False
    # the next check of the run does not ask again right away
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_horizon.await_count == 1
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["terrain_horizon"] == {"enabled": True, "read": False}


async def _full_hour(hass: HomeAssistant, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_calibration_learns_from_the_yield_meter(hass: HomeAssistant, enable_custom_integrations, fake_forecast, freezer, memory_store) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total"})
    await _setup(hass, entry)
    factor = hass.states.get(PREFIX + "calibration_factor")
    assert factor.state == "1.0" and factor.attributes["samples"] == 0 and factor.attributes["hourly"] == {}
    assert factor.attributes["yield_sensor"] == "sensor.pv_total"
    before = float(hass.states.get(PREFIX + "energy_today").state)
    assert factor.attributes["energy_today_uncalibrated"] == before

    # 11:00: the first reading; 12:00: the plant made half of what was forecast for the hour
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 11, 0, 5, tzinfo=UTC))
    assert hass.states.get(PREFIX + "calibration_factor").attributes["samples"] == 0
    forecast_wh = entry.runtime_data.data.raw_plant.hour_at(datetime(2026, 6, 21, 11, 30, tzinfo=UTC)).energy_wh
    assert forecast_wh > 5000
    hass.states.async_set("sensor.pv_total", str(1000.0 + forecast_wh / 2000.0), {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 12, 0, 5, tzinfo=UTC))

    factor = hass.states.get(PREFIX + "calibration_factor")
    assert factor.attributes["samples"] == 1 and factor.attributes["last_sample"] == "2026-06-21T11:00:00+00:00"
    assert 0.9 < float(factor.state) < 1.0  # one hour is weak evidence
    # learned for the hour from 11 UTC, shown in local time
    assert list(factor.attributes["hourly"]) == ["13:00"] and 0.75 < factor.attributes["hourly"]["13:00"] < 0.9
    today = hass.states.get(PREFIX + "energy_today")
    assert today.attributes["calibrated"] is True
    assert before * 0.9 < float(today.state) < before
    assert factor.attributes["energy_today_uncalibrated"] == before  # the model itself is unchanged
    planes = today.attributes["planes"]
    assert abs(planes["South"] + planes["East"] - float(today.state)) < 0.02

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["options"]["yield_sensor"] == "**REDACTED**"
    assert diagnostics["calibration"]["samples"] == 1 and list(diagnostics["calibration"]["factors_utc"]) == [11]
    assert "sensor.pv_total" not in str(diagnostics)

    # an unavailable meter teaches nothing and breaks nothing
    hass.states.async_set("sensor.pv_total", "unavailable", {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 13, 0, 5, tzinfo=UTC))
    assert hass.states.get(PREFIX + "calibration_factor").attributes["samples"] == 1

    # what was learned survives a restart
    learned = hass.states.get(PREFIX + "calibration_factor").state
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["calibration"]["samples"] == 1
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "calibration_factor").state == learned

    # another plane: the learned factors no longer fit and start again
    hass.config_entries.async_update_entry(entry, options={"yield_sensor": "sensor.pv_total", "planes": [{**PLANES[0], "kwp": 12.0}, PLANES[1]]})
    await hass.async_block_till_done(wait_background_tasks=True)
    factor = hass.states.get(PREFIX + "calibration_factor")
    assert factor.state == "1.0" and factor.attributes["samples"] == 0

    # the meter is taken out: the sensor goes, and so does what was kept
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "calibration_factor") is None
    assert er.async_get(hass).async_get(PREFIX + "calibration_factor") is None
    assert hass.states.get(PREFIX + "energy_today").attributes["calibrated"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["calibration"] is None


@freeze_time(NOW)
async def test_no_yield_meter_no_calibration(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert hass.states.get(PREFIX + "calibration_factor") is None
    assert entry.runtime_data.calibration is None
    assert hass.states.get(PREFIX + "energy_today").attributes["calibrated"] is False


async def test_calibration_stays_under_the_inverter_limit(hass: HomeAssistant, enable_custom_integrations, fake_forecast, freezer) -> None:
    freezer.move_to(NOW)
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total", "plant_inverter_kw": 3.0})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    noon = datetime(2026, 6, 21, 11, 30, tzinfo=UTC)
    assert coordinator.data.plant.hour_at(noon).power_kw == 3.0  # clipped already

    # the plant made more than forecast in that hour, often enough to be believed
    coordinator.calibration.bins[11] = [45000.0, 30000.0, 30.0]
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.calibration.factor(11) > 1.2
    assert coordinator.data.plant.hour_at(noon).power_kw == 3.0
    assert coordinator.data.raw_plant.hour_at(noon).power_kw == 3.0
