"""Raster windows: independent full-array references, window I/O guards and atomic failures."""
import argparse
import hashlib
import json

import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box

from test_analysis_extensions import tif, rcli, raster
from test_daily import frame, write
from test_raster_numeric import direct
import _raster_numeric as n


@pytest.mark.parametrize('size', [1, 3, 16])
@pytest.mark.parametrize('fmt', ['GTiff', 'COG'])
def test_difference_index_where_chain(tmp_path, size, fmt):
    a = np.arange(35, dtype=float).reshape(5, 7)
    b = np.flip(a, axis=1).copy()
    a[1, 3] = np.nan
    b[3, 1] = np.nan
    a[4, 6] = b[4, 6] = 0
    first, second = tif(tmp_path/'a.tif', a), tif(tmp_path/'b.tif', b)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (first, second)]
    common = {'block_size': size, 'format': fmt, 'max_elements': 3*min(size, 5)*min(size, 7)}
    diff, dr = direct(tmp_path, 'combine', [first, second], dict(common, method='subtract'))
    total, _ = direct(tmp_path, 'combine', [first, second], dict(common, method='add'))
    index, ir = direct(tmp_path, 'combine', [diff/'result.tif', total/'result.tif'], dict(common, method='divide', invalid='nodata'))
    condition, _ = direct(tmp_path, 'combine', [first, second], dict(common, method='gt'))
    result, _ = direct(tmp_path, 'combine', [condition/'result.tif', index/'result.tif', first], dict(common, method='where'))
    expected_diff = a-b
    with np.errstate(all='ignore'):
        expected_index = (a-b)/(a+b)
    valid = np.isfinite(a) & np.isfinite(b)
    expected = np.where(valid, np.where(a > b, expected_index, a), np.nan)
    for folder, ref in [(diff, expected_diff), (index, expected_index), (result, expected)]:
        with rio.open(folder/'result.tif') as ds:
            np.testing.assert_equal(ds.read(1), ref)
            np.testing.assert_equal(ds.read_masks(1) > 0, np.isfinite(ref))
            if fmt == 'COG':
                assert ds.tags(ns='IMAGE_STRUCTURE')['LAYOUT'] == 'COG'
    assert dr['decoded_sha256'] == hashlib.sha256(expected_diff.astype('<f8').tobytes()).hexdigest()
    assert ir['execution']['mode'] == 'windowed'
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in (first, second)]


@pytest.mark.parametrize('size', [1, 3, 16])
def test_calibration_multiband_split_mask_and_canonical_hash(tmp_path, size):
    data = np.arange(70, dtype='int16').reshape(2, 5, 7)
    mask = np.full((5, 7), 255, dtype='uint8'); mask[2, 3] = 0
    source = tmp_path/'source.tif'
    with rio.Env(GDAL_TIFF_INTERNAL_MASK=True), rio.open(source, 'w', driver='GTiff',
            count=2, width=7, height=5, dtype='int16', crs='EPSG:3857', transform=from_origin(0, 5, 1, 1)) as ds:
        ds.write(data); ds.write_mask(mask); ds.scales = (.5, 2); ds.offsets = (-1, 3)
    p = {'operands': [{'input': 0, 'band': 2}, {'input': 0, 'band': 1, 'scale': 3, 'offset': 0}],
         'calibrate': True, 'block_size': size, 'dtype': 'float32'}
    expected = np.array([data[1]*2+3, data[0]*3], dtype='float32'); expected[:, 2, 3] = np.nan
    folder, info = direct(tmp_path, 'bands', [source], p)
    with rio.open(folder/'result.tif') as ds:
        np.testing.assert_equal(ds.read(), expected)
        assert ds.scales == (1., 1.) and ds.offsets == (0., 0.)
    assert info['decoded_sha256'] == hashlib.sha256(expected.astype('<f4').tobytes()).hexdigest()
    folder, info = direct(tmp_path, 'bands', [source], dict(p, split=True, format='COG'))
    for i, artifact in enumerate(info['artifacts']):
        with rio.open(folder/artifact['artifact']) as ds:
            np.testing.assert_equal(ds.read(1), expected[i])
            assert ds.descriptions == (f'operand_{i+1}',)


