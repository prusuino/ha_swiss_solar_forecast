"""Diagnostics download: the planes, the run, what the reads cost and the
hourly result with the hours under low stratus, the terrain horizon in
brief, the snow as it is followed,
the correction from a measurement station, what the calibration learned
and how accurate the forecast was day by day, with the place redacted
(the horizon's full profile and the station's name are left out: they
tell where the plant is)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calibration import BINS_BY
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
        "bins_by": BINS_BY,  # the sun's bearing/height in degrees, the lower edges of each part of the sky
        "factors": calibration.factors(),
        # per part of the sky: Wh counted, Wh forecast, hours (older ones fade)
        "bins": {key: [round(v, 1) for v in calibration.bins[key]] for key in calibration.factors()},
        "samples": calibration.samples,
        "last_sample": calibration.last_sample,
        "reading_time": calibration.reading_time,
        "has_reading": calibration.reading_wh is not None,
        "logged_hours": len(calibration.log),
        # the hours under low stratus, learned together: factor, then Wh counted, Wh forecast, hours (older ones fade)
        "low_stratus": {
            "factor": round(calibration.stratus_factor, 3) if calibration.stratus_learned else None,
            "sums": [round(v, 1) for v in calibration.stratus],
            "marked_hours": len(calibration.stratus_log),
        },
    }


def _snow(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    if coordinator is None:
        return None
    snow = coordinator.snow
    if snow is None:
        return {"enabled": coordinator.use_snow}
    return {
        "enabled": True,
        "followed_until": snow.time or None,
        "ground_mm": snow.ground,
        "planes": [{"load_mm": load, "cover": cover} for load, cover in snow.planes],
        "hours_kept": len(snow.history),
    }


def _accuracy(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    accuracy = coordinator.accuracy if coordinator else None
    if accuracy is None:
        return None
    today = dt_util.now().date()
    return {
        "7_days": accuracy.figures(today, 7),
        "30_days": accuracy.figures(today, 30),
        "days": accuracy.daily(today, 30),
        "days_kept": len(accuracy.days),
        "open_hours": len(accuracy.pending),
    }


def _station(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    if coordinator is None:
        return None
    station = coordinator.station
    found = coordinator.correction
    height = station.get("height")
    return {
        "enabled": coordinator.use_station,
        "found": bool(station),
        "distance_km": station.get("distance_km"),
        "height_difference_m": None if height is None or coordinator.height is None else round(height - coordinator.height),
        "correction": None
        if found is None
        else {
            "ratio": found.ratio,
            "measured_w_m2": found.measured,
            "forecast_w_m2": found.expected,
            "measured_at": found.measured_at.isoformat(),
            "trust": found.trust,
        },
    }


def _finished_hours(coordinator: SolarForecastCoordinator | None) -> dict[str, Any] | None:
    if coordinator is None:
        return None
    return {"kept": len(coordinator.frozen.hours), "measured": len(coordinator.frozen.measured)}


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
        "accuracy": _accuracy(coordinator),
        "snow": _snow(coordinator),
        "station_correction": _station(coordinator),
        "finished_hours": _finished_hours(coordinator),
        "planes": [{"label": p.label, "has_roof": p.has_roof, **asdict(p.plane)} for p in coordinator.planes] if coordinator else None,
        "forecast": {
            "loaded": coordinator is not None,
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "run": snap.run.isoformat() if snap and snap.run else None,
            "fetched": snap.fetched.isoformat() if snap and snap.fetched else None,
            "missing": snap.missing if snap else None,
            "series_runs": {param: run.isoformat() for param, run in sorted(coordinator.series_runs.items())} if coordinator else None,
            "diffuse_from_forecast": snap.diffuse_forecast if snap else None,
            "low_stratus_known": snap.stratus_known if snap else None,
            "requests_today": snap.requests_today if snap else None,
            "bytes_today": snap.bytes_today if snap else None,
            "last_read": {"requests": snap.last_requests, "bytes": snap.last_bytes} if snap else None,
            "hours": [
                {
                    "start": h.start.isoformat(),
                    "ghi": h.ghi,
                    "dhi": h.dhi,
                    "temperature": h.temperature,
                    "poa": round(h.poa, 1),
                    "power_kw": round(h.power_kw, 3),
                    "beam_fraction": round(h.beam_fraction, 2),
                    "wind": None if h.wind is None else round(h.wind, 1),
                    "snow_cover": round(h.snow_cover, 2),
                    "snow_loss_kw": round(h.snow_loss_kw, 3),
                    "low_stratus": h.start in snap.stratus,
                }
                for h in (snap.plant.hours if snap else [])
            ],
        },
    }
