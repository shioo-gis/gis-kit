#!/usr/bin/env python3
"""Bounded DEM diagnostics: Horn slope/aspect, 3x3 elevation range and optional analytical hillshade."""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np
import rasterio as rio
from pyproj import CRS
import scipy
from scipy.ndimage import correlate, minimum_filter, maximum_filter

from _budget import check_raster
from _delivery import bundle, digest, write_json
from _peak_rss import peak_rss_bytes
from raster import inspect, band_data, positive_integer


def derivatives(z, dx, dy):
    """Metre elevations; east/north spacings. Missing neighbours invalidate output."""
    if z.ndim != 2 or min(z.shape) < 3 or not np.isfinite([dx, dy]).all() or min(dx, dy) <= 0:
        raise ValueError('At least 3x3 pixels and positive finite spacing required')
    valid = np.isfinite(z)
    complete = minimum_filter(valid.astype('uint8'), size=3, mode='constant', cval=0).astype(bool)
    a = np.where(valid, z, 0.)
    kernel = np.array([[-1., 0., 1.], [-2., 0., 2.], [-1., 0., 1.]])
    east = correlate(a, kernel, mode='constant') / (8 * dx)
    north = -correlate(a, kernel.T, mode='constant') / (8 * dy)
    slope = np.degrees(np.arctan(np.hypot(east, north)))
    aspect = np.mod(np.degrees(np.arctan2(-east, -north)), 360.)
    aspect[(east == 0) & (north == 0)] = np.nan
    relief = maximum_filter(a, size=3) - minimum_filter(a, size=3)
    for values in (slope, aspect, relief):
        values[~complete] = np.nan
    return {'slope': slope, 'aspect': aspect, 'relief': relief, '_east': east, '_north': north, '_complete': complete}


def hillshade(east, north, complete, azimuth=315., altitude=45., z_factor=1.):
    """Lambertian illumination 0..1 from Horn gradients; light from `azimuth` (clockwise from grid
    north) at `altitude` degrees. Values only: the renderer colours them."""
    az, alt = np.radians(azimuth), np.radians(altitude)
    gx, gy = east * z_factor, north * z_factor
    # Surface normal (-gx, -gy, 1) against the unit light vector pointing towards the sun.
    light = np.array([np.sin(az) * np.cos(alt), np.cos(az) * np.cos(alt), np.sin(alt)])
    shade = (-gx * light[0] - gy * light[1] + light[2]) / np.sqrt(gx * gx + gy * gy + 1)
    shade = np.clip(shade, 0., 1.)
    shade[~complete] = np.nan
    return shade


def summary(a):
    v = a[np.isfinite(a)]
    return {'valid_pixels': int(v.size), 'missing_pixels': int(a.size-v.size),
            'min': float(v.min()) if v.size else None, 'max': float(v.max()) if v.size else None,
            'mean': float(v.mean()) if v.size else None,
            'quantiles': np.quantile(v, [0, .25, .5, .75, 1]).tolist() if v.size else []}


