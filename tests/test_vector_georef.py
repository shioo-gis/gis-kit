"""vector-georef: fit / define / apply of checked four-parameter and affine transforms for local-grid vectors."""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
# A city grid to CGCS2000 CM 114E: rotate 1.5 degrees, scale 1.00002, shift into the national grid.
ANGLE, SCALE, SHIFT = math.radians(1.5), 1.00002, (490_000.0, 2_480_000.0)
TRUTH = np.array([[SCALE*math.cos(ANGLE), -SCALE*math.sin(ANGLE), SHIFT[0]],
                  [SCALE*math.sin(ANGLE), SCALE*math.cos(ANGLE), SHIFT[1]]])


def truth(xy):
    xy = np.atleast_2d(xy)
    return xy @ TRUTH[:, :2].T + TRUTH[:, 2]


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPTS/'vector-georef.py'), *map(str, args)], capture_output=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout+result.stderr
    return result


def points_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'role', 'src_x', 'src_y', 'dst_x', 'dst_y', 'note'])
        writer.writerows(rows)
    return path


def control(path, noise=0.02, roles=('fit',)*4 + ('check',)*2, seed=7):
    """Corners and two inner points of a 2 km square, with noise in metres on the target side."""
    src = np.array([[0, 0], [2000, 0], [2000, 2000], [0, 2000], [600, 1300], [1500, 700]], dtype=float)[:len(roles)]
    dst = truth(src) + np.random.default_rng(seed).normal(0, noise, src.shape)
    return points_csv(path, [[f'P{i}', role, *s, *d, '道路中线交点'] for i, (role, s, d) in enumerate(zip(roles, src, dst))])


FIT = ('--target-crs', 'EPSG:4547', '--basis', '6 个道路中线交点，取自 CGCS2000 现状地形图')


def test_fit_helmert4_recovers_the_transform(tmp_path):
    cli('fit', '--gcp', control(tmp_path/'gcp.csv'), *FIT, '--max-check-rmse', 0.5, '--output', tmp_path/'t')
    report = json.loads((tmp_path/'t'/'transform.json').read_text(encoding='utf-8'))
    assert report['model'] == 'helmert4' and report['origin'] == 'fitted' and report['target_crs'] == 'EPSG:4547'
    assert report['parameters']['rotation_deg'] == pytest.approx(1.5, abs=2e-3)  # 2 cm noise over a 2 km baseline
    assert report['parameters']['scale_x'] == pytest.approx(SCALE, abs=1e-5) == report['parameters']['scale_y']
    assert report['fit']['count'] == 4 and report['check']['count'] == 2 and report['check']['rmse_m'] < 0.1
    assert report['acceptance']['status'] == 'pass' and report['fit_hull']['type'] == 'Polygon'
    assert [p['id'] for p in report['control_points']['points']] == [f'P{i}' for i in range(6)]
    assert (tmp_path/'t'/'residuals.png').stat().st_size > 10_000


def test_fit_holds_on_threshold_and_apply_needs_allow_hold(tmp_path):
    cli('fit', '--gcp', control(tmp_path/'gcp.csv', noise=0.3), *FIT, '--max-check-rmse', 0.05, '--output', tmp_path/'t')
    report = json.loads((tmp_path/'t'/'transform.json').read_text(encoding='utf-8'))
    assert report['acceptance']['status'] == 'hold' and 'Check RMSE' in report['acceptance']['reasons'][0]
    data = tmp_path/'data.gpkg'
    gpd.GeoDataFrame({'n': [1]}, geometry=[box(100, 100, 200, 200)]).to_file(data, layer='faces')
    assert 'on hold' in cli('apply', data, '--transform', tmp_path/'t'/'transform.json', '--output', tmp_path/'a', success=False).stderr
    cli('apply', data, '--transform', tmp_path/'t'/'transform.json', '--allow-hold', '--output', tmp_path/'b')
    assert json.loads((tmp_path/'b'/'manifest.json').read_text(encoding='utf-8'))['allow_hold'] is True


