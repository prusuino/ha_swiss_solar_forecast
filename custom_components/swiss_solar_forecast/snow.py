"""Snow on the modules.

A covered module produces nothing, however bright the day: after a
snowfall the radiation forecast alone is far too high until the snow has
gone. This module follows the snow from the forecast itself, hour by hour:

* Precipitation falls as snow when it is cold enough, and one millimetre
  of water (about a centimetre of fresh snow) covers a module completely.
* The snow slides off when the modules warm up, the faster the steeper
  they stand (after Marion et al., "Measured and modeled photovoltaic
  system energy losses from snow", the model also used by NREL's SAM).
* It melts with warm air and with sun, which is what clears a flat roof.

The same is done for the ground, without the sliding: snow on the ground
reflects more light onto steep planes.

What was worked out for the hours that are over is kept, so a new forecast
run continues where the last one stood. Everything here is an estimate:
the thresholds are taken from the literature, not fitted to a plant. With
a yield meter the coordinator corrects the one error that matters most,
modules shown as covered that are producing. Pure arithmetic."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SNOW_BELOW = 0.5  # °C: at and below, all precipitation falls as snow
RAIN_ABOVE = 2.0  # °C: at and above, all of it is rain
FULL_COVER_MM = 1.0  # mm of water that cover a module completely
MELT_PER_DEGREE = 0.125  # mm of water per hour and kelvin above freezing (3 mm per degree-day)
MELT_PER_WATT = 0.002  # mm of water per hour and W/m² of irradiance
MELT_SUN_FROM = -5.0  # °C: below, the sun warms the snow but melts none of it
SLIDE_PER_HOUR = 0.197  # share of the module length the snow slides in an hour on a vertical plane
SLIDE_SLOPE = 80.0  # W/m² per kelvin below freezing that it takes to make the snow slide
GROUND_SNOW_MM = 2.0  # mm of water on the ground from which it reflects like snow
COVER_SHOWN = 0.05  # a cover below this counts as free
HISTORY_KEEP = timedelta(hours=48)
GAP_MAX = timedelta(hours=24)  # a longer gap in the hours followed: what lay then is unknown, the snow starts at none

# (hour start, precipitation mm, air temperature °C, global irradiance W/m², irradiance on every plane W/m²)
SnowHour = tuple[datetime, float | None, float | None, float, list[float]]


def _key(when: datetime) -> str:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def _time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def snowfall(precipitation: float | None, temperature: float | None) -> float:
    """The part of an hour's precipitation (mm) that falls as snow."""
    if precipitation is None or precipitation <= 0 or temperature is None:
        return 0.0
    if temperature <= SNOW_BELOW:
        return precipitation
    if temperature >= RAIN_ABOVE:
        return 0.0
    return precipitation * (RAIN_ABOVE - temperature) / (RAIN_ABOVE - SNOW_BELOW)


def _melt(temperature: float, irradiance: float) -> float:
    """Millimetres of water that melt in an hour."""
    sun = min(1.0, max(0.0, (temperature - MELT_SUN_FROM) / -MELT_SUN_FROM))
    return max(0.0, temperature) * MELT_PER_DEGREE + max(0.0, irradiance) * MELT_PER_WATT * sun


def step_ground(load: float, precipitation: float | None, temperature: float | None, ghi: float) -> float:
    """The snow on the ground (mm of water) after an hour."""
    load += snowfall(precipitation, temperature)
    if load <= 0:
        return 0.0
    return max(0.0, load - _melt(0.0 if temperature is None else temperature, ghi))


def step_plane(load: float, cover: float, precipitation: float | None, temperature: float | None, poa: float, tilt: float) -> tuple[float, float]:
    """The snow on a module plane after an hour: its load (mm of water,
    mean over the plane) and the share of the plane it covers."""
    fall = snowfall(precipitation, temperature)
    if fall > 0:
        load += fall
        cover = max(cover, min(1.0, load / FULL_COVER_MM))
    if load <= 0 or cover <= 0:
        return 0.0, 0.0
    temp = 0.0 if temperature is None else temperature
    if temp + max(0.0, poa) / SLIDE_SLOPE > 0:
        left = max(0.0, cover - SLIDE_PER_HOUR * math.sin(math.radians(min(90.0, max(0.0, tilt)))))
        load *= left / cover
        cover = left
    load = max(0.0, load - _melt(temp, poa))
    # a thin rest no longer covers what it covered
    cover = min(cover, load / FULL_COVER_MM)
    if cover <= 0.001 or load <= 0.001:
        return 0.0, 0.0
    return load, cover


