"""The coordinator and the station's own shade: the record that shows
it, what is left out while the sun stands in it, what is set right once,
and what leaves with the diagnostics."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.swiss_solar_forecast import nowcast
from custom_components.swiss_solar_forecast.calibration import sun_bin
from custom_components.swiss_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.swiss_solar_forecast.ogd import OgdUnavailable, Station
from custom_components.swiss_solar_forecast.solar import SunPosition

from .conftest import WINTER_RUN, clear_sky_run, record_file, station_day
from .test_init import NOW, PREFIX, STORE_KEY, _entry, _full_hour, _setup

LAT, LON = 47.3769, 8.5417
STEP = timedelta(minutes=10)
NEAR = Station(abbr="ZZZ", name="Somewhere", height=420.0, lat=47.38, lon=8.55)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 21, hour, minute, tzinfo=UTC)


def _ridge(when: datetime, sun: SunPosition) -> float:
    """A ridge west of the station: the evening sun goes behind it."""
    return 0.15 if sun.azimuth >= 262 and sun.elevation < 35 else 1.0


def _days(first: date, count: int, share=_ridge) -> list[tuple[datetime, float]]:
    return [value for n in range(count) for value in station_day(first + timedelta(days=n), LAT, LON, share)]


def _record(first: date = date(2026, 6, 18), count: int = 3):
    """What reading the station's record returns: the days as one piece, one request."""
    body = record_file(_days(first, count))
    return [(body, None)], 1, len(body)


def _measured(share: float, newest: datetime, count: int = 6) -> list[tuple[datetime, float]]:
    """A station that received `share` of what was forecast, in ten-minute values up to `newest`."""
    forecast = dict(clear_sky_run().series["gre000h0"])
    ends = [newest - index * STEP for index in range(count)]
    return sorted((end, round(share * nowcast.expected_at(forecast, end, LAT, LON), 1)) for end in ends)


def _hours(hass: HomeAssistant) -> dict[datetime, int]:
    hourly = hass.states.get(PREFIX + "energy_today").attributes["hourly"]
    return {datetime.fromisoformat(start): watts for start, watts in hourly.items()}


