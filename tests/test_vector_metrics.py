from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from pyproj import CRS
from shapely.geometry import Point, Polygon, box


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


METRIC = _load("metric_script", SCRIPTS / "_metric.py")


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "gis-kit-mplconfig"))
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _write(path: Path, frame: gpd.GeoDataFrame) -> Path:
    frame.to_file(path, driver="GPKG", engine="pyogrio")
    return path


def test_analysis_frame_converts_us_survey_feet_and_rejects_missing_crs() -> None:
    frame = gpd.GeoDataFrame({"id": [1]}, geometry=[box(1_000_000, 200_000, 1_000_100, 200_100)], crs="EPSG:2263")
    projected, meters_per_unit = METRIC.analysis_frame(frame)
    assert CRS.from_user_input(projected.crs).to_epsg() == 2263
    assert meters_per_unit == pytest.approx(0.3048006096012192)
    assert projected.geometry.area.iloc[0] * meters_per_unit**2 == pytest.approx(929.0341161327483)

    no_crs = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)])
    with pytest.raises(ValueError, match="no CRS"):
        METRIC.analysis_frame(no_crs)

    fake_unknown_axes = SimpleNamespace(axis_info=[
        SimpleNamespace(unit_name="unknown", unit_conversion_factor=1.0),
        SimpleNamespace(unit_name="unknown", unit_conversion_factor=1.0),
    ], is_projected=True)
    with pytest.raises(ValueError, match="Cannot determine"):
        METRIC._horizontal_meters_per_unit(fake_unknown_axes)


def test_analysis_frame_requires_safe_local_utm_or_explicit_projected_crs() -> None:
    broad = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(-72.1, 40), Point(-71.9, 40)], crs="EPSG:4326")
    with pytest.raises(ValueError, match="UTM zones"):
        METRIC.analysis_frame(broad)
    projected, factor = METRIC.analysis_frame(broad, "EPSG:6933")
    assert projected.crs.to_epsg() == 6933
    assert factor == pytest.approx(1.0)


def test_analysis_frame_rejects_empty_and_invalid_geometries() -> None:
    empty = gpd.GeoDataFrame({"id": [1]}, geometry=[None], crs="EPSG:26918")
    with pytest.raises(ValueError, match="null"):
        METRIC.analysis_frame(empty)
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    invalid = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:26918")
    with pytest.raises(ValueError, match="invalid"):
        METRIC.analysis_frame(invalid)


def test_analysis_frames_checks_combined_geographic_extent() -> None:
    left = gpd.GeoDataFrame(geometry=[Point(-72.1, 40)], crs="EPSG:4326")
    right = gpd.GeoDataFrame(geometry=[Point(-71.9, 40)], crs="EPSG:4326")
    with pytest.raises(ValueError, match="UTM zones"):
        METRIC.analysis_frames([left, right])
    frames, factor = METRIC.analysis_frames([left, right], "EPSG:5070")
    assert frames[0].crs.to_epsg() == frames[1].crs.to_epsg() == 5070
    assert factor == pytest.approx(1.0)


@pytest.mark.parametrize(("crs", "expected_units_per_meter"), [("EPSG:26918", 1.0), ("EPSG:2263", 1 / 0.3048006096012192)])
def test_buffer_cli_keeps_meter_distance_and_source_output_crs(tmp_path: Path, crs: str, expected_units_per_meter: float) -> None:
    source = _write(tmp_path / "source.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[Point(1_000_000, 200_000)], crs=crs))
    output = tmp_path / "buffer.gpkg"
    result = _run("buffer.py", str(source), "--output", str(output), "--distance", "100")
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual.crs.to_epsg() == CRS.from_user_input(crs).to_epsg()
    assert (actual.geometry.iloc[0].bounds[2] - actual.geometry.iloc[0].bounds[0]) / 2 == pytest.approx(100 * expected_units_per_meter)


def test_buffer_explicit_analysis_crs_returns_original_geographic_crs(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-73.99, 40.75)], crs="EPSG:4326"))
    output = tmp_path / "buffer.gpkg"
    result = _run("buffer.py", str(source), "--output", str(output), "--distance", "100", "--analysis-crs", "EPSG:32618")
    assert result.returncode == 0, result.stderr
    assert gpd.read_file(output).crs.to_epsg() == 4326


def test_indicators_far_converts_square_feet_to_square_meters(tmp_path: Path) -> None:
    parcels = _write(tmp_path / "parcels.gpkg", gpd.GeoDataFrame({"pid": ["p"]}, geometry=[box(1_000_000, 200_000, 1_000_100, 200_100)], crs="EPSG:2263"))
    buildings = _write(tmp_path / "buildings.gpkg", gpd.GeoDataFrame({"floors": [2]}, geometry=[box(1_000_020, 200_020, 1_000_070, 200_070)], crs="EPSG:2263"))
    output = tmp_path / "far.gpkg"
    report = tmp_path / "assignment.json"
    result = _run("indicators.py", "far", "--parcels", str(parcels), "--buildings", str(buildings), "--floors-field", "floors", "--parcel-id", "pid", "--output", str(output), "--report-json", str(report))
    assert result.returncode == 0, result.stderr
    row = gpd.read_file(output).iloc[0]
    assert row["parcel_area_m2"] == pytest.approx(929.0341161327483)
    assert row["total_floor_area_m2"] == pytest.approx(464.5170580663741)
    assert row["FAR"] == pytest.approx(0.5)
    import json
    assigned = json.loads(report.read_text(encoding="utf-8"))
    assert (assigned["matched_buildings"], assigned["unmatched_buildings"]) == (1, 0)
    assert assigned["allocated_footprint_area_m2"] == pytest.approx(232.25852903318707)


