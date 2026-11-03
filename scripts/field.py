#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///
"""Field structure operations tool for geospatial layers."""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
from _metric import analysis_frame
from _safe_io import write_vector_atomic
from _budget import check_vector


BOOL_WORDS = {"true": True, "t": True, "yes": True, "y": True, "1": True,
              "false": False, "f": False, "no": False, "n": False, "0": False}


def bool_value(value):
    if isinstance(value, str):
        return BOOL_WORDS.get(value.strip().lower())
    try:
        return bool(value) if value in (0, 1) else None
    except (TypeError, ValueError):
        return None


def cast_bool(series):
    """Booleans from what each value says; nulls stay null, and any other value fails instead of becoming True."""
    present = series.dropna()
    known = present.map(bool_value)
    unknown = present[known.isna()].astype(str).unique()
    if len(unknown):
        raise ValueError(f"{len(unknown)} distinct values are not booleans, such as {', '.join(map(repr, unknown[:5]))}; "
                         f"accepted: true/false, t/f, yes/no, y/n, 1/0")
    return known.reindex(series.index).astype("boolean")


def read_input(args) -> gpd.GeoDataFrame:
    kwargs = {"engine": "pyogrio"}
    if hasattr(args, "layer") and args.layer:
        kwargs["layer"] = args.layer
    check_vector(args.input, kwargs.get("layer"))
    return gpd.read_file(args.input, **kwargs)


def write_output(gdf: gpd.GeoDataFrame, args) -> None:
    output = getattr(args, "output", None)
    inplace = getattr(args, "inplace", False)
    if output:
        dest = output
    elif inplace:
        dest = args.input
    else:
        dest = derive_output_path(args.input)
    if inplace:
        raise ValueError("--inplace is disabled: publish a new file with --output")
    write_vector_atomic(gdf, dest, overwrite=getattr(args, "overwrite", False),
                        protected_paths=[args.input])
    print(f"Written to {dest}")


def derive_output_path(raw_input: str) -> str:
    input_path = Path(raw_input)
    suffix = input_path.suffix.lower()

    if suffix == ".gdb" or input_path.is_dir():
        return str(input_path.with_name(f"{input_path.stem}_fields.gpkg"))

    if suffix:
        return str(input_path.with_name(f"{input_path.stem}_fields{input_path.suffix}"))

    return str(input_path.with_name(f"{input_path.name}_fields.gpkg"))


def cmd_list(args):
    gdf = read_input(args)
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    if not cols:
        print("No attribute fields.")
        return
    print(f"{'Field':<30} {'Type':<15} {'Nullable':<10} {'Sample Values'}")
    print("-" * 85)
    for col in cols:
        dtype = str(gdf[col].dtype)
        nullable = str(gdf[col].isna().any())
        samples = gdf[col].dropna().head(3).tolist()
        sample_str = ", ".join(str(s) for s in samples)
        if len(sample_str) > 40:
            sample_str = sample_str[:37] + "..."
        print(f"{col:<30} {dtype:<15} {nullable:<10} {sample_str}")


def cmd_rename(args):
    gdf = read_input(args)
    if args.src not in gdf.columns:
        print(f"Field '{args.src}' not found.", file=sys.stderr)
        sys.exit(1)
    if args.src == gdf.geometry.name:
        raise ValueError("Cannot rename active geometry with an attribute operation")
    if args.to in gdf.columns:
        raise ValueError(f"Field already exists: {args.to}")
    gdf = gdf.rename(columns={args.src: args.to})
    write_output(gdf, args)