async def _restart(hass: HomeAssistant, entry, change) -> None:
    """Unload, let `change` rewrite what the entry kept, and set up again."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    change()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_station_in_the_shade_is_not_asked(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer) -> None:
    stations, radiation = fake_stations
    record, history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    record.return_value = _record()
    freezer.move_to(NOW)
    await _setup(hass, _entry())

    # the record of the year was read whole, once; in June the earlier years are not needed
    assert record.call_count == 1 and record.call_args.args[1:] == ("ZZZ", None) and history.call_count == 0
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "100" and found.attributes["station_in_shade"] is False
    shade = found.attributes["station_shade"]
    assert shade and all(260 <= int(bearing) < 310 and 5 < height <= 40 for bearing, height in shade.items())
    assert shade["270"] > 30
    before = _hours(hass)

    # in the evening the sun stands behind the ridge: the station receives a fraction, under a cloudless sky
    radiation.return_value = (_measured(0.15, _at(16, 40)), 2000)
    await _full_hour(hass, freezer, _at(16, 41))
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "unknown" and found.attributes["station_in_shade"] is True and found.attributes["measured_w_m2"] is None
    # the hours ahead stay as the forecast has them, and the record is not read again that day
    assert _hours(hass)[_at(17)] == before[_at(17)] > 0
    assert record.call_count == 1 and found.attributes["sky_samples"] == 0


async def test_without_a_record_the_station_is_taken_at_its_word(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer) -> None:
    stations, radiation = fake_stations
    record, _history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    await _setup(hass, _entry())
    found = hass.states.get(PREFIX + "station_correction")
    assert found.state == "100" and found.attributes["station_in_shade"] is None and found.attributes["station_shade"] is None
    before = _hours(hass)

    radiation.return_value = (_measured(0.15, _at(16, 40)), 2000)
    await _full_hour(hass, freezer, _at(16, 41))
    found = hass.states.get(PREFIX + "station_correction")
    # its word changes the hours ahead (the planes here face away from the evening sun: a clouded sky gives them more)
    assert found.state == "30" and _hours(hass)[_at(17)] != before[_at(17)]
    # an empty answer is asked for again later, not with every wake
    assert record.call_count == 2


async def test_a_record_that_cannot_be_read_is_asked_for_later(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer) -> None:
    stations, radiation = fake_stations
    record, _history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    record.side_effect = OgdUnavailable("no answer")
    freezer.move_to(NOW)
    await _setup(hass, _entry())
    assert hass.states.get(PREFIX + "station_correction").state == "100" and record.call_count == 1
    radiation.return_value = (_measured(1.0, _at(10, 30)), 2000)
    await _full_hour(hass, freezer, _at(10, 31))
    assert hass.states.get(PREFIX + "station_correction").state == "100" and record.call_count == 1
    # three hours later it is tried again, and this time it answers
    record.side_effect = None
    record.return_value = _record()
    radiation.return_value = (_measured(1.0, _at(13, 10)), 2000)
    await _full_hour(hass, freezer, _at(13, 20))
    found = hass.states.get(PREFIX + "station_correction")
    assert record.call_count == 2 and found.attributes["station_shade"] and found.attributes["station_in_shade"] is False


async def test_after_the_first_time_only_the_tail_is_read(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    record, _history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    record.return_value = _record()
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    view = (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"]
    assert view["known"] is True and view["covered"] == "2026-06-20" and (view["days"], view["sunny_days"]) == (3, 3)

    # two days behind: the tail of three days is asked for (the running day comes with it and is not taken)
    def two_days_behind() -> None:
        memory_store[STORE_KEY]["data"]["sky_view"]["covered"] = "2026-06-18"

    await _restart(hass, entry, two_days_behind)
    assert record.call_count == 2 and record.call_args.args[1:] == ("ZZZ", 3)
    view = (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"]
    assert view["covered"] == "2026-06-20" and view["days"] == 5

    # a tail that does not reach back to the last day taken: the whole record is read instead
    def four_days_behind() -> None:
        memory_store[STORE_KEY]["data"]["sky_view"]["covered"] = "2026-06-16"

    record.side_effect = [_record(date(2026, 6, 20), 1), _record(date(2026, 6, 15), 6)]
    await _restart(hass, entry, four_days_behind)
    assert [call.args[2] for call in record.call_args_list[-2:]] == [5, None]
    view = (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"]
    assert view["covered"] == "2026-06-20" and view["days"] == 9


async def test_early_in_a_year_the_end_of_the_year_before_is_read_too(hass: HomeAssistant, enable_custom_integrations, snow_forecast, fake_stations, fake_record, freezer) -> None:
    stations, radiation = fake_stations
    record, history = fake_record
    stations.return_value = ([NEAR], 150000)
    now = WINTER_RUN.replace(hour=11, minute=15)
    radiation.return_value = ([(now - STEP * index, 120.0) for index in range(1, 7)], 2000)
    older = record_file(_days(date(2025, 11, 10), 3, lambda when, sun: 1.0))
    history.return_value = ([(older, None)], 3, len(older))
    record.return_value = _record(date(2026, 1, 10), 5)
    freezer.move_to(now)
    entry = _entry()
    await _setup(hass, entry)
    assert history.call_count == 1 and history.call_args.args[1:] == ("ZZZ", 2026)
    assert record.call_count == 1 and record.call_args.args[2] is None
    view = (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"]
    assert view["covered"] == "2026-01-14" and view["days"] == 8


async def test_what_was_learned_in_the_shade_is_dropped_once(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer, memory_store) -> None:
    stations, radiation = fake_stations
    record, _history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    freezer.move_to(NOW)
    hass.states.async_set("sensor.pv_total", "1000.0", {"unit_of_measurement": "kWh"})
    entry = _entry(options={"yield_sensor": "sensor.pv_total"})
    await _setup(hass, entry)
    evening = sun_bin(datetime(2026, 6, 20, 16, 0, tzinfo=UTC), LAT, LON)
    noon = sun_bin(datetime(2026, 6, 20, 11, 0, tzinfo=UTC), LAT, LON)

    def learned_in_the_shade() -> None:
        data = memory_store[STORE_KEY]["data"]
        data["sky"]["sums"] = {"clear": [600.0, 800.0, 12.0], "mixed": [300.0, 300.0, 8.0], "grey": [100.0, 80.0, 9.0]}
        data["calibration"]["bins"] = {evening: [1500.0, 1000.0, 5.0], noon: [5000.0, 5000.0, 5.0]}

    record.return_value = _record()
    await _restart(hass, entry, learned_in_the_shade)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    # the clear and the mixed sky were judged by hours in the shade; the grey sky casts no shadow
    assert set(diagnostics["station_correction"]["sky"]["sums"]) == {"grey"}
    # the part of the sky behind the ridge is learned afresh, the rest stays
    assert set(diagnostics["calibration"]["bins"]) == {noon}
    assert diagnostics["station_correction"]["sky_view"]["settled"] is True

    # that happens once: what is learned from now on stays
    def learned_since() -> None:
        data = memory_store[STORE_KEY]["data"]
        assert data["sky_view"]["settled"] is True
        data["sky"]["sums"]["clear"] = [700.0, 800.0, 12.0]
        data["calibration"]["bins"][evening] = [1000.0, 1000.0, 5.0]

    await _restart(hass, entry, learned_since)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert set(diagnostics["station_correction"]["sky"]["sums"]) == {"clear", "grey"}
    assert set(diagnostics["calibration"]["bins"]) == {evening, noon}


async def test_the_shade_leaves_with_the_diagnostics_in_brief(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_stations, fake_record, freezer) -> None:
    stations, radiation = fake_stations
    record, _history = fake_record
    stations.return_value = ([NEAR], 150000)
    radiation.return_value = (_measured(1.0, _at(10, 10)), 2000)
    record.return_value = _record()
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    view = (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"]
    # how much of the sky is shaded and how high, not where: that would tell the station
    assert view == {
        "known": True,
        "covered": "2026-06-20",
        "days": 3,
        "sunny_days": 3,
        "settled": True,
        "shaded_bearings": view["shaded_bearings"],
        "highest_shade": view["highest_shade"],
        "in_shade_now": False,
    }
    assert view["shaded_bearings"] >= 4 and 30 <= view["highest_shade"] <= 40


async def test_without_a_station_there_is_no_view(hass: HomeAssistant, enable_custom_integrations, fake_forecast, fake_record, freezer) -> None:
    record, _history = fake_record
    freezer.move_to(NOW)
    entry = _entry()
    await _setup(hass, entry)
    assert record.call_count == 0
    assert (await async_get_config_entry_diagnostics(hass, entry))["station_correction"]["sky_view"] is None
    found = hass.states.get(PREFIX + "station_correction")
    assert found.attributes["station_in_shade"] is None and found.attributes["station_shade"] is None
