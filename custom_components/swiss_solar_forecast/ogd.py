"""Client for the MeteoSwiss open data files on data.geo.admin.ch.

One kind of file is read, the local forecast: one CSV per parameter and model run, covering all
  5800 forecast points of Switzerland — 30 to 34 MB for an hourly parameter.
  Downloading them would cost hundreds of megabytes a day, so this module
  reads only the rows of one point with HTTP Range requests. Rows are
  grouped by point in most files (a binary search finds the block in some
  25 small requests, about 40 KB in all) and by timestamp in a few (the
  point's row sits at a fixed position inside every block, so the position
  of the first block predicts all the others — but every one of the 200
  blocks needs a read of its own, some 2.3 MB in all, which is why the
  coordinator keeps such a series for hours instead of reading it with
  every run). Both layouts are stable per parameter; the layout is detected
  on every read anyway.

A second, small kind of file serves the short-term correction: what one
measurement station of the SwissMetNet network recorded today, in steps
of ten minutes (some 20 KB at the end of the day, read whole).

The run of a forecast file is part of its name, so the newest run is found
by probing the current hour and the hours before it with HEAD requests —
no catalogue query is needed. Pure standard library plus aiohttp (which
Home Assistant ships); CSV parsing happens on the few rows that were read.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    COLLECTION_STATIONS,
    DATA_BASE,
    DATE_MAJOR_PARAMS,
    FORECAST_META_POINTS_URL,
    PARAM_STATION_GLOBAL,
    RADIATION_POINT_TYPE,
    STAC_BASE,
    STATIONS_META_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# identity: a compressed transfer would make byte ranges meaningless
_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
_TIMEOUT = aiohttp.ClientTimeout(total=60, sock_connect=15)
_TIMEOUT_STATION = aiohttp.ClientTimeout(total=20, sock_connect=10)  # a small file that is asked for again soon
_PROBE = 1024  # bytes read per binary-search probe
_WINDOW = 3 * 1024  # bytes read around a predicted row in a date-major file
_SMALL_FILE = 300 * 1024  # below this a single read beats any search
_MAX_PARALLEL = 6  # concurrent Range requests against one file
_MAX_PARALLEL_FILES = 3  # forecast files read at once (each with up to _MAX_PARALLEL ranges)


class OgdError(Exception):
    """A file could not be read or made no sense."""


class OgdUnavailable(OgdError):
    """The service answered with an error or did not answer."""


# --- Points and stations -----------------------------------------------------


@dataclass(frozen=True)
class Point:
    """A forecast point: a postal-code centre, a station or a mountain spot."""

    point_type: int
    point_id: int
    name: str
    postal_code: str
    station_abbr: str
    height: float | None
    lat: float
    lon: float

    @property
    def key(self) -> tuple[int, int]:
        return self.point_id, self.point_type


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, good enough to rank stations and points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_csv(body: bytes) -> list[dict[str, str]]:
    """The semicolon-separated, Latin-1 encoded CSV MeteoSwiss publishes."""
    text = body.decode("latin-1")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


async def _fetch(session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout = _TIMEOUT) -> bytes:
    try:
        async with session.get(url, headers=_HEADERS, timeout=timeout) as resp:
            if resp.status != 200:
                raise OgdUnavailable(f"HTTP {resp.status} for {url.rsplit('/', 1)[-1]}")
            return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise OgdUnavailable(f"{type(err).__name__} for {url.rsplit('/', 1)[-1]}") from err


async def async_fetch_points(session: aiohttp.ClientSession) -> list[Point]:
    """All forecast points with their coordinates (about 800 KB, read at setup)."""
    rows = _parse_csv(await _fetch(session, FORECAST_META_POINTS_URL))
    points: list[Point] = []
    for row in rows:
        try:
            lat, lon = float(row["point_coordinates_wgs84_lat"]), float(row["point_coordinates_wgs84_lon"])
            points.append(
                Point(
                    point_type=int(row["point_type_id"]),
                    point_id=int(row["point_id"]),
                    name=row.get("point_name", "").strip(),
                    postal_code=row.get("postal_code", "").strip(),
                    station_abbr=row.get("station_abbr", "").strip(),
                    height=_float(row.get("point_height_masl")),
                    lat=lat,
                    lon=lon,
                )
            )
        except (KeyError, ValueError):
            continue
    if not points:
        raise OgdError("the forecast point list is empty")
    return points


def nearest(items, lat: float, lon: float, count: int = 3):
    """The `count` items closest to a location, with their distance in km."""
    ranked = sorted(((distance_km(lat, lon, i.lat, i.lon), i) for i in items), key=lambda x: x[0])
    return ranked[:count]


def nearest_radiation_point(points: list[Point], lat: float, lon: float) -> Point:
    """The closest forecast point that carries radiation. A measurement
    station or a mountain point may lie closer to a plant than the centre
    of its postal code, and the list of points names them all alike."""
    usable = [p for p in points if p.point_type == RADIATION_POINT_TYPE]
    if not usable:
        raise OgdError("the forecast point list names no postal-code centre")
    return nearest(usable, lat, lon, 1)[0][1]


@dataclass(frozen=True)
class Station:
    """One measurement station of the SwissMetNet network."""

    abbr: str
    name: str
    height: float | None
    lat: float
    lon: float


async def async_fetch_stations(session: aiohttp.ClientSession) -> tuple[list[Station], int]:
    """All measurement stations with their coordinates, and the bytes read (about 150 KB)."""
    body = await _fetch(session, STATIONS_META_URL)
    stations: list[Station] = []
    for row in _parse_csv(body):
        try:
            abbr = row["station_abbr"].strip()
            lat, lon = float(row["station_coordinates_wgs84_lat"]), float(row["station_coordinates_wgs84_lon"])
        except (KeyError, ValueError, AttributeError):
            continue
        if abbr:
            stations.append(Station(abbr=abbr, name=(row.get("station_name") or "").strip(), height=_float(row.get("station_height_masl")), lat=lat, lon=lon))
    if not stations:
        raise OgdError("the station list is empty")
    return stations, len(body)


def station_now_url(abbr: str) -> str:
    """Today's ten-minute values of one station (the file starts anew at 00:00 UTC)."""
    abbr = abbr.lower()
    return f"{DATA_BASE}/{COLLECTION_STATIONS}/{abbr}/ogd-smn_{abbr}_t_now.csv"


