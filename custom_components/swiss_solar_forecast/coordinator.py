"""The data coordinator: reads the three radiation and temperature series
of the entry's forecast point whenever MeteoSwiss publishes a new model
run, and turns them into the hourly plane output.

It wakes every 15 minutes, asks with one HEAD request whether a new run
exists and reads the files only then (a few hundred kilobytes per run with
range requests). What a read cost is kept for the diagnostics, so the
traffic stays visible."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_INVERTER_KW,
    CONF_KWP,
    CONF_LOSSES,
    CONF_POINT_ID,
    CONF_POINT_LAT,
    CONF_POINT_LON,
    CONF_POINT_TYPE,
    CONF_TILT,
    DEFAULT_AZIMUTH,
    DEFAULT_DAMPING,
    DEFAULT_LOSSES,
    DEFAULT_TILT,
    DOMAIN,
    HOURLY_PARAMS,
    PARAM_DIFFUSE,
    PARAM_GLOBAL,
    PARAM_TEMPERATURE,
)
from .issues import IssueReporter
from .ogd import OgdError, OgdUnavailable, async_catalogue_latest_run, async_fetch_forecast, async_find_latest_run
from .solar import HourForecast, Plane, forecast_hours

_LOGGER = logging.getLogger(__name__)

_CHECK_INTERVAL = timedelta(minutes=15)
# A kept run older than this is no forecast any more: the entities go unavailable.
_FORECAST_MAX_AGE = timedelta(hours=12)


@dataclass
class ForecastSnapshot:
    """What the entities read."""

    run: datetime | None = None
    fetched: datetime | None = None
    hours: list[HourForecast] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    diffuse_forecast: bool = False  # whether the diffuse share came from MeteoSwiss or was estimated
    # traffic since local midnight
    traffic_day: datetime | None = None
    requests_today: int = 0
    bytes_today: int = 0
    last_requests: int = 0
    last_bytes: int = 0

    def day_energy_wh(self, day: date) -> float | None:
        values = [h.energy_wh for h in self.hours if dt_util.as_local(h.start).date() == day]
        return sum(values) if values else None

    def remaining_today_wh(self, now: datetime) -> float | None:
        """What is still to come today: the current hour pro rata, later hours whole."""
        today = dt_util.as_local(now).date()
        total = 0.0
        seen = False
        for h in self.hours:
            if dt_util.as_local(h.start).date() != today:
                continue
            seen = True
            end = h.start + timedelta(hours=1)
            if end <= now:
                continue
            if h.start <= now:
                total += h.energy_wh * (end - now).total_seconds() / 3600
            else:
                total += h.energy_wh
        return total if seen else None

    def hour_at(self, when: datetime) -> HourForecast | None:
        for h in self.hours:
            if h.start <= when < h.start + timedelta(hours=1):
                return h
        return None

    def peak(self, day: date) -> HourForecast | None:
        hours = [h for h in self.hours if dt_util.as_local(h.start).date() == day]
        return max(hours, key=lambda h: h.power_kw) if hours else None


def plane_from_entry(entry: ConfigEntry) -> Plane:
    """The plane as configured: options win over the data they started from."""

    def get(key: str, default: float) -> float:
        value = entry.options.get(key, entry.data.get(key, default))
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return Plane(
        kwp=get(CONF_KWP, 0.0),
        azimuth=get(CONF_AZIMUTH, DEFAULT_AZIMUTH),
        tilt=get(CONF_TILT, DEFAULT_TILT),
        losses=get(CONF_LOSSES, DEFAULT_LOSSES),
        inverter_kw=get(CONF_INVERTER_KW, 0.0),
        damping=get(CONF_DAMPING, DEFAULT_DAMPING),
    )


class SolarForecastCoordinator(DataUpdateCoordinator[ForecastSnapshot]):
    """The plane output forecast of one entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} forecast", update_interval=_CHECK_INTERVAL, config_entry=entry)
        self.entry = entry
        self.point = (int(entry.data[CONF_POINT_ID]), int(entry.data[CONF_POINT_TYPE]))
        self.lat = float(entry.data[CONF_POINT_LAT])
        self.lon = float(entry.data[CONF_POINT_LON])
        self.plane = plane_from_entry(entry)
        self._snapshot = ForecastSnapshot()
        self._series: dict[str, list[tuple[datetime, float | None]]] = {}
        self._warned_run: datetime | None = None
        self.issues = IssueReporter(hass, entry.entry_id)

    async def _async_update_data(self) -> ForecastSnapshot:
        try:
            snap = await self._async_fetch()
        except UpdateFailed as err:
            self.issues.failure("forecast_unreachable", {"error": str(err)})
            raise
        self.issues.clear("forecast_unreachable")
        return snap

    def _account(self, snap: ForecastSnapshot, requests: int, nbytes: int) -> None:
        today = dt_util.start_of_local_day()
        if snap.traffic_day != today:
            snap.traffic_day = today
            snap.requests_today = 0
            snap.bytes_today = 0
        snap.requests_today += requests
        snap.bytes_today += nbytes
        snap.last_requests = requests
        snap.last_bytes = nbytes

    @staticmethod
    def _usable(snap: ForecastSnapshot, now: datetime) -> bool:
        if snap.run is None:
            return False
        run = snap.run if snap.run.tzinfo else snap.run.replace(tzinfo=timezone.utc)
        return now - run <= _FORECAST_MAX_AGE

    def _compute(self, snap: ForecastSnapshot) -> None:
        """The plane output from the kept series (also after a plane change)."""
        ghi = dict(self._series.get(PARAM_GLOBAL, []))
        dhi = dict(self._series.get(PARAM_DIFFUSE, []))
        temp = dict(self._series.get(PARAM_TEMPERATURE, []))
        rows = [(ts, value, dhi.get(ts), temp.get(ts)) for ts, value in sorted(ghi.items())]
        snap.hours = forecast_hours(rows, self.lat, self.lon, self.plane)
        snap.diffuse_forecast = bool(dhi)

    async def _async_fetch(self) -> ForecastSnapshot:
        session = async_get_clientsession(self.hass)
        snap = self._snapshot
        now = dt_util.utcnow()
        try:
            run = await async_find_latest_run(session, now)
            if run is None:
                run = await async_catalogue_latest_run(session)
        except OgdUnavailable as err:
            if self._usable(snap, now):
                _LOGGER.debug("Run check failed, keeping run %s: %s", snap.run, err)
                return snap
            raise UpdateFailed(str(err)) from err
        self._account(snap, 1, 0)
        if run is None:
            if self._usable(snap, now):
                return snap
            raise UpdateFailed("MeteoSwiss has published no local forecast run for the last hours")
        if run == snap.run and snap.hours:
            return snap
        try:
            data = await async_fetch_forecast(session, run, self.point, HOURLY_PARAMS)
        except OgdUnavailable as err:
            if self._usable(snap, now):
                if self._warned_run != run:
                    self._warned_run = run
                    _LOGGER.warning("Forecast run %s not read, keeping the previous one: %s", run, err)
                return snap
            raise UpdateFailed(str(err)) from err
        except OgdError as err:
            raise UpdateFailed(str(err)) from err
        if PARAM_GLOBAL not in data.series:
            if self._usable(snap, now):
                return snap
            raise UpdateFailed(f"run {run:%Y-%m-%d %H:%M} has no global radiation for this point")
        self._account(snap, data.stats.requests, data.stats.bytes)
        self._series = data.series
        snap.run = run
        snap.fetched = now
        snap.missing = data.missing
        self._compute(snap)
        if data.missing:
            _LOGGER.debug("Run %s: parameters not read: %s", run, ", ".join(data.missing))
        return snap