def test_apply_moves_every_layer_and_marks_extrapolation(tmp_path):
    cli('fit', '--gcp', control(tmp_path/'gcp.csv', noise=0.0), *FIT, '--max-check-rmse', 0.5, '--output', tmp_path/'t')
    data = tmp_path/'faces.gpkg'
    with pytest.warns(UserWarning):
        gpd.GeoDataFrame({'face_id': [1, 2]}, geometry=[box(100, 100, 300, 300), box(1900, 1900, 2300, 2300)]).to_file(data, layer='faces')
    with pytest.warns(UserWarning):
        gpd.GeoDataFrame({'kind': ['gap']}, geometry=[LineString([(500, 500), (500.3, 500)])]).to_file(data, layer='qa_gaps')
    cli('apply', data, '--transform', tmp_path/'t'/'transform.json', '--output', tmp_path/'out')
    manifest = json.loads((tmp_path/'out'/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['layers'] == {'faces': 2, 'qa_gaps': 1} and manifest['extrapolated'] == {'faces': 1, 'qa_gaps': 0}
    faces = gpd.read_file(tmp_path/'out'/'faces.gpkg', layer='faces')
    assert faces.crs.to_epsg() == 4547 and list(faces['extrapolated']) == [False, True]
    moved = shapely.get_coordinates(faces.geometry.iloc[0])
    assert np.allclose(moved, truth(shapely.get_coordinates(box(100, 100, 300, 300))), atol=1e-6)
    assert faces.geometry.iloc[0].area == pytest.approx(200*200*SCALE**2, rel=1e-9)
    gap = gpd.read_file(tmp_path/'out'/'faces.gpkg', layer='qa_gaps')
    assert gap['kind'].iloc[0] == 'gap' and gap.geometry.iloc[0].length == pytest.approx(0.3*SCALE, rel=1e-6)


def test_define_official_parameters(tmp_path):
    official = ('define', '--origin', 'official', '--tx', SHIFT[0], '--ty', SHIFT[1], '--scale', SCALE, '--rotation', 1.5, *FIT)
    cli(*official, '--output', tmp_path/'unchecked')
    report = json.loads((tmp_path/'unchecked'/'transform.json').read_text(encoding='utf-8'))
    assert report['origin'] == 'official' and report['acceptance']['status'] == 'hold'
    assert np.allclose(report['matrix'], TRUTH)
    checks = control(tmp_path/'gcp.csv', noise=0.01)
    cli(*official, '--gcp', checks, '--max-check-rmse', 0.05, '--output', tmp_path/'checked')
    report = json.loads((tmp_path/'checked'/'transform.json').read_text(encoding='utf-8'))
    assert report['acceptance']['status'] == 'pass' and report['check']['count'] == 6 and report['fit']['count'] == 0
    assert report['fit_hull'] is None
    matrix = ','.join(str(v) for v in TRUTH.ravel())
    cli('define', '--origin', 'user', '--model', 'affine6', '--matrix', matrix, *FIT, '--output', tmp_path/'affine')
    assert np.allclose(json.loads((tmp_path/'affine'/'transform.json').read_text(encoding='utf-8'))['matrix'], TRUTH)


def test_refusals(tmp_path):
    out = iter(tmp_path/f'o{i}' for i in range(20))
    few = control(tmp_path/'few.csv', roles=('fit', 'fit', 'check'))
    assert 'at least 3 fit points' in cli('fit', '--gcp', few, *FIT, '--max-check-rmse', 1, '--output', next(out), success=False).stderr
    four = control(tmp_path/'four.csv', roles=('fit',)*3 + ('check',))
    assert 'at least 4 fit points' in cli('fit', '--gcp', four, '--model', 'affine6', *FIT, '--max-check-rmse', 1,
                                          '--output', next(out), success=False).stderr
    line = points_csv(tmp_path/'line.csv', [[f'L{i}', 'fit', i*100, 0, *truth([i*100, 0])[0]] for i in range(4)] +
                      [['C', 'check', 50, 50, *truth([50, 50])[0]]])
    assert 'collinear' in cli('fit', '--gcp', line, '--model', 'affine6', *FIT, '--max-check-rmse', 1,
                              '--output', next(out), success=False).stderr
    assert '--max-check-rmse' in cli('fit', '--gcp', control(tmp_path/'g.csv'), *FIT, '--output', next(out), success=False).stderr
    assert 'projected CRS in metres' in cli('fit', '--gcp', control(tmp_path/'h.csv'), '--target-crs', 'EPSG:4490', '--basis', 'x',
                                            '--max-check-rmse', 1, '--output', next(out), success=False).stderr
    bad = points_csv(tmp_path/'bad.csv', [['A', 'fit', 0, 0, 0, 0], ['A', 'check', 1, 1, 1, 1]])
    assert 'unique' in cli('fit', '--gcp', bad, *FIT, '--max-check-rmse', 1, '--output', next(out), success=False).stderr
    role = points_csv(tmp_path/'role.csv', [['A', 'train', 0, 0, 0, 0]])
    assert 'fit or check' in cli('fit', '--gcp', role, *FIT, '--max-check-rmse', 1, '--output', next(out), success=False).stderr
    cli('fit', '--gcp', control(tmp_path/'ok.csv'), *FIT, '--max-check-rmse', 1, '--output', tmp_path/'t')
    placed = tmp_path/'placed.gpkg'
    gpd.GeoDataFrame({'n': [1]}, geometry=[Point(1, 1)], crs='EPSG:4547').to_file(placed)
    assert 'expects no CRS' in cli('apply', placed, '--transform', tmp_path/'t'/'transform.json', '--output', next(out),
                                   success=False).stderr
    assert not any(p.exists() for p in tmp_path.glob('o[0-9]*'))
