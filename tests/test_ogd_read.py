"""The forecast-file reader against files served from memory: the size
of a file, the run discovery, and the ways one point's rows are read out
of a forecast file with byte ranges, whatever order the file has."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import cache

import aiohttp
import pytest

from custom_components.swiss_solar_forecast import ogd
from custom_components.swiss_solar_forecast.const import COLLECTION_FORECAST, STAC_BASE
from custom_components.swiss_solar_forecast.ogd import (
    OgdError,
    OgdUnavailable,
    ReadStats,
    _RangeFile,
    async_catalogue_latest_run,
    async_fetch_forecast,
    async_find_latest_run,
    async_read_point,
    forecast_file_url,
)

from .fakes import FakeResponse, FakeSession

RUN = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
HEADER = b"point_id;point_type_id;Date;value\n"

# An invented country: 300 stations, 1500 postal codes, 300 mountain spots.
STATIONS = [(number, 1) for number in range(1, 301)]
POSTAL = [(100100 + number * 500, 2) for number in range(1500)]
MOUNTAINS = [(number, 3) for number in range(1, 301)]
POINTS = STATIONS + POSTAL + MOUNTAINS
ZURICH = (800100, 2)
NOWHERE = (800101, 2)

EVERYWHERE = [(1, 1), (300, 1), (100100, 2), ZURICH, (849600, 2), (1, 3), (300, 3)]

# Station numbers of one to five digits, listed the way some files list
# them: as text (1, 10, 100, 1000, 10000, 10001 … 1001, 10010 …).
NUMBERS = [*range(1, 400), *range(1000, 1100), *range(10000, 11200)]
AS_TEXT = sorted(((number, 1) for number in NUMBERS), key=lambda point: str(point[0]))
MOUNTAINS_AS_TEXT = sorted(MOUNTAINS, key=lambda point: str(point[0]))

# The other way files list them: by number, stations and mountain spots
# mixed (a number can be both), the postal codes after them, and at the
# very end three spots the list has out of order.
OUT_OF_ORDER = [(230, 3), (242, 3), (213, 3)]
BY_NUMBER = (
    sorted([*((number, 1) for number in NUMBERS), *((number, 3) for number in range(150, 1100) if (number, 3) not in OUT_OF_ORDER)])
    + POSTAL
    + OUT_OF_ORDER
)


def stamps(count: int, step: timedelta = HOUR) -> list[datetime]:
    return [RUN + step * index for index in range(count)]


def value(point: tuple[int, int], index: int) -> float:
    return float(point[0] % 97) + index * 0.5


def row(point: tuple[int, int], when: datetime, index: int) -> bytes:
    return f"{point[0]};{point[1]};{when:%Y%m%d%H%M};{value(point, index)}\n".encode()


def expected(point: tuple[int, int], times: list[datetime]) -> list[tuple[datetime, float]]:
    return [(when, value(point, index)) for index, when in enumerate(times)]


def point_major(times: list[datetime], points: list[tuple[int, int]] | None = None) -> bytes:
    """All rows of a point together, the points grouped by type."""
    return HEADER + b"".join(row(point, when, index) for point in points or POINTS for index, when in enumerate(times))


def date_major(
    times: list[datetime],
    padding: dict[int, int] | None = None,
    until: dict[tuple[int, int], int] | None = None,
    points: list[tuple[int, int]] | None = None,
) -> bytes:
    """All points of a timestamp together. `padding` puts that many rows of
    an unrelated type at the start of a block (the block gets longer than
    its neighbours); `until` drops a point from that block on."""
    parts = [HEADER]
    for index, when in enumerate(times):
        for number in range((padding or {}).get(index, 0)):
            parts.append(f"{number};9;{when:%Y%m%d%H%M};0.0\n".encode())
        for point in points or POINTS:
            if until and index >= until.get(point, len(times)):
                continue
            parts.append(row(point, when, index))
    return b"".join(parts)


@cache
def hourly_by_point() -> bytes:
    return point_major(stamps(12))


@cache
def daily_by_date() -> bytes:
    return date_major(stamps(8, DAY))


@cache
def hourly_by_date() -> bytes:
    return date_major(stamps(64))


async def read(body: bytes, point: tuple[int, int], param: str, **session_options):
    url = forecast_file_url(RUN, param)
    session = FakeSession({url: body}, **session_options)
    stats = ReadStats()
    rows = await async_read_point(session, url, point, param, stats)
    return rows, stats


def range_file(body: bytes, session: FakeSession | None = None) -> _RangeFile:
    url = "https://example.invalid/file.csv"
    return _RangeFile(session or FakeSession({url: body}), url, len(body), ReadStats())


# --- plain fetches ---------------------------------------------------------------


async def test_size_from_head_or_from_a_one_byte_range() -> None:
    url = "https://example.invalid/file.csv"
    body = b"x" * 4321
    assert await ogd._head_size(FakeSession({url: body}), url) == 4321
    assert await ogd._head_size(FakeSession(), url) is None
    assert await ogd._head_size(FakeSession({url: FakeResponse(403)}), url) is None

    # HEAD without a length: the total comes from the range answer ...
    session = FakeSession({url: body}, head_length=False)
    assert await ogd._head_size(session, url) == 4321
    assert session.calls[-1][2]["Range"] == "bytes=0-0"
    # ... or from a full answer of a host that ignores ranges
    assert await ogd._head_size(FakeSession({url: body}, head_length=False, honour_range=False), url) == 4321

    def gone_after_head(method: str, _url: str, _headers: dict[str, str]) -> FakeResponse:
        return FakeResponse(200) if method == "HEAD" else FakeResponse(404)

    assert await ogd._head_size(FakeSession({url: gone_after_head}), url) is None


async def test_size_failures() -> None:
    url = "https://example.invalid/file.csv"
    with pytest.raises(OgdUnavailable, match="HTTP 500 for file.csv"):
        await ogd._head_size(FakeSession({url: FakeResponse(500)}), url)
    with pytest.raises(OgdUnavailable, match="no size for file.csv"):
        await ogd._head_size(FakeSession({url: FakeResponse(200)}), url)
    with pytest.raises(OgdUnavailable, match="ClientError for file.csv"):
        await ogd._head_size(FakeSession({url: aiohttp.ClientError("boom")}), url)


async def test_latest_run_is_probed_backwards() -> None:
    now = RUN + timedelta(hours=2, minutes=30)
    session = FakeSession({forecast_file_url(RUN, "jp2000d0"): b"x" * 100})
    assert await async_find_latest_run(session, now) == RUN
    assert session.urls("HEAD") == [forecast_file_url(RUN + HOUR * back, "jp2000d0") for back in (2, 1, 0)]

    nothing = FakeSession()
    assert await async_find_latest_run(nothing, now) is None
    assert len(nothing.calls) == 7
    assert await async_find_latest_run(session, now, max_back=1) is None

    stats = ReadStats()
    assert await async_find_latest_run(session, now, stats=stats) == RUN
    assert stats.requests == 3  # the two hours without a run, then the run


async def test_latest_run_from_the_catalogue() -> None:
    url = f"{STAC_BASE}/{COLLECTION_FORECAST}/items?limit=3"
    catalogue = {
        "features": [
            {"assets": {"vnut12.lssw.202611080900.tre200h0.csv": {}, "vnut12.lssw.202611081000.tre200h0.csv": {}, "readme.txt": {}}},
            {"assets": {"vnut12.lssw.202611080800.tre200h0.csv": {}}},
            {},
        ]
    }
    assert await async_catalogue_latest_run(FakeSession({url: json.dumps(catalogue).encode()})) == RUN + HOUR
    assert await async_catalogue_latest_run(FakeSession({url: b'{"features": []}'})) is None
    with pytest.raises(OgdError, match="STAC answer"):
        await async_catalogue_latest_run(FakeSession({url: b"<html>"}))
    with pytest.raises(OgdError, match="STAC answer"):
        await async_catalogue_latest_run(FakeSession({url: b'{"other": 1}'}))


# --- byte ranges -------------------------------------------------------------------


async def test_range_read_counts_and_clips() -> None:
    body = bytes(range(256)) * 4
    rf = range_file(body)
    assert await rf.read(10, 5) == body[10:15]
    assert await rf.read(-20, 5) == body[0:5]
    assert await rf.read(1020, 100) == body[1020:]
    assert await rf.read(5000, 10) == b""
    assert rf.stats.requests == 3
    assert rf.stats.bytes == 5 + 5 + 4

    # A host that ignores the range sends everything: the slice is taken.
    url = "https://example.invalid/file.csv"
    whole = range_file(body, FakeSession({url: body}, honour_range=False))
    assert await whole.read(10, 5) == body[10:15]
    assert whole.stats.bytes == len(body)


async def test_range_read_failures() -> None:
    url = "https://example.invalid/file.csv"
    with pytest.raises(OgdUnavailable, match="HTTP 503 for file.csv"):
        await range_file(b"abc", FakeSession({url: FakeResponse(503)})).read(0, 3)
    with pytest.raises(OgdUnavailable, match="ClientError for file.csv"):
        await range_file(b"abc", FakeSession({url: aiohttp.ClientError("boom")})).read(0, 3)


async def test_first_row_at_an_offset() -> None:
    body = HEADER + row(ZURICH, RUN, 0) + row(ZURICH, RUN + HOUR, 1)
    rf = range_file(body)
    # from the start the header is skipped, from inside a row the next one is taken
    assert await ogd._row_at(rf, 0) == ((800100, 2, "202611080900"), len(HEADER))
    second = len(HEADER) + len(row(ZURICH, RUN, 0))
    assert await ogd._row_at(rf, len(HEADER) + 3) == ((800100, 2, "202611081000"), second)

    # no line end in sight, even in the larger read
    assert await ogd._row_at(range_file(b"x" * 20000), 10) == (None, 10)
    # nothing but lines that are no data rows
    key, _offset = await ogd._row_at(range_file(b"a\n" + b"junk\n" * 6), 1)
    assert key is None
    # the row after the offset is cut off
    key, _offset = await ogd._row_at(range_file(b"a\n800100;2;2026"), 1)
    assert key is None


def test_rows_of_a_chunk_without_a_line_end() -> None:
    assert ogd._rows_of(b"800100;2;202611080900;1.0", 0, ZURICH, False) == []


# --- reading one point ---------------------------------------------------------------


async def test_small_file_is_read_whole() -> None:
    times = stamps(5)
    body = point_major(times, [(1, 1), ZURICH, (5, 3)])
    rows, stats = await read(body, ZURICH, "rre150h0")
    assert rows == expected(ZURICH, times)
    assert stats.requests == 2  # the size and the file
    assert stats.bytes == len(body)

    with pytest.raises(OgdError, match="point 2/800101 not in"):
        await read(body, NOWHERE, "rre150h0")


async def test_missing_file() -> None:
    url = forecast_file_url(RUN, "rre150h0")
    with pytest.raises(OgdUnavailable, match="missing file"):
        await async_read_point(FakeSession(), url, ZURICH, "rre150h0", ReadStats())


@pytest.mark.parametrize("point", EVERYWHERE)
async def test_rows_grouped_by_point(point: tuple[int, int]) -> None:
    body = hourly_by_point()
    assert len(body) > ogd._SMALL_FILE
    rows, stats = await read(body, point, "rre150h0")
    assert rows == expected(point, stamps(12))
    assert stats.bytes < len(body) / 5


async def test_rows_grouped_by_point_without_the_point() -> None:
    with pytest.raises(OgdError):
        await read(hourly_by_point(), NOWHERE, "rre150h0")


@pytest.mark.parametrize(
    "point", [(1, 1), (11, 1), (103, 1), (399, 1), (1024, 1), (10239, 1), (10778, 1), (11199, 1), ZURICH, (1, 3), (31, 3), (300, 3)]
)
async def test_rows_grouped_by_point_with_the_numbers_sorted_as_text(point: tuple[int, int]) -> None:
    """Some of the published files list the station points as text. A
    search by number misses many of them there; the search by text finds
    every one, with a handful of requests."""
    times = stamps(9, DAY)
    body = point_major(times, AS_TEXT + POSTAL + MOUNTAINS_AS_TEXT)
    assert len(body) > ogd._SMALL_FILE
    rows, stats = await read(body, point, "rka150p0")
    assert rows == expected(point, times)
    assert stats.requests < 80
    assert stats.bytes < len(body) / 5


async def test_rows_sorted_as_text_without_the_point() -> None:
    body = point_major(stamps(9, DAY), AS_TEXT + POSTAL + MOUNTAINS_AS_TEXT)
    with pytest.raises(OgdError):
        await read(body, (5000, 1), "rka150p0")


MIXED = [(1, 1), (150, 1), (150, 3), (399, 1), (1099, 3), (10778, 1), (11199, 1), (100100, 2), ZURICH, (849600, 2)]


@pytest.mark.parametrize("point", MIXED)
async def test_rows_grouped_by_point_with_the_types_mixed(point: tuple[int, int]) -> None:
    """Other files list the points by number, whatever their type: a
    station and a mountain spot stand next to each other, the postal
    codes follow because their numbers are the longest."""
    times = stamps(9, DAY)
    body = point_major(times, BY_NUMBER)
    assert ogd._SMALL_FILE < len(body) <= ogd._DAILY_FILE
    rows, stats = await read(body, point, "jp2000d0")
    assert rows == expected(point, times)
    assert stats.requests < 80
    assert stats.bytes < len(body) / 5


@pytest.mark.parametrize("point", OUT_OF_ORDER)
async def test_a_point_out_of_order_is_found_by_reading_the_daily_file(point: tuple[int, int]) -> None:
    """No search finds a point that is not where the order puts it; a
    file of daily values is small enough to be read whole instead."""
    times = stamps(9, DAY)
    body = point_major(times, BY_NUMBER)
    rows, stats = await read(body, point, "jp2000d0")
    assert rows == expected(point, times)
    assert len(body) <= stats.bytes < 2 * len(body)
    assert stats.requests < 80


async def test_a_point_out_of_order_in_a_large_file_is_given_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file of hourly values has 30 MB and is never read whole: the
    parameter is missing for that point, after a bounded number of
    requests."""
    monkeypatch.setattr(ogd, "_DAILY_FILE", ogd._SMALL_FILE)
    body = point_major(stamps(9, DAY), BY_NUMBER)
    url = forecast_file_url(RUN, "jww003i0")
    stats = ReadStats()
    with pytest.raises(OgdError):
        await async_read_point(FakeSession({url: body}), url, OUT_OF_ORDER[0], "jww003i0", stats)
    assert stats.requests < 150
    assert stats.bytes < len(body)


