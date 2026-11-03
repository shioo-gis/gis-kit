from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/join.py"


def _write(path: Path, frame: gpd.GeoDataFrame) -> Path:
    frame.to_file(path, driver="GPKG", engine="pyogrio")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=ROOT, env=env, capture_output=True, encoding="utf-8", errors="replace")


def _join(left: Path, right: Path, output: Path, *options: str) -> subprocess.CompletedProcess[str]:
    return _run("--left", str(left), "--right", str(right), "--output", str(output), *options)


def test_all_keeps_every_pair_unmatched_rows_stable_ids_and_report(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"name": ["target", "empty"]}, geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"name": [None, "second"]}, geometry=[Point(1, 1), Point(2, 2)], crs="EPSG:26918"))
    output, report_path = tmp_path / "joined.gpkg", tmp_path / "report.json"
    result = _join(left, right, output, "--agg", "all", "--report-json", str(report_path))
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert len(actual) == 3
    assert actual["left_source_id"].tolist() == ["left:0", "left:0", "left:1"]
    assert actual["right_source_id"].tolist() == ["right:0", "right:1", None]
    assert actual["name_right"].iloc[0] is None
    assert actual["name_right"].iloc[1] == "second"
    assert actual["match_count"].tolist() == [2, 2, 0]
    assert json.loads(actual["matched_source_ids"].iloc[0]) == ["right:0", "right:1"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["left_matched"] == 1 and report["left_unmatched"] == 1
    assert report["right_matched"] == 2 and report["right_unmatched"] == 0
    assert report["analysis_crs"] == "EPSG:26918"


def test_first_preserves_first_entire_row_even_when_value_is_null(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"key": [1]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"label": [None, "later"], "score": [10, 20]}, geometry=[Point(1, 1), Point(2, 2)], crs="EPSG:26918"))
    output = tmp_path / "first.gpkg"
    result = _join(left, right, output, "--agg", "first")
    assert result.returncode == 0, result.stderr
    row = gpd.read_file(output).iloc[0]
    assert row["right_source_id"] == "right:0"
    assert pd.isna(row["label"])
    assert row["score"] == 10
    assert row["match_count"] == 2


def test_sum_skips_nulls_and_all_null_sum_remains_null(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"id": ["some", "none", "all_null"]}, geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30), box(40, 40, 60, 60)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"amount": [None, 4.0, None, None]}, geometry=[Point(1, 1), Point(2, 2), Point(3, 3), Point(50, 50)], crs="EPSG:26918"))
    output = tmp_path / "sum.gpkg"
    result = _join(left, right, output, "--agg", "sum", "--fields", "amount")
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output).sort_values("id")
    by_id = actual.set_index("id")
    assert by_id.loc["some", "amount"] == 4.0
    assert pd.isna(by_id.loc["none", "amount"])
    assert by_id.loc["some", "match_count"] == 3
    assert by_id.loc["none", "match_count"] == 0
    assert pd.isna(by_id.loc["all_null", "amount"])
    assert by_id.loc["all_null", "match_count"] == 1


def test_count_counts_non_null_fields_and_total_match_pairs_separately(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"id": ["p"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"amount": [None, 4.0, None]}, geometry=[Point(1, 1), Point(2, 2), Point(3, 3)], crs="EPSG:26918"))
    output = tmp_path / "count.gpkg"
    result = _join(left, right, output, "--agg", "count", "--fields", "amount")
    assert result.returncode == 0, result.stderr
    row = gpd.read_file(output).iloc[0]
    assert row["amount"] == 1
    assert row["match_count"] == 3


def test_right_nearest_searches_from_each_right_feature_with_meter_cap(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"name": ["L0", "L100"]}, geometry=[Point(0, 0), Point(100, 0)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"name": ["R1", "R2", "R99"]}, geometry=[Point(1, 0), Point(2, 0), Point(99, 0)], crs="EPSG:26918"))
    output = tmp_path / "right-nearest.gpkg"
    result = _join(left, right, output, "--how", "right", "--predicate", "nearest", "--max-distance", "2.5", "--agg", "all")
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output).sort_values("right_source_id")
    assert actual["right_source_id"].tolist() == ["right:0", "right:1", "right:2"]
    assert actual["left_source_id"].tolist() == ["left:0", "left:0", "left:1"]
    assert actual["distance_m"].tolist() == pytest.approx([1, 2, 1])
    capped = tmp_path / "right-capped.gpkg"
    capped_result = _join(left, right, capped, "--how", "right", "--predicate", "nearest", "--max-distance", "1.5", "--agg", "all")
    assert capped_result.returncode == 0, capped_result.stderr
    cap_rows = gpd.read_file(capped).sort_values("right_source_id")
    assert cap_rows["match_count"].tolist() == [1, 0, 1]
    assert cap_rows["left_source_id"].iloc[1] is None


