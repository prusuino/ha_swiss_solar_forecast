# Notices

## Data — MeteoSwiss Open Data

The radiation and temperature forecast comes from the **MeteoSwiss local forecast** published as open government data on the federal geodata platform (data.geo.admin.ch), licence **CC BY 4.0**. Source: Federal Office of Meteorology and Climatology MeteoSwiss. The integration reads only the rows of one forecast point per file with HTTP range requests and identifies itself with its own User-Agent.

The station correction uses the ten-minute global radiation of the **MeteoSwiss automatic measurement network (SwissMetNet)**, published the same way under the same licence.

The integration is not affiliated with or endorsed by MeteoSwiss.

## Roof data — Sonnendach (SFOE)

The roof planes offered at setup, with their orientation, tilt, area, suitability class and long-term yield, come from **Sonnendach**, the solar cadastre of the Swiss Federal Office of Energy (SFOE), published as open government data (dataset *Solarenergie: Eignung Dächer*) and read through the federal geo API (api3.geo.admin.ch). Source attribution: Bundesamt für Energie BFE. The address search uses the federal address register through the same API (swisstopo).

## Terrain — swisstopo elevation model

The terrain horizon of a place is computed from height profiles of the elevation model of the **Federal Office of Topography swisstopo** (swissALTI3D, and the coarser model beyond the border), read through the profile service of the federal geo API (api3.geo.admin.ch), open government data. Source: Federal Office of Topography swisstopo. The integration reads five profiles per plant once and keeps the result.

The integration is not affiliated with or endorsed by swisstopo.

## Model

The transposition from the horizontal to the module plane uses the isotropic sky model, the diffuse estimate (used only when MeteoSwiss publishes no diffuse radiation) follows Erbs et al. (1982), the cell temperature the NOCT convention and the sun position the formulas of the NOAA solar calculator. The terrain horizon blocks the direct beam only; the diffuse sky is not reduced. The self-calibration is a ratio of counted to forecast energy per hour of the day with exponential fading. No external library is used.
