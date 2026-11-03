# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pyogrio
from pyproj import CRS

from _budget import check_vector
from _safe_io import AtomicVectorOutput, stream_vector

# Inputs picked up by directory batch mode; single-file mode reads anything GDAL can open.
# MapInfo .tab/.mif sidecars (.dat/.map/.id/.mid) and the GML .xsd are not listed on purpose.
SUPPORTED_EXTENSIONS = {
    ".gpkg", ".shp", ".geojson", ".json", ".geojsonl", ".fgb", ".gdb",
    ".kml", ".kmz", ".gpx", ".gml", ".tab", ".mif", ".dxf",
}


def require_layer(path: Path, layer: str | None) -> None:
    """Refuse to pick the first layer of a multi-layer source (GPX always has five) without --layer."""
    if layer:
        return
    names = [name for name, _ in pyogrio.list_layers(path)]
    if len(names) > 1:
        raise ValueError(f"{path} has {len(names)} layers ({', '.join(names)}); pass --layer to choose one")


def check_fields(path: Path, layer: str | None, fields: list[str] | None) -> None:
    """Refuse --fields names the source does not have, before anything is written."""
    if not fields:
        return
    available = [str(name) for name in pyogrio.read_info(path, layer=layer)["fields"]]
    missing = [name for name in fields if name not in available]
    if missing:
        raise ValueError(f"{path} has no field {', '.join(missing)}; fields: {', '.join(available)}")


def read_file(path: Path, layer: str | None, encoding: str | None, fields: list[str] | None = None) -> gpd.GeoDataFrame:
    kwargs: dict = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    if fields:
        kwargs["columns"] = fields
    if encoding:
        kwargs["encoding"] = encoding
    if path.suffix.lower() == ".gdb":
        kwargs["engine"] = "pyogrio"
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def write_file(gdf, path, layer, *, overwrite=False, protected_paths=()):
    with AtomicVectorOutput(path, overwrite=overwrite, protected_paths=protected_paths) as out:
        if layer and out.driver == "GPKG":
            out.layer = layer
        out.write(gdf)
        out.commit(expected_features=len(gdf), expected_crs=gdf.crs,
                   required_fields=[c for c in gdf.columns if c != gdf.geometry.name])


def stream_convert(input_path, output_path, layer, out_layer, crs, encoding, overwrite, fields=None) -> bool:
    """Chunked read/reproject/append for layers larger than one chunk; False means use one full read."""
    with AtomicVectorOutput(output_path, overwrite=overwrite, protected_paths=[input_path]) as out:
        if out_layer and out.driver == "GPKG":
            out.layer = out_layer
        meta = {}

        def write(frame, first, **options):
            if first:
                meta.update(crs=frame.crs, fields=[c for c in frame.columns if c != frame.geometry.name])
            out.write(frame, append=not first, **options)

        count = stream_vector(input_path, write, driver=out.driver, layer=layer,
                              transform=(lambda frame: frame.to_crs(crs)) if crs else None,
                              **({"encoding": encoding} if encoding else {}), **({"columns": fields} if fields else {}))
        if count is None:
            return False
        out.commit(expected_features=count, expected_crs=meta["crs"], required_fields=meta["fields"])
    print(f"Streamed {count} features from {input_path}" + (f" (layer: {layer})" if layer else "")
          + (f"; reprojected to {CRS.from_user_input(crs).to_string()}" if crs else ""))
    print(f"Written to {output_path}" + (f" (layer: {out_layer})" if out_layer else ""))
    return True


def convert_single(input_path: Path, output_path: Path, layer: str | None, output_layer: str | None, crs: str | None, encoding: str | None, overwrite=False, fields: list[str] | None = None) -> None:
    require_layer(input_path, layer)
    check_fields(input_path, layer, fields)
    if stream_convert(input_path, output_path, layer, output_layer or layer, crs, encoding, overwrite, fields):
        return
    gdf = read_file(input_path, layer, encoding, fields)
    print(f"Read {len(gdf)} features from {input_path}" + (f" (layer: {layer})" if layer else ""))

    if crs:
        src_crs = gdf.crs
        gdf = gdf.to_crs(crs)
        print(f"Reprojected: {src_crs} -> {gdf.crs}")

    out_layer = output_layer or layer
    write_file(gdf, output_path, out_layer, overwrite=overwrite, protected_paths=[input_path])
    print(f"Written to {output_path}" + (f" (layer: {out_layer})" if out_layer else ""))


def build_output_path(input_path: Path, output_ext: str) -> Path:
    return input_path.with_suffix(output_ext)


def main() -> None:
    parser = argparse.ArgumentParser(description="GIS format conversion and CRS reprojection")
    parser.add_argument("input", help="Input file or directory path")
    parser.add_argument("--output", help="Output file path (format inferred from extension)")
    parser.add_argument("--layer", help="Source layer name")
    parser.add_argument("--output-layer", help="Output layer name (defaults to source layer)")
    parser.add_argument("--crs", help="Target CRS (e.g., EPSG:4326)")
    parser.add_argument("--encoding", help="Source file encoding (e.g., gbk, gb2312)")
    parser.add_argument("--fields", help="Comma-separated fields to keep (geometry is always kept); default all")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    fields = [name.strip() for name in args.fields.split(",") if name.strip()] if args.fields else None

    input_path = Path(args.input)

    if input_path.is_dir() and input_path.suffix.lower() != ".gdb":
        if not args.output:
            print("ERROR: --output (with target extension) is required for batch directory conversion", file=sys.stderr)
            sys.exit(1)
        out_ext = Path(args.output).suffix
        if not out_ext:
            print("ERROR: --output must have a file extension to infer target format", file=sys.stderr)
            sys.exit(1)
        output_dir = Path(args.output).parent if Path(args.output).parent != Path(".") else input_path
        output_dir.mkdir(parents=True, exist_ok=True)

        files = [f for f in input_path.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file()]
        if not files:
            files = [f for f in input_path.iterdir() if f.is_dir() and f.suffix.lower() == ".gdb"]
        print(f"Batch mode: {len(files)} files found in {input_path}")
        # roads.tab and roads.mif would both become roads<ext>; refuse before writing anything.
        targets: dict[str, list[str]] = {}
        for f in files:
            targets.setdefault(f"{f.stem}{out_ext}".lower(), []).append(f.name)
        clashes = [names for names in targets.values() if len(names) > 1]
        if clashes:
            print("ERROR: inputs would write the same output: "
                  + "; ".join(", ".join(sorted(names)) for names in clashes), file=sys.stderr)
            sys.exit(1)
        failures = 0
        for f in sorted(files):
            out = output_dir / f"{f.stem}{out_ext}"
            try:
                convert_single(f, out, args.layer, args.output_layer, args.crs, args.encoding, args.overwrite, fields)
            except Exception as e:
                failures += 1
                print(f"ERROR processing {f}: {e}", file=sys.stderr)
        if failures:
            sys.exit(1)
    else:
        if not args.output:
            print("ERROR: --output is required", file=sys.stderr)
            sys.exit(1)
        try:
            convert_single(input_path, Path(args.output), args.layer, args.output_layer, args.crs, args.encoding, args.overwrite, fields)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
