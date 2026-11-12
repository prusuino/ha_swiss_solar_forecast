# Security Policy

## Supported Versions

Only the latest release is supported. Please update to the latest version before reporting an issue.

## Reporting a Vulnerability

This integration only reads public data from two Swiss federal services over HTTPS. It has no credentials, no write access to any external system and no third-party dependencies.

**What it sends:**

- **`https://data.geo.admin.ch`** (the federal geodata platform that hosts the MeteoSwiss open data): one HEAD request every 15 minutes to see whether a new model run exists (a second one for the hour before while the run of the current hour is not out yet, up to seven during an outage of the forecast), and for a new run range (`Range: bytes=…`) requests into five parameter files (radiation, diffuse radiation, precipitation, wind, and every six hours the temperature); at setup and when a plant is reconfigured, the list of forecast points. The URLs carry the run time and the parameter name. The nearest forecast point is chosen locally from the downloaded list; the byte ranges that are read are those of that point's rows (one of about 5800 points, a few kilometres apart). With the station correction on (the default): every 30 days the list of the measurement stations, and while the sun is up, once in 15 minutes, the file of the day of the nearest station. The station is chosen locally from the list; the URL carries its abbreviation, so the platform sees which of about 160 stations — one within 25 km of the place — is read. Switch the station correction off under Configure → Whole plant to leave these requests out.
- **`https://api3.geo.admin.ch`** (the federal geo API of swisstopo), only in the setup and Reconfigure dialogs and once after them:
  - the **address text you type**, to find the building (skipped when you place the marker instead);
  - the **coordinates of the marker**, to read the roof planes of the building there from the solar cadastre, and the cadastre's building id for its other planes;
  - with the terrain horizon on (the default): five requests for terrain profiles along lines that **start at the coordinates of the place**. They are made once per place and the result is kept; switch the terrain horizon off under Configure → Whole plant to leave them out.

  So the federal geo API sees the place of the plant, as it does when you open the same building on its public map. No account, cookie or key is involved; the user agent names this integration.
- **Nothing about your plant's output leaves your instance.** The self-calibration reads the state of the energy sensor you name from your own Home Assistant and compares it locally.

**What it stores:** in Home Assistant's config entries the place (coordinates, the address text it was found with, the forecast point), the planes with their figures and the cadastre's roof ids, and the options; in one file per entry under `.storage` the terrain horizon, the snow as it is followed (millimetres on the ground and on the planes, the last two days hour by hour), what the calibration learned (hourly factors and the recent hours of forecast and metered energy) and the daily sums behind the accuracy figures (forecast and metered energy of the last 40 days). The file is removed with the entry. The diagnostics download redacts the coordinates, the address, the forecast point, the entry's title and unique id, the roof ids and the name of the energy sensor.

**Parsing:** the forecast files are CSV; only the rows of the configured point are read, split on `;` and converted to numbers. The answers of the geo API are JSON; from them numbers and the address labels are used, the labels as plain text in the dialog. Nothing from a file or an answer is executed, rendered as HTML or written to disk as received.

If you believe you have found a security issue, please report it privately via [GitHub Security Advisories](../../security/advisories/new) rather than opening a public issue. For anything else, please use the regular [Issues](../../issues) tab.
