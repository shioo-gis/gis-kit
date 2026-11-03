"""Daily vector operations. Parameters and result records are versioned by daily.py."""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import math
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point, LineString, Polygon, box
from shapely.ops import split, substring, nearest_points, voronoi_diagram

from _core import reproject
from _metric import analysis_frame, analysis_frames, validate_measurement_geometries


def stable(frame, key):
    if key not in frame or frame[key].isna().any() or frame[key].astype(str).eq('').any():
        raise ValueError(f"Missing/empty source ID: {key}")
    if frame[key].astype(str).duplicated().any():
        raise ValueError(f"Duplicate source ID: {key}")
    return frame


def metric(frame, crs=None):
    result, factor = analysis_frame(frame, crs)
    if result.geometry.has_z.any():
        raise ValueError("Z/M operations are unsupported; explicitly prepare 2D input")
    return result, factor


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite value")
    return result


def coordinate(value):
    text = str(value).strip().upper()
    try:
        return number(text)
    except ValueError:
        match = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*[°:]\s*(\d+(?:\.\d+)?)\s*[\'′:]\s*(\d+(?:\.\d+)?)\s*["″]?\s*([NSEW]?)', text)
        if not match:
            raise ValueError("Expected decimal degrees or D°M'S\" hemisphere")
        deg, minute, second = map(float, match.groups()[:3])
        hemisphere = match.group(4)
        if not 0 <= minute < 60 or not 0 <= second < 60 or (text.startswith('-') and hemisphere in 'NE' and hemisphere):
            raise ValueError("Invalid DMS")
        sign = -1 if text.startswith('-') or hemisphere in ('S', 'W') else 1
        return sign * (abs(deg) + minute / 60 + second / 3600)


def normalize(frame, p):
    """Keep original values; failed rows remain in a separate auditable table."""
    if not isinstance(frame,gpd.GeoDataFrame) and 'geometry' in frame.columns:
        raise ValueError('Reserved geometry column collision; rename the tabular column before import')
    original = frame.copy()
    out = frame.copy()
    failures = []
    bad = set()
    def fail(i, field, error):
        bad.add(i)
        failures.append({'row': int(i), 'field': field, 'reason': str(error),
                         'original': {str(k): str(v) for k, v in original.loc[i].items()}})
    for target, spec in p.get('fields', {}).items():
        if target in out.columns:
            raise ValueError(f"Field collision: {target}; preserve original and use a new name")
        source = spec['source']
        if source not in out:
            raise ValueError(f"Missing source field: {source}")
        values = []
        for i, value in out[source].items():
            try:
                if pd.isna(value) or value == '':
                    converted = None
                else:
                    converted = value
                    if 'codes' in spec:
                        converted = spec['codes'][str(value)]
                    kind = spec.get('type', 'string')
                    if kind == 'string': converted = str(converted)
                    elif kind == 'float': converted = number(converted)
                    elif kind == 'int':
                        x = number(converted)
                        if x != int(x): raise ValueError('Non-integral value')
                        converted = int(x)
                    elif kind == 'date': converted = dt.datetime.strptime(str(converted), spec['format']).date().isoformat()
                    elif kind == 'bool':
                        if str(converted).lower() not in ('true', 'false', '1', '0'): raise ValueError('Invalid boolean')
                        converted = str(converted).lower() in ('true', '1')
                    else: raise ValueError(f'Unknown conversion: {kind}')
                values.append(converted)
            except (ValueError, TypeError, KeyError) as error:
                values.append(None); fail(i, source, error)
        dtype = {'int': 'Int64', 'float': 'Float64', 'bool': 'boolean'}.get(spec.get('type'), 'string')
        out[target] = pd.Series(values, index=out.index, dtype=dtype)
    if not isinstance(out, gpd.GeoDataFrame):
        geometries = []
        if not p.get('crs'): raise ValueError('Tabular geometry requires explicit crs')
        from pyproj import CRS
        geographic = CRS(p['crs']).is_geographic
        for i, row in out.iterrows():
            try:
                if 'wkt' in p:
                    geom = shapely.from_wkt(row[p['wkt']])
                else:
                    x, y = coordinate(row[p['x']]), coordinate(row[p['y']])
                    if geographic and (abs(x) > 180 or abs(y) > 90):
                        raise ValueError('Illegal coordinates; possible XY reversal, not corrected')
                    geom = Point(x, y)
                if geom is None or geom.is_empty or not geom.is_valid or not np.isfinite(geom.bounds).all():
                    raise ValueError('Invalid geometry')
                if geographic and (geom.bounds[0] < -180 or geom.bounds[2] > 180 or geom.bounds[1] < -90 or geom.bounds[3] > 90):
                    raise ValueError('Illegal geographic WKT extent; not corrected')
                if geom.has_z: raise ValueError('Explicit 2D geometry required')
                geometries.append(geom)
            except (ValueError, TypeError, shapely.errors.GEOSException) as error:
                geometries.append(None); fail(i, 'geometry', error)
        out = gpd.GeoDataFrame(out, geometry=geometries, crs=p['crs'])
    result = out.drop(index=list(bad)).copy()
    key = p['id']
    if key not in result: raise ValueError('Missing ID field')
    duplicates = result[key].isna() | result[key].astype(str).eq('') | result[key].astype(str).duplicated(keep=False)
    for i in result.index[duplicates]: fail(i, key, 'Missing or duplicate key')
    result = result.drop(index=result.index[duplicates])
    return result, {'failures': failures, 'input_rows': len(frame), 'accepted_rows': len(result), 'rejected_rows': len(bad)}


