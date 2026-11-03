#!/usr/bin/env python3
"""Explicit GRASS r.walk directed terrain costs, candidate path and excess-cost corridor."""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

import geopandas as gpd
import numpy as np
import rasterio as rio
from shapely.geometry import LineString
from _budget import check_raster
from _delivery import bundle, digest, write_json
from _safe_io import iter_vector_chunks, write_vector_atomic
from raster import inspect, band_data, band_matches
from terrain import derivatives, summary

_spec = importlib.util.spec_from_file_location('terrain_backend', Path(__file__).with_name('terrain-backend.py'))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


def validate(p, ds):
    common = {'vertical_unit', 'vertical_datum', 'source_description', 'max_pixels'}
    if not isinstance(p, dict) or set(p) - (common | {'start', 'end', 'walk_coeff', 'slope_factor', 'friction_lambda', 'corridor_extra_seconds', 'cost_assumptions'}):
        raise ValueError('Unknown terrain cost parameter')
    B.validate('hydrology', dict({k: v for k, v in p.items() if k in common}, flow_method='D8', threshold_cells=1), ds)
    # z, friction, from/to cost and back direction, excess, corridor, slope and derivative temporaries.
    check_raster(ds, arrays=10)
    if not isinstance(p.get('cost_assumptions'), str) or not p['cost_assumptions'].strip():
        raise ValueError('Explicit friction/obstacle and walking assumptions required')
    coeff = p['walk_coeff']
    if not isinstance(coeff, list) or len(coeff) != 4:
        raise ValueError('walk_coeff requires a,b,c,d')
    a, b, c, d = [B.finite(v, -1e20) for v in coeff]
    sf = B.finite(p['slope_factor'], -1e20)
    if a <= 0 or b < 0 or c < 0 or d > 0 or sf >= 0 or a + c * sf <= 0:
        raise ValueError('Coefficients must give strictly positive movement costs on all slopes')
    B.finite(p['friction_lambda']); B.finite(p['corridor_extra_seconds'])
    points = []
    for key in ('start', 'end'):
        xy = p[key]
        if not isinstance(xy, list) or len(xy) != 2:
            raise ValueError('Point requires [x,y]')
        for v in xy: B.finite(v, -1e20)
        r, col = ds.index(*xy)
        if not (0 <= r < ds.height and 0 <= col < ds.width):
            raise ValueError('Point outside DEM')
        x, y = ds.xy(r, col)
        if max(abs(x-xy[0])/ds.res[0], abs(y-xy[1])/ds.res[1]) > 1e-9:
            raise ValueError('Points must be pixel centres; snap explicitly')
        points.append((r, col))
    return points


def step_cost(z0, z1, distance, f0, f1, p):
    """Independent edge-cost check, not a routing implementation."""
    dh = z1-z0
    a, b, c, d = p['walk_coeff']
    coefficient = b if dh >= 0 else (c if dh/distance >= p['slope_factor'] else d)
    return a*distance + coefficient*dh + p['friction_lambda']*(f0+f1)*.5*distance


def trace(direction, start, end):
    # r.walk outdir points backwards towards the start; eight-neighbour mode only.
    offsets = {45: (-1, 1), 90: (-1, 0), 135: (-1, -1), 180: (0, -1),
               225: (1, -1), 270: (1, 0), 315: (1, 1), 360: (0, 1)}
    path = [end]; seen = {end}
    while path[-1] != start:
        r, c = path[-1]; v = direction[r, c]
        if not np.isfinite(v) or v not in offsets:
            raise ValueError('Broken predecessor direction')
        dr, dc = offsets[v]; q = (r+dr, c+dc)
        if not (0 <= q[0] < direction.shape[0] and 0 <= q[1] < direction.shape[1]) or q in seen:
            raise ValueError('Cyclic or outside predecessor path')
        path.append(q); seen.add(q)
    return path[::-1]


