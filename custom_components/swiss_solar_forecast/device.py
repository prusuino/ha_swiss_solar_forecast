"""Device info and entity-id helpers shared by the platforms."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import CONF_ADDRESS, CONF_PLANT_NAME, CONF_POINT_NAME, DOCS_URL, DOMAIN, VERSION
from .coordinator import planes_from_entry


def entry_suffix(entry: ConfigEntry) -> str:
    """The last four characters of the entry id: keeps the suggested entity
    ids of two entries (two plants) apart. Only the id suggested on first
    creation is affected; existing entities keep theirs."""
    return entry.entry_id[-4:].lower()


def plant_name(entry: ConfigEntry) -> str:
    return entry.data.get(CONF_PLANT_NAME) or entry.title or entry.data.get(CONF_POINT_NAME) or "Solar"


def device_info(entry: ConfigEntry) -> DeviceInfo:
    planes = planes_from_entry(entry)
    kwp = sum(p.plane.kwp for p in planes)
    if len(planes) == 1:
        p = planes[0].plane
        model = f"{kwp:g} kWp · {p.azimuth:.0f}° / {p.tilt:.0f}°"
    else:
        model = f"{kwp:g} kWp · {len(planes)} planes"
    place = entry.data.get(CONF_ADDRESS) or entry.data.get(CONF_POINT_NAME) or ""
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