@pytest.mark.parametrize("point", [(1, 1), (120, 1), (10778, 1)])
async def test_block_reader_ends_on_a_file_grouped_by_point(point: tuple[int, int]) -> None:
    """The reader for files grouped by date is the second try on a file
    grouped by point. Every row is a block of its own there and the
    windows keep covering the rows already found: it must return them
    once and stop, not ask for the same bytes for ever."""
    times = stamps(9, DAY)
    rf = range_file(point_major(times, AS_TEXT + POSTAL))
    rows = await ogd._read_date_major(rf, point)
    assert ogd._parse_rows(rows) == expected(point, times)
    assert len(rows) == len(times)
    assert rf.stats.requests < 40


async def test_a_read_has_a_request_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a search does, one read of a file stops asking after a
    fixed number of requests."""
    monkeypatch.setattr(ogd, "_MAX_READS", 3)
    rf = range_file(bytes(4096))
    for start in (0, 100, 200):
        await rf.read(start, 10)
    with pytest.raises(OgdError, match="too many requests for file.csv"):
        await rf.read(300, 10)
    assert rf.stats.requests == 3


@pytest.mark.parametrize("point", [(104, 2), (130, 2), (158, 2)])
async def test_block_longer_than_the_first_window(point: tuple[int, int]) -> None:
    """A point with more rows than one read holds: the window is widened."""
    times = stamps(800, timedelta(minutes=1))
    body = point_major(times, [(100 + number, 2) for number in range(60)])
    rows, stats = await read(body, point, "rre150h0")
    assert rows == expected(point, times)
    assert stats.bytes < len(body) / 5


@pytest.mark.parametrize("point", EVERYWHERE)
async def test_daily_rows_grouped_by_date(point: tuple[int, int]) -> None:
    body = daily_by_date()
    assert ogd._SMALL_FILE < len(body) <= 3 * 1024 * 1024
    rows, stats = await read(body, point, "tre200h0")
    assert rows == expected(point, stamps(8, DAY))
    assert stats.bytes < len(body) / 2


@pytest.mark.parametrize("point", MIXED)
async def test_daily_rows_grouped_by_date_with_the_types_mixed(point: tuple[int, int]) -> None:
    """The daily temperature files: one block per day, the points of a
    block listed by number with the types mixed."""
    times = stamps(9, DAY)
    body = date_major(times, points=BY_NUMBER)
    assert ogd._SMALL_FILE < len(body) <= ogd._DAILY_FILE
    rows, stats = await read(body, point, "tre200h0")
    assert rows == expected(point, times)
    assert stats.bytes < len(body) / 2


@pytest.mark.parametrize("point", OUT_OF_ORDER)
async def test_daily_rows_of_a_point_out_of_order(point: tuple[int, int]) -> None:
    """The spots at the end of every block, out of order: the search in
    the first block finds nothing and the file is read whole."""
    times = stamps(9, DAY)
    body = date_major(times, points=BY_NUMBER)
    rows, stats = await read(body, point, "tre200h0")
    assert rows == expected(point, times)
    assert stats.requests < 80


async def test_daily_rows_of_the_last_point_with_a_shorter_last_block() -> None:
    """The last block is shorter than the one before: for the last point
    of a block the predicted place lies past the end of the file. The
    last day must not be lost for that."""
    times = stamps(9, DAY)
    last = POINTS[-1]
    body = date_major(times, until={point: 8 for point in STATIONS[:40]})
    rows, _stats = await read(body, last, "tre200h0")
    assert rows == expected(last, times)


async def test_daily_rows_with_a_block_out_of_step() -> None:
    """One block is much longer than the others: the prediction misses and
    every block is searched instead."""
    times = stamps(8, DAY)
    rows, _stats = await read(date_major(times, padding={4: 700}), ZURICH, "tre200h0")
    assert rows == expected(ZURICH, times)


async def test_daily_rows_without_the_point() -> None:
    with pytest.raises(OgdError, match="point 2/800101 not in"):
        await read(daily_by_date(), NOWHERE, "tre200h0")


async def test_date_major_helpers() -> None:
    times = stamps(8, DAY)
    assert await ogd._read_block_search(range_file(daily_by_date()), ZURICH) == [
        row(ZURICH, when, index).rstrip() for index, when in enumerate(times)
    ]
    # a single timestamp block: nothing to predict, the file is read whole
    single = date_major(stamps(1))
    assert await ogd._read_date_major_small(range_file(single), ZURICH) == [row(ZURICH, RUN, 0).rstrip()]
    # no row of the wanted timestamp follows: the upper bound is the answer
    assert await ogd._date_boundary(range_file(single), 0, len(single), "999999999999") == len(single)


@pytest.mark.parametrize("point", EVERYWHERE)
async def test_hourly_rows_grouped_by_date(point: tuple[int, int]) -> None:
    body = hourly_by_date()
    assert len(body) > 3 * 1024 * 1024
    rows, stats = await read(body, point, "tre200h0")
    assert rows == expected(point, stamps(64))
    assert stats.bytes < len(body) / 2


@pytest.mark.parametrize("padding", [{13: 300}, {20: 300}, {11: 300, 30: 300}])
async def test_hourly_rows_with_blocks_out_of_step(padding: dict[int, int]) -> None:
    """Longer blocks move every later row: the reader finds its way back."""
    times = stamps(64)
    rows, _stats = await read(date_major(times, padding=padding), ZURICH, "tre200h0")
    assert rows == expected(ZURICH, times)


async def test_hourly_rows_that_end_early() -> None:
    """The point is dropped from the file after some blocks: the reader
    gives up after a few misses and returns what it found."""
    times = stamps(64)
    rows, stats = await read(date_major(times, until={ZURICH: 14}), ZURICH, "tre200h0")
    assert rows == expected(ZURICH, times)[:14]
    assert stats.bytes < 1024 * 1024


async def test_hourly_rows_without_the_point() -> None:
    with pytest.raises(OgdError, match="first blocks of a date-major file"):
        await read(hourly_by_date(), NOWHERE, "tre200h0")


# --- a whole run -----------------------------------------------------------------------


async def test_forecast_lists_what_could_not_be_read() -> None:
    times = stamps(5)
    body = point_major(times, [ZURICH])
    session = FakeSession(
        {
            forecast_file_url(RUN, "rre150h0"): body,
            forecast_file_url(RUN, "fu3010h0"): body,
            forecast_file_url(RUN, "ods000h0"): point_major(times, [(1, 1)]),
        }
    )
    data = await async_fetch_forecast(session, RUN, ZURICH, ("rre150h0", "fu3010h0", "ods000h0", "gre000h0"))
    assert data.run == RUN
    assert data.point == ZURICH
    assert set(data.series) == {"rre150h0", "fu3010h0"}
    assert data.series["rre150h0"] == expected(ZURICH, times)
    assert sorted(data.missing) == ["gre000h0", "ods000h0"]
    assert data.stats.requests == 7  # four sizes, three files
    assert data.stats.bytes > 0

    with pytest.raises(OgdUnavailable, match="no forecast file of run 2026-11-08 09:00"):
        await async_fetch_forecast(FakeSession(), RUN, ZURICH, ("rre150h0", "fu3010h0"))
