"""The coordinator learning what the weather model gets wrong by the kind
of sky: what the station's hours teach, where a learned factor applies,
what is kept and what leaves with the diagnostics."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.swiss_solar_forecast import nowcast
from custom_components.swiss_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.swiss_solar_forecast.ogd import Station

from .conftest import clear_sky_run
from .test_init import NOW, PREFIX, STORE_KEY, _entry, _full_hour, _setup

STEP = timedelta(minutes=10)
NEAR = Station(abbr="ZZZ", name="Somewhere", height=420.0, lat=47.38, lon=8.55)
FAR = Station(abbr="FAR", name="Elsewhere", height=420.0, lat=47.90, lon=8.55)
NOTHING = {"clear": None, "mixed": None, "grey": None}


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


async def _restart(hass: HomeAssistant, entry, memory_store, sky: dict | None = None) -> None:
    """Unload, put `sky` in place of what was learned, and set up again."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    if sky is not None:
        memory_store[STORE_KEY]["data"]["sky"].update(sky)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_stations_hours_teach_the_sky(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([FAR, NEAR], 150000)
    radiation.return_value = (_measured(0.6, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)

    # nothing learned yet: the hour before the start was never seen as a forecast
    found = hass.states.get(PREFIX + "station_correction")
    assert found.attributes["sky_factors"] == NOTHING and found.attributes["sky_samples"] == 0
    assert found.attributes["sky_hours"] == {"clear": 0.0, "mixed": 0.0, "grey": 0.0} and found.attributes["sky_last_sample"] is None
    assert hass.states.get(PREFIX + "energy_today").attributes["sky_correction"] is False

    # an hour later the station is through with the hour that was running
    radiation.return_value = (_measured(0.6, _at(11, 10), count=8), 2000)
    await _full_hour(hass, freezer, _at(11, 16))
    found = hass.states.get(PREFIX + "station_correction")
    assert found.attributes["sky_samples"] == 1 and found.attributes["sky_last_sample"] == "2026-06-21T10:00:00+00:00"
    assert found.attributes["sky_hours"] == {"clear": 1.0, "mixed": 0.0, "grey": 0.0}
    assert found.attributes["sky_factors"] == NOTHING  # one hour is no evidence
    assert hass.states.get(PREFIX + "energy_today").attributes["sky_correction"] is False

    # the same file read again teaches nothing new
    await _full_hour(hass, freezer, _at(11, 31))
    assert hass.states.get(PREFIX + "station_correction").attributes["sky_samples"] == 1

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    told = diagnostics["station_correction"]["sky"]
    assert told["samples"] == 1 and told["factors"] == NOTHING and told["logged_hours"] > 10 and told["trust"] > 0.9
    measured, forecast, count = told["sums"]["clear"]
    assert count == 1.0 and 0.55 < measured / forecast < 0.65
    skies = {hour["start"]: hour["sky"] for hour in diagnostics["forecast"]["hours"]}
    assert skies["2026-06-21T11:00:00+00:00"] == "clear" and skies["2026-06-21T23:00:00+00:00"] is None
    # the place is the user's: neither the station nor where it was learned leaves with the diagnostics
    assert "Somewhere" not in str(diagnostics) and "ZZZ" not in str(diagnostics) and "at" not in told

    # kept over a restart, with the place it belongs to
    await _restart(hass, entry, memory_store)
    kept = memory_store[STORE_KEY]["data"]["sky"]
    assert kept["samples"] == 1 and kept["at"] == [47.377, 8.542] and kept["done"] == "2026-06-21T10:00:00+00:00"
    assert hass.states.get(PREFIX + "station_correction").attributes["sky_samples"] == 1


async def test_a_learned_sky_corrects_the_hours_further_ahead(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([FAR, NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    before = _hours(hass)
    tomorrow = float(hass.states.get(PREFIX + "energy_tomorrow").state)

    # thirty clear hours in which the station received six tenths of the forecast
    await _restart(hass, entry, memory_store, {"sums": {"clear": [18000.0, 30000.0, 30.0]}, "samples": 30})
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "100"  # the last hour was as forecast
    assert found.attributes["sky_factors"] == {"clear": 0.6, "mixed": None, "grey": None} and found.attributes["sky_hours"]["clear"] == 30.0
    state = hass.states.get(PREFIX + "energy_today")
    assert state.attributes["sky_correction"] is True
    after = _hours(hass)
    assert after[_at(9)] == before[_at(9)]  # the hours that are over
    assert after[_at(10)] > 0.9 * before[_at(10)]  # the running hour follows the last hour's measurement
    assert after[_at(10)] / before[_at(10)] > after[_at(12)] / before[_at(12)] > after[_at(14)] / before[_at(14)]  # the factor takes over
    assert 0.4 * before[_at(14)] < after[_at(14)] < 0.8 * before[_at(14)]  # beyond the measurement's reach it stands alone
    assert 0.4 * tomorrow < float(hass.states.get(PREFIX + "energy_tomorrow").state) < 0.8 * tomorrow

    # switched off in the options: the forecast as the run has it, nothing is kept
    hass.config_entries.async_update_entry(entry, options={"station_correction": False})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(PREFIX + "energy_today").attributes["sky_correction"] is False
    assert _hours(hass)[_at(14)] == before[_at(14)]
    assert abs(float(hass.states.get(PREFIX + "energy_tomorrow").state) - tomorrow) < 0.01
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["station_correction"]["sky"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert memory_store[STORE_KEY]["data"]["sky"] is None


async def test_without_a_station_in_reach_nothing_is_corrected(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([FAR], 150000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    before = _hours(hass)

    # what an earlier station taught stays, but counts nothing without a station
    await _restart(hass, entry, memory_store, {"sums": {"clear": [18000.0, 30000.0, 30.0]}, "samples": 30})
    found = hass.states.get(PREFIX + "station_correction")
    assert found.attributes["station"] is None and found.attributes["sky_factors"]["clear"] == 0.6
    assert hass.states.get(PREFIX + "energy_today").attributes["sky_correction"] is False
    assert _hours(hass) == before
    radiation.assert_not_called()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["station_correction"]["sky"]["trust"] == 0.0


async def test_what_was_learned_elsewhere_does_not_count(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    before = _hours(hass)
    await _restart(hass, entry, memory_store, {"sums": {"clear": [18000.0, 30000.0, 30.0]}, "samples": 30, "at": [46.0, 7.0]})
    found = hass.states.get(PREFIX + "station_correction")
    assert found.attributes["sky_factors"] == NOTHING and found.attributes["sky_samples"] == 0
    assert _hours(hass)[_at(14)] == before[_at(14)]
