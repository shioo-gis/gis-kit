"""convert.py directory batch picks up the vector formats GDAL already reads, refuses ambiguous inputs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import Point

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPTS/'convert.py'), *map(str, args)], capture_output=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout+result.stderr
    return result


def points():
    return gpd.GeoDataFrame({'name': ['甲', '乙']}, geometry=[Point(120, 30), Point(121, 31)], crs=4326)


FORMATS = [('.tab', 'MapInfo File'), ('.mif', 'MapInfo File'), ('.kmz', 'LIBKML'), ('.gml', 'GML'),
           ('.fgb', 'FlatGeobuf'), ('.geojsonl', 'GeoJSONSeq'), ('.dxf', 'DXF')]


@pytest.mark.parametrize('suffix,driver', FORMATS)
def test_batch_directory_reads_format(tmp_path, suffix, driver):
    source = tmp_path/'in'; source.mkdir()
    frame = points()[['geometry']] if driver == 'DXF' else points()  # DXF has a fixed schema
    pyogrio.write_dataframe(frame, source/f'sample{suffix}', driver=driver)
    out = tmp_path/'out'
    result = cli(source, '--output', out/'x.gpkg')
    assert 'Batch mode: 1 files' in result.stdout  # sidecars (.dat/.map/.id/.mid/.xsd) are not inputs
    converted = gpd.read_file(out/'sample.gpkg')
    assert len(converted) == 2
    if driver == 'DXF':
        assert converted.crs is None  # never invented
    else:
        assert converted.crs.to_epsg() == 4326
        assert sorted(converted[[c for c in converted.columns if c.lower() == 'name'][0]]) == ['乙', '甲']


def test_multilayer_source_requires_layer(tmp_path):
    source = tmp_path/'walk.gpx'
    source.write_text('<?xml version="1.0"?><gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
                      '<wpt lat="30" lon="120"><name>甲</name></wpt><wpt lat="31" lon="121"><name>乙</name></wpt></gpx>',
                      encoding='utf-8')
    result = cli(source, '--output', tmp_path/'bad.gpkg', success=False)
    assert 'pass --layer' in result.stderr and 'track_points' in result.stderr
    assert not (tmp_path/'bad.gpkg').exists()
    cli(source, '--layer', 'waypoints', '--output', tmp_path/'waypoints.gpkg')
    assert list(gpd.read_file(tmp_path/'waypoints.gpkg').name) == ['甲', '乙']


def test_batch_refuses_outputs_with_the_same_name(tmp_path):
    source = tmp_path/'in'; source.mkdir()
    pyogrio.write_dataframe(points(), source/'roads.tab', driver='MapInfo File')
    pyogrio.write_dataframe(points(), source/'Roads.mif', driver='MapInfo File')
    out = tmp_path/'out'
    result = cli(source, '--output', out/'x.gpkg', '--overwrite', success=False)
    assert 'Roads.mif, roads.tab' in result.stderr
    assert not list(out.glob('*.gpkg'))


@pytest.mark.parametrize('suffix', ['.geojson', '.fgb', '.gpkg'])
def test_output_layer_is_named_after_the_file(tmp_path, suffix):
    source = tmp_path/'source.gpkg'
    points().to_file(source, layer='pts')
    cli(source, '--output', tmp_path/f'规划用地{suffix}')
    assert [name for name, _ in pyogrio.list_layers(tmp_path/f'规划用地{suffix}')] == ['规划用地']  # not the staging name


def test_fields_keeps_only_the_named_fields(tmp_path):
    source = tmp_path/'source.gpkg'
    frame = points().assign(code=['R1', 'R2'], area=[1.5, 2.5])
    frame.to_file(source)
    cli(source, '--output', tmp_path/'thin.geojson', '--fields', 'code, name', '--crs', 'EPSG:3857')
    thin = gpd.read_file(tmp_path/'thin.geojson')
    assert list(thin.columns) == ['name', 'code', 'geometry'] and thin.crs.to_epsg() == 3857
    assert thin.geometry.geom_equals_exact(frame.to_crs(3857).geometry, 1e-6).all()
    result = cli(source, '--output', tmp_path/'bad.geojson', '--fields', 'code,height', success=False)
    assert 'no field height' in result.stderr and 'area' in result.stderr
    assert not (tmp_path/'bad.geojson').exists()
