#!/usr/bin/env python3
"""Acquire real native-resolution annual European TerraClimate grids, 1950–2025.

Run: uv run --with-requirements requirements-data.txt python scripts/build_data.py
Downloads use at most four concurrent HTTP requests; NetCDF decoding is sequential.
Every annual cell requires all 12 months of both variables. Fixed breakpoints pool
encoded RAW land cells across all selected years, equally weighted (not area-
weighted). Annual valid masks may vary; years therefore contribute their actual
valid-cell counts. Histogram order statistics exactly implement NumPy's linear
percentiles without retaining the pooled sample array.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import gzip
import sys
import time
import json
from pathlib import Path
from urllib.parse import urlencode
import zipfile

import numpy as np
import requests
from scipy.ndimage import gaussian_filter
import shapefile
import shapely
from shapely.geometry import box, mapping, shape
from shapely.geometry.polygon import orient
import xarray as xr

FIRST_YEAR = 1950
LAST_YEAR = 2025
GENERATOR_VERSION = 2
BBOX = (-25.0, 34.0, 60.0, 72.0)
TEMPERATURE_PERCENTILES = [0, 20, 45, 70, 100]
PRECIPITATION_PERCENTILES = [0, 60, 78, 90, 100]
EQUAL_PERCENTILES = [0, 25, 50, 75, 100]
NODATA = -32768
BASE = "https://thredds.northwestknowledge.net/thredds/ncss/"
NATURAL_EARTH_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"


def climate_url(variable: str, year: int) -> str:
    west, south, east, north = BBOX
    # NCSS NetCDF4 export fails HTTP 500 for packed tmax; ppt supports it.
    accept = "netcdf" if variable == "tmax" else "netcdf4"
    query = {
        "var": variable, "south": int(south), "north": int(north),
        "west": int(west), "east": int(east), "disableProjSubset": "on",
        "addLatLon": "true", "horizStride": 1, "accept": accept,
        "time_start": f"{year}-01-01T00:00:00Z",
        "time_end": f"{year}-12-31T23:59:59Z", "timeStride": 1,
    }
    return BASE + f"grid/TERRACLIMATE_ALL/data/TerraClimate_{variable}_{year}.nc?" + urlencode(query)


def acquire(url: str, target: Path, legacy_record: dict | None = None) -> Path:
    receipt = target.with_suffix(target.suffix + ".source.json")
    if target.exists():
        record = json.loads(receipt.read_text()) if receipt.exists() else legacy_record
        if record and record["url"] == url and record["sha256"] == digest(target):
            if not receipt.exists():
                write_json(receipt, source_record(url, target))
            return target
    temporary = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            print(f"Downloading {url}", flush=True)
            with requests.get(url, stream=True, timeout=(30, 900)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
            # Reject error documents even when the service responds HTTP 200.
            with temporary.open("rb") as source:
                magic = source.read(8)
            if not (magic.startswith(b"CDF") or magic == b"\x89HDF\r\n\x1a\n"
                    or url == NATURAL_EARTH_URL and magic.startswith(b"PK")):
                raise ValueError(f"Not a NetCDF/ZIP response from {url}: {magic!r}")
            temporary.replace(target)
            write_json(receipt, source_record(url, target))
            return target
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_record(url: str, path: Path) -> dict:
    return {
        "url": url, "sha256": digest(path), "bytes": path.stat().st_size,
        "retrievedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def annual_field(path: Path, variable: str, year: int):
    # xarray decodes _FillValue/missing_value BEFORE CF scale_factor/add_offset.
    with xr.open_dataset(path, engine="netcdf4", mask_and_scale=True, decode_times=True) as dataset:
        field = dataset[variable].transpose("time", "lat", "lon")
        encoding = {key: json_value(field.encoding[key]) for key in
                    ("scale_factor", "add_offset", "_FillValue", "missing_value")
                    if key in field.encoding}
        dates = np.datetime_as_string(dataset.time.values, unit="D").tolist()
        expected = [f"{year}-{month:02d}-01" for month in range(1, 13)]
        if dates != expected:
            raise ValueError(f"{variable}: expected all 12 months exactly, got {dates}")
        west, south, east, north = BBOX
        field = field.where(
            (field.lon >= west) & (field.lon <= east)
            & (field.lat >= south) & (field.lat <= north), drop=True,
        ).sortby("lon").sortby("lat", ascending=False)
        values = np.asarray(field.values, dtype=np.float64)
        complete = np.isfinite(values).all(axis=0)
        annual = values.mean(axis=0) if variable == "tmax" else values.sum(axis=0)
        annual[~complete] = np.nan
        global_attributes = {key: json_value(value) for key, value in dataset.attrs.items()}
        details = {
            "dates": dates, "months": len(dates), "cfPacking": encoding,
            "units": field.attrs.get("units"), "description": field.attrs.get("description"),
            "globalAttributes": global_attributes,
            "completeYearCellsBeforeEuropeMask": int(complete.sum()),
            "aggregation": "arithmetic mean of 12 monthly maximum-temperature fields (not mean temperature; no day weighting)"
            if variable == "tmax" else "sum of 12 monthly accumulated precipitation fields",
        }
        print(f"{variable}: {dates}; packing={encoding}; shape={annual.shape}", flush=True)
        return annual, field.lon.values.copy(), field.lat.values.copy(), details


def european_countries(path: Path):
    clipped = []
    features = []
    region = box(*BBOX)
    version = None
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.upper().endswith(".VERSION.TXT"):
                version = archive.read(member).decode().strip()
        with archive.open("ne_10m_admin_0_countries.shp") as shp, \
                archive.open("ne_10m_admin_0_countries.shx") as shx, \
                archive.open("ne_10m_admin_0_countries.dbf") as dbf:
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf, encoding="utf-8")
            for record in reader.iterShapeRecords():
                properties = record.record.as_dict()
                if properties["CONTINENT"] != "Europe":
                    continue
                geometry = shape(record.shape.__geo_interface__).intersection(region)
                if geometry.is_empty:
                    continue
                if not geometry.is_valid:
                    geometry = shapely.make_valid(geometry)
                polygons = [geometry] if geometry.geom_type == "Polygon" else [
                    part for part in geometry.geoms if part.geom_type == "Polygon"
                ]
                if not polygons:
                    continue
                geometry = shapely.union_all(polygons)
                clipped.append(geometry)
                # Display borders only are simplified; the raster mask uses unsimplified geometry.
                display = geometry.simplify(0.01, preserve_topology=True)
                display = orient(display, sign=1.0) if display.geom_type == "Polygon" else shapely.MultiPolygon(
                    [orient(part, sign=1.0) for part in display.geoms]
                )
                features.append({
                    "type": "Feature",
                    "properties": {"name": properties["NAME_EN"], "continent": "Europe", "isoA3": properties["ADM0_A3"]},
                    "geometry": mapping(display),
                })
    features.sort(key=lambda feature: feature["properties"]["name"])
    return shapely.union_all(clipped), {"type": "FeatureCollection", "features": features}, version


def encode_fields(temperature, precipitation, valid):
    encoded = np.full((*valid.shape, 2), NODATA, dtype="<i2")
    for index, (field, multiplier) in enumerate(((temperature, 100), (precipitation, 1))):
        rounded = np.rint(field[valid] * multiplier)
        if np.any((rounded <= NODATA) | (rounded > np.iinfo(np.int16).max)):
            raise ValueError("Annual values exceed the signed-int16 contract")
        encoded[..., index][valid] = rounded.astype("<i2")
    return encoded


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def export_binary(path: Path, encoded):
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=6) as compressed:
            compressed.write(memoryview(encoded).cast("B"))
    temporary.replace(path)


def load_binary(path: Path):
    with gzip.open(path, "rb") as source:
        payload = source.read()
    if len(payload) != 2040 * 912 * 4:
        raise ValueError(f"Unexpected decompressed size for {path}")
    return np.frombuffer(payload, dtype="<i2").reshape(912, 2040, 2)


def histogram_percentiles(histogram, percentiles, scale):
    cumulative = np.cumsum(histogram)
    count = int(cumulative[-1])
    if not count:
        raise ValueError("Empty pooled annual histogram")
    result = []
    for percentile in percentiles:
        position = (count - 1) * (percentile / 100)
        lower, upper = int(np.floor(position)), int(np.ceil(position))
        left = int(np.searchsorted(cumulative, lower, side="right")) + NODATA
        right = int(np.searchsorted(cumulative, upper, side="right")) + NODATA
        # Divide before interpolation, exactly as np.percentile on decoded cells.
        left, right = left / scale, right / scale
        weight = position - lower
        value = left + (right - left) * weight if weight < 0.5 else right - (right - left) * (1 - weight)
        result.append(float(value))
    return result


def generate_year(year, paths, urls, args, geometry, countries, natural_earth_version):
    temperature, lon, lat, temperature_details = annual_field(paths["tmax"], "tmax", year)
    precipitation, ppt_lon, ppt_lat, precipitation_details = annual_field(paths["ppt"], "ppt", year)
    if not np.array_equal(lon, ppt_lon) or not np.array_equal(lat, ppt_lat):
        raise ValueError("tmax and ppt grids must match exactly")
    if (len(lat), len(lon)) != (912, 2040):
        raise ValueError(f"Expected native 912×2040 grid, got {(len(lat), len(lon))}")
    dx, dy = float(np.diff(lon).mean()), float(-np.diff(lat).mean())
    if not np.allclose(np.diff(lon), 1 / 24, atol=1e-10) or not np.allclose(-np.diff(lat), 1 / 24, atol=1e-10):
        raise ValueError("Expected the native 1/24-degree TerraClimate grid")
    european_land = shapely.intersects_xy(geometry, lon[None, :], lat[:, None])
    complete = np.isfinite(temperature) & np.isfinite(precipitation)
    valid = european_land & complete
    if not valid.any():
        raise ValueError("No complete-year European cells")

    raw = encode_fields(temperature, precipitation, valid)
    directory = args.output_dir / "years" / str(year)
    directory.mkdir(parents=True, exist_ok=True)
    export_binary(directory / "climate.bin.gz", raw)
    encoded_temperature = raw[..., 0][valid].astype(np.float64) / 100
    encoded_precipitation = raw[..., 1][valid].astype(np.float64)
    field_means = [float(temperature[valid].mean()), float(precipitation[valid].mean())]
    smoothed = [gaussian_filter(np.where(valid, field, mean), sigma=2, mode="reflect")
                for field, mean in zip((temperature, precipitation), field_means)]
    smoothed_encoded = encode_fields(*smoothed, valid)
    if not np.array_equal(raw[..., 0] == NODATA, smoothed_encoded[..., 0] == NODATA):
        raise ValueError(f"{year}: raw and smoothed masks differ")
    export_binary(directory / "smoothed.bin.gz", smoothed_encoded)

    samples = []
    for name, longitude, latitude in [
        ("Madrid", -3.7038, 40.4168), ("London", -0.1276, 51.5072),
        ("Paris", 2.3522, 48.8566), ("Berlin", 13.4050, 52.5200),
        ("Rome", 12.4964, 41.9028), ("Oslo", 10.7522, 59.9139),
        ("Reykjavik", -21.9426, 64.1466), ("Helsinki", 24.9384, 60.1699),
    ]:
        column, row = int(np.argmin(np.abs(lon - longitude))), int(np.argmin(np.abs(lat - latitude)))
        samples.append({
            "name": name, "requestedLongitude": longitude, "requestedLatitude": latitude,
            "longitude": float(lon[column]), "latitude": float(lat[row]), "row": row, "column": column,
            "valid": bool(valid[row, column]),
            "tmaxC": float(raw[row, column, 0]) / 100 if valid[row, column] else None,
            "pptMm": int(raw[row, column, 1]) if valid[row, column] else None,
            "unroundedTmaxC": float(temperature[row, column]) if valid[row, column] else None,
            "unroundedPptMm": float(precipitation[row, column]) if valid[row, column] else None,
        })
    metadata = {
        "width": len(lon), "height": len(lat), "west": float(lon[0]), "north": float(lat[0]),
        "dx": dx, "dy": dy, "year": year, "validCells": int(valid.sum()),
        "temperaturePercentiles": TEMPERATURE_PERCENTILES,
        "precipitationPercentiles": PRECIPITATION_PERCENTILES, "equalPercentiles": EQUAL_PERCENTILES,
        "temperatureRange": [float(encoded_temperature.min()), float(encoded_temperature.max())],
        "precipitationRange": [float(encoded_precipitation.min()), float(encoded_precipitation.max())],
        "fullPrecisionTemperatureRange": [float(temperature[valid].min()), float(temperature[valid].max())],
        "fullPrecisionPrecipitationRange": [float(precipitation[valid].min()), float(precipitation[valid].max())],
        "temperatureLabel": "Annual mean monthly maximum temperature", "temperatureUnits": "°C",
        "precipitationLabel": "Annual accumulated precipitation", "precipitationUnits": "mm",
        "rawFile": f"years/{year}/climate.bin.gz", "smoothedFile": f"years/{year}/smoothed.bin.gz",
        "metadataFile": f"years/{year}/metadata.json", "borderFile": "europe.geojson",
        "binary": {"byteOrder": "little-endian", "type": "int16", "layout": "row-major interleaved [tmax × 100, ppt mm]",
                   "nodata": NODATA, "bytesPerCell": 4, "rounding": "nearest integer, ties to even (numpy.rint)",
                   "temperaturePrecisionC": 0.01, "precipitationPrecisionMm": 1},
        "percentileMethod": "Fixed pooled raw annual European land-cell histogram order statistics, NumPy linear interpolation; equal cell-year weighting, no area weighting. Annual valid masks may vary.",
        "quantization": "Annual fields are aggregated at decoded float64 precision, then rounded to 0.01°C and integer mm. All break arrays and primary ranges use encoded raw values for consistent client classification. Maximum rounding errors are 0.005°C and 0.5mm.",
        "scope": {
            "label": "Europe", "bbox": list(BBOX), "continentFilter": "Natural Earth CONTINENT == Europe",
            "selection": "Unsimplified Natural Earth 1:10m country polygons tagged Europe intersected with [-25,34,60,72]; keep grid-cell centers on or inside that intersection. European mainland, Iceland and islands inside bbox included; overseas territories and far-northern islands outside bbox excluded. Russia is included only within bbox; Turkey, Cyprus and the Caucasus countries tagged Asia are excluded. This is a country-continent selection, not a physical Europe/Asia boundary.",
            "countries": [feature["properties"]["name"] for feature in countries["features"]],
            "europeanLandCells": int(european_land.sum()),
            "incompleteYearEuropeanCells": int((european_land & ~complete).sum()),
            "mask": "Both variables finite in all 12 months AND unsimplified European land at grid-cell center; ocean and non-European land are nodata in both binaries.",
            "borderSimplificationDegrees": 0.01, "borderWinding": "RFC7946 counterclockwise exterior rings",
        },
        "smoothing": {
            "enabledByDefault": True, "method": "scipy.ndimage.gaussian_filter", "sigmaCells": 2,
            "edgeMode": "reflect", "truncate": 4.0,
            "description": "Fill every invalid/ocean/non-European cell with the valid-European raw full-precision field mean separately; Gaussian filter sigma=2 native cells; restore original valid mask; quantize same as raw. Raw percentiles remain unchanged.",
            "meanFillTmaxC": field_means[0], "meanFillPptMm": field_means[1],
            "caveat": "Mean filling biases coastal and mask-edge smoothed values toward the European mean. Smoothing is visual only; inspection and statistics use the corresponding year's unsmoothed climate.bin.gz.",
        },
        "sources": {
            "terraClimate": {
                "dataset": "TerraClimate", "version": temperature_details["globalAttributes"]["version"],
                "catalog": "https://thredds.northwestknowledge.net/thredds/catalog/TERRACLIMATE_ALL/data/catalog.xml",
                "homepage": "https://www.climatologylab.org/terraclimate.html",
                "nativeResolutionDegrees": 1 / 24, "horizStride": 1,
                "license": temperature_details["globalAttributes"]["license"],
                "licenseSource": "CC0 license attribute in both downloaded NetCDF files",
                "citation": temperature_details["globalAttributes"]["references"],
                "foundationalCitation": "Abatzoglou, J.T., Dobrowski, S.Z., Parks, S.A. & Hegewisch, K.C. (2018). TerraClimate, a high-resolution global dataset of monthly climate and climatic water balance from 1958–2015. Scientific Data 5, 170191.",
                "foundationalDoi": "https://doi.org/10.1038/sdata.2017.191",
                "tmax": {**source_record(urls["tmax"], paths["tmax"]), **temperature_details},
                "ppt": {**source_record(urls["ppt"], paths["ppt"]), **precipitation_details},
            },
            "naturalEarth": {
                **source_record(urls["naturalEarth"], paths["naturalEarth"]),
                "dataset": "Natural Earth 1:10m Admin 0 Countries", "version": natural_earth_version,
                "homepage": "https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-0-countries/",
                "license": "Public domain", "licenseSource": "https://www.naturalearthdata.com/about/terms-of-use/",
                "boundaries": "Natural Earth de facto country boundaries, not a statement on sovereignty.",
            },
        },
        "representativeValues": samples,
        "generation": {"script": "scripts/build_data.py", "dependencies": "requirements-data.txt",
                       "command": args.command, "generatorVersion": GENERATOR_VERSION,
                       "scriptSha256": digest(Path(__file__)),
                       "dependenciesSha256": digest(Path(__file__).resolve().parents[1] / "requirements-data.txt"),
                       "sourceCacheDefault": "/tmp/bivariate-climate-map-source-cache"},
        "assetSha256": {f"years/{year}/{filename}": digest(directory / filename)
                        for filename in ("climate.bin.gz", "smoothed.bin.gz")},
    }
    write_json(directory / "metadata.json", metadata)
    return metadata, raw


def resume_year(year, paths, urls, args):
    path = args.output_dir / "years" / str(year) / "metadata.json"
    if not path.exists():
        return None
    metadata = json.loads(path.read_text())
    generation = metadata.get("generation", {})
    if (generation.get("generatorVersion") != GENERATOR_VERSION
            or generation.get("scriptSha256") != digest(Path(__file__))
            or generation.get("dependenciesSha256") != digest(Path(__file__).resolve().parents[1] / "requirements-data.txt")):
        return None
    for variable in ("tmax", "ppt"):
        source = metadata["sources"]["terraClimate"][variable]
        if source["url"] != urls[variable] or source["sha256"] != digest(paths[variable]):
            return None
        if source["dates"] != [f"{year}-{month:02d}-01" for month in range(1, 13)]:
            return None
    if metadata["sources"]["naturalEarth"]["sha256"] != digest(paths["naturalEarth"]):
        return None
    for filename, expected_hash in metadata["assetSha256"].items():
        asset = args.output_dir / filename
        if not asset.exists() or digest(asset) != expected_hash:
            return None
    raw = load_binary(args.output_dir / metadata["rawFile"])
    smoothed = load_binary(args.output_dir / metadata["smoothedFile"])
    valid = raw[..., 0] != NODATA
    if (not np.array_equal(valid, smoothed[..., 0] != NODATA)
            or not np.array_equal(valid, raw[..., 1] != NODATA)
            or not np.array_equal(valid, smoothed[..., 1] != NODATA)
            or int(valid.sum()) != metadata["validCells"]):
        raise ValueError(f"{year}: invalid resumed masks/count")
    print(f"Resuming verified {year}", flush=True)
    return metadata, raw


def main():
    if "--regions" in sys.argv:
        from build_regions import main as regions_main
        regions_main([argument for argument in sys.argv[1:] if argument != "--regions"])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=FIRST_YEAR)
    parser.add_argument("--end-year", type=int, default=LAST_YEAR)
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/bivariate-climate-map-source-cache"))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--download-workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    if not FIRST_YEAR <= args.start_year <= args.end_year <= LAST_YEAR:
        parser.error("Year range must be inclusive and within 1950–2025")
    args.command = "uv run --with-requirements requirements-data.txt python " + " ".join(sys.argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    previous_path = args.output_dir / "metadata.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    # The existing 2024 aggregate subsets have recorded exact query/hash provenance.
    # Keep that independently verified query instead of redownloading equivalent data.
    legacy = previous.get("sources", {})
    if previous.get("year") != 2024:
        legacy_path = args.output_dir / "years" / "2024" / "metadata.json"
        legacy = json.loads(legacy_path.read_text()).get("sources", {}) if legacy_path.exists() else {}
    natural_path = args.cache_dir / "ne_10m_admin_0_countries.zip"
    acquire(NATURAL_EARTH_URL, natural_path, legacy.get("naturalEarth"))
    geometry, countries, natural_version = european_countries(natural_path)
    shapely.prepare(geometry)
    border = args.output_dir / "europe.geojson"
    border_text = json.dumps(countries, separators=(",", ":")) + "\n"
    if not border.exists() or border.read_text() != border_text:
        border.write_text(border_text)
    years = list(range(args.start_year, args.end_year + 1))
    temperature_histogram = np.zeros(65536, dtype=np.int64)
    precipitation_histogram = np.zeros(65536, dtype=np.int64)
    frames, annual_metadata = {}, {}
    with ThreadPoolExecutor(max_workers=args.download_workers) as pool:
        pending = {}

        def schedule(year):
            urls = {"naturalEarth": NATURAL_EARTH_URL}
            paths = {"naturalEarth": natural_path}
            futures = []
            for variable in ("tmax", "ppt"):
                record = legacy.get("terraClimate", {}).get(variable) if year == 2024 else None
                # A receipt also preserves the original aggregate source across resumes.
                path = args.cache_dir / f"TerraClimate_{variable}_{year}_europe.nc"
                receipt = path.with_suffix(path.suffix + ".source.json")
                if record is None and year == 2024 and receipt.exists():
                    record = json.loads(receipt.read_text())
                url = record["url"] if record and path.exists() else climate_url(variable, year)
                urls[variable], paths[variable] = url, path
                futures.append(pool.submit(acquire, url, path, record))
            pending[year] = paths, urls, futures

        # At most two years in flight, hence at most four downloads and bounded disk work.
        for year in years[:2]:
            schedule(year)
        for index, year in enumerate(years):
            paths, urls, futures = pending.pop(year)
            for future in futures:
                future.result()
            result = None if args.no_resume else resume_year(year, paths, urls, args)
            metadata, raw = result or generate_year(year, paths, urls, args, geometry, countries, natural_version)
            if annual_metadata:
                reference = annual_metadata[years[0]]
                for key in ("width", "height", "west", "north", "dx", "dy"):
                    if metadata[key] != reference[key]:
                        raise ValueError(f"{year}: grid differs from first year: {key}")
            valid = raw[..., 0] != NODATA
            for component, histogram in enumerate((temperature_histogram, precipitation_histogram)):
                histogram += np.bincount(raw[..., component][valid].astype(np.int32) - NODATA, minlength=65536)
            annual_metadata[year] = metadata
            frames[str(year)] = {key: metadata[key] for key in
                                ("year", "rawFile", "smoothedFile", "metadataFile", "validCells",
                                 "temperatureRange", "precipitationRange",
                                 "fullPrecisionTemperatureRange", "fullPrecisionPrecipitationRange")}
            print(f"Exported {year}: {metadata['validCells']:,} valid cells", flush=True)
            if index + 2 < len(years):
                schedule(years[index + 2])

    manifest = dict(annual_metadata[years[-1]])
    manifest.update({
        "years": years, "defaultYear": years[-1], "frames": frames,
        "thresholdReference": f"Pooled annual European land cells, {years[0]}–{years[-1]}",
        "pooledValidCellYears": int(temperature_histogram.sum()),
        "validCellsRange": [min(m["validCells"] for m in annual_metadata.values()),
                            max(m["validCells"] for m in annual_metadata.values())],
        "temperaturePercentiles": TEMPERATURE_PERCENTILES,
        "precipitationPercentiles": PRECIPITATION_PERCENTILES, "equalPercentiles": EQUAL_PERCENTILES,
        "temperatureBreaks": histogram_percentiles(temperature_histogram, TEMPERATURE_PERCENTILES, 100),
        "precipitationBreaks": histogram_percentiles(precipitation_histogram, PRECIPITATION_PERCENTILES, 1),
        "equalTemperatureBreaks": histogram_percentiles(temperature_histogram, EQUAL_PERCENTILES, 100),
        "equalPrecipitationBreaks": histogram_percentiles(precipitation_histogram, EQUAL_PERCENTILES, 1),
        "temperatureRange": histogram_percentiles(temperature_histogram, [0, 100], 100),
        "precipitationRange": histogram_percentiles(precipitation_histogram, [0, 100], 1),
        "fullPrecisionTemperatureRange": [min(m["fullPrecisionTemperatureRange"][0] for m in annual_metadata.values()),
                                          max(m["fullPrecisionTemperatureRange"][1] for m in annual_metadata.values())],
        "fullPrecisionPrecipitationRange": [min(m["fullPrecisionPrecipitationRange"][0] for m in annual_metadata.values()),
                                            max(m["fullPrecisionPrecipitationRange"][1] for m in annual_metadata.values())],
        "annualMaskVariation": "Each year independently requires all 12 finite monthly values of both variables. Masks may vary across years; raw and smoothed masks are identical within each year.",
        "assetSha256": {filename: sha for metadata in annual_metadata.values()
                        for filename, sha in metadata["assetSha256"].items()},
    })
    manifest["assetSha256"]["europe.geojson"] = digest(border)
    manifest["totalCompressedBytes"] = sum((args.output_dir / filename).stat().st_size
                                            for filename in manifest["assetSha256"] if filename.endswith(".gz"))
    # Publish only once every requested year and both pooled distributions are complete.
    write_json(args.output_dir / "metadata.json", manifest)
    if years == list(range(FIRST_YEAR, LAST_YEAR + 1)):
        for filename in ("climate.bin", "smoothed.bin"):
            (args.output_dir / filename).unlink(missing_ok=True)
    print(json.dumps({key: manifest[key] for key in
                      ("years", "pooledValidCellYears", "validCellsRange", "totalCompressedBytes",
                       "temperatureBreaks", "precipitationBreaks", "equalTemperatureBreaks",
                       "equalPrecipitationBreaks")}, indent=2), flush=True)


if __name__ == "__main__":
    main()
