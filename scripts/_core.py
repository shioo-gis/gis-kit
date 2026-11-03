"""Helpers shared by the vector tools, kept apart from any one tool's CLI."""
from __future__ import annotations

import datetime
from functools import lru_cache
import importlib.metadata
import math
from pathlib import Path
import platform
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS, Transformer
import shapely

from _budget import check_vector
from _delivery import fact, write_json


def read(path, layer=None, encoding='utf-8', sheet=0):
    suffix = Path(path).suffix.lower()
    if suffix == '.csv':
        import csv
        with open(path,encoding=encoding,newline='') as stream: header=next(csv.reader(stream),[])
        if len(header)!=len(set(header)): raise ValueError('Duplicate CSV headers')
        return pd.read_csv(path,dtype=str,keep_default_na=False,encoding=encoding)
    if suffix in ('.xlsx','.xls'): return pd.read_excel(path,dtype=str,keep_default_na=False,sheet_name=sheet)
    check_vector(path,layer)
    return gpd.read_file(path,layer=layer,engine='pyogrio').reset_index(drop=True)


def environment():
    return {'python':platform.python_version(), 'python_executable':sys.executable,
            'capabilities':{'coverage_simplify':callable(getattr(shapely,'coverage_simplify',None)) and shapely.geos_version >= (3,12,0)},
            'packages':{p:importlib.metadata.version(p) for p in ('geopandas','shapely','pyogrio','pyproj','pandas','numpy')},
            'geos':shapely.geos_version_string,'gdal':pyogrio.__gdal_version_string__,
            'drivers':pyogrio.list_drivers(), 'optional':{p:bool(__import__('importlib.util',fromlist=['find_spec']).find_spec(p)) for p in ('openpyxl','rasterio','qgis','pysal')}}


def clean(value):
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    if value is None or value is pd.NA: return None
    if isinstance(value,(datetime.date,datetime.datetime)): return value.isoformat()
    if hasattr(value,'item'): value=value.item()
    if isinstance(value,float) and not __import__('math').isfinite(value): return None
    return value


# Coordinates of these projections wrap at the meridian opposite their centre and cannot hold a ring around a pole.
_WRAPPING_METHODS = ('mercator', 'cylindrical', 'equirectangular', 'plate carr', 'miller', 'gall', 'robinson', 'mollweide', 'sinusoidal',
                     'eckert', 'winkel', 'natural earth', 'equal earth', 'hammer', 'aitoff', 'albers', 'conic', 'polyconic')
_CYLINDRICAL_METHODS = ('mercator', 'cylindrical', 'equirectangular', 'plate carr', 'miller', 'gall')
# Geographic edges longer than this are curved in other projections; shorter ones are left untouched.
_DENSIFY_DEGREES = 1.0
_COORDINATE_LIMIT = 1e10
_REPROJECT_HINTS = {
    'publish': 'Keep the source CRS or pick a target that has no seam there, such as a polar or local projection; gis-kit does not split features at a seam',
    'align': 'Convert the other input to this CRS first (gis.py convert IN --crs CRS --output OUT), so the comparison happens where these features stay whole',
    'measure': 'Choose an analysis CRS that has no seam there, such as a polar or local projection',
    'return': 'Convert the input to the analysis CRS first (gis.py convert IN --crs CRS --output OUT) and run the tool there, so the result never has to cross the seam',
}


def _horizontal_crs(crs: CRS) -> CRS:
    while crs.is_bound or crs.is_compound:
        crs = crs.source_crs if crs.is_bound else crs.sub_crs_list[0]
    return crs


def _projection_method(crs: CRS) -> str:
    operation = _horizontal_crs(crs).coordinate_operation
    return operation.method_name.lower() if operation is not None else ''


def _uses_method(crs: CRS, methods: tuple[str, ...]) -> bool:
    name = _projection_method(crs)
    return 'transverse' not in name and 'oblique' not in name and any(word in name for word in methods)


def _seam_longitude(crs: CRS) -> float | None:
    """Longitude at which coordinates of this CRS wrap around; None when there is no such meridian."""
    horizontal = _horizontal_crs(crs)
    if horizontal.is_geographic:
        return 180.0
    if not _uses_method(horizontal, _WRAPPING_METHODS):
        return None
    centre = next((math.degrees(p.value*p.unit_conversion_factor) for p in horizontal.coordinate_operation.params
                   if p.name.lower().startswith('longitude of')), 0.0)
    seam = (centre + 360) % 360 - 180
    return 180.0 if seam == -180 else seam


