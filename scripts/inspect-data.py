# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "tabulate",
# ]
# ///

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import pyogrio
from _table import tabulate
from _core import read_vector
from _safe_io import iter_vector_chunks


def resolve_path(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        sys.exit(f"Error: file not found: {p}")
    return p


def list_layers(path: Path) -> list[dict]:
    try:
        info = pyogrio.list_layers(path)
    except Exception as e:
        sys.exit(f"Error reading layers: {e}")

    rows = []
    for name, geom_type in info:
        try:
            meta = pyogrio.read_info(path, layer=name)
            rows.append({
                "layer": name,
                "features": meta.get("features", "?"),
                "geometry_type": geom_type or "None",
                "crs": str(meta.get("crs", "Unknown")),
            })
        except Exception:
            rows.append({
                "layer": name,
                "features": "?",
                "geometry_type": geom_type or "None",
                "crs": "?",
            })
    return rows


def pick_layer(path: Path, layer: str | None) -> str:
    layers = pyogrio.list_layers(path)
    if len(layers) == 0:
        sys.exit("Error: no layers found in file")
    names = [l[0] for l in layers]
    if layer is None:
        return names[0]
    if layer not in names:
        sys.exit(f"Error: layer '{layer}' not found. Available: {', '.join(names)}")
    return layer


def read_layer(path: Path, layer: str, rows: int | None = None, *, require_geometry: bool = True) -> gpd.GeoDataFrame:
    try:
        return read_vector(path, layer, require_geometry=require_geometry, check_budget=rows is None, rows=rows)
    except Exception as e:
        sys.exit(f"Error reading layer '{layer}': {e}")


def cmd_layers(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    rows = list_layers(path)
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, allow_nan=False))
        return
    if not rows:
        print("No layers found.")
        return
    print(tabulate(rows, headers="keys", tablefmt="simple"))


