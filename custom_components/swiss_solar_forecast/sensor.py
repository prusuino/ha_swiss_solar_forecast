"""Sensors: the energy of today, tomorrow and the rest of today, the power
of the current and the next hour, today's peak, and diagnostics about the
run and the traffic."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SolarForecastConfigEntry
from .const import ATTRIBUTION, DOMAIN
from .coordinator import ForecastSnapshot, SolarForecastCoordinator
from .device import device_info, entry_suffix

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
    "run": (SensorDeviceClass.TIMESTAMP, None, None, False, EntityCategory.DIAGNOSTIC),
    "requests_today": (None, None, SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
    "bytes_today": (SensorDeviceClass.DATA_SIZE, "B", SensorStateClass.TOTAL, False, EntityCategory.DIAGNOSTIC),
}
_TICK = timedelta(minutes=5)  # the time-dependent values (now, remaining) move between runs


async def async_setup_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = [SolarForecastSensor(coordinator, entry, key) for key in _META]
    async_add_entities(entities)

    @callback
    def _tick(_now: datetime) -> None:
        for entity in entities:
            if entity.enabled:
                entity.async_write_ha_state()

    entry.async_on_unload(async_track_time_interval(hass, _tick, _TICK))


class SolarForecastSensor(CoordinatorEntity[SolarForecastCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: SolarForecastCoordinator, entry: SolarForecastConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        device_class, unit, state_class, enabled, category = _META[key]
        if device_class is not None:
            self._attr_device_class = device_class
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if state_class is not None:
            self._attr_state_class = state_class
        if category is not None:
            self._attr_entity_category = category
        self._attr_entity_registry_enabled_default = enabled
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # the id suggested on first creation: keeps two planes apart, stays stable on renames
        self.entity_id = f"sensor.{DOMAIN}_{entry_suffix(entry)}_{key}"
        self._attr_device_info = device_info(entry)
        if key in ("energy_today", "energy_tomorrow", "energy_day_after_tomorrow"):
            self._attr_suggested_display_precision = 2
        elif key in ("power_now", "power_next_hour", "peak_power_today", "peak_power_tomorrow"):
            self._attr_suggested_display_precision = 0

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None and bool(self.coordinator.data.hours)

    def _day(self, offset: int):
        return (dt_util.now() + timedelta(days=offset)).date()

    @property
    def native_value(self) -> Any:
        snap: ForecastSnapshot = self.coordinator.data
        now = dt_util.utcnow()
        key = self._key
        if key == "energy_today":
            return _kwh(snap.day_energy_wh(self._day(0)))
        if key == "energy_tomorrow":
            return _kwh(snap.day_energy_wh(self._day(1)))
        if key == "energy_day_after_tomorrow":
            return _kwh(snap.day_energy_wh(self._day(2)))
        if key == "energy_remaining_today":
            return _kwh(snap.remaining_today_wh(now))
        if key == "power_now":
            hour = snap.hour_at(now)
            return round(hour.power_kw * 1000) if hour else None
        if key == "power_next_hour":
            hour = snap.hour_at(now + timedelta(hours=1))
            return round(hour.power_kw * 1000) if hour else None
        if key == "peak_power_today":
            peak = snap.peak(self._day(0))
            return round(peak.power_kw * 1000) if peak else None
        if key == "peak_power_tomorrow":
            peak = snap.peak(self._day(1))
            return round(peak.power_kw * 1000) if peak else None
        if key == "peak_time_today":
            peak = snap.peak(self._day(0))
            return peak.start + timedelta(minutes=30) if peak and peak.power_kw > 0 else None
        if key == "run":
            return snap.run
        if key == "requests_today":
            return snap.requests_today
        if key == "bytes_today":
            return snap.bytes_today
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snap: ForecastSnapshot = self.coordinator.data
        if self._key == "energy_today":
            return {
                "hourly": {h.start.isoformat(): round(h.power_kw * 1000) for h in snap.hours},
                "daily": _daily(snap),
                "diffuse_from_forecast": snap.diffuse_forecast,
            }
        if self._key == "run":
            return {"fetched": snap.fetched.isoformat() if snap.fetched else None, "missing": snap.missing}
        return None


def _kwh(wh: float | None) -> float | None:
    return None if wh is None else round(wh / 1000.0, 3)


def _daily(snap: ForecastSnapshot) -> dict[str, float]:
    out: dict[str, float] = {}
    for h in snap.hours:
        day = dt_util.as_local(h.start).date().isoformat()
        out[day] = out.get(day, 0.0) + h.energy_wh
    return {day: round(wh / 1000.0, 2) for day, wh in out.items()}