def update(left, right, p):
    key = p['id']; stable(left, key); stable(right, key)
    if left.crs is None or right.crs is None: raise ValueError('Missing CRS')
    right = reproject(right, left.crs, purpose='align')
    fields = p.get('fields', [])
    if key in fields or left.geometry.name in fields: raise ValueError('ID/geometry cannot be attribute update fields')
    for f in fields:
        if f not in left or f not in right: raise ValueError(f'Missing field: {f}')
    out = left.copy().set_index(key, drop=False)
    # IDs retain their type; no accidental numeric/string coalescing.
    changes = []
    for _, row in right.iterrows():
        identity = row[key]
        if identity not in out.index:
            if p.get('append', False):
                out = pd.concat([out, gpd.GeoDataFrame([row], geometry=right.geometry.name, crs=left.crs).set_index(key, drop=False)])
            changes.append({'id': identity, 'kind': 'new', 'adopted': p.get('append', False)})
            continue
        for field in fields:
            old, new = out.at[identity, field], row[field]
            equal = (pd.isna(old) and pd.isna(new)) or (not pd.isna(old) and not pd.isna(new) and old == new)
            if not equal:
                changes.append({'id': identity, 'kind': 'attribute', 'field': field, 'before': old, 'after': new})
                out.at[identity, field] = new
        if not out.at[identity, left.geometry.name].equals(row.geometry):
            changes.append({'id': identity, 'kind': 'geometry', 'adopted': p.get('geometry_update', False), 'before_wkt': out.at[identity, left.geometry.name].wkt, 'after_wkt': row.geometry.wkt})
            if p.get('geometry_update', False): out.at[identity, left.geometry.name] = row.geometry
    return gpd.GeoDataFrame(out.reset_index(drop=True), geometry=left.geometry.name, crs=left.crs), {
        'changes': changes, 'unmatched_left': [v for v in left[key] if v not in set(right[key])],
        'matched': int(left[key].isin(right[key]).sum()), 'duplicate_policy': 'reject before publication'}


