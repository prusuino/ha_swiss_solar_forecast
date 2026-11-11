"""The coordinator with a measurement station nearby, and the hours that
are over: what the station changes and what it leaves alone, what a later
run may no longer touch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.swiss_solar_forecast import nowcast
from custom_components.swiss_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable, Station

from .conftest import RUN, clear_sky_run
from .test_init import FETCH, FIND, NOW, PREFIX, STORE_KEY, _entry, _full_hour, _setup

STEP = timedelta(minutes=10)
NEAR = Station(abbr="ZZZ", name="Somewhere", height=420.0, lat=47.38, lon=8.55)
FAR = Station(abbr="FAR", name="Elsewhere", height=420.0, lat=47.90, lon=8.55)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 21, hour, minute, tzinfo=UTC)


def _measured(share: float, newest: datetime, count: int = 6) -> list[tuple[datetime, float]]:
    """A station that received `share` of what was forecast, in ten-minute values up to `newest`."""
    forecast = dict(clear_sky_run().series["gre000h0"])
    ends = [newest - index * STEP for index in range(count)]
    return sorted((end, round(share * nowcast.expected_at(forecast, end, 47.3769, 8.5417), 1)) for end in ends)


def _hours(hass: HomeAssistant) -> dict[datetime, int]:
    hourly = hass.states.get(PREFIX + "energy_today").attributes["hourly"]
    return {datetime.fromisoformat(start): watts for start, watts in hourly.items()}


async def test_a_station_under_cloud_lowers_the_hours_ahead(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([FAR, NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)

    # the station sees what was forecast: nothing changes
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "100" and found.attributes["station"] == "Somewhere" and 0 < found.attributes["distance_km"] < 2
    assert stations.call_count == 1 and radiation.call_args.args[1] == "ZZZ"
    before = _hours(hass)
    today = float(hass.states.get(PREFIX + "energy_today").state)

    # a quarter of an hour later it stands under cloud
    radiation.return_value = (_measured(0.5, _at(10, 20)), 2000)
    await _full_hour(hass, freezer, _at(10, 31))
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "50" and found.attributes["measured_at"] == "2026-06-21T10:20:00+00:00"
    assert 0 < found.attributes["weight"] <= 1 and found.attributes["measured_w_m2"] < found.attributes["forecast_w_m2"]
    after = _hours(hass)
    assert after[_at(10)] < 0.75 * before[_at(10)]  # the running hour takes most of it
    assert after[_at(10)] / before[_at(10)] < after[_at(12)] / before[_at(12)] < 1  # fading out
    assert after[_at(14)] == before[_at(14)]  # out of reach
    assert after[_at(9)] == before[_at(9)]  # the hours that are over
    state = hass.states.get(PREFIX + "energy_today")
    assert float(state.state) < today and state.attributes["station_correction"] is True
    assert stations.call_count == 1  # the station is chosen once

    # the place is the user's: neither the station's name nor its abbreviation leaves with the diagnostics
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    told = diagnostics["station_correction"]
    assert told["enabled"] is True and told["found"] is True and abs(told["correction"]["ratio"] - 0.5) < 0.01
    assert "Somewhere" not in str(diagnostics) and "ZZZ" not in str(diagnostics)

    # the station falls silent: its last word grows old and the forecast stands as the run has it
    await _full_hour(hass, freezer, _at(11, 46))
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "unknown" and found.attributes["station"] == "Somewhere"
    state = hass.states.get(PREFIX + "energy_today")
    assert state.attributes["station_correction"] is False and _hours(hass)[_at(12)] == before[_at(12)]
    assert _hours(hass)[_at(10)] == after[_at(10)]  # the hour ended as it was last seen


async def test_switched_off_no_station_is_asked(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([NEAR], 150000)
    freezer.move_to(NOW)
    await _setup(hass, _entry(options={"station_correction": False}))
    stations.assert_not_called()
    radiation.assert_not_called()
    assert hass.states.get(PREFIX + "station_correction") is None
    assert hass.states.get(PREFIX + "energy_today").attributes["station_correction"] is False


async def test_no_station_in_reach(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([FAR], 150000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "unknown" and found.attributes["station"] is None
    # known for a month: the list is not read again with every look at the forecast, nor after a restart
    await _full_hour(hass, freezer, _at(10, 31))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["station"]["abbr"] == ""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert stations.call_count == 1
    radiation.assert_not_called()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["station_correction"]["found"] is False and diagnostics["station_correction"]["correction"] is None


async def test_a_station_that_does_not_answer_costs_nothing(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([NEAR], 150000)
    radiation.side_effect = OgdUnavailable("no answer")
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    assert entry.runtime_data.last_update_success
    assert float(hass.states.get(PREFIX + "energy_today").state) > 0
    assert hass.states.get(PREFIX + "station_correction").state == "unknown"
    asked = radiation.call_count
    await _full_hour(hass, freezer, _at(10, 31))
    assert radiation.call_count == asked  # not with every look at the forecast
    assert entry.runtime_data.last_update_success


async def test_a_later_run_leaves_the_finished_hours(hass: HomeAssistant, enable_custom_integrations, freezer, memory_store) -> None:
    later = RUN + timedelta(hours=6)
    dull = clear_sky_run(later)
    for param in ("gre000h0", "ods000h0"):
        dull.series[param] = [(end, None if value is None else value * 0.5) for end, value in dull.series[param]]
    freezer.move_to(NOW)
    entry = _entry()
    with patch(FIND, return_value=RUN) as find, patch(FETCH, return_value=clear_sky_run()) as fetch:
        await _setup(hass, entry)
        before = _hours(hass)
        today = float(hass.states.get(PREFIX + "energy_today").state)

        # a run that sees half the light, for the morning as well: the morning is over
        find.return_value = later
        fetch.return_value = dull
        await _full_hour(hass, freezer, _at(10, 31))
        after = _hours(hass)
        assert after[_at(8)] == before[_at(8)] and after[_at(9)] == before[_at(9)]
        assert after[_at(12)] < 0.7 * before[_at(12)]
        assert 0.4 * today < float(hass.states.get(PREFIX + "energy_today").state) < 0.95 * today
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["finished_hours"]["kept"] > 0 and diagnostics["finished_hours"]["measured"] == 0

        # and stays over after a restart
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert memory_store[STORE_KEY]["data"]["frozen"]["hours"]
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _hours(hass)[_at(9)] == before[_at(9)] and _hours(hass)[_at(12)] == after[_at(12)]
