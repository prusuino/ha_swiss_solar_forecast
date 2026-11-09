"""The cadastre client's parsing and the typical-day arithmetic, with a
captured geo API answer."""
from __future__ import annotations

from custom_components.swiss_solar_forecast.sonnendach import _roof_from_attributes, _strip_html, compass_from_sonnendach, typical_day_kwh

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
