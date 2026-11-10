"""Swiss Solar Forecast: the expected output of a PV plane from the
MeteoSwiss local forecast of global and diffuse radiation (open data)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_POINT_HEIGHT,
    CONF_POINT_ID,
    CONF_POINT_LAT,
    CONF_POINT_LON,
    CONF_POINT_NAME,
    CONF_POINT_TYPE,
    DOMAIN,
    RADIATION_POINT_TYPE,
)
from .coordinator import STORE_VERSION, SolarForecastCoordinator, storage_key
from .issues import async_clear_entry_issues
from .ogd import OgdError, async_fetch_points, nearest_radiation_point

type SolarForecastConfigEntry = ConfigEntry[SolarForecastCoordinator]

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def _async_leave_point_without_radiation(hass: HomeAssistant, entry: SolarForecastConfigEntry) -> None:
    """Up to 0.3.0 the setup took the nearest forecast point of any kind. A
    plant closer to a measurement station or a mountain point than to the
    centre of its postal code got a point without radiation and never
    loaded: such an entry moves to the nearest point that has it."""
    if int(entry.data[CONF_POINT_TYPE]) == RADIATION_POINT_TYPE:
        return
    try:
        points = await async_fetch_points(async_get_clientsession(hass))
        point = nearest_radiation_point(points, float(entry.data[CONF_POINT_LAT]), float(entry.data[CONF_POINT_LON]))
    except OgdError as err:
        raise ConfigEntryNotReady(f"the forecast point list could not be read: {err}") from err
    _LOGGER.info("Forecast point %s carries no radiation, using %s instead", entry.data.get(CONF_POINT_NAME), point.name)
    # no update listener yet: this change reloads nothing
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_POINT_TYPE: point.point_type,
            CONF_POINT_ID: point.point_id,
            CONF_POINT_NAME: point.name,
            CONF_POINT_HEIGHT: point.height,
        },
    )


async def async_setup_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry) -> bool:
    await _async_leave_point_without_radiation(hass, entry)
    coordinator = SolarForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_start_learning())
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SolarForecastConfigEntry) -> None:
    """A changed plane or place: reload, the entities keep their ids."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save()
        async_clear_entry_issues(hass, entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SolarForecastConfigEntry) -> None:
    """A removed plant leaves nothing behind: its terrain horizon and what the calibration learned go too."""
    await Store(hass, STORE_VERSION, storage_key(entry.entry_id)).async_remove()
