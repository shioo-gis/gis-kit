from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import CRS
from shapely.geometry import Polygon, box

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/topo.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("topology_script", SCRIPT)
TOPO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOPO
SPEC.loader.exec_module(TOPO)


def _frame(geometries, crs="EPSG:3857", **columns):
    return gpd.GeoDataFrame(columns, geometry=geometries, crs=crs)


def _run(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), *args],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8", errors="replace",
    )


def test_geographic_overlap_threshold_is_square_metres_and_json_errors_keep_input_crs(tmp_path: Path) -> None:
    source = tmp_path / "geographic.gpkg"
    errors = tmp_path / "errors.gpkg"
    report_path = tmp_path / "report.json"
    _frame(
        [box(0, 0, 0.001, 0.001), box(0.0009, 0, 0.0019, 0.001)],
        "EPSG:4326",
        label=["west", "east"],
    ).to_file(source, driver="GPKG", engine="pyogrio")

    result = _run(
        source,
        "--check", "overlaps",
        "--overlap-threshold", "0.01",
        "--format", "json",
        "--output", str(report_path),
        "--output-errors", str(errors),
        "--fail-on-error",
    )

    assert result.returncode == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["issues_by_check"]["overlaps"] == 1
    issue = report["issues"][0]
    assert issue["area_m2"] == pytest.approx(1233.3, rel=0.03)
    assert issue["source_a_id"] == "source:000000000000"
    assert issue["source_b_id"] == "source:000000000001"
    assert report["analysis"]["area_threshold_units"] == "square metres"
    saved = gpd.read_file(errors, layer="overlaps", engine="pyogrio")
    assert saved.crs.to_epsg() == 4326
    assert saved.loc[0, "source_a_id"] == issue["source_a_id"]
    assert saved.loc[0, "source_b_id"] == issue["source_b_id"]


def test_projected_us_survey_feet_area_threshold_is_converted_to_square_metres() -> None:
    foot = 1 / 0.3048006096012192
    frame = _frame(
        [box(300_000, 120_000, 300_010, 120_010), box(300_010 - foot, 120_000, 300_020, 120_010)],
        "EPSG:2263",
    )
    frame.index = [10, 20]

    report = TOPO.check_topology(
        frame,
        checks={"overlaps"},
        overlap_threshold_m2=0.5,
    )

    assert report.analysis_crs == "EPSG:2263"
    assert report.meters_per_unit == pytest.approx(0.3048006096012192)
    assert report._issue_totals["overlaps"] == 1
    assert report.errors[0].feature_a == "source:000000000000"
    assert report.errors[0].feature_b == "source:000000000001"
    assert report.errors[0].position_a == 0
    assert report.errors[0].position_b == 1
    assert report.errors[0].area_m2 == pytest.approx(3.048006096, rel=1e-8)


def test_missing_crs_is_rejected_and_wide_geographic_input_needs_explicit_analysis_crs(tmp_path: Path) -> None:
    missing = tmp_path / "missing.gpkg"
    _frame([box(0, 0, 1, 1)], None).to_file(missing, driver="GPKG", engine="pyogrio")
    result = _run(missing, "--check", "slivers")
    assert result.returncode == 2
    assert "has no CRS" in result.stderr

    wide = _frame([box(-10, 0, -9.999, 0.001), box(10, 0, 10.001, 0.001)], "EPSG:4326")
    with pytest.raises(ValueError, match="local UTM|UTM"):
        TOPO.check_topology(wide, checks={"overlaps"})
    report = TOPO.check_topology(wide, checks={"overlaps"}, analysis_crs="EPSG:3857")
    assert report.analysis_crs == "EPSG:3857"
    assert report.analysis_method == "caller-specified projected CRS"


def test_cli_checks_combined_source_and_expected_coverage_extent(tmp_path: Path) -> None:
    source = tmp_path / "source.gpkg"
    coverage = tmp_path / "wide-coverage.gpkg"
    report_path = tmp_path / "explicit-analysis.json"
    _frame([box(116, 30, 116.01, 30.01)], "EPSG:4326").to_file(source, driver="GPKG", engine="pyogrio")
    _frame([box(100, 25, 120, 35)], "EPSG:4326").to_file(coverage, driver="GPKG", engine="pyogrio")

    automatic = _run(source, "--expected-coverage", str(coverage), "--format", "json")
    automatic_exclusion = _run(source, "--exclusions", str(coverage), "--check", "gaps")
    explicit = _run(
        source,
        "--expected-coverage", str(coverage),
        "--analysis-crs", "EPSG:3857",
        "--format", "json",
        "--output", str(report_path),
    )

    assert automatic.returncode == 2
    assert "too broad" in automatic.stderr or "UTM" in automatic.stderr
    assert automatic_exclusion.returncode == 2
    assert "too broad" in automatic_exclusion.stderr or "UTM" in automatic_exclusion.stderr
    assert explicit.returncode == 0, explicit.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["analysis"]["crs"] == "EPSG:3857"


