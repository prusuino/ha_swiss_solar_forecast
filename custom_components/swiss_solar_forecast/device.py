"""Device info and entity-id helpers shared by the platforms."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import CONF_ADDRESS, CONF_PLANT_NAME, CONF_POINT_NAME, CONF_ROOF_AREA, DOCS_URL, DOMAIN, VERSION
from .coordinator import plane_from_entry


def entry_suffix(entry: ConfigEntry) -> str:
    """The last four characters of the entry id: keeps the suggested entity
    ids of two entries (two planes) apart. Only the id suggested on first
    creation is affected; existing entities keep theirs."""
    return entry.entry_id[-4:].lower()


def plant_name(entry: ConfigEntry) -> str:
    return entry.data.get(CONF_PLANT_NAME) or entry.title or entry.data.get(CONF_POINT_NAME) or "Solar"


def device_info(entry: ConfigEntry) -> DeviceInfo:
    plane = plane_from_entry(entry)
    place = entry.data.get(CONF_ADDRESS) or entry.data.get(CONF_POINT_NAME) or ""
    model = f"{plane.kwp:g} kWp · {plane.azimuth:.0f}° / {plane.tilt:.0f}°"
    if entry.data.get(CONF_ROOF_AREA):
        model = f"{model} · {float(entry.data[CONF_ROOF_AREA]):.0f} m²"
    if place:
        model = f"{model} · {place}"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        sw_version=VERSION,
        name=plant_name(entry),
        manufacturer="MeteoSwiss Open Data",
        model=model,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=DOCS_URL,
    )