@pytest.mark.parametrize("assignment", ["area", "unique"])
def test_indicators_cross_parcel_assignment_is_explicit_and_conservative(tmp_path: Path, assignment: str) -> None:
    parcels = _write(tmp_path / "parcels.gpkg", gpd.GeoDataFrame({"pid": ["A", "B"]}, geometry=[box(0, 0, 100, 100), box(100, 0, 200, 100)], crs="EPSG:26918"))
    buildings = _write(tmp_path / "buildings.gpkg", gpd.GeoDataFrame({"floors": [2]}, geometry=[box(50, 20, 150, 80)], crs="EPSG:26918"))
    output = tmp_path / f"summary-{assignment}.gpkg"
    result = _run("indicators.py", "summary", "--parcels", str(parcels), "--buildings", str(buildings), "--floors-field", "floors", "--parcel-id", "pid", "--assignment", assignment, "--output", str(output))
    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output).sort_values("pid")
    if assignment == "area":
        assert actual["building_area_m2"].tolist() == pytest.approx([3000, 3000])
        assert actual["total_floor_area_m2"].tolist() == pytest.approx([6000, 6000])
        assert actual["density"].tolist() == pytest.approx([0.3, 0.3])
    else:
        assert actual["building_area_m2"].tolist() == pytest.approx([6000, 0])
        assert actual["total_floor_area_m2"].tolist() == pytest.approx([12000, 0])
        assert actual["density"].tolist() == pytest.approx([0.6, 0])
    assert '"matched_buildings": 1' in result.stdout