@lru_cache(maxsize=16)
def _singularities(source_wkt: str, target_wkt: str):
    """The target's seam and poles, drawn in source coordinates: what a point-by-point transform cannot carry across."""
    source, target = CRS.from_wkt(source_wkt), CRS.from_wkt(target_wkt)
    seam_longitude = _seam_longitude(target)
    if seam_longitude is None:
        return None, None
    to_source = Transformer.from_crs(_horizontal_crs(target).geodetic_crs, source, always_xy=True)
    latitudes = np.linspace(-90, 90, 721)
    runs = []
    for longitude in (180.0, -180.0) if abs(seam_longitude) == 180 else (seam_longitude,):
        x, y = to_source.transform(np.full(latitudes.shape, longitude), latitudes)
        finite = np.isfinite(x) & np.isfinite(y)
        for part in np.split(np.arange(len(x)), np.flatnonzero(np.diff(finite)) + 1):
            if finite[part[0]] and len(part) > 1:
                runs.append(np.column_stack([x[part], y[part]]))
    seam = shapely.multilinestrings([shapely.linestrings(run) for run in runs]) if runs else None
    if _horizontal_crs(target).is_geographic and source.is_geographic:
        poles = None
    elif source.is_geographic:
        poles = shapely.multilinestrings([[(-180, -90), (180, -90)], [(-180, 90), (180, 90)]])
    else:
        x, y = to_source.transform([0.0, 0.0], [-90.0, 90.0])
        points = [(a, b) for a, b in zip(x, y) if math.isfinite(a) and math.isfinite(b)]
        poles = shapely.multipoints(points) if points else None
    for shape in (seam, poles):
        if shape is not None:
            shapely.prepare(shape)
    return seam, poles


def _positions(mask) -> str:
    found = np.flatnonzero(mask)
    return ','.join(map(str, found[:10].tolist())) + (f' and {len(found)-10} more' if len(found) > 10 else '')


def _refuse_singular(geometries, source: CRS, target: CRS, purpose: str) -> None:
    """Features holding a pole or crossing the target's seam flip without any error, and can stay valid while doing so."""
    seam, poles = _singularities(source.to_wkt(), target.to_wkt())
    if seam is None and poles is None:
        return
    usable = ~(shapely.is_missing(geometries) | shapely.is_empty(geometries))
    extended = usable & (shapely.get_dimensions(geometries) > 0)
    if not extended.any():
        return
    # Only the checks repair: this function never changes what it refuses or passes through.
    whole = np.where(shapely.is_valid(geometries) | ~extended, geometries, shapely.make_valid(geometries))
    pole_hits = shapely.intersects(whole, poles) & extended if poles is not None else np.zeros(len(whole), bool)
    seam_hits = np.zeros(len(whole), bool)
    if seam is not None:
        near = np.flatnonzero(shapely.intersects(whole, seam) & extended)
        seam_hits[near] = shapely.relate_pattern(whole[near], seam, 'T********')
        seam_hits &= ~pole_hits  # a ring around a pole crosses the seam too; name the cause once
    if not (pole_hits.any() or seam_hits.any()):
        return
    problems = ([f'row position(s) {_positions(pole_hits)} touch a pole'] if pole_hits.any() else []) \
        + ([f'row position(s) {_positions(seam_hits)} cross the {_seam_longitude(target):g} degree meridian where the target coordinates wrap'] if seam_hits.any() else [])
    raise ValueError(f'Cannot reproject from {_crs_label(source)} to {_crs_label(target)}: {"; ".join(problems)}. '
                     f'The transform would flip these features without an error. {_REPROJECT_HINTS.get(purpose, _REPROJECT_HINTS["publish"])}.')


def _crs_label(crs: CRS) -> str:
    authority = crs.to_authority(min_confidence=70)
    return ':'.join(authority) if authority else crs.name