def test_nearest_max_distance_and_output_are_meters_for_feet_crs(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[Point(1_000_000, 200_000)], crs="EPSG:2263"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"id": [2]}, geometry=[Point(1_000_003, 200_000)], crs="EPSG:2263"))
    output = tmp_path / "nearest.gpkg"
    result = _join(left, right, output, "--predicate", "nearest", "--max-distance", "1", "--agg", "first")
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert len(actual) == 1
    assert actual["distance_m"].iloc[0] == pytest.approx(3 * 0.3048006096012192)
    refused = tmp_path / "refused.gpkg"
    no_match = _join(left, right, refused, "--predicate", "nearest", "--max-distance", "0.5")
    assert no_match.returncode == 0, no_match.stderr
    assert gpd.read_file(refused)["match_count"].iloc[0] == 0


def test_explicit_stable_source_id_fields_are_preserved_and_validated(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"left_key": ["L-custom"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"right_key": ["R-custom"]}, geometry=[Point(1, 1)], crs="EPSG:26918"))
    output = tmp_path / "explicit-ids.gpkg"
    result = _join(left, right, output, "--left-id-field", "left_key", "--right-id-field", "right_key")
    assert result.returncode == 0, result.stderr
    row = gpd.read_file(output).iloc[0]
    assert row["left_source_id"] == "L-custom"
    assert row["right_source_id"] == "R-custom"
    invalid = _write(tmp_path / "invalid-right.gpkg", gpd.GeoDataFrame({"rid": ["same", "same"]}, geometry=[Point(1, 1), Point(2, 2)], crs="EPSG:26918"))
    failed = _join(left, invalid, tmp_path / "invalid-id.gpkg", "--right-id-field", "rid")
    assert failed.returncode != 0
    assert "unique and non-null" in failed.stderr


def test_right_and_inner_intersects_preserve_expected_unmatched_side(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"id": ["inside"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"v": [1, 2]}, geometry=[Point(1, 1), Point(100, 100)], crs="EPSG:26918"))
    inner = tmp_path / "inner.gpkg"
    inner_result = _join(left, right, inner, "--how", "inner", "--agg", "all")
    assert inner_result.returncode == 0, inner_result.stderr
    assert len(gpd.read_file(inner)) == 1
    right_output = tmp_path / "right-joined.gpkg"
    right_result = _join(left, right, right_output, "--how", "right", "--agg", "all")
    assert right_result.returncode == 0, right_result.stderr
    actual = gpd.read_file(right_output).sort_values("right_source_id")
    assert len(actual) == 2
    assert actual["match_count"].tolist() == [1, 0]
    assert actual["left_source_id"].iloc[1] is None


def test_inner_no_match_writes_empty_layer_with_full_schema(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"key": [1]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"value": [3]}, geometry=[Point(100, 100)], crs="EPSG:26918"))
    output = tmp_path / "empty-inner.gpkg"
    result = _join(left, right, output, "--how", "inner", "--agg", "all")
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual.empty
    assert {"left_source_id", "right_source_id", "match_count", "matched_source_ids", "value"}.issubset(actual.columns)


def test_geographic_join_validates_combined_utm_scope_and_allows_explicit_crs(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-72.1, 40)], crs="EPSG:4326"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"id": [2]}, geometry=[Point(-71.9, 40)], crs="EPSG:4326"))
    output = tmp_path / "safe.gpkg"
    refused = _join(left, right, output, "--predicate", "nearest")
    assert refused.returncode != 0
    assert "UTM zones" in refused.stderr
    accepted = _join(left, right, output, "--predicate", "nearest", "--analysis-crs", "EPSG:5070")
    assert accepted.returncode == 0, accepted.stderr


def test_field_collisions_are_deterministic_or_rejected(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"name": ["left"], "name_right": ["occupied"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"name": ["right"]}, geometry=[Point(1, 1)], crs="EPSG:26918"))
    output = tmp_path / "collision.gpkg"
    result = _join(left, right, output)
    assert result.returncode != 0
    assert "collision" in result.stderr.lower()
    assert not output.exists()
    output2 = tmp_path / "renamed.gpkg"
    renamed = _join(left, right, output2, "--suffix", "_source")
    assert renamed.returncode == 0, renamed.stderr
    assert "name_source" in gpd.read_file(output2).columns


def test_nearest_distance_field_cannot_overwrite_source_id_or_existing_property(tmp_path: Path) -> None:
    left = _write(tmp_path / "left.gpkg", gpd.GeoDataFrame({"match_count": [7]}, geometry=[Point(0, 0)], crs="EPSG:26918"))
    right = _write(tmp_path / "right.gpkg", gpd.GeoDataFrame({"v": [1]}, geometry=[Point(1, 0)], crs="EPSG:26918"))
    output = tmp_path / "protected.gpkg"
    result = _join(left, right, output, "--predicate", "nearest", "--distance-field", "match_count")
    assert result.returncode != 0
    assert "reserved output field" in result.stderr
    assert not output.exists()
