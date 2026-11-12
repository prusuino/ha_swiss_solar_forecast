"""What the weather model gets wrong, by the kind of sky.

A weather model is not wrong at random. It lets too much light through a
broken sky, keeps a grey lid shut that opens, or the other way round, and
it does so again the next time the sky looks the same. A measurement
station nearby tells which: hour by hour, the radiation it received is
set against what the forecast had for that hour, and the hours are sorted
by the sky the forecast expected:

* clear: the forecast has most of what a cloudless sky would give,
* mixed: clearly less than that, but far from dark,
* grey: a fraction of it.

For each of the three the measured and the forecast radiation are summed.
Their ratio is the factor on the forecast radiation of the coming hours
with that kind of sky: the hours of today beyond the reach of the
short-term correction, tomorrow and the day after.

It is cautious:

* An hour is measured against the forecast as it stood three hours before
  the hour began. That is where the short-term correction ends and this
  factor takes over.
* Only hours with the sun well above the horizon count, and only with a
  forecast worth comparing.
* Radiation is summed, not ratios: a dim hour weighs little, and one odd
  hour moves next to nothing.
* A factor applies after some hours of its sky only, is held near 1 while
  the evidence is thin, and is limited.
* Earlier hours fade, so the factors follow the season and the model.
* The farther the station, the less of a factor is applied.

It needs no meter and no setting, and no traffic of its own: the station's
file of the day is read for the short-term correction anyway.

Pure arithmetic and plain data, so it can be tested without a network.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .nowcast import REACH_HOURS, Correction
from .solar import sun_position

CLEAR = "clear"
MIXED = "mixed"
GREY = "grey"
SKIES = (CLEAR, MIXED, GREY)
CLEAR_FROM = 0.75  # of the cloudless sky's radiation: from here on the forecast expects a clear hour
GREY_BELOW = 0.4  # of the cloudless sky's radiation: below it the forecast expects a grey hour
MIN_CLEAR_SKY = 10.0  # W/m² a cloudless sky gives in the hour: below it there is no sky to speak of
LEAD = timedelta(hours=REACH_HOURS)  # the forecast an hour is measured against stood this long before it
LOG_AHEAD = timedelta(hours=24)  # the forecast is kept for the hours up to here
LOG_KEEP = timedelta(hours=36)
MIN_ELEVATION = 10.0  # degrees at both ends of an hour: a lower sun is behind hills and haze, here or at the station
MIN_FORECAST = 50.0  # W/m² forecast on average: below it an hour teaches nothing
MIN_VALUES = 5  # of an hour's six ten-minute values
DECAY = 0.985  # weight left of a sky's earlier hours when a new one arrives (some sixty hours of memory)
MIN_HOURS = 5.5  # of a kind of sky, earlier ones faded: its factor applies from the sixth hour
PRIOR = 10.0  # hours' worth of belief that the weather model is right
FULL_TRUST = 30.0  # hours of a sky from which its factor stands on the measurements alone
FACTOR_MIN = 0.5
FACTOR_MAX = 1.5
_STEP = timedelta(minutes=10)
_HOUR = timedelta(hours=1)


def clear_sky(when: datetime, lat: float, lon: float) -> float:
    """The global radiation of a cloudless sky at a moment, W/m², after
    Haurwitz: from the sun's height alone, which is all a sorting needs."""
    sine = math.sin(math.radians(sun_position(when, lat, lon).elevation))
    if sine <= 0:
        return 0.0
    return 1098.0 * sine * math.exp(-0.059 / sine)


def clear_sky_hour(start: datetime, lat: float, lon: float) -> float:
    """The same as the mean of the hour that starts at `start`."""
    return sum(clear_sky(start + _STEP * step + _STEP / 2, lat, lon) for step in range(6)) / 6


def sky_of(ghi: float | None, start: datetime, lat: float, lon: float) -> str | None:
    """The kind of sky a forecast of `ghi` W/m² expects for the hour that
    starts at `start`; None at night and without a forecast."""
    if ghi is None:
        return None
    clear = clear_sky_hour(start, lat, lon)
    if clear < MIN_CLEAR_SKY:
        return None
    share = ghi / clear
    if share >= CLEAR_FROM:
        return CLEAR
    return GREY if share < GREY_BELOW else MIXED


