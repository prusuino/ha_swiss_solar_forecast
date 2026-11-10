"""The data coordinator: reads the radiation, temperature, precipitation
and wind series of the entry's forecast point whenever MeteoSwiss publishes
a new model run, and turns them into the hourly output of every plane of
the plant and of the plant as a whole.

Three things refine that output, all optional: the terrain horizon of the
place (read once from the swisstopo elevation model, it blocks the direct
beam while the sun stands behind a mountain), the snow on the modules
(followed hour by hour from precipitation, temperature and irradiance) and
the self-calibration against the plant's own energy meter (a factor per
hour of the day, learned from what was forecast and what was counted).
With the meter the coordinator also keeps the record of how well the
forecast did. All of it lives in a small store of the entry, so a restart
loses nothing.

It wakes every 15 minutes, asks with one HEAD request whether a new run
exists and reads the files only then, with range requests. The temperature
file is laid out so that a read costs many times what the others cost; its
series is kept and read again every few hours only. What a read cost is
kept for the diagnostics, so the traffic stays visible."""
from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .accuracy import Accuracy
from .calibration import Calibration, energy_wh
from .const import (
    ALBEDO_SNOW,
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_HORIZON,
    CONF_INVERTER_KW,
    CONF_KWP,
    CONF_LOSSES,
    CONF_PLANE_LABEL,
    CONF_PLANES,
    CONF_PLANT_INVERTER_KW,
    CONF_POINT_ID,
    CONF_POINT_LAT,
    CONF_POINT_LON,
    CONF_POINT_TYPE,
    CONF_ROOF_IRRADIATION,
    CONF_ROOF_MONTHLY,
    CONF_SNOW,
    CONF_TILT,
    CONF_YIELD_SENSOR,
    DEFAULT_AZIMUTH,
    DEFAULT_DAMPING,
    DEFAULT_LOSSES,
    DEFAULT_TILT,
    DOMAIN,
    HOURLY_PARAMS,
    KEPT_PARAM_HOURS,
    KEPT_PARAMS,
    PARAM_DIFFUSE,
    PARAM_GLOBAL,
    PARAM_PRECIPITATION,
    PARAM_TEMPERATURE,
    PARAM_WIND,
)
from .horizon import Horizon, HorizonError, async_fetch_horizon
from .issues import IssueReporter
from .ogd import OgdError, OgdUnavailable, async_catalogue_latest_run, async_fetch_forecast, async_find_latest_run
from .snow import COVER_SHOWN, GROUND_SNOW_MM, SnowState
from .solar import HourForecast, Plane, forecast_hours, plane_power_kw
from .sonnendach import typical_day_kwh

_LOGGER = logging.getLogger(__name__)

_CHECK_INTERVAL = timedelta(minutes=15)
# A kept run older than this is no forecast any more: the entities go unavailable.
_FORECAST_MAX_AGE = timedelta(hours=12)
# A costly series is read again when the run it came from is this old.
_KEPT_PARAM_AGE = timedelta(hours=KEPT_PARAM_HOURS)
# The terrain is read once; after a failure the next try waits, longer each time.
_HORIZON_RETRY = timedelta(hours=6)
_HORIZON_RETRY_MAX = timedelta(days=7)
_SAVE_DELAY = 10  # seconds: several changes in a row are written once
_HOUR = timedelta(hours=1)
# The meter against the snow: modules the forecast shows at least half covered
# that deliver this share of their snow-free output are free after all.
_SNOW_REFUTED_COVER = 0.5
_SNOW_REFUTED_SHARE = 0.6
STORE_VERSION = 1


def storage_key(entry_id: str) -> str:
    """Where an entry keeps its terrain horizon, its snow and what was learned from its meter."""
    return f"{DOMAIN}.{entry_id}"


