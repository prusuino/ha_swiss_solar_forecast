# Notices

## Data — MeteoSwiss Open Data

The radiation and temperature forecast comes from the **MeteoSwiss local forecast** published as open government data on the federal geodata platform (data.geo.admin.ch), licence **CC BY 4.0**. Source: Federal Office of Meteorology and Climatology MeteoSwiss. The integration reads only the rows of one forecast point per file with HTTP range requests and identifies itself with its own User-Agent.

The integration is not affiliated with or endorsed by MeteoSwiss.

## Roof data — Sonnendach (SFOE)

The roof planes offered at setup, with their orientation, tilt, area, suitability class and long-term yield, come from **Sonnendach**, the solar cadastre of the Swiss Federal Office of Energy (SFOE), published as open government data (dataset *Solarenergie: Eignung Dächer*) and read through the federal geo API (api3.geo.admin.ch). Source attribution: Bundesamt für Energie BFE. The address search uses the federal address register through the same API (swisstopo).

## Model

The transposition from the horizontal to the module plane uses the isotropic sky model, the diffuse estimate (used only when MeteoSwiss publishes no diffuse radiation) follows Erbs et al. (1982), the cell temperature the NOCT convention and the sun position the formulas of the NOAA solar calculator. No external library is used.
