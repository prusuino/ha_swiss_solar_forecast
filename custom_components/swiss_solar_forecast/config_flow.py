"""Config flow: the place (Home Assistant location, a marker on the map or
a place/postal-code search), then the forecast point nearest to it and the
module plane. Configure changes the plane; Reconfigure moves the place."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_INVERTER_KW,
    CONF_KWP,
    CONF_LOSSES,
    CONF_PLANT_NAME,
    CONF_POINT_HEIGHT,
    CONF_POINT_ID,
    CONF_POINT_LAT,
    CONF_POINT_LON,
    CONF_POINT_NAME,
    CONF_POINT_TYPE,
    CONF_TILT,
    DEFAULT_AZIMUTH,
    DEFAULT_DAMPING,
    DEFAULT_LOSSES,
    DEFAULT_TILT,
    DOMAIN,
    POINT_TYPE_MOUNTAIN,
    POINT_TYPE_POSTAL,
    POINT_TYPE_STATION,
)
from .ogd import OgdError, Point, async_fetch_points, nearest

_LOGGER = logging.getLogger(__name__)

CONF_LOCATION = "location"
CONF_SEARCH = "search"
CONF_POINT = "point"
_MAX_MATCHES = 12
_NEAREST_POINTS = 5
PLANE_KEYS = (CONF_KWP, CONF_AZIMUTH, CONF_TILT, CONF_LOSSES, CONF_INVERTER_KW, CONF_DAMPING)


def _point_label(distance: float, point: Point) -> str:
    kind = {POINT_TYPE_STATION: "station", POINT_TYPE_POSTAL: "", POINT_TYPE_MOUNTAIN: "mountain"}.get(point.point_type, "")
    name = f"{point.postal_code} {point.name}" if point.postal_code else point.name
    height = f", {point.height:.0f} m" if point.height is not None else ""
    kind_text = f" ({kind})" if kind else ""
    return f"{name}{kind_text}{height} – {distance:.1f} km"


def _point_value(point: Point) -> str:
    return f"{point.point_type}:{point.point_id}"


def search_points(points: list[Point], text: str) -> list[Point]:
    """The points whose postal code or name matches the text: postal code
    prefix first, then names starting with the text, then names containing
    it. '8001 Zürich' style works too; the postal code alone decides then."""
    q = " ".join(text.split()).lower()
    if not q:
        return []
    first = q.split(" ", 1)[0]
    plz = first if first.isdigit() else ""
    name_q = q[len(first):].strip() if plz else q
    tiers: tuple[list[Point], list[Point], list[Point]] = ([], [], [])
    for p in points:
        name = (p.name or "").lower()
        code = str(p.postal_code or "")
        if plz:
            if code.startswith(plz) and (not name_q or name_q in name):
                tiers[0].append(p)
            continue
        if name.startswith(q):
            tiers[1].append(p)
        elif q in name:
            tiers[2].append(p)
    out = [p for tier in tiers for p in sorted(tier, key=lambda p: (p.point_type != POINT_TYPE_POSTAL, str(p.postal_code or ""), p.name or ""))]
    if plz and not out and len(plz) >= 3:
        target = int(plz.ljust(4, "0")[:4])
        near = [p for p in points if str(p.postal_code or "").isdigit() and str(p.postal_code)[:2] == plz[:2] and (not name_q or name_q in (p.name or "").lower())]
        near.sort(key=lambda p: (abs(int(p.postal_code) - target), p.name or ""))
        out = near
    return out[:_MAX_MATCHES]


def plane_schema(current: dict[str, Any] | None = None, with_name: bool = True) -> dict[Any, Any]:
    """The plane fields, with the current values as defaults."""
    cur = current or {}
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_PLANT_NAME, default=cur.get(CONF_PLANT_NAME, "Solar"))] = selector.TextSelector()
    fields.update(
        {
            vol.Required(CONF_KWP, default=cur.get(CONF_KWP, 5.0)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.1, max=1000, step=0.01, unit_of_measurement="kWp", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_AZIMUTH, default=cur.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=360, step=1, unit_of_measurement="°", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_TILT, default=cur.get(CONF_TILT, DEFAULT_TILT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=90, step=1, unit_of_measurement="°", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_LOSSES, default=cur.get(CONF_LOSSES, DEFAULT_LOSSES)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=50, step=0.5, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_INVERTER_KW, default=cur.get(CONF_INVERTER_KW, 0.0)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=1000, step=0.01, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DAMPING, default=cur.get(CONF_DAMPING, DEFAULT_DAMPING)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.2, max=2.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
        }
    )
    return fields


def _plane_values(user_input: dict[str, Any]) -> dict[str, float]:
    return {key: float(user_input[key]) for key in PLANE_KEYS if key in user_input}


class SwissSolarForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._lat: float | None = None
        self._lon: float | None = None
        self._all_points: list[Point] = []
        self._points: list[tuple[float, Point]] = []
        self._matches: list[Point] = []
        self._plane: dict[str, Any] = {}
        self._entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SwissSolarForecastOptionsFlow()

    # ------------------------------------------------------------ shared

    async def _async_location_submitted(self, user_input: dict[str, Any], search_step: str, place_step: str) -> tuple[ConfigFlowResult | None, dict[str, str]]:
        errors: dict[str, str] = {}
        loc = user_input[CONF_LOCATION]
        self._lat, self._lon = float(loc["latitude"]), float(loc["longitude"])
        search = str(user_input.get(CONF_SEARCH) or "").strip()
        try:
            self._all_points = await async_fetch_points(async_get_clientsession(self.hass))
        except OgdError as err:
            _LOGGER.debug("Point list not readable: %s", err)
            errors["base"] = "cannot_connect"
            return None, errors
        if search:
            self._matches = search_points(self._all_points, search)
            if not self._matches:
                errors[CONF_SEARCH] = "no_match"
                return None, errors
            return await getattr(self, f"async_step_{search_step}")(), errors
        self._points = nearest(self._all_points, self._lat, self._lon, _NEAREST_POINTS)
        return await getattr(self, f"async_step_{place_step}")(), errors

    def _location_schema(self, lat: float, lon: float, with_plane: bool) -> vol.Schema:
        fields: dict[Any, Any] = {
            vol.Required(CONF_LOCATION, default={"latitude": lat, "longitude": lon}): selector.LocationSelector(),
            vol.Optional(CONF_SEARCH, default=""): selector.TextSelector(),
        }
        if with_plane:
            fields.update(plane_schema())
        return vol.Schema(fields)

    async def _async_search_form(self, step_id: str, user_input: dict[str, Any] | None, place_step: str) -> ConfigFlowResult:
        if user_input is not None:
            chosen = user_input[CONF_POINT]
            point = next(p for p in self._matches if _point_value(p) == chosen)
            self._lat, self._lon = point.lat, point.lon
            others = [(d, p) for d, p in nearest(self._all_points, point.lat, point.lon, _NEAREST_POINTS) if _point_value(p) != chosen]
            self._points = [(0.0, point)] + others[: _NEAREST_POINTS - 1]
            return await getattr(self, f"async_step_{place_step}")()
        options = [selector.SelectOptionDict(value=_point_value(p), label=_point_label(0.0, p).rsplit(" – ", 1)[0]) for p in self._matches]
        schema = vol.Schema(
            {
                vol.Required(CONF_POINT, default=options[0]["value"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def _async_place_form(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            chosen = user_input[CONF_POINT]
            point = next(p for _d, p in self._points if _point_value(p) == chosen)
            return await self._async_finish(point)
        options = [selector.SelectOptionDict(value=_point_value(p), label=_point_label(d, p)) for d, p in self._points]
        default = options[0]["value"]
        if self._entry is not None:
            current = f"{self._entry.data.get(CONF_POINT_TYPE)}:{self._entry.data.get(CONF_POINT_ID)}"
            if any(o["value"] == current for o in options):
                default = current
        schema = vol.Schema(
            {
                vol.Required(CONF_POINT, default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def _async_finish(self, point: Point) -> ConfigFlowResult:
        place = {
            CONF_POINT_TYPE: point.point_type,
            CONF_POINT_ID: point.point_id,
            CONF_POINT_NAME: point.name,
            CONF_POINT_LAT: point.lat,
            CONF_POINT_LON: point.lon,
            CONF_POINT_HEIGHT: point.height,
        }
        if self._entry is None:
            # one entry per plane: the same point with the same orientation and tilt is a duplicate
            unique_id = f"{point.point_type}-{point.point_id}-{self._plane[CONF_AZIMUTH]:.0f}-{self._plane[CONF_TILT]:.0f}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=str(self._plane[CONF_PLANT_NAME]), data={**place, **self._plane})
        azimuth = float(self._entry.options.get(CONF_AZIMUTH, self._entry.data.get(CONF_AZIMUTH, DEFAULT_AZIMUTH)))
        tilt = float(self._entry.options.get(CONF_TILT, self._entry.data.get(CONF_TILT, DEFAULT_TILT)))
        unique_id = f"{point.point_type}-{point.point_id}-{azimuth:.0f}-{tilt:.0f}"
        await self.async_set_unique_id(unique_id)
        if unique_id != self._entry.unique_id:
            self._abort_if_unique_id_configured()
        # Update only: the entry's update listener reloads it on a change.
        self.hass.config_entries.async_update_entry(self._entry, unique_id=unique_id, data={**self._entry.data, **place})
        return self.async_abort(reason="reconfigure_successful")

    # ------------------------------------------------------------- setup

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._plane = {CONF_PLANT_NAME: str(user_input.get(CONF_PLANT_NAME) or "Solar").strip() or "Solar", **_plane_values(user_input)}
            result, errors = await self._async_location_submitted(user_input, "search", "place")
            if result is not None:
                return result
        schema = self._location_schema(self.hass.config.latitude, self.hass.config.longitude, with_plane=True)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_search_form("search", user_input, "place")

    async def async_step_place(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_place_form("place", user_input)

    # ------------------------------------------------------- reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Move the entry to another place; the plane stays as set in Configure."""
        self._entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            result, errors = await self._async_location_submitted(user_input, "reconfigure_search", "reconfigure_place")
            if result is not None:
                return result
        lat = float(self._entry.data.get(CONF_POINT_LAT, self.hass.config.latitude))
        lon = float(self._entry.data.get(CONF_POINT_LON, self.hass.config.longitude))
        return self.async_show_form(step_id="reconfigure", data_schema=self._location_schema(lat, lon, with_plane=False), errors=errors)

    async def async_step_reconfigure_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_search_form("reconfigure_search", user_input, "reconfigure_place")

    async def async_step_reconfigure_place(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_place_form("reconfigure_place", user_input)


class SwissSolarForecastOptionsFlow(OptionsFlow):
    """The plane: peak power, orientation, tilt, losses, inverter limit, damping."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(title="", data={**entry.options, **_plane_values(user_input)})
        current = {key: entry.options.get(key, entry.data.get(key)) for key in PLANE_KEYS}
        current = {k: v for k, v in current.items() if v is not None}
        return self.async_show_form(step_id="init", data_schema=vol.Schema(plane_schema(current, with_name=False)))
