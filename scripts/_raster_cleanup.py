"""Bounded derived raster cleaning and categorical accounting; never edit observations."""
from types import SimpleNamespace

import numpy as np
import scipy
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

import _raster_numeric as n
from _daily import stable
from _grid import band_data, guard_pixels, inspect, positive_integer, windows

OPERATIONS = ('fill_holes', 'focal', 'aggregate', 'regions', 'sieve', 'seed_region',
              'morphology', 'class_area', 'transition', 'class_combine', 'zonal_fill')


def structure(p):
    return ndi.generate_binary_structure(2, {4: 1, 8: 2}[n.choice(p.get('connectivity', 4), (4, 8))])


def categories(a):
    v = a[np.isfinite(a)]
    if np.any(v != np.floor(v)):
        raise ValueError('Categorical values must be exact integers')


def regions(a, p):
    categories(a)
    labels = np.zeros(a.shape, dtype=np.int32)
    rows = []
    for value in np.unique(a[np.isfinite(a)]):
        parts, count = ndi.label(a == value, structure(p))
        offset = len(rows)
        known = parts > 0
        labels[known] = parts[known]+offset
        counts = np.bincount(parts.ravel(), minlength=count+1)
        rows.extend(dict(instance_id=offset+part, category=float(value), pixels=int(counts[part]))
                    for part in range(1, count+1))
    return labels, rows


def reduce_values(values, method):
    values = values[np.isfinite(values)]
    if not len(values):
        return np.nan
    if method == 'mode':
        keys, counts = np.unique(values, return_counts=True)
        return keys[np.argmax(counts)]  # ascending class tie
    return float({'mean': np.mean, 'sum': np.sum, 'min': np.min, 'max': np.max,
                  'median': np.median, 'std': np.std}[method](values))


def footprint(ds, p):
    if 'radius_m' in p:
        radius = n.finite(p['radius_m'])
        if radius <= 0:
            raise ValueError('Positive radius_m required')
        factor = n.area_factor(ds)**.5
        dx, dy = ds.transform.a*factor, -ds.transform.e*factor
        x, y = int(np.floor(radius/dx)), int(np.floor(radius/dy))
        if (2*x+1)*(2*y+1) > positive_integer(p.get('max_window_cells', 10001)):
            raise ValueError('Footprint exceeds max_window_cells')
        yy, xx = np.mgrid[-y:y+1, -x:x+1]
        return (xx*dx)**2+(yy*dy)**2 <= radius**2
    size = positive_integer(p.get('size', 3))
    if size % 2 != 1 or size*size > positive_integer(p.get('max_window_cells', 10001)):
        raise ValueError('Odd bounded window size required')
    return np.ones((size, size), bool)


def focal(a, ds, p):
    method = n.choice(p.get('statistic', 'mean'), ('mean', 'sum', 'min', 'max', 'median', 'std', 'mode'))
    if p.get('kind') == 'categorical':
        categories(a)
        if method != 'mode':
            raise ValueError('Categorical focal requires mode')
    else:
        n.choice(p.get('kind'), ('continuous',))
    fp = footprint(ds, p)
    ry, rx = np.array(fp.shape)//2
    boundary = n.choice(p.get('boundary', 'partial'), ('partial', 'pad', 'truncate'))
    threshold = n.finite(p.get('min_coverage', 1))
    if not 0 <= threshold <= 1:
        raise ValueError('min_coverage outside [0,1]')
    out = np.full(a.shape, np.nan)
    for win in windows(ds.width, ds.height, positive_integer(p.get('block_size', 256))):
        r, c, h, w = map(int, (win.row_off, win.col_off, win.height, win.width))
        r0, r1 = max(0, r-ry), min(a.shape[0], r+h+ry)
        c0, c1 = max(0, c-rx), min(a.shape[1], c+w+rx)
        sub = a[r0:r1, c0:c1]
        known = ndi.correlate(np.isfinite(sub).astype(float), fp.astype(float), mode='constant', cval=0)
        available = ndi.correlate(np.ones(sub.shape), fp.astype(float), mode='constant', cval=0)
        denom = available if boundary == 'partial' else float(fp.sum())
        result = ndi.generic_filter(sub, lambda v: reduce_values(v, method), footprint=fp, mode='constant', cval=np.nan)
        result[(known == 0) | (known < threshold*denom)] = np.nan
        if boundary == 'truncate':
            result[available < fp.sum()] = np.nan
        out[r:r+h, c:c+w] = result[r-r0:r-r0+h, c-c0:c-c0+w]
    if not p.get('derive_missing', False):
        out[~np.isfinite(a)] = np.nan
    return out


