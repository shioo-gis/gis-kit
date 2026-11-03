# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "geopy>=2.4",
# ]
# ///

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from geopy.geocoders import Nominatim
from shapely.geometry import Point

from _core import reproject


def make_geocoder(provider: str, api_key: str | None, delay: float):
    if provider == "nominatim":
        return Nominatim(user_agent="gis-geocode-script", timeout=10)
    elif provider == "gaode":
        if not api_key:
            print("ERROR: --api-key is required for gaode provider", file=sys.stderr)
            sys.exit(1)
        from geopy.geocoders import Gaode
        return Gaode(api_key=api_key, timeout=10)
    else:
        print(f"ERROR: Unknown provider '{provider}'", file=sys.stderr)
        sys.exit(1)


def read_input(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".csv":
        return pd.read_csv(path)
    elif ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    else:
        print(f"ERROR: Unsupported input format '{ext}'. Use .csv or .xlsx", file=sys.stderr)
        sys.exit(1)


def geocode_batch(
    df: pd.DataFrame,
    address_field: str,
    geocoder,
    delay: float,
    skip_errors: bool,
) -> tuple[list[dict], list[dict]]:
    results = []
    failures = []
    total = len(df)

    for i, (idx, row) in enumerate(df.iterrows()):
        address = str(row[address_field]).strip()
        if not address or address == "nan":
            failures.append({"index": idx, "address": address, "error": "empty address"})
            print(f"  [{i+1}/{total}] SKIP (empty)")
            continue

        try:
            location = geocoder.geocode(address)
            if location:
                results.append({
                    "index": idx,
                    "address": address,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "matched": location.address,
                    **{k: v for k, v in row.items() if k != address_field},
                })
                print(f"  [{i+1}/{total}] OK: {address}")
            else:
                failures.append({"index": idx, "address": address, "error": "no result"})
                print(f"  [{i+1}/{total}] FAIL: {address} (no result)")
                if not skip_errors:
                    print("ERROR: Geocoding failed. Use --skip-errors to continue.", file=sys.stderr)
                    sys.exit(1)
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            failures.append({"index": idx, "address": address, "error": str(e)})
            print(f"  [{i+1}/{total}] FAIL: {address} ({e})")
            if not skip_errors:
                print("ERROR: Geocoding failed. Use --skip-errors to continue.", file=sys.stderr)
                sys.exit(1)

        if i < total - 1:
            time.sleep(delay)

    return results, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch geocode addresses to point layer")
    parser.add_argument("--input", required=True, help="Input CSV/Excel file with addresses")
    parser.add_argument("--output", default="geocoded.gpkg", help="Output file (default: geocoded.gpkg)")
    parser.add_argument("--address-field", required=True, help="Column name containing addresses")
    parser.add_argument("--provider", default="nominatim", choices=["nominatim", "gaode"], help="Geocoding provider")
    parser.add_argument("--api-key", help="API key for commercial providers")
    parser.add_argument("--crs", default="EPSG:4326", help="Output CRS (default: EPSG:4326)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests in seconds (default: 1.0)")
    parser.add_argument("--skip-errors", action="store_true", help="Continue on geocoding failures")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    df = read_input(input_path)
    if args.address_field not in df.columns:
        print(f"ERROR: Field '{args.address_field}' not found. Available: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    print(f"Geocoding {len(df)} addresses using {args.provider}...")
    geocoder = make_geocoder(args.provider, args.api_key, args.delay)
    results, failures = geocode_batch(df, args.address_field, geocoder, args.delay, args.skip_errors)

    if results:
        rdf = pd.DataFrame(results)
        geometry = [Point(xy) for xy in zip(rdf["longitude"], rdf["latitude"])]
        gdf = gpd.GeoDataFrame(rdf, geometry=geometry, crs="EPSG:4326")
        gdf = gdf.drop(columns=["index"])

        if args.crs != "EPSG:4326":
            gdf = reproject(gdf, args.crs, purpose='publish')

        output_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(output_path, engine="pyogrio")
        print(f"{len(results)}/{len(df)} addresses geocoded -> {output_path}")
    else:
        print("No addresses were successfully geocoded.", file=sys.stderr)

    if failures:
        fail_path = output_path.with_name(output_path.stem + "_failed.csv")
        pd.DataFrame(failures).to_csv(fail_path, index=False)
        print(f"{len(failures)} failures saved to {fail_path}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
