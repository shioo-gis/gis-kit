# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from _core import reproject
from _safe_io import AtomicVectorOutput


def read_info(path: str, layer: str | None) -> dict:
    return pyogrio.read_info(path, layer=layer)


def resolve_dtype(dtypes: list[str]) -> str:
    kinds = set(dtypes)
    if all(dtype.startswith(("int", "uint")) for dtype in kinds):
        return "Int64"
    if all(dtype.startswith(("int", "uint", "float")) for dtype in kinds):
        return "Float64"
    if all(dtype.startswith("datetime64") for dtype in kinds):
        return "datetime64[ms]"
    return "object"


def collect_fields(infos: list[dict], mode: str) -> tuple[list[str], dict[str, str]]:
    field_sets = [list(info["fields"]) for info in infos]
    if mode == "intersection":
        allowed = set(field_sets[0]).intersection(*field_sets[1:])
        fields = [field for field in field_sets[0] if field in allowed]
    else:
        fields = []
        for source_fields in field_sets:
            fields.extend(field for field in source_fields if field not in fields)

    dtypes: dict[str, list[str]] = {field: [] for field in fields}
    for info in infos:
        dtypes.update({field: dtypes[field] + [str(dtype)] for field, dtype in zip(info["fields"], info["dtypes"]) if field in dtypes})
    return fields, {field: resolve_dtype(types) for field, types in dtypes.items()}


def align_chunk(gdf: gpd.GeoDataFrame, fields: list[str], dtypes: dict[str, str]) -> gpd.GeoDataFrame:
    for field in fields:
        if field not in gdf:
            gdf[field] = pd.Series(pd.NA, index=gdf.index, dtype=dtypes[field])
        elif dtypes[field] != "object":
            gdf[field] = gdf[field].astype(dtypes[field])
    return gdf[fields + [gdf.geometry.name]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge multiple data sources into one.")
    parser.add_argument("--inputs", nargs="+", required=True, help="Input files")
    parser.add_argument("--output", required=True, help="Output file")
    parser.add_argument("--layers", help="Comma-separated layer names, one per input")
    parser.add_argument("--target-crs", help="Target CRS (default: first input's CRS)")
    parser.add_argument("--chunk-size", type=int, default=10_000, help="Features read and written at a time")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing single-layer output file")
    parser.add_argument(
        "--align-fields",
        choices=["union", "intersection"],
        default="union",
        help="How to handle field mismatch",
    )
    parser.add_argument("--add-source", action="store_true", help="Add source_file field")
    args = parser.parse_args()

    layers = args.layers.split(",") if args.layers else [None] * len(args.inputs)
    if len(layers) != len(args.inputs):
        sys.exit("Number of layers must match number of inputs")
    if args.chunk_size <= 0:
        sys.exit("--chunk-size must be positive")

    layers = [layer.strip() if layer else None for layer in layers]
    try:
        infos = [read_info(path, layer) for path, layer in zip(args.inputs, layers)]
    except Exception as exc:
        sys.exit(f"Unable to inspect input: {exc}")
    target_crs = args.target_crs or infos[0]["crs"]
    crs_presence = [info.get("crs") is not None for info in infos]
    if any(crs_presence) and not all(crs_presence):
        sys.exit("Inputs mix defined and missing CRS; assign a correct CRS before merging")
    if target_crs is not None and not all(crs_presence):
        sys.exit("Cannot reproject an input without a CRS; assign a correct CRS before merging")
    geom_types = {info["geometry_type"] for info in infos}
    if len(geom_types) > 1:
        warnings.warn(f"Mixed geometry types detected: {geom_types}", stacklevel=2)

    fields, dtypes = collect_fields(infos, args.align_fields)
    if args.add_source:
        if "source_file" in fields:
            sys.exit("--add-source conflicts with an input field named 'source_file'")
        fields.append("source_file")
        dtypes["source_file"] = "object"

    output = Path(args.output)
    total = 0
    written = False
    try:
        with AtomicVectorOutput(output, overwrite=args.overwrite, protected_paths=args.inputs) as staged:
            for path, layer, info in zip(args.inputs, layers, infos):
                count = info["features"]
                if count < 0:
                    raise ValueError(f"Input driver does not report a feature count: {path}")
                for start in range(0, count, args.chunk_size):
                    gdf = pyogrio.read_dataframe(
                        path,
                        layer=layer,
                        skip_features=start,
                        max_features=args.chunk_size,
                    )
                    if gdf.crs and target_crs:
                        gdf = reproject(gdf, target_crs, purpose='publish')
                    if args.add_source:
                        gdf["source_file"] = Path(path).name
                    gdf = align_chunk(gdf, fields, dtypes)
                    staged.write(gdf, append=written)
                    written = True
                    total += len(gdf)
            if not written:
                empty = gpd.GeoDataFrame(
                    {field: pd.Series(dtype=dtypes[field]) for field in fields},
                    geometry=gpd.GeoSeries([], crs=target_crs),
                )
                staged.write(empty)
            staged.commit(
                expected_features=total,
                required_fields=fields,
                expected_crs=target_crs,
            )
    except Exception as exc:
        sys.exit(f"Merge failed; no incomplete output was published: {exc}")

    print(f"Merged {len(args.inputs)} inputs ({total} features) to {output}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
