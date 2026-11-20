"""The part of the sky a measurement station sees the sun in.

A station's radiation sensor stands somewhere too: beside a tree, a mast
or a building. While the sun is behind such a thing the sensor receives
the light of the sky alone, or what gets through the branches: a fraction
of what a plant a few kilometres away is getting at that moment. Taken at
its word, the station then says "overcast" on a cloudless afternoon. The
short-term correction cuts the hours ahead, and the hour teaches the
weather model a fault it did not have.

The station's own record shows where that happens. The sky is laid out in
fields by the sun's compass bearing and height, and every ten-minute value
is set against what a cloudless sky gives at the sun's height. For every
field the days are counted on which the sensor had the sun there, and the
days on which it had not:

* A sunny day is one with hours of full sun and no more than a couple of
  stretches without it. On such a day a stretch without sun is rarely a
  cloud, and what a tree does it does again on the next sunny day at the
  same place of the sun. Such a day counts for every field the sun passed,
  from the day's first half hour of full sun on: as a day in the sun or
  as a day in the shade, by what the sensor received there.
* What comes before that first full sun counts for nothing. A grey start
  of a day is what fog looks like, and fog is what the station is needed
  for.
* Full sun in a field counts on any day: it proves that the sensor sees
  the sun there.

A field with two days in the shade or more, and several times as many of
them as days in the sun, is taken to be in the shade. An obstacle stands
on the ground, so the fields below it that the record says nothing about
go with it, and so do the first few degrees above it: the sun reaches a
new height on some sunny day, and that day must not be the one that
teaches. One value of full sun in such a field sets it free. Values
measured while the sun stood in a shaded field, or right before and
after, are left out of the correction and of what the weather model is
judged by.

Earlier days fade with every new one in the same field: a felled tree is
forgotten on the first sunny day, a new building is learned within a
couple of weeks of them.

Pure arithmetic and plain data, so it can be tested without a network.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .bias import clear_sky
from .solar import sun_position

AZIMUTH_STEP = 5  # degrees of compass bearing per field
BAND = 2.5  # degrees of the sun's height per field
FLOOR = 5.0  # degrees: below it nothing is judged (hills and haze everywhere)
TOP = 45.0  # degrees: nothing is taken to shade a sun that high
SUN_FROM = 0.85  # of the cloudless sky's radiation: full sun
SUN_FROM_LOW = 0.75  # the same for a low sun, whose light no simple formula knows that well
LOW_SUN = 15.0  # degrees: below it the sun counts as low
SURE_SUN = 0.9  # of the cloudless sky's radiation: full sun beyond doubt, at any height
DISTURBED_ABOVE = 1.25  # of the cloudless sky's radiation: light thrown in by cloud edges, no steady sun
DAY_ELEVATION = 10.0  # degrees: the values a day is judged by
DAY_SUN_VALUES = 12  # ten-minute values of full sun a sunny day has at least (two hours)
DAY_STRETCHES = 2  # stretches without full sun a sunny day may have: more mean clouds
DAY_BLIPS = 3  # single values without full sun a sunny day may have: more mean clouds
SUN_FIRST = 3  # values of full sun in a row from which a sunny day counts (half an hour)
SHADED_BELOW = 0.8  # of the cloudless sky's radiation in a field on a sunny day: below it the day was one in the shade
DECAY = 0.9  # weight left of a field's earlier days when a new one comes
MIN_SHADED = 1.9  # days in the shade a field needs: two, the earlier one faded
SUN_COUNTS = 3.0  # a day in the sun against a day in the shade
ABOVE_SHADE = 2  # fields right above shade that go with it while the record says nothing about them (five degrees)
_STEP = timedelta(minutes=10)
_HALF_STEP = _STEP / 2
_BANDS = int((TOP - FLOOR) / BAND)
_SUN, _SHADE = 0, 1


def _column(azimuth: float) -> int:
    return int(azimuth % 360.0 // AZIMUTH_STEP) * AZIMUTH_STEP


def _band(elevation: float) -> int | None:
    """The band of a height of the sun; None below the floor and above the top."""
    if elevation < FLOOR or elevation >= TOP:
        return None
    return int((elevation - FLOOR) // BAND)


@dataclass(frozen=True)
class _Sample:
    end: datetime
    column: int
    band: int | None
    height: float
    share: float  # of the cloudless sky's radiation
    sun: bool  # full sun


def _stretches(samples: list[_Sample]) -> list[tuple[int, int]]:
    """The runs of values without full sun, as (first index, one past the
    last). A gap in the record ends a run."""
    runs: list[tuple[int, int]] = []
    first: int | None = None
    for index, sample in enumerate(samples):
        gap = index > 0 and sample.end - samples[index - 1].end != _STEP
        if first is not None and (sample.sun or gap):
            runs.append((first, index))
            first = None
        if not sample.sun and first is None:
            first = index
    if first is not None:
        runs.append((first, len(samples)))
    return runs


def _sunny(samples: list[_Sample]) -> bool:
    """Whether a day's values show a sunny day: hours of full sun, and no
    more than a couple of stretches and single values without it."""
    high = [s for s in samples if s.height >= DAY_ELEVATION]
    runs = _stretches(high)
    return (
        sum(1 for s in high if s.sun) >= DAY_SUN_VALUES
        and sum(1 for first, last in runs if last - first >= 2) <= DAY_STRETCHES
        and sum(1 for first, last in runs if last - first == 1) <= DAY_BLIPS
    )


def _first_sun(samples: list[_Sample]) -> int | None:
    """The index from which a sunny day counts: the start of its first half hour of full sun."""
    run = 0
    for index, sample in enumerate(samples):
        gap = index > 0 and sample.end - samples[index - 1].end != _STEP
        run = run + 1 if sample.sun and not gap else int(sample.sun)
        if run >= SUN_FIRST:
            return index - SUN_FIRST + 1
    return None


@dataclass
class SkyView:
    """What one station's record tells about the sky around its sensor."""

    station: str = ""  # the station's abbreviation: another station starts afresh
    lat: float = 0.0  # the station's place: where the sun stands follows from it
    lon: float = 0.0
    cells: dict[str, list[float]] = field(default_factory=dict)  # "bearing/band" -> [days in the sun, days in the shade], faded
    covered: str = ""  # the last day (ISO, UTC) taken from the record
    days: int = 0  # days taken in all
    sunny: int = 0  # of them, sunny days
    settled: bool = False  # whether what was learned before the view was known has been set right
    _shaded: dict[int, list[bool]] = field(default_factory=dict, repr=False, compare=False)

    # ---------------------------------------------------------------- record

    def take(self, values: Iterable[tuple[datetime, float]], before: date) -> int:
        """Take the days of a record (end of ten minutes in UTC, W/m²) that
        were not taken yet and are over (earlier than `before`), oldest
        first. Returns the number of days taken."""
        by_day: dict[date, list[tuple[datetime, float]]] = {}
        for end, value in values:
            day = (end - _STEP).astimezone(UTC).date()
            if day < before and day.isoformat() > self.covered:
                by_day.setdefault(day, []).append((end, value))
        for day in sorted(by_day):
            self.add_day(by_day[day])
            self.covered = day.isoformat()
        return len(by_day)

    def add_day(self, values: Iterable[tuple[datetime, float]]) -> bool:
        """One day of the station's ten-minute values. Returns whether it was a sunny day."""
        samples: list[_Sample] = []
        for end, value in sorted(values):
            middle = end - _HALF_STEP
            sun = sun_position(middle, self.lat, self.lon)
            cloudless = clear_sky(middle, self.lat, self.lon) if sun.elevation >= FLOOR else 0.0
            if cloudless <= 0:
                continue
            share = max(0.0, value) / cloudless
            full = SUN_FROM if sun.elevation >= LOW_SUN else SUN_FROM_LOW
            samples.append(_Sample(end, _column(sun.azimuth), _band(sun.elevation), sun.elevation, share, full <= share <= DISTURBED_ABOVE))
        self.days += 1
        first = _first_sun(samples) if _sunny(samples) else None
        shares: dict[tuple[int, int], list[float]] = {}
        for index, sample in enumerate(samples):
            if sample.band is None:
                continue
            if first is not None and index >= first:
                shares.setdefault((sample.column, sample.band), []).append(min(1.0, sample.share))
            elif SURE_SUN <= sample.share <= DISTURBED_ABOVE:
                # full sun proves the view on any day
                shares.setdefault((sample.column, sample.band), []).append(1.0)
        for (column, band), day_shares in shares.items():
            key = f"{column}/{band}"
            was = self.cells.get(key, [0.0, 0.0])
            in_shade = sum(day_shares) / len(day_shares) < SHADED_BELOW
            self.cells[key] = [was[_SUN] * DECAY + (not in_shade), was[_SHADE] * DECAY + in_shade]
        if shares:
            self._shaded = {}
        if first is not None:
            self.sunny += 1
        return first is not None

    # ------------------------------------------------------------------ view

    def _column_shade(self, column: int) -> list[bool]:
        """Per band of a bearing whether the station is in the shade there."""
        if column not in self._shaded:
            # what each field's own days say: in the shade (True), in the sun (False), or nothing sure (None)
            own: list[bool | None] = []
            leaning: list[bool] = []  # days in the shade and hardly any in the sun, but too few of them
            for band in range(_BANDS):
                sun, shade = self.cells.get(f"{column}/{band}") or (0.0, 0.0)
                mostly_shade = shade >= 0.5 and shade >= SUN_COUNTS * sun
                if mostly_shade and shade >= MIN_SHADED:
                    own.append(True)
                elif mostly_shade or max(sun, shade) < 0.5:
                    own.append(None)
                else:
                    own.append(False)
                leaning.append(mostly_shade)
            shaded = [bool(state) for state in own]
            for band in range(_BANDS):
                if own[band] is not None:
                    continue
                above = next((own[b] for b in range(band + 1, _BANDS) if own[b] is not None), False)
                below = next((b for b in reversed(range(band)) if own[b] is not None), None)
                # An obstacle stands on the ground: what is below shade is in the shade. Its top is where the sun
                # was last seen behind it, or a little higher: the fields right above shade go with it until the sun
                # is seen in them, and so does a field that leans to shade.
                over_shade = below is not None and own[below] is True and (leaning[band] or band - below <= ABOVE_SHADE)
                shaded[band] = bool(above) or over_shade
            self._shaded[column] = shaded
        return self._shaded[column]

    def sees(self, when: datetime) -> bool:
        """Whether the station's sensor sees the sun's place at a moment."""
        sun = sun_position(when, self.lat, self.lon)
        if sun.elevation >= TOP:
            return True
        band = _band(sun.elevation)
        return not self._column_shade(_column(sun.azimuth))[band if band is not None else 0]

    def usable(self, values: Iterable[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
        """The ten-minute values (end, W/m²) measured while the sensor saw
        the sun's place. The values before and after must be free as well:
        a shadow's edge does not keep to the fields."""
        return [(end, value) for end, value in values if all(self.sees(end - _HALF_STEP + _STEP * step) for step in (-1, 0, 1))]

    def profile(self) -> dict[int, float]:
        """Compass bearing (lower edge of the field) -> the height of the sun up to which the station is in the shade; only where it is."""
        out: dict[int, float] = {}
        for column in sorted({int(key.partition("/")[0]) for key in self.cells}):
            shade = self._column_shade(column)
            if any(shade):
                out[column] = FLOOR + (max(band for band in range(_BANDS) if shade[band]) + 1) * BAND
        return out

    @property
    def known(self) -> bool:
        """Whether a record was taken at all."""
        return bool(self.covered)

    # ---------------------------------------------------------- persistence

    def as_dict(self) -> dict:
        return {
            "station": self.station,
            "at": [round(self.lat, 3), round(self.lon, 3)],
            "cells": {key: [round(value, 3) for value in cell] for key, cell in self.cells.items() if max(cell) >= 0.01},
            "covered": self.covered,
            "days": self.days,
            "sunny": self.sunny,
            "settled": self.settled,
        }

    @classmethod
    def from_dict(cls, data: object, station: str, lat: float, lon: float) -> SkyView:
        """What was kept, when it belongs to this station; otherwise a fresh start."""
        fresh = cls(station=station, lat=lat, lon=lon)
        if not isinstance(data, dict) or data.get("station") != station or data.get("at") != [round(lat, 3), round(lon, 3)]:
            return fresh
        try:
            cells = {str(key): [float(value) for value in cell] for key, cell in (data.get("cells") or {}).items()}
            covered = str(data.get("covered") or "")
            if covered:
                date.fromisoformat(covered)
            return cls(
                station=station,
                lat=lat,
                lon=lon,
                cells={key: cell for key, cell in cells.items() if len(cell) == 2 and _valid(key)},
                covered=covered,
                days=int(data.get("days") or 0),
                sunny=int(data.get("sunny") or 0),
                settled=bool(data.get("settled")),
            )
        except (AttributeError, TypeError, ValueError):
            return fresh


def _valid(key: str) -> bool:
    column, _, band = key.partition("/")
    if not (column.isdigit() and band.isdigit()):
        return False
    return int(column) < 360 and int(column) % AZIMUTH_STEP == 0 and int(band) < _BANDS
