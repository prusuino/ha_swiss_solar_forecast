"""Low stratus: the grey lid over the plateau.

In autumn and winter cold air lies between the Jura and the Alps, and a
layer of low stratus (Hochnebel, stratus bas, nebbia alta) or fog closes
it for hours or days while the sky above is clear. For a PV plant these
are hours of their own kind: all light is diffuse, and whether the lid
opens at eleven, at two or not at all is the hardest thing a weather
model has to tell on such a day.

The MeteoSwiss local forecast gives the cloud cover in three layers. An
hour counts as low stratus when

* the low clouds cover most of the sky,
* hardly a cloud stands above them (a front has clouds at every height),
* nothing worth mentioning falls from them, and
* the light the forecast has for the hour is mostly diffuse, where the
  forecast gives the diffuse share.

What is done with it: the hours are shown (how many of them today and
tomorrow, when the lid is expected to open), and the self-calibration
learns them apart from the rest, since a factor learned for a part of the
sky the sun stands in means little when the sun is not seen.

The thresholds are plain meteorological sense, not fitted to a plant.
Pure arithmetic and plain data, so it can be tested without a network.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import date, datetime, timedelta

LOW_FROM = 0.75  # share of the sky the low clouds cover, from which the lid counts as closed
MID_UP_TO = 0.3  # share covered by the medium-high clouds, up to which the sky above counts as open
PRECIPITATION_UP_TO = 0.1  # mm in the hour: more than drizzle is weather, not a lid
DIFFUSE_SHARE_FROM = 0.75  # of the global radiation: below it the forecast lets the sun through
_HOUR = timedelta(hours=1)


def is_low_stratus(
    low: float | None, mid: float | None, precipitation: float | None = None, ghi: float | None = None, dhi: float | None = None
) -> bool:
    """Whether an hour with these forecast values lies under low stratus.
    The cloud covers are shares of the sky (0…1); without them nothing is
    said. Precipitation and radiation only speak against it when known."""
    if low is None or mid is None:
        return False
    if low < LOW_FROM or mid > MID_UP_TO:
        return False
    if precipitation is not None and precipitation > PRECIPITATION_UP_TO:
        return False
    if ghi is not None and dhi is not None and ghi > 0 and dhi / ghi < DIFFUSE_SHARE_FROM:
        return False
    return True


def stratus_hours(
    low: Mapping[datetime, float | None],
    mid: Mapping[datetime, float | None],
    precipitation: Mapping[datetime, float | None] | None = None,
    ghi: Mapping[datetime, float | None] | None = None,
    dhi: Mapping[datetime, float | None] | None = None,
) -> set[datetime]:
    """The starts of the hours under low stratus. The series are hourly
    values kept under the end of their hour, as MeteoSwiss publishes them."""
    precipitation, ghi, dhi = precipitation or {}, ghi or {}, dhi or {}
    return {end - _HOUR for end, value in low.items() if is_low_stratus(value, mid.get(end), precipitation.get(end), ghi.get(end), dhi.get(end))}


def day_hours(stratus: Collection[datetime], daylight: Iterable[datetime], day: date, local: Callable[[datetime], datetime]) -> list[datetime]:
    """The daylight hours of a local day that lie under low stratus, in
    order. `daylight` holds the starts of the hours with light, `local`
    turns a time into local time."""
    return sorted(start for start in daylight if start in stratus and local(start).date() == day)


def clears_at(stratus: Collection[datetime], daylight: Iterable[datetime], day: date, local: Callable[[datetime], datetime]) -> datetime | None:
    """When the lid is expected to open on a local day: the end of its last
    hour under low stratus, when hours of daylight follow it. None without
    such an hour, and when the stratus stays until dusk."""
    lit = sorted(start for start in daylight if local(start).date() == day)
    covered = [start for start in lit if start in stratus]
    if not covered or covered[-1] == lit[-1]:
        return None
    return covered[-1] + _HOUR
