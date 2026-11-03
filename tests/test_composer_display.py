"""Analytical values, display edges, machine output and failed publication."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
from PIL import Image
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('raster_cli', SCRIPTS / 'raster.py')
raster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(raster)
from _delivery import bundle


def fixture(tmp_path, crs='EPSG:3857', scale=1):
    source = tmp_path / 'two bands.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=3, height=2, count=2, dtype='float64',
                       crs=crs, transform=from_origin(0, 200, 100, 100), nodata=-9999) as ds:
        ds.write(np.full((2, 3), 999.), 1)
        ds.write(np.array([[0., 10., 20.], [-9999, 9., 19.]]), 2)
        ds.scales = (1, scale)
    return source


def params(**extra):
    return dict(band=2, breaks=[10, 20], colors=['#112233', '#445566', '#778899'], unit='m', status='hold', **extra)


def test_display_band_boundaries_zero_nodata_and_readback(tmp_path):
    source = fixture(tmp_path)
    before = source.read_bytes()
    result = raster.process('display', [source], params(), tmp_path)
    assert result['class_pixels'] == [2, 2, 1]
    assert result['nodata_pixels'] == 1 and result['status'] == 'hold'
    a = np.asarray(Image.open(tmp_path / 'display.png'))
    assert a[0, 0].tolist() == [17, 34, 51, 255]
    assert a[0, 1].tolist() == [68, 85, 102, 255]
    assert a[0, 2].tolist() == [119, 136, 153, 255]
    assert a[1, 0, 3] == 0
    assert result['coordinates'][0][1] > result['coordinates'][3][1]
    assert source.read_bytes() == before


@pytest.mark.parametrize('update', [{'band': 3}, {'breaks': [20, 10]}, {'colors': ['red']}, {'unit': ''}, {'status': 'auto'}, {'max_pixels': 2}])
def test_display_rejects_and_does_not_publish(tmp_path, update):
    source = fixture(tmp_path)
    p = params(); p.update(update)
    settings = tmp_path / 'params.json'; settings.write_text(json.dumps(p), encoding='utf-8')
    output = tmp_path / 'published'
    from argparse import Namespace
    with pytest.raises(ValueError):
        raster.execute(Namespace(operation='display', input=[str(source)], params=str(settings), output=str(output)))
    assert not output.exists()
    assert not list(tmp_path.glob('.raster-*'))


@pytest.mark.parametrize('crs,scale', [('EPSG:4326', 1), ('EPSG:3857', 2)])
def test_display_rejects_unwarped_or_uncalibrated(tmp_path, crs, scale):
    with pytest.raises(ValueError):
        raster.process('display', [fixture(tmp_path, crs, scale)], params(), tmp_path)


def test_injected_png_write_failure_never_publishes(tmp_path, monkeypatch):
    source = fixture(tmp_path)
    settings = tmp_path / 'params.json'; settings.write_text(json.dumps(params()), encoding='utf-8')
    def broken(self, path, *a, **kw):
        Path(path).write_bytes(b'partial')
        raise OSError('injected partial write')
    monkeypatch.setattr(Image.Image, 'save', broken)
    from argparse import Namespace
    with pytest.raises(OSError, match='injected'):
        raster.execute(Namespace(operation='display', input=[str(source)], params=str(settings), output=str(tmp_path / 'published')))
    assert not (tmp_path / 'published').exists()
    assert not list(tmp_path.glob('.raster-*'))


def test_complete_bundle_failure_preserves_source(tmp_path):
    source = fixture(tmp_path); before = source.read_bytes()
    with pytest.raises(OSError):
        with bundle(tmp_path / 'delivery') as stage:
            (stage / 'analysis.json').write_text('{}', encoding='utf-8')
            raise OSError('render failed after analysis')
    assert not (tmp_path / 'delivery').exists()
    assert not list(tmp_path.glob('.delivery-*'))
    assert source.read_bytes() == before


def test_machine_inspection_and_stats_cli(tmp_path):
    data = tmp_path / 'layers.gpkg'
    gpd.GeoDataFrame({'value': [0., None], 'status': ['candidate','hold']}, geometry=[box(0,0,1,1),box(1,0,2,1)], crs=3857).to_file(data, layer='wanted')
    gpd.GeoDataFrame({'other': [99]}, geometry=[box(0,0,3,3)], crs=3857).to_file(data, layer='other')
    def call(script, args):
        return subprocess.run([sys.executable,str(SCRIPTS/script),*map(str,args)],capture_output=True,text=True)
    r = call('inspect-data.py',[data,'--layer','wanted','summary','--json']); assert r.returncode == 0, r.stderr
    v = json.loads(r.stdout); assert v['features'] == 2 and v['numeric']['value']['count'] == 1
    r = call('inspect-data.py',[data,'layers','--json']); assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)) == 2
    r = call('stats.py',['describe',data,'--layer','wanted','--json']); assert r.returncode == 0,r.stderr
    assert json.loads(r.stdout)[0]['mean'] == 0
    r = call('inspect-data.py',[data,'--layer','missing','summary','--json'])
    assert r.returncode != 0 and r.stdout == '' and 'not found' in r.stderr


def test_empty_layer_inventory_is_still_json(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('inspect_cli', SCRIPTS / 'inspect-data.py')
    inspector = importlib.util.module_from_spec(spec); spec.loader.exec_module(inspector)
    path = tmp_path / 'empty.gpkg'; path.touch()
    monkeypatch.setattr(inspector, 'list_layers', lambda path: [])
    from argparse import Namespace
    inspector.cmd_layers(Namespace(file=str(path), json=True))
    assert json.loads(capsys.readouterr().out) == []


def values_params(**extra):
    return dict(band=2, mode='values', breaks=[10, 20], unit='m', status='hold', **extra)


def test_values_mode_encodes_class_ids_and_writes_contracts(tmp_path):
    source = fixture(tmp_path)
    before = source.read_bytes()
    result = raster.process('display', [source], values_params(labels=['低', '中', '高']), tmp_path)
    a = np.asarray(Image.open(tmp_path / 'values.png'))
    # [[0, 10, 20], [NoData, 9, 19]] with breaks [10, 20]: lower inclusive, upper exclusive.
    assert a[..., 0].tolist() == [[0, 1, 2], [0, 0, 1]] and a[..., 1].max() == 0
    assert a[..., 3].tolist() == [[255, 255, 255], [0, 255, 255]]
    contract = json.loads((tmp_path / 'raster.json').read_text(encoding='utf-8'))
    scheme = json.loads((tmp_path / 'scheme.json').read_text(encoding='utf-8'))
    assert contract['contract'] == 'display-raster' and contract['schema_version'] == 1
    assert contract['encoding'] == 'class-index' and contract['sampling'] == 'nearest' and contract['scheme'] == 'scheme.json'
    assert scheme['contract'] == 'class-scheme' and scheme['kind'] == 'sequential'
    assert [c['label'] for c in scheme['classes']] == ['低', '中', '高']
    assert scheme['classes'][1] == {'id': 1, 'min': 10, 'max': 20, 'label': '中'}
    assert scheme['counts'] == [2, 2, 1] and result['class_pixels'] == [2, 2, 1] and result['nodata_pixels'] == 1
    assert 'colors' not in scheme and source.read_bytes() == before


def test_values_mode_continuous_rgb24_round_trips_within_quantization(tmp_path):
    source = fixture(tmp_path)
    p = values_params(encoding='value-rgb24', kind='continuous'); p.pop('breaks')
    raster.process('display', [source], p, tmp_path)
    contract = json.loads((tmp_path / 'raster.json').read_text(encoding='utf-8'))
    a = np.asarray(Image.open(tmp_path / 'values.png')).astype('int64')
    decoded = contract['offset'] + contract['scale'] * (a[..., 0] * 65536 + a[..., 1] * 256 + a[..., 2])
    expected = np.array([[0., 10., 20.], [0., 9., 19.]])
    valid = a[..., 3] == 255
    assert np.all(np.abs(decoded[valid] - expected[valid]) <= contract['quantization'] + 1e-12)
    assert contract['sampling'] == 'linear' and json.loads((tmp_path / 'scheme.json').read_text(encoding='utf-8'))['domain'] == [0, 20]


def test_values_mode_categories_and_refusals(tmp_path):
    source = fixture(tmp_path)
    p = values_params(categories=[{'code': 0, 'label': '水', 'group': '自然'}, {'code': 9, 'label': '林'}, {'code': 10, 'label': '田'}, {'code': 19, 'label': '城'}, {'code': 20, 'label': '路'}])
    p.pop('breaks')
    raster.process('display', [source], p, tmp_path)
    a = np.asarray(Image.open(tmp_path / 'values.png'))
    assert a[..., 0].tolist() == [[0, 2, 4], [0, 1, 3]]
    scheme = json.loads((tmp_path / 'scheme.json').read_text(encoding='utf-8'))
    assert scheme['kind'] == 'categorical' and scheme['classes'][0]['group'] == '自然' and scheme['unlisted'] == 0
    with pytest.raises(ValueError, match='no listed category'):
        raster.process('display', [source], {**p, 'categories': p['categories'][:2]}, tmp_path)
    with pytest.raises(ValueError, match='carries no colours'):
        raster.process('display', [source], values_params(colors=['#000000'] * 3), tmp_path)
    with pytest.raises(ValueError, match='encoding'):
        raster.process('display', [source], values_params(encoding='png8'), tmp_path)


def test_vector_scheme_cli_methods_counts_and_nodata(tmp_path):
    frame = gpd.GeoDataFrame({'v': [1.0, 2.0, 3.0, 4.0, None], 'kind': ['a', 'b', 'a', 'c', 'b']},
                             geometry=[box(i, 0, i + 1, 1) for i in range(5)], crs=3857)
    data = tmp_path / 'zones.gpkg'; frame.to_file(data, layer='z')
    def run(field, p, name):
        settings = tmp_path / f'{name}.json'; settings.write_text(json.dumps(p), encoding='utf-8')
        result = subprocess.run([sys.executable, SCRIPTS / 'scheme.py', data, '--layer', 'z', '--field', field, '--params', settings, '--output', tmp_path / name],
                                capture_output=True, text=True)
        return result, (json.loads((tmp_path / name / 'scheme.json').read_text(encoding='utf-8')) if result.returncode == 0 else None)
    _, quantile = run('v', {'unit': '人', 'status': 'candidate', 'method': 'quantile', 'classes': 2}, 'q')
    assert quantile['method'] == 'quantile' and quantile['breaks'] == [2.5] and quantile['counts'] == [2, 2]
    assert quantile['nodata']['count'] == 1 and quantile['classes'][0]['label'] == '< 2.5 人'
    _, categories = run('kind', {'unit': 'dimensionless', 'status': 'hold', 'categories': 'unique'}, 'c')
    assert [c['code'] for c in categories['classes']] == ['a', 'b', 'c'] and categories['counts'] == [2, 2, 1]
    failed, _ = run('v', {'unit': '人', 'status': 'auto', 'breaks': [2]}, 'bad')
    assert failed.returncode != 0 and not (tmp_path / 'bad').exists()
