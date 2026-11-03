"""CLI regression tests for vector filtering, batching, and transactional output."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import box


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path


def command(*args: str, expected: int = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == expected, f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}"
    return result


def write_frame(path: Path, frame: gpd.GeoDataFrame, *, layer: str | None = None, append: bool = False) -> None:
    kwargs = {"driver": "GPKG", "append": append}
    if layer:
        kwargs["layer"] = layer
    pyogrio.write_dataframe(frame, path, **kwargs)


def test_clip_crs_fields_and_filters(work: Path) -> None:
    source = work / "source.gpkg"
    gpd.GeoDataFrame(
        {"id": [1, 2], "zone": ["west", "east"], "value": [10, 30]},
        geometry=[box(-74.0, 40.0, -73.99, 40.01), box(-70.0, 43.0, -69.99, 43.01)],
        crs="EPSG:4326",
    ).to_file(source, engine="pyogrio")
    mask = work / "projected-mask.gpkg"
    gpd.GeoDataFrame(
        geometry=[box(-74.1, 39.9, -73.8, 40.2)], crs="EPSG:4326"
    ).to_crs("EPSG:3857").to_file(mask, engine="pyogrio")

    projected = work / "projected-clip.gpkg"
    command(
        "scripts/clip.py", str(source), "--clip-layer", str(mask), "--output", str(projected)
    )
    projected_result = gpd.read_file(projected)
    assert projected_result["id"].tolist() == [1]
    assert projected_result.crs.to_epsg() == 4326

    pushed = work / "ogr-filter.gpkg"
    command(
        "scripts/clip.py", str(source), "--ogr-where", "value >= 20",
        "--columns", "id,zone", "--output", str(pushed),
    )
    pushed_result = gpd.read_file(pushed)
    assert pushed_result["id"].tolist() == [2]
    assert set(pushed_result.columns) == {"id", "zone", "geometry"}

    geojson = work / "filtered.geojson"
    command(
        "scripts/clip.py", str(source), "--ogr-where", '"id" = 1',
        "--output", str(geojson),
    )
    assert gpd.read_file(geojson)["id"].tolist() == [1]

    empty = work / "empty.gpkg"
    command("scripts/clip.py", str(source), "--ogr-where", "id < 0", "--output", str(empty))
    assert len(gpd.read_file(empty)) == 0

    pandas_filtered = work / "pandas-filter.gpkg"
    command("scripts/clip.py", str(source), "--where", "value >= 20", "--output", str(pandas_filtered))
    assert gpd.read_file(pandas_filtered)["id"].tolist() == [2]

    inverted = work / "inverted.gpkg"
    command(
        "scripts/clip.py", str(source), "--clip-layer", str(mask), "--invert", "--output", str(inverted)
    )
    assert gpd.read_file(inverted)["id"].tolist() == [2]

    invalid = command(
        "scripts/clip.py", str(source), "--ogr-where", "id > 0", "--invert",
        "--output", str(work / "invalid-invert.gpkg"), expected=1,
    )
    assert "cannot be combined" in invalid.stderr


def test_dissolve_groups_and_limits(work: Path) -> None:
    source = work / "groups.gpkg"
    gpd.GeoDataFrame(
        {"zone": ["A", "B", "A", None], "value": [None, 4, None, 7], "label": [None, "b", "third", "null-row"]},
        geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1), box(6, 0, 7, 1)],
        crs="EPSG:4326",
    ).to_file(source, engine="pyogrio")

    global_output = work / "global-no-multi.gpkg"
    command(
        "scripts/dissolve.py", str(source), "--by", "zone", "--agg", "value:sum,label:first",
        "--no-multi", "--output", str(global_output),
    )
    globally_dissolved = gpd.read_file(global_output)
    assert len(globally_dissolved) == 4
    assert "zone" in globally_dissolved.columns
    group_a = globally_dissolved[globally_dissolved["zone"] == "A"]
    assert len(group_a) == 2
    assert group_a["value"].isna().all()
    assert group_a["label"].isna().all()
    assert globally_dissolved.loc[globally_dissolved["zone"] == "B", "value"].tolist() == [4]
    assert globally_dissolved.loc[globally_dissolved["zone"].isna(), "value"].tolist() == [7]

    defaults = work / "default-first.gpkg"
    command("scripts/dissolve.py", str(source), "--by", "zone", "--output", str(defaults))
    default_result = gpd.read_file(defaults)
    assert default_result.loc[default_result["zone"] == "A", "label"].isna().all()

    blocks = work / "block-results.gpkg"
    command(
        "scripts/dissolve.py", str(source), "--by", "zone", "--agg", "value:sum",
        "--no-global-merge", "--chunk-size", "1", "--output", str(blocks),
    )
    block_result = gpd.read_file(blocks)
    assert len(block_result) == 4
    assert (block_result["zone"] == "A").sum() == 2
    assert (block_result["zone"] == "B").sum() == 1
    assert block_result["zone"].isna().sum() == 1

    coverage = work / "coverage.gpkg"
    command("scripts/dissolve.py", str(source), "--by", "zone", "--method", "coverage", "--output", str(coverage))
    assert len(gpd.read_file(coverage)) == 3


def test_merge_atomic_failure_and_output_protection(work: Path) -> None:
    source = work / "merge-source.gpkg"
    gpd.GeoDataFrame(
        {"id": [1, 2]}, geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1)], crs="EPSG:4326"
    ).to_file(source, engine="pyogrio")
    second = work / "merge-second.gpkg"
    gpd.GeoDataFrame({"extra": ["x"]}, geometry=[box(4, 0, 5, 1)], crs="EPSG:4326").to_file(second, engine="pyogrio")

    merged = work / "merged.gpkg"
    command(
        "scripts/merge.py", "--inputs", str(source), str(second), "--output", str(merged),
        "--chunk-size", "1", "--add-source",
    )
    result = gpd.read_file(merged)
    assert len(result) == 3
    assert {"id", "extra", "source_file"} <= set(result.columns)

    # Inject a true second-chunk read failure after the first chunk has been written
    # to the staging file. The existing destination must remain byte-for-byte readable.
    old_output = work / "old.gpkg"
    write_frame(old_output, gpd.GeoDataFrame({"marker": ["old"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"), layer="old")
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    original_argv = sys.argv[:]
    merge_module = None
    real_read = None
    try:
        import merge as merge_module

        real_read = merge_module.pyogrio.read_dataframe
        calls = []

        def fail_on_second_chunk(path, *args, **kwargs):
            calls.append((path, kwargs.get("skip_features", 0)))
            if Path(path) == source and kwargs.get("skip_features", 0) == 1:
                raise RuntimeError("injected read failure")
            return real_read(path, *args, **kwargs)

        merge_module.pyogrio.read_dataframe = fail_on_second_chunk
        sys.argv = [
            "merge.py", "--inputs", str(source), "--output", str(old_output),
            "--chunk-size", "1", "--overwrite",
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                merge_module.main()
            except SystemExit as error:
                assert "no incomplete output was published" in str(error.code)
            else:
                raise AssertionError("injected merge failure should be reported")
        assert len(calls) >= 2
        assert gpd.read_file(old_output)["marker"].tolist() == ["old"]
        assert not list(work.glob(".old.*.tmp.gpkg"))
    finally:
        if merge_module is not None and real_read is not None:
            merge_module.pyogrio.read_dataframe = real_read
        sys.argv = original_argv
        sys.path.remove(scripts)
        sys.modules.pop("merge", None)

    overwrite_error = command("scripts/clip.py", str(source), "--output", str(merged), expected=1)
    assert "--overwrite" in overwrite_error.stderr
    assert len(gpd.read_file(merged)) == 3

    replace_target = work / "replace-target.gpkg"
    write_frame(
        replace_target,
        gpd.GeoDataFrame({"marker": ["old"]}, geometry=[box(20, 20, 21, 21)], crs="EPSG:4326"),
    )
    command(
        "scripts/clip.py", str(source), "--ogr-where", "id = 1",
        "--output", str(replace_target), "--overwrite",
    )
    replaced = gpd.read_file(replace_target)
    assert replaced["id"].tolist() == [1]
    assert "marker" not in replaced.columns

    multi_layer = work / "multi-layer.gpkg"
    write_frame(multi_layer, gpd.GeoDataFrame({"v": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"), layer="one")
    write_frame(multi_layer, gpd.GeoDataFrame({"v": [2]}, geometry=[box(2, 0, 3, 1)], crs="EPSG:4326"), layer="two", append=True)
    layers_before = pyogrio.list_layers(multi_layer).copy()
    protected = command(
        "scripts/clip.py", str(source), "--output", str(multi_layer), "--overwrite", expected=1
    )
    assert "Refusing to replace a GeoPackage" in protected.stderr
    assert pyogrio.list_layers(multi_layer).tolist() == layers_before.tolist()

    same_input = command(
        "scripts/clip.py", str(source), "--output", str(source), "--overwrite", expected=1
    )
    assert "must not replace an input" in same_input.stderr


def test_batch_structured_artifact_report(work: Path) -> None:
    inputs = work / "inputs"
    outputs = work / "outputs"
    inputs.mkdir()
    source = inputs / "source.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326").to_file(source, engine="pyogrio")

    report = work / "batch-report.json"
    template = f'{sys.executable} -c "import shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2])" {{input}} {{output}}'
    command(
        "scripts/batch.py", "--input-dir", str(inputs), "--output-dir", str(outputs),
        "--command", template, "--workers", "1", "--report", str(report),
    )
    report_data = json.loads(report.read_text(encoding="utf-8"))
    assert report_data["succeeded"] == 1
    item = report_data["results"][0]
    assert item["status"] == "succeeded"
    assert item["artifact_status"] == "verified"
    assert item["artifact_features"] == 1

    stale_report = work / "stale-report.json"
    no_op_template = f'{sys.executable} -c "pass"'
    command(
        "scripts/batch.py", "--input-dir", str(inputs), "--output-dir", str(outputs),
        "--command", no_op_template, "--workers", "1", "--report", str(stale_report), expected=1,
    )
    stale_item = json.loads(stale_report.read_text(encoding="utf-8"))["results"][0]
    assert stale_item["status"] == "artifact_failed"
    assert stale_item["artifact_status"] == "output artifact was not updated"

    missing_output = work / "missing-output"
    missing_report = work / "missing-report.json"
    command(
        "scripts/batch.py", "--input-dir", str(inputs), "--output-dir", str(missing_output),
        "--command", no_op_template, "--workers", "1", "--report", str(missing_report), expected=1,
    )
    missing_item = json.loads(missing_report.read_text(encoding="utf-8"))["results"][0]
    assert missing_item["status"] == "artifact_failed"
    assert missing_item["artifact_status"] == "output artifact is missing"


def test_benchmark_repeats_and_rss_reporting(work: Path) -> None:
    report = work / "benchmark-report.json"
    command(
        "scripts/benchmark.py",
        "--case", f"ok={sys.executable} -c 'pass'",
        "--gate", "ok", "--report", str(report), "--repeat", "3",
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["repeat"] == 3
    case = result["results"][0]
    assert case["status"] == "passed"
    assert len(case["runs"]) == 3
    assert case["median_elapsed_seconds"] == case["elapsed_seconds"]
    assert len(case["range_seconds"]) == 2
    assert "rss_observed" in case and "peak_rss_mb" in case


def test_batch_rejects_unchanged_gdb_directory(work: Path) -> None:
    """A readable pre-existing FileGDB is not evidence that a child produced it."""
    import shutil
    inputs, outputs = work / "inputs", work / "outputs"
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / "data.gdb"
    frame = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 1, 1)], crs=4326)
    pyogrio.write_dataframe(frame, source, driver="OpenFileGDB", layer="data")
    target = outputs / source.name
    shutil.copytree(source, target)
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    report = work / "batch.json"
    command("scripts/batch.py", "--input-dir", str(inputs), "--output-dir", str(outputs),
            "--pattern", "*.gdb", "--command", f'{sys.executable} -c "pass"',
            "--workers", "1", "--report", str(report), expected=1)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["failed"] == 1 and result["succeeded"] == 0
    assert result["results"][0]["artifact_status"] == "output artifact was not updated"
    assert before == {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gis-vector-execution-") as directory:
        work = Path(directory)
        test_clip_crs_fields_and_filters(work)
        test_dissolve_groups_and_limits(work)
        test_merge_atomic_failure_and_output_protection(work)
        test_batch_structured_artifact_report(work)
        test_benchmark_repeats_and_rss_reporting(work)
        gdb_work = work / "gdb-check"
        gdb_work.mkdir()
        test_batch_rejects_unchanged_gdb_directory(gdb_work)


if __name__ == "__main__":
    main()
