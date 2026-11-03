"""Shared, fail-closed CRS and geometry handling for planar measurements.

``meters_per_unit`` is metres represented by one coordinate unit in the
returned projected frame. Areas must therefore be multiplied by its square.
"""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
from pyproj import CRS, Proj, Transformer

from _core import _uses_method, reproject

# A projected source is measured directly only while its linear scale stays this close to 1 over the layer.
# Regional projections stay inside 5% (polar stereographic down to about 57 degrees, national Albers);
# Mercator grows without bound and has no standard region, so it gets the tolerance of a good local grid.
_SCALE_TOLERANCE = 0.05
_MERCATOR_SCALE_TOLERANCE = 0.005


def _horizontal_meters_per_unit(crs: CRS) -> float:
    if not crs.is_projected:
        raise ValueError("Analysis CRS must be projected")
    axes = crs.axis_info
    if len(axes) < 2:
        raise ValueError("Analysis CRS must define two horizontal axes")
    first, second = axes[:2]
    names = [axis.unit_name for axis in (first, second)]
    factors = [axis.unit_conversion_factor for axis in (first, second)]
    if any(not name or name.strip().lower() in {"unknown", "undefined", "none", "unitless"} for name in names):
        raise ValueError("Cannot determine analysis CRS horizontal units")
    if any(factor is None or not math.isfinite(factor) or factor <= 0 for factor in factors):
        raise ValueError("Cannot determine analysis CRS horizontal units")
    if names[0] != names[1] or not math.isclose(factors[0], factors[1], rel_tol=1e-12):
        raise ValueError("Analysis CRS horizontal axes must use the same known linear unit")
    return float(factors[0])


def ground_scale_range(crs: CRS, bounds) -> tuple[float, float] | None:
    """Smallest and largest linear scale factor of a projected CRS inside bounds given in that CRS."""
    if not all(math.isfinite(float(value)) for value in bounds):
        return None
    x, y = np.meshgrid(np.linspace(bounds[0], bounds[2], 9), np.linspace(bounds[1], bounds[3], 9))
    try:
        longitude, latitude = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True).transform(x.ravel(), y.ravel())
        factors = Proj(crs).get_factors(longitude, latitude)
    except Exception:  # Not every CRS can be reduced to one PROJ operation; absence of a number is not a finding.
        return None
    scales = np.concatenate([factors.meridional_scale, factors.parallel_scale])
    scales = scales[np.isfinite(scales)]
    return (float(scales.min()), float(scales.max())) if len(scales) else None


def _excess_scale(crs: CRS, layers: list[gpd.GeoDataFrame]) -> tuple[float, float] | None:
    """The scale range when it rules this CRS out for measuring these layers, else None."""
    tolerance = _MERCATOR_SCALE_TOLERANCE if _uses_method(crs, ("mercator",)) else _SCALE_TOLERANCE
    for layer in layers:
        bounds = layer.total_bounds
        if not CRS.from_user_input(layer.crs).equals(crs) and all(math.isfinite(float(value)) for value in bounds):
            bounds = Transformer.from_crs(layer.crs, crs, always_xy=True).transform_bounds(*bounds, densify_pts=9)
        found = ground_scale_range(crs, bounds)
        if found is not None and max(found[1] - 1, 1 - found[0]) > tolerance:
            return found
    return None


def _local_utm_instead(source: CRS, gdf: gpd.GeoDataFrame, scale: tuple[float, float]) -> CRS:
    try:
        return _local_utm_crs(gdf)
    except ValueError as exc:
        name = ":".join(source.to_authority() or ()) or source.name
        raise ValueError(f"{name} changes distances by a factor of {scale[0]:.3f} to {scale[1]:.3f} in this extent, so it is not "
                         f"used for measuring, and a local UTM does not fit either: {exc}. Pass --analysis-crs with a projected "
                         "CRS suited to the whole extent") from None