def cmd_schema(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    layer = pick_layer(path, args.layer)
    # Nulls are a fact about every row, so scan them all; attributes only, a chunk at a time.
    try:
        info = pyogrio.read_info(path, layer=layer)
        with_null, samples = set(), {}
        for _, frame in iter_vector_chunks(path, layer=layer, read_geometry=False):
            with_null.update(frame.columns[frame.isna().any()])
            for col in frame.columns:
                seen = samples.setdefault(col, [])
                seen += frame[col].dropna().head(3 - len(seen)).tolist()
    except Exception as e:
        sys.exit(f"Error reading layer '{layer}': {e}")

    rows = []
    # The declared type: pandas turns an integer field with nulls into float64.
    for col, dtype in zip(info["fields"], info["dtypes"]):
        sample_str = ", ".join(str(v) for v in samples.get(col, []))
        rows.append({
            "field": str(col),
            "dtype": str(dtype),
            "nullable": str(col in with_null),
            "samples": sample_str[:80],
        })

    print(f"Layer: {layer}")
    print(f"Geometry column: {'geometry' if info['geometry_type'] else 'none (table)'}")
    print()
    print(tabulate(rows, headers="keys", tablefmt="simple"))


def cmd_summary(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    layer = pick_layer(path, args.layer)
    gdf = read_layer(path, layer)

    if getattr(args, "json", False):
        numeric = gdf.select_dtypes(include="number")
        result = {"layer": layer, "crs": str(gdf.crs) if gdf.crs else None, "features": len(gdf),
                  "bounds": gdf.total_bounds.tolist(), "fields": [c for c in gdf.columns if c != gdf.geometry.name],
                  "geometry_types": gdf.geometry.geom_type.value_counts().to_dict(),
                  "null_geometries": int(gdf.geometry.isna().sum()),
                  "numeric": json.loads(numeric.describe().to_json(double_precision=15)) if len(numeric.columns) else {}}
        # pandas encodes empty bounds / non-finite statistics as JSON null.
        import pandas as pd
        print(pd.Series(result).to_json(force_ascii=False, double_precision=15))
        return
    total_bounds = gdf.total_bounds
    bbox_str = f"({total_bounds[0]:.6f}, {total_bounds[1]:.6f}, {total_bounds[2]:.6f}, {total_bounds[3]:.6f})"

    geom_types = gdf.geometry.geom_type.value_counts()
    geom_rows = [{"geometry_type": k, "count": v} for k, v in geom_types.items()]

    print(f"Layer:    {layer}")
    print(f"CRS:      {gdf.crs}")
    print(f"Features: {len(gdf)}")
    print(f"Bbox:     {bbox_str}")
    print(f"Fields:   {len(gdf.columns) - 1}")
    print()

    print("Geometry type distribution:")
    print(tabulate(geom_rows, headers="keys", tablefmt="simple"))
    print()

    numeric_cols = gdf.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        stats = gdf[numeric_cols].describe().T
        stats = stats[["count", "mean", "min", "max"]].reset_index()
        stats.columns = ["field", "count", "mean", "min", "max"]
        for c in ["mean", "min", "max"]:
            stats[c] = stats[c].map(lambda v: f"{v:.4g}")
        print("Numeric field stats:")
        print(tabulate(stats.to_dict("records"), headers="keys", tablefmt="simple"))
        print()

    text_cols = gdf.select_dtypes(include="object").columns.tolist()
    if gdf.geometry.name in text_cols:
        text_cols.remove(gdf.geometry.name)
    if text_cols:
        text_rows = []
        for col in text_cols:
            series = gdf[col]
            text_rows.append({
                "field": col,
                "non_null": series.count(),
                "unique": series.nunique(),
            })
        print("Text field stats:")
        print(tabulate(text_rows, headers="keys", tablefmt="simple"))


def cmd_head(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    layer = pick_layer(path, args.layer)
    gdf = read_layer(path, layer, rows=args.n, require_geometry=False)

    df = gdf.drop(columns="geometry", errors="ignore") if isinstance(gdf, gpd.GeoDataFrame) else gdf
    print(f"Layer: {layer} (first {len(df)} rows)")
    print()
    print(tabulate(df, headers="keys", tablefmt="simple", showindex=False))


def cmd_values(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    layer = pick_layer(path, args.layer)
    gdf = read_layer(path, layer, require_geometry=False)

    field = args.field
    if field not in gdf.columns:
        available = [c for c in gdf.columns if c != "geometry"]
        sys.exit(f"Error: field '{field}' not found. Available: {', '.join(available)}")

    series = gdf[field]
    vc = series.value_counts(dropna=False)

    limit = args.top
    if limit and len(vc) > limit:
        vc = vc.head(limit)
        truncated = True
    else:
        truncated = False

    rows = []
    for val, count in vc.items():
        display = str(val) if val is not None else "<null>"
        rows.append({"value": display[:120], "count": count})

    print(f"Layer: {layer} | Field: {field}")
    print(f"Total: {len(series)} | Unique: {series.nunique()} | Null: {series.isna().sum()}")
    print()
    print(tabulate(rows, headers="keys", tablefmt="simple"))
    if truncated:
        print(f"\n(showing top {limit} of {series.nunique()} unique values)")


def cmd_bbox(args: argparse.Namespace) -> None:
    path = resolve_path(args.file)
    layer = pick_layer(path, args.layer)
    gdf = read_layer(path, layer)

    minx, miny, maxx, maxy = gdf.total_bounds
    print(f"Layer: {layer}")
    print(f"CRS:   {gdf.crs}")
    print(f"Min X: {minx}")
    print(f"Min Y: {miny}")
    print(f"Max X: {maxx}")
    print(f"Max Y: {maxy}")

    if args.format == "wkt":
        print(f"\nWKT: POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, {minx} {maxy}, {minx} {miny}))")
    elif args.format == "geojson":
        import json
        bbox_geojson = {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]]
        }
        print(f"\nGeoJSON:\n{json.dumps(bbox_geojson, indent=2)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="inspect-data",
        description="Inspect vector GIS data files (GPKG, GDB, Shapefile, GeoJSON, KML, ...)",
    )
    parser.add_argument("file", help="Path to the GIS data file")
    parser.add_argument("--layer", "-l", default=None, help="Layer name (defaults to first layer)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("layers", help="List all layers in the file").add_argument("--json", action="store_true", help="Pure JSON on stdout")
    sub.add_parser("schema", help="Show field structure of a layer")
    sub.add_parser("summary", help="Full overview of a layer").add_argument("--json", action="store_true", help="Pure JSON on stdout; unknown values are null")

    head_p = sub.add_parser("head", help="Preview first N rows")
    head_p.add_argument("-n", type=int, default=10, help="Number of rows (default: 10)")

    val_p = sub.add_parser("values", help="Show value distribution for a field")
    val_p.add_argument("field", help="Field name to inspect")
    val_p.add_argument("--top", type=int, default=None, help="Show only top N values")

    bbox_p = sub.add_parser("bbox", help="Print bounding box of a layer")
    bbox_p.add_argument("--format", "-f", choices=["plain", "wkt", "geojson"], default="plain",
                        help="Output format (default: plain)")

    args = parser.parse_args()

    dispatch = {
        "layers": cmd_layers,
        "schema": cmd_schema,
        "summary": cmd_summary,
        "head": cmd_head,
        "values": cmd_values,
        "bbox": cmd_bbox,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