def fill(a, ds, p, protected, allowed):
    kind = n.choice(p['kind'], ('continuous', 'categorical'))
    method = n.choice(p['method'], ('nearest', 'idw'))
    if kind == 'categorical':
        categories(a)
        if method != 'nearest':
            raise ValueError('Categorical holes require nearest')
    limit = positive_integer(p['max_hole_pixels'])
    distance = n.finite(p['max_distance_m'])
    if distance <= 0:
        raise ValueError('Positive max_distance_m required')
    factor = n.area_factor(ds)**.5
    sampling = (-ds.transform.e*factor, ds.transform.a*factor)
    holes, count = ndi.label(~np.isfinite(a), structure(p))
    counts = np.bincount(holes.ravel(), minlength=count+1)
    eligible = counts <= limit
    eligible[0] = False
    edge_ids = np.unique(np.concatenate((holes[0], holes[-1], holes[:, 0], holes[:, -1])))
    eligible[edge_ids] = False
    eligible[np.unique(holes[protected | ~allowed])] = False
    selected = eligible[holes]
    donors = np.isfinite(a) & allowed & ~protected
    out = a.copy()
    if not donors.any() or not selected.any():
        return out
    positions = np.argwhere(donors)*sampling
    tree = cKDTree(positions)
    donor_values = a[donors]
    budget = positive_integer(p.get('max_donor_pairs', 2000000))
    for r, c in np.argwhere(selected):
        point = np.array([r, c])*sampling
        ids = tree.query_ball_point(point, distance)
        budget -= len(ids)
        if budget < 0:
            raise ValueError('Exceeded max_donor_pairs')
        if not ids:
            continue
        ids = np.array(sorted(ids))
        d = np.linalg.norm(positions[ids]-point, axis=1)
        if method == 'nearest':
            out[r, c] = donor_values[ids[np.argmin(d)]]
        else:
            weights = (d.min()/d)**2
            out[r, c] = np.dot(weights, donor_values[ids])/weights.sum()
    return out


