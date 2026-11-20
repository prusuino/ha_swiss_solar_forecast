"""The measurement stations: the list with their places, a station's
ten-minute file and its global radiation, and its record of the year,
whole and by its tail, read with a faked answer."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.swiss_solar_forecast import ogd
from custom_components.swiss_solar_forecast.ogd import (
    OgdError,
    OgdUnavailable,
    Station,
    async_fetch_station_history,
    async_fetch_station_radiation,
    async_fetch_station_record,
    async_fetch_stations,
    distance_km,
    parse_station_radiation,
    parse_station_record,
    station_history_url,
    station_now_url,
    station_record_url,
)

from .conftest import record_file, station_day
from .fakes import FakeSession

STATIONS = (
    "station_abbr;station_name;station_canton;station_height_masl;station_coordinates_wgs84_lat;station_coordinates_wgs84_lon\n"
    "BER;Bern / Zollikofen;BE;553.0;46.990744;7.464061\n"
    "ZER;Zermatt;VS;1638.0;46.029272;7.752433\n"
    "GVE;Genève / Cointrin;GE;;46.247519;6.127742\n"
    ";No name;BE;500.0;46.9;7.4\n"
    "BAD;Broken;BE;500.0;north;7.4\n"
).encode("latin-1")

NOW_FILE = (
    b"station_abbr;reference_timestamp;tre200s0;gre000z0;sre000z0\n"
    b"BER;21.06.2026 10:10;21.4;655;10\n"
    b"BER;21.06.2026 10:00;21.2;640;10\n"
    b"BER;21.06.2026 10:20;21.5;;10\n"
    b"BER;some time;21.5;700;10\n"
    b"BER;21.06.2026 10:30;21.6;-;10\n"
    b"BER;21.06.2026 00:10;14.0;0;0\n"
)


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self) -> bytes:
        return self._body


class _Files:
    """Answers every request with the same file."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        return _Response(self.status, self.body)


def test_a_stations_file_of_today() -> None:
    assert station_now_url("BER") == "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/ogd-smn_ber_t_now.csv"


def test_the_global_radiation_of_a_station() -> None:
    values = parse_station_radiation(NOW_FILE)
    # oldest first; rows without a value or without a time are left out
    assert values == [
        (datetime(2026, 6, 21, 0, 10, tzinfo=UTC), 0.0),
        (datetime(2026, 6, 21, 10, 0, tzinfo=UTC), 640.0),
        (datetime(2026, 6, 21, 10, 10, tzinfo=UTC), 655.0),
    ]
    # a station without a radiation sensor, and a file that is something else
    assert parse_station_radiation(b"station_abbr;reference_timestamp;tre200s0\nXXX;21.06.2026 10:10;21.4\n") == []
    assert parse_station_radiation(b"") == []


async def test_the_list_of_stations() -> None:
    files = _Files(STATIONS)
    stations, nbytes = await async_fetch_stations(files)
    assert nbytes == len(STATIONS) and len(files.calls) == 1
    assert files.calls[0]["url"].endswith("/ch.meteoschweiz.ogd-smn/ogd-smn_meta_stations.csv")
    assert "SwissSolarForecast" in files.calls[0]["headers"]["User-Agent"]
    # rows without an abbreviation or without a place are left out
    assert [station.abbr for station in stations] == ["BER", "ZER", "GVE"]
    assert stations[0] == Station(abbr="BER", name="Bern / Zollikofen", height=553.0, lat=46.990744, lon=7.464061)
    assert stations[2].name == "Genève / Cointrin" and stations[2].height is None
    assert 4.5 < distance_km(46.9480, 7.4474, stations[0].lat, stations[0].lon) < 5.5


async def test_a_list_without_stations_is_an_error() -> None:
    with pytest.raises(OgdError):
        await async_fetch_stations(_Files(b"station_abbr;station_name\n"))
    with pytest.raises(OgdUnavailable):
        await async_fetch_stations(_Files(b"", status=503))


async def test_what_a_station_measured_today() -> None:
    files = _Files(NOW_FILE)
    values, nbytes = await async_fetch_station_radiation(files, "BER")
    assert nbytes == len(NOW_FILE) and len(values) == 3 and values[-1][1] == 655.0
    assert files.calls[0]["url"] == station_now_url("BER")
    with pytest.raises(OgdUnavailable):
        await async_fetch_station_radiation(_Files(b"", status=404), "BER")


# --- a station's record ------------------------------------------------------


def _record(days: int = 6) -> list[tuple[datetime, float]]:
    return [value for n in range(days) for value in station_day(date(2026, 11, 1) + timedelta(days=n), 46.99, 7.46)]


