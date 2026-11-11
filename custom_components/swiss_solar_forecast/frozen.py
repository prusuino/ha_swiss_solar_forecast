"""The hours of the day that are over.

Every model run computes the whole day again, the hours that are over
included. Left alone, the day's sum would keep moving in the evening with
weather that was forecast for the morning and never came. So an hour is
kept as it was forecast when it ended, and later runs leave it alone.

With a yield meter the plant knows better still: what the meter counted
in a finished hour takes the forecast's place in the day's sum, so the
day's figure ends at what the plant really delivered. The forecast of the
hour is kept beside it (the charts and the accuracy figures go on showing
what was forecast).

Pure arithmetic and plain data, so it can be tested without a clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

KEEP = timedelta(hours=48)
_HOUR = timedelta(hours=1)


def _key(when: datetime) -> str:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


@dataclass
class Frozen:
    """The finished hours of one plant."""

    fingerprint: str = ""  # of the plant's figures and the meter: a change starts afresh
    hours: dict[str, list[float]] = field(default_factory=dict)  # hour start (ISO, UTC) -> [plant Wh, plane 1 Wh, …, plant Wh before the self-calibration], the last forecast
    measured: dict[str, float] = field(default_factory=dict)  # hour start (ISO, UTC) -> Wh the yield meter counted

    def settle(self, start: datetime, values: list[float], now: datetime) -> list[float]:
        """The energy that counts for an hour (the plant's, then every
        plane's). An hour still running, or one seen for the first time,
        takes the values just computed; an hour that is over keeps those
        it ended with."""
        key = _key(start)
        if start + _HOUR <= now:
            kept = self.hours.get(key)
            if kept is not None and len(kept) == len(values):
                return kept
        self.hours[key] = [round(value, 1) for value in values]
        return values

    def measure(self, start: datetime, wh: float) -> None:
        """What the meter counted in a finished hour."""
        self.measured[_key(start)] = round(wh, 1)

    def measured_hours(self) -> dict[datetime, float]:
        """The measured hours, by their start."""
        return {datetime.fromisoformat(key): wh for key, wh in self.measured.items()}

    def prune(self, now: datetime) -> None:
        """Drop what is older than two days."""
        limit = _key(now - KEEP)
        for kept in (self.hours, self.measured):
            for key in [k for k in kept if k < limit]:
                del kept[key]

    # ---------------------------------------------------------- persistence

    def as_dict(self) -> dict:
        return {"fingerprint": self.fingerprint, "hours": self.hours, "measured": self.measured}

    @classmethod
    def from_dict(cls, data: object, fingerprint: str) -> Frozen:
        """What was kept, when it belongs to this plant and meter; otherwise a fresh start."""
        if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
            return cls(fingerprint=fingerprint)
        try:
            hours = {_key(datetime.fromisoformat(str(key))): [float(v) for v in values] for key, values in (data.get("hours") or {}).items()}
            measured = {_key(datetime.fromisoformat(str(key))): float(value) for key, value in (data.get("measured") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            return cls(fingerprint=fingerprint)
        return cls(fingerprint=fingerprint, hours=hours, measured=measured)
