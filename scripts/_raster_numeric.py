"""Windowed local and bounded global raster operations; no implicit alignment."""
import csv
import hashlib
import itertools
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio as rio
from pyproj import CRS
from rasterio.features import geometry_mask, rasterize
from rasterio.windows import Window, from_bounds
from shapely.geometry import box, mapping
from shapely.ops import unary_union

from _budget import check_raster, check_vector
from _core import reproject
from _delivery import fingerprint
from _grid import band_data, inspect, positive_integer, windows

OPERATIONS = ('transform', 'combine', 'update', 'rasterize', 'bands', 'allocate', 'redistribute')


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Expected finite number')
    return value


def choice(value, allowed):
    if value not in allowed:
        raise ValueError(f'Expected one of {allowed}, got {value}')
    return value


def same_grid(a, b):
    if (a.crs, a.transform, a.width, a.height) != (b.crs, b.transform, b.width, b.height):
        raise ValueError('Grids differ; explicitly align first')


def area_factor(ds):
    from _metric import _horizontal_meters_per_unit
    return _horizontal_meters_per_unit(CRS(ds.crs))**2


def polygons(path, ds, layer=None, polygon_only=True):
    check_vector(path, layer)
    frame = gpd.read_file(path, layer=layer)
    if frame.empty or frame.crs is None or frame.geometry.isna().any() or frame.geometry.is_empty.any() or not frame.geometry.is_valid.all() or (polygon_only and not frame.geom_type.isin(['Polygon', 'MultiPolygon']).all()):
        raise ValueError('Nonempty valid polygons with CRS required')
    frame = reproject(frame, ds.crs, purpose='align')
    if not frame.geometry.is_valid.all():
        raise ValueError('Invalid transformed polygons')
    return frame


def coverage(geometry, ds, mode, budget):
    """Exact planar fractions or GDAL center/touch semantics, clipped candidate window."""
    choice(mode, ('center', 'all_touched', 'fractional'))
    out = np.zeros((ds.height, ds.width))
    if geometry.is_empty:
        return out
    if mode != 'fractional':
        return rasterize([(mapping(geometry), 1.)], out_shape=out.shape, transform=ds.transform,
                         fill=0., all_touched=mode == 'all_touched', dtype='float64')
    area_factor(ds)
    window = from_bounds(*geometry.bounds, transform=ds.transform)
    c0, r0 = max(0, math.floor(window.col_off)), max(0, math.floor(window.row_off))
    c1 = min(ds.width, math.ceil(window.col_off + window.width))
    r1 = min(ds.height, math.ceil(window.row_off + window.height))
    budget[0] -= max(0, r1-r0)*max(0, c1-c0)
    if budget[0] < 0:
        raise ValueError('Exceeded max_intersections guard')
    pixel_area = abs(ds.transform.determinant)
    for r in range(r0, r1):
        for c in range(c0, c1):
            x, y = ds.transform*(c, r)
            x1, y1 = ds.transform*(c+1, r+1)
            out[r, c] = min(1., max(0., geometry.intersection(box(x, y1, x1, y)).area/pixel_area))
    return out


