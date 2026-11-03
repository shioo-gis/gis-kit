# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "openpyxl>=3.1",
# ]
# ///

from __future__ import annotations

import argparse
import os
import tempfile
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from _metric import analysis_frame
from _budget import check_vector


def read_layer(path: Path, layer: str | None) -> gpd.GeoDataFrame:
    kwargs: dict = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def add_coords(gdf: gpd.GeoDataFrame, analysis_crs=None) -> pd.DataFrame:
    df = pd.DataFrame(gdf)
    work, _ = analysis_frame(gdf, analysis_crs)
    centroids = work.geometry.centroid.to_crs(gdf.crs)
    df.insert(0, "Y", centroids.y)
    df.insert(0, "X", centroids.x)
    return df


def auto_fit_columns(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    for i, col in enumerate(df.columns):
        max_len = max(
            df[col].astype(str).map(len).max(),
            len(str(col)),
        )
        worksheet.column_dimensions[chr(65 + i) if i < 26 else f"{chr(64 + i // 26)}{chr(65 + i % 26)}"].width = min(max_len + 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export attribute table to CSV or Excel")
    parser.add_argument("input", help="Input geospatial file")
    parser.add_argument("--output", required=True, help="Output file (.csv or .xlsx)")
    parser.add_argument("--layer", help="Layer name")
    parser.add_argument("--fields", help="Comma-separated list of fields to export")
    parser.add_argument("--where", help="Filter condition (pandas query syntax)")
    parser.add_argument("--include-coords", action="store_true", help="Add X/Y coordinate columns")
    parser.add_argument("--encoding", default="utf-8-sig", help="Output CSV encoding (default: utf-8-sig)")
    parser.add_argument("--no-index", action="store_true", help="Exclude row index")
    parser.add_argument("--analysis-crs", help="Projected centroid analysis CRS; X/Y use source CRS")
    parser.add_argument("--overwrite", action="store_true", help="Replace an independent existing table")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    ext = output_path.suffix.lower()

    if ext not in (".csv", ".xlsx"):
        print(f"ERROR: Unsupported output format '{ext}'. Use .csv or .xlsx", file=sys.stderr)
        sys.exit(1)

    gdf = read_layer(input_path, args.layer)

    if args.include_coords:
        df = add_coords(gdf, args.analysis_crs)
    else:
        df = pd.DataFrame(gdf)

    if gdf.geometry.name in df.columns:
        df = df.drop(columns=[gdf.geometry.name])

    if args.fields:
        requested = [f.strip() for f in args.fields.split(",")]
        coord_cols = [c for c in ("X", "Y") if c in df.columns and args.include_coords]
        keep = coord_cols + [f for f in requested if f in df.columns]
        missing = [f for f in requested if f not in df.columns]
        if missing:
            print(f"WARNING: Fields not found: {missing}", file=sys.stderr)
        df = df[keep]

    if args.where:
        before = len(df)
        df = df.query(args.where)
        print(f"Filter applied: {before} -> {len(df)} rows")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    index = not args.no_index

    if output_path.resolve()==input_path.resolve() or (output_path.exists() and os.path.samefile(output_path,input_path)):
        raise ValueError("Output must not replace input")
    if output_path.is_symlink() or (output_path.exists() and not args.overwrite):
        raise ValueError("Output exists or is a symlink; choose a new path or --overwrite")
    fd, name = tempfile.mkstemp(prefix='.'+output_path.stem+'-',suffix=ext,dir=output_path.parent)
    os.close(fd)
    staged=Path(name)
    try:
        if ext == ".csv":
            df.to_csv(staged,index=index,encoding=args.encoding)
            rows=sum(len(chunk) for chunk in pd.read_csv(staged,encoding=args.encoding,chunksize=100_000))
        else:
            with pd.ExcelWriter(staged,engine="openpyxl") as writer:
                df.to_excel(writer,index=index,sheet_name="data")
                auto_fit_columns(writer,"data",df)
            rows=len(pd.read_excel(staged))
        if rows!=len(df): raise ValueError("Table row count differs on readback")
        if args.overwrite:
            if output_path.is_symlink(): raise ValueError("Output became a symlink")
            os.replace(staged,output_path)
        else:
            os.link(staged,output_path)
    finally:
        staged.unlink(missing_ok=True)

    print(f"{len(df)} rows exported to {output_path}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
