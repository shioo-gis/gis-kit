#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "shapely>=2.0",
# ]
# ///
"""Check polygon topology in an explicit metric analysis frame."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry import Polygon
from shapely.validation import explain_validity

from _safe_io import iter_vector_chunks
from _core import read_vector, reproject

DEFAULT_CHECKS = "gaps,overlaps,slivers,invalid-geometries,duplicates"
KNOWN_CHECKS = {"gaps", "overlaps", "slivers", "invalid-geometries", "duplicates", "self-intersections"}
DEFAULT_MAX_ISSUES = 10_000
DEFAULT_BATCH_SIZE = 64
CANDIDATE_CHUNK_SIZE = 10_000


@dataclass
class TopoError:
    check: str
    geometry: object | None
    detail: str
    feature_a: str | None = None
    feature_b: str | None = None
    position_a: int | None = None
    position_b: int | None = None
    severity: str = "error"
    basis: str = "topology"
    area_m2: float | None = None
    geometry_crs: str | None = None

    def as_json(self) -> dict[str, Any]:
        geom = self.geometry
        return {
            "check": self.check,
            "detail": self.detail,
            "feature_a": self.feature_a,
            "feature_b": self.feature_b,
            "source_a_id": self.feature_a,
            "source_b_id": self.feature_b,
            "position_a": self.position_a,
            "position_b": self.position_b,
            "severity": self.severity,
            "basis": self.basis,
            "area_m2": self.area_m2,
            "geometry_crs": self.geometry_crs,
            "geometry": None,
            "geometry_type": None if geom is None else geom.geom_type,
            "has_geometry": geom is not None and not geom.is_empty,
        }


@dataclass
class TopoReport:
    source_crs: str | None = None
    analysis_crs: str | None = None
    meters_per_unit: float | None = None
    analysis_method: str | None = None
    checks_requested: list[str] = field(default_factory=list)
    errors: list[TopoError] = field(default_factory=list)
    excluded_features: list[dict[str, Any]] = field(default_factory=list)
    feature_count: int = 0
    analyzed_feature_count: int = 0
    max_issues_per_check: int = DEFAULT_MAX_ISSUES
    candidate_pairs_examined: int = 0
    candidate_limit: int | None = None
    candidate_limit_reached: bool = False
    truncated_by_check: dict[str, int] = field(default_factory=dict)
    analysis_unavailable_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    _issue_totals: dict[str, int] = field(default_factory=dict, repr=False)
    _error_totals: dict[str, int] = field(default_factory=dict, repr=False)
    _retained_counts: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def error_count(self) -> int:
        return sum(self._error_totals.values())

    def add(self, issue: TopoError) -> None:
        total = self._issue_totals.get(issue.check, 0)
        self._issue_totals[issue.check] = total + 1
        if issue.severity == "error":
            self._error_totals[issue.check] = self._error_totals.get(issue.check, 0) + 1
        retained = self._retained_counts.get(issue.check, 0)
        if retained < self.max_issues_per_check:
            self.errors.append(issue)
            self._retained_counts[issue.check] = retained + 1

    def finish(self) -> None:
        self.truncated_by_check = {
            check: total - self._retained_counts.get(check, 0)
            for check, total in self._issue_totals.items()
            if total > self._retained_counts.get(check, 0)
        }

    def as_json(self) -> dict[str, Any]:
        self.finish()
        totals: dict[str, int] = dict(self._issue_totals)
        return {
            "status": "incomplete" if self.incomplete else "complete",
            "feature_count": self.feature_count,
            "analyzed_feature_count": self.analyzed_feature_count,
            "source_crs": self.source_crs,
            "analysis": {
                "crs": self.analysis_crs,
                "meters_per_unit": self.meters_per_unit,
                "method": self.analysis_method,
                "unavailable_reason": self.analysis_unavailable_reason,
                "area_threshold_units": "square metres",
            },
            "checks_requested": self.checks_requested,
            "summary": {
                "issue_count": sum(totals.values()),
                "error_count": self.error_count,
                "issues_by_check": totals,
                "retained_by_check": {
                    check: self._retained_counts.get(check, 0)
                    for check in totals
                },
                "omitted_by_check": self.truncated_by_check,
            },
            "resource_limits": {
                "max_retained_issues_per_check": self.max_issues_per_check,
                "candidate_pairs_examined": self.candidate_pairs_examined,
                "candidate_limit": self.candidate_limit,
                "candidate_limit_reached": self.candidate_limit_reached,
            },
            "excluded_features": self.excluded_features,
            "warnings": self.warnings,
            "issues": [error.as_json() for error in self.errors],
        }

    @property
    def incomplete(self) -> bool:
        return (
            self.candidate_limit_reached
            or bool(self.truncated_by_check)
            or bool(self.analysis_unavailable_reason)
            or bool(self.excluded_features)
        )


def _format_area(area: float | None) -> str:
    return "unknown" if area is None else f"{area:.6f} m²"


def format_report(report: TopoReport) -> str:
    data = report.as_json()
    summary = data["summary"]
    lines = [
        "=== Topology Check Report ===",
        f"Status: {data['status']}",
        f"Features: {report.feature_count} total, {report.analyzed_feature_count} analyzed",
        f"CRS: source={report.source_crs}; analysis={report.analysis_crs or 'unavailable'}",
        f"Issues: {summary['issue_count']} ({summary['error_count']} errors)",
    ]
    if report.analysis_unavailable_reason:
        lines.append(f"Analysis unavailable: {report.analysis_unavailable_reason}")
    if report.candidate_limit_reached:
        lines.append("WARNING: candidate-pair limit reached; topology results are incomplete.")
    for check, total in summary["issues_by_check"].items():
        lines.append(f"{check}: {total}")
        for issue in report.errors:
            if issue.check != check:
                continue
            identities = ""
            if issue.feature_a is not None:
                identities = f" [{issue.feature_a}"
                if issue.feature_b is not None:
                    identities += f" & {issue.feature_b}"
                identities += "]"
            suffix = f", area={_format_area(issue.area_m2)}" if issue.area_m2 is not None else ""
            lines.append(f"  - {issue.severity}: {issue.detail}{identities}{suffix}")
        omitted = report.truncated_by_check.get(check, 0)
        if omitted:
            lines.append(f"  - {omitted} additional issue(s) omitted by output limit")
    if report.excluded_features:
        lines.append(f"Excluded features: {len(report.excluded_features)}")
        for item in report.excluded_features:
            lines.append(f"  - {item['source_id']}: {item['reason']}")
    if report.warnings:
        lines.extend(f"WARNING: {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"


def _source_ids(gdf: gpd.GeoDataFrame, id_field: str | None) -> list[str]:
    if id_field is None:
        if "source_id" in gdf.columns:
            raise ValueError("Input field 'source_id' conflicts with generated source IDs; choose --id-field source_id or rename the input field")
        return [f"source:{position:012d}" for position in range(len(gdf))]
    if id_field not in gdf.columns:
        raise ValueError(f"ID field does not exist: {id_field}")
    values = gdf[id_field]
    if values.isna().any():
        raise ValueError(f"ID field {id_field!r} must contain unique, non-null values")
    strings = [str(value) for value in values.tolist()]
    if values.duplicated().any() or len(set(strings)) != len(strings):
        raise ValueError(f"ID field {id_field!r} must contain unique, non-null values")
    return strings


def _record_invalid_features(
    gdf: gpd.GeoDataFrame, source_ids: list[str], report: TopoReport, checks: set[str]
) -> list[int]:
    valid_positions: list[int] = []
    for position in range(len(gdf)):
        geom = gdf.geometry.iloc[position]
        reason = None
        if geom is None:
            reason = "missing geometry"
        elif geom.is_empty:
            reason = "empty geometry"
        elif geom.geom_type not in {"Polygon", "MultiPolygon"}:
            reason = f"unsupported geometry type: {geom.geom_type}"
        elif not geom.is_valid:
            reason = f"invalid geometry: {explain_validity(geom)}"
        if reason is None:
            valid_positions.append(position)
            continue
        item = {"position": position, "source_id": source_ids[position], "reason": reason}
        report.excluded_features.append(item)
        if "invalid-geometries" in checks:
            report.add(
                TopoError(
                    "invalid-geometries",
                    geom,
                    reason,
                    feature_a=source_ids[position],
                    position_a=position,
                    severity="error",
                    basis="input_validation",
                    geometry_crs=CRS.from_user_input(gdf.crs).to_string() if gdf.crs else None,
                )
            )
    return valid_positions


def _polygon_parts(geometry: object) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(_polygon_parts(part))
        return result
    return []


def _holes(geometry: object) -> list[Polygon]:
    result: list[Polygon] = []
    for polygon in _polygon_parts(geometry):
        result.extend(Polygon(interior) for interior in polygon.interiors)
    return result


def _coverage_union(
    layer: gpd.GeoDataFrame | None, *, target_crs: object, name: str
) -> object | None:
    if layer is None:
        return None
    if layer.crs is None:
        raise ValueError(f"{name} layer has no CRS")
    if len(layer) == 0:
        raise ValueError(f"{name} layer is empty")
    if layer.geometry.isna().any() or layer.geometry.is_empty.any():
        raise ValueError(f"{name} layer contains missing or empty geometries")
    if (~layer.geometry.is_valid).any():
        bad = [str(i) for i, ok in enumerate(layer.geometry.is_valid.tolist()) if not ok][:5]
        raise ValueError(f"{name} layer contains invalid geometry at row position(s): {', '.join(bad)}")
    if any(g.geom_type not in {"Polygon", "MultiPolygon"} for g in layer.geometry):
        raise ValueError(f"{name} layer must contain only polygon geometries")
    projected = reproject(layer, target_crs, purpose='measure')
    return shapely.union_all(projected.geometry.array)


def _area_issue(
    report: TopoReport,
    *,
    check: str,
    geometry: object,
    detail: str,
    source_id: str | None = None,
    position: int | None = None,
    area_m2: float,
    severity: str = "error",
    basis: str = "topology",
) -> None:
    report.add(
        TopoError(
            check,
            geometry,
            detail,
            feature_a=source_id,
            position_a=position,
            severity=severity,
            basis=basis,
            area_m2=area_m2,
            geometry_crs=report.analysis_crs,
        )
    )


def _check_gaps(
    report: TopoReport,
    valid_geometries: object,
    *,
    expected_union: object | None,
    exclusions_union: object | None,
    factor_m: float,
) -> None:
    dissolved = shapely.union_all(valid_geometries)
    if expected_union is None:
        holes = _holes(dissolved)
        basis = "identifiable_hole_only"
        severity = "info"
        label = "identifiable hole; absence of expected coverage was not established"
    else:
        missing = shapely.difference(expected_union, dissolved)
        if exclusions_union is not None:
            missing = shapely.difference(missing, exclusions_union)
        holes = _polygon_parts(missing)
        basis = "expected_coverage_minus_source_and_exclusions"
        severity = "error"
        label = "uncovered expected area"
    if exclusions_union is not None and expected_union is None:
        holes = _polygon_parts(shapely.difference(shapely.union_all(holes), exclusions_union)) if holes else []
    for hole in holes:
        area_m2 = float(shapely.area(hole)) * factor_m * factor_m
        _area_issue(
            report,
            check="gaps",
            geometry=hole,
            detail=f"{label}; area={area_m2:.6f} m²",
            area_m2=area_m2,
            severity=severity,
            basis=basis,
        )


def _check_candidates(
    report: TopoReport,
    projected: gpd.GeoDataFrame,
    valid_positions: list[int],
    source_ids: list[str],
    *,
    factor_m: float,
    overlap_threshold_m2: float,
    sliver_threshold_m2: float,
    checks: set[str],
    batch_size: int,
    max_candidates: int,
) -> None:
    geom_array = projected.geometry.array
    positions = np.asarray(valid_positions, dtype=np.int64)
    count = len(positions)
    report.analyzed_feature_count = count
    if "slivers" in checks:
        areas_m2 = shapely.area(geom_array) * factor_m * factor_m
        for local_pos in np.flatnonzero(areas_m2 < sliver_threshold_m2):
            original_pos = int(positions[local_pos])
            area_m2 = float(areas_m2[local_pos])
            _area_issue(
                report,
                check="slivers",
                geometry=geom_array[local_pos],
                detail=f"feature area below {sliver_threshold_m2:g} m²",
                source_id=source_ids[original_pos],
                position=original_pos,
                area_m2=area_m2,
                basis="feature_area_threshold_m2",
            )
    candidate_checks = checks.intersection({"overlaps", "duplicates"})
    if not candidate_checks or count < 2:
        return
    valid_gdf = projected.reset_index(drop=True)
    index = valid_gdf.sindex
    valid_array = valid_gdf.geometry.array
    # Bound candidate matrices by shrinking the query batch as N grows. Dense cases
    # fall back to one query geometry at a time before large vectorized intersections.
    bounded_batch = max(1, min(batch_size, 100_000 // max(1, count)))
    candidate_budget = max_candidates
    for start in range(0, count, bounded_batch):
        stop = min(count, start + bounded_batch)
        pairs = index.query(valid_array[start:stop], predicate="intersects")
        if pairs.size == 0:
            continue
        left = pairs[0].astype(np.int64, copy=False) + start
        right = pairs[1].astype(np.int64, copy=False)
        keep = left < right
        if not np.any(keep):
            continue
        pair_array = np.column_stack((left[keep], right[keep]))
        pair_array = np.unique(pair_array, axis=0)
        if pair_array.shape[0] > candidate_budget:
            pair_array = pair_array[:candidate_budget]
            report.candidate_limit_reached = True
        candidate_budget -= int(pair_array.shape[0])
        report.candidate_pairs_examined += int(pair_array.shape[0])
        for candidate_start in range(0, len(pair_array), CANDIDATE_CHUNK_SIZE):
            candidate_end = min(len(pair_array), candidate_start + CANDIDATE_CHUNK_SIZE)
            chunk = pair_array[candidate_start:candidate_end]
            left_idx, right_idx = chunk[:, 0], chunk[:, 1]
            duplicate = shapely.equals(valid_array[left_idx], valid_array[right_idx])
            if "duplicates" in candidate_checks:
                for k in np.flatnonzero(duplicate):
                    pos_a, pos_b = int(positions[left_idx[k]]), int(positions[right_idx[k]])
                    report.add(
                        TopoError(
                            "duplicates",
                            valid_array[left_idx[k]],
                            "geometrically identical source features",
                            feature_a=source_ids[pos_a],
                            feature_b=source_ids[pos_b],
                            position_a=pos_a,
                            position_b=pos_b,
                            basis="exact_geometry_equality",
                            geometry_crs=report.analysis_crs,
                        )
                    )
            if "overlaps" not in candidate_checks:
                continue
            report_overlap = ~duplicate if "duplicates" in candidate_checks else np.ones(len(duplicate), dtype=bool)
            if not np.any(report_overlap):
                continue
            lidx, ridx = left_idx[report_overlap], right_idx[report_overlap]
            intersections = shapely.intersection(valid_array[lidx], valid_array[ridx])
            area_values = shapely.area(intersections) * factor_m * factor_m
            reportable = (area_values > 0) & (area_values >= overlap_threshold_m2)
            for k in np.flatnonzero(reportable):
                pos_a, pos_b = int(positions[lidx[k]]), int(positions[ridx[k]])
                area_m2 = float(area_values[k])
                report.add(
                    TopoError(
                        "overlaps",
                        intersections[k],
                        f"intersection area meets {overlap_threshold_m2:g} m² threshold",
                        feature_a=source_ids[pos_a],
                        feature_b=source_ids[pos_b],
                        position_a=pos_a,
                        position_b=pos_b,
                        basis="pairwise_polygon_intersection_area_m2",
                        area_m2=area_m2,
                        geometry_crs=report.analysis_crs,
                    )
                )
        if report.candidate_limit_reached:
            break


def check_topology(
    gdf: gpd.GeoDataFrame,
    *,
    checks: set[str] | None = None,
    sliver_threshold_m2: float = 1.0,
    overlap_threshold_m2: float = 0.01,
    expected_coverage: gpd.GeoDataFrame | None = None,
    exclusions: gpd.GeoDataFrame | None = None,
    id_field: str | None = None,
    analysis_crs: str | None = None,
    max_issues: int = DEFAULT_MAX_ISSUES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_candidates: int = 1_000_000,
) -> TopoReport:
    checks = set(checks or DEFAULT_CHECKS.split(","))
    if "self-intersections" in checks:
        checks.remove("self-intersections")
        checks.add("invalid-geometries")
    unknown = checks - KNOWN_CHECKS
    if unknown:
        raise ValueError(f"Unknown check(s): {', '.join(sorted(unknown))}")
    for name, value in (("sliver threshold", sliver_threshold_m2), ("overlap threshold", overlap_threshold_m2)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number in square metres")
    if max_issues < 1:
        raise ValueError("max issues per check must be at least 1")
    if batch_size < 1 or max_candidates < 1:
        raise ValueError("batch size and candidate limit must be positive integers")
    if gdf.crs is None:
        raise ValueError("Input layer has no CRS; topology area thresholds in square metres cannot be interpreted")

    source_crs = CRS.from_user_input(gdf.crs)
    source_ids = _source_ids(gdf, id_field)
    report = TopoReport(
        source_crs=source_crs.to_string(),
        checks_requested=sorted(checks),
        feature_count=len(gdf),
        max_issues_per_check=max_issues,
        candidate_limit=max_candidates,
    )
    if len(gdf) == 0:
        report.analysis_unavailable_reason = "input layer contains no feature rows"
        report.excluded_features.append({"position": None, "source_id": None, "reason": "empty input layer"})
        report.add(TopoError("invalid-geometries", None, "empty input layer; no feature rows", basis="input_validation", geometry_crs=source_crs.to_string()))
        report.finish()
        return report

    valid_positions = _record_invalid_features(gdf, source_ids, report, checks)
    if report.excluded_features and valid_positions:
        report.warnings.append("Some input features were excluded; requested topology checks are incomplete.")
    if not valid_positions:
        report.analysis_unavailable_reason = "all input geometries are empty, invalid, or unsupported; no topology calculations were run"
        report.finish()
        return report

    # _metric owns CRS selection and units. Invalid/empty rows are excluded before
    # reprojection so one malformed source feature cannot abort valid-feature checks.
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from _metric import analysis_frames

    valid_input = gdf.iloc[valid_positions].copy()
    analysis_layers = [valid_input]
    expected_index: int | None = None
    exclusions_index: int | None = None
    if expected_coverage is not None:
        expected_index = len(analysis_layers)
        analysis_layers.append(expected_coverage)
    if exclusions is not None:
        exclusions_index = len(analysis_layers)
        analysis_layers.append(exclusions)
    projected_layers, factor_m = analysis_frames(analysis_layers, analysis_crs=analysis_crs)
    projected = projected_layers[0]
    report.analysis_crs = CRS.from_user_input(projected.crs).to_string()
    report.meters_per_unit = float(factor_m)
    report.analysis_method = (
        "caller-specified projected CRS" if analysis_crs else
        "local UTM for bounded joint analysis extent" if source_crs.is_geographic or not source_crs.equals(projected.crs) else
        "input projected CRS with verified horizontal units"
    )
    if expected_index is not None:
        expected_union = _coverage_union(projected_layers[expected_index], target_crs=projected.crs, name="Expected coverage")
    else:
        expected_union = None
    exclusions_union = (
        _coverage_union(projected_layers[exclusions_index], target_crs=projected.crs, name="Exclusions")
        if exclusions_index is not None
        else None
    )

    if "gaps" in checks:
        _check_gaps(
            report,
            projected.geometry.array,
            expected_union=expected_union,
            exclusions_union=exclusions_union,
            factor_m=float(factor_m),
        )
    if checks.intersection({"overlaps", "slivers", "duplicates"}):
        _check_candidates(
            report,
            projected,
            valid_positions,
            source_ids,
            factor_m=float(factor_m),
            overlap_threshold_m2=overlap_threshold_m2,
            sliver_threshold_m2=sliver_threshold_m2,
            checks=checks,
            batch_size=batch_size,
            max_candidates=max_candidates,
        )
    if report.candidate_limit_reached:
        report.warnings.append("Candidate-pair limit reached; pairwise checks are incomplete.")
    if report.truncated_by_check:
        report.warnings.append("Issue output was capped per check; omitted issue geometries were not exported.")
    report.finish()
    return report


def _write_errors(
    report: TopoReport,
    path: str,
    source_crs: object,
    *,
    protected_paths: tuple[str, ...] = (),
    overwrite: bool = False,
) -> int:
    report.finish()
    groups: dict[str, list[dict[str, Any]]] = {}
    for issue in report.errors:
        groups.setdefault(issue.check, []).append({
            "check": issue.check,
            "severity": issue.severity,
            "basis": issue.basis,
            "detail": issue.detail,
            "source_a_id": issue.feature_a,
            "source_b_id": issue.feature_b,
            "source_a_position": issue.position_a,
            "source_b_position": issue.position_b,
            "area_m2": issue.area_m2,
            "geometry": issue.geometry,
            "_geometry_crs": issue.geometry_crs or report.analysis_crs or str(source_crs),
        })
    if not groups:
        return 0
    from pyproj import CRS
    import pyogrio

    destination = Path(path)
    if destination.suffix.lower() != ".gpkg":
        raise ValueError("--output-errors must end with .gpkg")
    if not destination.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {destination.parent}")
    if any(destination.resolve() == Path(item).resolve() for item in protected_paths if item):
        raise ValueError("Error GeoPackage output must not overwrite an input dataset")
    if destination.exists() and not overwrite:
        raise ValueError("Error GeoPackage already exists; pass --overwrite-errors to replace it")

    staging_fd, staging_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".staging.gpkg", dir=str(destination.parent)
    )
    os.close(staging_fd)
    staging = Path(staging_name)
    staging.unlink()
    try:
        # Stage beside the destination, verify all layers after reopening, then
        # atomically replace only after the complete package passes validation.
        mode = "w"
        for layer_name, rows in groups.items():
            by_crs: dict[str, list[int]] = {}
            for i, row in enumerate(rows):
                if row["geometry"] is not None:
                    by_crs.setdefault(str(row["_geometry_crs"]), []).append(i)
            for geometry_crs, positions in by_crs.items():
                source_geometries = [rows[i]["geometry"] for i in positions]
                output_geometries = reproject(gpd.GeoSeries(source_geometries, crs=geometry_crs), source_crs, purpose='return')
                for i, geometry in zip(positions, output_geometries):
                    rows[i]["geometry"] = geometry
            for row in rows:
                row.pop("_geometry_crs", None)
            error_gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=source_crs)
            error_gdf.to_file(str(staging), layer=layer_name[:63], driver="GPKG", engine="pyogrio", mode=mode)
            mode = "a"

        layers = {str(row[0]) for row in pyogrio.list_layers(str(staging))}
        if layers != set(groups):
            raise ValueError(f"Staged error GeoPackage layers differ from requested output: {layers!r}")
        expected_crs = CRS.from_user_input(source_crs)
        for layer_name, rows in groups.items():
            count = 0
            for start, check in iter_vector_chunks(staging, layer=layer_name):
                if check.crs is None or CRS.from_user_input(check.crs) != expected_crs:
                    raise ValueError(f"Staged error layer {layer_name!r} did not retain the input CRS")
                part = rows[start:start + len(check)]
                if len(part) != len(check):
                    raise ValueError(f"Staged error layer {layer_name!r} failed feature-count verification")
                for field_name in ("source_a_id", "source_b_id", "source_a_position", "source_b_position"):
                    if check[field_name].tolist() != [row[field_name] for row in part]:
                        raise ValueError(f"Staged error layer {layer_name!r} failed {field_name} verification")
                count += len(check)
            if count != len(rows):
                raise ValueError(f"Staged error layer {layer_name!r} failed feature-count verification")

        if overwrite:
            os.replace(staging, destination)
        else:
            # Same-directory hard-link creation is atomic and fails with
            # FileExistsError if another process creates the target after our
            # earlier existence check; unlike os.replace it never clobbers it.
            os.link(staging, destination)
            staging.unlink()
    finally:
        if staging.exists():
            staging.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(staging) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    return sum(len(rows) for rows in groups.values())


def _read_layer(path: str, layer: str | None) -> gpd.GeoDataFrame:
    return read_vector(path, layer)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check polygon topology with metric area thresholds.")
    parser.add_argument("input", help="Input vector file path")
    parser.add_argument("--layer", help="Input layer name")
    parser.add_argument("--output", help="Write readable or JSON report to this path (default: stdout)")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Report format")
    parser.add_argument("--output-errors", help="Write retained issue geometries to a GeoPackage")
    parser.add_argument("--overwrite-errors", action="store_true", help="Explicitly replace an existing error GeoPackage after staged verification")
    parser.add_argument("--check", default=DEFAULT_CHECKS, help=f"Comma-separated checks: {','.join(sorted(KNOWN_CHECKS))}")
    parser.add_argument("--sliver-threshold", type=float, default=1.0, help="Minimum feature area in square metres to avoid a sliver issue")
    parser.add_argument("--overlap-threshold", type=float, default=0.01, help="Minimum pairwise overlap area in square metres to report")
    parser.add_argument("--expected-coverage", help="Polygon layer defining area expected to be covered (path)")
    parser.add_argument("--expected-coverage-layer", help="Layer name in --expected-coverage dataset")
    parser.add_argument("--exclusions", help="Polygon layer of legal holes excluded from gap reporting (path)")
    parser.add_argument("--exclusions-layer", help="Layer name in --exclusions dataset")
    parser.add_argument("--id-field", help="Unique, non-null input field used as stable source ID")
    parser.add_argument("--analysis-crs", help="Explicit projected analysis CRS; caller is responsible for its area of use")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES, help="Maximum retained issue records per check (default: 10000)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Maximum candidate query batch size")
    parser.add_argument("--max-candidates", type=int, default=1_000_000, help="Maximum candidate pairs examined; reaching this limit marks the report incomplete")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit nonzero when error-severity issues exist")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        gdf = _read_layer(args.input, args.layer)
        coverage = _read_layer(args.expected_coverage, args.expected_coverage_layer) if args.expected_coverage else None
        exclusions = _read_layer(args.exclusions, args.exclusions_layer) if args.exclusions else None
        protected_paths = tuple(path for path in (args.input, args.expected_coverage, args.exclusions) if path)
        protected_resolved = {Path(path).resolve() for path in protected_paths}
        if args.output and Path(args.output).resolve() in protected_resolved:
            raise ValueError("Report output must not overwrite an input dataset")
        if args.output and args.output_errors and Path(args.output).resolve() == Path(args.output_errors).resolve():
            raise ValueError("Report output and error GeoPackage must use different paths")
        selected_checks = {item.strip() for item in args.check.split(",") if item.strip()}
        report = check_topology(
            gdf,
            checks=selected_checks,
            sliver_threshold_m2=args.sliver_threshold,
            overlap_threshold_m2=args.overlap_threshold,
            expected_coverage=coverage,
            exclusions=exclusions,
            id_field=args.id_field,
            analysis_crs=args.analysis_crs,
            max_issues=args.max_issues,
            batch_size=args.batch_size,
            max_candidates=args.max_candidates,
        )
        content = json.dumps(report.as_json(), ensure_ascii=False, indent=2) if args.format == "json" else format_report(report)
        if args.output:
            Path(args.output).write_text(content + ("\n" if args.format == "json" else ""), encoding="utf-8")
        else:
            print(content)
        if args.output_errors:
            count = _write_errors(
                report,
                args.output_errors,
                gdf.crs,
                protected_paths=protected_paths,
                overwrite=args.overwrite_errors,
            )
            print(f"Error geometries saved: {count} retained issue(s)" if count else "No issue geometries to save.")
        return 2 if args.fail_on_error and (report.error_count or report.incomplete) else 0
    except Exception as exc:
        print(f"topo.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    raise SystemExit(main())