@dataclass
class PlaneConfig:
    """One plane of the plant as configured, with its cadastre data if any."""

    index: int
    label: str
    plane: Plane
    roof_irradiation: float = 0.0  # kWh/m²/a on the plane, from the cadastre
    roof_monthly: dict[int, float] = field(default_factory=dict)  # month -> percent of the year
    raw: dict = field(default_factory=dict)

    @property
    def has_roof(self) -> bool:
        return self.roof_irradiation > 0 and len(self.roof_monthly) == 12

    def typical_day_wh(self, day: date) -> float | None:
        kwh = typical_day_kwh(
            self.plane.kwp, self.roof_irradiation, self.roof_monthly, day.month, monthrange(day.year, day.month)[1], self.plane.losses, self.plane.damping
        )
        return None if kwh is None else kwh * 1000.0


def _f(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def plane_from_dict(raw: dict) -> Plane:
    return Plane(
        kwp=_f(raw.get(CONF_KWP), 0.0),
        azimuth=_f(raw.get(CONF_AZIMUTH), DEFAULT_AZIMUTH),
        tilt=_f(raw.get(CONF_TILT), DEFAULT_TILT),
        losses=_f(raw.get(CONF_LOSSES), DEFAULT_LOSSES),
        inverter_kw=_f(raw.get(CONF_INVERTER_KW), 0.0),
        damping=_f(raw.get(CONF_DAMPING), DEFAULT_DAMPING),
    )


def planes_from_entry(entry: ConfigEntry) -> list[PlaneConfig]:
    """The planes as configured: the options' list wins over the data's."""
    raw_planes = entry.options.get(CONF_PLANES) or entry.data.get(CONF_PLANES) or []
    out: list[PlaneConfig] = []
    for index, raw in enumerate(raw_planes):
        if not isinstance(raw, dict):
            continue
        monthly: dict[int, float] = {}
        for k, v in (raw.get(CONF_ROOF_MONTHLY) or {}).items():
            try:
                monthly[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        out.append(
            PlaneConfig(
                index=index,
                label=str(raw.get(CONF_PLANE_LABEL) or f"Plane {index + 1}"),
                plane=plane_from_dict(raw),
                roof_irradiation=_f(raw.get(CONF_ROOF_IRRADIATION), 0.0),
                roof_monthly=monthly,
                raw=raw,
            )
        )
    return out


@dataclass
class Series:
    """The hourly output of one plane, or of the plant."""

    hours: list[HourForecast] = field(default_factory=list)

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

    def daily_kwh(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in self.hours:
            day = dt_util.as_local(h.start).date().isoformat()
            out[day] = out.get(day, 0.0) + h.energy_wh
        return {day: round(wh / 1000.0, 2) for day, wh in out.items()}


@dataclass
class ForecastSnapshot:
    """What the entities read."""

    run: datetime | None = None
    fetched: datetime | None = None
    plant: Series = field(default_factory=Series)
    planes: list[Series] = field(default_factory=list)
    raw_plant: Series = field(default_factory=Series)  # the plant before the self-calibration
    missing: list[str] = field(default_factory=list)
    diffuse_forecast: bool = False  # whether the diffuse share came from MeteoSwiss or was estimated
    snow_known: bool = False  # whether the snow could be followed (switched on, precipitation in the run)
    ground_snow: dict[datetime, float] = field(default_factory=dict)  # hour start -> mm of water on the ground
    # traffic since local midnight
    traffic_day: datetime | None = None
    requests_today: int = 0
    bytes_today: int = 0
    # what the last read of a model run cost
    last_requests: int = 0
    last_bytes: int = 0


def stretched(kept: list[tuple[datetime, float | None]], timestamps) -> list[tuple[datetime, float | None]]:
    """A series kept from an earlier run ends a few hours before the new run
    does: the hours it lacks repeat the same hour of the day before."""
    values = dict(kept)
    for ts in sorted(timestamps):
        if ts not in values and values.get(ts - timedelta(days=1)) is not None:
            values[ts] = values[ts - timedelta(days=1)]
    return sorted(values.items())


def plant_inverter_kw(entry: ConfigEntry) -> float:
    return _f(entry.options.get(CONF_PLANT_INVERTER_KW, entry.data.get(CONF_PLANT_INVERTER_KW)), 0.0)


def _local_date(when: datetime) -> date:
    return dt_util.as_local(when).date()


def _under_snow(hour: HourForecast, plane: Plane, cover: float) -> HourForecast:
    """A plane's hour with a share of its modules under snow."""
    if cover <= 0:
        return hour
    power = plane_power_kw(hour.poa, hour.temperature, plane, hour.wind, share=1.0 - cover)
    return replace(hour, power_kw=power, snow_cover=cover, snow_loss_kw=max(0.0, hour.power_kw - power))


def _sum_hours(per_plane: list[list[HourForecast]], limit_kw: float = 0.0, kwp: list[float] | None = None) -> list[HourForecast]:
    """The plant's hours: the planes' values added per hour start, clipped
    at the plant's inverter limit when one is set. The plant's snow cover
    is the planes' mean, weighted with their peak power (`kwp`)."""
    by_start: dict[datetime, list[tuple[HourForecast, float]]] = {}
    for index, hours in enumerate(per_plane):
        weight = kwp[index] if kwp and index < len(kwp) else 1.0
        for h in hours:
            by_start.setdefault(h.start, []).append((h, weight))
    out: list[HourForecast] = []
    for start in sorted(by_start):
        items = [h for h, _weight in by_start[start]]
        weights = sum(weight for _h, weight in by_start[start])
        first = items[0]
        power = sum(h.power_kw for h in items)
        free = sum(h.power_kw + h.snow_loss_kw for h in items)
        if limit_kw > 0:
            power, free = min(power, limit_kw), min(free, limit_kw)
        out.append(
            HourForecast(
                start=start,
                ghi=first.ghi,
                dhi=first.dhi,
                temperature=first.temperature,
                poa=sum(h.poa for h in items) / len(items),
                power_kw=power,
                beam_fraction=first.beam_fraction,
                wind=first.wind,
                snow_cover=sum(h.snow_cover * weight for h, weight in by_start[start]) / weights if weights > 0 else max(h.snow_cover for h in items),
                snow_loss_kw=max(0.0, free - power),
            )
        )
    return out


class SolarForecastCoordinator(DataUpdateCoordinator[ForecastSnapshot]):
    """The output forecast of one plant (one or more planes)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} forecast", update_interval=_CHECK_INTERVAL, config_entry=entry)
        self.entry = entry
        self.point = (int(entry.data[CONF_POINT_ID]), int(entry.data[CONF_POINT_TYPE]))
        self.lat = float(entry.data[CONF_POINT_LAT])
        self.lon = float(entry.data[CONF_POINT_LON])
        self.planes = planes_from_entry(entry)
        self.inverter_kw = plant_inverter_kw(entry)
        self._snapshot = ForecastSnapshot()
        self._series: dict[str, list[tuple[datetime, float | None]]] = {}
        self._series_run: dict[str, datetime] = {}  # the run every series in use was read from
        self._warned_run: datetime | None = None
        self.issues = IssueReporter(hass, entry.entry_id)
        self.use_horizon = bool(entry.options.get(CONF_HORIZON, True))
        self.yield_sensor = str(entry.options.get(CONF_YIELD_SENSOR) or "")
        self.calibration: Calibration | None = None
        self.accuracy: Accuracy | None = None
        self.use_snow = bool(entry.options.get(CONF_SNOW, True))
        self.snow: SnowState | None = None
        self._horizon: Horizon | None = None
        self._horizon_task = None
        self._horizon_retry_at: datetime | None = None
        self._horizon_failures = 0
        self._store: Store[dict] = Store(hass, STORE_VERSION, storage_key(entry.entry_id))

    @property
    def series_runs(self) -> dict[str, datetime]:
        """The run every series in use was read from (a kept one is older than the forecast's run)."""
        return dict(self._series_run)

    @property
    def horizon(self) -> Horizon | None:
        """The terrain horizon in use (None while it is not read, or switched off)."""
        return self._horizon if self.use_horizon else None

    # ---------------------------------------------------------------- store

    async def _async_setup(self) -> None:
        """What was kept for this plant: its terrain horizon, the snow on it and what was learned from its meter."""
        kept = await self._store.async_load() or {}
        horizon = Horizon.from_dict(kept.get("horizon"))
        if horizon is not None and horizon.matches(self.lat, self.lon):
            self._horizon = horizon
        if self.use_snow:
            self.snow = SnowState.from_dict(kept.get("snow"), len(self.planes))
        elif kept.get("snow"):
            self._store.async_delay_save(self._kept, _SAVE_DELAY)
        if self.yield_sensor:
            self.calibration = Calibration.from_dict(kept.get("calibration"), self._fingerprint())
            self.accuracy = Accuracy.from_dict(kept.get("accuracy"), self._fingerprint())
        elif kept.get("calibration") or kept.get("accuracy"):
            # the meter was taken away: what was learned with it goes too
            self._store.async_delay_save(self._kept, _SAVE_DELAY)

    def _kept(self) -> dict:
        return {
            "horizon": self._horizon.as_dict() if self._horizon else None,
            "calibration": self.calibration.as_dict() if self.calibration else None,
            "accuracy": self.accuracy.as_dict() if self.accuracy else None,
            "snow": self.snow.as_dict() if self.snow else None,
        }

    async def async_save(self) -> None:
        """Write what is kept now (on unload; otherwise it is written a few seconds after a change)."""
        await self._store.async_save(self._kept())

    def _fingerprint(self) -> str:
        """The meter and the figures the forecast is computed from: when one changes, what was learned no longer fits."""
        planes = ";".join(
            f"{p.plane.kwp:g},{p.plane.azimuth:g},{p.plane.tilt:g},{p.plane.losses:g},{p.plane.inverter_kw:g},{p.plane.damping:g}" for p in self.planes
        )
        return f"{self.yield_sensor}|{self.inverter_kw:g}|{planes}"

    def _recompute(self) -> None:
        """The same run computed again (new horizon, new factor, snow gone) and handed to the entities."""
        if self._series and self.last_update_success:
            self._compute(self._snapshot)
            self.async_set_updated_data(self._snapshot)

    # -------------------------------------------------------------- horizon

    def _start_horizon_read(self) -> None:
        """Read the terrain horizon in the background when it is wanted and not there yet."""
        if not self.use_horizon or self._horizon is not None or self._horizon_task is not None:
            return
        if self._horizon_retry_at is not None and dt_util.utcnow() < self._horizon_retry_at:
            return
        self._horizon_task = self.entry.async_create_background_task(self.hass, self._async_read_horizon(), f"{DOMAIN} terrain horizon")

    async def _async_read_horizon(self) -> None:
        try:
            horizon, requests, nbytes = await async_fetch_horizon(async_get_clientsession(self.hass), self.lat, self.lon, dt_util.utcnow())
        except HorizonError as err:
            self._horizon_failures += 1
            wait = min(_HORIZON_RETRY * 2 ** (self._horizon_failures - 1), _HORIZON_RETRY_MAX)
            self._horizon_retry_at = dt_util.utcnow() + wait
            _LOGGER.debug("Terrain horizon not read, next try in %s: %s", wait, err)
            return
        finally:
            self._horizon_task = None
        self._horizon = horizon
        self._horizon_failures = 0
        self._account(self._snapshot, requests, nbytes)
        self._store.async_delay_save(self._kept, _SAVE_DELAY)
        self._recompute()

    # ---------------------------------------------------------- calibration

    @callback
    def async_start_learning(self) -> CALLBACK_TYPE:
        """Read the yield meter at every full hour (when one is set); returns the stop function."""
        if self.calibration is None:
            return lambda: None
        return async_track_time_change(self.hass, self._async_meter_tick, minute=0, second=5)

    @callback
    def _async_meter_tick(self, now: datetime) -> None:
        """A full hour: the meter's increase since the last one against that hour's forecast."""
        calibration = self.calibration
        if calibration is None:
            return
        state = self.hass.states.get(self.yield_sensor)
        reading = energy_wh(state.state, state.attributes.get("unit_of_measurement")) if state is not None else None
        hour_end = dt_util.as_utc(now).replace(minute=0, second=0, microsecond=0)
        start = hour_end - _HOUR
        nominal_wh = self.total_kwp * 1000.0  # an hour at peak power
        min_wh, max_wh = max(20.0, 0.02 * nominal_wh), 1.3 * nominal_wh
        counted = calibration.increase(hour_end, reading)
        learned = calibration.meter_read(hour_end, reading, min_wh, max_wh)
        if counted is not None and 0 <= counted <= max_wh:
            if self.accuracy is not None:
                self.accuracy.measured(start, counted, _local_date)
            if self.snow is not None and self._snow_refuted(start, counted, min_wh):
                # the plant produces: its modules are free, whatever the snow model says
                self._recompute()  # follows the snow up to this hour first
                self.snow.clear_modules()
                learned = True
        self._store.async_delay_save(self._kept, _SAVE_DELAY)
        if learned:
            self._recompute()

    def _snow_refuted(self, start: datetime, counted_wh: float, min_wh: float) -> bool:
        """Whether the meter shows the modules free in an hour the forecast had them under snow."""
        hour = self._snapshot.plant.hour_at(start)
        if hour is None or hour.snow_cover < _SNOW_REFUTED_COVER:
            return False
        free_wh = (hour.power_kw + hour.snow_loss_kw) * 1000.0
        return free_wh >= min_wh and counted_wh >= _SNOW_REFUTED_SHARE * free_wh

    @property
    def total_kwp(self) -> float:
        return sum(p.plane.kwp for p in self.planes)

    def typical_day_wh(self, day: date) -> float | None:
        """The plant's long-term expectation of a day: the planes' sum, for the planes with a cadastre roof."""
        values = [p.typical_day_wh(day) for p in self.planes if p.has_roof]
        values = [v for v in values if v is not None]
        return sum(values) if values else None

    async def _async_update_data(self) -> ForecastSnapshot:
        try:
            snap = await self._async_fetch()
        except UpdateFailed as err:
            self.issues.failure("forecast_unreachable", {"error": str(err)})
            raise
        self.issues.clear("forecast_unreachable")
        self._start_horizon_read()
        return snap

    def _account(self, snap: ForecastSnapshot, requests: int, nbytes: int) -> None:
        today = dt_util.start_of_local_day()
        if snap.traffic_day != today:
            snap.traffic_day = today
            snap.requests_today = 0
            snap.bytes_today = 0
        snap.requests_today += requests
        snap.bytes_today += nbytes

    def _kept_serves(self, param: str, run: datetime, max_age: timedelta = _KEPT_PARAM_AGE) -> bool:
        """Whether the series of a costly parameter, read from an earlier run, is young enough to serve this run."""
        kept_run = self._series_run.get(param)
        return param in KEPT_PARAMS and bool(self._series.get(param)) and kept_run is not None and timedelta(0) <= run - kept_run < max_age

    @staticmethod
    def _usable(snap: ForecastSnapshot, now: datetime) -> bool:
        if snap.run is None:
            return False
        run = snap.run if snap.run.tzinfo else snap.run.replace(tzinfo=timezone.utc)
        return now - run <= _FORECAST_MAX_AGE

    def _compute(self, snap: ForecastSnapshot) -> None:
        """Every plane's output from the kept series, and the plant's sum."""
        now = dt_util.utcnow()
        ghi = dict(self._series.get(PARAM_GLOBAL, []))
        dhi = dict(self._series.get(PARAM_DIFFUSE, []))
        temp = dict(self._series.get(PARAM_TEMPERATURE, []))
        wind = dict(self._series.get(PARAM_WIND, []))  # km/h
        precipitation = dict(self._series.get(PARAM_PRECIPITATION, []))
        rows = [(ts, value, dhi.get(ts), temp.get(ts), None if wind.get(ts) is None else wind[ts] / 3.6) for ts, value in sorted(ghi.items())]
        horizon = self.horizon.elevation_at if self.horizon is not None else None
        snow = self.snow
        albedo: dict[datetime, float] | None = None
        snap.ground_snow = {}
        snap.snow_known = snow is not None and bool(precipitation)
        if snow is not None:
            # snow on the ground first: it brightens what the steep planes receive
            weather = [(ts - _HOUR, precipitation.get(ts), temp.get(ts), value, []) for ts, value in sorted(ghi.items()) if value is not None]
            snap.ground_snow = snow.ground_track(weather)
            albedo = {start: ALBEDO_SNOW for start, load in snap.ground_snow.items() if load >= GROUND_SNOW_MM}
        per_plane = [forecast_hours(rows, self.lat, self.lon, p.plane, horizon, albedo) for p in self.planes]
        if snow is not None and per_plane:
            # then the snow on the modules, which melts and slides with what each plane receives
            weather = [
                (h.start, precipitation.get(h.start + _HOUR), h.temperature, h.ghi, [hours[index].poa for hours in per_plane]) for index, h in enumerate(per_plane[0])
            ]
            covers = snow.advance(weather, [p.plane.tilt for p in self.planes], now)
            per_plane = [[_under_snow(h, p.plane, covers[h.start][number]) for h in hours] for number, (p, hours) in enumerate(zip(self.planes, per_plane))]
            self._store.async_delay_save(self._kept, _SAVE_DELAY)
        raw_plant = _sum_hours(per_plane, self.inverter_kw, [p.plane.kwp for p in self.planes])
        snap.raw_plant = Series(raw_plant)
        calibration = self.calibration
        if calibration is not None:
            # what is forecast now is what the meter will be compared with; then the learned factors apply.
            # Hours with snow on the modules are left out: the snow is no property of the plant.
            calibration.record_forecast([(h.start, h.energy_wh) for h in raw_plant], now, skip={h.start for h in raw_plant if h.snow_cover > COVER_SHOWN})
            self._store.async_delay_save(self._kept, _SAVE_DELAY)
            factors = [calibration.factor(hour) for hour in range(24)]

            def scaled(hour: HourForecast, limit_kw: float) -> HourForecast:
                factor = factors[hour.start.astimezone(UTC).hour]
                power = hour.power_kw * factor
                free = (hour.power_kw + hour.snow_loss_kw) * factor
                if limit_kw > 0:
                    power, free = min(power, limit_kw), min(free, limit_kw)
                return replace(hour, power_kw=power, snow_loss_kw=max(0.0, free - power))

            # the factors were learned against the plant behind its inverter limit, so they apply behind it too
            per_plane = [[scaled(h, p.plane.inverter_kw) for h in hours] for p, hours in zip(self.planes, per_plane)]
            plant = [scaled(h, self.inverter_kw) for h in raw_plant]
        else:
            plant = raw_plant
        if self.accuracy is not None:
            self.accuracy.record([(h.start, h.energy_wh) for h in plant], now, _local_date)
        snap.planes = [Series(hours) for hours in per_plane]
        snap.plant = Series(plant)
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
        if run == snap.run and snap.plant.hours:
            return snap
        params = tuple(p for p in HOURLY_PARAMS if not self._kept_serves(p, run))
        try:
            data = await async_fetch_forecast(session, run, self.point, params)
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
        snap.last_requests, snap.last_bytes = data.stats.requests, data.stats.bytes
        series = dict(data.series)
        runs = {param: run for param in series}
        for param in KEPT_PARAMS:
            # not read this time, or not readable: the series of an earlier run serves
            # (one that could not be read again for as long as a forecast counts as one)
            if param not in series and self._kept_serves(param, run, _FORECAST_MAX_AGE):
                series[param] = stretched(self._series[param], [ts for ts, _value in series[PARAM_GLOBAL]])
                runs[param] = self._series_run[param]
        self._series, self._series_run = series, runs
        snap.run = run
        snap.fetched = now
        snap.missing = [param for param in data.missing if param not in series]
        self._compute(snap)
        if data.missing:
            _LOGGER.debug("Run %s: parameters not read: %s", run, ", ".join(data.missing))
        return snap
