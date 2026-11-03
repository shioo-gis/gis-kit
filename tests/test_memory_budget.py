"""Opt-in memory budget: size parsing, estimates, and the exit-code/stderr contract."""
import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import Polygon

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import _budget as B

P = dict(vertical_unit='metre', vertical_datum='unknown', source_description='synthetic regression')


def dem(path, size=50):
    with rio.open(path, 'w', driver='GTiff', width=size, height=size, count=1, dtype='float32', crs='EPSG:32645',
                  transform=from_origin(500000, 3100000, 10, 10), nodata=np.nan) as d:
        d.write(np.indices((size, size))[1].astype('float32'), 1)
    return path


def run_terrain(tmp_path, *budget):
    (tmp_path/'p.json').write_text(json.dumps(P), encoding='utf-8')
    return subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), *budget, 'terrain', str(dem(tmp_path/'dem.tif')),
                           '--params', str(tmp_path/'p.json'), '--output', str(tmp_path/'out')],
                          capture_output=True, encoding='utf-8', errors='replace')


@pytest.mark.parametrize('text,expected', [('1048576', 2**20), ('2GiB', 2*2**30), ('512 MB', 512*10**6),
                                           ('1.5kib', 1536), ('3b', 3)])
def test_parse_size(text, expected):
    assert B.parse_size(text) == expected


@pytest.mark.parametrize('text', ['', '2x', '-1GB', '0', '0.1b', 'GiB'])
def test_parse_size_rejects(text):
    with pytest.raises(ValueError):
        B.parse_size(text)


def test_require_passes_without_budget_or_estimate(monkeypatch):
    monkeypatch.setenv(B.ENV, 'off')
    B.require(10**18, basis='decoded_pixels', input='x.tif')
    monkeypatch.setenv(B.ENV, '1KiB')
    B.require(None, basis='sampled_features', input='x.gpkg')
    B.require(1024, basis='decoded_pixels', input='x.tif')


def test_require_raises_exit_code_and_machine_line(monkeypatch, capsys):
    monkeypatch.setenv(B.ENV, '1KiB')
    with pytest.raises(B.BudgetExceeded) as caught:
        try:
            B.require(1025, basis='sampled_features', input='x.gpkg', layer='roads')
        except Exception:  # existing handlers must not swallow the budget failure
            pytest.fail('BudgetExceeded caught as an ordinary Exception')
    assert isinstance(caught.value, SystemExit) and caught.value.code == 3
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.startswith(B.ERROR_PREFIX)
    record = json.loads(last[len(B.ERROR_PREFIX):])
    assert record == caught.value.record
    assert record['code'] == 'memory_budget_exceeded' and record['layer'] == 'roads'
    assert record['estimated_bytes'] == 1025 and record['budget_bytes'] == 1024 and Path(record['input']).is_absolute()


def test_no_budget_does_not_sample(monkeypatch, tmp_path):
    monkeypatch.setenv(B.ENV, 'off')
    monkeypatch.setattr(B, 'vector_bytes', lambda *a, **k: pytest.fail('sampled without a budget'))
    B.check_vector(tmp_path/'missing.gpkg')


def test_vector_estimate_scales_with_count_and_vertices(tmp_path):
    def layer(name, n, vertices):
        t = np.linspace(0, 2*np.pi, vertices, endpoint=False)
        polygons = [Polygon(np.c_[i+np.cos(t), np.sin(t)]) for i in range(n)]
        path = tmp_path/f'{name}.gpkg'
        gpd.GeoDataFrame({'v': range(n)}, geometry=polygons, crs='EPSG:3857').to_file(path, engine='pyogrio')
        return B.vector_bytes(path)
    small, more, denser = layer('small', 500, 8), layer('more', 2000, 8), layer('denser', 500, 64)
    assert more == pytest.approx(4*small, rel=0.05)
    assert denser > 2*small
    gpd.GeoDataFrame({'v': []}, geometry=[], crs='EPSG:3857').to_file(tmp_path/'empty.gpkg', engine='pyogrio')
    assert B.vector_bytes(tmp_path/'empty.gpkg') == 0


def test_cli_budget_exceeded_exits_3_before_output(tmp_path):
    result = run_terrain(tmp_path, '--memory-budget', '64KiB')
    assert result.returncode == 3, result.stderr
    record = json.loads(result.stderr.strip().splitlines()[-1].removeprefix(B.ERROR_PREFIX))
    assert record['tool'] == 'terrain' and record['basis'] == 'decoded_pixels'
    assert record['estimated_bytes'] == 50*50*14*8 and record['budget_bytes'] == 64*1024
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('budget', [('--memory-budget', '1GiB'), ()])
def test_cli_budget_large_enough_or_unset_succeeds(tmp_path, monkeypatch, budget):
    monkeypatch.delenv(B.ENV, raising=False)
    result = run_terrain(tmp_path, *budget)
    assert result.returncode == 0, result.stderr


def test_cli_rejects_invalid_budget(tmp_path):
    result = run_terrain(tmp_path, '--memory-budget', 'lots')
    assert result.returncode == 2 and 'Invalid memory size' in result.stderr


def vectors(path, n=2000):
    gpd.GeoDataFrame({'zone': np.arange(n) % 5, 'name': [f'feature-{i}' for i in range(n)]},
                     geometry=[Polygon([(i, 0), (i+1, 0), (i+1, 1), (i, 1)]) for i in range(n)],
                     crs='EPSG:3857').to_file(path, engine='pyogrio')
    return path


def gis(*args, budget='64KiB'):
    return subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), '--memory-budget', budget, *map(str, args)],
                          capture_output=True, encoding='utf-8', errors='replace')


def budget_record(result):
    assert result.returncode == 3, result.stderr
    return json.loads(result.stderr.strip().splitlines()[-1].removeprefix(B.ERROR_PREFIX))


def test_cli_vector_tools_refuse_whole_reads_over_budget(tmp_path):
    source = vectors(tmp_path/'zones.gpkg')
    record = budget_record(gis('dissolve', source, '--by', 'zone', '--output', tmp_path/'out.gpkg'))
    assert record['tool'] == 'dissolve' and record['basis'] == 'sampled_features'
    assert record['input'] == str(source) and record['estimated_bytes'] > record['budget_bytes']
    assert not (tmp_path/'out.gpkg').exists()
    assert budget_record(gis('join', '--left', source, '--right', vectors(tmp_path/'other.gpkg', 10), '--output', tmp_path/'j.gpkg'))['input'] == str(source)
    assert budget_record(gis('inspect-data', source, 'summary'))['tool'] == 'inspect-data'


def test_cli_partial_reads_are_not_checked(tmp_path):
    source = vectors(tmp_path/'zones.gpkg')
    head = gis('inspect-data', source, 'head', '-n', '3')
    assert head.returncode == 0, head.stderr
    assert gis('dissolve', source, '--by', 'zone', '--output', tmp_path/'ok.gpkg', budget='1GiB').returncode == 0
