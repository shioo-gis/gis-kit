"""Runnable smoke test for chunked GIS paths: python3 tests/test_large_data_paths.py."""

from pathlib import Path
import subprocess
import sys
import tempfile

import geopandas as gpd
from shapely.geometry import box


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True, capture_output=True, encoding='utf-8', errors='replace')


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        source = work / "source.gpkg"
        gpd.GeoDataFrame(
            {"zone": ["A", "B", "A"], "value": [1, 2, 3]},
            geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1)],
            crs="EPSG:4326",
        ).to_file(source, layer="items", engine="pyogrio")
        mask = work / "mask.gpkg"
        gpd.GeoDataFrame(geometry=[box(-0.5, -0.5, 1.5, 1.5)], crs="EPSG:4326").to_file(mask, engine="pyogrio")
        second = work / "second.gpkg"
        gpd.GeoDataFrame({"other": ["x"]}, geometry=[box(6, 0, 7, 1)], crs="EPSG:4326").to_file(second, engine="pyogrio")

        clipped = work / "clip.gpkg"
        run("scripts/clip.py", str(source), "--layer", "items", "--clip-layer", str(mask), "--bbox=-1,-1,1.5,2", "--output", str(clipped))
        assert len(gpd.read_file(clipped)) == 1

        filtered = work / "filter.gpkg"
        run("scripts/clip.py", str(source), "--layer", "items", "--spatial-filter", "intersects", "--filter-layer", str(mask), "--output", str(filtered))
        assert len(gpd.read_file(filtered)) == 1

        inverted = work / "invert.gpkg"
        run("scripts/clip.py", str(source), "--layer", "items", "--bbox=-1,-1,1.5,2", "--invert", "--output", str(inverted))
        assert len(gpd.read_file(inverted)) == 2

        blocks = work / "blocks.gpkg"
        run("scripts/dissolve.py", str(source), "--layer", "items", "--by", "zone", "--no-global-merge", "--chunk-size", "2", "--output", str(blocks))
        block_result = gpd.read_file(blocks)
        assert len(block_result) == 3
        assert block_result["zone"].value_counts().to_dict() == {"A": 2, "B": 1}

        coverage = work / "coverage.gpkg"
        run("scripts/dissolve.py", str(source), "--layer", "items", "--by", "zone", "--method", "coverage", "--output", str(coverage))
        coverage_result = gpd.read_file(coverage)
        assert coverage_result["zone"].value_counts().to_dict() == {"A": 1, "B": 1}

        merged = work / "merged.gpkg"
        run("scripts/merge.py", "--inputs", str(source), str(second), "--output", str(merged), "--layers", "items,", "--chunk-size", "1", "--add-source")
        result = gpd.read_file(merged)
        assert len(result) == 4
        assert {"zone", "value", "other", "source_file"} <= set(result.columns)


if __name__ == "__main__":
    main()