def process(operation, datasets, p, stage, stack):
    import rasterio as rio
    from _delivery import write_json
    from _budget import check_raster
    ds = datasets[0]
    guard_pixels(len(datasets)*ds.width*ds.height, positive_integer(p.get('max_elements', 10000000)), f'{len(datasets)} input(s) of {ds.width} x {ds.height}', param='max_elements', way_out='Warp to a coarser resolution or clip first')
    # Operand decodes plus output, labels, masks and the encoded copy; regions measured 69-77 bytes per pixel.
    check_raster(ds, arrays=len(datasets)+9)
    arrays = []
    for other in datasets:
        n.same_grid(ds, other)
        arrays.append(band_data(other, positive_integer(p.get('band', 1))).filled(np.nan))
    if operation not in ('transition', 'class_combine') and len(arrays) != 1:
        raise ValueError('Operation requires one input')
    a = arrays[0]
    protected = np.zeros(a.shape, bool)
    if 'protected' in p:
        other = stack.enter_context(rio.open(p['protected']))
        n.same_grid(ds, other)
        mask = band_data(other, 1).filled(np.nan)
        # Unknown protection is conservative: never modify it.
        protected = ~np.isfinite(mask) | (mask != 0)
    allowed = np.ones(a.shape, bool)
    if 'scope' in p:
        other = stack.enter_context(rio.open(p['scope']))
        n.same_grid(ds, other)
        mask = band_data(other, 1).filled(np.nan)
        allowed = np.isfinite(mask) & (mask != 0)
    details = dict(source=inspect(ds), scipy_version=scipy.__version__, derived=True, original_evidence_modified=False,
                   applicability='Bounded north-up aligned grid; numerical candidate, not observed evidence',
                   valid_scope='Finite input support; missing stays unknown unless explicit fill/derive_missing',
                   area_method='projected planar square metres when area reported', parameters=p)
    grid = ds
    if operation == 'fill_holes':
        out = fill(a, ds, p, protected, allowed)
    elif operation == 'focal':
        out = focal(a, ds, p)
    elif operation == 'aggregate':
        factor = positive_integer(p['factor'])
        boundary = n.choice(p['boundary'], ('partial', 'pad', 'truncate'))
        method = n.choice(p['statistic'], ('mean', 'sum', 'min', 'max', 'median', 'std', 'mode'))
        if n.choice(p['kind'], ('continuous', 'categorical')) == 'categorical':
            categories(a)
            if method != 'mode':
                raise ValueError('Categorical aggregation requires mode')
        threshold = n.finite(p.get('min_coverage', 1))
        if not 0 <= threshold <= 1:
            raise ValueError('min_coverage outside [0,1]')
        h, w = (np.array(a.shape)//factor if boundary == 'truncate' else (np.array(a.shape)+factor-1)//factor)
        if min(h, w) < 1:
            raise ValueError('No complete aggregate block')
        out = np.full((h, w), np.nan); coverage = np.zeros((h, w))
        for r, c in np.ndindex(out.shape):
            sub = a[r*factor:(r+1)*factor, c*factor:(c+1)*factor]
            denom = sub.size if boundary == 'partial' else factor*factor
            coverage[r, c] = np.isfinite(sub).sum()/denom
            if coverage[r, c] >= threshold:
                out[r, c] = reduce_values(sub, method)
        grid = SimpleNamespace(width=int(w), height=int(h), crs=ds.crs,
                               transform=ds.transform*rio.Affine.scale(factor))
        details['coverage_artifact'] = n.write(stage, 'coverage.tif', coverage, grid, {})
        details['boundary'] = boundary
    elif operation in ('regions', 'sieve', 'seed_region'):
        labels, rows = regions(a, p)
        pixel_area = abs(ds.transform.determinant)*n.area_factor(ds)
        for row in rows:
            row['area_m2'] = row['pixels']*pixel_area
        details['instances'] = rows
        if operation == 'regions':
            out = np.where(labels > 0, labels, np.nan)
        elif operation == 'seed_region':
            ids = set()
            for seed in p['seeds']:
                r, c = seed
                if any(isinstance(v, bool) or not isinstance(v, int) for v in seed) or not (0 <= r < a.shape[0] and 0 <= c < a.shape[1]) or labels[r, c] == 0:
                    raise ValueError('Seed must be a known in-grid row/column')
                ids.add(labels[r, c])
            if not ids:
                raise ValueError('Nonempty seeds required')
            out = np.where(np.isin(labels, list(ids)), a, np.nan)
        else:
            minimum = n.finite(p['min_area_m2'])
            if minimum < 0:
                raise ValueError('Negative min_area_m2')
            protected_ids = set(np.unique(labels[protected]))
            small = [row['instance_id'] for row in rows if row['area_m2'] < minimum and row['instance_id'] not in protected_ids]
            replace = np.isin(labels, small) & allowed & ~protected
            out = a.copy()
            method = n.choice(p.get('method', 'remove'), ('remove', 'nearest'))
            if method == 'remove':
                out[replace] = np.nan
            else:
                max_distance = n.finite(p['max_distance_m'])
                if max_distance <= 0:
                    raise ValueError('Positive max_distance_m required')
                donors = (labels > 0) & ~np.isin(labels, small) & allowed & ~protected
                if donors.any():
                    f = n.area_factor(ds)**.5
                    distances, indices = ndi.distance_transform_edt(~donors, sampling=(-ds.transform.e*f, ds.transform.a*f), return_indices=True)
                    replace &= distances <= max_distance
                    out[replace] = a[tuple(indices[:, replace])]
    elif operation == 'morphology':
        categories(a)
        value, background = n.finite(p['category']), n.finite(p['background'])
        if value == background or value != int(value) or background != int(background):
            raise ValueError('Distinct integer category/background required')
        target = a == value
        method = n.choice(p['method'], ('dilate', 'erode', 'open', 'close'))
        fn = {'dilate': ndi.binary_dilation, 'erode': ndi.binary_erosion, 'open': ndi.binary_opening, 'close': ndi.binary_closing}[method]
        changed = fn(target, structure=footprint(ds, p), iterations=positive_integer(p.get('iterations', 1)), border_value=0)
        out = a.copy()
        editable = ((a == value) | (a == background)) & ~protected & allowed
        out[editable] = np.where(changed[editable], value, background)
    elif operation in ('class_combine', 'transition'):
        if operation == 'transition' and len(arrays) != 2:
            raise ValueError('Transition requires two dates')
        for data in arrays:
            categories(data)
        joint = np.logical_and.reduce([np.isfinite(data) for data in arrays])
        tuples, inverse, counts = np.unique(np.stack(arrays, axis=-1)[joint], axis=0, return_inverse=True, return_counts=True)
        out = np.full(a.shape, np.nan); out[joint] = inverse+1
        details['codebook'] = [dict(code=i+1, categories=values.tolist(), pixels=int(counts[i])) for i, values in enumerate(tuples)]
        if operation == 'transition':
            area = abs(ds.transform.determinant)*n.area_factor(ds)
            for row in details['codebook']:
                row['area_m2'] = row['pixels']*area
            details.update(joint_valid_area_m2=int(joint.sum())*area,
                           before_only_area_m2=int((np.isfinite(a) & ~np.isfinite(arrays[1])).sum())*area,
                           after_only_area_m2=int((~np.isfinite(a) & np.isfinite(arrays[1])).sum())*area,
                           both_unknown_area_m2=int((~np.isfinite(a) & ~np.isfinite(arrays[1])).sum())*area)
            change = np.where(joint, (a != arrays[1]).astype(float), np.nan)
            details['change_artifact'] = n.write(stage, 'change.tif', change, ds, {})
    elif operation in ('class_area', 'zonal_fill'):
        zones = n.polygons(p['vector'], ds, p.get('layer')); stable(zones, p['id'])
        if operation == 'class_area':
            categories(a)
        method = n.choice(p['method'], ('center', 'all_touched', 'fractional'))
        pixel_area = abs(ds.transform.determinant)*n.area_factor(ds)
        budget = [positive_integer(p.get('max_intersections', 2000000))]
        rows = []; out = np.full(a.shape, np.nan); occupied = np.zeros(a.shape, bool)
        for _, zone in zones.iterrows():
            weights = n.coverage(zone.geometry, ds, method, budget)
            valid = np.isfinite(a) & (weights > 0)
            area = float(weights[valid].sum()*pixel_area)
            total = float(weights.sum()*pixel_area)
            row = dict(zone_id=str(zone[p['id']]), raster_support_area_m2=total, valid_area_m2=area,
                       coverage=area/total if total else None, polygon_area_m2=float(zone.geometry.area*n.area_factor(ds)))
            if operation == 'class_area':
                row['classes'] = [dict(category=float(value), area_m2=float(weights[valid & (a == value)].sum()*pixel_area)) for value in np.unique(a[valid])]
            else:
                stat = n.choice(p.get('statistic', 'mean'), ('mean', 'sum'))
                result = float(np.sum(a[valid]*weights[valid])) if valid.any() else None
                if stat == 'mean' and result is not None:
                    result /= weights[valid].sum()
                row['value'] = result
                selected = weights > 0
                if (occupied & selected).any():
                    raise ValueError('Overlapping zone pixel support; explicit separate outputs required')
                occupied |= selected
                if result is not None:
                    out[selected & np.isfinite(a)] = result
            rows.append(row)
        details['zones'] = rows
        if operation == 'class_area':
            write_json(stage/'table.json', rows)
            from _delivery import digest
            return dict(details, artifact='table.json', artifact_sha256=digest(stage/'table.json'))
    else:
        raise ValueError('Unknown cleanup operation')
    if operation in ('fill_holes', 'focal', 'sieve', 'morphology'):
        out[protected | ~allowed] = a[protected | ~allowed]
        changed = ~((out == a) | (np.isnan(out) & np.isnan(a)))
        details['repair_artifact'] = n.write(stage, 'modified.tif', changed.astype(float), ds, {})
        details['modified_pixels'] = int(changed.sum())
        details['interpolated_pixels'] = int((~np.isfinite(a) & np.isfinite(out)).sum())
    details.update(n.write(stage, 'result.tif', out, grid, p))
    return details
