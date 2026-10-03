#!/usr/bin/env python3
"""Download published climate-map assets, or a region/year preview, without deletion."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
from urllib.parse import quote

import requests

ASSET_ROOT = "https://www.danielpradilla.info/projects/bivariate-climate-map/data/"
CHUNK_SIZE = 1024 * 1024
HTTP_OPTIONS = {"headers": {"Accept-Encoding": "identity"}, "timeout": (30, 180)}


def asset_path(relative):
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or ":" in relative:
        raise ValueError(f"Invalid published asset path: {relative!r}")
    return path


def asset_url(relative):
    return ASSET_ROOT + quote(str(asset_path(relative)), safe="/")


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def fetch_document(relative, expected_hash=None):
    with requests.get(asset_url(relative), **HTTP_OPTIONS) as response:
        response.raise_for_status()
        payload = response.content
    if expected_hash and hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f"SHA256 mismatch: {relative}")
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError(f"Expected JSON object: {relative}")
    return payload, document


def atomic_write(path, payload):
    # Exact existing metadata is left untouched, including its modification time.
    if path.is_file() and digest(path) == hashlib.sha256(payload).hexdigest():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                     suffix=".part", delete=False) as temporary:
        temporary.write(payload)
        staging = Path(temporary.name)
    try:
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def validate_unhashed(path, relative, expected_bytes=None):
    if relative.endswith(".gz"):
        size = 0
        with gzip.open(path, "rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                size += len(chunk)
        if expected_bytes is not None and size != expected_bytes:
            raise ValueError(f"Decoded size mismatch: {relative}: {size} != {expected_bytes}")
    elif relative.endswith((".json", ".geojson")):
        with path.open("rb") as source:
            value = json.load(source)
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {relative}")
    elif not path.stat().st_size:
        raise ValueError(f"Empty asset: {relative}")


def download_file(output, relative, expected_hash=None, expected_bytes=None):
    target = output / asset_path(relative)
    if expected_hash and target.is_file() and digest(target) == expected_hash:
        return f"Reused {relative}"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with requests.get(asset_url(relative), stream=True, **HTTP_OPTIONS) as response:
            response.raise_for_status()
            checksum = hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".",
                                             suffix=".part", delete=False) as temporary:
                staging = Path(temporary.name)
                # Do not transparently decompress .bin.gz HTTP responses: hashes
                # in assetSha256 identify the actual compressed file bytes.
                for chunk in response.raw.stream(CHUNK_SIZE, decode_content=False):
                    temporary.write(chunk)
                    checksum.update(chunk)
        if expected_hash:
            if checksum.hexdigest() != expected_hash:
                raise ValueError(f"SHA256 mismatch: {relative}")
        else:
            validate_unhashed(staging, relative, expected_bytes)
        if target.is_file() and digest(target) == checksum.hexdigest():
            return f"Reused {relative}"
        staging.replace(target)
        return f"Downloaded {relative}"
    except Exception as error:
        raise RuntimeError(f"Could not acquire {relative}: {error}") from error
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def download_frame(output, manifest, frame):
    relative = frame["metadataFile"]
    hashes = manifest.get("assetSha256", {})
    payload, annual = fetch_document(relative, hashes.get(relative))
    for key in ("year", "rawFile", "smoothedFile", "metadataFile", "validCells"):
        if annual.get(key) != frame.get(key):
            raise ValueError(f"Annual metadata disagrees with frame index: {relative} ({key})")
    annual_hashes = annual.get("assetSha256", {})
    cells = manifest.get("storedCells", manifest["width"] * manifest["height"])
    for key in ("rawFile", "smoothedFile"):
        filename = frame[key]
        manifest_hash = hashes.get(filename)
        annual_hash = annual_hashes.get(filename)
        if manifest_hash and annual_hash and manifest_hash != annual_hash:
            raise ValueError(f"Conflicting published SHA256 values: {filename}")
        download_file(output, filename, manifest_hash or annual_hash, cells * 4)
    # A frame's provenance is exposed only once both grids are complete.
    atomic_write(output / asset_path(relative), payload)
    return f"Ready {relative}"


def bounded_downloads(tasks):
    """Keep only four futures live, regardless of the requested history size."""
    iterator = iter(tasks)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = set()
        for _ in range(4):
            task = next(iterator, None)
            if task is not None:
                function, arguments = task
                pending.add(pool.submit(function, *arguments))
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                print(future.result(), flush=True)
                task = next(iterator, None)
                if task is not None:
                    function, arguments = task
                    pending.add(pool.submit(function, *arguments))


def encode_document(document):
    return (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def preview_manifest(source, years, output):
    manifest = copy.deepcopy(source)
    manifest["years"] = years
    manifest["frames"] = {str(year): source["frames"][str(year)] for year in years}
    default = source.get("defaultYear")
    manifest["defaultYear"] = default if default in years else max(years)
    frame = manifest["frames"][str(manifest["defaultYear"])]
    for key in ("rawFile", "smoothedFile", "metadataFile", "validCells"):
        manifest[key] = frame[key]
    if "year" in manifest:
        manifest["year"] = frame["year"]
    annual = json.loads((output / asset_path(frame["metadataFile"])).read_bytes())
    if "representativeValues" in annual:
        manifest["representativeValues"] = annual["representativeValues"]
    if "terraClimate" in annual.get("sources", {}):
        manifest["sources"] = annual["sources"]
    elif "sources" in annual:
        manifest["sources"]["terraClimate"] = annual["sources"]
    for key in ("meanFillTmaxC", "meanFillPptMm"):
        if key in manifest.get("smoothing", {}) and key in annual.get("smoothing", {}):
            manifest["smoothing"][key] = annual["smoothing"][key]
    # Baseline and all physical break/range arrays remain historical, not refit
    # to the preview. Preserve an existing sourceBaseline verbatim.
    if "sourceBaseline" not in manifest:
        manifest["sourceBaseline"] = copy.deepcopy(source.get("baseline", {
            "startYear": min(source["years"]), "endYear": max(source["years"]),
            "complete": True,
        }))
    selected = {source["borderFile"]}
    if source.get("cellIndexFile"):
        selected.add(source["cellIndexFile"])
    for record in manifest["frames"].values():
        selected.update(record[key] for key in ("rawFile", "smoothedFile", "metadataFile"))
    manifest["assetSha256"] = {path: checksum for path, checksum in source.get("assetSha256", {}).items()
                               if path in selected}
    manifest["totalCompressedBytes"] = sum((output / asset_path(path)).stat().st_size
                                             for path in selected if path.endswith(".gz"))
    if "coverageEvidence" in manifest:
        manifest["coverageEvidence"] = {str(year): source["coverageEvidence"][str(year)]
                                        for year in years if str(year) in source["coverageEvidence"]}
    manifest["downloadSelection"] = {"assetRoot": ASSET_ROOT, "years": years,
                                      "note": "Available frames are limited locally; historical thresholds and source baseline are unchanged."}
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", action="append", help="Region id; repeat to select several (default: all eight)")
    parser.add_argument("--year", type=int, action="append", help="Year; repeat to select several (default: 1950–2025)")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    args = parser.parse_args()
    registry_bytes, registry = fetch_document("regions/index.json")
    known = {region["id"] for region in registry["regions"]}
    requested = set(args.region) if args.region else known
    unknown = requested - known
    if unknown:
        parser.error(f"Unknown region(s): {', '.join(sorted(unknown))}; choose from {', '.join(sorted(known))}")
    years = sorted(set(args.year if args.year else range(1950, 2026)))
    if any(year < 1950 or year > 2025 for year in years):
        parser.error("Years must lie within 1950–2025")
    regions = [region for region in registry["regions"] if region["id"] in requested]
    manifests = []
    for region in regions:
        payload, manifest = fetch_document(region["metadataFile"])
        missing = set(years) - set(manifest["years"])
        if missing:
            raise ValueError(f"{region['id']}: published frames unavailable for {sorted(missing)}")
        for year in years:
            frame = manifest["frames"][str(year)]
            if frame["year"] != year:
                raise ValueError(f"{region['id']}: frame year mismatch for {year}")
            for key in ("rawFile", "smoothedFile", "metadataFile"):
                asset_path(frame[key])
        manifests.append((region, payload, manifest))

    def tasks():
        provenance = set()
        for _, _, manifest in manifests:
            hashes = manifest.get("assetSha256", {})
            shared = [(manifest["borderFile"], None)]
            if manifest.get("cellIndexFile"):
                shared.append((manifest["cellIndexFile"], manifest["storedCells"] * 4))
            for path, size in shared:
                yield download_file, (args.output_dir, path, hashes.get(path), size)
            for year in years:
                evidence = manifest.get("coverageEvidence", {}).get(str(year), {})
                path = evidence.get("sourceMetadataFile")
                if path and path not in provenance:
                    provenance.add(path)
                    yield download_file, (args.output_dir, path, hashes.get(path))
                yield download_frame, (args.output_dir, manifest, manifest["frames"][str(year)])

    bounded_downloads(tasks())
    # Publish app-visible metadata only after the entire selection succeeds.
    for region, payload, manifest in manifests:
        if years != manifest["years"]:
            payload = encode_document(preview_manifest(manifest, years, args.output_dir))
        atomic_write(args.output_dir / asset_path(region["metadataFile"]), payload)
    if requested != known:
        registry = copy.deepcopy(registry)
        registry["regions"] = regions
        if registry["defaultRegion"] not in requested:
            registry["defaultRegion"] = regions[0]["id"]
        registry_bytes = encode_document(registry)
    atomic_write(args.output_dir / "regions" / "index.json", registry_bytes)
    print(f"Ready: {len(regions)} region(s), {len(years)} year(s) each in {args.output_dir}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, requests.RequestException) as error:
        print(f"Asset download failed: {error}", file=sys.stderr)
        sys.exit(1)