def parse_station_radiation(body: bytes) -> list[tuple[datetime, float]]:
    """The global radiation in a station's ten-minute file: (end of the ten
    minutes in UTC, W/m²), oldest first. A station without a radiation
    sensor leaves the column empty and yields nothing."""
    values: list[tuple[datetime, float]] = []
    for row in _parse_csv(body):
        value = _float((row.get(PARAM_STATION_GLOBAL) or "").strip())
        if value is None:
            continue
        try:
            end = datetime.strptime((row.get("reference_timestamp") or "").strip(), "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
        except ValueError:
            continue
        values.append((end, value))
    values.sort()
    return values


async def async_fetch_station_radiation(session: aiohttp.ClientSession, abbr: str) -> tuple[list[tuple[datetime, float]], int]:
    """What a station measured today, and the bytes read."""
    body = await _fetch(session, station_now_url(abbr), _TIMEOUT_STATION)
    return parse_station_radiation(body), len(body)


# --- Forecast runs -----------------------------------------------------------


def forecast_file_url(run: datetime, param: str) -> str:
    """The file of one parameter of one model run (run in UTC, whole hour)."""
    day = run.strftime("%Y%m%d")
    stamp = run.strftime("%Y%m%d%H%M")
    return f"{DATA_BASE}/{COLLECTION_FORECAST}/{day}-ch/vnut12.lssw.{stamp}.{param}.csv"


async def _head_size(session: aiohttp.ClientSession, url: str) -> int | None:
    """The file's size, or None when it does not exist (yet). A HEAD
    answer without Content-Length is followed by a one-byte range request,
    whose Content-Range names the total."""
    name = url.rsplit("/", 1)[-1]
    try:
        async with session.head(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
            if resp.status in (403, 404):
                return None
            if resp.status != 200:
                raise OgdUnavailable(f"HTTP {resp.status} for {name}")
            if resp.headers.get("Content-Length"):
                return int(resp.headers["Content-Length"])
        async with session.get(url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=_TIMEOUT) as resp:
            if resp.status in (403, 404):
                return None
            total = resp.headers.get("Content-Range", "").rsplit("/", 1)[-1]
            if resp.status == 206 and total.isdigit():
                return int(total)
            if resp.status == 200 and resp.headers.get("Content-Length"):
                return int(resp.headers["Content-Length"])
            raise OgdUnavailable(f"no size for {name} (HTTP {resp.status})")
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise OgdUnavailable(f"{type(err).__name__} for {name}") from err


async def async_find_latest_run(
    session: aiohttp.ClientSession, now: datetime, probe_param: str = "jp2000d0", max_back: int = 6
) -> datetime | None:
    """The newest run whose files exist, trying the current hour and the
    hours before it. None when none of them exists (an outage, or a gap
    longer than `max_back` hours)."""
    start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for back in range(max_back + 1):
        run = start - timedelta(hours=back)
        if await _head_size(session, forecast_file_url(run, probe_param)) is not None:
            return run
    return None


async def async_catalogue_latest_run(session: aiohttp.ClientSession) -> datetime | None:
    """Fallback discovery through the STAC catalogue, should the file-name
    pattern ever change: the newest run named in the newest day's item."""
    body = await _fetch(session, f"{STAC_BASE}/{COLLECTION_FORECAST}/items?limit=3")
    import json

    try:
        items = json.loads(body)["features"]
    except (ValueError, KeyError) as err:
        raise OgdError("STAC answer is not what was expected") from err
    runs: list[datetime] = []
    for item in items:
        for key in item.get("assets", {}):
            parts = key.split(".")
            if len(parts) >= 4 and len(parts[2]) == 12 and parts[2].isdigit():
                runs.append(datetime.strptime(parts[2], "%Y%m%d%H%M").replace(tzinfo=UTC))
    return max(runs) if runs else None


# --- Reading one point out of a forecast file --------------------------------


@dataclass
class ReadStats:
    """What a read cost — shown as diagnostics so the traffic stays visible."""

    requests: int = 0
    bytes: int = 0


class _RangeFile:
    def __init__(self, session: aiohttp.ClientSession, url: str, size: int, stats: ReadStats) -> None:
        self._session = session
        self._url = url
        self.size = size
        self.stats = stats
        self._sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def read(self, start: int, length: int) -> bytes:
        start = max(0, start)
        end = min(self.size - 1, start + length - 1)
        if end < start:
            return b""
        headers = {**_HEADERS, "Range": f"bytes={start}-{end}"}
        async with self._sem:
            try:
                async with self._session.get(self._url, headers=headers, timeout=_TIMEOUT) as resp:
                    if resp.status not in (200, 206):
                        raise OgdUnavailable(f"HTTP {resp.status} for {self._url.rsplit('/', 1)[-1]}")
                    body = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise OgdUnavailable(f"{type(err).__name__} for {self._url.rsplit('/', 1)[-1]}") from err
        self.stats.requests += 1
        self.stats.bytes += len(body)
        if len(body) > end - start + 1:
            # The server ignored the range and sent everything: use the slice.
            body = body[start : end + 1]
        return body


def _key(line: bytes) -> tuple[int, int, str] | None:
    """(point_id, point_type_id, timestamp) of a data row; None for the header."""
    parts = line.split(b";")
    if len(parts) < 4:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2].decode()
    except ValueError:
        return None


def _rows_of(chunk: bytes, chunk_offset: int, point: tuple[int, int], at_start: bool) -> list[tuple[int, bytes]]:
    """(absolute offset, line) of every complete row of `point` in a chunk."""
    out: list[tuple[int, bytes]] = []
    pos = 0
    if not at_start:
        nl = chunk.find(b"\n")
        if nl < 0:
            return out
        pos = nl + 1
    while True:
        end = chunk.find(b"\n", pos)
        if end < 0:
            break
        line = chunk[pos:end].rstrip(b"\r")
        k = _key(line)
        if k and (k[0], k[1]) == point:
            out.append((chunk_offset + pos, line))
        pos = end + 1
    return out


async def _row_at(rf: _RangeFile, offset: int) -> tuple[tuple[int, int, str] | None, int]:
    """The first complete data row at or after `offset`, with its offset."""
    chunk = await rf.read(offset, _PROBE)
    pos = 0
    if offset > 0:
        nl = chunk.find(b"\n")
        if nl < 0:
            chunk = await rf.read(offset, _PROBE * 8)
            nl = chunk.find(b"\n")
            if nl < 0:
                return None, offset
        pos = nl + 1
    for _ in range(3):  # skip the header line and the odd short row
        end = chunk.find(b"\n", pos)
        if end < 0:
            return None, offset + pos
        k = _key(chunk[pos:end].rstrip(b"\r"))
        if k is not None:
            return k, offset + pos
        pos = end + 1
    return None, offset + pos


async def _read_point_major(rf: _RangeFile, point: tuple[int, int], lo: int = 0, hi: int | None = None) -> list[bytes]:
    return [line for _off, line in await _find_point_block(rf, point, lo, hi)]


async def _find_point_block(
    rf: _RangeFile, point: tuple[int, int], lo: int = 0, hi: int | None = None
) -> list[tuple[int, bytes]]:
    """Binary search for the point's block between `lo` and `hi` (the
    whole file by default). Points are grouped by type (the order of the
    types is whatever the file has) and sorted by id inside a type, so
    the search first narrows to the type's segment with a few probes,
    then bisects by id."""
    pid, ptype = point
    hi = rf.size if hi is None else hi
    span_lo, span_hi = lo, hi
    probes = 12
    table: list[tuple[int, tuple[int, int, str]]] = []
    results = await asyncio.gather(
        *(_row_at(rf, span_lo + i * (span_hi - span_lo) // probes) for i in range(probes))
    )
    for k, off in results:
        if k and off < span_hi:
            table.append((off, k))
    table.sort()
    same = [off for off, k in table if k[1] == ptype]
    if same:
        before = [off for off, k in table if k[1] != ptype and off < same[0]]
        after = [off for off, k in table if k[1] != ptype and off > same[-1]]
        lo = max(before) if before else span_lo
        hi = min(after) if after else span_hi
    found: int | None = None
    for _ in range(48):
        if hi - lo <= 2 * _PROBE:
            break
        mid = (lo + hi) // 2
        k, off = await _row_at(rf, mid)
        if k is None:
            hi = mid
            continue
        if k[1] != ptype:
            # Still outside the type's segment: move towards it.
            if same and off < same[0]:
                lo = off
            else:
                hi = mid
            continue
        if k[0] < pid:
            lo = off
        elif k[0] > pid:
            hi = mid
        else:
            found = off
            break
    centre = found if found is not None else lo
    start, length = max(span_lo, centre - 8 * _PROBE), 16 * _PROBE
    rows: list[tuple[int, bytes]] = []
    for _ in range(8):
        chunk = await rf.read(start, min(length, span_hi + 2 * _PROBE - start))
        rows = [r for r in _rows_of(chunk, start, point, start == 0) if r[0] < span_hi]
        if not rows:
            return []
        first_off, last_off = rows[0][0], rows[-1][0]
        touches_start = start > span_lo and first_off - start < 64
        touches_end = start + length < span_hi and start + length - last_off < 128
        if not touches_start and not touches_end:
            break
        if touches_start:
            start = max(0, start - 8 * _PROBE)
            length += 8 * _PROBE
        if touches_end:
            length += 8 * _PROBE
    return rows


async def _date_samples(rf: _RangeFile, probes: int) -> list[tuple[int, str]]:
    """(offset, timestamp) at evenly spaced offsets, sorted by offset."""
    results = await asyncio.gather(*(_row_at(rf, i * rf.size // probes) for i in range(probes)))
    return sorted((off, k[2]) for k, off in results if k)


async def _date_boundary(rf: _RangeFile, lo: int, hi: int, date_after: str) -> int:
    """The offset where the rows of `date_after` begin, between an offset
    `lo` that lies in the previous timestamp block and `hi` in this one."""
    for _ in range(40):
        if hi - lo <= _PROBE:
            break
        mid = (lo + hi) // 2
        k, off = await _row_at(rf, mid)
        if k is None or k[2] >= date_after:
            hi = mid
        else:
            lo = off
    chunk = await rf.read(lo, hi - lo + 4 * _PROBE)
    pos = chunk.find(b"\n") + 1 if lo > 0 else 0
    while pos < len(chunk):
        end = chunk.find(b"\n", pos)
        if end < 0:
            break
        k = _key(chunk[pos:end].rstrip(b"\r"))
        if k and k[2] >= date_after:
            return lo + pos
        pos = end + 1
    return hi


async def _read_date_major_small(rf: _RangeFile, point: tuple[int, int]) -> list[bytes]:
    """Date-major file with a handful of timestamp blocks (the daily
    temperature files): find the point's row in the first block by
    search, then read a small window at the predicted position in each
    later block, one block at a time, correcting the step with every hit.
    Costs about 80 KB where reading the file would cost 1.3 MB."""
    samples = await _date_samples(rf, 12)
    dates = sorted({d for _o, d in samples})
    if len(dates) < 2:
        return await _read_whole(rf, point)
    lo = max(o for o, d in samples if d == dates[0])
    hi = min(o for o, d in samples if d == dates[1])
    first_end = await _date_boundary(rf, lo, hi, dates[1])
    rows = await _find_point_block(rf, point, 0, first_end)
    if not rows:
        return await _read_block_search(rf, point)
    out = [line for _off, line in rows]
    last_hit = rows[0][0]
    step = first_end
    for _ in range(len(dates) - 1):
        predicted = last_hit + step
        if predicted >= rf.size:
            break
        hit = None
        for window in (4 * 1024, 24 * 1024):
            start = max(0, predicted - window // 2)
            chunk = await rf.read(start, window)
            found = _rows_of(chunk, start, point, start == 0)
            if found:
                hit = found[0]
                break
        if hit is None:
            return await _read_block_search(rf, point)
        out.append(hit[1])
        step = hit[0] - last_hit
        last_hit = hit[0]
    return out


async def _read_block_search(rf: _RangeFile, point: tuple[int, int]) -> list[bytes]:
    """Date-major file with few timestamp blocks (the daily files): find
    every block's start by bisecting on the timestamp, then search the
    point inside each block the way a point-major file is searched."""
    samples = await _date_samples(rf, 36)
    dates = sorted({d for _o, d in samples})
    starts: dict[str, int] = {dates[0]: 0}
    bounds: list[tuple[str, int, int]] = []
    for prev, nxt in zip(dates, dates[1:]):
        lo = max(o for o, d in samples if d == prev)
        hi = min(o for o, d in samples if d == nxt)
        bounds.append((nxt, lo, hi))
    found = await asyncio.gather(*(_date_boundary(rf, lo, hi, d) for d, lo, hi in bounds))
    for (d, _lo, _hi), off in zip(bounds, found):
        starts[d] = off
    ordered = [starts[d] for d in dates] + [rf.size]
    blocks = await asyncio.gather(
        *(_read_point_major(rf, point, lo=ordered[i], hi=ordered[i + 1]) for i in range(len(dates)))
    )
    rows: list[bytes] = []
    for block_rows in blocks:
        rows.extend(block_rows)
    return rows


async def _read_date_major(rf: _RangeFile, point: tuple[int, int]) -> list[bytes]:
    """Date-major file with many blocks (the hourly files): read the first
    half megabyte whole, which gives the point's position inside a block
    and the block length, then read a small window around the predicted
    position in every later block — a few blocks per round, the window
    growing with the distance from the last confirmed row, the step
    re-measured from every hit. The first block can be short (a run
    starts with a partial hour), so the step is taken from a later pair."""
    head = await rf.read(0, 512 * 1024)
    lines = head.split(b"\n")
    offset = 0
    block_starts: list[int] = []
    current_date: str | None = None
    row_pos: int | None = None
    row_block = -1
    rows_out: list[bytes] = []
    for line in lines[:-1]:
        k = _key(line.rstrip(b"\r"))
        if k:
            if k[2] != current_date:
                current_date = k[2]
                block_starts.append(offset)
            if (k[0], k[1]) == point:
                rows_out.append(line.rstrip(b"\r"))
                row_pos = offset - block_starts[-1]
                row_block = len(block_starts) - 1
        offset += len(line) + 1
    if row_pos is None or len(block_starts) < 2:
        raise OgdError("point not found in the first blocks of a date-major file")
    if len(block_starts) >= 3:
        step = block_starts[-1] - block_starts[-2]
    else:
        step = block_starts[1] - block_starts[0]
    last_hit = block_starts[row_block] + row_pos
    predicted = last_hit + step
    strikes = 0
    while predicted < rf.size and strikes < 4:
        # Measured drift of the predicted position: one block ahead a few
        # hundred bytes (1.6 KB at most), four ahead up to 11 KB. Three
        # blocks per round with windows of 4, 8 and 12 KB cover that.
        targets = [predicted + i * step for i in range(3) if predicted + i * step < rf.size]
        if not targets:
            break
        windows = [4 * 1024 * (i + 1) for i in range(len(targets))]
        chunks = await asyncio.gather(*(rf.read(t - w // 2, w) for t, w in zip(targets, windows)))
        hits: list[int] = []
        for t, w, chunk in zip(targets, windows, chunks):
            start = max(0, t - w // 2)
            found = _rows_of(chunk, start, point, start == 0)
            if not found:
                break
            hits.append(found[0][0])
            rows_out.append(found[0][1])
        if hits:
            if len(hits) >= 2:
                step = round((hits[-1] - hits[0]) / (len(hits) - 1))
            else:
                step = hits[0] - last_hit
            last_hit = hits[-1]
            predicted = last_hit + step
            strikes = 0 if len(hits) == len(targets) else strikes + 1
        else:
            t = targets[0]
            start = max(0, t - 32 * 1024)
            chunk = await rf.read(start, 64 * 1024)
            found = _rows_of(chunk, start, point, start == 0)
            if found:
                rows_out.append(found[0][1])
                step = found[0][0] - last_hit
                last_hit = found[0][0]
                predicted = last_hit + step
                strikes = 0
            else:
                strikes += 1
                predicted += step
    return rows_out


async def _read_whole(rf: _RangeFile, point: tuple[int, int]) -> list[bytes]:
    chunk = await rf.read(0, rf.size)
    return [line for _off, line in _rows_of(chunk, 0, point, True)]


def _parse_rows(rows: list[bytes]) -> list[tuple[datetime, float | None]]:
    out: list[tuple[datetime, float | None]] = []
    for line in rows:
        parts = line.split(b";")
        if len(parts) < 4:
            continue
        try:
            ts = datetime.strptime(parts[2].decode(), "%Y%m%d%H%M").replace(tzinfo=UTC)
        except ValueError:
            continue
        out.append((ts, _float(parts[3].decode("latin-1"))))
    out.sort(key=lambda x: x[0])
    return out


async def async_read_point(
    session: aiohttp.ClientSession, url: str, point: tuple[int, int], param: str, stats: ReadStats
) -> list[tuple[datetime, float | None]]:
    """The (timestamp, value) rows of one point in one forecast file."""
    size = await _head_size(session, url)
    stats.requests += 1
    if size is None:
        raise OgdUnavailable(f"missing file {url.rsplit('/', 1)[-1]}")
    rf = _RangeFile(session, url, size, stats)
    if size <= _SMALL_FILE:
        rows = await _read_whole(rf, point)
    elif param in DATE_MAJOR_PARAMS:
        # Few blocks (daily values): search each block; many (hourly): predict.
        if size <= 3 * 1024 * 1024:
            rows = await _read_date_major_small(rf, point)
        else:
            rows = await _read_date_major(rf, point)
    else:
        rows = await _read_point_major(rf, point)
        if not rows:
            # The layout may have changed: try the other strategy once.
            rows = await _read_date_major(rf, point)
    if not rows:
        raise OgdError(f"point {point[1]}/{point[0]} not in {url.rsplit('/', 1)[-1]}")
    return _parse_rows(rows)


@dataclass
class ForecastData:
    """One point's values of one model run."""

    run: datetime
    point: tuple[int, int]
    series: dict[str, list[tuple[datetime, float | None]]] = field(default_factory=dict)
    stats: ReadStats = field(default_factory=ReadStats)
    missing: list[str] = field(default_factory=list)


async def async_fetch_forecast(
    session: aiohttp.ClientSession, run: datetime, point: tuple[int, int], params: tuple[str, ...]
) -> ForecastData:
    """All requested parameters of a run for a point. A parameter whose
    file is missing or unreadable is listed in `missing` rather than
    failing the whole fetch; the entities then show what there is."""
    data = ForecastData(run=run, point=point)
    limit = asyncio.Semaphore(_MAX_PARALLEL_FILES)

    async def one(param: str) -> None:
        try:
            async with limit:
                data.series[param] = await async_read_point(session, forecast_file_url(run, param), point, param, data.stats)
        except OgdError as err:
            _LOGGER.debug("Forecast %s for %s not read: %s", param, point, err)
            data.missing.append(param)

    await asyncio.gather(*(one(p) for p in params))
    if not data.series:
        raise OgdUnavailable(f"no forecast file of run {run:%Y-%m-%d %H:%M} could be read")
    return data