def test_indicators_report_matched_unmatched_and_partial_buildings(tmp_path: Path) -> None:
    parcels = _write(tmp_path / "parcels.gpkg", gpd.GeoDataFrame({"pid": ["p"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    buildings = _write(tmp_path / "buildings.gpkg", gpd.GeoDataFrame({"floors": [2, 3]}, geometry=[box(5, 0, 15, 10), box(20, 20, 30, 30)], crs="EPSG:26918"))
    report = tmp_path / "assignment.json"
    result = _run("indicators.py", "far", "--parcels", str(parcels), "--buildings", str(buildings), "--floors-field", "floors", "--parcel-id", "pid", "--report-json", str(report))
    assert result.returncode == 0, result.stderr
    import json
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["matched_buildings"] == 1
    assert details["unmatched_buildings"] == 1
    assert details["partially_unallocated_buildings"] == 1
    assert details["source_footprint_area_m2"] == pytest.approx(200)
    assert details["allocated_footprint_area_m2"] == pytest.approx(50)
    assert details["unallocated_footprint_area_m2"] == pytest.approx(150)


def test_indicators_reject_overlapping_parcels_instead_of_double_counting(tmp_path: Path) -> None:
    parcels = _write(tmp_path / "parcels.gpkg", gpd.GeoDataFrame({"pid": ["A", "B"]}, geometry=[box(0, 0, 100, 100), box(50, 0, 150, 100)], crs="EPSG:26918"))
    buildings = _write(tmp_path / "buildings.gpkg", gpd.GeoDataFrame({"floors": [2]}, geometry=[box(60, 20, 90, 50)], crs="EPSG:26918"))
    output = tmp_path / "rejected.gpkg"
    result = _run("indicators.py", "far", "--parcels", str(parcels), "--buildings", str(buildings), "--floors-field", "floors", "--parcel-id", "pid", "--output", str(output))
    assert result.returncode != 0
    assert "overlap" in result.stderr.lower()
    assert not output.exists()


@pytest.mark.parametrize("value", [0, -1, 1.5, float("nan"), None])
def test_indicators_reject_illegal_floor_counts(tmp_path: Path, value: float | None) -> None:
    parcels = _write(tmp_path / "parcels.gpkg", gpd.GeoDataFrame({"pid": ["p"]}, geometry=[box(0, 0, 100, 100)], crs="EPSG:26918"))
    buildings = _write(tmp_path / "buildings.gpkg", gpd.GeoDataFrame({"floors": [value]}, geometry=[box(10, 10, 20, 20)], crs="EPSG:26918"))
    output = tmp_path / "bad-floor.gpkg"
    result = _run("indicators.py", "far", "--parcels", str(parcels), "--buildings", str(buildings), "--floors-field", "floors", "--parcel-id", "pid", "--output", str(output))
    assert result.returncode != 0
    assert "floor" in result.stderr.lower()
    assert not output.exists()


def test_stats_spatial_supports_optional_study_area_and_descriptive_nni(tmp_path: Path) -> None:
    points = _write(tmp_path / "points.gpkg", gpd.GeoDataFrame({"id": [1, 2, 3]}, geometry=[Point(0, 0), Point(3, 0), Point(0, 4)], crs="EPSG:26918"))
    study = _write(tmp_path / "study.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[box(-10, -10, 10, 10)], crs="EPSG:26918"))
    measured = _run("stats.py", "spatial", str(points), "--study-area", str(study))
    assert measured.returncode == 0, measured.stderr
    assert "mean nearest neighbor distance m" in measured.stdout
    assert "no significance test" in measured.stdout
    assert "study-area geometry; feature centroids covered" in measured.stdout
    bbox = _run("stats.py", "spatial", str(points))
    assert bbox.returncode == 0, bbox.stderr
    assert "layer-bounds bbox fallback (legacy default" in bbox.stdout
    assert "feature density per km2" in bbox.stdout


def test_stats_degenerate_bbox_still_reports_nn_distance_without_significance(tmp_path: Path) -> None:
    points = _write(tmp_path / "points.gpkg", gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(0, 0), Point(5, 0)], crs="EPSG:26918"))
    result = _run("stats.py", "spatial", str(points))
    assert result.returncode == 0, result.stderr
    assert "mean nearest neighbor distance m" in result.stdout
    assert "5.000000" in result.stdout
    assert "N/A: zero-area denominator" in result.stdout
    assert "pattern" not in result.stdout.lower()


def test_stats_study_area_counts_centroids_not_intersecting_slivers(tmp_path: Path) -> None:
    points = _write(tmp_path / "points.gpkg", gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(0, 0), Point(100, 100)], crs="EPSG:26918"))
    study = _write(tmp_path / "study.gpkg", gpd.GeoDataFrame({"id": [1]}, geometry=[box(-1, -1, 1, 1)], crs="EPSG:26918"))
    result = _run("stats.py", "spatial", str(points), "--study-area", str(study))
    assert result.returncode == 0, result.stderr
    import re
    assert re.search(r"feature count\s+1", result.stdout)


def test_stats_cross_reports_within_boundary_and_multi_zone_intersects(tmp_path: Path) -> None:
    zones = _write(tmp_path / "zones.gpkg", gpd.GeoDataFrame({"zone": ["A", "B"]}, geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)], crs="EPSG:26918"))
    targets = _write(tmp_path / "targets.gpkg", gpd.GeoDataFrame({"amount": [2, 5]}, geometry=[Point(5, 5), Point(10, 5)], crs="EPSG:26918"))
    within = _run("stats.py", "cross", "--input", str(zones), "--target", str(targets), "--zone-field", "zone", "--sum-field", "amount")
    assert within.returncode == 0, within.stderr
    assert '"predicate": "within"' in within.stdout
    assert '"match_pairs": 1' in within.stdout
    assert "boundary-only" in within.stdout
    intersects = _run("stats.py", "cross", "--input", str(zones), "--target", str(targets), "--zone-field", "zone", "--sum-field", "amount", "--predicate", "intersects")
    assert intersects.returncode == 0, intersects.stderr
    assert '"match_pairs": 3' in intersects.stdout
    assert "may match multiple zones" in intersects.stdout


def test_stats_cross_rejects_nonunique_zone_ids_and_missing_crs(tmp_path: Path) -> None:
    zones = _write(tmp_path / "zones.gpkg", gpd.GeoDataFrame({"zone": ["A", "A"]}, geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)], crs="EPSG:26918"))
    targets = _write(tmp_path / "targets.gpkg", gpd.GeoDataFrame({"amount": [1]}, geometry=[Point(1, 1)], crs="EPSG:26918"))
    duplicate = _run("stats.py", "cross", "--input", str(zones), "--target", str(targets), "--zone-field", "zone")
    assert duplicate.returncode != 0
    assert "unique and non-null" in duplicate.stderr
    no_crs = _write(tmp_path / "no-crs.gpkg", gpd.GeoDataFrame({"zone": ["A"]}, geometry=[box(0, 0, 10, 10)]))
    missing = _run("stats.py", "cross", "--input", str(no_crs), "--target", str(targets), "--zone-field", "zone")
    assert missing.returncode != 0
    assert "known CRS" in missing.stderr


def test_stats_cross_rejects_output_name_conflicts(tmp_path: Path) -> None:
    zones = _write(tmp_path / "zones.gpkg", gpd.GeoDataFrame({"count": ["A"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:26918"))
    targets = _write(tmp_path / "targets.gpkg", gpd.GeoDataFrame({"amount": [1]}, geometry=[Point(1, 1)], crs="EPSG:26918"))
    result = _run("stats.py", "cross", "--input", str(zones), "--target", str(targets), "--zone-field", "count")
    assert result.returncode != 0
    assert "conflicts" in result.stderr