def relations(left, right, p):
    stable(left, p['id']); stable(right, p.get('right_id', p['id']))
    (a, b), factor = analysis_frames([left, right], p.get('analysis_crs'))
    aid, bid = p['id'], p.get('right_id', p['id'])
    mode = p.get('method', 'nearest'); rows = []; unmatched = []
    limit = int(p.get('max_pairs', 100000))
    if limit < 1: raise ValueError('max_pairs must be positive')
    radius = p.get('radius_m')
    if radius is not None and number(radius) < 0: raise ValueError('Negative radius')
    k = int(p.get('k', 1))
    if k < 1: raise ValueError('k must be positive')
    bands = p.get('bands_m', [])
    if mode == 'bands' and (not bands or bands != sorted(set(bands)) or bands[0] <= 0): raise ValueError('bands_m must increase above zero')
    if mode not in ('nearest', 'adjacency', 'bands'): raise ValueError('Unknown relationship method')
    if mode=='adjacency' and (not a.geom_type.isin(['Polygon','MultiPolygon']).all() or not b.geom_type.isin(['Polygon','MultiPolygon']).all()): raise ValueError('Adjacency requires polygons')
    for _, source in a.iterrows():
        geom = source.geometry
        if mode == 'adjacency': candidates = b.sindex.query(geom, predicate='intersects')
        elif radius is not None or mode == 'bands':
            distance = (max(bands) if mode == 'bands' else radius) / factor
            candidates = b.sindex.query(box(geom.bounds[0]-distance, geom.bounds[1]-distance, geom.bounds[2]+distance, geom.bounds[3]+distance))
        else:
            # One source at a time; bounded memory, never materialize an N x M matrix.
            candidates = range(len(b))
        matches = []
        for j in candidates:
            target = b.iloc[j]
            if p.get('exclude_self') and source[aid] == target[bid]: continue
            category = p.get('category')
            if category and (pd.isna(source[category]) or pd.isna(target[category]) or source[category] != target[category]): continue
            d = geom.distance(target.geometry) * factor
            if radius is not None and d > radius: continue
            if mode == 'bands' and d > max(bands): continue
            matches.append((d, str(target[bid]), int(j)))
        matches.sort()
        if mode == 'nearest': matches = matches[:k]  # Equal distance: stable ID ascending, exactly K maximum.
        if not matches: unmatched.append(source[aid])
        for rank, (distance, _, j) in enumerate(matches, 1):
            target = b.iloc[j]; near_a, near_b = nearest_points(geom, target.geometry)
            base = {'source_id': source[aid], 'target_id': target[bid], 'distance_m': distance, 'rank': rank,
                    'near_source_x': near_a.x, 'near_source_y': near_a.y, 'near_target_x': near_b.x, 'near_target_y': near_b.y,
                    'connector_wkt': LineString([near_a, near_b]).wkt}
            if mode == 'adjacency':
                shared = geom.boundary.intersection(target.geometry.boundary).length * factor
                overlap = geom.intersection(target.geometry).area * factor**2
                base.update(shared_m=shared, overlap_m2=overlap,
                            source_ratio=overlap/(geom.area*factor**2) if geom.area else None,
                            target_ratio=overlap/(target.geometry.area*factor**2) if target.geometry.area else None,
                            contact='overlap' if overlap > 0 else ('edge' if shared > 0 else 'point'))
            if mode == 'bands':
                for index, upper in enumerate(bands):
                    if distance <= upper and (p.get('cumulative', False) or index == 0 or distance > bands[index-1]):
                        rows.append(dict(base, band_max_m=upper))
            else: rows.append(base)
            if len(rows) > limit: raise ValueError('Relationship output exceeds max_pairs')
    counts = Counter((r['source_id'],r.get('band_max_m')) for r in rows)
    band_counts = [{'source_id':identity, 'band_max_m':upper, 'count':counts[(identity,upper)]} for identity in a[aid] for upper in bands] if mode=='bands' else []
    return pd.DataFrame(rows, columns=list(rows[0]) if rows else ['source_id', 'target_id', 'distance_m', 'rank']), {
        'band_counts':band_counts,
        'unmatched': unmatched, 'analysis_crs': a.crs.to_string(), 'method': mode, 'units': 'm,m2',
        'tie_policy': 'distance then string stable ID; exactly K maximum'}