def test_cli_rejects_expected_coverage_without_crs(tmp_path: Path) -> None:
    source = tmp_path / "source.gpkg"
    coverage = tmp_path / "coverage-no-crs.gpkg"
    _frame([box(116, 30, 116.01, 30.01)], "EPSG:4326").to_file(source, driver="GPKG", engine="pyogrio")
    _frame([box(116, 30, 116.02, 30.02)], None).to_file(coverage, driver="GPKG", engine="pyogrio")

    result = _run(source, "--expected-coverage", str(coverage), "--check", "gaps")

    assert result.returncode == 2
    assert "known CRS" in result.stderr


def test_cli_rejects_projected_expected_coverage_with_unknown_axis_units(tmp_path: Path) -> None:
    source = tmp_path / "source.gpkg"
    coverage = tmp_path / "coverage-unknown-units.gpkg"
    _frame([box(116, 30, 116.01, 30.01)], "EPSG:4326").to_file(source, driver="GPKG", engine="pyogrio")
    unknown_wkt = re.sub(r',ID\["EPSG",\d+\]', "", CRS.from_epsg(32650).to_wkt())
    unknown_wkt = unknown_wkt.replace('LENGTHUNIT["metre",1', 'LENGTHUNIT["unknown",1')
    unknown_wkt = unknown_wkt.replace('PARAMETER["Longitude of natural origin",117', 'PARAMETER["Longitude of natural origin",118')
    unknown_wkt = unknown_wkt.replace("WGS 84 / UTM zone 50N", "Custom mystery zone")
    unknown_crs = CRS.from_wkt(unknown_wkt)
    assert all(axis.unit_name == "unknown" for axis in unknown_crs.axis_info[:2])
    _frame([box(300_000, 3_300_000, 301_000, 3_301_000)], unknown_crs).to_file(
        coverage, driver="GPKG", engine="pyogrio"
    )

    result = _run(source, "--expected-coverage", str(coverage), "--check", "gaps")

    assert result.returncode == 2
    assert "Cannot determine" in result.stderr or "known linear unit" in result.stderr


def test_duplicate_geometries_are_diagnosed_separately_from_overlaps() -> None:
    frame = _frame([box(0, 0, 10, 10), box(0, 0, 10, 10)], "EPSG:3857", parcel=["a", "b"])
    report = TOPO.check_topology(frame, checks={"duplicates", "overlaps"}, id_field="parcel")

    assert report._issue_totals == {"duplicates": 1}
    assert report.errors[0].feature_a == "a"
    assert report.errors[0].feature_b == "b"
    assert report.errors[0].basis == "exact_geometry_equality"
    overlaps_only = TOPO.check_topology(frame, checks={"overlaps"})
    assert overlaps_only._issue_totals == {"overlaps": 1}


def test_expected_coverage_and_exclusions_distinguish_known_gap_from_legal_hole() -> None:
    source = _frame([box(0, 0, 4, 10), box(6, 0, 10, 10)], "EPSG:3857")
    expected = _frame([box(0, 0, 10, 10)], "EPSG:3857")
    exclusion = _frame([box(4, 0, 6, 10)], "EPSG:3857")

    uncovered = TOPO.check_topology(source, checks={"gaps"}, expected_coverage=expected)
    allowed = TOPO.check_topology(source, checks={"gaps"}, expected_coverage=expected, exclusions=exclusion)

    assert uncovered._issue_totals["gaps"] == 1
    assert uncovered.errors[0].severity == "error"
    assert uncovered.errors[0].basis == "expected_coverage_minus_source_and_exclusions"
    assert uncovered.errors[0].area_m2 == pytest.approx(20)
    assert allowed._issue_totals == {}


def test_unreferenced_hole_is_reported_as_identifiable_not_as_error() -> None:
    frame = _frame(
        [box(0, 0, 4, 1), box(0, 3, 4, 4), box(0, 1, 1, 3), box(3, 1, 4, 3)],
        "EPSG:3857",
    )
    report = TOPO.check_topology(frame, checks={"gaps"})

    assert report._issue_totals["gaps"] == 1
    assert report.errors[0].severity == "info"
    assert report.errors[0].basis == "identifiable_hole_only"
    assert report.error_count == 0


