"""Setup with a faked run: the plant's and the planes' sensors, the
cadastre yardstick, the energy dashboard hook, the repair notice when the
data cannot be read, unloading, the terrain horizon, the
self-calibration against a yield meter, the snow on the modules and the
forecast's accuracy."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.swiss_solar_forecast.const import DOMAIN, HOURLY_PARAMS
from custom_components.swiss_solar_forecast.coordinator import stretched
from custom_components.swiss_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.swiss_solar_forecast.energy import async_get_solar_forecast
from custom_components.swiss_solar_forecast.horizon import HorizonError
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable

from .conftest import POINTS, RUN, WINTER_RUN, horizon_reply, snow_run

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


def _registered(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    registry = er.async_get(hass)
    return {known.entity_id.removeprefix(PREFIX) for known in er.async_entries_for_config_entry(registry, entry.entry_id)}


async def _change_planes(hass: HomeAssistant, entry: MockConfigEntry, planes: list[dict]) -> None:
    hass.config_entries.async_update_entry(entry, data={**entry.data, "planes": planes})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_sensors_of_a_removed_plane_leave_the_registry(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    """Three planes become two: the third plane's sensors go, the enabled
    and the disabled ones, the others stay with their history."""
    west = {**PLANES[1], "label": "West", "azimuth": 270.0}
    entry = _entry([*PLANES, west])
    await _setup(hass, entry)
    before = _registered(hass, entry)
    assert {"plane3_energy_today", "plane3_power_now", "plane3_energy_tomorrow", "plane3_peak_power_today"} <= before

    await _change_planes(hass, entry, PLANES)
    after = _registered(hass, entry)
    assert after == {name for name in before if not name.startswith("plane3_")}
    assert hass.states.get(PREFIX + "plane3_energy_today") is None
    assert hass.states.get(PREFIX + "plane2_energy_today") is not None


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_a_plant_left_with_one_plane_loses_the_plane_sensors(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    """Two planes become one, and that one has no roof of the cadastre:
    the per-plane sensors go and so does the cadastre's yardstick."""
    entry = _entry()
    await _setup(hass, entry)
    before = _registered(hass, entry)
    assert {"plane1_energy_today", "plane2_energy_today", "plane1_typical_day_energy", "typical_day_energy", "forecast_vs_typical"} <= before

    await _change_planes(hass, entry, [PLANES[1]])
    after = _registered(hass, entry)
    assert after == {name for name in before if not name.startswith("plane")} - {"typical_day_energy", "forecast_vs_typical"}
    assert hass.states.get(PREFIX + "energy_today") is not None


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_a_roof_entered_by_hand_loses_the_yardstick_of_its_plane(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    """The first plane is no longer a roof of the cadastre: its typical
    day goes, for the plane and for the plant, everything else stays."""
    entry = _entry()
    await _setup(hass, entry)
    before = _registered(hass, entry)

    by_hand = {key: value for key, value in PLANES[0].items() if not key.startswith("roof_")}
    await _change_planes(hass, entry, [by_hand, PLANES[1]])
    gone = {"plane1_typical_day_energy", "plane1_forecast_vs_typical", "typical_day_energy", "forecast_vs_typical"}
    assert gone <= before
    assert _registered(hass, entry) == before - gone


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


POINT_LIST = "custom_components.swiss_solar_forecast.async_fetch_points"
ON_A_STATION = {**ENTRY_DATA, "point_type": 1, "point_id": 118, "point_name": "Chur / Station", "point_lat": 46.8700, "point_lon": 9.5300, "point_height": 556.0}


def _entry_on_a_station() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id="46.8700,9.5300:our-roof", title="Our roof", data=ON_A_STATION, entry_id="solar1234")


