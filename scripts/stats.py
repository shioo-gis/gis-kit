#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "matplotlib>=3.8",
#     "numpy",
#     "scipy",
#     "tabulate",
# ]
# ///
"""GIS vector data statistical analysis CLI."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from _sandbox import configure_writable_caches

configure_writable_caches()

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from _table import tabulate

try:
    from _metric import analysis_frame, analysis_frames, analysis_method
except ImportError:  # Support direct module loading by tests.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _metric import analysis_frame, analysis_frames, analysis_method
from _budget import check_vector


def _setup_chinese_font():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _theme import get_chinese_font
        name = get_chinese_font()
    except Exception:
        return
    if name != "DejaVu Sans":
        plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False


def _apply_theme(fig, ax):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _theme import apply_theme
        apply_theme(fig, ax)
    except ImportError:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()


def _read(path: str, layer: str | None = None) -> gpd.GeoDataFrame:
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def _save_or_show(fig, output: str | None):
    if output:
        fig.savefig(output, dpi=150, bbox_inches="tight")
        print(f"saved: {output}")
    else:
        plt.show()
    plt.close(fig)


# --- subcommands ---

def cmd_describe(args):
    gdf = _read(args.input, args.layer)
    numeric = gdf.select_dtypes(include="number")
    if numeric.empty:
        sys.exit("no numeric fields found")
    stats = numeric.describe().T
    stats.index.name = "field"
    if getattr(args, "json", False):
        print(stats.reset_index().to_json(orient="records", force_ascii=False, double_precision=15))
        return
    print(tabulate(stats.reset_index(), headers="keys", tablefmt="simple", floatfmt=".4f", showindex=False))


def cmd_frequency(args):
    gdf = _read(args.input, args.layer)
    if args.field not in gdf.columns:
        sys.exit(f"field not found: {args.field}")
    counts = gdf[args.field].value_counts().reset_index()
    counts.columns = [args.field, "count"]
    counts["pct"] = (counts["count"] / counts["count"].sum() * 100).round(2)
    print(tabulate(counts, headers="keys", tablefmt="simple", showindex=False))


def cmd_spatial(args):
    source = _read(args.input, args.layer)
    try:
        if args.study_area:
            study_source = _read(args.study_area, args.study_area_layer)
            (gdf, study), meters_per_unit = analysis_frames([source, study_source], args.analysis_crs)
            study_geom = study.geometry.union_all()
            keep = study_geom.covers(gdf.geometry.centroid)
            gdf = gdf.loc[keep].copy()
            study_area_m2 = float(study_geom.area * meters_per_unit**2)
            area_method = "study-area geometry; feature centroids covered"
            if study_area_m2 <= 0:
                raise ValueError("Study area must have positive area")
        else:
            gdf, meters_per_unit = analysis_frame(source, args.analysis_crs)
            bounds_for_area = gdf.total_bounds
            width, height = bounds_for_area[2] - bounds_for_area[0], bounds_for_area[3] - bounds_for_area[1]
            study_area_m2 = float(width * height * meters_per_unit**2)
            area_method = "layer-bounds bbox fallback (explicitly acknowledged)" if args.bbox_fallback else "layer-bounds bbox fallback (legacy default; assumption disclosed)"
        if not math.isfinite(study_area_m2) or study_area_m2 < 0:
            raise ValueError("Study area must have finite non-negative area")
        if gdf.empty:
            raise ValueError("No valid input features intersect the study area")
    except (ValueError, TypeError, OverflowError) as exc:
        raise SystemExit(f"Error: {exc}")

    bounds = gdf.total_bounds
    xmin, ymin, xmax, ymax = bounds
    width, height = xmax - xmin, ymax - ymin
    bounds_area_m2 = width * height * meters_per_unit**2
    n = len(gdf)

    rows = [
        ("feature count", n),
        ("extent xmin (analysis CRS units)", f"{xmin:.6f}"),
        ("extent ymin (analysis CRS units)", f"{ymin:.6f}"),
        ("extent xmax (analysis CRS units)", f"{xmax:.6f}"),
        ("extent ymax (analysis CRS units)", f"{ymax:.6f}"),
        ("extent width m", f"{width * meters_per_unit:.6f}"),
        ("extent height m", f"{height * meters_per_unit:.6f}"),
        ("bounding area m2", f"{bounds_area_m2:.6f}"),
        ("study area m2", f"{study_area_m2:.6f}" if study_area_m2 > 0 else "N/A: zero-area bbox"),
        ("study area method", area_method),
        ("feature density per km2", f"{n / study_area_m2 * 1_000_000:.6f}" if study_area_m2 > 0 else "N/A: zero-area denominator"),
        ("analysis CRS", str(gdf.crs)),
        ("analysis CRS method", analysis_method(source.crs, args.analysis_crs, used=gdf.crs)),
    ]

    if n > 1:
        from scipy.spatial import cKDTree
        centroids = gdf.geometry.centroid
        coords = np.column_stack([centroids.x, centroids.y])
        tree = cKDTree(coords)
        dists, _ = tree.query(coords, k=2)
        nn_dists_m = dists[:, 1] * meters_per_unit
        mean_nn_m = float(nn_dists_m.mean())
        expected_nn_m = 0.5 / np.sqrt(n / study_area_m2) if study_area_m2 > 0 else float("nan")
        nni = mean_nn_m / expected_nn_m if math.isfinite(expected_nn_m) else float("nan")
        rows += [
            ("mean nearest neighbor distance m", f"{mean_nn_m:.6f}"),
            ("CSR reference distance m (descriptive)", f"{expected_nn_m:.6f}" if math.isfinite(expected_nn_m) else "N/A: zero-area denominator"),
            ("nearest neighbor index (descriptive; no significance test)", f"{nni:.4f}" if math.isfinite(nni) else "N/A: zero-area denominator"),
        ]
    else:
        rows.append(("nearest neighbor statistics", "N/A: fewer than two features"))

    print(tabulate(rows, headers=["metric", "value"], tablefmt="simple"))


def cmd_cross(args):
    zones_source = _read(args.input, args.layer).reset_index(drop=True)
    targets_source = _read(args.target, args.target_layer).reset_index(drop=True)
    zone_id = args.zone_field
    if zone_id not in zones_source.columns:
        sys.exit(f"zone field not found: {zone_id}")
    if zones_source[zone_id].isna().any() or zones_source[zone_id].duplicated().any():
        sys.exit("zone field must be unique and non-null")
    result_names = ["count"] + (["sum_" + args.sum_field] if args.sum_field else [])
    if zone_id in result_names or len(set(result_names)) != len(result_names):
        sys.exit("zone ID field conflicts with a generated cross-statistics field")
    if args.sum_field:
        if args.sum_field not in targets_source.columns:
            sys.exit(f"sum field not found: {args.sum_field}")
        if not pd.api.types.is_numeric_dtype(targets_source[args.sum_field].dtype):
            sys.exit(f"sum field must be numeric: {args.sum_field}")

    try:
        (zones, targets), factor = analysis_frames([zones_source, targets_source], args.analysis_crs)
        # Run the relation on geometry-only frames so same-named attributes and
        # pandas index labels cannot change spatial identity or field selection.
        zone_geometries = gpd.GeoDataFrame({"_zone_position": np.arange(len(zones))}, geometry=zones.geometry, crs=zones.crs)
        target_geometries = gpd.GeoDataFrame({"_target_position": np.arange(len(targets))}, geometry=targets.geometry, crs=targets.crs)
        joined = gpd.sjoin(target_geometries, zone_geometries, how="inner", predicate=args.predicate)
        pairs = sorted((int(row["_target_position"]), int(row["_zone_position"])) for _, row in joined.iterrows())
    except (ValueError, TypeError, OverflowError) as exc:
        sys.exit(f"Error: {exc}")

    by_zone: dict[int, list[int]] = {i: [] for i in range(len(zones))}
    for target_position, zone_position in pairs:
        by_zone[zone_position].append(target_position)
    rows = []
    for position, zone_id_value in enumerate(zones_source[zone_id].tolist()):
        matching_targets = by_zone[position]
        row = {zone_id: zone_id_value, "count": len(matching_targets)}
        if args.sum_field:
            values = targets_source.iloc[matching_targets][args.sum_field]
            total = values.sum(min_count=1) if not values.empty else np.nan
            row["sum_" + args.sum_field] = total if pd.notna(total) else None
        rows.append(row)
    matched_targets = {target_position for target_position, _ in pairs}
    report = {
        "zones": len(zones_source),
        "target_features": len(targets_source),
        "matched_target_features": len(matched_targets),
        "unmatched_target_features": len(targets_source) - len(matched_targets),
        "match_pairs": len(pairs),
        "predicate": args.predicate,
        "analysis_crs": str(zones.crs),
        "analysis_method": analysis_method(zones_source.crs, args.analysis_crs, used=zones.crs),
        "meters_per_unit": factor,
        "boundary_policy": "within excludes boundary-only features" if args.predicate == "within" else "intersects includes boundary contacts and may match multiple zones",
    }
    print(tabulate(rows, headers="keys", tablefmt="simple", showindex=False))
    print("Cross match report: " + json.dumps(report, ensure_ascii=False, sort_keys=True))


def cmd_chart(args):
    _setup_chinese_font()
    gdf = _read(args.input, args.layer)
    field = args.field
    if field not in gdf.columns:
        sys.exit(f"field not found: {field}")

    fig, ax = plt.subplots(figsize=(8, 5))

    kind = args.type
    if kind == "bar":
        counts = gdf[field].value_counts().head(args.top_n)
        counts.plot.bar(ax=ax)
        ax.set_ylabel("count")
    elif kind == "pie":
        counts = gdf[field].value_counts().head(args.top_n)
        counts.plot.pie(ax=ax, autopct="%.1f%%")
        ax.set_ylabel("")
    elif kind == "hist":
        gdf[field].dropna().plot.hist(ax=ax, bins=args.bins, edgecolor="white")
        ax.set_xlabel(field)
        ax.set_ylabel("count")

    ax.set_title(args.title or field)
    _apply_theme(fig, ax)
    _save_or_show(fig, args.output)


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stats", description="GIS vector data statistics")
    sub = p.add_subparsers(dest="command", required=True)

    # describe
    s = sub.add_parser("describe", help="descriptive statistics for numeric fields")
    s.add_argument("input", help="input vector file")
    s.add_argument("--layer", default=None)

    s.add_argument("--json", action="store_true", help="Pure JSON on stdout; missing statistics are null")

    # frequency
    s = sub.add_parser("frequency", help="frequency table for a categorical field")
    s.add_argument("input", help="input vector file")
    s.add_argument("--field", required=True)
    s.add_argument("--layer", default=None)

    # spatial
    s = sub.add_parser("spatial", help="spatial statistics")
    s.add_argument("input", help="input vector file")
    s.add_argument("--layer", default=None)
    s.add_argument("--analysis-crs", help="Explicit projected CRS; required for broad geographic layers")
    s.add_argument("--study-area", help="Study area vector file used as the density/NNI denominator")
    s.add_argument("--study-area-layer")
    s.add_argument("--bbox-fallback", action="store_true", help="Acknowledge the default input-bounds surrogate when --study-area is omitted")

    # cross
    s = sub.add_parser("cross", help="cross analysis (point-in-polygon / overlay)")
    s.add_argument("--input", required=True, help="zones layer")
    s.add_argument("--target", required=True, help="features to count/sum")
    s.add_argument("--zone-field", required=True, help="field identifying zones")
    s.add_argument("--sum-field", default=None, help="numeric field to sum per zone")
    s.add_argument("--predicate", choices=["within", "intersects"], default="within", help="within excludes boundary-only matches; intersects includes boundaries and multi-zone matches")
    s.add_argument("--analysis-crs", help="Projected CRS for spatial relation; broad geographic extents require it")
    s.add_argument("--layer", default=None, help="layer name for zones")
    s.add_argument("--target-layer", default=None, help="layer name for targets")

    # chart
    s = sub.add_parser("chart", help="generate a chart for a field")
    s.add_argument("input", help="input vector file")
    s.add_argument("--field", required=True)
    s.add_argument("--type", choices=["bar", "pie", "hist"], default="bar")
    s.add_argument("--output", "-o", default=None, help="output file (PNG/PDF)")
    s.add_argument("--title", default=None)
    s.add_argument("--bins", type=int, default=30)
    s.add_argument("--top-n", type=int, default=20)
    s.add_argument("--layer", default=None)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "describe": cmd_describe,
        "frequency": cmd_frequency,
        "spatial": cmd_spatial,
        "cross": cmd_cross,
        "chart": cmd_chart,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