def overlay(left, right, p):
    stable(left, p['id']); stable(right, p.get('right_id', p['id']))
    (a, b), factor = analysis_frames([left, right], p.get('analysis_crs'))
    if not a.geom_type.isin(['Polygon', 'MultiPolygon']).all() or not b.geom_type.isin(['Polygon', 'MultiPolygon']).all():
        raise ValueError('Overlay first release requires polygon layers')
    # Prefix every source attribute; collisions and provenance are independent of backend suffixes.
    a = a.rename(columns={c: 'left_'+c for c in a.columns if c != a.geometry.name})
    b = b.rename(columns={c: 'right_'+c for c in b.columns if c != b.geometry.name})
    method = p.get('method', 'intersection')
    result = gpd.overlay(a, b, how=method, keep_geom_type=False, make_valid=False)
    result['area_m2'] = result.geometry.area * factor**2
    right_union = b.geometry.union_all()
    report = {'method': method, 'analysis_crs': a.crs.to_string(), 'units': 'm2',
              'input_area_m2': float(a.geometry.area.sum()*factor**2),
              'output_area_m2': float(result.area_m2.sum()), 'dimension_policy': 'retain lower-dimensional intersections',
              'empty_source_ids': [row['left_'+p['id']] for _, row in a.iterrows() if row.geometry.difference(right_union).is_empty],
              'semantics': 'global overlay, not tiled',
              'source_coverage': [{'source_id': row['left_'+p['id']], 'overlap_m2': row.geometry.intersection(right_union).area*factor**2, 'outside_m2': row.geometry.difference(right_union).area*factor**2} for _,row in a.iterrows()]}
    return result, report


def allocate(left, right, p):
    if p.get('quantity_type') != 'total' or p.get('assumption') != 'uniform':
        raise ValueError('Area allocation requires quantity_type=total and assumption=uniform; rates/classes cannot be summed')
    stable(left, p['id']); stable(right, p.get('right_id', p['id']))
    (a, b), factor = analysis_frames([left, right], p.get('analysis_crs'))
    if not a.geom_type.isin(['Polygon', 'MultiPolygon']).all() or not b.geom_type.isin(['Polygon', 'MultiPolygon']).all(): raise ValueError('Allocation requires polygons')
    # Any positive double coverage fails; touching boundaries are allowed.
    for i, geom in enumerate(b.geometry):
        for j in b.sindex.query(geom, predicate='intersects'):
            if j > i and geom.intersection(b.geometry.iloc[j]).area > 0:
                raise ValueError('Target coverage conflict: overlapping allocation zones')
    table = []; balances = []
    for _, row in a.iterrows():
        total = number(row[p['value']]); assigned = 0.; candidates = []
        if row.geometry.area <= 0: raise ValueError('Zero area allocation source')
        for j in b.sindex.query(row.geometry, predicate='intersects'):
            target = b.iloc[j]; area = row.geometry.intersection(target.geometry).area
            if area <= 0: continue
            amount = total * area / row.geometry.area; assigned += amount
            candidates.append((area, str(target[p.get('right_id', p['id'])]), target[p.get('right_id', p['id'])]))
            table.append({'source_id': row[p['id']], 'target_id': target[p.get('right_id', p['id'])], 'area_m2': area*factor**2, 'allocated': amount})
        candidates.sort(key=lambda x: (-x[0], x[1]))
        balances.append({'source_id': row[p['id']], 'source_total': total, 'allocated': assigned, 'unallocated': total-assigned,
                         'maximum_overlap_id': candidates[0][2] if candidates else None})
    return pd.DataFrame(table, columns=['source_id', 'target_id', 'area_m2', 'allocated']), {
        'balances': balances, 'assumption': 'uniform within each source polygon', 'coverage_conflicts': [], 'analysis_crs': a.crs.to_string()}