@dataclass
class SnowState:
    """The snow as it lay at the end of the last hour that is over, and
    what was worked out for the hours before."""

    time: str = ""  # end of the last hour followed for good (ISO, UTC)
    ground: float = 0.0  # mm of water
    planes: list[list[float]] = field(default_factory=list)  # per plane: [load mm of water, covered share]
    history: dict[str, list[float]] = field(default_factory=dict)  # hour start -> [ground at its start, mean cover per plane…]

    def _walk(self, hours: list[SnowHour], tilts: list[float], now: datetime | None) -> tuple[dict[datetime, float], dict[datetime, list[float]]]:
        """Follow the snow through the hours. Hours already followed for
        good come from the history; the others continue from the kept
        state. With `now`, the hours that are over by then are kept."""
        count = len(tilts)
        done = _time(self.time)
        ground = self.ground
        planes = [list(p) for p in self.planes] if len(self.planes) == count else [[0.0, 0.0] for _ in range(count)]
        grounds: dict[datetime, float] = {}
        covers: dict[datetime, list[float]] = {}
        started = False
        for start, precipitation, temperature, ghi, poas in sorted(hours, key=lambda hour: hour[0]):
            end = start + timedelta(hours=1)
            key = _key(start)
            if done is not None and end <= done:
                kept = self.history.get(key) or []
                grounds[start] = kept[0] if kept else 0.0
                covers[start] = list(kept[1:]) if len(kept) == count + 1 else [0.0] * count
                continue
            if not started:
                started = True
                if done is None or start - done > GAP_MAX:
                    ground = 0.0
                    planes = [[0.0, 0.0] for _ in range(count)]
            grounds[start] = ground
            before = [plane[1] for plane in planes]
            ground = step_ground(ground, precipitation, temperature, ghi)
            planes = [
                list(step_plane(plane[0], plane[1], precipitation, temperature, poas[index] if index < len(poas) else 0.0, tilts[index]))
                for index, plane in enumerate(planes)
            ]
            covers[start] = [(a + plane[1]) / 2 for a, plane in zip(before, planes)]
            if now is not None and end <= now:
                self.time = _key(end)
                self.ground = round(ground, 3)
                self.planes = [[round(plane[0], 3), round(plane[1], 4)] for plane in planes]
                self.history[key] = [round(grounds[start], 2)] + [round(cover, 3) for cover in covers[start]]
        if now is not None:
            limit = _key(now - HISTORY_KEEP)
            for key in [k for k in self.history if k < limit]:
                del self.history[key]
        return grounds, covers

    def ground_track(self, hours: list[SnowHour]) -> dict[datetime, float]:
        """The snow on the ground (mm of water) at the start of every hour; nothing is kept."""
        return self._walk(hours, [], None)[0]

    def advance(self, hours: list[SnowHour], tilts: list[float], now: datetime) -> dict[datetime, list[float]]:
        """The covered share of every plane, as the mean of each hour. The
        hours that are over at `now` are kept and not worked out again."""
        if len(self.planes) != len(tilts):
            self.planes = [[0.0, 0.0] for _ in tilts]
            self.history = {}
        return self._walk(hours, tilts, now)[1]

    def clear_modules(self) -> None:
        """The modules are free (the plant produces); the ground keeps its snow."""
        self.planes = [[0.0, 0.0] for _ in self.planes]

    @property
    def covered(self) -> bool:
        return any(plane[1] > COVER_SHOWN for plane in self.planes)

    # ---------------------------------------------------------------- store

    def as_dict(self) -> dict:
        return {"time": self.time, "ground": self.ground, "planes": self.planes, "history": self.history}

    @classmethod
    def from_dict(cls, data: dict | None, planes: int) -> SnowState:
        """What was kept, when it fits the number of planes; otherwise no snow."""
        fresh = cls(planes=[[0.0, 0.0] for _ in range(planes)])
        if not isinstance(data, dict):
            return fresh
        try:
            kept = [[max(0.0, float(load)), min(1.0, max(0.0, float(cover)))] for load, cover in data.get("planes") or []]
            history = {str(key): [float(v) for v in values] for key, values in (data.get("history") or {}).items()}
            ground = max(0.0, float(data.get("ground") or 0.0))
        except (TypeError, ValueError):
            return fresh
        time = str(data.get("time") or "")
        if _time(time) is None:
            return fresh
        if len(kept) != planes:
            return cls(time=time, ground=ground, planes=fresh.planes)
        return cls(time=time, ground=ground, planes=kept, history=history)