@pytest.mark.parametrize('method', ['fill', 'assign', 'replace'])
@pytest.mark.parametrize('coverage', ['center', 'all_touched'])
@pytest.mark.parametrize('bounds', [(1.2, -2.2, 5.2, .8), (3, -1, 6, 2)])
def test_update_window_edges(tmp_path, method, coverage, bounds):
    a = np.arange(35, dtype=float).reshape(5, 7); a[1, 1] = np.nan
    b = np.full_like(a, 9); b[2, 3] = np.nan
    first, second = tif(tmp_path/'a.tif', a), tif(tmp_path/'b.tif', b)
    geometry = box(*bounds)
    vector = write(tmp_path, 'mask', frame([geometry], id=['a']))
    from rasterio.features import geometry_mask
    mask = geometry_mask([geometry], a.shape, from_origin(0, 2, 1, 1), invert=True, all_touched=coverage == 'all_touched')
    expected = a.copy()
    if method == 'fill': expected = np.where(np.isfinite(a), a, b)
    elif method == 'assign': expected[mask] = 0
    else: expected[mask] = b[mask]
    for size in [1, 3, 16]:
        folder, _ = direct(tmp_path, 'update', [first] if method == 'assign' else [first, second],
            {'method': method, 'vector': str(vector), 'coverage': coverage, 'value': 0, 'block_size': size})
        with rio.open(folder/'result.tif') as ds:
            np.testing.assert_equal(ds.read(1), expected)


@pytest.mark.parametrize('method,params,reference', [
    ('abs', {}, lambda a: np.abs(a)),
    ('clip', {'min': 0, 'max': 10}, lambda a: np.clip(a, 0, 10)),
    ('power', {'exponent': 2}, lambda a: a*a),
    ('rules', {'mapping': {'0': 100}, 'unmapped': 'keep'}, lambda a: np.where(a == 0, 100, a)),
])
def test_local_transform(tmp_path, method, params, reference):
    a = np.arange(-12, 23, dtype=float).reshape(5, 7); a[4, 6] = np.nan
    source = tif(tmp_path/'a.tif', a)
    folder, record = direct(tmp_path, 'transform', [source], dict(params, method=method, block_size=3, max_elements=9))
    with rio.open(folder/'result.tif') as ds: np.testing.assert_equal(ds.read(1), reference(a))
    assert record['execution']['mode'] == 'windowed'
    with pytest.raises(ValueError, match='max_elements'):
        direct(tmp_path, 'transform', [source], {'method': 'auto_class', 'mode': 'quantile', 'classes': 2, 'max_elements': 9})


def test_window_read_write_budget_including_readback(tmp_path, monkeypatch):
    source = tif(tmp_path/'a.tif', np.zeros((13, 19)))
    original = rio.open
    events = []
    class Checked:
        def __init__(self, ds): self.ds = ds
        def __getattr__(self, name): return getattr(self.ds, name)
        def __enter__(self): self.ds.__enter__(); return self
        def __exit__(self, *args): return self.ds.__exit__(*args)
        def check(self, kw):
            win = kw.get('window'); assert win is not None
            assert win.width*win.height <= 9
            events.append(win)
        def read(self, *args, **kw): self.check(kw); return self.ds.read(*args, **kw)
        def write(self, *args, **kw): self.check(kw); return self.ds.write(*args, **kw)
    monkeypatch.setattr(rio, 'open', lambda *a, **kw: Checked(original(*a, **kw)))
    direct(tmp_path, 'combine', [source, source], {'method': 'sum', 'block_size': 3, 'max_elements': 18})
    assert len(events) > 100