def rules(frame, other, p, fingerprints=None):
    key = p['id']
    if key not in frame: raise ValueError('Missing source ID field')
    errors = []
    allowed = {'unique', 'required', 'enum', 'compare', 'foreign_key', 'within', 'disjoint'}
    rule_ids = [r['id'] for r in p['rules']]
    if len(rule_ids) != len(set(rule_ids)): raise ValueError('Duplicate rule IDs')
    for rule in p['rules']:
        kind = rule['kind']; field = rule.get('field')
        if kind not in allowed: raise ValueError('Unsupported rule kind')
        if kind == 'unique': mask = frame[field].duplicated(keep=False) | frame[field].isna()
        elif kind == 'required': mask = frame[field].isna() | frame[field].astype(str).eq('')
        elif kind == 'enum': mask = ~frame[field].isin(rule['values'])
        elif kind == 'compare':
            operators = {'le': lambda a,b:a<=b, 'lt':lambda a,b:a<b, 'eq':lambda a,b:a==b, 'ge':lambda a,b:a>=b, 'gt':lambda a,b:a>b}
            rhs = frame[rule['other_field']] if 'other_field' in rule else rule['value']
            mask = (~operators[rule['op']](frame[field], rhs)).fillna(True) | frame[field].isna()
        elif kind == 'foreign_key':
            if other is None: raise ValueError('Rule requires right input')
            mask = ~frame[field].isin(other[rule['right_field']]) | frame[field].isna()
        else:
            if other is None or frame.crs is None or other.crs is None: raise ValueError('Spatial rule requires known CRS')
            validate_measurement_geometries(frame); validate_measurement_geometries(other)
            union = reproject(other, frame.crs, purpose='align').geometry.union_all()
            mask = ~frame.geometry.covered_by(union) if kind == 'within' else frame.geometry.intersects(union)
        for i in frame.index[mask]:
            geom = frame.loc[i].geometry
            errors.append({'source_id': frame.loc[i, key], 'row': int(i), 'rule_id': rule['id'],
                           'severity': rule.get('severity', 'error'), 'review': 'unreviewed',
                           'location_wkt': geom.representative_point().wkt if geom is not None and not geom.is_empty else None})
    rule_hash = hashlib.sha256(json.dumps(p['rules'],sort_keys=True,separators=(',', ':')).encode()).hexdigest()
    stale = []
    for approval in p.get('approvals', []):
        if (not fingerprints or approval.get('input_sha256') != fingerprints[0]
                or approval.get('right_sha256') != (fingerprints[1] if len(fingerprints)>1 else None)
                or approval.get('rules_sha256') != rule_hash or not approval.get('approved_by')):
            stale.append({'rule_id':approval.get('rule_id'),'source_id':approval.get('source_id'),'reason':'stale or incomplete approval'}); continue
        for error in errors:
            if (error['rule_id']==approval.get('rule_id') and error['source_id']==approval.get('source_id')
                    and error['row']==approval.get('row')):
                error['review']='approved_exception'
    return pd.DataFrame(errors, columns=['source_id', 'row', 'rule_id', 'severity', 'review', 'location_wkt']), {
        'passed': not any(e['review']!='approved_exception' for e in errors), 'auto_repair': False,
        'rules_sha256':rule_hash, 'stale_approvals':stale,
        'approval_policy':'explicit approved_by, rule hash, both input hashes, source ID and row must match'}



