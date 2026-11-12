"""Sensors of the plant (the planes' sum) and of every plane: energy of
today, tomorrow and the rest of today, power now and next hour, today's
peak, the cadastre's typical day, the snow on the modules, the hours under
low stratus, and diagnostics
about the run, the traffic, the terrain horizon, the correction from a
measurement station, the self-calibration and how accurate the forecast
was."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SolarForecastConfigEntry, fog
from .const import ATTRIBUTION, DOMAIN
from .coordinator import ForecastSnapshot, PlaneConfig, Series, SolarForecastCoordinator
from .device import device_info, entry_suffix
from .horizon import ATTRIBUTION_TERRAIN
from .snow import COVER_SHOWN
from .sonnendach import ATTRIBUTION_SONNENDACH

# key: (device class, unit, state class, enabled by default, entity category)
_META: dict[str, tuple[SensorDeviceClass | None, str | None, SensorStateClass | None, bool, EntityCategory | None]] = {
    "energy_today": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, True, None),
    "energy_tomorrow": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, True, None),
    "energy_remaining_today": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, True, None),
    "energy_day_after_tomorrow": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, False, None),
    "power_now": (SensorDeviceClass.POWER, UnitOfPower.WATT, SensorStateClass.MEASUREMENT, True, None),
    "power_next_hour": (SensorDeviceClass.POWER, UnitOfPower.WATT, SensorStateClass.MEASUREMENT, True, None),
    "peak_power_today": (SensorDeviceClass.POWER, UnitOfPower.WATT, None, True, None),
    "peak_time_today": (SensorDeviceClass.TIMESTAMP, None, None, True, None),
    "peak_power_tomorrow": (SensorDeviceClass.POWER, UnitOfPower.WATT, None, False, None),
    # from the Sonnendach cadastre, only for planes chosen there
    "typical_day_energy": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, True, None),
    "forecast_vs_typical": (None, "%", SensorStateClass.MEASUREMENT, True, None),
    # today's daylight hours the forecast sees under low stratus (unknown while a run carries no cloud layers)
    "low_stratus_today": (SensorDeviceClass.DURATION, UnitOfTime.HOURS, None, True, None),
    "run": (SensorDeviceClass.TIMESTAMP, None, None, False, EntityCategory.DIAGNOSTIC),
    "requests_today": (None, None, SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
    "bytes_today": (SensorDeviceClass.DATA_SIZE, "B", SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
    # only with the snow model / the terrain horizon / the station correction switched on / with a yield meter set
    "snow_cover": (None, "%", SensorStateClass.MEASUREMENT, True, None),
    "terrain_horizon": (None, "°", SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
    "station_correction": (None, "%", SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
    "calibration_factor": (None, None, SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
    "accuracy_7_days": (None, "%", SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
    "accuracy_30_days": (None, "%", SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
}
_ROOF_KEYS = ("typical_day_energy", "forecast_vs_typical")
_ACCURACY_DAYS = {"accuracy_7_days": 7, "accuracy_30_days": 30}
# per plane: the figures worth a sensor of their own (the rest is in the plant's attributes)
_PLANE_KEYS = ("energy_today", "energy_tomorrow", "power_now", "peak_power_today", "typical_day_energy", "forecast_vs_typical")
_PLANE_ENABLED = ("energy_today", "power_now")
_TICK = timedelta(minutes=5)  # the time-dependent values (now, remaining) move between runs


async def async_setup_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    any_roof = any(p.has_roof for p in coordinator.planes)
    optional = {
        "snow_cover": coordinator.use_snow,
        "terrain_horizon": coordinator.use_horizon,
        "station_correction": coordinator.use_station,
        "calibration_factor": coordinator.calibration is not None,
        "accuracy_7_days": coordinator.accuracy is not None,
        "accuracy_30_days": coordinator.accuracy is not None,
    }
    entities: list[SensorEntity] = [
        PlantSensor(coordinator, entry, key) for key in _META if (any_roof or key not in _ROOF_KEYS) and optional.get(key, True)
    ]
    if len(coordinator.planes) > 1:
        for plane in coordinator.planes:
            entities += [PlaneSensor(coordinator, entry, key, plane) for key in _PLANE_KEYS if plane.has_roof or key not in _ROOF_KEYS]
    # What the entry no longer provides leaves the registry, it would only
    # stand there unavailable: a sensor switched off in the options, the
    # sensors of a plane that was removed, the per-plane sensors of a plant
    # left with one plane, the cadastre's yardstick of a roof now entered
    # by hand.
    provided = {entity.unique_id for entity in entities}
    registry = er.async_get(hass)
    for known in er.async_entries_for_config_entry(registry, entry.entry_id):
        if known.domain == "sensor" and known.unique_id not in provided:
            registry.async_remove(known.entity_id)
    async_add_entities(entities)

    @callback
    def _tick(_now: datetime) -> None:
        for entity in entities:
            if entity.enabled:
                entity.async_write_ha_state()

    entry.async_on_unload(async_track_time_interval(hass, _tick, _TICK))


def _kwh(wh: float | None) -> float | None:
    return None if wh is None else round(wh / 1000.0, 3)


def _value(key: str, series: Series, typical_wh, day0, day1, day2, now: datetime) -> Any:
    """The figure of a key for a series (the plant's or a plane's)."""
    if key == "energy_today":
        return _kwh(series.day_energy_wh(day0))
    if key == "energy_tomorrow":
        return _kwh(series.day_energy_wh(day1))
    if key == "energy_day_after_tomorrow":
        return _kwh(series.day_energy_wh(day2))
    if key == "energy_remaining_today":
        return _kwh(series.remaining_today_wh(now))
    if key == "power_now":
        hour = series.hour_at(now)
        return round(hour.power_kw * 1000) if hour else None
    if key == "power_next_hour":
        hour = series.hour_at(now + timedelta(hours=1))
        return round(hour.power_kw * 1000) if hour else None
    if key == "peak_power_today":
        peak = series.peak(day0)
        return round(peak.power_kw * 1000) if peak else None
    if key == "peak_power_tomorrow":
        peak = series.peak(day1)
        return round(peak.power_kw * 1000) if peak else None
    if key == "peak_time_today":
        peak = series.peak(day0)
        return peak.start + timedelta(minutes=30) if peak and peak.power_kw > 0 else None
    if key == "typical_day_energy":
        return _kwh(typical_wh(day0))
    if key == "forecast_vs_typical":
        typical = typical_wh(day0)
        today = series.day_energy_wh(day0)
        return round(today / typical * 100) if typical and today is not None else None
    return None


class _Base(CoordinatorEntity[SolarForecastCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: SolarForecastCoordinator, entry: SolarForecastConfigEntry, key: str, suffix: str = "") -> None:
        super().__init__(coordinator)
        self._key = key
        self._entry = entry
        device_class, unit, state_class, enabled, category = _META[key]
        if device_class is not None:
            self._attr_device_class = device_class
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if state_class is not None:
            self._attr_state_class = state_class
        if category is not None:
            self._attr_entity_category = category
        if key in _ROOF_KEYS:
            self._attr_attribution = ATTRIBUTION_SONNENDACH
        elif key == "terrain_horizon":
            self._attr_attribution = ATTRIBUTION_TERRAIN
        self._attr_entity_registry_enabled_default = enabled
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{suffix}{key}"
        # the id suggested on first creation: keeps two plants apart, stays stable on renames
        self.entity_id = f"sensor.{DOMAIN}_{entry_suffix(entry)}_{suffix}{key}"
        self._attr_device_info = device_info(entry)
        if key in ("energy_today", "energy_tomorrow", "energy_day_after_tomorrow", "energy_remaining_today", "typical_day_energy"):
            self._attr_suggested_display_precision = 2
        elif unit == UnitOfPower.WATT:
            self._attr_suggested_display_precision = 0
        elif key == "terrain_horizon":
            self._attr_suggested_display_precision = 1
        elif key == "calibration_factor":
            self._attr_suggested_display_precision = 2
        elif key in _ACCURACY_DAYS:
            self._attr_suggested_display_precision = 1
        elif key in ("snow_cover", "station_correction", "low_stratus_today"):
            self._attr_suggested_display_precision = 0

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None and bool(self.coordinator.data.plant.hours)

    def _days(self):
        today = dt_util.now().date()
        return today, today + timedelta(days=1), today + timedelta(days=2)


def _daylight(snap: ForecastSnapshot) -> list[datetime]:
    """The starts of the hours in which the forecast has light."""
    return [hour.start for hour in snap.raw_plant.hours if (hour.ghi or 0) > 0]


class PlantSensor(_Base):
    """The plant: the planes' sum."""

    @property
    def native_value(self) -> Any:
        snap: ForecastSnapshot = self.coordinator.data
        if self._key == "run":
            return snap.run
        if self._key == "requests_today":
            return snap.requests_today
        if self._key == "bytes_today":
            return snap.bytes_today
        if self._key == "terrain_horizon":
            horizon = self.coordinator.horizon
            return None if horizon is None else round(horizon.highest[0], 1)
        if self._key == "station_correction":
            # what the station measured in the last hour, in percent of what was forecast for it
            found = self.coordinator.correction
            return None if found is None else round(found.ratio * 100)
        if self._key == "calibration_factor":
            calibration = self.coordinator.calibration
            return None if calibration is None else round(calibration.overall, 3)
        day0, day1, day2 = self._days()
        if self._key in _ACCURACY_DAYS:
            accuracy = self.coordinator.accuracy
            return None if accuracy is None else accuracy.figures(day0, _ACCURACY_DAYS[self._key])["day_ahead_accuracy"]
        if self._key == "snow_cover":
            hour = snap.plant.hour_at(dt_util.utcnow())
            return round(hour.snow_cover * 100) if hour and snap.snow_known else None
        if self._key == "low_stratus_today":
            return len(fog.day_hours(snap.stratus, _daylight(snap), day0, dt_util.as_local)) if snap.stratus_known else None
        if self._key == "forecast_vs_typical":
            # like against like: only the planes with a cadastre roof have an expectation
            typical = self.coordinator.typical_day_wh(day0)
            today = [series.day_energy_wh(day0) for p, series in zip(self.coordinator.planes, snap.planes) if p.has_roof]
            today = [t for t in today if t is not None]
            return round(sum(today) / typical * 100) if typical and today else None
        return _value(self._key, snap.plant, self.coordinator.typical_day_wh, day0, day1, day2, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snap: ForecastSnapshot = self.coordinator.data
        if self._key == "energy_today":
            day0 = self._days()[0]
            return {
                "hourly": {h.start.isoformat(): round(h.power_kw * 1000) for h in snap.plant.hours},
                "daily": snap.plant.daily_kwh(),
                "planes": {p.label: _kwh(series.day_energy_wh(day0)) for p, series in zip(self.coordinator.planes, snap.planes)},
                "kwp": round(self.coordinator.total_kwp, 2),
                "diffuse_from_forecast": snap.diffuse_forecast,
                "terrain_horizon": self.coordinator.horizon is not None,
                "calibrated": self.coordinator.calibration is not None and self.coordinator.calibration.samples > 0,
                # today's hours that are over and count with what the yield meter counted in them
                "measured_hours": sum(1 for start in snap.plant.measured if dt_util.as_local(start).date() == day0),
                "station_correction": self.coordinator.correction is not None,
                # whether a factor learned for a kind of sky is at work on the hours further ahead
                "sky_correction": self.coordinator.sky is not None and self.coordinator.sky.in_force and self.coordinator.station_trust > 0,
            }
        if self._key == "terrain_horizon":
            horizon = self.coordinator.horizon
            if horizon is None:
                return None
            return {
                "highest_azimuth": horizon.highest[1],
                "site_height": horizon.site_height,
                "profile": horizon.profile(),
                "fetched": horizon.fetched or None,
            }
        if self._key == "calibration_factor":
            calibration = self.coordinator.calibration
            if calibration is None:
                return None
            day0 = self._days()[0]
            learned = calibration.factors()
            today = {}
            for hour in snap.raw_plant.hours:
                local = dt_util.as_local(hour.start)
                if local.date() != day0 or (hour.ghi or 0) <= 0:
                    continue
                if hour.start in snap.stratus:
                    if calibration.stratus_learned:
                        today[local.strftime("%H:%M")] = round(calibration.stratus_factor, 3)
                elif (key := calibration.bin_of(hour.start)) in learned:
                    today[local.strftime("%H:%M")] = learned[key]
            return {
                # today's hours whose part of the sky has evidence of its own (the others take the overall factor),
                # and the hours under low stratus once such hours were learned
                "hourly": today,
                # all that was learned, by the sun's bearing and height in degrees (the lower edges of each part)
                "sun_position": learned,
                # the factor of the hours under low stratus and how many of them stand behind it (older ones fade)
                "low_stratus": round(calibration.stratus_factor, 3) if calibration.stratus_learned else None,
                "low_stratus_samples": round(calibration.stratus[2], 1),
                "samples": calibration.samples,
                "last_sample": calibration.last_sample or None,
                "yield_sensor": self.coordinator.yield_sensor,
                "energy_today_uncalibrated": _kwh(snap.raw_plant.day_energy_wh(day0)),
            }
        if self._key == "station_correction":
            station = self.coordinator.station
            found = self.coordinator.correction
            sky = self.coordinator.sky
            now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            return {
                "station": station.get("name") or None,
                "distance_km": station.get("distance_km"),
                "measured_w_m2": None if found is None else found.measured,
                "forecast_w_m2": None if found is None else found.expected,
                "measured_at": None if found is None else found.measured_at.isoformat(),
                # the share of the difference that is applied to the current hour
                "weight": None if found is None else round(found.weight(now), 2),
                # what was learned for the hours further ahead: the factor on the forecast radiation under a clear,
                # a mixed and a grey sky (none while a sky has too few hours), and the hours behind each (older ones fade)
                "sky_factors": None if sky is None else sky.factors(),
                "sky_hours": None if sky is None else sky.hours(),
                "sky_samples": None if sky is None else sky.samples,
                "sky_last_sample": None if sky is None else sky.last_sample or None,
            }
        if self._key in _ACCURACY_DAYS:
            accuracy = self.coordinator.accuracy
            if accuracy is None:
                return None
            figures = accuracy.figures(self._days()[0], _ACCURACY_DAYS[self._key])
            del figures["day_ahead_accuracy"]  # the state
            return figures
        if self._key == "low_stratus_today":
            if not snap.stratus_known:
                return None
            day0, day1, _day2 = self._days()
            lit = _daylight(snap)
            clears = fog.clears_at(snap.stratus, lit, day0, dt_util.as_local)
            return {
                # the hours of today under low stratus, by their start in local time
                "hours": [dt_util.as_local(start).strftime("%H:%M") for start in fog.day_hours(snap.stratus, lit, day0, dt_util.as_local)],
                # when the lid is expected to open: the end of today's last such hour, if daylight follows it
                "clears_at": clears.isoformat() if clears else None,
                "daylight_hours": sum(1 for start in lit if dt_util.as_local(start).date() == day0),
                "tomorrow": len(fog.day_hours(snap.stratus, lit, day1, dt_util.as_local)),
            }
        if self._key == "snow_cover":
            now = dt_util.utcnow()
            hour = snap.plant.hour_at(now)
            if hour is None or not snap.snow_known:
                return None
            day0 = self._days()[0]
            ground = snap.ground_snow.get(hour.start)
            covers = {p.label: series.hour_at(now) for p, series in zip(self.coordinator.planes, snap.planes)}
            # when the modules are expected to be free again: the first hour without snow on them
            free = next((h.start for h in snap.plant.hours if h.start > hour.start and h.snow_cover < COVER_SHOWN), None)
            return {
                "planes": {label: round(h.snow_cover * 100) for label, h in covers.items() if h is not None},
                "ground_snow_mm": None if ground is None else round(ground, 1),
                "snow_free_from": free.isoformat() if free and hour.snow_cover >= COVER_SHOWN else None,
                "energy_lost_today": _kwh(sum(h.snow_loss_kw * 1000.0 for h in snap.plant.hours if dt_util.as_local(h.start).date() == day0)),
            }
        if self._key == "run":
            return {
                "fetched": snap.fetched.isoformat() if snap.fetched else None,
                "missing": snap.missing,
                "requests": snap.last_requests,
                "bytes": snap.last_bytes,
            }
        if self._key == "typical_day_energy":
            day0, day1, _day2 = self._days()
            typical = self.coordinator.typical_day_wh(day0)
            return {
                "typical_tomorrow": _kwh(self.coordinator.typical_day_wh(day1)),
                "typical_month_total": _kwh((typical or 0) * monthrange(day0.year, day0.month)[1]) if typical else None,
                "planes": {p.label: _kwh(p.typical_day_wh(day0)) for p in self.coordinator.planes if p.has_roof},
            }
        return None


class PlaneSensor(_Base):
    """One plane of a plant with several."""

    def __init__(self, coordinator: SolarForecastCoordinator, entry: SolarForecastConfigEntry, key: str, plane: PlaneConfig) -> None:
        super().__init__(coordinator, entry, key, suffix=f"plane{plane.index + 1}_")
        self._plane = plane
        self._attr_translation_key = f"plane_{key}"
        self._attr_translation_placeholders = {"plane": plane.label}
        self._attr_entity_registry_enabled_default = key in _PLANE_ENABLED

    @property
    def native_value(self) -> Any:
        snap: ForecastSnapshot = self.coordinator.data
        if self._plane.index >= len(snap.planes):
            return None
        day0, day1, day2 = self._days()
        return _value(self._key, snap.planes[self._plane.index], self._plane.typical_day_wh, day0, day1, day2, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._plane.raw
        if self._key == "energy_today":
            return {
                "kwp": self._plane.plane.kwp,
                "azimuth": self._plane.plane.azimuth,
                "tilt": self._plane.plane.tilt,
                "roof_area_m2": raw.get("roof_area"),
                "roof_suitability_class": raw.get("roof_class"),
            }
        return None
