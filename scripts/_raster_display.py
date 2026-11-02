"""Web Mercator display grids only; analytical rasters remain untouched.

mode colors (default): classified RGBA for standalone use and QA plots.
mode values: uncoloured class ids or values plus a class scheme, coloured later by the map renderer.
"""
import hashlib
import json
import math
import re

import numpy as np
from PIL import Image
from rasterio.warp import transform


def display(ds, values, parameters, stage):
    mode = parameters.get('mode', 'colors')
    if mode == 'values':
        return display_values(ds, values, parameters, stage)
    if mode != 'colors':
        raise ValueError('mode must be colors or values')
    if ds.crs.to_epsg() != 3857:
        raise ValueError('Display requires EPSG:3857; warp explicitly before coloring')
    breaks = parameters['breaks']
    colors = parameters['colors']
    if not isinstance(breaks, list) or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in breaks):
        raise ValueError('breaks must be finite numbers')
    if any(a >= b for a, b in zip(breaks, breaks[1:])):
        raise ValueError('breaks must be strictly increasing')
    if not isinstance(colors, list) or len(colors) != len(breaks) + 1 or any(not isinstance(c, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in colors):
        raise ValueError('colors must contain one #RRGGBB per interval')
    if not isinstance(parameters.get('unit'), str) or not parameters['unit'].strip():
        raise ValueError('Explicit unit required (use dimensionless when appropriate)')
    if parameters.get('status') not in ('candidate', 'hold', 'accepted'):
        raise ValueError('Explicit candidate/hold/accepted status required; display does not grant acceptance')
    bound = 20037508.342789244
    if any(abs(v) > bound + 1e-7 for v in ds.bounds):
        raise ValueError('Display outside Web Mercator world bounds')
    valid = ~np.ma.getmaskarray(values)
    indices = np.searchsorted(breaks, values.data, side='right')
    rgba = np.zeros((*values.shape, 4), dtype='uint8')
    counts = []
    for i, color in enumerate(colors):
        selected = valid & (indices == i)
        rgba[selected] = [int(color[j:j+2], 16) for j in (1, 3, 5)] + [255]
        counts.append(int(selected.sum()))
    target = stage / 'display.png'
    Image.fromarray(rgba).save(target)
    with Image.open(target) as decoded:
        if not np.array_equal(np.asarray(decoded.convert('RGBA')), rgba):
            raise ValueError('Display PNG readback differs')
    left, bottom, right, top = ds.bounds
    xs, ys = transform(ds.crs, 'EPSG:4326', [left, right, right, left], [top, top, bottom, bottom])
    return {'artifact': 'display.png', 'artifact_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'coordinates': [list(p) for p in zip(xs, ys)], 'unit': parameters['unit'],
            'status': parameters['status'], 'breaks': breaks, 'colors': colors,
            'interval_rule': 'lower inclusive, upper exclusive; first/last unbounded',
            'class_pixels': counts, 'valid_pixels': int(valid.sum()),
            'nodata_pixels': int((~valid).sum()), 'nodata_display': 'transparent; unknown, not zero',
            'display_crs': 'EPSG:3857', 'resampling': 'none; source grid retained'}


ENCODINGS = ('class-index', 'value-rgb24')
RGB24 = 2 ** 24 - 1


def _corners(ds):
    left, bottom, right, top = ds.bounds
    xs, ys = transform(ds.crs, 'EPSG:4326', [left, right, right, left], [top, top, bottom, bottom])
    return [list(p) for p in zip(xs, ys)]


def display_values(ds, values, parameters, stage):
    """Uncoloured display grid for a renderer that colours by theme (contract display-raster v1).

    class-index: R + 256*G is the scheme class id, nearest sampling. value-rgb24: offset + scale *
    (R*65536 + G*256 + B), linear sampling. Alpha 0 marks NoData in both; the scheme travels alongside.
    """
    from _scheme import build, class_index
    if ds.crs.to_epsg() != 3857:
        raise ValueError('Display requires EPSG:3857; warp explicitly before encoding')
    bound = 20037508.342789244
    if any(abs(v) > bound + 1e-7 for v in ds.bounds):
        raise ValueError('Display outside Web Mercator world bounds')
    if 'colors' in parameters:
        raise ValueError('values mode carries no colours; the renderer colours by theme from the scheme')
    encoding = parameters.get('encoding', 'class-index')
    if encoding not in ENCODINGS:
        raise ValueError(f'encoding must be one of {", ".join(ENCODINGS)}')
    valid = ~np.ma.getmaskarray(values)
    data = values.data
    scheme = build(data[valid], parameters, field=f"band {parameters.get('band', 1)}")
    rgba = np.zeros((*values.shape, 4), dtype='uint8')
    rgba[..., 3] = np.where(valid, 255, 0)
    extra = {}
    if encoding == 'class-index':
        ids = np.zeros(values.shape, dtype='int64')
        ids[valid] = class_index(data[valid], scheme)
        if np.any(ids < 0):
            raise ValueError('Values outside the listed categories cannot be class-index encoded')
        rgba[..., 0] = ids & 255
        rgba[..., 1] = ids >> 8
        sampling = 'nearest'
    else:
        lo, hi = scheme['domain'] if scheme.get('domain') else (0.0, 0.0)
        scale = (hi - lo) / RGB24 if hi > lo else 1.0
        q = np.zeros(values.shape, dtype='int64')
        q[valid] = np.clip(np.rint((data[valid] - lo) / scale), 0, RGB24).astype('int64')
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = q >> 16, (q >> 8) & 255, q & 255
        sampling = 'linear'
        extra = {'offset': lo, 'scale': scale, 'quantization': scale / 2 if hi > lo else 0.0}
    target = stage / 'values.png'
    Image.fromarray(rgba).save(target)
    with Image.open(target) as decoded:
        if not np.array_equal(np.asarray(decoded.convert('RGBA')), rgba):
            raise ValueError('Values PNG readback differs')
    (stage / 'scheme.json').write_text(json.dumps(scheme, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    contract = {'contract': 'display-raster', 'schema_version': 1, 'image': 'values.png',
                'image_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'width': ds.width, 'height': ds.height,
                'encoding': encoding, **extra, 'nodata': 'alpha 0', 'sampling': sampling,
                'coordinates': _corners(ds), 'crs': 'EPSG:3857', 'scheme': 'scheme.json',
                'scheme_sha256': hashlib.sha256((stage / 'scheme.json').read_bytes()).hexdigest(),
                'unit': scheme['unit'], 'status': scheme['status'], 'record': 'record.json'}
    (stage / 'raster.json').write_text(json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return {'artifact': 'values.png', 'artifact_sha256': contract['image_sha256'], 'display_raster': 'raster.json',
            'class_scheme': 'scheme.json', 'encoding': encoding, 'coordinates': contract['coordinates'],
            'unit': scheme['unit'], 'status': scheme['status'], 'scheme_kind': scheme['kind'],
            'class_pixels': scheme['counts'], 'valid_pixels': int(valid.sum()), 'nodata_pixels': int((~valid).sum()),
            'nodata_display': 'alpha 0; unknown, not zero', 'display_crs': 'EPSG:3857',
            'resampling': 'none; source grid retained', **extra}
