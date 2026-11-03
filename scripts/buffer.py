# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

import geopandas as gpd
from pyproj import CRS

try:
    from _metric import analysis_frame, analysis_method
except ImportError:  # Support direct module loading by the regression tests.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _metric import analysis_frame, analysis_method

try:
    from _safe_io import write_vector_atomic
except ImportError:
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _safe_io import write_vector_atomic
from _core import read_vector, reproject


CAP_STYLES = {"round": "round", "flat": "flat", "square": "square"}
JOIN_STYLES = {"round": "round", "mitre": "mitre", "bevel": "bevel"}


def _estimate_utm_crs(gdf: gpd.GeoDataFrame) -> str:
    geographic = gdf.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = geographic.total_bounds
    if not all(math.isfinite(value) for value in (minx, miny, maxx, maxy)):
        raise ValueError("Geographic extent is empty or non-finite; cannot select a local UTM CRS")
    if minx < -180 or maxx > 180 or miny < -80 or maxy > 84:
        raise ValueError("Geographic extent is outside the reliable UTM latitude/longitude domain")
    if maxx - minx > 6 or maxy - miny > 8:
        raise ValueError("Geographic extent is too broad for a reliable local UTM buffer")

    def zone(longitude: float) -> int:
        return min(60, max(1, int((longitude + 180) // 6) + 1))

    if zone(minx) != zone(maxx):
        raise ValueError("Geographic extent crosses UTM zones; cannot select a reliable local projection")
    center_lon, center_lat = (minx + maxx) / 2, (miny + maxy) / 2
    zone_number = zone(center_lon)
    epsg = (32600 if center_lat >= 0 else 32700) + zone_number
    return f"EPSG:{epsg}"


def _horizontal_unit_to_meters(crs: object) -> float:
    axes = crs.axis_info  # type: ignore[attr-defined]
    if len(axes) < 2:
        raise ValueError("CRS does not define two horizontal coordinate axes")
    factors = [axis.unit_conversion_factor for axis in axes[:2]]
    names = [axis.unit_name for axis in axes[:2]]
    if any(not name or name.strip().lower() in {"unknown", "undefined", "none", "unitless"} for name in names):
        raise ValueError("Cannot determine CRS horizontal axis units")
    if any(factor is None or not math.isfinite(factor) or factor <= 0 for factor in factors):
        raise ValueError("Cannot determine CRS horizontal axis units")
    if names[0] != names[1] or not math.isclose(factors[0], factors[1], rel_tol=1e-12):
        raise ValueError("CRS horizontal axis units are inconsistent")
    return factors[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate buffers around geometries.")
    parser.add_argument("input", help="Input file path")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--layer", help="Layer name to read")
    parser.add_argument("--distance", type=float, help="Buffer distance in meters")
    parser.add_argument("--field", help="Field whose values are buffer distances in meters")
    parser.add_argument(
        "--distances",
        help="Comma-separated distances in meters for concentric rings: one feature per input row and distance, "
        "with the distance in buffer_m and the ring number (1 = innermost) in ring",
    )
    parser.add_argument("--analysis-crs", help="Projected CRS used for buffer calculations")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output GeoPackage")
    parser.add_argument("--segments", type=int, default=16, help="Circle approximation segments")
    parser.add_argument("--dissolve", action="store_true", help="Dissolve overlapping buffers")
    parser.add_argument("--cap-style", choices=CAP_STYLES, default="round")
    parser.add_argument("--join-style", choices=JOIN_STYLES, default="round")
    args = parser.parse_args()

    given = [name for name, value in (("--distance", args.distance), ("--field", args.field), ("--distances", args.distances)) if value is not None]
    if not given:
        parser.error("--distance, --field or --distances is required")
    if len(given) > 1:
        parser.error("give only one of --distance, --field and --distances")
    rings = None
    if args.distances is not None:
        try:
            rings = [float(part) for part in args.distances.split(",") if part.strip()]
        except ValueError:
            parser.error("--distances must be comma-separated numbers")
        if not rings or any(not math.isfinite(value) or value <= 0 for value in rings):
            parser.error("--distances must be positive finite numbers")
        if len(set(rings)) != len(rings):
            parser.error("--distances must not repeat a distance")
        rings = sorted(rings)

    output = args.output or str(Path(args.input).stem) + "_buffer.gpkg"
    try:
        gdf = read_vector(args.input, args.layer)
    except Exception as exc:
        sys.exit(f"Error reading input: {exc}")
    original_crs = gdf.crs
    if rings is not None:
        clash = [name for name in ("buffer_m", "ring") if name in gdf.columns]
        if clash:
            parser.error("Input already has field(s) {}; rename them before buffering rings".format(", ".join(clash)))
        # One row per input feature and ring, in input order, innermost ring first.
        gdf = gdf.loc[gdf.index.repeat(len(rings))].reset_index(drop=True)
        gdf["buffer_m"] = rings * (len(gdf) // len(rings))
        gdf["ring"] = list(range(1, len(rings) + 1)) * (len(gdf) // len(rings))

    try:
        gdf, meters_per_unit = analysis_frame(gdf, args.analysis_crs)
        analysis_crs = gdf.crs
        method = analysis_method(original_crs, args.analysis_crs, used=analysis_crs)
        coordinate_units_per_meter = 1 / meters_per_unit
        reprojected = CRS.from_user_input(gdf.crs) != CRS.from_user_input(original_crs)

        if args.field:
            if args.field not in gdf.columns:
                raise ValueError("Field does not exist: {}".format(args.field))
            distances_m = [float(value) for value in gdf[args.field]]
        elif rings is not None:
            distances_m = [float(value) for value in gdf["buffer_m"]]
        else:
            distances_m = [args.distance] * len(gdf)
        if any(value is None or not math.isfinite(value) for value in distances_m):
            raise ValueError("Buffer distances must be finite numbers")
        coordinate_distances = [value * coordinate_units_per_meter for value in distances_m]
        if any(not math.isfinite(value) for value in coordinate_distances):
            raise ValueError("Converted buffer distances are outside the supported numeric range")
        # Positional on purpose: geopandas 1.2 renamed this argument resolution -> quad_segs and warns on the old
        # name, while every earlier release rejects the new one.
        gdf["geometry"] = gdf.geometry.buffer(
            coordinate_distances,
            args.segments,
            cap_style=CAP_STYLES[args.cap_style],
            join_style=JOIN_STYLES[args.join_style],
        )
        if reprojected and args.analysis_crs is None:
            nonempty = gdf.loc[gdf.geometry.notna() & ~gdf.geometry.is_empty]
            if not nonempty.empty:
                try:
                    _estimate_utm_crs(nonempty)
                except ValueError as exc:
                    raise ValueError(f"After buffering: {exc}. The input itself fits {analysis_crs.to_string()}: "
                                     f"pass --analysis-crs {analysis_crs.to_string()} to keep using it, "
                                     "or name another projected CRS") from None
    except (TypeError, ValueError, OverflowError) as exc:
        parser.error(str(exc))
    except Exception as exc:
        parser.error("Cannot reliably interpret or apply buffer distances: {}".format(exc))

    if args.dissolve:
        # Rings dissolve per distance: overlapping service areas of several centres merge ring by ring.
        gdf = gdf.dissolve(by=["ring", "buffer_m"], as_index=False)[["ring", "buffer_m", "geometry"]] if rings is not None else gdf.dissolve()

    if reprojected:
        try:
            gdf = reproject(gdf, original_crs, purpose='return')
            nonempty = gdf.loc[gdf.geometry.notna() & ~gdf.geometry.is_empty]
            if not nonempty.empty and not all(math.isfinite(value) for value in nonempty.total_bounds):
                parser.error("Reprojected geographic buffer contains non-finite coordinates")
        except Exception as exc:
            parser.error("Cannot reliably reproject geographic buffer to the input CRS: {}".format(exc))

    try:
        evidence = write_vector_atomic(
            gdf, output, overwrite=args.overwrite, protected_paths=[args.input]
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Written {evidence['features']} features to {output} in source CRS {gdf.crs}; analysis_crs={analysis_crs}; method={method}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
