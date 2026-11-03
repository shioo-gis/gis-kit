"""Peak memory is bytes where the platform reports it and None where it cannot (no resource module on Windows)."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'scripts'
sys.path.insert(0, str(SCRIPTS))
import _peak_rss

RECORDERS = ['network', 'terrain', 'terrain-cost', 'terrain-detail', 'suitability', 'hydro-review', 'nonlinear-georef', 'spatial-inference', '_grass_worker', '_walk_worker']
# Setting the entry to None makes `import resource` raise ModuleNotFoundError, as it does on Windows.
NO_RESOURCE = f"import sys; sys.modules['resource'] = None; sys.path.insert(0, {str(SCRIPTS)!r})\n"


def child(code, *args):
    return subprocess.run([sys.executable, '-B', '-c', NO_RESOURCE+code, *map(str, args)], capture_output=True, text=True, encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONUTF8': '1'})


def run_script(name, *args):
    """Run a script as __main__ in a process that has no resource module."""
    run = child("import runpy; script = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(script, run_name='__main__')", SCRIPTS/f'{name}.py', *args)
    assert run.returncode == 0, run.stderr


def record(output):
    return json.loads((Path(output)/'record.json').read_text(encoding='utf-8'))


def params(path, **values):
    path.write_text(json.dumps(values), encoding='utf-8')
    return path


@pytest.mark.parametrize('platform,reported,expected', [('darwin', 4096, 4096), ('linux', 4096, 4096*1024), ('freebsd14', 8, 8192)])
def test_units_are_bytes_on_every_platform(monkeypatch, platform, reported, expected):
    fake = SimpleNamespace(RUSAGE_SELF='self', RUSAGE_CHILDREN='children', getrusage=lambda who: SimpleNamespace(ru_maxrss=reported))
    monkeypatch.setattr(_peak_rss, 'resource', fake)
    monkeypatch.setattr(_peak_rss.sys, 'platform', platform)
    assert _peak_rss.peak_rss_bytes() == expected


def test_children_asks_for_the_children_figure(monkeypatch):
    sizes = {'self': 1, 'children': 2}
    fake = SimpleNamespace(RUSAGE_SELF='self', RUSAGE_CHILDREN='children', getrusage=lambda who: SimpleNamespace(ru_maxrss=sizes[who]))
    monkeypatch.setattr(_peak_rss, 'resource', fake)
    monkeypatch.setattr(_peak_rss.sys, 'platform', 'darwin')
    assert (_peak_rss.peak_rss_bytes(), _peak_rss.peak_rss_bytes(children=True)) == (1, 2)


def test_real_figure_where_the_platform_has_one():
    pytest.importorskip('resource')
    assert _peak_rss.peak_rss_bytes() > 2**20  # an interpreter with these imports is over 1 MiB in either unit


def test_every_recording_script_imports_without_resource_and_reports_none():
    # terrain-cost and nonlinear-georef need GRASS or GDAL to reach their record, so importing is what CI can check for them.
    code = (f"import importlib.util, json\n"
            f"for name in {RECORDERS!r}:\n"
            f"    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), f'{{sys.path[0]}}/{{name}}.py')\n"
            f"    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
            f"from _peak_rss import peak_rss_bytes\n"
            f"print(json.dumps([peak_rss_bytes(), peak_rss_bytes(children=True)]))\n")
    run = child(code)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.splitlines()[-1]) == [None, None]


def test_only_the_helper_touches_resource():
    # A new top-level `import resource` would stop that script on Windows before it does anything.
    found = []
    for path in sorted(SCRIPTS.glob('*.py')):
        if path.name in ('_peak_rss.py', 'make_base.py'):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            if 'resource' in names:
                found.append(f'{path.name}:{node.lineno}')
    assert not found, found


def dem(path):
    with rio.open(path, 'w', driver='GTiff', width=5, height=5, count=1, dtype='float64', crs='EPSG:32645', transform=from_origin(500000, 3100000, 10, 10)) as d:
        d.write(np.tile(np.arange(5.), (5, 1)), 1)
    return path


SURFACE = dict(vertical_unit='metre', vertical_datum='unknown', source_description='synthetic regression')


def test_terrain_record_says_none(tmp_path):
    run_script('terrain', dem(tmp_path/'dem.tif'), '--params', params(tmp_path/'p.json', **SURFACE), '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources']['max_rss_bytes'] is None


def test_terrain_detail_record_says_none(tmp_path):
    run_script('terrain-detail', dem(tmp_path/'dem.tif'), '--params', params(tmp_path/'p.json', **SURFACE, levels_m=[1.5]), '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources']['max_rss_bytes'] is None


def test_suitability_record_says_none(tmp_path):
    source = tmp_path/'objects.gpkg'
    gpd.GeoDataFrame({'id': ['a', 'b'], 'a': [0, 10]}, geometry=[Point(0, 0), Point(10, 0)], crs=32645).to_file(source, driver='GPKG')
    p = params(tmp_path/'p.json', id='id', study='synthetic', criteria={'a': dict(field='a', low=0, high=10, prefer='high')}, scenarios=[dict(id='base', weights={'a': 1}, threshold=.5)])
    run_script('suitability', source, '--params', p, '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources']['max_rss_bytes'] is None


def test_network_record_says_none(tmp_path):
    edges, origins, facilities = tmp_path/'edges.gpkg', tmp_path/'origins.gpkg', tmp_path/'facilities.gpkg'
    gpd.GeoDataFrame({'id': ['ab'], 'u': ['a'], 'v': ['b'], 'direction': ['both'], 'speed': [10]}, geometry=[LineString([(0, 0), (10, 0)])], crs=32645).to_file(edges, driver='GPKG')
    gpd.GeoDataFrame({'id': ['start']}, geometry=[Point(0, 0)], crs=32645).to_file(origins, driver='GPKG')
    gpd.GeoDataFrame({'id': ['end']}, geometry=[Point(10, 0)], crs=32645).to_file(facilities, driver='GPKG')
    p = params(tmp_path/'p.json', edge_id='id', **{'from': 'u', 'to': 'v'}, direction='direction', speed_kmh='speed', mode='shortest', analysis_crs='EPSG:32645', turn_restrictions='not_modelled', cost_assumptions='test', origin_id='id', facility_id='id', snap_m=1, budget=12, facility_sets={'baseline': ['end']})
    run_script('network', edges, '--params', p, '--origins', origins, '--facilities', facilities, '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources']['max_rss_bytes'] is None


def test_spatial_inference_record_says_none(tmp_path):
    source = tmp_path/'points.gpkg'
    gpd.GeoDataFrame({'id': ['a', 'b']}, geometry=[Point(1, 1), Point(3, 1)], crs=32645).to_file(source, driver='GPKG')
    p = params(tmp_path/'p.json', id='id', study='synthetic', analysis_crs=32645, operation='kde', bandwidth_m=2, resolution_m=1, bounds=[0, 0, 4, 4])
    run_script('spatial-inference', source, '--params', p, '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources'] == {'self_max_rss_bytes': None, 'completed_child_max_rss_bytes': None}


def test_hydro_review_record_says_none(tmp_path):
    rivers, streams = tmp_path/'rivers.gpkg', tmp_path/'streams.tif'
    gpd.GeoDataFrame({'id': ['match']}, geometry=[LineString([(5, 5), (5, 25)])], crs=32645).to_file(rivers, driver='GPKG')
    cells = np.zeros((4, 4), dtype='uint8')
    cells[:, 0] = 1
    with rio.open(streams, 'w', driver='GTiff', width=4, height=4, count=1, dtype='uint8', transform=from_origin(0, 40, 10, 10), crs=32645) as d:
        d.write(cells, 1)
    p = params(tmp_path/'p.json', id='id', spacing_m=10, tolerance_m=0, min_matched_fraction=1, rationale='Exact synthetic stream centres')
    run_script('hydro-review', rivers, streams, '--params', p, '--output', tmp_path/'out')
    assert record(tmp_path/'out')['resources']['max_rss_bytes'] is None


@pytest.mark.parametrize('name,job,field', [
    ('_grass_worker', dict(operation='viewshed', input='dem.tif', params=dict(observers=[], target_height_m=0, max_distance_m=1)), 'child_max_rss_bytes'),
    ('_walk_worker', dict(params=dict(start=[0, 0], end=[1, 1], walk_coeff=[1, 1, 1, 1], slope_factor=-0.2, friction_lambda=1)), 'completed_child_max_rss_bytes'),
])
def test_grass_worker_report_says_none(tmp_path, name, job, field):
    # GRASS modules replaced by a stub: the point is the report the worker writes at the end.
    path = params(tmp_path/'job.json', work=str(tmp_path), **job)
    code = ("import runpy, subprocess\n"
            "from types import SimpleNamespace\n"
            "subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)\n"
            "script = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(script, run_name='__main__')\n")
    run = child(code, SCRIPTS/f'{name}.py', path)
    assert run.returncode == 0, run.stderr
    assert json.loads((tmp_path/'worker.json').read_text(encoding='utf-8'))[field] is None
