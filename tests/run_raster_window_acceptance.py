#!/usr/bin/env python3
"""Real elevation contrast chain and separate-process window memory workloads.

Source is a supplied local elevation sample; contrast to 2000 m is diagnostic,
not temporal change or a vegetation index. Benchmark grids are synthetic.
All generated evidence is written to the supplied new directory outside the repo.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for data in iter(lambda: f.read(1024*1024), b''): h.update(data)
    return h.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('--source-description', required=True)
    parser.add_argument('--source-record', required=True, help='Existing provenance JSON, copied unchanged')
    args = parser.parse_args()
    source, root = Path(args.source).resolve(), Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    logs = root/'logs'; logs.mkdir()
    before = digest(source)
    shutil.copy2(source, root/'source.tif')
    shutil.copy2(args.source_record, root/'source-record.json')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    calls = []
    def call(name, operation, inputs, params, measured=False):
        spec = root/(name+'.json'); save(spec, params)
        dest = root/name
        cmd = [sys.executable, str(SCRIPTS/'raster.py'), operation, *map(str, inputs), '--params', str(spec), '--output', str(dest)]
        # Same interpreter/process runs the actual CLI; getrusage includes imports,
        # reads, computation, encoding, COG conversion and verification.
        if measured:
            wrapper = ("import runpy,resource,sys,os; script=sys.argv.pop(1); sys.path.insert(0,os.path.dirname(script)); "
                       "runpy.run_path(script,run_name='__main__'); "
                       "print('PEAK_RSS_BYTES='+str(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*"
                       "(1 if sys.platform=='darwin' else 1024)),file=sys.stderr)")
            cmd = [sys.executable, '-c', wrapper, *cmd[1:]]
        start = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        seconds = time.monotonic()-start
        item = dict(name=name, command=cmd, returncode=result.returncode, seconds=seconds,
                    stdout=result.stdout, stderr=result.stderr)
        if measured:
            match = re.search(r'PEAK_RSS_BYTES=(\d+)', result.stderr)
            if not match:
                save(logs/(name+'.json'), item)
                raise RuntimeError('Missing peak RSS; see command log')
            item['peak_rss_bytes'] = int(match[1])
        save(logs/(name+'.json'), item); calls.append(item)
        if result.returncode: raise RuntimeError(f'{name} failed: {result.stderr[-1500:]}')
        return dest/'result.tif', json.loads((dest/'record.json').read_text(encoding='utf-8'))
    source = root/'source.tif'
    with rio.open(source) as ds:
        data = ds.read(1, masked=True).astype(float).filled(np.nan)
        transform, crs, bounds = ds.transform, ds.crs, ds.bounds
        assert ds.scales[0] == 1 and ds.offsets[0] == 0
    data[~np.isfinite(data)] = np.nan
    threshold = 2000.
    reference = np.where(np.isfinite(data), threshold, np.nan)
    expected_diff = data-reference
    with np.errstate(all='ignore'): expected_index = expected_diff/(data+reference)
    expected_index[~np.isfinite(expected_index)] = np.nan
    expected_where = np.where(np.isfinite(data), np.where(data > threshold, expected_index, 0.), np.nan)
    zones = gpd.GeoDataFrame({'id': ['west', 'east']}, crs=crs, geometry=[
        box(bounds.left, bounds.bottom, (bounds.left+bounds.right)/2, bounds.top),
        box((bounds.left+bounds.right)/2, bounds.bottom, bounds.right, bounds.top)])
    zones.to_file(root/'zones.gpkg', driver='GPKG', layer='zones')
    verified = []
    last = {}
    for size in (17, 64, 256):
        p = {'block_size': size, 'format': 'COG'}
        stem = f'window-{size}'
        constant, _ = call(stem+'-reference', 'bands', [source], dict(p, calibrate=True, operands=[{'input': 0, 'band': 1, 'scale': 0, 'offset': threshold}]))
        difference, _ = call(stem+'-difference', 'combine', [source, constant], dict(p, method='subtract'))
        total, _ = call(stem+'-sum', 'combine', [source, constant], dict(p, method='add'))
        index, _ = call(stem+'-index', 'combine', [difference, total], dict(p, method='divide', invalid='nodata'))
        condition, _ = call(stem+'-condition', 'combine', [source, constant], dict(p, method='gt'))
        zero, _ = call(stem+'-zero', 'bands', [source], dict(p, calibrate=True, operands=[{'input': 0, 'scale': 0, 'offset': 0}]))
        conditional, _ = call(stem+'-conditional', 'combine', [condition, index, zero], dict(p, method='where'))
        for name, file, expected in [('difference', difference, expected_diff), ('index', index, expected_index), ('conditional', conditional, expected_where)]:
            with rio.open(file) as ds:
                np.testing.assert_equal(ds.read(1), expected)
                np.testing.assert_equal(ds.read_masks(1) > 0, np.isfinite(expected))
                assert ds.crs == crs and ds.transform == transform
                assert ds.tags(ns='IMAGE_STRUCTURE')['LAYOUT'] == 'COG'
            record = json.loads((file.parent/'record.json').read_text(encoding='utf-8'))
            if name in last: assert last[name] == record['decoded_sha256']
            last[name] = record['decoded_sha256']
            verified.append(dict(name=name, block_size=size, decoded_sha256=last[name], file=str(file.relative_to(root))))
        _, table = call(stem+'-zonal', 'zonal', [index], dict(vector=str(root/'zones.gpkg'), layer='zones', id='id', method='center', block_size=size))
        # Independent coordinate containment; no rasterization/shared zonal reducer.
        rr, cc = np.indices(data.shape)
        xx = transform.c+(cc+.5)*transform.a; yy = transform.f+(rr+.5)*transform.e
        for geometry, row in zip(zones.geometry, table['zones']):
            left, bottom, right, top = geometry.bounds
            selected = (xx > left) & (xx <= right) & (yy >= bottom) & (yy < top) & np.isfinite(expected_index)
            values = expected_index[selected]
            assert row['valid_pixels'] == len(values)
            np.testing.assert_allclose(row['sum'], values.sum(), rtol=1e-12, atol=1e-9)
            np.testing.assert_allclose(row['mean'], values.mean(), rtol=1e-12, atol=1e-12)
    assert digest(source) == before and digest(args.source) == before
    # Diagnostic inspection only, not a formal Composer map.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), layout='constrained')
    for ax, values, title in zip(axes, [data, expected_diff, expected_where], ['Source elevation (m)', 'Elevation minus 2000 m', 'Contrast above 2000 m (unitless)']):
        im = ax.imshow(values, cmap='viridis'); ax.set_title(title); ax.set_axis_off(); fig.colorbar(im, ax=ax, shrink=.7)
    fig.suptitle('Local GEBCO-labelled sample | candidate | diagnostic contrast, not temporal change')
    fig.savefig(root/'diagnostic.png', dpi=150); plt.close(fig)
    benchmarks = []
    for side in (1024, 4096):
        path = root/f'synthetic-{side}.tif'
        with rio.open(path, 'w', driver='GTiff', width=side, height=side, count=2, dtype='float32',
                crs='EPSG:3857', transform=from_origin(0, side, 1, 1), tiled=True, blockxsize=256, blockysize=256,
                compress='deflate', nodata=np.nan) as ds:
            for y in range(0, side, 256):
                for x in range(0, side, 256):
                    r, c = np.indices((min(256, side-y), min(256, side-x)))
                    a = ((r+y)*3+(c+x)*7).astype('float32')/100
                    ds.write(a, 1, window=Window(x, y, a.shape[1], a.shape[0]))
                    ds.write(a*.75+1, 2, window=Window(x, y, a.shape[1], a.shape[0]))
        source_hash = digest(path)
        canonical = None
        for size in (128, 512):
            for fmt in ('GTiff', 'COG'):
                name = f'memory-{side}-{size}-{fmt}'
                result, record = call(name, 'combine', [path], dict(method='subtract', operands=[{'input': 0, 'band': 1}, {'input': 0, 'band': 2}],
                    block_size=size, format=fmt, gdal_cache_mb=32), measured=True)
                with rio.open(result) as ds:
                    for y in range(0, side, 256):
                        for x in range(0, side, 256):
                            r, c = np.indices((min(256, side-y), min(256, side-x)))
                            a = ((r+y)*3+(c+x)*7).astype('float32')/100
                            expected = a.astype(float)-(a*.75+1).astype(float)
                            np.testing.assert_equal(ds.read(1, window=Window(x, y, a.shape[1], a.shape[0])), expected)
                if canonical is not None: assert canonical == record['decoded_sha256']
                canonical = record['decoded_sha256']
                benchmarks.append(dict(side=side, pixels=side*side, operands=2, block_size=size, format=fmt,
                    gdal_cache_mb=32, peak_rss_bytes=calls[-1]['peak_rss_bytes'], seconds=calls[-1]['seconds'],
                    decoded_sha256=canonical, source_sha256=source_hash))
                # Large synthetic rasters are reproducible, not permanent evidence.
                shutil.copy2(result.parent/'record.json', logs/(name+'-record.json'))
                shutil.rmtree(result.parent)
        assert digest(path) == source_hash
        path.unlink()
    save(root/'acceptance.json', dict(source_sha256=before, source_description=args.source_description,
        original_unchanged=True, status='candidate', independent_full_array_reference=True,
        verified=verified, benchmark=benchmarks, diagnostic_png='diagnostic.png',
        limits=['Upstream identity and vertical datum unverified', '2000 m reference is constructed; no temporal change or vegetation claim',
                'Memory measurements are Darwin process peak RSS including imports/GDAL/COG; no speedup baseline',
                'Vector geometries/rules and source codec blocks are additional memory costs']))
    print(root/'acceptance.json')


if __name__ == '__main__': main()
