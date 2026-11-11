"""Config flow: the place (map marker or address), the roof planes from the
Sonnendach cadastre (tick all that carry modules) or planes entered by
hand, then the figures of every plane one after the other. The yield meter for
the self-calibration can be named right at the start. One entry is
one plant with all its planes; the nearest MeteoSwiss forecast point that
carries radiation is chosen automatically. Configure changes a plane's figures;
Reconfigure moves the plant or changes its planes."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADDRESS,
    CONF_AZIMUTH,
    CONF_DAMPING,
    CONF_HORIZON,
    CONF_KWP,
    CONF_LOSSES,
    CONF_PLANE_LABEL,
    CONF_PLANES,
    CONF_PLANT_INVERTER_KW,
    CONF_PLANT_NAME,
    CONF_POINT_HEIGHT,
    CONF_POINT_ID,
    CONF_POINT_LAT,
    CONF_POINT_LON,
    CONF_POINT_NAME,
    CONF_POINT_TYPE,
    CONF_ROOF_AREA,
    CONF_ROOF_CLASS,
    CONF_ROOF_ID,
    CONF_ROOF_IRRADIATION,
    CONF_ROOF_MONTHLY,
    CONF_SNOW,
    CONF_STATION,
    CONF_TILT,
    CONF_YIELD_SENSOR,
    DEFAULT_AZIMUTH,
    DEFAULT_DAMPING,
    DEFAULT_LOSSES,
    DEFAULT_TILT,
    DOMAIN,
)
from .ogd import OgdError, async_fetch_points, nearest_radiation_point
from .sonnendach import Address, Roof, SonnendachError, async_geocode, async_roofs_at

_LOGGER = logging.getLogger(__name__)

CONF_LOCATION = "location"
CONF_SEARCH = "search"
CONF_ROOFS = "roofs"
CONF_PLANE = "plane"
PLANT = "plant"
MANUAL = "manual"
PLANE_KEYS = (CONF_KWP, CONF_AZIMUTH, CONF_TILT, CONF_LOSSES, CONF_DAMPING)
ROOF_KEYS = (CONF_ROOF_ID, CONF_ROOF_AREA, CONF_ROOF_CLASS, CONF_ROOF_IRRADIATION, CONF_ROOF_MONTHLY)

_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_name(azimuth: float) -> str:
    return _COMPASS[int((azimuth % 360) / 22.5 + 0.5) % 16]


def roof_label(roof: Roof, lang: str) -> str:
    """'SE 130°, 23°, 85 m², 4 (good), 14 500 kWh/a'."""
    text = roof.suitability_text.get(lang) or roof.suitability_text.get("en") or ""
    cls = f"{roof.suitability} ({text})" if text else str(roof.suitability)
    return f"{compass_name(roof.azimuth)} {roof.azimuth:.0f}°, {roof.tilt:.0f}°, {roof.area:.0f} m², {cls}, {roof.yield_kwh_year:,.0f} kWh/a".replace(",", " ")


def plane_label(azimuth: float, tilt: float) -> str:
    return f"{compass_name(azimuth)} {azimuth:.0f}° / {tilt:.0f}°"


def plane_schema(current: dict[str, Any] | None = None, with_label: bool = True) -> dict[Any, Any]:
    """The plane fields, with the current (or suggested) values as defaults."""
    cur = current or {}
    fields: dict[Any, Any] = {}
    if with_label:
        fields[vol.Required(CONF_PLANE_LABEL, default=cur.get(CONF_PLANE_LABEL, ""))] = selector.TextSelector()
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
            vol.Required(CONF_DAMPING, default=cur.get(CONF_DAMPING, DEFAULT_DAMPING)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.2, max=2.0, step=0.01, mode=selector.NumberSelectorMode.BOX)
            ),
        }
    )
    return fields


def _inverter_selector() -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=10000, step=0.01, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX))


def _meter_selector() -> selector.EntitySelector:
    """The energy sensor that counts what the whole plant produces."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="energy"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "plant"