@freeze_time(datetime(2026, 6, 21, 10, 15, tzinfo=UTC))
async def test_an_entry_on_a_station_moves_to_a_point_with_radiation(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    """Set up before the point was chosen by its kind: the entry sits on a station and never loaded."""
    entry = _entry_on_a_station()
    with patch(POINT_LIST, return_value=POINTS) as point_list:
        await _setup(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert (entry.data["point_type"], entry.data["point_id"], entry.data["point_name"], entry.data["point_height"]) == (2, 700000, "Chur", 593.0)
        # the place itself stays, and so does everything else
        assert (entry.data["point_lat"], entry.data["point_lon"]) == (46.8700, 9.5300)
        assert entry.data["planes"] == PLANES and entry.unique_id == "46.8700,9.5300:our-roof"
        assert entry.runtime_data.point == (700000, 2)
        assert hass.states.get(PREFIX + "energy_today") is not None
        # moved once: a reload asks for no list again
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED and point_list.call_count == 1


async def test_an_entry_on_a_station_waits_for_the_point_list(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    entry = _entry_on_a_station()
    entry.add_to_hass(hass)
    with patch(POINT_LIST, side_effect=OgdUnavailable("HTTP 503")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY and entry.data["point_type"] == 1


async def test_a_postal_code_point_asks_for_no_list(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    with patch(POINT_LIST, side_effect=AssertionError("not needed")) as point_list:
        await _setup(hass, _entry())
    assert point_list.call_count == 0


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
    # learned for the part of the sky the sun stood in from 11 UTC; today's hours are shown in local time
    sky = entry.runtime_data.calibration.bin_of(datetime(2026, 6, 21, 11, 0, tzinfo=UTC))
    assert sky.endswith("/40") and list(factor.attributes["sun_position"]) == [sky]
    assert 0.75 < factor.attributes["hourly"]["13:00"] < 0.9
    assert factor.attributes["sun_position"][sky] == factor.attributes["hourly"]["13:00"]
    today = hass.states.get(PREFIX + "energy_today")
    assert today.attributes["calibrated"] is True
    # the hour that is over counts with what the meter counted, the hours ahead with what was learned
    short = forecast_wh / 2000.0
    assert today.attributes["measured_hours"] == 1
    assert before - short - 0.1 * before < float(today.state) < before - short + 0.01
    assert abs(factor.attributes["energy_today_uncalibrated"] - before) < 0.01  # the model itself is unchanged
    # the planes keep their forecast: nobody knows which of them fell short
    planes = today.attributes["planes"]
    assert abs(planes["South"] + planes["East"] - float(today.state) - short) < 0.03

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["options"]["yield_sensor"] == "**REDACTED**"
    assert diagnostics["calibration"]["samples"] == 1 and diagnostics["calibration"]["bins_by"] == "sun_position"
    assert list(diagnostics["calibration"]["factors"]) == [sky] and diagnostics["finished_hours"]["measured"] == 1
    assert "sensor.pv_total" not in str(diagnostics)

    # an unavailable meter teaches nothing and breaks nothing
    hass.states.async_set("sensor.pv_total", "unavailable", {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 13, 0, 5, tzinfo=UTC))
    assert hass.states.get(PREFIX + "calibration_factor").attributes["samples"] == 1

    # what was learned survives a restart, and so does the hour the meter counted
    learned = hass.states.get(PREFIX + "calibration_factor").state
    counted = float(hass.states.get(PREFIX + "energy_today").state)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["calibration"]["samples"] == 1
    assert len(memory_store[STORE_KEY]["data"]["frozen"]["measured"]) == 1
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "calibration_factor").state == learned
    today = hass.states.get(PREFIX + "energy_today")
    assert today.attributes["measured_hours"] == 1 and abs(float(today.state) - counted) < 0.01

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
    # and nothing to measure the forecast against
    assert hass.states.get(PREFIX + "accuracy_7_days") is None and hass.states.get(PREFIX + "accuracy_30_days") is None
    assert entry.runtime_data.accuracy is None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["accuracy"] is None


async def test_calibration_stays_under_the_inverter_limit(hass: HomeAssistant, enable_custom_integrations, fake_forecast, freezer) -> None:
    freezer.move_to(NOW)
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total", "plant_inverter_kw": 3.0})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    noon = datetime(2026, 6, 21, 11, 30, tzinfo=UTC)
    assert coordinator.data.plant.hour_at(noon).power_kw == 3.0  # clipped already

    # the plant made more than forecast in that hour, often enough to be believed
    sky = coordinator.calibration.bin_of(datetime(2026, 6, 21, 11, 0, tzinfo=UTC))
    coordinator.calibration.bins[sky] = [45000.0, 30000.0, 30.0]
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.calibration.factor(sky) > 1.2
    assert coordinator.data.plant.hour_at(noon).power_kw == 3.0
    assert coordinator.data.raw_plant.hour_at(noon).power_kw == 3.0


WINTER = datetime(2026, 1, 15, 8, 15, tzinfo=UTC)
FETCH = "custom_components.swiss_solar_forecast.coordinator.async_fetch_forecast"


@freeze_time(NOW)
async def test_snow_is_unknown_without_a_precipitation_forecast(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    entry = _entry()
    await _setup(hass, entry)
    snow = hass.states.get(PREFIX + "snow_cover")
    assert snow is not None and snow.state == "unknown"
    hour = entry.runtime_data.data.plant.hour_at(NOW)
    assert hour.snow_cover == 0.0 and hour.snow_loss_kw == 0.0


@freeze_time(WINTER)
async def test_snow_on_the_modules(hass: HomeAssistant, enable_custom_integrations, snow_forecast, memory_store) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    # without the snow model: the plain forecast, and no sensor
    await _setup(hass, _entry(options={"snow": False}))
    assert hass.states.get(PREFIX + "snow_cover") is None
    free = float(hass.states.get(PREFIX + "energy_today").state)
    assert free > 10
    await hass.config_entries.async_remove("solar1234")
    await hass.async_block_till_done()

    # six millimetres fell in the night; at a quarter past nine the first sun is on the modules
    entry = _entry()
    await _setup(hass, entry)
    snow = hass.states.get(PREFIX + "snow_cover")
    assert 90 <= int(snow.state) <= 100
    assert set(snow.attributes["planes"]) == {"South", "East"} and min(snow.attributes["planes"].values()) >= 90
    assert snow.attributes["ground_snow_mm"] == 6.0
    assert snow.attributes["snow_free_from"] > "2026-01-15T09"  # some hour later, when it has slid off and melted
    lost = snow.attributes["energy_lost_today"]
    today = hass.states.get(PREFIX + "energy_today")
    assert float(today.state) < 0.8 * free and lost > 0.2 * free
    # what the snow takes and what is left make the free plant's day (a little more: the white ground reflects)
    assert free - 0.05 < float(today.state) + lost < 1.15 * free
    plant = entry.runtime_data.data.plant
    night = plant.hour_at(datetime(2026, 1, 15, 3, 30, tzinfo=UTC))
    assert night.snow_cover > 0.5 and night.power_kw == 0.0 and night.snow_loss_kw == 0.0
    # the third day is mild: nothing is left on the modules
    assert plant.hour_at(datetime(2026, 1, 17, 11, 30, tzinfo=UTC)).snow_cover == 0.0
    assert plant.hour_at(datetime(2026, 1, 17, 11, 30, tzinfo=UTC)).snow_loss_kw == 0.0
    assert plant.hour_at(datetime(2026, 1, 15, 9, 30, tzinfo=UTC)).wind == 2.0  # 7.2 km/h

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["snow"] == {
        "enabled": True,
        "followed_until": "2026-01-15T08:00:00+00:00",
        "ground_mm": 6.0,
        "planes": [{"load_mm": 6.0, "cover": 1.0}, {"load_mm": 6.0, "cover": 1.0}],
        "hours_kept": 8,
    }

    # kept over a restart: the next run no longer knows of the snowfall, the snow is still there
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    kept = memory_store[STORE_KEY]["data"]["snow"]
    assert kept["time"] == "2026-01-15T08:00:00+00:00" and kept["planes"] == [[6.0, 1.0], [6.0, 1.0]]
    with patch(FETCH, return_value=snow_run(snowfall_hours=0)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    again = hass.states.get(PREFIX + "snow_cover")
    assert again.state == snow.state and again.attributes["ground_snow_mm"] == 6.0

    # switched off in the options: the plain forecast again, the sensor goes, nothing is kept
    hass.config_entries.async_update_entry(entry, options={"snow": False})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "snow_cover") is None
    assert er.async_get(hass).async_get(PREFIX + "snow_cover") is None
    assert abs(float(hass.states.get(PREFIX + "energy_today").state) - free) < 0.01
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["snow"] == {"enabled": False}
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["snow"] is None


async def test_the_meter_shows_the_modules_free(hass: HomeAssistant, enable_custom_integrations, snow_forecast, freezer) -> None:
    freezer.move_to(WINTER)
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total"})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    before = float(hass.states.get(PREFIX + "energy_today").state)

    await _full_hour(hass, freezer, datetime(2026, 1, 15, 9, 0, 5, tzinfo=UTC))
    hour = coordinator.data.plant.hour_at(datetime(2026, 1, 15, 9, 30, tzinfo=UTC))
    assert hour.snow_cover > 0.5 and hour.snow_loss_kw > hour.power_kw
    free_wh = (hour.power_kw + hour.snow_loss_kw) * 1000.0
    # the plant made nearly what free modules would have made: somebody swept them, or the snow slid off early
    hass.states.async_set("sensor.pv_total", str(1000.0 + 0.9 * free_wh / 1000.0), {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 1, 15, 10, 0, 5, tzinfo=UTC))

    snow = hass.states.get(PREFIX + "snow_cover")
    assert snow.state == "0" and snow.attributes["snow_free_from"] is None
    assert snow.attributes["ground_snow_mm"] > 5  # the ground keeps its snow
    assert coordinator.snow.planes == [[0.0, 0.0], [0.0, 0.0]]
    assert float(hass.states.get(PREFIX + "energy_today").state) > before
    # the hour that is over stays as it was forecast
    assert abs(coordinator.data.plant.hour_at(datetime(2026, 1, 15, 9, 30, tzinfo=UTC)).snow_cover - hour.snow_cover) < 0.001
    # an hour under snow teaches the calibration nothing, but it counts for the accuracy
    assert coordinator.calibration.samples == 0
    assert coordinator.accuracy.days["2026-01-15"][3] == 1


async def test_a_plant_under_snow_stays_under_snow(hass: HomeAssistant, enable_custom_integrations, snow_forecast, freezer) -> None:
    freezer.move_to(WINTER)
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total"})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    await _full_hour(hass, freezer, datetime(2026, 1, 15, 9, 0, 5, tzinfo=UTC))
    hour = coordinator.data.plant.hour_at(datetime(2026, 1, 15, 9, 30, tzinfo=UTC))
    # the meter counts about what the forecast said under snow
    hass.states.async_set("sensor.pv_total", str(1000.0 + hour.energy_wh / 1000.0), {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 1, 15, 10, 0, 5, tzinfo=UTC))
    assert coordinator.snow.covered and int(hass.states.get(PREFIX + "snow_cover").state) > 50
    assert coordinator.calibration.samples == 0


async def test_accuracy_against_the_yield_meter(hass: HomeAssistant, enable_custom_integrations, fake_forecast, freezer, memory_store) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total"})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    week = hass.states.get(PREFIX + "accuracy_7_days")
    assert week.state == "unknown" and week.attributes["days"] == 0 and week.attributes["same_day_accuracy"] is None
    assert er.async_get(hass).async_get(PREFIX + "accuracy_30_days").entity_category == "diagnostic"

    # the meter closes an hour: forecast and count go to the day's sums
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 11, 0, 5, tzinfo=UTC))
    forecast_wh = coordinator.data.plant.hour_at(datetime(2026, 6, 21, 11, 30, tzinfo=UTC)).energy_wh
    hass.states.async_set("sensor.pv_total", str(1000.0 + 0.9 * forecast_wh / 1000.0), {"unit_of_measurement": "kWh"})
    await _full_hour(hass, freezer, datetime(2026, 6, 21, 12, 0, 5, tzinfo=UTC))
    sums = coordinator.accuracy.days["2026-06-21"]
    assert sums[3] == 1 and abs(sums[0] - 0.9 * forecast_wh) < 1 and abs(sums[1] - forecast_wh) < 1
    assert sums[6] == 0  # the hour's forecast of the day before was never seen
    assert hass.states.get(PREFIX + "accuracy_7_days").state == "unknown"  # the running day does not count

    # a week of full days: 10 % too high the day before, 5 % too high in the end
    for day in range(14, 21):
        coordinator.accuracy.days[f"2026-06-{day}"] = [50000.0, 52500.0, 4000.0, 24.0, 50000.0, 55000.0, 24.0]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    week = hass.states.get(PREFIX + "accuracy_7_days")
    assert week.state == "90.0"
    assert week.attributes["same_day_accuracy"] == 95.0 and week.attributes["hourly_accuracy"] == 92.0
    assert week.attributes["bias"] == 5.0 and week.attributes["days"] == 7 and week.attributes["days_day_ahead"] == 7
    assert week.attributes["measured_kwh"] == 350.0 and week.attributes["forecast_kwh"] == 367.5
    assert "day_ahead_accuracy" not in week.attributes
    month = hass.states.get(PREFIX + "accuracy_30_days")
    assert month.state == "90.0" and month.attributes["days"] == 7

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["accuracy"]["7_days"]["day_ahead_accuracy"] == 90.0 and diagnostics["accuracy"]["days_kept"] == 8
    assert diagnostics["accuracy"]["open_hours"] > 24

    # kept over a restart
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(memory_store[STORE_KEY]["data"]["accuracy"]["days"]) == 8
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "accuracy_7_days").state == "90.0"

    # another plane: the old days say nothing about the new plant
    hass.config_entries.async_update_entry(entry, options={"yield_sensor": "sensor.pv_total", "planes": [{**PLANES[0], "kwp": 12.0}, PLANES[1]]})
    await hass.async_block_till_done(wait_background_tasks=True)
    week = hass.states.get(PREFIX + "accuracy_7_days")
    assert week.state == "unknown" and week.attributes["days"] == 0

    # the meter is taken out: the sensors go, and so does what was kept
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "accuracy_7_days") is None and hass.states.get(PREFIX + "accuracy_30_days") is None
    assert er.async_get(hass).async_get(PREFIX + "accuracy_7_days") is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["accuracy"] is None


FIND = "custom_components.swiss_solar_forecast.coordinator.async_find_latest_run"


CATALOGUE = "custom_components.swiss_solar_forecast.coordinator.async_catalogue_latest_run"


@freeze_time(NOW)
async def test_the_run_check_is_counted_with_the_requests_it_took(hass: HomeAssistant, enable_custom_integrations, fake_forecast) -> None:
    """One HEAD request as a rule, two while the run of the hour is not
    out yet, and the catalogue on top when no run was found."""
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data

    def probing(requests: int, found: datetime | None):
        async def find(session, now, stats=None):
            stats.requests += requests
            return found

        return find

    async def cost_of_a_check() -> int:
        before = coordinator.data.requests_today
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.last_update_success
        return coordinator.data.requests_today - before

    with patch(FIND, side_effect=probing(0, RUN)):
        besides = await cost_of_a_check()  # whatever else a wake reads
    with patch(FIND, side_effect=probing(1, RUN)):
        assert await cost_of_a_check() == besides + 1
    with patch(FIND, side_effect=probing(2, RUN)):
        assert await cost_of_a_check() == besides + 2
    with patch(FIND, side_effect=probing(7, None)), patch(CATALOGUE, return_value=RUN):
        assert await cost_of_a_check() == besides + 8


async def test_the_temperature_is_not_read_with_every_run(hass: HomeAssistant, enable_custom_integrations, freezer) -> None:
    """The temperature file costs many times the others: its series is kept for some hours."""
    hour = timedelta(hours=1)
    asked: list[tuple[datetime, tuple[str, ...]]] = []
    unreadable: set[str] = set()

    async def fetch(session, run, point, params):
        asked.append((run, params))
        data = snow_run(run)
        data.series = {p: s for p, s in data.series.items() if p in params and p not in unreadable}
        data.missing = [p for p in params if p in unreadable]
        data.stats.requests, data.stats.bytes = 26 * len(params), 40_000 * len(params)
        return data

    async def at(run: datetime) -> None:
        freezer.move_to(run + 4 * hour + timedelta(minutes=15))
        find.return_value = run
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    freezer.move_to(WINTER)
    entry = _entry()
    with patch(FIND, return_value=WINTER_RUN) as find, patch(FETCH, side_effect=fetch):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        assert asked == [(WINTER_RUN, HOURLY_PARAMS)]
        assert coordinator.series_runs == {param: WINTER_RUN for param in HOURLY_PARAMS}
        assert (coordinator.data.last_requests, coordinator.data.last_bytes) == (130, 200_000)

        # the next runs read the four cheap files and go on with the temperature they have
        for later in (1, 5):
            await at(WINTER_RUN + later * hour)
            run, params = asked[-1]
            assert run == WINTER_RUN + later * hour and params == tuple(p for p in HOURLY_PARAMS if p != "tre200h0")
            assert coordinator.series_runs["tre200h0"] == WINTER_RUN and coordinator.series_runs["gre000h0"] == run
            assert coordinator.data.run == run and coordinator.data.missing == []
            assert coordinator.data.plant.hour_at(dt_now(freezer)).temperature == -3.0
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["forecast"]["series_runs"]["tre200h0"] == "2026-01-15T04:00:00+00:00"
        assert diagnostics["forecast"]["series_runs"]["gre000h0"] == "2026-01-15T09:00:00+00:00"
        # what the last read cost stays visible, whatever was asked in between
        assert diagnostics["forecast"]["last_read"] == {"requests": 104, "bytes": 160_000}
        assert diagnostics["forecast"]["requests_today"] > 130 + 2 * 104

        # after six hours it is read again
        await at(WINTER_RUN + 6 * hour)
        assert asked[-1] == (WINTER_RUN + 6 * hour, HOURLY_PARAMS)
        assert coordinator.series_runs["tre200h0"] == WINTER_RUN + 6 * hour

        # a temperature file that cannot be read: the kept series serves on, for as long as a forecast counts
        unreadable.add("tre200h0")
        await at(WINTER_RUN + 12 * hour)
        assert asked[-1][1] == HOURLY_PARAMS
        assert coordinator.series_runs["tre200h0"] == WINTER_RUN + 6 * hour and coordinator.data.missing == []
        assert coordinator.data.plant.hour_at(dt_now(freezer)).temperature == -3.0
        await at(WINTER_RUN + 19 * hour)
        assert asked[-1][1] == HOURLY_PARAMS
        assert "tre200h0" not in coordinator.series_runs and coordinator.data.missing == ["tre200h0"]
        assert coordinator.data.plant.hour_at(dt_now(freezer)).temperature is None

        # readable again
        unreadable.clear()
        await at(WINTER_RUN + 20 * hour)
        assert coordinator.series_runs["tre200h0"] == WINTER_RUN + 20 * hour and coordinator.data.missing == []


def dt_now(freezer) -> datetime:
    return freezer().replace(tzinfo=UTC)


def test_a_kept_series_is_stretched_to_the_end_of_the_new_run() -> None:
    first = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
    kept = [(first + timedelta(hours=i), float(i % 24)) for i in range(48)]
    wanted = [first + timedelta(hours=i) for i in range(3, 51)]
    out = dict(stretched(kept, wanted))
    assert len(out) == 51 and all(out[ts] == value for ts, value in kept)
    # the three hours the kept series lacks repeat the day before
    assert [out[first + timedelta(hours=i)] for i in (48, 49, 50)] == [0.0, 1.0, 2.0]
    # nothing to repeat, nothing made up
    assert stretched([(first, None)], [first + timedelta(days=1)]) == [(first, None)]