def cmd_drop(args):
    gdf = read_input(args)
    fields = [f.strip() for f in args.fields.split(",")]
    missing = [f for f in fields if f not in gdf.columns]
    if missing:
        print(f"Fields not found: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if gdf.geometry.name in fields:
        raise ValueError("Cannot drop active geometry")
    gdf = gdf.drop(columns=fields)
    write_output(gdf, args)


def cmd_cast(args):
    gdf = read_input(args)
    if args.field not in gdf.columns:
        print(f"Field '{args.field}' not found.", file=sys.stderr)
        sys.exit(1)
    if args.field == gdf.geometry.name:
        raise ValueError("Cannot cast active geometry")
    type_map = {"int": "Int64", "float": "float64", "str": "str", "bool": "boolean"}
    target = type_map.get(args.type)
    if target is None:
        print(f"Unknown type '{args.type}'. Use: int, float, str, bool", file=sys.stderr)
        sys.exit(1)
    try:
        gdf[args.field] = cast_bool(gdf[args.field]) if args.type == "bool" else gdf[args.field].astype(target)
    except (ValueError, TypeError) as e:
        print(f"Cast failed: {e}", file=sys.stderr)
        sys.exit(1)
    write_output(gdf, args)


def cmd_add_area(args):
    gdf = read_input(args)
    name = args.name or "area_m2"
    if name in gdf.columns:
        raise ValueError(f"Field already exists: {name}")
    work, factor = analysis_frame(gdf, args.crs)
    gdf[name] = work.geometry.area * factor**2
    write_output(gdf, args)


def cmd_add_length(args):
    gdf = read_input(args)
    name = args.name or "length_m"
    if name in gdf.columns:
        raise ValueError(f"Field already exists: {name}")
    work, factor = analysis_frame(gdf, args.crs)
    gdf[name] = work.geometry.length * factor
    write_output(gdf, args)


def cmd_add_centroid(args):
    gdf = read_input(args)
    x_name = args.x_name or "centroid_x"
    y_name = args.y_name or "centroid_y"
    if x_name == y_name or x_name in gdf.columns or y_name in gdf.columns:
        raise ValueError("Centroid field collision")
    work, _ = analysis_frame(gdf, args.crs)
    centroids = work.geometry.centroid.to_crs(gdf.crs)
    gdf[x_name] = centroids.x
    gdf[y_name] = centroids.y
    write_output(gdf, args)


def add_common_args(sub):
    sub.add_argument("input", help="Input file path")
    sub.add_argument("--layer", help="Layer name")
    sub.add_argument("--output", help="Output file path (default: create sibling copy)")
    sub.add_argument("--overwrite", action="store_true")
    sub.add_argument("--inplace", action="store_true", help="Overwrite input file")


def main():
    parser = argparse.ArgumentParser(description="Field structure operations tool")
    subs = parser.add_subparsers(dest="command", required=True)

    p_list = subs.add_parser("list", help="List all fields")
    p_list.add_argument("input", help="Input file path")
    p_list.add_argument("--layer", help="Layer name")

    p_rename = subs.add_parser("rename", help="Rename a field")
    add_common_args(p_rename)
    p_rename.add_argument("--from", dest="src", required=True, help="Current field name")
    p_rename.add_argument("--to", required=True, help="New field name")

    p_drop = subs.add_parser("drop", help="Drop fields")
    add_common_args(p_drop)
    p_drop.add_argument("--fields", required=True, help="Comma-separated field names")

    p_cast = subs.add_parser("cast", help="Cast field type")
    add_common_args(p_cast)
    p_cast.add_argument("--field", required=True, help="Field name")
    p_cast.add_argument("--type", required=True, choices=["int", "float", "str", "bool"])

    p_area = subs.add_parser("add-area", help="Add area field")
    add_common_args(p_area)
    p_area.add_argument("--name", default="area_m2", help="Field name for area")
    p_area.add_argument("--crs", help="Projected CRS for calculation")

    p_length = subs.add_parser("add-length", help="Add length field")
    add_common_args(p_length)
    p_length.add_argument("--name", default="length_m", help="Field name for length")
    p_length.add_argument("--crs", help="Projected CRS for calculation")

    p_centroid = subs.add_parser("add-centroid", help="Add centroid X/Y fields")
    add_common_args(p_centroid)
    p_centroid.add_argument("--crs", help="Projected analysis CRS; output coordinates use source CRS")
    p_centroid.add_argument("--x-name", default="centroid_x", help="X field name")
    p_centroid.add_argument("--y-name", default="centroid_y", help="Y field name")

    args = parser.parse_args()
    dispatch = {
        "list": cmd_list,
        "rename": cmd_rename,
        "drop": cmd_drop,
        "cast": cmd_cast,
        "add-area": cmd_add_area,
        "add-length": cmd_add_length,
        "add-centroid": cmd_add_centroid,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