@dataclass
class SkyBias:
    """What was learned at one place, and what is needed to learn more."""

    lat: float = 0.0
    lon: float = 0.0
    sums: dict[str, list[float]] = field(default_factory=dict)  # kind of sky -> [measured W/m², forecast W/m², hours], faded
    log: dict[str, float] = field(default_factory=dict)  # hour start (ISO, UTC) -> forecast W/m² as it stood LEAD before
    done: str = ""  # start (ISO, UTC) of the last hour the station was looked at for
    samples: int = 0  # hours learned in total
    last_sample: str = ""  # start of the last learned hour

    # ------------------------------------------------------------- forecast

    def record_forecast(self, hours: Iterable[tuple[datetime, float | None]], now: datetime) -> bool:
        """Keep the forecast radiation (hour start, W/m²) as it comes, before
        any correction. An hour's value is replaced until the hour is LEAD
        away and stays from then on; an hour first seen later keeps what
        was first seen. Returns whether anything changed."""
        before = dict(self.log)
        for start, ghi in hours:
            if ghi is None or start + _HOUR <= now or start - now > LOG_AHEAD:
                continue
            key = _key(start)
            if start - now >= LEAD or key not in self.log:
                self.log[key] = round(ghi, 1)
        limit = _key(now - LOG_KEEP)
        for key in [k for k in self.log if k < limit]:
            del self.log[key]
        return self.log != before

    # -------------------------------------------------------------- station

    def learn(self, values: Iterable[tuple[datetime, float]]) -> int:
        """What the station measured today (end of ten minutes, W/m²): every
        hour it has finished and that was not looked at yet is set against
        its forecast. Returns the number of hours learned."""
        by_hour: dict[datetime, list[float]] = {}
        newest: datetime | None = None
        for end, value in values:
            by_hour.setdefault((end - _STEP).replace(minute=0, second=0, microsecond=0), []).append(value)
            newest = end if newest is None or end > newest else newest
        learned = 0
        for start in sorted(by_hour):
            key = _key(start)
            if newest is None or start + _HOUR > newest or key <= self.done:
                continue  # the station is not through with the hour, or the hour was looked at before
            self.done = key
            forecast = self.log.get(key)
            if len(by_hour[start]) < MIN_VALUES or forecast is None or forecast < MIN_FORECAST:
                continue
            if min(sun_position(when, self.lat, self.lon).elevation for when in (start, start + _HOUR)) < MIN_ELEVATION:
                continue
            sky = sky_of(forecast, start, self.lat, self.lon)
            if sky is None:
                continue
            measured_mean = max(0.0, sum(by_hour[start]) / len(by_hour[start]))
            measured, expected, count = self.sums.get(sky, [0.0, 0.0, 0.0])
            self.sums[sky] = [measured * DECAY + measured_mean, expected * DECAY + forecast, count * DECAY + 1.0]
            self.samples += 1
            self.last_sample = key
            learned += 1
        return learned

    # -------------------------------------------------------------- factors

    def learned(self, sky: str | None) -> bool:
        """Whether a kind of sky has the hours behind it that its factor needs."""
        entry = self.sums.get(sky or "")
        return bool(entry) and entry[1] > 0 and entry[2] >= MIN_HOURS

    def factor(self, sky: str | None) -> float:
        """The factor on the forecast radiation under a kind of sky: its
        measurements, held near 1 while they are few. 1 before it has the
        hours it needs."""
        if not self.learned(sky):
            return 1.0
        measured, expected, count = self.sums[sky or ""]
        weight = PRIOR * max(0.0, 1.0 - count / FULL_TRUST)
        return min(FACTOR_MAX, max(FACTOR_MIN, (count * measured / expected + weight) / (count + weight)))

    def sky_at(self, start: datetime, ghi: float | None) -> str | None:
        """The kind of sky the forecast expects for the hour that starts at `start`."""
        return sky_of(ghi, start, self.lat, self.lon)

    def factor_at(self, start: datetime, ghi: float | None) -> float:
        """The factor of the hour that starts at `start`, for which `ghi` W/m² are forecast."""
        return self.factor(self.sky_at(start, ghi))

    @property
    def in_force(self) -> bool:
        """Whether any kind of sky has a factor."""
        return any(self.learned(sky) for sky in SKIES)

    def factors(self) -> dict[str, float | None]:
        """The factor of every kind of sky; None where there are too few hours yet."""
        return {sky: round(self.factor(sky), 3) if self.learned(sky) else None for sky in SKIES}

    def hours(self) -> dict[str, float]:
        """The hours behind every kind of sky (earlier ones fade)."""
        return {sky: round(self.sums.get(sky, [0.0, 0.0, 0.0])[2], 1) for sky in SKIES}

    # ---------------------------------------------------------- persistence

    def as_dict(self) -> dict:
        return {
            "at": [round(self.lat, 3), round(self.lon, 3)],
            "sums": {sky: [round(v, 3) for v in values] for sky, values in self.sums.items()},
            "log": dict(self.log),
            "done": self.done,
            "samples": self.samples,
            "last_sample": self.last_sample,
        }

    @classmethod
    def from_dict(cls, data: object, lat: float, lon: float) -> SkyBias:
        """What was kept, when it belongs to this place; otherwise a fresh start."""
        if not isinstance(data, dict) or data.get("at") != [round(lat, 3), round(lon, 3)]:
            return cls(lat=lat, lon=lon)
        try:
            sums = {str(sky): [float(v) for v in values] for sky, values in (data.get("sums") or {}).items()}
            return cls(
                lat=lat,
                lon=lon,
                sums={sky: values for sky, values in sums.items() if sky in SKIES and len(values) == 3},
                log={str(key): float(value) for key, value in (data.get("log") or {}).items()},
                done=str(data.get("done") or ""),
                samples=int(data.get("samples") or 0),
                last_sample=str(data.get("last_sample") or ""),
            )
        except (AttributeError, TypeError, ValueError):
            return cls(lat=lat, lon=lon)


def corrected(rows: list[tuple], learned: SkyBias | None, found: Correction | None, now: datetime, trust: float) -> list[tuple]:
    """The forecast rows (hour end, global, diffuse, …) with the learned
    factor of each hour's sky on the hours not yet over. `trust` is what
    the station counts at its distance. Where the short-term correction
    `found` reaches, it speaks instead: the factor takes the share of an
    hour that correction leaves. What is added or taken is direct light:
    the diffuse share stays, limited to the new global radiation."""
    if learned is None or trust <= 0 or not learned.in_force:
        return rows
    near = found is not None and found.fresh(now)
    out = []
    for row in rows:
        end, ghi, dhi = row[0], row[1], row[2]
        if ghi is None or ghi <= 0 or end <= now:
            out.append(row)
            continue
        start = end - _HOUR
        share = trust * (1.0 - (found.reach(start) if near else 0.0))
        factor = 1.0 + (learned.factor_at(start, ghi) - 1.0) * share
        if factor == 1.0:
            out.append(row)
            continue
        new = ghi * factor
        out.append((end, new, None if dhi is None else min(dhi, new), *row[3:]))
    return out


def _key(when: datetime) -> str:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
