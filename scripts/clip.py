# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "shapely>=2.0",
# ]
# ///
import argparse
import math
import sys

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import box

from _safe_io import write_vector_atomic
from _budget import check_vector


def read_gdf(path, layer=None, bbox=None, *, columns=None, where=None):
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    if bbox:
        kwargs["bbox"] = bbox
    if columns is not None:
        kwargs["columns"] = columns
    if where:
        kwargs["where"] = where
    if not bbox and not where:  # filtered reads load only part of the layer
        check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def parse_bbox(value):
    coords = [float(x) for x in value.split(",")]
    if len(coords) != 4 or not all(math.isfinite(coord) for coord in coords) or coords[0] > coords[2] or coords[1] > coords[3]:
        raise ValueError("--bbox requires minx,miny,maxx,maxy")
    return tuple(coords)


def overlap_bbox(first, second):
    """Return shared bounds, keeping the first safe superset when disjoint."""
    left = max(first[0], second[0])
    bottom = max(first[1], second[1])
    right = min(first[2], second[2])
    top = min(first[3], second[3])
    return (left, bottom, right, top) if left <= right and bottom <= top else first


def finite_bounds(frame):
    if frame is None or frame.empty:
        return None
    bounds = tuple(frame.total_bounds)
    return bounds if all(math.isfinite(value) for value in bounds) else None


def main():
    parser = argparse.ArgumentParser(description="Clip and spatial/attribute filter")
    parser.add_argument("input", help="Input vector file")
    parser.add_argument("--output", "-o", required=True, help="Output file")
    parser.add_argument("--layer", help="Input layer name")
    parser.add_argument("--clip-layer", help="Path to clip mask vector file")
    parser.add_argument("--clip-layer-name", help="Layer name within clip mask file")
    parser.add_argument("--bbox", help="Bounding box: minx,miny,maxx,maxy")
    parser.add_argument("--where", help="Attribute filter (pandas query syntax)")
    parser.add_argument("--ogr-where", help="OGR SQL attribute filter pushed down to the input driver")
    parser.add_argument("--columns", help="Comma-separated output fields to read and keep")
    parser.add_argument("--spatial-filter", choices=["intersects", "within", "contains"],
                        help="Spatial predicate for --filter-layer")
    parser.add_argument("--filter-layer", help="Vector file for spatial predicate filter")
    parser.add_argument("--filter-layer-name", help="Layer name within the spatial filter file")
    parser.add_argument("--invert", action="store_true", help="Invert selection")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing single-layer output file")
    args = parser.parse_args()

    try:
        bbox = parse_bbox(args.bbox) if args.bbox else None
    except ValueError as exc:
        sys.exit(f"Error: {exc}")

    if args.spatial_filter and not args.filter_layer:
        sys.exit("Error: --spatial-filter requires --filter-layer")
    if args.ogr_where and args.invert:
        sys.exit("Error: --ogr-where cannot be combined with --invert; inversion must see every source feature")

    try:
        source_info = pyogrio.read_info(args.input, layer=args.layer)
        source_crs = source_info.get("crs")
        clip_gdf = read_gdf(args.clip_layer, args.clip_layer_name) if args.clip_layer else None
        filter_gdf = read_gdf(args.filter_layer, args.filter_layer_name) if args.spatial_filter else None

        def align_mask_crs(mask, label):
            if mask is None:
                return None
            if (mask.crs is None) != (source_crs is None):
                raise ValueError(f"{label} and input must both define a CRS, or both be unreferenced")
            if source_crs is not None and mask.crs != source_crs:
                return mask.to_crs(source_crs)
            return mask

        clip_gdf = align_mask_crs(clip_gdf, "Clip mask")
        filter_gdf = align_mask_crs(filter_gdf, "Spatial filter")
        requested_columns = None
        if args.columns:
            requested_columns = [field.strip() for field in args.columns.split(",") if field.strip()]
            if not requested_columns or len(requested_columns) != len(set(requested_columns)):
                raise ValueError("--columns requires a non-empty, duplicate-free comma-separated field list")
            missing = set(requested_columns) - set(source_info.get("fields", ()))
            if missing:
                raise ValueError(f"Unknown --columns field(s): {', '.join(sorted(missing))}")
        # Pandas eval can reference arbitrary valid field expressions; when it is
        # present, read the full schema to preserve its historical behavior.
        # We only push a projection when an OGR predicate is also pushed down;
        # field-only reads were not a stable win on the available 3k/12k cases.
        read_columns = requested_columns if requested_columns and args.ogr_where and not args.where else None
    except Exception as exc:
        sys.exit(f"Error: {exc}")

    # Do not push a bbox for inverted selection: rows outside that bbox are results.
    read_bbox = None if args.invert else bbox
    if not args.invert and clip_gdf is not None:
        clip_bbox = finite_bounds(clip_gdf)
        if clip_bbox is not None:
            read_bbox = overlap_bbox(read_bbox, clip_bbox) if read_bbox else clip_bbox
    if not args.invert and filter_gdf is not None:
        filter_bbox = finite_bounds(filter_gdf)
        if filter_bbox is not None:
            read_bbox = overlap_bbox(read_bbox, filter_bbox) if read_bbox else filter_bbox

    try:
        gdf = read_gdf(
            args.input,
            args.layer,
            read_bbox,
            columns=read_columns,
            where=args.ogr_where,
        )
    except Exception as exc:
        sys.exit(f"Error reading input: {exc}")
    candidates = len(gdf)
    mask = pd.Series(True, index=gdf.index)

    if bbox:
        bbox_geom = box(*bbox)
        spatial_mask = gdf.intersects(bbox_geom)
        mask = mask & spatial_mask.fillna(False)

    if clip_gdf is not None:
        clip_union = clip_gdf.union_all()
        spatial_mask = gdf.intersects(clip_union)
        mask = mask & spatial_mask.fillna(False)
        gdf = gdf.copy()
        gdf.loc[mask, "geometry"] = gdf.loc[mask, "geometry"].intersection(clip_union)

    if args.spatial_filter:
        filter_union = filter_gdf.union_all()
        predicate_fn = getattr(gdf, args.spatial_filter)
        spatial_mask = predicate_fn(filter_union)
        mask = mask & spatial_mask.fillna(False)

    if args.where:
        attr_mask = gdf.eval(args.where)
        if not isinstance(attr_mask, pd.Series):
            attr_mask = pd.Series(bool(attr_mask), index=gdf.index)
        mask = mask & attr_mask.fillna(False).astype(bool)

    if args.invert:
        mask = ~mask

    result = gdf[mask].reset_index(drop=True)
    print(f"{len(result)}/{candidates} candidate features selected.")

    if args.clip_layer and not args.invert:
        result = result[~result.is_empty]
        print(f"{len(result)} features after removing empty geometries.")

    if requested_columns is not None:
        result = result[requested_columns + [result.geometry.name]]

    try:
        write_vector_atomic(
            result,
            args.output,
            overwrite=args.overwrite,
            protected_paths=[args.input, args.clip_layer, args.filter_layer],
        )
    except Exception as exc:
        sys.exit(f"Error writing output: {exc}")
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