def execute(source, friction, p, output, grass):
    started = time.perf_counter()
    paths = [Path(source).resolve(), Path(friction).resolve()]
    grass = Path(grass).resolve()
    if not grass.is_file() or not os.access(grass, os.X_OK):
        raise ValueError('Explicit executable GRASS launcher required')
    hashes = [digest(x) for x in paths]
    code = {n: digest(Path(__file__).with_name(n)) for n in ('terrain-cost.py', '_walk_worker.py', 'terrain-backend.py', 'terrain.py', 'raster.py', '_delivery.py', '_safe_io.py')}
    for path in paths:
        if any(Path(str(path)+s).exists() for s in ('.msk', '.ovr', '.aux.xml')):
            raise ValueError('External sidecars unsupported')
    with rio.open(paths[0]) as ds:
        meta = inspect(ds); start, end = validate(p, ds); z = band_data(ds, 1)
        if np.ma.getmaskarray(z).any():
            raise ValueError('Complete DEM required; missing elevation is not a known obstacle')
        profile = dict(driver='GTiff', width=ds.width, height=ds.height, count=1, dtype='float64', crs=ds.crs, transform=ds.transform, nodata=np.nan, compress='deflate')
    with rio.open(paths[1]) as ds:
        inspect(ds)
        if ds.count != 1 or ds.shape != z.shape or ds.crs != profile['crs'] or ds.transform != profile['transform']:
            raise ValueError('Friction grid must exactly match DEM')
        if ds.units[0] not in (None, 's/m'):
            raise ValueError('Friction requires seconds per metre')
        f = band_data(ds, 1).filled(np.nan)
        if (f[np.isfinite(f)] < 0).any():
            raise ValueError('Friction must be nonnegative; NoData explicitly means barrier')
        if not np.isfinite(f[start]) or not np.isfinite(f[end]):
            raise ValueError('Endpoint is a barrier')
    z = z.filled(np.nan)
    with bundle(output) as stage:
        with tempfile.TemporaryDirectory(prefix='.walk-work-', dir=stage.parent) as tmp:
            work = Path(tmp); (work/'home').mkdir()
            env = dict(os.environ, HOME=str(work/'home'), TMPDIR=str(work), PYTHONDONTWRITEBYTECODE='1', GRASS_MESSAGE_FORMAT='plain')
            env.pop('GISRC', None); env.pop('GISBASE', None)
            def run(args):
                result = subprocess.run(args, env=env, capture_output=True, text=True)
                if result.returncode:
                    raise ValueError(f'GRASS walk session failed (exit {result.returncode}); no result published')
                return result.stdout.strip()
            version = run([str(grass), '--version'])
            for name, a in [('dem', z), ('friction', f)]:
                with rio.open(work/f'{name}.tif', 'w', **profile) as ds: ds.write(a, 1)
            run([str(grass), '-c', str(work/'dem.tif'), str(work/'location'), '-e'])
            write_json(work/'job.json', dict(work=str(work), params=p))
            run([str(grass), str(work/'location/PERMANENT'), '--exec', sys.executable, str(Path(__file__).with_name('_walk_worker.py')), str(work/'job.json')])
            arrays = {}; errors = {}
            for name in ('from_start', 'to_end', 'back_direction'):
                with rio.open(work/f'{name}.tif') as ds:
                    if ds.shape != z.shape or ds.crs != profile['crs']:
                        raise ValueError('Backend grid mismatch')
                    errors[name] = B.grid_error(ds.transform, profile['transform'], z.shape)
                    arrays[name] = ds.read(1, masked=True).filled(np.nan)
            worker = json.loads((work/'worker.json').read_text(encoding='utf-8'))
        cost = arrays['from_start'][end]
        tolerance = 1e-7  # seconds absolute plus 1e-10 relative, numerical only
        if arrays['from_start'][start] != 0 or arrays['to_end'][end] != 0:
            raise ValueError('Source accumulation must be zero')
        for name in ('from_start', 'to_end'):
            a = arrays[name]
            if (a[np.isfinite(a)] < 0).any() or np.isfinite(a[~np.isfinite(f)]).any():
                raise ValueError('Invalid cost or traversed barrier')
        reachable = bool(np.isfinite(cost))
        rows = []; path = []; checked = None
        if reachable:
            if not math.isclose(cost, arrays['to_end'][start], rel_tol=1e-10, abs_tol=tolerance):
                raise ValueError('Transpose cost disagreement')
            path = trace(arrays['back_direction'], start, end)
            checked = sum(step_cost(z[u], z[v], math.hypot((v[1]-u[1])*profile['transform'].a, (v[0]-u[0])*profile['transform'].e), f[u], f[v], p) for u, v in zip(path, path[1:]))
            if not math.isclose(cost, checked, rel_tol=1e-10, abs_tol=tolerance):
                raise ValueError('Path edge-cost recomputation mismatch')
            if len(path) > 1:
                rows.append(dict(route_id='start_to_end', cost_seconds=float(cost), geometry=LineString([rio.transform.xy(profile['transform'], *q) for q in path])))
        elif np.isfinite(arrays['to_end'][start]):
            raise ValueError('Transpose reachability disagreement')
        excess = arrays['from_start'] + arrays['to_end'] - cost if reachable else np.full(z.shape, np.nan)
        if reachable and np.any(excess[np.isfinite(excess)] < -(tolerance+abs(cost)*1e-10)):
            raise ValueError('Negative corridor excess beyond numerical tolerance')
        excess = np.maximum(excess, 0)
        corridor = np.where(np.isfinite(excess), (excess <= p['corridor_extra_seconds'] + tolerance).astype(float), np.nan)
        arrays.update(excess_seconds=excess, corridor=corridor, slope=derivatives(z, profile['transform'].a, -profile['transform'].e)['slope'], friction=f)
        units = {'slope': 'degree', 'friction': 's/m', 'corridor': '1', 'back_direction': 'degree'}
        artifacts = {}
        for name, a in arrays.items():
            dest = stage/f'{name}.tif'; unit = units.get(name, 's')
            with rio.open(dest, 'w', **profile) as ds: ds.write(a, 1); ds.set_band_unit(1, unit)
            with rio.open(dest) as ds:
                if ds.crs != profile['crs'] or ds.transform != profile['transform'] or ds.units != (unit,) or not band_matches(ds, 1, a):
                    raise ValueError('Raster readback mismatch')
            artifacts[dest.name] = dict(sha256=digest(dest), summary=summary(a))
        routes = gpd.GeoDataFrame(rows, columns=['route_id', 'cost_seconds', 'geometry'], geometry='geometry', crs=profile['crs'])
        write_vector_atomic(routes, stage/'routes.gpkg')
        count = 0
        for start, back in iter_vector_chunks(stage/'routes.gpkg'):
            part = routes.iloc[start:start+len(back)]
            if not back.geometry.equals(part.geometry) or back.route_id.tolist() != part.route_id.tolist() or back.cost_seconds.tolist() != part.cost_seconds.tolist() or back.crs != routes.crs:
                raise ValueError('Route readback mismatch')
            count += len(back)
        if count != len(routes):
            raise ValueError('Route readback mismatch')
        artifacts['routes.gpkg'] = dict(sha256=digest(stage/'routes.gpkg'), count=len(routes))
        if hashes != [digest(x) for x in paths] or code != {n: digest(Path(__file__).with_name(n)) for n in code}:
            raise ValueError('Source or implementation changed')
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)
        write_json(stage/'record.json', dict(status='candidate', parameters=p, inputs=[dict(path=str(x), sha256=h) for x, h in zip(paths, hashes)], source_grid=meta, implementation=code, backend_version=version, backend_grid_error_pixels=errors, reachable=reachable, cost_seconds=float(cost) if reachable else None, path_recomputed_seconds=checked, path_cells=[list(q) for q in path], artifacts=artifacts, resources=dict(wall_seconds=time.perf_counter()-started, self_max_rss_bytes=rss, worker=worker), assumptions=['Eight-neighbour GRASS r.walk without knights; cell-centre graph permits diagonal corner contact, barriers exclude cells only.', 'Friction NoData is an explicitly supplied barrier; complete DEM required. No hidden slope cutoff.', 'to_end uses negated elevation from end: exact transpose of directed edge costs, not ordinary downhill cost from end.', 'Corridor = cells with d(start,v)+d(v,end)-d(start,end) <= declared extra seconds; finite graph walks, not a continuous safe travel area.', 'Slope is Horn 3x3 diagnostic; edge costs use signed elevation difference / horizontal distance. Parameters are scenarios, not surveyed walking times.'], numerical_tolerance=dict(absolute_seconds=tolerance, relative=1e-10)))
    return Path(output)


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    a = argparse.ArgumentParser(description=__doc__); a.add_argument('input'); a.add_argument('--friction', required=True); a.add_argument('--params', required=True); a.add_argument('--grass', required=True); a.add_argument('--output', required=True); v = a.parse_args()
    try: execute(v.input, v.friction, json.loads(Path(v.params).read_text(encoding='utf-8')), v.output, v.grass)
    except (ValueError, KeyError, TypeError, OSError) as e: a.exit(1, f'ERROR: {e}\n')
