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
import unicodedata
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from _budget import check_vector
from _safe_io import write_vector_atomic


def sanitize(value: str) -> str:
    return re.sub(r'[^\w\-]', '_', str(value)).strip('_')


def output_name(prefix: str, value, extension: str) -> str:
    return f"{prefix}_{'NULL' if pd.isna(value) else sanitize(value)}.{extension}"


def same_file_key(name: str) -> str:
    """What decides that two names are one file on a case- or normalization-insensitive volume."""
    return unicodedata.normalize("NFC", name).casefold()


def check_targets(groups: list, input_path: Path, overwrite: bool) -> None:
    """Refuse, before the first write, whatever would lose rows or replace a file unasked."""
    by_file: dict[str, list] = {}
    for value, _, path in groups:
        by_file.setdefault(same_file_key(path.name), []).append((value, path.name))
    clashes = [entries for entries in by_file.values() if len(entries) > 1]
    if clashes:
        lines = [f"  {' and '.join('null' if pd.isna(v) else repr(v) for v, _ in entries)} -> {entries[0][1]}" for entries in clashes]
        sys.exit("Error: these values would share one output file and lose rows; nothing was written. "
                 "Change the values or split by another field.\n" + "\n".join(lines))
    if any(path.resolve() == input_path.resolve() for _, _, path in groups):
        sys.exit(f"Error: an output would replace the input {input_path}; nothing was written. Use --output-dir or --prefix.")
    existing = [str(path) for _, _, path in groups if path.exists()]
    if existing and not overwrite:
        sys.exit("Error: outputs already exist; nothing was written. Pass --overwrite to replace them:\n  " + "\n  ".join(existing))


def write_group(group: gpd.GeoDataFrame, path: Path, input_path: Path, overwrite: bool) -> int:
    """Write one value's rows and return the count read back from the file."""
    if path.suffix == ".shp":  # Several files per output, so outside the atomic writer.
        group.to_file(path, engine="pyogrio")
        return int(pyogrio.read_info(path, force_feature_count=True)["features"])
    return write_vector_atomic(group, path, overwrite=overwrite, protected_paths=[input_path])["features"]


def main():
    parser = argparse.ArgumentParser(description="Split layer into files by field value")
    parser.add_argument("input", help="Input file path")
    parser.add_argument("--output-dir", help="Output directory (default: current dir)")
    parser.add_argument("--layer", help="Layer name to read")
    parser.add_argument("--by", required=True, help="Field to split by")
    parser.add_argument("--format", default="gpkg", choices=["gpkg", "shp", "geojson"], help="Output format")
    parser.add_argument("--prefix", help="Filename prefix (default: input filename)")
    parser.add_argument("--overwrite", action="store_true", help="Replace outputs that already exist")
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

    # Rows whose value is null go to their own file rather than disappearing.
    groups = [(value, group, output_dir / output_name(prefix, value, args.format))
              for value, group in gdf.groupby(args.by, dropna=False)]
    check_targets(groups, input_path, args.overwrite)

    files = written = 0
    for value, group_gdf, out_path in groups:
        try:
            features = write_group(group_gdf, out_path, input_path, args.overwrite)
        except (ValueError, OSError, RuntimeError) as e:
            sys.exit(f"Error writing {out_path}: {e}\n{files} files with {written} of {len(gdf)} features were written before this.")
        files += 1
        written += features
        print(f"  {out_path} ({features} features{', null value' if pd.isna(value) else ''})")

    if written != len(gdf):
        sys.exit(f"Error: {files} files hold {written} features, but the input has {len(gdf)}.")
    print(f"\n{files} files created in {output_dir} ({written} of {len(gdf)} features)")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
