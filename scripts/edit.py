# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "shapely>=2.0",
# ]
# ///
import argparse
import json
import sys

import geopandas as gpd
from pathlib import Path
from _safe_io import write_vector_atomic
from shapely import wkt
from _budget import check_vector


def read_gdf(path, layer=None):
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def save_gdf(gdf, output, input_path):
    source = Path(input_path)
    dest = output or source.with_name(source.stem + "_edited.gpkg")
    write_vector_atomic(gdf, dest, protected_paths=[input_path])
    print(f"Saved to {dest}")


def preview(gdf, mask, yes):
    affected = gdf[mask]
    n = len(affected)
    if n == 0:
        print("No features matched.")
        sys.exit(0)
    print(f"{n} feature(s) matched.")
    if not yes:
        print(affected.head().to_string())
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            sys.exit(0)
    return affected, n


def cmd_query(args):
    gdf = read_gdf(args.input, args.layer)
    result = gdf.query(args.condition)
    print(f"{len(result)} feature(s) matched.")
    print(result.to_string())


def cmd_update(args):
    gdf = read_gdf(args.input, args.layer)
    mask = gdf.eval(args.where)
    _, n = preview(gdf, mask, args.yes)
    for assignment in args.set:
        field, expr = assignment.split("=", 1)
        field = field.strip()
        expr = expr.strip()
        if "{" in expr:
            gdf.loc[mask, field] = gdf[mask].apply(
                lambda row: expr.format(**{c: row[c] for c in gdf.columns}), axis=1
            )
        else:
            try:
                val = json.loads(expr)
            except (json.JSONDecodeError, ValueError):
                val = expr
            gdf.loc[mask, field] = val
    print(f"{n} feature(s) updated.")
    save_gdf(gdf, args.output, args.input)


def cmd_delete(args):
    gdf = read_gdf(args.input, args.layer)
    mask = gdf.eval(args.where)
    _, n = preview(gdf, mask, args.yes)
    gdf = gdf[~mask].reset_index(drop=True)
    print(f"{n} feature(s) deleted.")
    save_gdf(gdf, args.output, args.input)


def cmd_add(args):
    gdf = read_gdf(args.input, args.layer)
    geom = wkt.loads(args.geom)
    attrs = json.loads(args.attrs) if args.attrs else {}
    attrs["geometry"] = geom
    new_row = gpd.GeoDataFrame([attrs], geometry="geometry", crs=gdf.crs)
    gdf = gpd.GeoDataFrame(
        __import__("pandas").concat([gdf, new_row], ignore_index=True),
        geometry="geometry",
        crs=gdf.crs,
    )
    print("1 feature added.")
    save_gdf(gdf, args.output, args.input)


def cmd_calc(args):
    gdf = read_gdf(args.input, args.layer)
    gdf[args.field] = gdf.apply(lambda row: eval(args.expr), axis=1)  # noqa: S307
    print(f"Calculated field '{args.field}' for {len(gdf)} feature(s).")
    if not args.yes:
        print(gdf[[args.field]].head().to_string())
    save_gdf(gdf, args.output, args.input)


def main():
    parser = argparse.ArgumentParser(description="Vector attribute editor")
    parser.add_argument("input", help="Input vector file")
    parser.add_argument("--layer", help="Layer name")
    parser.add_argument("--output", "-o", help="Output file (default: overwrite input)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_query = sub.add_parser("query")
    p_query.add_argument("condition", help="Pandas query expression")

    p_update = sub.add_parser("update")
    p_update.add_argument("--where", required=True)
    p_update.add_argument("--set", required=True, action="append")

    p_delete = sub.add_parser("delete")
    p_delete.add_argument("--where", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--geom", required=True, help="WKT geometry")
    p_add.add_argument("--attrs", help="JSON attributes")

    p_calc = sub.add_parser("calc")
    p_calc.add_argument("--field", required=True)
    p_calc.add_argument("--expr", required=True, help="Python expression using row.*")

    args = parser.parse_args()
    {"query": cmd_query, "update": cmd_update, "delete": cmd_delete, "add": cmd_add, "calc": cmd_calc}[args.command](args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