def _local_utm_crs(gdf: gpd.GeoDataFrame) -> CRS:
    """Choose UTM only when the complete geographic bounds fit one safe zone."""
    geographic = gdf.to_crs("EPSG:4326")
    bounds = geographic.total_bounds
    if len(bounds) != 4 or not all(math.isfinite(float(value)) for value in bounds):
        raise ValueError("Geographic extent is empty or non-finite; cannot select local UTM")
    minx, miny, maxx, maxy = map(float, bounds)
    if minx < -180 or maxx > 180 or miny < -80 or maxy > 84:
        raise ValueError("Geographic extent is outside the reliable UTM latitude/longitude domain")
    if maxx - minx > 6 or maxy - miny > 8:
        raise ValueError("Geographic extent is too broad for reliable local UTM analysis")

    def zone(longitude: float) -> int:
        return min(60, max(1, int((longitude + 180) // 6) + 1))

    if zone(minx) != zone(maxx):
        raise ValueError("Geographic extent crosses UTM zones; specify --analysis-crs")
    center_lon, center_lat = (minx + maxx) / 2, (miny + maxy) / 2
    zone_number = zone(center_lon)
    epsg = (32600 if center_lat >= 0 else 32700) + zone_number
    return CRS.from_epsg(epsg)


def validate_measurement_geometries(gdf: gpd.GeoDataFrame) -> None:
    """Reject empty, null, or invalid geometry before planar measurements.

    Callers which need to retain topology diagnostics can inspect in source CRS,
    exclude bad rows explicitly, and pass only the valid subset here.
    """
    if gdf.empty:
        raise ValueError("Cannot measure an empty layer")
    geometry = gdf.geometry
    null_positions = [i for i, value in enumerate(geometry) if value is None]
    empty_positions = [i for i, value in enumerate(geometry) if value is not None and value.is_empty]
    invalid_positions = [i for i, value in enumerate(geometry) if value is not None and not value.is_empty and not value.is_valid]
    if null_positions or empty_positions or invalid_positions:
        details = []
        if null_positions:
            details.append("null=" + ",".join(map(str, null_positions[:10])))
        if empty_positions:
            details.append("empty=" + ",".join(map(str, empty_positions[:10])))
        if invalid_positions:
            details.append("invalid=" + ",".join(map(str, invalid_positions[:10])))
        raise ValueError("Measurement input has unusable geometry positions (" + "; ".join(details) + ")")


def analysis_frame(
    gdf: gpd.GeoDataFrame, analysis_crs: str | int | CRS | None = None
) -> tuple[gpd.GeoDataFrame, float]:
    """Return a valid layer in a projected analysis CRS and metres per unit.

    A projected source CRS is the default analysis CRS unless its scale drifts
    too far over the layer, as Web Mercator's does. Those and geographic sources
    get an automatically selected local UTM only if their complete bounds pass
    conservative zone/extent limits; wider work requires an explicit CRS.
    The returned geometry coordinates are never silently interpreted as metres.
    """
    if gdf.crs is None:
        raise ValueError("Input layer has no CRS; measurement requires a known CRS")
    validate_measurement_geometries(gdf)
    source_crs = CRS.from_user_input(gdf.crs)
    # Validate source projected axes too: a known target CRS does not repair
    # coordinates whose source units are unknown or internally inconsistent.
    if source_crs.is_projected:
        _horizontal_meters_per_unit(source_crs)
    if analysis_crs is not None:
        target_crs = CRS.from_user_input(analysis_crs)
        if not target_crs.is_projected:
            raise ValueError("--analysis-crs must be a projected CRS")
    elif source_crs.is_projected:
        scale = _excess_scale(source_crs, [gdf])
        target_crs = source_crs if scale is None else _local_utm_instead(source_crs, gdf, scale)
    elif source_crs.is_geographic:
        target_crs = _local_utm_crs(gdf)
    else:
        raise ValueError("Input CRS is neither geographic nor projected; specify a projected --analysis-crs")

    meters_per_unit = _horizontal_meters_per_unit(target_crs)
    projected = reproject(gdf, target_crs, purpose='measure')
    validate_measurement_geometries(projected)
    return projected, meters_per_unit


def analysis_frames(
    layers: list[gpd.GeoDataFrame], analysis_crs: str | int | CRS | None = None
) -> tuple[list[gpd.GeoDataFrame], float]:
    """Project related layers into one CRS, checking their combined local extent."""
    if not layers:
        raise ValueError("At least one layer is required for analysis")
    if any(layer.crs is None for layer in layers):
        raise ValueError("All input layers must have known CRS")
    first_crs = CRS.from_user_input(layers[0].crs)
    if analysis_crs is not None:
        target = CRS.from_user_input(analysis_crs)
        if not target.is_projected:
            raise ValueError("--analysis-crs must be a projected CRS")
    elif first_crs.is_projected and _excess_scale(first_crs, layers) is None:
        target = first_crs
    elif first_crs.is_geographic or first_crs.is_projected:
        all_geographic = []
        for layer in layers:
            validate_measurement_geometries(layer)
            all_geographic.extend(layer.to_crs("EPSG:4326").geometry.tolist())
        combined = gpd.GeoDataFrame(geometry=all_geographic, crs="EPSG:4326")
        combined_projected, _ = analysis_frame(combined)
        target = CRS.from_user_input(combined_projected.crs)
    else:
        raise ValueError("Input CRS is neither geographic nor projected; specify a projected --analysis-crs")
    frames: list[gpd.GeoDataFrame] = []
    factors: list[float] = []
    for layer in layers:
        projected, factor = analysis_frame(layer, target)
        frames.append(projected)
        factors.append(factor)
    first_factor = factors[0]
    if any(not math.isclose(first_factor, factor, rel_tol=1e-12) for factor in factors[1:]):
        raise ValueError("Analysis layers have inconsistent horizontal units")
    return frames, first_factor


def analysis_method(source: gpd.GeoDataFrame | str | int | CRS | object, analysis_crs: str | int | CRS | None = None, *, used: str | int | CRS | None = None) -> str:
    """Describe how the analysis CRS was selected and who owns its scope; ``used`` is the CRS the analysis ran in."""
    if analysis_crs is not None:
        return "caller-specified projected CRS; caller is responsible for study-area suitability"
    source_crs = source.crs if hasattr(source, "crs") else source
    if source_crs is not None and CRS.from_user_input(source_crs).is_projected:
        if used is not None and not CRS.from_user_input(used).equals(CRS.from_user_input(source_crs)):
            return "automatic local UTM because the source projected CRS changes distances too much over this extent"
        return "source projected CRS; suitability is assumed from input metadata"
    return "automatic local UTM only within one zone, <=6 degree longitude and <=8 degree latitude extent, and UTM latitude limits"