def _plane_values(user_input: dict[str, Any]) -> dict[str, float]:
    return {key: float(user_input[key]) for key in PLANE_KEYS if key in user_input}


def _roof_values(roof: Roof | None) -> dict[str, Any]:
    if roof is None:
        return {}
    return {
        CONF_ROOF_ID: roof.roof_id,
        CONF_ROOF_AREA: round(roof.area, 1),
        CONF_ROOF_CLASS: roof.suitability,
        CONF_ROOF_IRRADIATION: round(roof.irradiation_kwh_m2_year, 1),
        CONF_ROOF_MONTHLY: {str(m): round(s, 3) for m, s in roof.monthly_share.items()},
    }


class SwissSolarForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._lat: float | None = None
        self._lon: float | None = None
        self._address: str = ""
        self._addresses: list[Address] = []
        self._roofs: list[Roof] = []
        self._queue: list[Roof | None] = []  # the planes still to be entered; None = by hand
        self._planes: list[dict[str, Any]] = []
        self._name: str = "Solar"
        self._plant_inverter_kw: float = 0.0
        self._yield_sensor: str = ""
        self._entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SwissSolarForecastOptionsFlow()

    @property
    def _lang(self) -> str:
        lang = (self.hass.config.language or "en").split("-")[0]
        return lang if lang in ("de", "fr", "it", "en") else "en"

    # ------------------------------------------------------------ steps shared by setup and reconfigure

    def _location_schema(self, lat: float, lon: float, with_name: bool) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if with_name:
            fields[vol.Required(CONF_PLANT_NAME, default=self._name)] = selector.TextSelector()
        fields[vol.Required(CONF_LOCATION, default={"latitude": lat, "longitude": lon})] = selector.LocationSelector()
        fields[vol.Optional(CONF_SEARCH, default="")] = selector.TextSelector()
        if with_name:
            fields[vol.Required(CONF_PLANT_INVERTER_KW, default=self._plant_inverter_kw)] = _inverter_selector()
            # optional, and worth asking for at once: with the meter the forecast corrects itself
            fields[vol.Optional(CONF_YIELD_SENSOR, description={"suggested_value": self._yield_sensor or None})] = _meter_selector()
        return vol.Schema(fields)

    async def _async_place_submitted(self, user_input: dict[str, Any], prefix: str) -> tuple[ConfigFlowResult | None, dict[str, str]]:
        """Marker or address → the roofs there. Returns the next step, or
        None with the errors to show on the form."""
        errors: dict[str, str] = {}
        loc = user_input[CONF_LOCATION]
        self._lat, self._lon = float(loc["latitude"]), float(loc["longitude"])
        self._address = self._kept_address()
        search = str(user_input.get(CONF_SEARCH) or "").strip()
        session = async_get_clientsession(self.hass)
        if search:
            try:
                self._addresses = await async_geocode(session, search)
            except SonnendachError as err:
                _LOGGER.debug("Address search failed: %s", err)
                errors["base"] = "cannot_connect"
                return None, errors
            if not self._addresses:
                errors[CONF_SEARCH] = "no_match"
                return None, errors
            if len(self._addresses) > 1:
                return await getattr(self, f"async_step_{prefix}address")(), errors
            self._use_address(self._addresses[0])
        return await self._async_load_roofs(prefix), errors

    def _kept_address(self) -> str:
        """The address of the entry being reconfigured, for as long as the
        marker stays where it is (to about ten metres). A marker moved
        elsewhere has no address until one is searched for."""
        if self._entry is None:
            return ""
        data = self._entry.data
        try:
            moved = abs(float(data[CONF_POINT_LAT]) - self._lat) > 1e-4 or abs(float(data[CONF_POINT_LON]) - self._lon) > 1e-4
        except (KeyError, TypeError, ValueError):
            return ""
        return "" if moved else str(data.get(CONF_ADDRESS) or "")

    def _use_address(self, address: Address) -> None:
        self._address = address.label
        self._lat, self._lon = address.lat, address.lon

    async def _async_load_roofs(self, prefix: str) -> ConfigFlowResult:
        try:
            self._roofs = await async_roofs_at(async_get_clientsession(self.hass), self._lat, self._lon)
        except SonnendachError as err:
            _LOGGER.debug("Roof lookup failed, manual planes: %s", err)
            self._roofs = []
        return await getattr(self, f"async_step_{prefix}roofs")()

    async def _async_address_form(self, step_id: str, user_input: dict[str, Any] | None, prefix: str) -> ConfigFlowResult:
        if user_input is not None:
            chosen = user_input[CONF_SEARCH]
            self._use_address(next(a for a in self._addresses if a.label == chosen))
            return await self._async_load_roofs(prefix)
        options = [selector.SelectOptionDict(value=a.label, label=a.label) for a in self._addresses]
        schema = vol.Schema(
            {
                vol.Required(CONF_SEARCH, default=options[0]["value"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema)

    def _current_planes(self) -> list[dict[str, Any]]:
        if self._entry is None:
            return []
        return list(self._entry.options.get(CONF_PLANES) or self._entry.data.get(CONF_PLANES) or [])

    async def _async_roofs_form(self, step_id: str, user_input: dict[str, Any] | None, prefix: str) -> ConfigFlowResult:
        """Tick every plane that carries modules; 'by hand' adds a plane
        the cadastre does not know."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = list(user_input.get(CONF_ROOFS) or [])
            if not chosen:
                errors[CONF_ROOFS] = "pick_one"
            else:
                self._queue = [next(r for r in self._roofs if str(r.roof_id) == c) if c != MANUAL else None for c in chosen]
                self._planes = []
                return await getattr(self, f"async_step_{prefix}plane")()
        options = [selector.SelectOptionDict(value=str(r.roof_id), label=roof_label(r, self._lang)) for r in self._roofs]
        options.append(selector.SelectOptionDict(value=MANUAL, label="✎"))
        current_ids = {str(p.get(CONF_ROOF_ID)) for p in self._current_planes() if p.get(CONF_ROOF_ID) is not None}
        default = [o["value"] for o in options if o["value"] in current_ids] or ([options[0]["value"]] if self._roofs else [MANUAL])
        schema = vol.Schema(
            {
                vol.Required(CONF_ROOFS, default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST, multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(self._roofs)), "place": self._address or f"{self._lat:.4f}, {self._lon:.4f}"},
        )

    def _plane_defaults(self, roof: Roof | None) -> dict[str, Any]:
        """Suggested figures: the plane's current ones when the entry already
        has this roof (reconfigure), else the roof's, else the defaults."""
        cur: dict[str, Any] = {}
        existing = None
        for p in self._current_planes():
            if roof is not None and p.get(CONF_ROOF_ID) == roof.roof_id:
                existing = p
                break
        if existing is None and roof is None:
            manual = [p for p in self._current_planes() if p.get(CONF_ROOF_ID) is None]
            done_manual = sum(1 for p in self._planes if p.get(CONF_ROOF_ID) is None)
            if done_manual < len(manual):
                existing = manual[done_manual]
        if existing:
            cur = {k: existing[k] for k in (CONF_PLANE_LABEL, *PLANE_KEYS) if k in existing}
        if roof is not None:
            cur.setdefault(CONF_AZIMUTH, round(roof.azimuth))
            cur.setdefault(CONF_TILT, round(roof.tilt))
            cur.setdefault(CONF_KWP, roof.suggested_kwp)
            cur.setdefault(CONF_PLANE_LABEL, plane_label(roof.azimuth, roof.tilt))
        else:
            cur.setdefault(CONF_PLANE_LABEL, f"Plane {len(self._planes) + 1}")
        return cur

    async def _async_plane_form(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        roof = self._queue[len(self._planes)] if len(self._planes) < len(self._queue) else None
        if user_input is not None:
            values = _plane_values(user_input)
            label = str(user_input.get(CONF_PLANE_LABEL) or "").strip() or plane_label(values[CONF_AZIMUTH], values[CONF_TILT])
            self._planes.append({CONF_PLANE_LABEL: label, **values, **_roof_values(roof)})
            if len(self._planes) < len(self._queue):
                return await getattr(self, f"async_step_{step_id}")()
            return await self._async_finish()
        placeholders = {
            "index": str(len(self._planes) + 1),
            "total": str(len(self._queue)),
            "roof": roof_label(roof, self._lang) if roof else "—",
            "kwp_hint": f"{roof.suggested_kwp:g}" if roof else "–",
        }
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(plane_schema(self._plane_defaults(roof))), description_placeholders=placeholders)

    async def _async_finish(self) -> ConfigFlowResult:
        """The forecast point with radiation nearest to the place, then the entry."""
        try:
            points = await async_fetch_points(async_get_clientsession(self.hass))
            point = nearest_radiation_point(points, self._lat, self._lon)
        except OgdError as err:
            _LOGGER.debug("Point list not readable: %s", err)
            return self.async_abort(reason="cannot_connect")
        place = {
            CONF_POINT_TYPE: point.point_type,
            CONF_POINT_ID: point.point_id,
            CONF_POINT_NAME: point.name,
            CONF_POINT_LAT: self._lat,
            CONF_POINT_LON: self._lon,
            CONF_POINT_HEIGHT: point.height,
            CONF_ADDRESS: self._address,
            CONF_PLANT_NAME: self._name,
            CONF_PLANT_INVERTER_KW: self._plant_inverter_kw,
            CONF_PLANES: self._planes,
        }
        # one entry per inverter: the place (to about ten metres) and the plant's name
        unique_id = f"{self._lat:.4f},{self._lon:.4f}:{_slug(self._name)}"
        await self.async_set_unique_id(unique_id)
        if self._entry is None:
            self._abort_if_unique_id_configured()
            options = {CONF_YIELD_SENSOR: self._yield_sensor} if self._yield_sensor else {}
            return self.async_create_entry(title=self._name, data=place, options=options)
        if unique_id != self._entry.unique_id:
            self._abort_if_unique_id_configured()
        # Update only: the entry's update listener reloads it on a change.
        self.hass.config_entries.async_update_entry(
            self._entry,
            unique_id=unique_id,
            data={**self._entry.data, **place},
            options={k: v for k, v in self._entry.options.items() if k not in (CONF_PLANES, CONF_PLANT_INVERTER_KW)},
        )
        return self.async_abort(reason="reconfigure_successful")

    # ------------------------------------------------------------- setup

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = str(user_input.get(CONF_PLANT_NAME) or "Solar").strip() or "Solar"
            self._plant_inverter_kw = float(user_input.get(CONF_PLANT_INVERTER_KW) or 0.0)
            self._yield_sensor = str(user_input.get(CONF_YIELD_SENSOR) or "")
            result, errors = await self._async_place_submitted(user_input, "")
            if result is not None:
                return result
        schema = self._location_schema(self.hass.config.latitude, self.hass.config.longitude, with_name=True)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_address(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_address_form("address", user_input, "")

    async def async_step_roofs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_roofs_form("roofs", user_input, "")

    async def async_step_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_plane_form("plane", user_input)

    # ------------------------------------------------------- reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Another place, or other planes: marker or address, the roofs there, the figures of every plane."""
        self._entry = self._get_reconfigure_entry()
        self._name = self._entry.data.get(CONF_PLANT_NAME) or self._entry.title
        self._plant_inverter_kw = float(self._entry.options.get(CONF_PLANT_INVERTER_KW, self._entry.data.get(CONF_PLANT_INVERTER_KW, 0.0)) or 0.0)
        errors: dict[str, str] = {}
        if user_input is not None:
            result, errors = await self._async_place_submitted(user_input, "reconfigure_")
            if result is not None:
                return result
        lat = float(self._entry.data.get(CONF_POINT_LAT, self.hass.config.latitude))
        lon = float(self._entry.data.get(CONF_POINT_LON, self.hass.config.longitude))
        return self.async_show_form(step_id="reconfigure", data_schema=self._location_schema(lat, lon, with_name=False), errors=errors)

    async def async_step_reconfigure_address(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_address_form("reconfigure_address", user_input, "reconfigure_")

    async def async_step_reconfigure_roofs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_roofs_form("reconfigure_roofs", user_input, "reconfigure_")

    async def async_step_reconfigure_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_plane_form("reconfigure_plane", user_input)


class SwissSolarForecastOptionsFlow(OptionsFlow):
    """A plane's figures: pick the plane (when there are several), then
    peak power, orientation, tilt, losses, inverter limit and damping."""

    def __init__(self) -> None:
        self._index = 0

    def _planes(self) -> list[dict[str, Any]]:
        entry = self.config_entry
        return [dict(p) for p in (entry.options.get(CONF_PLANES) or entry.data.get(CONF_PLANES) or [])]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        planes = self._planes()
        if user_input is not None:
            if user_input[CONF_PLANE] == PLANT:
                return await self.async_step_plant()
            self._index = int(user_input[CONF_PLANE])
            return await self.async_step_plane()
        options = [selector.SelectOptionDict(value=PLANT, label="Σ")]
        options += [selector.SelectOptionDict(value=str(i), label=f"{p.get(CONF_PLANE_LABEL) or i + 1} · {p.get(CONF_KWP, 0):g} kWp") for i, p in enumerate(planes)]
        schema = vol.Schema(
            {vol.Required(CONF_PLANE, default="0" if planes else PLANT): selector.SelectSelector(selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST))}
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_plant(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            options = {
                **entry.options,
                CONF_PLANT_INVERTER_KW: float(user_input.get(CONF_PLANT_INVERTER_KW) or 0.0),
                CONF_HORIZON: bool(user_input.get(CONF_HORIZON, True)),
                CONF_SNOW: bool(user_input.get(CONF_SNOW, True)),
                CONF_STATION: bool(user_input.get(CONF_STATION, True)),
            }
            # an emptied field means: no meter, no self-calibration
            options.pop(CONF_YIELD_SENSOR, None)
            if user_input.get(CONF_YIELD_SENSOR):
                options[CONF_YIELD_SENSOR] = str(user_input[CONF_YIELD_SENSOR])
            return self.async_create_entry(title="", data=options)
        current = float(entry.options.get(CONF_PLANT_INVERTER_KW, entry.data.get(CONF_PLANT_INVERTER_KW, 0.0)) or 0.0)
        schema = vol.Schema(
            {
                vol.Required(CONF_PLANT_INVERTER_KW, default=current): _inverter_selector(),
                vol.Required(CONF_HORIZON, default=bool(entry.options.get(CONF_HORIZON, True))): selector.BooleanSelector(),
                vol.Required(CONF_SNOW, default=bool(entry.options.get(CONF_SNOW, True))): selector.BooleanSelector(),
                vol.Required(CONF_STATION, default=bool(entry.options.get(CONF_STATION, True))): selector.BooleanSelector(),
                vol.Optional(CONF_YIELD_SENSOR, description={"suggested_value": entry.options.get(CONF_YIELD_SENSOR)}): _meter_selector(),
            }
        )
        return self.async_show_form(step_id="plant", data_schema=schema)

    async def async_step_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        planes = self._planes()
        if not planes:
            return self.async_abort(reason="no_planes")
        current = planes[self._index]
        if user_input is not None:
            label = str(user_input.get(CONF_PLANE_LABEL) or "").strip() or current.get(CONF_PLANE_LABEL) or f"Plane {self._index + 1}"
            planes[self._index] = {**current, CONF_PLANE_LABEL: label, **_plane_values(user_input)}
            return self.async_create_entry(title="", data={**self.config_entry.options, CONF_PLANES: planes})
        defaults = {k: current[k] for k in (CONF_PLANE_LABEL, *PLANE_KEYS) if k in current}
        return self.async_show_form(
            step_id="plane", data_schema=vol.Schema(plane_schema(defaults)), description_placeholders={"plane": str(current.get(CONF_PLANE_LABEL) or self._index + 1)}
        )
