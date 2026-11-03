# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "shapely>=2.0",
# ]
# ///

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from _safe_io import write_vector_atomic
from _budget import check_vector


def fix_geometries(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    invalid_mask = ~gdf.geometry.is_valid & gdf.geometry.notna()
    count = invalid_mask.sum()
    if count > 0:
        gdf.loc[invalid_mask, gdf.geometry.name] = gdf.loc[invalid_mask].geometry.apply(shapely.make_valid)
    return gdf, int(count)


def remove_null_geom(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    mask = gdf.geometry.isna()
    count = mask.sum()
    return gdf[~mask].copy(), int(count)


def remove_empty_geom(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    mask = gdf.geometry.notna() & gdf.geometry.is_empty
    count = mask.sum()
    return gdf[~mask].copy(), int(count)


def remove_duplicates(gdf: gpd.GeoDataFrame, by: str) -> tuple[gpd.GeoDataFrame, int]:
    before = len(gdf)
    attr_cols = [c for c in gdf.columns if c != gdf.geometry.name]

    if by == "geometry":
        gdf = gdf.drop_duplicates(subset=[gdf.geometry.name])
    elif by == "attributes":
        gdf = gdf.drop_duplicates(subset=attr_cols)
    else:
        keys = gdf[attr_cols].copy()
        # Geometry is a Series outside the original attribute namespace.
        duplicate_attributes = keys.apply(tuple, axis=1)
        duplicate_geometry = gdf.geometry.to_wkb()
        mask = pd.DataFrame({'attributes': duplicate_attributes, 'geometry': duplicate_geometry}).duplicated()
        gdf = gdf.loc[~mask].copy()

    return gdf, before - len(gdf)


def looks_garbled(value: str) -> bool:
    """A Latin-1 symbol or control next to another high character: bytes read in the wrong encoding, not spelling.

    Accented letters alone ('ÄÖ', 'Müller') prove nothing: they also decode as GBK, but they are ordinary text.
    """
    return any(re.search("[\x80-\xbf\xd7\xf7]", run) for run in re.findall("[\x80-\xff]{2,}", value))


def fix_encoding(gdf: gpd.GeoDataFrame, source_encoding: str) -> tuple[gpd.GeoDataFrame, int, list[str]]:
    """Repair a field only when it proves garbled: every non-ASCII value decodes and at least one looks garbled.

    Any other field stays as it was, and the returned notes say why.
    """
    fixed = 0
    notes: list[str] = []
    for col in gdf.select_dtypes(include=["object"]).columns:
        if col == gdf.geometry.name:
            continue
        repaired: dict[str, str] = {}
        others: set[str] = set()
        for val in gdf[col]:
            if isinstance(val, str) and not val.isascii():
                try:
                    decoded = val.encode("latin-1").decode(source_encoding)
                    if decoded != val:
                        repaired[val] = decoded
                except (UnicodeDecodeError, UnicodeEncodeError):
                    others.add(val)
        if not repaired:
            continue
        before, after = next(iter(repaired.items()))
        changed = int(gdf[col].map(lambda v: isinstance(v, str) and v in repaired).sum())
        candidates = f"{changed} values could be {source_encoding} read as Latin-1 (such as {before!r} -> {after!r})"
        if others:
            notes.append(f"Encoding left unchanged in '{col}': {candidates}, but {len(others)} other values could not (such as {min(others)!r})")
        elif not any(looks_garbled(val) for val in repaired):
            notes.append(f"Encoding left unchanged in '{col}': {candidates}, but nothing shows they are garbled")
        else:
            gdf[col] = gdf[col].map(lambda v: repaired.get(v, v) if isinstance(v, str) else v)
            fixed += changed
            notes.append(f"Encoding fixed in '{col}': {changed} values (such as {before!r} -> {after!r})")
    return gdf, fixed, notes


def drop_null_fields(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[str]]:
    null_cols = [c for c in gdf.columns if c != gdf.geometry.name and gdf[c].isna().all()]
    if null_cols:
        gdf = gdf.drop(columns=null_cols)
    return gdf, null_cols


def main() -> None:
    parser = argparse.ArgumentParser(description="Geometry repair and data cleaning")
    parser.add_argument("input", help="Input file path")
    parser.add_argument("--output", help="Output file path (default: input_fixed.gpkg)")
    parser.add_argument("--layer", help="Layer name")
    parser.add_argument("--fix-geom", action=argparse.BooleanOptionalAction, default=True, help="Fix invalid geometries")
    parser.add_argument("--remove-null-geom", action="store_true", help="Remove features with null geometry")
    parser.add_argument("--remove-empty", action="store_true", help="Remove empty geometries")
    parser.add_argument("--remove-duplicates", action="store_true", help="Remove duplicate features")
    parser.add_argument("--dedupe-by", choices=["geometry", "attributes", "both"], default="both")
    parser.add_argument("--fix-encoding", action="store_true", help="Repair text garbled by reading --source-encoding bytes as Latin-1; a field that does not prove garbled is left unchanged and reported")
    parser.add_argument("--source-encoding", default="gbk", help="Source encoding (default: gbk)")
    parser.add_argument("--drop-null-fields", action="store_true", help="Drop entirely null fields")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else input_path.with_name(f"{input_path.stem}_fixed.gpkg")

    read_kwargs: dict = {"engine": "pyogrio"}
    if args.layer:
        read_kwargs["layer"] = args.layer
    check_vector(input_path, args.layer)
    gdf = gpd.read_file(input_path, **read_kwargs)

    total = len(gdf)
    report: list[str] = [f"Input: {input_path} ({total} features)"]

    if args.fix_geom:
        gdf, count = fix_geometries(gdf)
        report.append(f"Fixed geometries: {count}")

    if args.remove_null_geom:
        gdf, count = remove_null_geom(gdf)
        report.append(f"Removed null geometries: {count}")

    if args.remove_empty:
        gdf, count = remove_empty_geom(gdf)
        report.append(f"Removed empty geometries: {count}")

    if args.remove_duplicates:
        gdf, count = remove_duplicates(gdf, args.dedupe_by)
        report.append(f"Removed duplicates (by {args.dedupe_by}): {count}")

    if args.fix_encoding:
        gdf, count, notes = fix_encoding(gdf, args.source_encoding)
        report.append(f"Fixed encoding values: {count}")
        report.extend(notes)

    if args.drop_null_fields:
        gdf, cols = drop_null_fields(gdf)
        report.append(f"Dropped null fields: {len(cols)}" + (f" ({', '.join(cols)})" if cols else ""))

    report.append(f"Output: {output_path} ({len(gdf)} features)")

    write_vector_atomic(gdf, output_path, overwrite=args.overwrite, protected_paths=[input_path])

    print("\n".join(report))


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