def geometry(frame, other, p):
    stable(frame, p['id'])
    work, factor = metric(frame, p.get('analysis_crs'))
    method = p['method']; output = []; diagnostics = []
    distance = p.get('distance_m', 1) / factor
    if not math.isfinite(distance) or distance <= 0: raise ValueError('distance_m must be finite and positive')
    def emit(row, geom, part, **attrs):
        values = {c: row[c] for c in work.columns if c != work.geometry.name}
        reserved = {'source_id', 'part_id', 'geometry'} | set(attrs)
        if reserved.intersection(values): raise ValueError('Reserved output field collision')
        values.update(source_id=row[p['id']], part_id=part, geometry=geom, **attrs)
        output.append(values)
        if len(output) > int(p.get('max_features', 100000)): raise ValueError('Geometry output exceeds max_features')
    if method == 'connect':
        if not work.geom_type.eq('Point').all(): raise ValueError('connect requires points')
        group = p['group']; order = p['order']
        if work[[group, order]].isna().any().any() or work.duplicated([group, order]).any(): raise ValueError('Missing/duplicate point ordering')
        for value, rows in work.groupby(group, sort=True):
            rows = rows.sort_values(order)
            if len(rows) < 2: raise ValueError('At least two points per line')
            emit(rows.iloc[0], LineString(list(rows.geometry)), 0, source_ids=json.dumps(rows[p['id']].astype(str).tolist(),ensure_ascii=False))
    else:
        cutters = None
        if other is not None:
            (work, cutter_frame), factor = analysis_frames([frame, other], work.crs)
            cutters = cutter_frame.geometry.union_all()
        for _, row in work.iterrows():
            geom = row.geometry
            parts = list(geom.geoms) if hasattr(geom, 'geoms') else [geom]
            count = 0
            for part_index, part in enumerate(parts):
                results = []
                if method == 'explode': results = [(part, {})]
                elif method in ('boundary', 'rings', 'representative', 'label'):
                    if part.geom_type != 'Polygon': raise ValueError('Operation requires polygons')
                    if method == 'boundary': results = [(part.boundary, {})]
                    elif method == 'rings': results = [(LineString(part.exterior), {'ring': 'outer'})] + [(LineString(r), {'ring': 'hole'}) for r in part.interiors]
                    else: results = [(part.representative_point(), {'candidate': True})]
                elif method == 'split':
                    if cutters is None: raise ValueError('split requires right input cutters')
                    if part.geom_type not in ('Polygon', 'LineString'): raise ValueError('split requires polygon/line input')
                    pieces = [part]
                    cutter_parts = list(cutters.geoms) if hasattr(cutters, 'geoms') else [cutters]
                    for cutter in cutter_parts:
                        pieces = [piece for current in pieces for piece in split(current, cutter).geoms]  # Coincident line cutters fail.
                    rebuilt = shapely.union_all(pieces)
                    tolerance = number(p.get('tolerance', 1e-9))
                    if tolerance < 0: raise ValueError('Negative reconstruction tolerance')
                    area_tolerance = number(p.get('area_tolerance',1e-9))
                    if area_tolerance < 0: raise ValueError('Negative area reconstruction tolerance')
                    if (part.hausdorff_distance(rebuilt) > tolerance or abs(part.area-rebuilt.area) > area_tolerance
                            or (part.geom_type=='LineString' and abs(sum(x.length for x in pieces)-part.length)>tolerance)):
                        raise ValueError('Split reconstruction failed')
                    results = [(x, {}) for x in pieces]
                elif method in ('segment', 'sample', 'locate', 'cross', 'offset'):
                    if part.geom_type != 'LineString' or part.length == 0: raise ValueError('Along-line operation requires nonzero lines')
                    if method == 'offset':
                        side = p.get('side', 'left')
                        if side not in ('left', 'right'): raise ValueError('side must be left/right')
                        shifted = part.offset_curve(distance if side == 'left' else -distance)
                        if shifted.is_empty: diagnostics.append({'source_id': row[p['id']], 'part': part_index, 'reason': 'empty offset'})
                        else: results = [(shifted, {'side': side})]
                    else:
                        if method == 'locate':
                            positions = [number(x)/factor for x in p['measures_m']]
                            if any(x < 0 or x > part.length for x in positions): raise ValueError('Measure outside part')
                        else:
                            # Each stored multipart part starts at zero; shared endpoints appear once per part.
                            n = int(math.ceil(part.length/distance))
                            if n > int(p.get('max_features', 100000)): raise ValueError('Too many samples')
                            positions = [i*distance for i in range(n)] + [part.length]
                        if method == 'segment': results = [(substring(part, a, b), {'start_m': a*factor, 'end_m': b*factor}) for a,b in zip(positions, positions[1:])]
                        else:
                            for m in positions:
                                point = part.interpolate(m)
                                if method == 'cross':
                                    epsilon = min(distance/1000, part.length/1000)
                                    start, end = part.interpolate(max(0,m-epsilon)), part.interpolate(min(part.length,m+epsilon))
                                    dx,dy = end.x-start.x,end.y-start.y; norm = math.hypot(dx,dy)
                                    if norm == 0: raise ValueError('Undefined cross-section tangent')
                                    half = number(p['width_m'])/factor/2
                                    if half <= 0: raise ValueError('width_m must be positive')
                                    point = LineString([(point.x-dy/norm*half,point.y+dx/norm*half),(point.x+dy/norm*half,point.y-dx/norm*half)])
                                results.append((point, {'measure_m': m*factor}))
                else: raise ValueError('Unknown geometry method')
                for result, attrs in results:
                    emit(row, result, count, source_part=part_index, **attrs); count += 1
    result = gpd.GeoDataFrame(output, geometry='geometry', crs=work.crs) if output else gpd.GeoDataFrame({'source_id': [], 'part_id': []}, geometry=[], crs=work.crs)
    return result, {'analysis_crs': work.crs.to_string(), 'diagnostics': diagnostics,
                    'units': 'metres for measures and widths; geometry coordinates in analysis CRS units',
                    'multipart_order': 'stored order, measures restart per part', 'z_m': 'reject',
                    'sampling': 'engineering candidates, not new observations'}


