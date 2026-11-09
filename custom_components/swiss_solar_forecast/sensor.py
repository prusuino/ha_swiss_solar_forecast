"""Sensors of the plant (the planes' sum) and of every plane: energy of
today, tomorrow and the rest of today, power now and next hour, today's
peak, the cadastre's typical day, and diagnostics about the run, the
traffic, the terrain horizon and the self-calibration."""
from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, time, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SolarForecastConfigEntry
from .const import ATTRIBUTION, DOMAIN
from .coordinator import ForecastSnapshot, PlaneConfig, Series, SolarForecastCoordinator
from .device import device_info, entry_suffix
from .horizon import ATTRIBUTION_TERRAIN
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
    "run": (SensorDeviceClass.TIMESTAMP, None, None, False, EntityCategory.DIAGNOSTIC),
    "requests_today": (None, None, SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
    "bytes_today": (SensorDeviceClass.DATA_SIZE, "B", SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
    # only with the terrain horizon switched on / with a yield meter set
    "terrain_horizon": (None, "°", SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
    "calibration_factor": (None, None, SensorStateClass.MEASUREMENT, True, EntityCategory.DIAGNOSTIC),
}
_ROOF_KEYS = ("typical_day_energy", "forecast_vs_typical")
_DIAGNOSTIC_KEYS = ("run", "requests_today", "bytes_today", "terrain_horizon", "calibration_factor")
# per plane: the figures worth a sensor of their own (the rest is in the plant's attributes)
_PLANE_KEYS = ("energy_today", "energy_tomorrow", "power_now", "peak_power_today", "typical_day_energy", "forecast_vs_typical")
_PLANE_ENABLED = ("energy_today", "power_now")
_TICK = timedelta(minutes=5)  # the time-dependent values (now, remaining) move between runs


async def async_setup_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    any_roof = any(p.has_roof for p in coordinator.planes)
    optional = {"terrain_horizon": coordinator.use_horizon, "calibration_factor": coordinator.calibration is not None}
    registry = er.async_get(hass)
    for key, wanted in optional.items():
        # switched off in the options: the sensor goes, it would only stand there unavailable
        if not wanted and (entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")):
            registry.async_remove(entity_id)
    entities: list[SensorEntity] = [
        PlantSensor(coordinator, entry, key) for key in _META if (any_roof or key not in _ROOF_KEYS) and optional.get(key, True)
    ]
    if len(coordinator.planes) > 1:
        for plane in coordinator.planes:
            entities += [PlaneSensor(coordinator, entry, key, plane) for key in _PLANE_KEYS if plane.has_roof or key not in _ROOF_KEYS]
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

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None and bool(self.coordinator.data.plant.hours)

    def _days(self):
        today = dt_util.now().date()
        return today, today + timedelta(days=1), today + timedelta(days=2)


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
        if self._key == "calibration_factor":
            calibration = self.coordinator.calibration
            return None if calibration is None else round(calibration.overall, 3)
        day0, day1, day2 = self._days()
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
            return {
                # per hour of the day in local time; learned in UTC, so the labels move with the clock change
                "hourly": {
                    dt_util.as_local(datetime.combine(day0, time(hour), UTC)).strftime("%H:%M"): factor for hour, factor in calibration.factors().items()
                },
                "samples": calibration.samples,
                "last_sample": calibration.last_sample or None,
                "yield_sensor": self.coordinator.yield_sensor,
                "energy_today_uncalibrated": _kwh(snap.raw_plant.day_energy_wh(day0)),
            }
        if self._key == "run":
            return {"fetched": snap.fetched.isoformat() if snap.fetched else None, "missing": snap.missing}
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
