"""The measurement stations: the list with their places, a station's
ten-minute file and its global radiation, read with a faked answer."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.swiss_solar_forecast.ogd import (
    OgdError,
    OgdUnavailable,
    Station,
    async_fetch_station_radiation,
    async_fetch_stations,
    distance_km,
    parse_station_radiation,
    station_now_url,
)

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
