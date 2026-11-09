"""Sonnendach, the Swiss solar cadastre of the Federal Office of Energy
(SFOE): every roof plane of the country with its orientation, tilt, area,
suitability class and long-term yield, computed from the laser scan of the
buildings. Open government data, read through the federal geo API
(api3.geo.admin.ch) without a key.

Used at setup to find the roofs at a place (from the map marker or an
address) and to prefill the plane, and afterwards for the long-term
expectation of a day, against which the forecast can be read.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

from .const import USER_AGENT

_LOGGER = logging.getLogger(__name__)

API = "https://api3.geo.admin.ch/rest/services/api"
LAYER = "ch.bfe.solarenergie-eignung-daecher"
ATTRIBUTION_SONNENDACH = "Roof data: Sonnendach, Swiss Federal Office of Energy (SFOE), open data"
_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10)
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
_MIN_AREA = 5.0  # m², smaller planes are dormers and chimneys
_KWP_PER_M2 = 0.2  # a modern module field, for the suggested peak power


class SonnendachError(Exception):
    """The geo API could not be read or answered with nonsense."""


@dataclass(frozen=True)
class Address:
    label: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Roof:
    roof_id: int
    building_id: int
    azimuth: float  # compass bearing the plane faces, 0 north, 90 east, 180 south, 270 west
    tilt: float  # degrees from horizontal
    area: float  # m²
    suitability: int  # 1 (low) … 5 (top), the SFOE class
    suitability_text: dict[str, str]  # language -> text
    yield_kwh_year: float  # the SFOE's electricity yield for the whole plane, kWh/a
    irradiation_kwh_m2_year: float  # mean irradiation on the plane, kWh/m²/a
    monthly_share: dict[int, float] = field(default_factory=dict)  # month -> percent of the year's yield

    @property
    def suggested_kwp(self) -> float:
        return round(self.area * _KWP_PER_M2, 1)


def compass_from_sonnendach(value: float) -> float:
    """Sonnendach counts the orientation from south: 0 south, -90 east,
    90 west, ±180 north. A compass bearing is what the plane uses."""
    return (float(value) + 180.0) % 360.0


def _strip_html(text: str) -> str:
    out, skip = [], False
    for ch in text:
        if ch == "<":
            skip = True
        elif ch == ">":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out)


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, str]) -> dict:
    try:
        async with session.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                raise SonnendachError(f"HTTP {resp.status} from the geo API")
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        raise SonnendachError(f"{type(err).__name__} from the geo API") from err


async def async_geocode(session: aiohttp.ClientSession, text: str, limit: int = 8) -> list[Address]:
    """Addresses matching a text, with their coordinates (the federal
    address register: every street address of Switzerland)."""
    data = await _get_json(
        session,
        f"{API}/SearchServer",
        {"searchText": text, "type": "locations", "origins": "address", "sr": "4326", "limit": str(limit), "lang": "de"},
    )
    out: list[Address] = []
    for item in data.get("results", []):
        attrs = item.get("attrs", {})
        try:
            out.append(Address(label=_strip_html(str(attrs.get("label", ""))).strip(), lat=float(attrs["lat"]), lon=float(attrs["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _roof_from_attributes(attrs: dict, roof_id: int) -> Roof | None:
    try:
        area = float(attrs["flaeche"])
        months = [int(m) for m in attrs.get("monate", [])]
        values = [float(v) for v in attrs.get("monats_ertrag", [])]
        # the cadastre's monthly figures come in an unexplained unit; only their proportions are used
        total = sum(values)
        shares = [v / total * 100.0 for v in values] if total > 0 else []
        texts = str(attrs.get("klasse_text", "")).split("##")
        langs = ("de", "fr", "it", "en")
        return Roof(
            roof_id=roof_id,
            building_id=int(attrs["building_id"]),
            azimuth=compass_from_sonnendach(attrs["ausrichtung"]),
            tilt=float(attrs["neigung"]),
            area=area,
            suitability=int(attrs.get("klasse", 0)),
            suitability_text={lang: texts[i] for i, lang in enumerate(langs) if i < len(texts)},
            yield_kwh_year=float(attrs.get("stromertrag", 0.0)),
            irradiation_kwh_m2_year=float(attrs.get("mstrahlung", 0.0)),
            monthly_share={m: s for m, s in zip(months, shares)} if len(months) == len(shares) == 12 else {},
        )
    except (KeyError, TypeError, ValueError):
        return None


async def async_roofs_at(session: aiohttp.ClientSession, lat: float, lon: float) -> list[Roof]:
    """The roof planes of the building at a point, largest first. The
    point itself names the building; its other planes come from a lookup
    by building id."""
    d = 0.0008
    data = await _get_json(
        session,
        f"{API}/MapServer/identify",
        {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "imageDisplay": "500,600,96",
            "mapExtent": f"{lon - d},{lat - d},{lon + d},{lat + d}",
            "tolerance": "2",
            "layers": f"all:{LAYER}",
            "sr": "4326",
            "returnGeometry": "false",
            "lang": "de",
        },
    )
    buildings: list[int] = []
    for item in data.get("results", []):
        try:
            bid = int(item["attributes"]["building_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid not in buildings:
            buildings.append(bid)
    roofs: dict[int, Roof] = {}
    for bid in buildings[:3]:
        found = await _get_json(
            session,
            f"{API}/MapServer/find",
            {"layer": LAYER, "searchText": str(bid), "searchField": "building_id", "returnGeometry": "false", "lang": "de"},
        )
        for item in found.get("results", []):
            try:
                roof_id = int(item.get("id") or item["attributes"].get("label"))
            except (KeyError, TypeError, ValueError):
                continue
            roof = _roof_from_attributes(item.get("attributes", {}), roof_id)
            if roof and roof.area >= _MIN_AREA:
                roofs[roof_id] = roof
    return sorted(roofs.values(), key=lambda r: -r.area)


def typical_day_kwh(kwp: float, irradiation_kwh_m2_year: float, monthly_share: dict[int, float], month: int, days_in_month: int, losses: float, damping: float = 1.0) -> float | None:
    """The long-term expectation of a day of this month for the plane:
    peak power times the plane's irradiation of that month (the cadastre's
    yearly irradiation on the plane split by its monthly profile), after
    the system losses. A kWp delivers 1 kWh per kWh/m² of irradiation by
    definition, so no module area is needed."""
    if kwp <= 0 or irradiation_kwh_m2_year <= 0 or month not in monthly_share or days_in_month <= 0:
        return None
    month_kwh_m2 = irradiation_kwh_m2_year * monthly_share[month] / 100.0
    return kwp * month_kwh_m2 / days_in_month * (1 - losses / 100.0) * damping
