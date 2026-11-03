"""Streamed layer copies must write what one full read and write would."""
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
from shapely.geometry import MultiPolygon, Point, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
from _safe_io import STREAM_DRIVERS, full_read_plan, stream_vector


def tricky(n=10):
    """Late-only nulls, nullable bool, datetimes, and mixed single/multi polygons."""
    count = pd.array([*range(n-1), None], dtype='Int64')
    flag = pd.array([True]*(n-1)+[None], dtype='boolean')
    geometry = [box(i, 0, i+1, 1) if i < n-2 else MultiPolygon([box(i, 0, i+.5, 1), box(i+.6, 0, i+1, 1)]) for i in range(n)]
    return gpd.GeoDataFrame({'name': [f'n{i}' for i in range(n)], 'count': count, 'flag': flag, 'value': np.arange(n)/3,
                             'when': pd.date_range('2026-01-01', periods=n, freq='D')},
                            geometry=geometry, crs='EPSG:3857')


def snapshot(path, layer=None):
    info = pyogrio.read_info(path, layer=layer)
    return (list(info['fields']), list(info['dtypes']), info['geometry_type'], info['features'],
            pyogrio.read_dataframe(path, layer=layer))


def stream(source, target, driver, chunk_size, layer=None, transform=None):
    def write(frame, first, **options):
        frame.to_file(target, driver=driver, engine='pyogrio', mode='w' if first else 'a', **options)
    return stream_vector(source, write, driver=driver, layer=layer, chunk_size=chunk_size, transform=transform)


@pytest.mark.parametrize('source_name', ['source.gpkg', 'source.geojson'])  # GeoJSON keeps mixed single/multi types
@pytest.mark.parametrize('driver,suffix', [('GPKG', '.gpkg'), ('ESRI Shapefile', '.shp')])
@pytest.mark.parametrize('chunk_size', [1, 3, 9])
def test_stream_matches_full_write(tmp_path, source_name, driver, suffix, chunk_size):
    source = tmp_path/source_name
    tricky().to_file(source, engine='pyogrio')
    full, streamed = tmp_path/('full'+suffix), tmp_path/('streamed'+suffix)
    gpd.read_file(source, engine='pyogrio').to_file(full, driver=driver, engine='pyogrio')
    assert stream(source, streamed, driver, chunk_size) == 10
    *meta_full, frame_full = snapshot(full)
    *meta_streamed, frame_streamed = snapshot(streamed)
    assert meta_streamed == meta_full
    pd.testing.assert_frame_equal(frame_streamed, frame_full)


def test_plan_reproduces_full_read_dtypes_and_type(tmp_path):
    source = tmp_path/'source.geojson'
    tricky().to_file(source, engine='pyogrio')
    full = gpd.read_file(source, engine='pyogrio')
    plan = full_read_plan(source, driver='GPKG', chunk_size=3)
    assert plan['dtypes'] == {c: full[c].dtype for c in full.columns if c != 'geometry'}
    assert plan['geometry_type'] == 'MultiPolygon' and plan['promote_to_multi'] is True
    assert full_read_plan(source, driver='ESRI Shapefile', chunk_size=3)['promote_to_multi'] is False


def test_stream_transform_and_z(tmp_path):
    source = tmp_path/'z.gpkg'
    gpd.GeoDataFrame({'v': range(7)}, geometry=[Point(i, i, i) for i in range(7)], crs='EPSG:4326').to_file(source, engine='pyogrio')
    target = tmp_path/'out.gpkg'
    assert stream(source, target, 'GPKG', 2, transform=lambda f: f.to_crs('EPSG:3857')) == 7
    back = gpd.read_file(target, engine='pyogrio')
    expected = gpd.read_file(source, engine='pyogrio').to_crs('EPSG:3857')
    assert pyogrio.read_info(target)['geometry_type'] == 'Point Z' and back.crs == expected.crs
    assert back.geometry.geom_equals_exact(expected.geometry, 1e-6).all()


@pytest.mark.parametrize('driver', ['GeoJSON', 'FlatGeobuf', None])
def test_non_appendable_drivers_fall_back(tmp_path, driver):
    source = tmp_path/'source.gpkg'
    tricky().to_file(source, engine='pyogrio')
    assert driver not in STREAM_DRIVERS
    assert stream(source, tmp_path/'never.json', driver, 2) is None
    assert not (tmp_path/'never.json').exists()


def test_layer_within_one_chunk_is_not_streamed(tmp_path):
    source = tmp_path/'source.gpkg'
    tricky().to_file(source, engine='pyogrio')
    assert stream(source, tmp_path/'never.gpkg', 'GPKG', 10) is None


def test_cli_convert_and_gpkg_copy_stream_large_layers(tmp_path):
    n = 100_050
    source = tmp_path/'big.gpkg'
    gpd.GeoDataFrame({'id': np.arange(n), 'note': [None if i == n-1 else 'x' for i in range(n)]},
                     geometry=gpd.points_from_xy(np.arange(n)*1e-3, np.zeros(n)), crs='EPSG:4326').to_file(source, layer='pts', engine='pyogrio')
    full = gpd.read_file(source, engine='pyogrio')
    converted = tmp_path/'converted.gpkg'
    run = subprocess.run([sys.executable, str(SCRIPTS/'convert.py'), str(source), '--output', str(converted), '--crs', 'EPSG:3857'],
                         capture_output=True, encoding='utf-8', errors='replace')
    assert run.returncode == 0, run.stderr
    assert 'Streamed 100050 features' in run.stdout
    back = gpd.read_file(converted, engine='pyogrio')
    pd.testing.assert_frame_equal(back.drop(columns='geometry'), full.drop(columns='geometry'))
    assert back.geometry.geom_equals_exact(full.to_crs('EPSG:3857').geometry, 1e-6).all()
    thin = tmp_path/'thin.gpkg'
    run = subprocess.run([sys.executable, str(SCRIPTS/'convert.py'), str(source), '--output', str(thin), '--fields', 'note'],
                         capture_output=True, encoding='utf-8', errors='replace')
    assert run.returncode == 0 and 'Streamed 100050 features' in run.stdout, run.stderr
    pd.testing.assert_frame_equal(gpd.read_file(thin, engine='pyogrio'), full[['note', 'geometry']])
    copied = tmp_path/'copied.gpkg'
    run = subprocess.run([sys.executable, str(SCRIPTS/'gpkg.py'), 'copy', '--source', str(source), '--target', str(copied),
                          '--target-layer', 'kept'], capture_output=True, encoding='utf-8', errors='replace')
    assert run.returncode == 0, run.stderr
    pd.testing.assert_frame_equal(gpd.read_file(copied, layer='kept', engine='pyogrio'), full)