def grid(frame, p):
    work, factor = metric(frame, p.get('analysis_crs'))
    region = work.geometry.union_all(); size = number(p['size_m']) / factor
    if size <= 0: raise ValueError('size_m must be positive')
    origin = p['origin']; method = p.get('method', 'square'); rows = []
    if len(origin) != 2 or not all(math.isfinite(float(v)) for v in origin): raise ValueError('Invalid origin')
    xmin,ymin,xmax,ymax = region.bounds
    limit = int(p.get('max_features', 100000))
    if len(work) > limit: raise ValueError('Input exceeds max_features')
    if method == 'voronoi':
        if not work.geom_type.eq('Point').all(): raise ValueError('Voronoi requires points')
        stable(work, p['id'])
        if work.geometry.duplicated().any() or len(work) < 2: raise ValueError('Voronoi requires >=2 distinct sites')
        if 'bounds' not in p: raise ValueError('Voronoi requires explicit clipping bounds in analysis CRS')
        extent = box(*p['bounds'])
        if not work.geometry.covered_by(extent).all(): raise ValueError('Sites outside clipping bounds')
        cells = voronoi_diagram(region, envelope=extent)
        for cell in cells.geoms:
            owners = work[work.geometry.covered_by(cell)]
            if len(owners) != 1: raise ValueError('Ambiguous Voronoi source')
            rows.append({'cell_id': str(owners.iloc[0][p['id']]), 'geometry': cell.intersection(extent)})
    else:
        if not work.geom_type.isin(['Polygon', 'MultiPolygon']).all(): raise ValueError('Grid/sampling requires polygon study area')
        if method not in ('square','hex','stratified','spaced'): raise ValueError('Unknown grid method')
        dx,dy = (size*1.5, math.sqrt(3)*size) if method == 'hex' else (size,size)
        imin,imax = math.floor((xmin-origin[0])/dx)-1,math.ceil((xmax-origin[0])/dx)+1
        jmin,jmax = math.floor((ymin-origin[1])/dy)-2,math.ceil((ymax-origin[1])/dy)+2
        if (imax-imin+1)*(jmax-jmin+1) > limit: raise ValueError('Grid candidate count exceeds max_features')
        rng = np.random.default_rng(p.get('seed', 0)); accepted = []
        for i in range(imin,imax+1):
            for j in range(jmin,jmax+1):
                x,y = origin[0]+i*dx,origin[1]+j*dy
                if method == 'hex':
                    y += (i % 2)*dy/2
                    cell = Polygon([(x+size*math.cos(t*math.pi/3),y+size*math.sin(t*math.pi/3)) for t in range(6)])
                else: cell = box(x,y,x+size,y+size)
                clipped = cell.intersection(region)
                if clipped.is_empty or clipped.area == 0: continue
                if method in ('stratified','spaced'):
                    selected = None
                    for attempt in range(int(p.get('attempts_per_cell', 100))):
                        candidate = Point(rng.uniform(x,x+size),rng.uniform(y,y+size))
                        if not clipped.contains(candidate): continue
                        spacing = number(p.get('minimum_m', p['size_m'])) / factor
                        if method == 'spaced' and any(candidate.distance(old) < spacing for old in accepted): continue
                        selected = candidate; accepted.append(candidate); break
                    if selected is None: continue
                    clipped = selected
                rows.append({'cell_id': f'{i}:{j}', 'geometry': clipped})
    result = gpd.GeoDataFrame(rows, geometry='geometry', crs=work.crs) if rows else gpd.GeoDataFrame({'cell_id': []}, geometry=[], crs=work.crs)
    return result, {'analysis_crs': work.crs.to_string(), 'seed': p.get('seed',0), 'origin': origin,
                    'hex_size': 'circumradius', 'sampling_limit': 'bounded rejection; empty cells may have no sample',
                    'method': method}


