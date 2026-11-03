"""raster.py warp with lon/lat bounds reads only the covering source window."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import rasterio as rio
from rasterio.transform import from_origin

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'


def world(path):
    # 0.1° global grid (3600x1800); value = column index, so the crop's columns are checkable.
    with rio.open(path, 'w', driver='GTiff', width=3600, height=1800, count=1, dtype='float32', crs='EPSG:4326',
                  transform=from_origin(-180, 90, .1, .1), nodata=np.nan) as d:
        d.write(np.broadcast_to(np.arange(3600, dtype='float32'), (1800, 3600)), 1)
    return path


def warp(tmp_path, src, **params):
    (tmp_path/'p.json').write_text(json.dumps({'kind': 'continuous', 'resampling': 'nearest', **params}), encoding='utf-8')
    out = tmp_path/'out'
    result = subprocess.run([sys.executable, str(SCRIPTS/'raster.py'), 'warp', str(src), '--params', str(tmp_path/'p.json'), '--output', str(out)],
                            capture_output=True, text=True)
    return result, out


def test_bounds_window_crop_and_guard(tmp_path):
    src = world(tmp_path/'world.tif')
    result, out = warp(tmp_path, src, crs='EPSG:4326', bounds=[6.0, 45.8, 10.5, 47.8], max_pixels=10000)
    assert result.returncode == 0, result.stderr
    with rio.open(out/'result.tif') as d:
        left, bottom, right, top = d.bounds
        assert left <= 6.0 and right >= 10.5 and bottom <= 45.8 and top >= 47.8
        assert right - left < 5.0 and top - bottom < 2.5, 'only the covering window, one pixel of margin'
        values = d.read(1)
        # 6.0°E is the left edge of column 1860; 10.5°E lies in column 1905, plus one column of margin.
        assert values.min() == 1860 and values.max() in (1905, 1906)
    (tmp_path/'w').mkdir()
    whole, _ = warp(tmp_path/'w', src, crs='EPSG:4326', max_pixels=10000)
    assert whole.returncode == 1 and 'max_pixels' in whole.stderr
    (tmp_path/'b').mkdir()
    outside, _ = warp(tmp_path/'b', src, crs='EPSG:4326', bounds=[10, 50, 5, 60])
    assert outside.returncode == 1 and 'bounds' in outside.stderr
