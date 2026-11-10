"""Self-calibration against the plant's own yield meter.

A forecast from weather and geometry knows nothing about the tree that
shades the modules until ten, the chimney, the dirt or an optimistic
datasheet. The plant's energy meter does. Every full hour the meter's
increase is compared with what was forecast for that hour, and the ratio
is learned per hour of the day (in UTC, so it follows the sun and not the
clock change). The forecast is then multiplied by the learned factor of
each hour.

The learning is deliberately cautious:

* It sums energy, not ratios: a dull hour with a few watt-hours cannot
  outweigh a bright one.
* Old hours fade (each new sample of an hour lowers the weight of the
  earlier ones), so the factors follow the seasons: the tree's shadow in
  December is not the one in June.
* With few samples a factor stays near the plant's overall factor, and
  that one near 1; only evidence moves it. That caution fades as the
  samples come in: after about two weeks of productive hours the measured
  ratio stands alone.
* Hours with nearly nothing forecast, a meter standing still (outage) or
  a meter running backwards (reset) are skipped, and so are the hours in
  which the forecast itself sees snow on the modules: what the snow takes
  is no property of the plant to be learned.

Pure arithmetic and plain data, so it can be tested without a clock.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DECAY = 0.93  # weight left of the earlier samples of an hour when a new one arrives (about two weeks of memory)
PRIOR_OVERALL = 12.0  # samples' worth of belief that the plant's overall factor is 1
PRIOR_HOUR = 3.0  # samples' worth of belief that an hour's factor is the overall one
# Faded samples from which the belief counts nothing any more (an hour's
# faded count settles near 14 with a sample every day).
FULL_TRUST_OVERALL = 60.0
FULL_TRUST_HOUR = 10.0
FACTOR_MIN = 0.1
FACTOR_MAX = 1.5
LOG_KEEP = timedelta(hours=48)

_TO_WH = {"wh": 1.0, "kwh": 1000.0, "mwh": 1_000_000.0}


def energy_wh(value: object, unit: object) -> float | None:
    """A meter reading in Wh, from a state and its unit; None when it is no number or no energy unit."""
    factor = _TO_WH.get(str(unit or "").strip().lower())
    if factor is None:
        return None
    try:
        reading = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if reading != reading or reading in (float("inf"), float("-inf")):
        return None
    return reading * factor


@dataclass
class Calibration:
    """What was learned for one plant, and what is needed to learn more."""

    fingerprint: str = ""  # of the plant's figures and the meter: a change starts the learning afresh
    bins: dict[int, list[float]] = field(default_factory=dict)  # hour of day (UTC) -> [measured Wh, forecast Wh, samples], faded
    log: dict[str, float] = field(default_factory=dict)  # hour start (ISO, UTC) -> uncalibrated forecast Wh
    reading_time: str = ""  # the full hour (ISO, UTC) of the last meter reading
    reading_wh: float | None = None
    samples: int = 0  # hours learned in total
    last_sample: str = ""  # start of the last learned hour

    # ------------------------------------------------------------- forecast

    def record_forecast(self, hours: list[tuple[datetime, float]], now: datetime, skip: Collection[datetime] = ()) -> None:
        """Keep the uncalibrated forecast (hour start, Wh) of the hours not
        yet over; what was forecast for a finished hour stays as it was.
        The hours in `skip` (snow on the modules) are not to be learned."""
        for start, wh in hours:
            if start + timedelta(hours=1) <= now:
                continue
            if start in skip:
                self.log.pop(_key(start), None)
            else:
                self.log[_key(start)] = round(wh, 1)
        limit = _key(now - LOG_KEEP)
        for key in [k for k in self.log if k < limit]:
            del self.log[key]

    # ---------------------------------------------------------------- meter

    def increase(self, hour_end: datetime, reading_wh: float | None) -> float | None:
        """What the meter counted in the hour that ends at `hour_end`, when
        its reading of the hour's start is known. Ask before `meter_read`,
        which replaces the kept reading."""
        if reading_wh is None or self.reading_wh is None or self.reading_time != _key(hour_end - timedelta(hours=1)):
            return None
        return reading_wh - self.reading_wh

    def meter_read(self, hour_end: datetime, reading_wh: float | None, min_wh: float, max_wh: float) -> bool:
        """The meter at a full hour. Learns the hour that just ended when the
        reading of its start is known; returns whether something was learned."""
        previous_time, previous_wh = self.reading_time, self.reading_wh
        self.reading_time, self.reading_wh = _key(hour_end), reading_wh
        start = hour_end - timedelta(hours=1)
        if reading_wh is None or previous_wh is None or previous_time != _key(start):
            return False
        return self.learn(start, reading_wh - previous_wh, min_wh, max_wh)

    def learn(self, start: datetime, measured_wh: float, min_wh: float, max_wh: float) -> bool:
        """One finished hour: what the meter counted against what was forecast."""
        forecast = self.log.get(_key(start))
        if forecast is None or forecast < min_wh:
            return False  # nothing forecast worth comparing (night, deep overcast)
        if measured_wh <= 0 or measured_wh > max_wh:
            return False  # outage, snow, meter reset or a jump no plant of this size makes
        hour = start.astimezone(UTC).hour
        measured, expected, count = self.bins.get(hour, [0.0, 0.0, 0.0])
        self.bins[hour] = [measured * DECAY + measured_wh, expected * DECAY + forecast, count * DECAY + 1.0]
        self.samples += 1
        self.last_sample = _key(start)
        return True

    # -------------------------------------------------------------- factors

    @property
    def overall(self) -> float:
        """The plant's factor over all hours, held near 1 while there is little evidence."""
        measured = sum(b[0] for b in self.bins.values())
        expected = sum(b[1] for b in self.bins.values())
        count = sum(b[2] for b in self.bins.values())
        if expected <= 0 or count <= 0:
            return 1.0
        raw = min(2.0, max(0.05, measured / expected))
        return _clamp(_blend(raw, count, 1.0, PRIOR_OVERALL, FULL_TRUST_OVERALL))

    def factor(self, hour_utc: int) -> float:
        """The factor of an hour of the day: its own evidence, held near the overall factor while it is thin."""
        overall = self.overall
        entry = self.bins.get(hour_utc)
        if not entry or entry[1] <= 0 or entry[2] <= 0:
            return overall
        raw = min(2.0, max(0.05, entry[0] / entry[1]))
        return _clamp(_blend(raw, entry[2], overall, PRIOR_HOUR, FULL_TRUST_HOUR))

    def factors(self) -> dict[int, float]:
        """The factors of the hours with evidence."""
        return {hour: round(self.factor(hour), 3) for hour in sorted(self.bins)}

    # ---------------------------------------------------------- persistence

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "bins": {str(hour): [round(v, 3) for v in values] for hour, values in self.bins.items()},
            "log": self.log,
            "reading_time": self.reading_time,
            "reading_wh": self.reading_wh,
            "samples": self.samples,
            "last_sample": self.last_sample,
        }

    @classmethod
    def from_dict(cls, data: object, fingerprint: str) -> Calibration:
        """What was kept, when it belongs to this plant and meter; otherwise a fresh start."""
        if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
            return cls(fingerprint=fingerprint)
        try:
            bins = {int(hour): [float(v) for v in values][:3] for hour, values in (data.get("bins") or {}).items()}
            bins = {hour: values for hour, values in bins.items() if 0 <= hour <= 23 and len(values) == 3}
            log = {str(key): float(value) for key, value in (data.get("log") or {}).items()}
            reading = data.get("reading_wh")
            return cls(
                fingerprint=fingerprint,
                bins=bins,
                log=log,
                reading_time=str(data.get("reading_time") or ""),
                reading_wh=None if reading is None else float(reading),
                samples=int(data.get("samples") or 0),
                last_sample=str(data.get("last_sample") or ""),
            )
        except (AttributeError, TypeError, ValueError):
            return cls(fingerprint=fingerprint)


def _key(when: datetime) -> str:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def _blend(measured: float, count: float, belief: float, belief_weight: float, full_trust: float) -> float:
    """The measured ratio mixed with the belief; the belief weighs less with every sample and nothing from `full_trust` on."""
    weight = belief_weight * max(0.0, 1.0 - count / full_trust)
    return (count * measured + weight * belief) / (count + weight)


def _clamp(value: float) -> float:
    return min(FACTOR_MAX, max(FACTOR_MIN, value))
