#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["geopandas>=1.0", "pyogrio>=0.10", "tabulate"]
# ///
"""Calculate area-based urban indicators with explicit, auditable CRS rules."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
import tempfile

import geopandas as gpd
from shapely.ops import unary_union

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _table import tabulate

try:
    from _safe_io import write_vector_atomic
except ImportError:  # Support direct module loading by tests.
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _safe_io import write_vector_atomic

try:
    from _metric import analysis_frame, analysis_frames, analysis_method
except ImportError:  # Support direct module loading in the regression tests.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _metric import analysis_frame, analysis_frames, analysis_method
from _budget import check_vector


def _read(path: str, layer: str | None = None) -> gpd.GeoDataFrame:
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def _write_result(
    gdf: gpd.GeoDataFrame,
    output: str | None,
    *,
    overwrite: bool = False,
    protected_paths: tuple[str, ...] = (),
) -> None:
    if output is None:
        return
    path = Path(output)
    if path.suffix.lower() == ".csv":
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(path.resolve(strict=False) == Path(source).resolve(strict=False) for source in protected_paths):
            raise ValueError(f"Output must not replace an input dataset: {path}")
        if path.exists() and (path.is_symlink() or not path.is_file() or not overwrite):
            raise ValueError(f"Refusing to replace existing CSV output without a regular file and --overwrite: {path}")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                gdf.drop(columns=gdf.geometry.name, errors="ignore").to_csv(handle, index=False)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print("CSV output uses same-directory staging and atomic publication.")
    else:
        if path.suffix.lower() in {".gpkg", ".geojson", ".json", ".fgb"}:
            evidence = write_vector_atomic(gdf, path, overwrite=overwrite, protected_paths=protected_paths)
            print(f"=> {path} ({evidence['features']} features; atomic readback verified)")
        else:
            if any(path.resolve(strict=False) == Path(source).resolve(strict=False) for source in protected_paths):
                raise ValueError(f"Output must not replace an input dataset: {path}")
            if path.exists() and not overwrite:
                raise FileExistsError(f"Output already exists; pass --overwrite to replace it: {path}")
            gdf.to_file(path, engine="pyogrio")
            print("Warning: this multi-file/legacy format output is not atomic.")
            print(f"=> {path}")


def _emit_table(rows: list[dict], args: argparse.Namespace, analysis_crs: object, factor: float, source_crs: object) -> None:
    print(tabulate(rows, headers="keys", tablefmt="simple", floatfmt=".4f"))
    assignment = getattr(args, "assignment", "area")
    print(f"analysis_crs={analysis_crs}; method={analysis_method(source_crs, args.analysis_crs, used=analysis_crs)}; meters_per_unit={factor:.12g}; assignment={assignment}")


def _emit_assignment_report(report: dict[str, object], args: argparse.Namespace) -> None:
    print("Building assignment report: " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    if args.report_json:
        path = Path(args.report_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        protected = [Path(args.parcels), Path(args.buildings)]
        if getattr(args, "green", None):
            protected.append(Path(args.green))
        if any(path.resolve(strict=False) == source.resolve(strict=False) for source in protected):
            raise ValueError(f"Report path must not replace an input dataset: {path}")
        if getattr(args, "output", None) and path.resolve(strict=False) == Path(args.output).resolve(strict=False):
            raise ValueError("Report JSON path must differ from the vector/CSV output path")
        data = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if path.exists() and (path.is_symlink() or not path.is_file() or not getattr(args, "overwrite", False)):
            raise FileExistsError(f"Report already exists; pass --overwrite to replace it: {path}")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if getattr(args, "overwrite", False):
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _check_parcels(parcels: gpd.GeoDataFrame, parcel_id: str) -> None:
    if parcel_id not in parcels.columns:
        raise ValueError(f"Parcel ID field {parcel_id!r} not found")
    if parcels[parcel_id].isna().any() or parcels[parcel_id].duplicated().any():
        raise ValueError("Parcel ID field must be unique and non-null")


def _reject_overlapping_parcels(parcels: gpd.GeoDataFrame, factor: float) -> None:
    """Reject positive-area parcel overlap; boundary touches remain valid."""
    index = parcels.sindex
    for i, geom in enumerate(parcels.geometry):
        for j in index.query(geom, predicate="intersects"):
            j = int(j)
            if j <= i:
                continue
            overlap_m2 = geom.intersection(parcels.geometry.iloc[j]).area * factor * factor
            if overlap_m2 > 0:
                raise ValueError(f"Parcels at source rows {i} and {j} overlap by {overlap_m2:.9g} m²")


def _floor_counts(buildings: gpd.GeoDataFrame, floors_field: str) -> list[float]:
    if floors_field not in buildings.columns:
        raise ValueError(f"Floor field {floors_field!r} not found in buildings")
    floors: list[float] = []
    for i, raw in enumerate(buildings[floors_field]):
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Invalid floor count at building source row {i}: {raw!r}")
        if not math.isfinite(value) or value <= 0 or not math.isclose(value, round(value), abs_tol=1e-9):
            raise ValueError(f"Floor counts must be positive finite integers; invalid row {i}: {raw!r}")
        floors.append(value)
    return floors


def _prepare(
    parcels_source: gpd.GeoDataFrame,
    buildings_source: gpd.GeoDataFrame,
    parcel_id: str,
    analysis_crs: str | None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, float]:
    _check_parcels(parcels_source, parcel_id)
    (parcels, buildings), factor = analysis_frames([parcels_source, buildings_source], analysis_crs)
    parcel_areas = parcels.geometry.area * factor * factor
    if (parcel_areas <= 0).any():
        raise ValueError("Parcel geometries must have positive area")
    _reject_overlapping_parcels(parcels, factor)
    return parcels, buildings, factor


def _allocate_buildings(
    parcels: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    factor: float,
    assignment: str,
    floors: list[float] | None = None,
) -> tuple[list[float], list[float], dict[str, object]]:
    if assignment not in {"area", "unique"}:
        raise ValueError("assignment must be area or unique")
    n = len(parcels)
    building_area = [0.0] * n
    floor_area = [0.0] * n
    parcel_index = parcels.sindex
    report: dict[str, object] = {
        "assignment": assignment,
        "building_count": len(buildings),
        "matched_buildings": 0,
        "unmatched_buildings": 0,
        "partially_unallocated_buildings": 0,
        "source_footprint_area_m2": 0.0,
        "allocated_footprint_area_m2": 0.0,
        "unallocated_footprint_area_m2": 0.0,
    }
    for bpos, geom in enumerate(buildings.geometry):
        source_area = geom.area * factor * factor
        if not math.isfinite(source_area) or source_area <= 0:
            raise ValueError(f"Building source row {bpos} must have positive finite area")
        report["source_footprint_area_m2"] += source_area
        intersections: list[tuple[int, float]] = []
        for raw_pos in parcel_index.query(geom, predicate="intersects"):
            ppos = int(raw_pos)
            overlap = geom.intersection(parcels.geometry.iloc[ppos]).area * factor * factor
            if overlap > 0:
                intersections.append((ppos, overlap))
        if not intersections:
            report["unmatched_buildings"] += 1
            report["unallocated_footprint_area_m2"] += source_area
            continue
        report["matched_buildings"] += 1
        if assignment == "unique":
            # `max` keeps the first source parcel on equal overlap areas.
            ppos, _ = max(intersections, key=lambda item: (item[1], -item[0]))
            allocation = [(ppos, source_area)]
        else:
            allocation = intersections
            allocated = sum(area for _, area in allocation)
            if allocated < source_area - 1e-7:
                report["partially_unallocated_buildings"] += 1
                report["unallocated_footprint_area_m2"] += source_area - allocated
        for ppos, area_m2 in allocation:
            building_area[ppos] += area_m2
            if floors is not None:
                floor_area[ppos] += area_m2 * floors[bpos]
    if sum(building_area) > buildings.geometry.area.sum() * factor * factor + 1e-7 and assignment == "area":
        raise ValueError("Allocated building area exceeds source building area; parcel overlap may be present")
    report["allocated_footprint_area_m2"] = sum(building_area)
    return building_area, floor_area, report


def _base_result(source_parcels: gpd.GeoDataFrame, analysis_parcels: gpd.GeoDataFrame, factor: float) -> gpd.GeoDataFrame:
    result = source_parcels.copy()
    result["parcel_area_m2"] = analysis_parcels.geometry.area.to_numpy() * factor * factor
    return result


def cmd_far(args: argparse.Namespace) -> None:
    parcels_source, buildings_source = _read(args.parcels, args.parcels_layer), _read(args.buildings, args.buildings_layer)
    _check_parcels(parcels_source, args.parcel_id)
    floors = _floor_counts(buildings_source, args.floors_field)
    parcels, buildings, factor = _prepare(parcels_source, buildings_source, args.parcel_id, args.analysis_crs)
    _, floor_area, assignment_report = _allocate_buildings(parcels, buildings, factor, args.assignment, floors)
    result = _base_result(parcels_source, parcels, factor)
    result["total_floor_area_m2"] = floor_area
    result["FAR"] = result["total_floor_area_m2"] / result["parcel_area_m2"]
    rows = result[[args.parcel_id, "parcel_area_m2", "total_floor_area_m2", "FAR"]].to_dict("records")
    _emit_table(rows, args, parcels.crs, factor, parcels_source.crs)
    assignment_report["analysis_crs"] = str(parcels.crs)
    assignment_report["analysis_method"] = analysis_method(parcels_source.crs, args.analysis_crs, used=parcels.crs)
    _emit_assignment_report(assignment_report, args)
    _write_result(result, args.output, overwrite=args.overwrite, protected_paths=(args.parcels, args.buildings))


def cmd_density(args: argparse.Namespace) -> None:
    parcels_source, buildings_source = _read(args.parcels, args.parcels_layer), _read(args.buildings, args.buildings_layer)
    parcels, buildings, factor = _prepare(parcels_source, buildings_source, args.parcel_id, args.analysis_crs)
    building_area, _, assignment_report = _allocate_buildings(parcels, buildings, factor, args.assignment)
    result = _base_result(parcels_source, parcels, factor)
    result["building_area_m2"] = building_area
    result["density"] = result["building_area_m2"] / result["parcel_area_m2"]
    _emit_table(result[[args.parcel_id, "parcel_area_m2", "building_area_m2", "density"]].to_dict("records"), args, parcels.crs, factor, parcels_source.crs)
    assignment_report["analysis_crs"] = str(parcels.crs)
    assignment_report["analysis_method"] = analysis_method(parcels_source.crs, args.analysis_crs, used=parcels.crs)
    _emit_assignment_report(assignment_report, args)
    _write_result(result, args.output, overwrite=args.overwrite, protected_paths=(args.parcels, args.buildings))


def _green_by_parcel(
    parcels: gpd.GeoDataFrame, greens: gpd.GeoDataFrame, factor: float
) -> list[float]:
    index = greens.sindex
    values: list[float] = []
    for parcel in parcels.geometry:
        clipped = [parcel.intersection(greens.geometry.iloc[int(i)]) for i in index.query(parcel, predicate="intersects")]
        clipped = [geom for geom in clipped if not geom.is_empty and geom.area > 0]
        values.append(unary_union(clipped).area * factor * factor if clipped else 0.0)
    return values


def cmd_coverage(args: argparse.Namespace) -> None:
    boundary_source, target_source = _read(args.boundary, args.boundary_layer), _read(args.target, args.target_layer)
    (boundary, targets), factor = analysis_frames([boundary_source, target_source], args.analysis_crs)
    study_area_m2 = unary_union(boundary.geometry.tolist()).area * factor * factor
    if not math.isfinite(study_area_m2) or study_area_m2 <= 0:
        raise ValueError("Boundary must have positive finite area")
    clipped = [geom.intersection(unary_union(boundary.geometry.tolist())) for geom in targets.geometry]
    green_area_m2 = unary_union([g for g in clipped if not g.is_empty]).area * factor * factor
    ratio = green_area_m2 / study_area_m2
    print(tabulate([
        {"metric": "green_area_m2", "value": green_area_m2},
        {"metric": "study_area_m2", "value": study_area_m2},
        {"metric": "coverage_ratio", "value": ratio},
    ], headers="keys", tablefmt="simple", floatfmt=".6f"))
    print(f"analysis_crs={boundary.crs}; projection_method={analysis_method(boundary_source.crs, args.analysis_crs, used=boundary.crs)}; measurement_method=union area; meters_per_unit={factor:.12g}")
    if args.output:
        output = boundary_source.copy()
        output["green_area_m2"] = green_area_m2
        output["study_area_m2"] = study_area_m2
        output["coverage_ratio"] = ratio
        _write_result(output, args.output, overwrite=args.overwrite, protected_paths=(args.boundary, args.target))


def cmd_summary(args: argparse.Namespace) -> None:
    parcels_source, buildings_source = _read(args.parcels, args.parcels_layer), _read(args.buildings, args.buildings_layer)
    _check_parcels(parcels_source, args.parcel_id)
    floors = _floor_counts(buildings_source, args.floors_field)
    parcels, buildings, factor = _prepare(parcels_source, buildings_source, args.parcel_id, args.analysis_crs)
    building_area, floor_area, assignment_report = _allocate_buildings(parcels, buildings, factor, args.assignment, floors)
    result = _base_result(parcels_source, parcels, factor)
    result["building_area_m2"] = building_area
    result["total_floor_area_m2"] = floor_area
    result["FAR"] = result["total_floor_area_m2"] / result["parcel_area_m2"]
    result["density"] = result["building_area_m2"] / result["parcel_area_m2"]
    if args.green:
        green_source = _read(args.green, args.green_layer)
        (parcels_for_green, greens), green_factor = analysis_frames([parcels_source, green_source], args.analysis_crs)
        if not math.isclose(factor, green_factor, rel_tol=1e-12) or parcels_for_green.crs != parcels.crs:
            raise ValueError("Green and parcel analysis CRS differ; specify a suitable --analysis-crs")
        result["green_area_m2"] = _green_by_parcel(parcels, greens, factor)
        result["green_ratio"] = result["green_area_m2"] / result["parcel_area_m2"]
    columns = [args.parcel_id, "parcel_area_m2", "building_area_m2", "total_floor_area_m2", "FAR", "density"]
    if args.green:
        columns.append("green_ratio")
    _emit_table(result[columns].to_dict("records"), args, parcels.crs, factor, parcels_source.crs)
    assignment_report["analysis_crs"] = str(parcels.crs)
    assignment_report["analysis_method"] = analysis_method(parcels_source.crs, args.analysis_crs, used=parcels.crs)
    _emit_assignment_report(assignment_report, args)
    protected = (args.parcels, args.buildings) + ((args.green,) if args.green else ())
    _write_result(result, args.output, overwrite=args.overwrite, protected_paths=protected)


def _add_parcel_options(parser: argparse.ArgumentParser, include_floors: bool = False) -> None:
    parser.add_argument("--parcels", required=True)
    parser.add_argument("--parcels-layer")
    parser.add_argument("--parcel-id", required=True)
    parser.add_argument("--buildings", required=True)
    parser.add_argument("--buildings-layer")
    parser.add_argument("--analysis-crs", help="Explicit projected CRS; wide-area geographic work requires this")
    parser.add_argument("--assignment", choices=["area", "unique"], default="area", help="Area-proportional or whole-building parcel assignment")
    parser.add_argument("--output")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output where supported")
    parser.add_argument("--report-json", help="Optional building assignment report path")
    if include_floors:
        parser.add_argument("--floors-field", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calculate area-based urban design indicators")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("far", help="Floor Area Ratio")
    _add_parcel_options(p, include_floors=True)
    p = sub.add_parser("density", help="Building footprint density")
    _add_parcel_options(p)
    p = sub.add_parser("summary", help="All parcel indicators")
    _add_parcel_options(p, include_floors=True)
    p.add_argument("--green")
    p.add_argument("--green-layer")
    p = sub.add_parser("coverage", help="Target coverage within a study-area boundary")
    p.add_argument("--target", required=True)
    p.add_argument("--target-layer")
    p.add_argument("--boundary", required=True)
    p.add_argument("--boundary-layer")
    p.add_argument("--analysis-crs", help="Explicit projected CRS for measurement")
    p.add_argument("--output")
    p.add_argument("--overwrite", action="store_true", help="Replace an existing output where supported")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        {"far": cmd_far, "density": cmd_density, "summary": cmd_summary, "coverage": cmd_coverage}[args.command](args)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