def execute(source, params, output):
    started = time.perf_counter()
    allowed = {'schema_version', 'band', 'vertical_unit', 'vertical_datum', 'source_description', 'max_pixels', 'hillshade'}
    if not isinstance(params, dict) or set(params)-allowed or params.get('schema_version', 1) != 1:
        raise ValueError('Unknown parameter or schema')
    factors = {'metre': 1., 'foot': .3048, 'us_survey_foot': 1200/3937}
    if params.get('vertical_unit') not in factors:
        raise ValueError('Explicit vertical_unit required: metre, foot, us_survey_foot')
    for field in ('vertical_datum', 'source_description'):
        if not isinstance(params.get(field), str) or not params[field].strip():
            raise ValueError(f'Explicit {field} required; use unknown if unverified')
    shade = params.get('hillshade')
    if shade is not None:
        if not isinstance(shade, dict) or set(shade) - {'azimuth', 'altitude', 'z_factor'}:
            raise ValueError('hillshade takes azimuth, altitude and z_factor')
        shade = {'azimuth': 315., 'altitude': 45., 'z_factor': 1., **shade}
        if not all(isinstance(shade[k], (int, float)) and np.isfinite(shade[k]) for k in shade) \
                or not 0 <= shade['azimuth'] < 360 or not 0 < shade['altitude'] <= 90 or not 0 < shade['z_factor'] <= 100:
            raise ValueError('hillshade azimuth 0..360, altitude 0..90 and z_factor 0..100 required')
    source = Path(source).resolve()
    if any(Path(str(source)+s).exists() for s in ('.msk', '.aux.xml', '.ovr')):
        raise ValueError('External sidecars unsupported; prepare self-contained raster')
    identity = digest(source)
    implementation = {n: digest(Path(__file__).with_name(n)) for n in ('terrain.py', 'raster.py', '_delivery.py')}
    with bundle(output) as stage:
        with rio.open(source) as ds:
            meta = inspect(ds)
            crs = CRS(ds.crs)
            if not crs.is_projected or len(crs.axis_info) != 2:
                raise ValueError('Two-dimensional projected CRS required; warp explicitly first')
            axes = crs.axis_info
            if [a.direction for a in axes] != ['east', 'north']:
                raise ValueError('East/north projected axes required')
            t = ds.transform
            if t.b or t.d or t.a <= 0 or t.e >= 0:
                raise ValueError('North-up grid required; warp explicitly first')
            limit = positive_integer(params.get('max_pixels', 5000000))
            if ds.width*ds.height > limit:
                raise ValueError('Raster exceeds max_pixels memory guard')
            # z, zero-filled copy, east/north gradients, slope/aspect/relief and a filter temporary.
            check_raster(ds, arrays=10 if shade else 8)
            dx, dy = t.a*axes[0].unit_conversion_factor, -t.e*axes[1].unit_conversion_factor
            band = positive_integer(params.get('band', 1))
            if ds.units[band-1] is not None and ds.units[band-1] not in (params['vertical_unit'], {'metre':'m','foot':'ft','us_survey_foot':'us_survey_foot'}[params['vertical_unit']]):
                raise ValueError('Band unit conflicts with declared vertical unit')
            z = band_data(ds, band).filled(np.nan)*factors[params['vertical_unit']]
            arrays = derivatives(z, dx, dy)
            east, north, complete = arrays.pop('_east'), arrays.pop('_north'), arrays.pop('_complete')
            if shade is not None:
                arrays['hillshade'] = hillshade(east, north, complete, **shade)
            if not np.isfinite(arrays['slope']).any():
                raise ValueError('No complete valid 3x3 neighbourhood')
            profile = dict(driver='GTiff', width=ds.width, height=ds.height, count=1,
                           dtype='float64', crs=ds.crs, transform=t, nodata=np.nan, compress='deflate')
        artifacts = {}
        for name, values in arrays.items():
            path = stage/f'{name}.tif'
            unit = {'relief': 'metre', 'hillshade': 'ratio'}.get(name, 'degree')
            with rio.open(path, 'w', **profile) as out:
                out.write(values, 1)
                out.set_band_unit(1, unit)
                out.set_band_description(1, name)
            with rio.open(path) as check:
                decoded = check.read(1, masked=True).filled(np.nan)
                if check.crs != profile['crs'] or check.transform != t or check.units != (unit,) or not np.array_equal(decoded, values, equal_nan=True):
                    raise ValueError('Raster readback mismatch')
            artifacts[name] = {'file': path.name, 'sha256': digest(path), 'summary': summary(values), 'unit': unit}
        if digest(source) != identity or implementation != {n: digest(Path(__file__).with_name(n)) for n in implementation}:
            raise ValueError('Source or implementation changed during execution')
        write_json(stage/'record.json', {'schema_version': 1, 'status': 'complete',
            'source': {'name': source.name, 'sha256': identity, 'grid': meta}, 'parameters': params,
            'method': 'Horn 3x3; downhill aspect clockwise from grid north; 3x3 max-minus-min relief'
                      + ('; Lambertian hillshade 0..1 (light azimuth/altitude, z_factor scales gradients)' if shade else ''),
            'hillshade': shade,
            'assumptions': ['No filling or edge interpolation', 'Flat aspect is NoData',
                            'Projected grid distances; no ground-scale correction (z_factor is the only scale adjustment)', 'No hydrology or event reconstruction'],
            'spacing_metres': [dx, dy], 'elevation_metres': summary(z), 'artifacts': artifacts,
            'environment': {'python': sys.version, 'numpy': np.__version__, 'scipy': scipy.__version__,
                            'rasterio': rio.__version__, 'gdal': rio.__gdal_version__},
            'implementation': implementation, 'resources': {'wall_seconds': time.perf_counter()-started,
            'max_rss_bytes': peak_rss_bytes(),
            'rss_scope': 'process lifetime high-water mark; excludes child processes'}})
    return Path(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input'); parser.add_argument('--params', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        execute(args.input, json.loads(Path(args.params).read_text(encoding='utf-8')), args.output)
    except (ValueError, KeyError, TypeError, IndexError, OSError, rio.errors.RasterioError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