def select(frame, other, p):
    """Subtract restrictions, split connected components, and explain exclusions."""
    stable(frame,p['id'])
    (work, restriction),factor = analysis_frames([frame,other],p.get('analysis_crs'))
    union = restriction.geometry.union_all(); output = []; reasons = []
    threshold = number(p.get('min_area_m2',0))
    if threshold < 0: raise ValueError('Negative area threshold')
    for _, row in work.iterrows():
        residual = row.geometry.difference(union)
        parts = list(residual.geoms) if hasattr(residual,'geoms') else [residual]
        reasons.append({'source_id': row[p['id']], 'reason': 'restriction', 'removed_m2': (row.geometry.area-residual.area)*factor**2})
        for index,part in enumerate(parts):
            area = part.area*factor**2
            if part.is_empty or area < threshold or area == 0:
                reasons.append({'source_id': row[p['id']], 'part': index, 'reason': 'empty_or_below_min_area', 'area_m2':area}); continue
            attrs = row.to_dict(); attrs[work.geometry.name] = part
            if {'candidate_id','area_m2'}.intersection(attrs): raise ValueError('Candidate output field collision')
            attrs.update(candidate_id=f'{row[p["id"]]}:{index}', area_m2=area); output.append(attrs)
    result = gpd.GeoDataFrame(output,geometry=work.geometry.name,crs=work.crs) if output else work.iloc[:0].assign(candidate_id=pd.Series(dtype=str),area_m2=pd.Series(dtype=float))
    return result, {'exclusions':reasons, 'analysis_crs':work.crs.to_string(), 'connectivity':'each remaining polygon part; no cross-source dissolve'}


def attribute_join(left, right, p):
    stable(left, p['id'])
    key, right_key = p.get('key', p['id']), p.get('right_key', p.get('key', p['id']))
    if left[key].isna().any() or right[right_key].isna().any(): raise ValueError('Null join keys are forbidden')
    duplicates = right[right_key].duplicated(keep=False)
    if duplicates.any() and p.get('cardinality') != 'one_to_many':
        raise ValueError('Duplicate right keys require explicit cardinality=one_to_many')
    fields = p.get('fields', [c for c in right.columns if c != getattr(getattr(right, 'geometry', None), 'name', None)])
    prefix = p.get('prefix', 'joined_')
    names = {c: prefix+c for c in fields}
    if len(set(names.values())) != len(names) or set(names.values()).intersection(left.columns): raise ValueError('Joined field collision')
    projected = right[fields].copy()
    internal = '__join_key__'
    if internal in left or internal in names.values(): raise ValueError('Reserved join key collision')
    projected = projected.rename(columns=names)
    projected[internal] = right[right_key]
    out = left.merge(projected, how='left', left_on=key, right_on=internal, sort=False, validate='many_to_many' if duplicates.any() else 'many_to_one').drop(columns=internal)
    return out, {'matched_left': int(left[key].isin(right[right_key]).sum()),
                 'unmatched_left': left.loc[~left[key].isin(right[right_key]), p['id']].tolist(),
                 'unmatched_right_keys': right.loc[~right[right_key].isin(left[key]), right_key].tolist(),
                 'duplicate_right_keys': right.loc[duplicates, right_key].tolist(),
                 'cardinality': p.get('cardinality', 'many_to_one'), 'source_id': p['id']}
