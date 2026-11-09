"""Energy dashboard: the solar production forecast of an entry, in the
shape the dashboard's "forecast production" setting reads (watt-hours per
hour, keyed by the start of the hour). The plant's planes are summed."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .coordinator import SolarForecastCoordinator


async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str) -> dict[str, dict[str, float | int]] | None:
    entry = hass.config_entries.async_get_entry(config_entry_id)
    coordinator = getattr(entry, "runtime_data", None) if entry else None
    if not isinstance(coordinator, SolarForecastCoordinator) or coordinator.data is None:
        return None
    return {"wh_hours": {h.start.isoformat(): round(h.energy_wh) for h in coordinator.data.plant.hours}}
