# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///
"""Split a layer into multiple files by field value."""

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd
from _budget import check_vector


def sanitize(value: str) -> str:
    return re.sub(r'[^\w\-]', '_', str(value)).strip('_')


def main():
    parser = argparse.ArgumentParser(description="Split layer into files by field value")
    parser.add_argument("input", help="Input file path")
    parser.add_argument("--output-dir", help="Output directory (default: current dir)")
    parser.add_argument("--layer", help="Layer name to read")
    parser.add_argument("--by", required=True, help="Field to split by")
    parser.add_argument("--format", default="gpkg", choices=["gpkg", "shp", "geojson"], help="Output format")
    parser.add_argument("--prefix", help="Filename prefix (default: input filename)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = args.prefix or input_path.stem

    read_kwargs = {"engine": "pyogrio"}
    if args.layer:
        read_kwargs["layer"] = args.layer

    check_vector(input_path, args.layer)
    gdf = gpd.read_file(input_path, **read_kwargs)

    if args.by not in gdf.columns:
        sys.exit(f"Field '{args.by}' not found. Available: {', '.join(gdf.columns)}")

    groups = gdf.groupby(args.by)
    count = 0

    for value, group_gdf in groups:
        safe_value = sanitize(str(value))
        filename = f"{prefix}_{safe_value}.{args.format}"
        out_path = output_dir / filename
        group_gdf.to_file(out_path, engine="pyogrio")
        count += 1
        print(f"  {out_path} ({len(group_gdf)} features)")

    print(f"\n{count} files created in {output_dir}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
