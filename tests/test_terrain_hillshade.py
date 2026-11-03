"""Analytical hillshade from terrain.py: values only, oriented by light azimuth/altitude."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
P = dict(vertical_unit='metre', vertical_datum='unknown', source_description='synthetic plane')


def plane(path, east=0., north=0., size=12):
    """Elevation rising `east` metres per metre eastwards and `north` metres per metre northwards."""
    cols, rows = np.meshgrid(np.arange(size), np.arange(size))
    z = 1000 + east*cols*10 - north*rows*10
    with rio.open(path, 'w', driver='GTiff', width=size, height=size, count=1, dtype='float64', crs='EPSG:32632',
                  transform=from_origin(500000, 5000000, 10, 10), nodata=np.nan) as d:
        d.write(z, 1)
    return path


def run(tmp_path, dem, **params):
    (tmp_path/'p.json').write_text(json.dumps({**P, **params}), encoding='utf-8')
    out = tmp_path/'out'
    result = subprocess.run([sys.executable, str(SCRIPTS/'terrain.py'), str(dem), '--params', str(tmp_path/'p.json'), '--output', str(out)],
                            capture_output=True, encoding='utf-8', errors='replace')
    return result, out


def centre(path):
    with rio.open(path) as d:
        a = d.read(1)
        return a, d.units[0]


def test_flat_ground_receives_sin_altitude(tmp_path):
    result, out = run(tmp_path, plane(tmp_path/'flat.tif'), hillshade={'altitude': 30})
    assert result.returncode == 0, result.stderr
    shade, unit = centre(out/'hillshade.tif')
    assert unit == 'ratio'
    assert np.isnan(shade[0, 0]) and np.isnan(shade[-1, -1]), 'incomplete 3x3 neighbourhoods stay NoData'
    assert np.allclose(shade[1:-1, 1:-1], 0.5)
    record = json.loads((out/'record.json').read_text(encoding='utf-8'))
    assert record['hillshade'] == {'azimuth': 315., 'altitude': 30, 'z_factor': 1.}
    assert record['artifacts']['hillshade']['unit'] == 'ratio'


def test_slopes_facing_the_light_are_brighter_and_z_factor_steepens(tmp_path):
    shades = {}
    # Rising to the south-east means facing north-west, towards the default 315° light.
    for name, east, north, z_factor in [('lit', .5, -.5, 1.), ('dark', -.5, .5, 1.), ('steep', -.5, .5, 3.)]:
        (tmp_path/name).mkdir()
        result, out = run(tmp_path/name, plane(tmp_path/name/'dem.tif', east, north), hillshade={'z_factor': z_factor})
        assert result.returncode == 0, result.stderr
        shades[name] = centre(out/'hillshade.tif')[0][5, 5]
    assert shades['lit'] > np.sin(np.radians(45)) > shades['dark'] > shades['steep']


def test_hillshade_is_optional_and_validated(tmp_path):
    result, out = run(tmp_path, plane(tmp_path/'flat.tif'))
    assert result.returncode == 0 and not (out/'hillshade.tif').exists()
    (tmp_path/'x').mkdir()
    bad, _ = run(tmp_path/'x', plane(tmp_path/'x'/'dem.tif'), hillshade={'altitude': 120})
    assert bad.returncode == 1 and 'altitude' in bad.stderr