@pytest.mark.parametrize('case', ['zero', 'band', 'grid', 'rounding', 'conflict', 'overflow', 'calibration'])
def test_late_and_early_cli_failures_leave_no_bundle(tmp_path, case):
    a = np.ones((5, 7)); a[4, 6] = 0
    first, second = tif(tmp_path/'a.tif', a), tif(tmp_path/'b.tif', np.ones_like(a))
    operation, sources, p = 'combine', [second, first], {'method': 'divide', 'block_size': 3}
    if case == 'band': p['operands'] = [{'input': 0, 'band': 2}]
    if case == 'grid':
        second = tif(tmp_path/'shifted.tif', a, from_origin(.5, 2, 1, 1)); sources = [first, second]
    if case in ('rounding', 'conflict', 'overflow'):
        a[4, 6] = {'rounding': .5, 'conflict': 255, 'overflow': 256}[case]
        first = tif(tmp_path/'typed.tif', a); operation, sources = 'bands', [first]
        p.update(dtype='uint8', nodata_value=255)
    if case == 'calibration':
        with rio.open(first, 'r+') as ds: ds.scales = (2,)
        operation, sources = 'bands', [first]
    rcli(tmp_path, 'failed', operation, sources, p, success=False)
    assert not list(tmp_path.glob('.raster-*'))


@pytest.mark.parametrize('failure', ['write', 'cog', 'readback'])
def test_injected_failure_no_publication(tmp_path, monkeypatch, failure):
    source = tif(tmp_path/'a.tif', np.ones((5, 7)))
    spec = tmp_path/'params.json'; spec.write_text(json.dumps({'block_size': 3, 'format': 'COG' if failure == 'cog' else 'GTiff'}), encoding='utf-8')
    if failure == 'cog':
        import rasterio.shutil
        def broken(src, dst, **kw):
            dst.write_bytes(b'partial'); raise OSError('injected COG failure')
        monkeypatch.setattr(rasterio.shutil, 'copy', broken)
    else:
        original = rio.open
        class Broken:
            def __init__(self, ds): self.ds = ds; self.calls = 0
            def __getattr__(self, name): return getattr(self.ds, name)
            def __enter__(self): self.ds.__enter__(); return self
            def __exit__(self, *args): return self.ds.__exit__(*args)
            def write(self, *a, **kw):
                self.ds.write(*a, **kw); self.calls += 1
                if failure == 'write' and self.calls == 2: raise OSError('injected partial write')
            def read(self, *a, **kw):
                data = self.ds.read(*a, **kw)
                if failure == 'readback' and str(self.ds.name).endswith('result.tif'): data.flat[0] += 1
                return data
        monkeypatch.setattr(rio, 'open', lambda *a, **kw: Broken(original(*a, **kw)))
    with pytest.raises((ValueError, OSError)):
        raster.execute(argparse.Namespace(operation='bands', input=[str(source)], params=str(spec), output=str(tmp_path/'result')))
    assert not (tmp_path/'result').exists()
    assert not list(tmp_path.glob('.raster-*'))


def test_typed_window_encoding_error_and_rounding(tmp_path):
    a = np.linspace(0, 3, 35).reshape(5, 7); a[4, 6] = np.nan
    source = tif(tmp_path/'a.tif', a)
    hashes = []
    for size in [1, 3, 16]:
        folder, record = direct(tmp_path, 'bands', [source], {'dtype': 'float32', 'block_size': size})
        with rio.open(folder/'result.tif') as ds:
            np.testing.assert_equal(ds.read(1), a.astype('float32'))
        assert record['max_abs_encoding_error'] == np.nanmax(np.abs(a.astype('float32').astype(float)-a))
        hashes.append(record['decoded_sha256'])
        folder, _ = direct(tmp_path, 'bands', [source], {'dtype': 'uint8', 'nodata_value': 255, 'rounding': 'nearest_even', 'block_size': size})
        with rio.open(folder/'result.tif') as ds:
            np.testing.assert_equal(ds.read(1), np.where(np.isfinite(a), np.rint(a), 255).astype('uint8'))
    assert len(set(hashes)) == 1