def classify(data, p):
    policy = choice(p.get('unmapped', 'nodata'), ('keep', 'nodata', 'error'))
    out = data.copy() if policy == 'keep' else np.full(data.shape, np.nan)
    valid = np.isfinite(data)
    matched = np.zeros(data.shape, dtype=bool)
    overlap = choice(p.get('overlap', 'error'), ('error', 'first', 'last'))
    rules = list(p.get('rules', []))
    for key, value in p.get('mapping', {}).items():
        rules.append({'value': finite(key), 'output': value})
    if 'table' in p:
        with open(p['table'], newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                if 'value' in row:
                    rules.append({'value': finite(row['value']), 'output': finite(row['output'])})
                else:
                    rules.append({'min': finite(row['min']), 'max': finite(row['max']), 'output': finite(row['output'])})
    for rule in rules:
        if 'value' in rule:
            selected = valid & (data == finite(rule['value']))
        else:
            lo, hi = finite(rule['min']), finite(rule['max'])
            if lo > hi:
                raise ValueError('Reversed interval')
            closed = choice(rule.get('closed', 'left'), ('left', 'right', 'both', 'neither'))
            selected = valid & ((data >= lo) if closed in ('left', 'both') else (data > lo)) & ((data <= hi) if closed in ('right', 'both') else (data < hi))
        if overlap == 'error' and np.any(selected & matched):
            raise ValueError('Overlapping assignment rules')
        if overlap == 'first':
            selected &= ~matched
        out[selected] = np.nan if rule['output'] is None else finite(rule['output'])
        matched |= selected
    if policy == 'error' and np.any(valid & ~matched):
        raise ValueError('Unmapped values')
    return out


def transform(data, p):
    method = p['method']
    details = {}
    v = data[np.isfinite(data)]
    if method == 'rules':
        return classify(data, p), details
    if method == 'auto_class':
        if not len(v):
            raise ValueError('Automatic classes require valid values')
        count = positive_integer(p['classes'])
        if count > 10000:
            raise ValueError('Too many classes')
        mode = choice(p['mode'], ('equal', 'quantile'))
        edges = np.linspace(v.min(), v.max(), count+1) if mode == 'equal' else np.quantile(v, np.linspace(0, 1, count+1))
        if not np.isfinite(edges).all():
            raise ValueError('Automatic breaks overflow')
        edges = np.unique(edges)
        out = np.searchsorted(edges[1:-1], data, side='right').astype(float)+1
        out[~np.isfinite(data)] = np.nan
        details.update(breaks=edges.tolist(), effective_classes=max(1, len(edges)-1), endpoint='[left,right); final maximum included; ties collapse')
    elif method in ('minmax', 'zscore'):
        if not len(v):
            raise ValueError('Normalization requires valid values')
        center = v.min() if method == 'minmax' else v.mean()
        scale = v.max()-v.min() if method == 'minmax' else v.std()
        if not np.isfinite([center, scale]).all():
            raise ValueError('Normalization overflow')
        if scale == 0:
            policy = choice(p.get('constant', 'error'), ('error', 'zero', 'nodata'))
            if policy == 'error':
                raise ValueError('Constant input')
            out = np.where(np.isfinite(data), 0. if policy == 'zero' else np.nan, np.nan)
        else:
            out = (data-center)/scale
        details.update(center=float(center), scale=float(scale), standard_deviation='population')
    elif method in ('clip', 'invalidate'):
        lo, hi = finite(p['min']), finite(p['max'])
        if lo > hi:
            raise ValueError('Reversed bounds')
        out = np.clip(data, lo, hi) if method == 'clip' else np.where((data >= lo) & (data <= hi), data, np.nan)
    elif method in ('log', 'exp', 'sqrt', 'abs', 'power'):
        with np.errstate(all='ignore'):
            out = {'log': np.log, 'exp': np.exp, 'sqrt': np.sqrt, 'abs': np.abs}.get(method, lambda x: np.power(x, finite(p['exponent'])))(data)
        invalid = np.isfinite(data) & ~np.isfinite(out)
        policy = choice(p.get('invalid', 'error'), ('error', 'nodata'))
        if policy == 'error' and invalid.any():
            raise ValueError('Function domain error or overflow')
        out[invalid] = np.nan
    else:
        raise ValueError('Unknown transform method')
    if np.isinf(out).any():
        raise ValueError('Transform overflow')
    return out, details


def combine(arrays, p):
    method = p['method']
    a = np.stack(arrays)
    valid = np.isfinite(a)
    policy = choice(p.get('nodata', 'propagate'), ('propagate', 'ignore'))
    count = valid.sum(axis=0)
    mask = (count == len(a)) if policy == 'propagate' else (count >= p.get('min_valid', 1))
    minimum = positive_integer(p.get('min_valid', 1))
    if minimum > len(a):
        raise ValueError('min_valid exceeds operand count')
    with np.errstate(all='ignore'):
        if method == 'where':
            if len(a) != 3 or policy != 'propagate':
                raise ValueError('Where requires condition/true/false operands and propagate policy')
            out = np.where(a[0] != 0, a[1], a[2])
            mask = valid[0] & np.where(a[0] != 0, valid[1], valid[2])
        elif method in ('sum', 'mean', 'min', 'max', 'std', 'median'):
            # Masked operations avoid all-NaN warnings while retaining missingness.
            ma = np.ma.masked_invalid(a)
            funcs = {'sum': np.ma.sum, 'mean': np.ma.mean, 'min': np.ma.min, 'max': np.ma.max,
                     'std': np.ma.std, 'median': np.ma.median}
            out = funcs[method](ma, axis=0).filled(np.nan)
        elif method in ('weighted_sum', 'weighted_mean'):
            w = np.asarray(p['weights'], dtype=float)
            if w.shape != (len(a),) or not np.isfinite(w).all() or (w < 0).any() or w.sum() <= 0:
                raise ValueError('Finite nonnegative weights required, one per operand')
            out = np.nansum(a*w[:, None, None], axis=0)
            if method == 'weighted_mean':
                denom = (valid*w[:, None, None]).sum(axis=0)
                out = out/denom
        elif method in ('add', 'subtract', 'multiply', 'divide', 'gt', 'ge', 'eq', 'and', 'or'):
            if len(a) != 2 or policy != 'propagate':
                raise ValueError('Binary operations require two operands and propagate policy')
            funcs = {'add': np.add, 'subtract': np.subtract, 'multiply': np.multiply, 'divide': np.divide,
                     'gt': np.greater, 'ge': np.greater_equal, 'eq': np.equal, 'and': np.logical_and, 'or': np.logical_or}
            out = funcs[method](a[0], a[1]).astype(float)
        else:
            raise ValueError('Unknown combine method')
    bad = mask & ~np.isfinite(out)
    invalid = choice(p.get('invalid', 'error'), ('error', 'nodata'))
    if bad.any() and invalid == 'error':
        raise ValueError('Arithmetic domain error, zero denominator or overflow')
    out[~mask | bad] = np.nan
    return out


def encode(data, p):
    """Shared typed encoding for full-array and window writers."""
    data = np.asarray(data, dtype=float)
    if data.ndim == 2:
        data = data[None]
    if np.isinf(data).any():
        raise ValueError('Nonfinite output overflow')
    dtype = np.dtype(choice(p.get('dtype', 'float64'), ('uint8', 'int16', 'uint16', 'int32', 'uint32', 'float32', 'float64')))
    valid = np.isfinite(data)
    if dtype.kind in 'iu':
        rounding = choice(p.get('rounding', 'error'), ('error', 'nearest_even', 'floor', 'ceil'))
        if rounding == 'error' and np.any(data[valid] != np.floor(data[valid])):
            raise ValueError('Integer output requires explicit rounding')
        if rounding != 'error':
            data = {'nearest_even': np.rint, 'floor': np.floor, 'ceil': np.ceil}[rounding](data)
        nodata = finite(p['nodata_value'])
        info = np.iinfo(dtype)
        if nodata != int(nodata) or not info.min <= nodata <= info.max:
            raise ValueError('Unrepresentable NoData')
        if np.any(data[valid] == nodata):
            raise ValueError('NoData conflicts with valid value')
    else:
        if 'nodata_value' in p:
            raise ValueError('Floating output uses NaN NoData')
        info = np.finfo(dtype)
        nodata = np.nan
    if np.any((data[valid] < info.min) | (data[valid] > info.max)):
        raise ValueError('Output type overflow')
    encoded = np.where(valid, data, nodata).astype(dtype)
    error = float(np.max(np.abs(encoded[valid].astype(float)-data[valid]))) if valid.any() else 0.
    return encoded, valid, nodata, error


def row_strips(ds, pixels, bands=None):
    """Yield ``(band, window)`` full-width row strips of at most ``pixels`` cells (one row minimum)."""
    rows = max(1, pixels//ds.width)
    for band in bands or range(1, ds.count+1):
        for y in range(0, ds.height, rows):
            yield band, Window(0, y, ds.width, min(rows, ds.height-y))


def band_fsum(ds, band, pixels=1 << 20):
    """Exactly rounded sum of a band's unmasked values; fsum is order-independent, so strips match a full read."""
    return math.fsum(itertools.chain.from_iterable(
        ds.read(band, window=strip, masked=True).compressed() for _, strip in row_strips(ds, pixels, [band])))


def write(stage, filename, data, ds, p, descriptions=None):
    """Typed output, explicit rounding and full decoded readback before publication."""
    encoded, valid, nodata, error = encode(data, p)
    dtype = encoded.dtype
    data = encoded
    profile = dict(driver='GTiff', width=ds.width, height=ds.height, count=len(data), crs=ds.crs,
                   transform=ds.transform, dtype=dtype.name, nodata=nodata, compress='deflate')
    path = stage/filename
    with rio.open(path, 'w', **profile) as out:
        out.write(encoded)
        if descriptions:
            out.descriptions = tuple(descriptions)
    if p.get('format', 'GTiff') == 'COG':
        from rasterio.shutil import copy
        temp = stage/('cog-'+filename)
        copy(path, temp, driver='COG', compress='DEFLATE', overview_resampling='nearest')
        temp.replace(path)
    else:
        choice(p.get('format', 'GTiff'), ('GTiff',))
    digest = hashlib.sha256()
    with rio.open(path) as out:
        metadata = inspect(out)
        same_grid(out, ds)
        if out.count != len(data) or out.dtypes != (dtype.name,)*len(data):
            raise ValueError('Output metadata mismatch')
        if descriptions and out.descriptions != tuple(descriptions):
            raise ValueError('Band identity readback mismatch')
        if p.get('format') == 'COG' and out.tags(ns='IMAGE_STRUCTURE').get('LAYOUT') != 'COG':
            raise ValueError('COG layout missing')
        for win in windows(ds.width, ds.height, positive_integer(p.get('block_size', 256))):
            decoded = out.read(window=win, masked=True)
            slices = (slice(None), *win.toslices())
            if not np.array_equal(np.ma.getmaskarray(decoded), ~valid[slices]) or not np.array_equal(decoded.data, encoded[slices], equal_nan=True):
                raise ValueError('Decoded output mismatch')
        # Canonical band/row-order hash, read in full-width row strips.
        for band, strip in row_strips(out, positive_integer(p.get('block_size', 256))**2):
            digest.update(out.read(band, window=strip).astype(dtype.newbyteorder('<')).tobytes())
    return {'artifact': filename, 'artifact_sha256': fingerprint(path),
            'output': metadata, 'decoded_sha256': digest.hexdigest(), 'valid_pixels': int(valid.sum()),
            'max_abs_encoding_error': error}


def allocate(ds, arrays, p, stage, budget):
    from _daily import stable
    zones = polygons(p['vector'], ds, p.get('layer'))
    stable(zones, p['id'])
    zones = zones.assign(_sort_id=zones[p['id']].astype(str)).sort_values('_sort_id')
    overlap = choice(p.get('overlap', 'error'), ('error', 'independent'))
    if overlap == 'error':
        for i, geom in enumerate(zones.geometry):
            for j in zones.sindex.query(geom, predicate='intersects'):
                if j > i and geom.intersection(zones.geometry.iloc[j]).area > 0:
                    raise ValueError('Overlapping allocation zones')
    method = choice(p['method'], ('uniform', 'area', 'weighted'))
    if p.get('quantity') != 'total' or p.get('assumption') != 'redistribute_over_eligible_support':
        raise ValueError('Explicit total quantity and redistribution assumption required')
    if p.get('dtype', 'float64') != 'float64':
        raise ValueError('Allocation requires float64; integer uses largest_remainder flag')
    if (len(zones)+1)*ds.width*ds.height > p.get('max_elements', 10000000):
        raise ValueError('Allocation contribution bands exceed max_elements')
    factor = area_factor(ds)
    cell_area = abs(ds.transform.determinant)*factor
    exclusion = unary_union(polygons(p['exclude'], ds, p.get('exclude_layer')).geometry) if 'exclude' in p else None
    domain = arrays[0]
    if method == 'weighted':
        if len(arrays) != 2:
            raise ValueError('Weighted allocation requires domain and weight operands')
        weight_kind = choice(p['weight_kind'], ('per_area', 'per_pixel'))
        weights = arrays[1]
        if np.any(weights[np.isfinite(weights)] < 0):
            raise ValueError('Negative allocation weight')
    else:
        if len(arrays) != 1:
            raise ValueError('Uniform/area allocation takes one domain operand')
        weights = np.ones_like(domain)
        weight_kind = None
    total_grid = np.zeros_like(domain)
    covered = np.zeros(domain.shape, dtype=bool)
    bands, records = [], []
    for _, row in zones.iterrows():
        amount = finite(row[p['total_field']])
        if amount < 0 or amount > 2**53:
            raise ValueError('Total must be nonnegative and within float64 exact integer range')
        integer = p.get('integer', False)
        if integer and amount != math.floor(amount):
            raise ValueError('Integer allocation requires integer source total')
        geom = row.geometry.difference(exclusion) if exclusion is not None else row.geometry
        fractions = coverage(geom, ds, 'fractional', budget)
        valid = np.isfinite(domain) & np.isfinite(weights) & (fractions > 0)
        mass = np.where(valid, weights, 0.)
        if method == 'area' or (method == 'weighted' and weight_kind == 'per_area'):
            mass *= fractions*cell_area
        elif method == 'weighted':
            mass *= fractions
        # Uniform means equal shares per intersected eligible cell, explicitly not area weighting.
        denominator = math.fsum(mass.ravel())
        if not math.isfinite(denominator):
            raise ValueError('Allocation mass overflow')
        out = np.zeros_like(domain)
        if denominator > 0:
            out = mass/denominator*amount
            if integer:
                base = np.floor(out)
                remainder = int(amount)-sum(map(int, base.ravel()))
                candidates = np.flatnonzero(mass.ravel() > 0)
                order = candidates[np.argsort(-(out-base).ravel()[candidates], kind='stable')]
                if not 0 <= remainder <= len(order):
                    raise ValueError('Integer remainder precision error')
                base.ravel()[order[:remainder]] += 1
                out = base
        assigned = math.fsum(out.ravel())
        unallocated = amount if denominator == 0 else 0.
        error = assigned+unallocated-amount
        tolerance = max(1e-9, amount*1e-12)
        if abs(error) > tolerance:
            raise ValueError('Allocation conservation failed')
        total_grid += out
        covered |= valid
        bands.append(np.where(valid, out, np.nan))
        records.append({'zone_id': str(row[p['id']]), 'source_total': amount, 'allocated': assigned,
                        'unallocated': unallocated, 'conservation_error': error, 'tolerance': tolerance,
                        'reason': 'zero_weight_or_no_eligible_support' if denominator == 0 else None,
                        'eligible_area_m2': float(fractions[valid].sum()*cell_area),
                        'source_area_m2': float(row.geometry.area*factor), 'eligible_cells': int(valid.sum())})
    if p.get('integer') and np.any(total_grid > 2**53):
        raise ValueError('Aggregate integer total exceeds exact float64 range')
    result = write(stage, 'result.tif', np.where(covered, total_grid, np.nan), ds, p, ['aggregate_total_per_pixel'])
    contributions = write(stage, 'contributions.tif', bands, ds, p, [r['zone_id'] for r in records])
    # Source contributions are essential: a mixed boundary pixel cannot reconstruct zone identity.
    with rio.open(stage/'contributions.tif') as check:
        for i, record in enumerate(records, 1):
            back = band_fsum(check, i)
            if abs(back-record['allocated']) > record['tolerance']:
                raise ValueError('Source contribution back-calculation failed')
            record['readback_total'] = back
    result.update(zones=records, contributions=contributions, quantity='total_per_pixel',
                  back_calculation='sum each source contribution band; do not fraction-weight totals a second time',
                  overlap=overlap, integer_rule='largest remainder; row-major ties' if p.get('integer') else None,
                  spatial_weights_business_validated=False)
    return result


def redistribute(source, target, data, p, stage, budget):
    kind = choice(p['quantity'], ('total', 'density'))
    if source.crs != target.crs:
        raise ValueError('Conservative redistribution requires the same projected CRS')
    factor = area_factor(source)
    if p.get('assumption') != 'uniform_within_source_pixel' or p.get('dtype', 'float64') != 'float64':
        raise ValueError('Explicit within-pixel uniform assumption and float64 required')
    src_area = abs(source.transform.determinant)*factor
    dst_area = abs(target.transform.determinant)*factor
    out = np.zeros((target.height, target.width))
    touched = np.zeros(out.shape, dtype=bool)
    covered_area = np.zeros(out.shape)
    source_total, lost = [], []
    # Visit only intersecting destination cells, including non-divisible edges.
    for r, c in np.argwhere(np.isfinite(data)):
        amount = float(data[r, c])*(src_area if kind == 'density' else 1)
        if amount < 0:
            raise ValueError('Conserved quantities must be nonnegative')
        source_total.append(amount)
        x, y = source.transform*(int(c), int(r))
        x1, y1 = source.transform*(int(c)+1, int(r)+1)
        window = from_bounds(x, y1, x1, y, transform=target.transform)
        c0, r0 = max(0, math.floor(window.col_off)), max(0, math.floor(window.row_off))
        c1, r1 = min(target.width, math.ceil(window.col_off+window.width)), min(target.height, math.ceil(window.row_off+window.height))
        budget[0] -= max(0, c1-c0)*max(0, r1-r0)
        if budget[0] < 0:
            raise ValueError('Exceeded max_intersections guard')
        fractions = []
        for rr in range(r0, r1):
            for cc in range(c0, c1):
                xx, yy = target.transform*(cc, rr)
                xx1, yy1 = target.transform*(cc+1, rr+1)
                fraction = max(0., min(x1, xx1)-max(x, xx))*max(0., min(y, yy)-max(y1, yy1))/abs(source.transform.determinant)
                if fraction > 0:
                    out[rr, cc] += amount*fraction
                    touched[rr, cc] = True
                    covered_area[rr, cc] += fraction*src_area
                    fractions.append(fraction)
        lost.append(amount*max(0., 1-math.fsum(fractions)))
    assigned = math.fsum(out.ravel())
    original, unallocated = math.fsum(source_total), math.fsum(lost)
    tolerance = max(1e-9, original*1e-12)
    if not math.isfinite(original+assigned+unallocated) or abs(assigned+unallocated-original) > tolerance:
        raise ValueError('Conservative redistribution failed')
    if kind == 'density':
        out /= dst_area
    result = write(stage, 'result.tif', np.where(touched, out, np.nan), target, p)
    with rio.open(stage/'result.tif') as check:
        back = band_fsum(check, 1)*(dst_area if kind == 'density' else 1)
        if abs(back-assigned) > tolerance:
            raise ValueError('Conservative output back-calculation failed')
    support = write(stage, 'coverage.tif', np.where(touched, np.clip(covered_area/dst_area, 0., 1.), np.nan), target, p, ['known_source_area_fraction'])
    result.update(coverage=support, quantity=kind, area_unit='m2', source_total=original, allocated=assigned, unallocated=unallocated, readback_total=back,
                  conservation_error=assigned+unallocated-original, tolerance=tolerance,
                  density_denominator='full destination cell square metres' if kind == 'density' else None,
                  missing_support='NoData outside all valid source support; partial cells use known mass only')
    return result


def is_local(operation, p):
    return (operation in ('bands', 'combine', 'update') or
            operation == 'transform' and p.get('method') in
            ('rules', 'clip', 'invalidate', 'log', 'exp', 'sqrt', 'abs', 'power'))


def read_operand(datasets, item, p, operation, window=None):
    index = item['input']
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(datasets):
        raise ValueError('Invalid input index')
    other = datasets[index]
    same_grid(datasets[0], other)
    band = positive_integer(item.get('band', 1))
    if band > other.count:
        raise ValueError('Band outside dataset')
    if operation == 'bands' and p.get('calibrate', False):
        raw = other.read(band, window=window, masked=True)
        if raw.dtype.kind in 'iu' and np.any(np.abs(raw.compressed().astype('longdouble')) > 2**53):
            raise ValueError('Integer values exceed exact float64 range')
        data = raw.astype(float).filled(np.nan)
        # Nonfinite source values are missing before calibration, including scale=0.
        data[~np.isfinite(data)] = np.nan
        scale = finite(item.get('scale', other.scales[band-1]))
        offset = finite(item.get('offset', other.offsets[band-1]))
        with np.errstate(all='ignore'):
            data = data*scale+offset
        if np.isinf(data).any():
            raise ValueError('Calibration overflow')
        return data
    if 'scale' in item or 'offset' in item:
        raise ValueError('Scale/offset require bands calibrate=true')
    return band_data(other, band, window).filled(np.nan)


def local_values(operation, arrays, p, mask=None):
    if operation == 'bands':
        return np.asarray(arrays)
    if operation == 'combine':
        return combine(arrays, p)
    if operation == 'transform':
        if len(arrays) != 1:
            raise ValueError('Transform requires one operand')
        return transform(arrays[0], p)[0]
    method = choice(p['method'], ('assign', 'replace', 'fill'))
    out = arrays[0].copy()
    if method == 'fill':
        if len(arrays) < 2:
            raise ValueError('Fill requires fallback operands')
        for a in arrays[1:]:
            missing = ~np.isfinite(out)
            out[missing] = a[missing]
    elif method == 'assign':
        if len(arrays) != 1:
            raise ValueError('Assign requires one operand')
        out[mask] = finite(p['value']) if p['value'] is not None else np.nan
    else:
        if len(arrays) != 2:
            raise ValueError('Replace requires two operands')
        if choice(p.get('replacement_nodata', 'propagate'), ('propagate', 'keep')) == 'keep':
            mask &= np.isfinite(arrays[1])
        out[mask] = arrays[1][mask]
    return out


def process_windows(operation, datasets, selectors, p, stage):
    """Bounded raster buffers throughout calculation, encoding, COG and readback.

    Vector geometries/rules and source codec blocks are separate memory costs.
    No per-window metadata is retained. Output is still published by execute().
    """
    from contextlib import ExitStack
    from rasterio.windows import Window, transform as window_transform
    ds = datasets[0]
    size = positive_integer(p.get('block_size', 256))
    capacity = min(size, ds.width)*min(size, ds.height)
    if len(selectors)*capacity > positive_integer(p.get('max_elements', 10000000)):
        raise ValueError('Window operands exceed max_elements memory guard')
    cache = positive_integer(p.get('gdal_cache_mb', 64))
    fmt = choice(p.get('format', 'GTiff'), ('GTiff', 'COG'))
    geometries = None
    if operation == 'update' and p.get('method') in ('assign', 'replace'):
        choice(p['coverage'], ('center', 'all_touched'))
        geometries = [mapping(unary_union(polygons(p['vector'], ds, p.get('layer')).geometry))]
    split = operation == 'bands' and p.get('split', False)
    names = [f'band-{i+1}.tif' for i in range(len(selectors))] if split else ['result.tif']
    counts = [1]*len(names) if split else [len(selectors) if operation == 'bands' else 1]
    descriptions = [[f'operand_{i+1}'] for i in range(len(selectors))] if split else [None]
    expected = [hashlib.sha256() for _ in names]
    valid_counts, errors = [0]*len(names), [0.]*len(names)
    dtype = None
    with rio.Env(GDAL_CACHEMAX=cache*1024**2, GDAL_NUM_THREADS='1'), ExitStack() as stack:
        writers = []
        for win in windows(ds.width, ds.height, size):
            arrays = [read_operand(datasets, s, p, operation, win) for s in selectors]
            mask = None if geometries is None else geometry_mask(
                geometries, (int(win.height), int(win.width)), window_transform(win, ds.transform),
                invert=True, all_touched=p['coverage'] == 'all_touched')
            values = local_values(operation, arrays, p, mask)
            outputs = values if split else [values]
            for i, values in enumerate(outputs):
                encoded, valid, nodata, error = encode(values, p)
                if len(writers) <= i:
                    dtype = encoded.dtype
                    writer = stack.enter_context(rio.open(stage/names[i], 'w', driver='GTiff',
                        width=ds.width, height=ds.height, count=counts[i], crs=ds.crs,
                        transform=ds.transform, dtype=dtype.name, nodata=nodata,
                        tiled=True, blockxsize=256, blockysize=256, compress='deflate', BIGTIFF='IF_SAFER'))
                    if descriptions[i]:
                        writer.descriptions = tuple(descriptions[i])
                    writers.append(writer)
                writers[i].write(encoded, window=win)
                expected[i].update(encoded.astype(dtype.newbyteorder('<'), copy=False).tobytes())
                expected[i].update(valid.tobytes())
                valid_counts[i] += int(valid.sum())
                errors[i] = max(errors[i], error)
        stack.close()  # Close GTiff before COG conversion/readback; cache limit stays active.
        artifacts = []
        for i, name in enumerate(names):
            path = stage/name
            if fmt == 'COG':
                from rasterio.shutil import copy
                temp = stage/('cog-'+name)
                copy(path, temp, driver='COG', compress='DEFLATE', overview_resampling='nearest',
                     NUM_THREADS='1', BIGTIFF='IF_SAFER')
                temp.replace(path)
            with rio.open(path) as out:
                same_grid(out, ds)
                if out.count != counts[i] or out.dtypes != (dtype.name,)*counts[i]:
                    raise ValueError('Output metadata mismatch')
                if descriptions[i] and out.descriptions != tuple(descriptions[i]):
                    raise ValueError('Band identity readback mismatch')
                if out.scales != (1.,)*out.count or out.offsets != (0.,)*out.count:
                    raise ValueError('Output calibration not reset')
                if fmt == 'COG' and out.tags(ns='IMAGE_STRUCTURE').get('LAYOUT') != 'COG':
                    raise ValueError('COG layout missing')
                actual = hashlib.sha256()
                for win in windows(ds.width, ds.height, size):
                    decoded = out.read(window=win, masked=True)
                    actual.update(decoded.data.astype(dtype.newbyteorder('<'), copy=False).tobytes())
                    actual.update((~np.ma.getmaskarray(decoded)).tobytes())
                if actual.digest() != expected[i].digest():
                    raise ValueError('Decoded output mismatch')
                # Canonical band/row-order hash, independent of processing block size.
                # Bound this pass too: wide rows are broken into contiguous segments.
                digest = hashlib.sha256()
                width = min(ds.width, capacity)
                rows = max(1, capacity//width)
                for band in range(1, out.count+1):
                    for y in range(0, ds.height, rows):
                        for x in range(0, ds.width, width):
                            win = Window(x, y, min(width, ds.width-x), min(rows, ds.height-y))
                            digest.update(out.read(band, window=win).astype(dtype.newbyteorder('<'), copy=False).tobytes())
                artifacts.append({'artifact': name, 'artifact_sha256': fingerprint(path), 'output': inspect(out),
                    'decoded_sha256': digest.hexdigest(), 'valid_pixels': valid_counts[i],
                    'max_abs_encoding_error': errors[i]})
    details = {'operands': selectors, 'sources': [inspect(d) for d in datasets],
        'derived': True, 'original_evidence_modified': False,
        'execution': {'mode': 'windowed', 'block_size': size, 'max_window_pixels': capacity,
                      'gdal_cache_mb': cache, 'vector_masks': 'resident geometries' if geometries else None,
                      'memory_boundary': 'raster buffers bounded; codec blocks, metadata and vector/rule inputs additional'}}
    if operation == 'bands':
        details['calibration'] = [{'scale': finite(s.get('scale', datasets[s['input']].scales[positive_integer(s.get('band', 1))-1])),
                                   'offset': finite(s.get('offset', datasets[s['input']].offsets[positive_integer(s.get('band', 1))-1]))}
                                  for s in selectors] if p.get('calibrate') else None
    return dict(details, artifacts=artifacts) if split else dict(details, **artifacts[0])


def process(operation, datasets, p, stage, stack):
    ds = datasets[0]
    budget = [positive_integer(p.get('max_intersections', 2000000))]
    selectors = p.get('operands', [{'input': i, 'band': p.get('band', 1)} for i in range(len(datasets))])
    if not selectors:
        raise ValueError('At least one operand required')
    if is_local(operation, p):
        return process_windows(operation, datasets, selectors, p, stage)
    if len(selectors)*ds.width*ds.height > positive_integer(p.get('max_elements', 10000000)):
        raise ValueError('Operand arrays exceed max_elements memory guard')
    # Operands plus output, occupancy/validity masks and the encoded copy written out.
    check_raster(ds, arrays=len(selectors)+4)
    arrays = [read_operand(datasets, item, p, operation) for item in selectors]
    details = {'operands': selectors, 'sources': [inspect(d) for d in datasets],
               'execution': {'mode': 'bounded_full_array'},
               'derived': True, 'original_evidence_modified': False}
    if operation == 'transform':
        if len(arrays) != 1:
            raise ValueError('Transform requires one operand')
        out, detail = transform(arrays[0], p)
        details.update(detail)
    elif operation == 'rasterize':
        from _daily import stable
        vectors = polygons(p['vector'], ds, p.get('layer'), polygon_only=p['coverage'] == 'fractional')
        stable(vectors, p['id'])
        priority = choice(p['overlap'], ('error', 'first', 'last'))
        mode = choice(p['coverage'], ('center', 'all_touched', 'fractional'))
        vectors = vectors.assign(_sort_id=vectors[p['id']].astype(str)).sort_values('_sort_id')
        if 'priority_field' in p:
            if vectors[p['priority_field']].isna().any():
                raise ValueError('Missing priority')
            vectors = vectors.sort_values([p['priority_field'], '_sort_id'], kind='stable')
        if mode == 'fractional' and priority == 'last':
            vectors = vectors.iloc[::-1]
            priority = 'first'
        background = finite(p['background']) if p.get('background') is not None else np.nan
        out = np.full(arrays[0].shape, background)
        occupied = np.zeros(out.shape, dtype=bool)
        claimed = None
        for _, row in vectors.iterrows():
            geom = row.geometry
            if mode == 'fractional' and claimed is not None:
                if priority == 'error' and geom.intersection(claimed).area > 0:
                    raise ValueError('Overlapping rasterization polygons')
                if priority == 'first':
                    geom = geom.difference(claimed)
            weights = coverage(geom, ds, mode, budget)
            selected = weights > 0
            if mode == 'fractional':
                out[selected & ~occupied] = 0
                out[selected] += finite(row[p['field']])*weights[selected]
            else:
                if priority == 'error' and np.any(selected & occupied):
                    raise ValueError('Multiple features select the same raster cell')
                if priority == 'first':
                    selected &= ~occupied
                out[selected] = finite(row[p['field']])
            occupied |= selected
            claimed = geom if claimed is None else claimed.union(geom)
        details.update(value_semantics='coverage-weighted value over full cell; uncovered fraction contributes zero' if mode == 'fractional' else 'attribute value',
                       order=vectors[p['id']].astype(str).tolist(), reference_mask='grid only; source mask not inherited')
    elif operation == 'allocate':
        return dict(details, **allocate(ds, arrays, p, stage, budget))
    elif operation == 'redistribute':
        if len(arrays) != 1:
            raise ValueError('Redistribute requires one operand')
        target = stack.enter_context(rio.open(p['reference']))
        inspect(target)
        if target.transform.b or target.transform.d or target.transform.a <= 0 or target.transform.e >= 0 or target.width*target.height > min(positive_integer(p.get('max_pixels', 50000000)), positive_integer(p.get('max_elements', 10000000))):
            raise ValueError('Invalid or oversized target grid')
        check_raster(target, arrays=4)  # output, touched mask, covered area and the encoded copy
        return dict(details, **redistribute(ds, target, arrays[0], p, stage, budget))
    else:
        raise ValueError('Unknown numerical operation')
    return dict(details, **write(stage, 'result.tif', out, ds, p))