def test_the_record_files_of_a_station() -> None:
    assert station_record_url("BER") == "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/ogd-smn_ber_t_recent.csv"
    # the earlier years lie in files of ten years each; the one asked for holds the year before
    assert station_history_url("BER", 2026).endswith("/ber/ogd-smn_ber_t_historical_2020-2029.csv")
    assert station_history_url("BER", 2030).endswith("_t_historical_2020-2029.csv")
    assert station_history_url("BER", 2031).endswith("_t_historical_2030-2039.csv")


def test_the_radiation_in_a_record() -> None:
    values = _record(2)
    body = record_file(values)
    assert parse_station_record((body, None)) == values
    # a tail starts in the middle of a row: that row is left out, the columns come with it
    cut = body.index(b"\n", len(body) // 2) + 8
    rows_before = body[:cut].count(b"\n") - 1  # complete data rows before the cut-off one
    assert parse_station_record((body[cut:], (1, 4))) == values[rows_before + 1 :]
    # rows without a value or with a broken time are left out
    odd = b"station_abbr;reference_timestamp;gre000z0\nBER;01.11.2026 10:10;200\nBER;01.11.2026 10:20;\nBER;soon;300\nBER;01.11.2026 10:40;-\nshort\n"
    assert parse_station_record((odd, None)) == [(datetime(2026, 11, 1, 10, 10, tzinfo=UTC), 200.0)]
    # a station without a radiation sensor, and nothing at all
    assert parse_station_record((b"station_abbr;reference_timestamp;tre200s0\nBER;01.11.2026 10:10;4.2\n", None)) == []
    assert parse_station_record((b"", None)) == []


async def test_the_whole_record_is_one_request() -> None:
    body = record_file(_record())
    session = FakeSession({station_record_url("BER"): body})
    parts, requests, nbytes = await async_fetch_station_record(session, "BER")
    assert parts == [(body, None)] and (requests, nbytes) == (1, len(body))
    assert session.calls[0][0] == "GET" and "Range" not in session.calls[0][2]
    with pytest.raises(OgdUnavailable):
        await async_fetch_station_record(FakeSession(), "BER")


async def test_the_days_added_since_are_read_as_the_tail(monkeypatch) -> None:
    monkeypatch.setattr(ogd, "_RECORD_DAY", 144 * 60)  # the rows of the faked file are short
    values = _record()
    body = record_file(values)
    session = FakeSession({station_record_url("BER"): body})
    parts, requests, nbytes = await async_fetch_station_record(session, "BER", 2)
    # the size, the header line, the tail
    assert [(verb, headers.get("Range")) for verb, _url, headers in session.calls] == [
        ("HEAD", None),
        ("GET", "bytes=0-2047"),
        ("GET", f"bytes={len(body) - 2 * 144 * 60}-{len(body) - 1}"),
    ]
    assert requests == 3 and nbytes == 2048 + 2 * 144 * 60
    ((tail, columns),) = parts
    assert columns == (1, 4) and body.endswith(tail) and len(tail) == 2 * 144 * 60
    read = parse_station_record(parts[0])
    # the last two days are in it entirely
    assert set(values[-288:]) <= set(read) and len(read) < len(values)


async def test_a_short_record_is_read_whole_and_a_missing_one_is_an_error() -> None:
    body = record_file(_record(1))
    session = FakeSession({station_record_url("BER"): body})
    parts, requests, nbytes = await async_fetch_station_record(session, "BER", 3)
    assert parts == [(body, None)] and requests == 2 and nbytes == len(body)
    with pytest.raises(OgdUnavailable):
        await async_fetch_station_record(FakeSession(), "BER", 3)
    # a tail of a file without a radiation column
    bare = b"station_abbr;reference_timestamp;tre200s0\n" + b"BER;01.11.2026 10:10;4.2\n" * 6000
    with pytest.raises(OgdError):
        await async_fetch_station_record(FakeSession({station_record_url("BER"): bare}), "BER", 1)


async def test_the_end_of_the_earlier_years(monkeypatch) -> None:
    monkeypatch.setattr(ogd, "_HISTORY_TAIL", 20000)
    values = _record()
    body = record_file(values)
    session = FakeSession({station_history_url("BER", 2027): body})
    parts, requests, nbytes = await async_fetch_station_history(session, "BER", 2027)
    assert requests == 3 and nbytes == 2048 + 20000 and parts[0][1] == (1, 4)
    assert parse_station_record(parts[0])[-1] == values[-1]
    # a station that has no earlier years
    assert await async_fetch_station_history(FakeSession(), "NEW", 2027) == ([], 1, 0)