def test_invalid_geometries_are_explicitly_excluded_without_make_valid() -> None:
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    frame = _frame([bowtie, box(10, 10, 20, 20)], "EPSG:3857", parcel=["bad", "ok"])
    report = TOPO.check_topology(frame, checks={"invalid-geometries", "slivers"}, id_field="parcel")

    assert report.analyzed_feature_count == 1
    assert report.excluded_features == [{"position": 0, "source_id": "bad", "reason": "invalid geometry: Self-intersection[1 1]"}]
    assert report.errors[0].feature_a == "bad"
    assert "Self-intersection" in report.errors[0].detail
    assert report.errors[0].geometry.is_valid is False
    skipped_diagnostic = TOPO.check_topology(frame, checks={"slivers"})
    assert skipped_diagnostic.incomplete
    assert skipped_diagnostic._issue_totals == {}


def test_all_invalid_input_still_returns_diagnostic_report() -> None:
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    report = TOPO.check_topology(_frame([bowtie], "EPSG:3857"))

    assert report.analysis_crs is None
    assert report.analysis_unavailable_reason
    assert report._issue_totals["invalid-geometries"] == 1


def test_candidate_and_output_caps_mark_report_incomplete_without_losing_error_counts() -> None:
    frame = _frame([box(0, 0, 2, 2), box(1, 0, 3, 2), box(1.5, 0, 3.5, 2)], "EPSG:3857")
    report = TOPO.check_topology(frame, checks={"overlaps"}, max_issues=1)
    assert report._issue_totals["overlaps"] == 3
    assert len(report.errors) == 1
    assert report.error_count == 3
    assert report.as_json()["status"] == "incomplete"
    assert report.truncated_by_check == {"overlaps": 2}

    capped = TOPO.check_topology(frame, checks={"overlaps"}, max_candidates=1)
    assert capped.candidate_limit_reached
    assert capped.as_json()["status"] == "incomplete"


def test_cli_fail_on_error_is_opt_in(tmp_path: Path) -> None:
    source = tmp_path / "overlap.gpkg"
    _frame([box(0, 0, 10, 10), box(8, 0, 18, 10)], "EPSG:3857").to_file(source, driver="GPKG", engine="pyogrio")

    ordinary = _run(source, "--check", "overlaps")
    strict = _run(source, "--check", "overlaps", "--fail-on-error")

    assert ordinary.returncode == 0
    assert strict.returncode == 2
    assert "overlaps: 1" in strict.stdout


def test_candidate_truncation_fails_closed_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "near-threshold.gpkg"
    report_path = tmp_path / "report.json"
    _frame([box(0, 0, 10, 10), box(9, 0, 19, 10), box(8, 0, 18, 10)], "EPSG:3857").to_file(
        source, driver="GPKG", engine="pyogrio"
    )

    result = _run(
        source,
        "--check", "overlaps",
        "--overlap-threshold", "1000",
        "--max-candidates", "1",
        "--fail-on-error",
        "--format", "json",
        "--output", str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["error_count"] == 0
    assert report["status"] == "incomplete"
    assert result.returncode == 2


def test_error_gpkg_requires_explicit_overwrite_and_never_overwrites_input(tmp_path: Path) -> None:
    source = tmp_path / "overlap.gpkg"
    existing = tmp_path / "errors.gpkg"
    _frame([box(0, 0, 10, 10), box(8, 0, 18, 10)], "EPSG:3857").to_file(
        source, driver="GPKG", engine="pyogrio"
    )
    existing.write_bytes(b"keep this existing file")
    before = existing.read_bytes()

    refused = _run(source, "--check", "overlaps", "--output-errors", str(existing))
    assert refused.returncode == 2
    assert existing.read_bytes() == before

    replaced = _run(
        source,
        "--check", "overlaps",
        "--output-errors", str(existing),
        "--overwrite-errors",
    )
    assert replaced.returncode == 0, replaced.stderr
    assert len(gpd.read_file(existing, layer="overlaps", engine="pyogrio")) == 1

    protected = _run(source, "--check", "overlaps", "--output-errors", str(source), "--overwrite-errors")
    assert protected.returncode == 2
    assert len(gpd.read_file(source, engine="pyogrio")) == 2


def test_error_gpkg_no_clobber_is_atomic_when_target_appears_after_precheck(tmp_path: Path, monkeypatch) -> None:
    source = _frame([box(0, 0, 10, 10), box(8, 0, 18, 10)], "EPSG:3857")
    report = TOPO.check_topology(source, checks={"overlaps"})
    destination = tmp_path / "race.gpkg"
    real_link = TOPO.os.link

    def collide(staging, target):
        Path(target).write_bytes(b"created by concurrent process")
        return real_link(staging, target)

    monkeypatch.setattr(TOPO.os, "link", collide)
    with pytest.raises(FileExistsError):
        TOPO._write_errors(report, str(destination), source.crs)

    assert destination.read_bytes() == b"created by concurrent process"
    assert not list(tmp_path.glob(".*.staging.gpkg"))