def reproject(frame, target, *, purpose: str, facts: list | None = None):
    """Move a GeoDataFrame or GeoSeries to ``target``, refusing the cases where ``to_crs`` is silently wrong.

    Features that hold a pole or cross the target's seam are refused (no splitting). Long edges of geographic
    sources are densified where the target bends them. Non-finite results are refused. ``purpose`` is one of
    publish, align, measure, return and selects the way out named in the error. Geometry changes are appended to
    ``facts``, or written to stderr by tools that keep no record.
    """
    if frame.crs is None:
        raise ValueError('Cannot reproject a layer without a CRS; assign the correct CRS first')
    source, goal = CRS.from_user_input(frame.crs), CRS.from_user_input(target)
    if source.equals(goal):
        return frame.to_crs(goal)
    before = frame.geometry.to_numpy()
    _refuse_singular(before, source, goal, purpose)
    notes = []
    work = frame
    if _horizontal_crs(source).is_geographic and _horizontal_crs(goal).is_projected and not _uses_method(goal, _CYLINDRICAL_METHODS):
        dense = shapely.segmentize(before, _DENSIFY_DEGREES)
        added = int(shapely.get_num_coordinates(dense).sum() - shapely.get_num_coordinates(before).sum())
        if added:
            work = _with_geometry(frame, dense, source)
            notes.append(fact('edges_densified', 'info', f'Added {added} vertices to edges longer than {_DENSIFY_DEGREES:g} degree before reprojecting to {_crs_label(goal)}.', added_vertices=added))
    result = work.to_crs(goal)
    after = result.geometry.to_numpy()
    coordinates, owners = shapely.get_coordinates(after, return_index=True)
    # Near the far pole of a stereographic target PROJ returns 1e23 rather than infinity.
    bad = np.unique(owners[~(np.abs(coordinates) < _COORDINATE_LIMIT).all(axis=1)])
    if len(bad):
        mask = np.zeros(len(after), bool)
        mask[bad] = True
        raise ValueError(f'Cannot reproject from {_crs_label(source)} to {_crs_label(goal)}: row position(s) {_positions(mask)} '
                         f'fall outside the domain of the target CRS and have no usable coordinates. {_REPROJECT_HINTS.get(purpose, _REPROJECT_HINTS["publish"])}.')
    broken = ~shapely.is_missing(after) & ~shapely.is_valid(after) & shapely.is_valid(before)
    if broken.any():
        # A seam or pole cannot be the cause here: those were refused above. What remains are rings collapsing onto themselves.
        fixed = shapely.make_valid(after[broken], method='structure', keep_collapsed=False)
        if (shapely.get_dimensions(fixed) != shapely.get_dimensions(after[broken])).any() or shapely.is_empty(fixed).any():
            raise ValueError(f'Reprojecting to {_crs_label(goal)} made row position(s) {_positions(broken)} invalid and they could not be repaired without changing their type')
        after = after.copy()
        after[broken] = fixed
        result = _with_geometry(result, after, goal)
        notes.append(fact('geometry_repaired', 'warning', f'Repaired row position(s) {_positions(broken)} that became invalid in {_crs_label(goal)}.', count=int(broken.sum())))
    if facts is not None:
        facts.extend(notes)
    else:
        for note in notes:
            sys.stderr.write(f'{note["severity"].upper()}: {note["message"]}\n')
    return result


def record_facts(facts) -> dict:
    """Findings as record fields: warnings need the reader's attention, facts only state a property of the data."""
    return {'warnings': [item for item in facts if item['severity'] != 'info'], 'facts': [item for item in facts if item['severity'] == 'info']}


def report_warnings(facts) -> None:
    for item in facts:
        if item['severity'] != 'info':
            sys.stderr.write(f'WARNING: {item["message"]}\n')


def write_record(directory, fields: dict, *, operation: str, parameters: dict, inputs: list, facts=()) -> None:
    """record.json with the fields every tool shares, around what the tool adds; recipes read the tool's status and artifact as they are."""
    write_json(Path(directory)/'record.json', clean({**fields, 'schema_version': 1, 'operation': operation, 'parameters': parameters,
                                                     'inputs': inputs, **record_facts(facts)}))


def _with_geometry(frame, geometries, crs):
    if isinstance(frame, gpd.GeoDataFrame):
        return frame.set_geometry(gpd.GeoSeries(geometries, index=frame.index, crs=crs).rename(frame.geometry.name))
    return gpd.GeoSeries(geometries, index=frame.index, crs=crs, name=frame.name)
