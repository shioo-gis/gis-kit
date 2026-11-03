# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "shapely>=2.0",
# ]
# ///
"""Dissolve (merge geometries by attribute)."""

import argparse
import math
import sys
from pathlib import Path

import pyogrio
import shapely

from _safe_io import AtomicVectorOutput, write_vector_atomic
from _core import check_geometry, read_vector


def parse_agg(agg_str: str) -> dict:
    rules = {}
    for pair in agg_str.split(","):
        field, func = pair.strip().split(":")
        rules[field.strip()] = func.strip()
    return rules


def finish_dissolve(gdf, by, aggfunc, multi, method):
    by_fields = list(by or [])
    unknown_groups = set(by_fields) - set(gdf.columns)
    if unknown_groups:
        raise ValueError(f"Unknown dissolve field(s): {', '.join(sorted(unknown_groups))}")
    attribute_fields = [field for field in gdf.columns if field != gdf.geometry.name and field not in by_fields]
    if isinstance(aggfunc, dict):
        unknown_aggregates = set(aggfunc) - set(attribute_fields)
        if unknown_aggregates:
            raise ValueError(f"Unknown aggregate field(s), or group fields used as aggregates: {', '.join(sorted(unknown_aggregates))}")
        rules = dict(aggfunc)
    else:
        rules = {field: aggfunc for field in attribute_fields}

    def normalize(function):
        if function == "first":
            return lambda series: series.iloc[0]
        if function == "sum":
            return lambda series: series.sum(min_count=1)
        return function

    rules = {field: normalize(function) for field, function in rules.items()}
    work = gdf
    temporary_field = None
    if not rules:
        # GeoPandas/pandas reject an empty aggregation map; a private sentinel
        # preserves the group geometry without exporting a fake attribute.
        temporary_field = "__gis_dissolve_sentinel__"
        while temporary_field in gdf.columns:
            temporary_field += "_"
        work = gdf.copy()
        work[temporary_field] = 0
        rules[temporary_field] = "first"

    dissolved = work.dissolve(by=by, aggfunc=rules, method=method, dropna=False)
    if temporary_field:
        dissolved = dissolved.drop(columns=[temporary_field])
    if by:
        dissolved = dissolved.reset_index()
    else:
        dissolved = dissolved.reset_index(drop=True)
    if not multi:
        return dissolved.explode(index_parts=False, ignore_index=True)
    return dissolved


def validate_coverage(gdf, dissolved):
    input_geometries = gdf.geometry.array
    output_geometries = dissolved.geometry.array
    input_area = shapely.area(input_geometries).sum()
    output_area = shapely.area(output_geometries).sum()
    if not shapely.is_valid(input_geometries).all() or not shapely.is_valid(output_geometries).all() or not math.isclose(input_area, output_area, rel_tol=1e-9, abs_tol=1e-9):
        sys.exit("Coverage dissolve is invalid or changes area; use --method unary")


def main():
    parser = argparse.ArgumentParser(description="Dissolve geometries by attribute")
    parser.add_argument("input", help="Input file path")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--layer", help="Layer name to read")
    parser.add_argument("--by", help="Field(s) to dissolve by, comma-separated")
    parser.add_argument("--agg", help="Aggregation rules: field:func,field:func")
    parser.add_argument(
        "--method",
        choices=["unary", "coverage"],
        default="unary",
        help="Union algorithm (coverage requires valid, non-overlapping polygons)",
    )
    parser.add_argument(
        "--multi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep multipart (default) or explode to parts; aggregate attributes are copied to every part",
    )
    parser.add_argument(
        "--no-global-merge",
        action="store_true",
        help="Write independent block summaries (bounded memory; groups may repeat and aggregates are not global)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
        help="Features per block with --no-global-merge (default: 10000)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing single-layer output file")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    output = Path(args.output) if args.output else input_path.with_stem(input_path.stem + "_dissolved").with_suffix(".gpkg")

    try:
        check_geometry(input_path, args.layer)
    except Exception as exc:
        sys.exit(f"Error reading input: {exc}")

    by = [f.strip() for f in args.by.split(",")] if args.by else None
    aggfunc = parse_agg(args.agg) if args.agg else "first"

    if not args.no_global_merge:
        gdf = read_vector(input_path, args.layer, require_geometry=False)
        dissolved = finish_dissolve(gdf, by, aggfunc, args.multi, args.method)
        if args.method == "coverage":
            validate_coverage(gdf, dissolved)
        try:
            write_vector_atomic(dissolved, output, overwrite=args.overwrite, protected_paths=[input_path])
        except Exception as exc:
            sys.exit(f"Dissolve output was not published: {exc}")
        print(f"Dissolved {len(gdf)} -> {len(dissolved)} features -> {output}")
        return

    if args.chunk_size <= 0:
        sys.exit("--chunk-size must be positive")

    info = pyogrio.read_info(input_path, layer=args.layer)
    total = info["features"]
    if total < 0:
        sys.exit("Input driver does not report a feature count; use the default dissolve mode")
    if total == 0:
        sys.exit("Input has no features")

    # Block mode emits per-block summaries; it never claims globally dissolved groups.
    written = 0
    rows = 0
    try:
        with AtomicVectorOutput(output, overwrite=args.overwrite, protected_paths=[input_path]) as staged:
            for start in range(0, total, args.chunk_size):
                chunk = pyogrio.read_dataframe(
                    input_path,
                    layer=args.layer,
                    skip_features=start,
                    max_features=args.chunk_size,
                )
                dissolved = finish_dissolve(chunk, by, aggfunc, args.multi, args.method)
                if args.method == "coverage":
                    validate_coverage(chunk, dissolved)
                staged.write(dissolved, append=written > 0)
                written += len(chunk)
                rows += len(dissolved)
                print(f"Processed {min(written, total)}/{total} features.")
            staged.commit(
                expected_features=rows,
                required_fields=by or [],
                expected_crs=info.get("crs"),
            )
    except Exception as exc:
        sys.exit(f"Block dissolve failed; no incomplete output was published: {exc}")

    print(f"Block-dissolved {total} features -> {rows} rows -> {output}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
