"""How well the forecast did: what was forecast against what the plant's
yield meter counted, over the last 7 and 30 days.

Two forecasts of every hour are kept until the meter has counted it: the
last one made before the hour was over, and the one that stood when the
local day began (the forecast one plans the next day with). Every full
hour the meter's increase closes the hour, and the day's sums grow: energy
measured, energy forecast, and the hours' absolute errors.

The figures are deliberately plain:

* Day-ahead accuracy: 100 % minus the days' absolute errors (forecast at
  the start of the day against measured) as a share of the measured
  energy. A week of days that were each 10 % off gives 90 %.
* Same-day accuracy: the same with the last forecast of every hour.
* Hourly accuracy: the same hour by hour, which is stricter: a cloud that
  came an hour early counts twice here and not at all in the day's sum.
* Bias: whether the forecast was too high (+) or too low (−) on the whole.

Only days of which nearly every hour was counted enter the figures, so a
day with Home Assistant switched off at noon does not look like a miss.
Pure arithmetic and plain data; the local date comes from the caller."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

PENDING_KEEP = timedelta(hours=48)
DAYS_KEEP = 40
HOURS_COMPLETE = 20  # counted hours from which a day enters the figures

# a day's sums
_MEASURED, _FORECAST, _HOUR_ERROR, _HOURS, _MEASURED_AHEAD, _AHEAD, _AHEAD_HOURS = range(7)


def _key(when: datetime) -> str:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def _share(error: float, measured: float) -> float | None:
    """An accuracy in percent: what is left of 100 after the error's share of the measured energy."""
    if measured <= 0:
        return None
    return round(max(0.0, 100.0 * (1.0 - error / measured)), 1)


@dataclass
class Accuracy:
    """The open hours and the closed days of one plant."""

    fingerprint: str = ""  # of the plant's figures and the meter: a change starts afresh
    pending: dict[str, float] = field(default_factory=dict)  # hour start (ISO, UTC) -> Wh, the last forecast of the hour
    ahead: dict[str, float] = field(default_factory=dict)  # hour start -> Wh, as forecast when its local day began
    days: dict[str, list[float]] = field(default_factory=dict)  # local date -> the day's sums

    def record(self, hours: list[tuple[datetime, float]], now: datetime, local_date: Callable[[datetime], date]) -> None:
        """Keep the forecast (hour start, Wh) of the hours not yet over. The
        day-ahead value of an hour follows the forecast until the hour's
        local day begins and stays as it stood then."""
        today = local_date(now)
        for start, wh in hours:
            if start + timedelta(hours=1) <= now:
                continue
            key = _key(start)
            self.pending[key] = round(wh, 1)
            if local_date(start) > today:
                self.ahead[key] = round(wh, 1)
        limit = _key(now - PENDING_KEEP)
        for kept in (self.pending, self.ahead):
            for key in [k for k in kept if k < limit]:
                del kept[key]

    def measured(self, start: datetime, wh: float, local_date: Callable[[datetime], date]) -> bool:
        """The meter's count of a finished hour; returns whether the hour had a forecast to compare with."""
        key = _key(start)
        forecast = self.pending.pop(key, None)
        ahead = self.ahead.pop(key, None)
        if forecast is None:
            return False
        day = local_date(start)
        sums = self.days.setdefault(day.isoformat(), [0.0] * 7)
        sums[_MEASURED] += wh
        sums[_FORECAST] += forecast
        sums[_HOUR_ERROR] += abs(forecast - wh)
        sums[_HOURS] += 1
        if ahead is not None:
            sums[_MEASURED_AHEAD] += wh
            sums[_AHEAD] += ahead
            sums[_AHEAD_HOURS] += 1
        oldest = (day - timedelta(days=DAYS_KEEP)).isoformat()
        for old in [d for d in self.days if d < oldest]:
            del self.days[old]
        return True

    def figures(self, today: date, days: int) -> dict[str, float | int | None]:
        """The figures over the `days` full days before `today`."""
        first = (today - timedelta(days=days)).isoformat()
        last = today.isoformat()
        window = [sums for day, sums in self.days.items() if first <= day < last and sums[_HOURS] >= HOURS_COMPLETE]
        ahead = [sums for sums in window if sums[_AHEAD_HOURS] >= HOURS_COMPLETE]
        measured = sum(s[_MEASURED] for s in window)
        forecast = sum(s[_FORECAST] for s in window)
        return {
            "day_ahead_accuracy": _share(sum(abs(s[_AHEAD] - s[_MEASURED_AHEAD]) for s in ahead), sum(s[_MEASURED_AHEAD] for s in ahead)),
            "same_day_accuracy": _share(sum(abs(s[_FORECAST] - s[_MEASURED]) for s in window), measured),
            "hourly_accuracy": _share(sum(s[_HOUR_ERROR] for s in window), measured),
            "bias": round(100.0 * (forecast - measured) / measured, 1) if measured > 0 else None,
            "days": len(window),
            "days_day_ahead": len(ahead),
            "forecast_kwh": round(forecast / 1000.0, 2),
            "measured_kwh": round(measured / 1000.0, 2),
        }

    def daily(self, today: date, days: int) -> list[dict[str, float | int | str | None]]:
        """The complete days among the `days` before `today`, a row each, oldest first."""
        first = (today - timedelta(days=days)).isoformat()
        last = today.isoformat()
        rows: list[dict[str, float | int | str | None]] = []
        for day, sums in sorted(self.days.items()):
            if not first <= day < last or sums[_HOURS] < HOURS_COMPLETE:
                continue
            rows.append(
                {
                    "date": day,
                    "measured_kwh": round(sums[_MEASURED] / 1000.0, 2),
                    "forecast_kwh": round(sums[_FORECAST] / 1000.0, 2),
                    "day_ahead_kwh": round(sums[_AHEAD] / 1000.0, 2) if sums[_AHEAD_HOURS] >= HOURS_COMPLETE else None,
                    "hourly_error_kwh": round(sums[_HOUR_ERROR] / 1000.0, 2),
                    "hours": int(sums[_HOURS]),
                }
            )
        return rows

    # ---------------------------------------------------------- persistence

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "pending": self.pending,
            "ahead": self.ahead,
            "days": {day: [round(v, 1) for v in sums] for day, sums in self.days.items()},
        }

    @classmethod
    def from_dict(cls, data: object, fingerprint: str) -> Accuracy:
        """What was kept, when it belongs to this plant and meter; otherwise a fresh start."""
        if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
            return cls(fingerprint=fingerprint)
        try:
            days = {str(day): [float(v) for v in sums] for day, sums in (data.get("days") or {}).items()}
            return cls(
                fingerprint=fingerprint,
                pending={str(key): float(value) for key, value in (data.get("pending") or {}).items()},
                ahead={str(key): float(value) for key, value in (data.get("ahead") or {}).items()},
                days={day: sums for day, sums in days.items() if len(sums) == 7},
            )
        except (AttributeError, TypeError, ValueError):
            return cls(fingerprint=fingerprint)
