"""The cadastre client's parsing and the typical-day arithmetic, with a
captured geo API answer."""
from __future__ import annotations

import aiohttp
import pytest

from custom_components.swiss_solar_forecast.sonnendach import (
    API,
    Address,
    SonnendachError,
    _roof_from_attributes,
    _strip_html,
    async_geocode,
    async_roofs_at,
    compass_from_sonnendach,
    typical_day_kwh,
)

from .fakes import FakeResponse, FakeSession

ATTRS = {
    "building_id": 1510660,
    "flaeche": 245.33,
    "ausrichtung": 84,
    "neigung": 20,
    "klasse": 2,
    "klasse_text": "Mittel##Moyenne##Media##Mean##Mittel",
    "stromertrag": 37015,
    "mstrahlung": 943,
    "monate": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 12, 11],
    "monats_ertrag": [5.18, 11.10, 17.07, 23.77, 21.09, 18.84, 14.55, 8.21, 4.55, 3.29, 2.08, 4.30],
}


def test_compass_from_sonnendach() -> None:
    assert compass_from_sonnendach(0) == 180.0  # south
    assert compass_from_sonnendach(-90) == 90.0  # east
    assert compass_from_sonnendach(90) == 270.0  # west
    assert compass_from_sonnendach(-180) == 0.0 and compass_from_sonnendach(180) == 0.0


def test_roof_parsing_normalises_the_monthly_profile() -> None:
    roof = _roof_from_attributes(ATTRS, 14664870)
    assert roof is not None
    assert roof.roof_id == 14664870 and roof.building_id == 1510660
    assert roof.azimuth == 264.0 and roof.tilt == 20.0
    assert roof.suitability == 2 and roof.suitability_text == {"de": "Mittel", "fr": "Moyenne", "it": "Media", "en": "Mean"}
    assert abs(sum(roof.monthly_share.values()) - 100.0) < 1e-6
    assert roof.monthly_share[7] > roof.monthly_share[1]  # July beats January
    assert roof.suggested_kwp == 49.1


def test_roof_parsing_rejects_garbage() -> None:
    assert _roof_from_attributes({"flaeche": "x"}, 1) is None
    assert _roof_from_attributes({}, 1) is None


def test_typical_day_scales_with_peak_power_and_month() -> None:
    roof = _roof_from_attributes(ATTRS, 1)
    june = typical_day_kwh(10.0, roof.irradiation_kwh_m2_year, roof.monthly_share, 6, 30, 14.0)
    december = typical_day_kwh(10.0, roof.irradiation_kwh_m2_year, roof.monthly_share, 12, 31, 14.0)
    assert 30 <= june <= 50  # a June day on 10 kWp in Bern
    assert december < june / 5
    assert typical_day_kwh(20.0, roof.irradiation_kwh_m2_year, roof.monthly_share, 6, 30, 14.0) == 2 * june
    assert typical_day_kwh(0.0, roof.irradiation_kwh_m2_year, roof.monthly_share, 6, 30, 14.0) is None
    assert typical_day_kwh(10.0, roof.irradiation_kwh_m2_year, {}, 6, 30, 14.0) is None


def test_strip_html() -> None:
    assert _strip_html("Bundesplatz 3 <b>3011 Bern</b>") == "Bundesplatz 3 3011 Bern"


async def test_geocode_reads_addresses_and_skips_broken_ones() -> None:
    session = FakeSession(
        {
            f"{API}/SearchServer": FakeResponse(
                payload={
                    "results": [
                        {"attrs": {"label": "Bundesplatz 3 <b>3011 Bern</b>", "lat": 46.9466, "lon": 7.4442}},
                        {"attrs": {"label": "without a place"}},
                        {"attrs": {"label": "Bahnhofplatz 1 <b>3011 Bern</b>", "lat": "x", "lon": 7.44}},
                    ]
                }
            )
        }
    )
    assert await async_geocode(session, "Bundesplatz 3 Bern") == [Address("Bundesplatz 3 3011 Bern", 46.9466, 7.4442)]
    assert await async_geocode(FakeSession({f"{API}/SearchServer": FakeResponse(payload={})}), "nowhere") == []


async def test_roofs_at_collects_the_planes_of_the_building_largest_first() -> None:
    lookups: list[int] = []

    def find(method: str, url: str, headers: dict) -> FakeResponse:
        lookups.append(len(lookups))
        if len(lookups) > 1:  # the second building has nothing
            return FakeResponse(payload={})
        return FakeResponse(
            payload={
                "results": [
                    {"id": 11, "attributes": {**ATTRS, "flaeche": 40.0}},
                    {"id": 12, "attributes": ATTRS},
                    {"id": 13, "attributes": {**ATTRS, "flaeche": 3.0}},  # a dormer
                    {"id": 14, "attributes": {"flaeche": "x"}},  # garbage
                    {"attributes": {**ATTRS, "label": "15", "flaeche": 60.0}},  # the id under another name
                    {"attributes": ATTRS},  # no id at all
                ]
            }
        )

    session = FakeSession(
        {
            f"{API}/MapServer/identify": FakeResponse(
                payload={
                    "results": [
                        {"attributes": {"building_id": 1510660}},
                        {"attributes": {"building_id": 1510660}},  # a second plane of the same building
                        {"attributes": {}},
                        {"attributes": {"building_id": 1510661}},
                    ]
                }
            ),
            f"{API}/MapServer/find": find,
        }
    )
    roofs = await async_roofs_at(session, 46.9466, 7.4442)
    assert [(r.roof_id, r.area) for r in roofs] == [(12, 245.33), (15, 60.0), (11, 40.0)]
    assert len(lookups) == 2  # one lookup per building
    assert await async_roofs_at(FakeSession({f"{API}/MapServer/identify": FakeResponse(payload={})}), 46.9, 7.4) == []


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (FakeResponse(503), "HTTP 503 from the geo API"),
        (aiohttp.ClientError("down"), "ClientError from the geo API"),
        (TimeoutError(), "TimeoutError from the geo API"),
        (FakeResponse(payload=ValueError("not JSON")), "ValueError from the geo API"),
    ],
)
async def test_an_unreadable_geo_api_is_one_kind_of_error(answer: object, message: str) -> None:
    with pytest.raises(SonnendachError, match=message):
        await async_geocode(FakeSession({f"{API}/SearchServer": answer}), "Bern")
    with pytest.raises(SonnendachError, match=message):
        await async_roofs_at(FakeSession({f"{API}/MapServer/identify": answer}), 46.9, 7.4)
