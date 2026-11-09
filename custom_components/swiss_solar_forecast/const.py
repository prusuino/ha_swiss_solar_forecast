"""Constants for the Swiss Solar Forecast integration."""
from __future__ import annotations

import json
from pathlib import Path

DOMAIN = "swiss_solar_forecast"

ATTRIBUTION = "Source: MeteoSwiss (Open Data, CC BY 4.0)"

# The integration's own version, shown on the service card. Read from the
# manifest so that a release never needs to touch two places.
VERSION: str = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))["version"]

# Sent with every request. MeteoSwiss serves the files from the federal
# geodata platform; identifying the client helps them attribute traffic.
USER_AGENT = f"SwissSolarForecast/{VERSION} (Home Assistant integration; +https://github.com/prusuino/ha_swiss_solar_forecast)"

# --- Open data endpoints -----------------------------------------------------
DATA_BASE = "https://data.geo.admin.ch"
STAC_BASE = f"{DATA_BASE}/api/stac/v1/collections"
COLLECTION_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"
FORECAST_META_POINTS_URL = f"{DATA_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_point.csv"
DOCS_URL = "https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data"

# --- Config entry keys -------------------------------------------------------
CONF_POINT_TYPE = "point_type"  # 1 station, 2 postal code centre, 3 mountain point
CONF_POINT_ID = "point_id"
CONF_POINT_NAME = "point_name"
CONF_POINT_LAT = "point_lat"
CONF_POINT_LON = "point_lon"
CONF_POINT_HEIGHT = "point_height"
CONF_PLANT_NAME = "name"
CONF_PLANES = "planes"  # list of plane dicts (the keys below), one per roof plane of the plant
CONF_PLANT_INVERTER_KW = "plant_inverter_kw"  # AC limit of the plant as a whole, kW; 0 = none
CONF_HORIZON = "terrain_horizon"  # whether the terrain horizon (swisstopo elevation model) blocks the direct beam
CONF_YIELD_SENSOR = "yield_sensor"  # entity id of the plant's energy meter, for the self-calibration; empty = none
CONF_PLANE_LABEL = "label"  # short name of the plane, e.g. "SW 230°" or the user's text
CONF_KWP = "kwp"  # installed DC peak power of the plane, kWp
CONF_AZIMUTH = "azimuth"  # compass bearing the modules face, degrees: 0 north, 90 east, 180 south, 270 west
CONF_TILT = "tilt"  # inclination from horizontal, degrees: 0 flat, 90 vertical
CONF_LOSSES = "losses"  # system losses in percent (wiring, inverter, soiling, mismatch)
CONF_INVERTER_KW = "inverter_kw"  # AC limit of the inverter, kW; 0 = no clipping
CONF_DAMPING = "damping"  # multiplier applied to the result, for calibration against the real yield

# Roof plane from the Sonnendach cadastre (absent when the plane was entered by hand)
CONF_ROOF_ID = "roof_id"
CONF_ROOF_AREA = "roof_area"  # m²
CONF_ROOF_CLASS = "roof_class"  # SFOE suitability class 1…5
CONF_ROOF_IRRADIATION = "roof_irradiation"  # mean irradiation on the plane, kWh/m²/a
CONF_ROOF_MONTHLY = "roof_monthly"  # {"1": percent, …, "12": percent} of the year's yield
CONF_ADDRESS = "address"  # the address text the place was found with, for the device card

DEFAULT_LOSSES = 14.0
DEFAULT_DAMPING = 1.0
DEFAULT_TILT = 30.0
DEFAULT_AZIMUTH = 180.0

POINT_TYPE_STATION = 1
POINT_TYPE_POSTAL = 2
POINT_TYPE_MOUNTAIN = 3

# --- Forecast parameters -----------------------------------------------------
# Hourly values of the MeteoSwiss local forecast. The "h0" values are means
# over the hour that ends at the row's timestamp.
PARAM_GLOBAL = "gre000h0"  # global radiation on the horizontal, W/m², hourly mean
PARAM_DIFFUSE = "ods000h0"  # diffuse radiation, W/m², hourly mean
PARAM_TEMPERATURE = "tre200h0"  # air temperature 2 m, °C, hourly mean
HOURLY_PARAMS = (PARAM_GLOBAL, PARAM_DIFFUSE, PARAM_TEMPERATURE)
PROBE_PARAM = "jp2000d0"  # a small daily file: one HEAD request tells whether a run exists

# Files whose rows are grouped by timestamp rather than by point (measured
# on the published files; the layout is detected on every read anyway).
DATE_MAJOR_PARAMS = frozenset({"tre200h0"})

# --- Model constants ---------------------------------------------------------
ALBEDO = 0.2  # ground reflection used for the reflected share
NOCT = 45.0  # nominal operating cell temperature, °C
TEMPERATURE_COEFFICIENT = -0.004  # power change per kelvin of cell temperature above 25 °C
