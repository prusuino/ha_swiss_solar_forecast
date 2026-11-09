"""Diagnostics download: the planes, the run, what the reads cost and the
hourly result, the terrain horizon in brief and what the calibration
learned, with the place redacted (the horizon's full profile is left out:
it is a fingerprint of the place)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_POINT_ID, CONF_POINT_LAT, CONF_POINT_LON, CONF_POINT_NAME, CONF_YIELD_SENSOR, USER_AGENT
from .coordinator import SolarForecastCoordinator

_REDACT = (CONF_POINT_LAT, CONF_POINT_LON, CONF_POINT_NAME, CONF_POINT_ID, CONF_ADDRESS)


def _redact_planes(planes: Any) -> Any:
    if not isinstance(planes, list):
        return planes
    return [{k: ("**REDACTED**" if k == "roof_id" else v) for k, v in p.items()} if isinstance(p, dict) else p for p in planes]


def _horizon(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    if coordinator is None:
        return None
    horizon = coordinator.horizon
    if horizon is None:
        return {"enabled": coordinator.use_horizon, "read": False}
    elevations = horizon.elevations
    return {
        "enabled": True,
        "read": True,
        "fetched": horizon.fetched,
        "directions": len(elevations),
        "highest": max(elevations, default=0.0),
        "mean": round(sum(elevations) / len(elevations), 2) if elevations else 0.0,
    }


def _calibration(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    calibration = coordinator.calibration if coordinator else None
    if calibration is None:
        return None
    return {
        "overall": round(calibration.overall, 3),
        "factors_utc": calibration.factors(),
        "bins_utc": {hour: [round(v, 1) for v in values] for hour, values in sorted(calibration.bins.items())},
        "samples": calibration.samples,
        "last_sample": calibration.last_sample,
        "reading_time": calibration.reading_time,
        "has_reading": calibration.reading_wh is not None,
        "logged_hours": len(calibration.log),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: SolarForecastCoordinator | None = getattr(entry, "runtime_data", None)
    snap = coordinator.data if coordinator else None
    return {
        "integration": USER_AGENT,
        "entry": {
            "title": "**REDACTED**",
            "unique_id": "**REDACTED**" if entry.unique_id else None,
            "data": {k: ("**REDACTED**" if k in _REDACT else _redact_planes(v) if k == "planes" else v) for k, v in entry.data.items()},
            "options": {k: ("**REDACTED**" if k == CONF_YIELD_SENSOR else _redact_planes(v) if k == "planes" else v) for k, v in entry.options.items()},
        },
        "terrain_horizon": _horizon(coordinator),
        "calibration": _calibration(coordinator),
        "planes": [{"label": p.label, "has_roof": p.has_roof, **asdict(p.plane)} for p in coordinator.planes] if coordinator else None,
        "forecast": {
            "loaded": coordinator is not None,
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "run": snap.run.isoformat() if snap and snap.run else None,
            "fetched": snap.fetched.isoformat() if snap and snap.fetched else None,
            "missing": snap.missing if snap else None,
            "diffuse_from_forecast": snap.diffuse_forecast if snap else None,
            "requests_today": snap.requests_today if snap else None,
            "bytes_today": snap.bytes_today if snap else None,
            "hours": [
                {
                    "start": h.start.isoformat(),
                    "ghi": h.ghi,
                    "dhi": h.dhi,
                    "temperature": h.temperature,
                    "poa": round(h.poa, 1),
                    "power_kw": round(h.power_kw, 3),
                    "beam_fraction": round(h.beam_fraction, 2),
                }
                for h in (snap.plant.hours if snap else [])
            ],
        },
    }
