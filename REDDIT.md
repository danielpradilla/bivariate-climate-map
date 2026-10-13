# r/dataisbeautiful submission

## Title

[OC] Annual temperature and precipitation maps, 1950-2025

## Link

https://www.danielpradilla.info/projects/bivariate-climate-map/

## First top-level comment

Data: [TerraClimate v1.1](https://www.climatologylab.org/terraclimate.html), monthly maximum temperature and precipitation for 1950-2025. Land selection and borders use [Natural Earth](https://www.naturalearthdata.com/).

Tools: Python (NumPy, SciPy, netCDF4 and Shapely) for annual aggregation and map preparation; JavaScript, D3 and Canvas for the interactive maps. The [source code and explanatory notebook](https://github.com/danielpradilla/bivariate-climate-map) include the calculations and download instructions.

I made these maps to show temperature and precipitation together. Each colour combines one of four temperature classes with one of four precipitation classes. You can switch between seven continents and World, play through the years, turn smoothing off and inspect individual grid cells.

Temperature is the mean of all twelve monthly maximum-temperature fields, in degrees Celsius. Precipitation is the sum of the twelve monthly totals, in millimetres. Only cells with complete monthly coverage for both variables are displayed. These are gridded estimates, not individual weather-station readings.

Class thresholds are fitted to each region's full 1950-2025 distribution and stay fixed during playback. Each land-cell/year observation has equal weight; the thresholds are not area-weighted. Colours are comparable across years within a region, but continent views have different thresholds. Use World for a common scale across continents.

Continent views use the native grid, approximately 4 km at the equator. World samples every sixth source cell, approximately 28 km, without averaging. The default Gaussian smoothing has a standard deviation of two grid cells and can bias coastal colours toward the regional mean. The raw view avoids that smoothing. Geographic selections follow Natural Earth's country-continent tags with documented exceptions; they do not form an exhaustive physical-continent partition.

The palette, percentile choices and smoothing method build on [Milan Janosov's bivariate climate map](https://milanjanosov.substack.com/p/bivariate-climate-map-temperature). Earlier related examples include [Muhammad Mohsin Raza's tutorial](https://www.datawim.com/post/creating-professional-bivariate-maps-in-r/) and [Fil / Philippe Rivière's Pangea map](https://fil.github.io/pangea/plot/bivariate-map).

## Submission checks

- Submit a link post to the interactive visualization above, not a text-only post or a link to this draft. Keep `[OC]` only when submitting as the creator.
- Post the data-and-tools comment above as your first top-level comment. Credit the underlying data and the earlier visualization methods.
- Check for a popular repost of this visualization within the preceding month before submitting. Recheck the rules at submission time.
- The draft describes annual climate fields without claims about U.S. politics or personal data. Keep discussion constructive; do not add sensational claims, hate speech or copied material.

Rules checked on 14 October 2026 against the [live community sidebar](https://www.reddit.com/r/dataisbeautiful/) and [posting wiki](https://www.reddit.com/r/dataisbeautiful/wiki/index/). This is a local draft; nothing has been submitted to Reddit.
