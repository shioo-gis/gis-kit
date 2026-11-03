"""Measuring in a projection whose scale drifts, and terrain derivatives on any right-handed metre grid."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
from pyproj import CRS, Geod, Transformer
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))

from _grid import planar_metres_per_unit, right_handed
from _metric import analysis_frame, analysis_frames, analysis_method, ground_scale_range

MERCATOR = 'EPSG:3857'


def tool(name, *arguments, check=None):
    done = subprocess.run([sys.executable, str(SCRIPTS/f'{name}.py'), *map(str, arguments)], capture_output=True, text=True, encoding='utf-8')
    if check is not None:
        assert done.returncode == check, done.stderr
    return done


def mercator(longitude, latitude):
    return Transformer.from_crs('EPSG:4326', MERCATOR, always_xy=True).transform(longitude, latitude)


def square_at(latitude, side=1000.0):
    x, y = mercator(10, latitude)
    return box(x, y, x+side, y+side)


def geodesic_area(frame):
    return abs(Geod(ellps='WGS84').geometry_area_perimeter(frame.to_crs('EPSG:4326').geometry.iloc[0])[0])


def test_scale_range_matches_the_projection():
    low, high = ground_scale_range(CRS(MERCATOR), square_at(60, 10_000).bounds)
    assert low == pytest.approx(2.0, abs=0.01) and high == pytest.approx(2.0, abs=0.01)
    low, high = ground_scale_range(CRS('EPSG:3031'), (-2_000_000, -2_000_000, 2_000_000, 2_000_000))
    assert 0.97 < low < 0.98 and 1.0 < high < 1.05


@pytest.mark.parametrize('latitude', [60, 30, 10])
def test_mercator_measures_in_local_utm(latitude):
    frame = gpd.GeoDataFrame(geometry=[square_at(latitude)], crs=MERCATOR)
    work, factor = analysis_frame(frame)
    assert work.crs.to_epsg() == 32632 and factor == 1
    assert work.geometry.area.iloc[0] == pytest.approx(geodesic_area(frame), rel=0.002)


@pytest.mark.parametrize('code,bounds', [('EPSG:3031', (-2_000_000, -2_000_000, 2_000_000, 2_000_000)), ('EPSG:32650', (400_000, 3_000_000, 600_000, 3_200_000)),
                                         ('EPSG:4547', (400_000, 3_000_000, 600_000, 3_200_000)), ('EPSG:5070', (-2_000_000, 500_000, 2_000_000, 3_000_000))])
def test_ordinary_projections_stay_the_analysis_crs(code, bounds):
    frame = gpd.GeoDataFrame(geometry=[box(*bounds)], crs=code)
    work, factor = analysis_frame(frame)
    assert CRS(work.crs).equals(CRS(code)) and factor == 1
    assert analysis_method(frame, None, used=work.crs).startswith('source projected CRS')


def test_mercator_across_zones_names_the_way_out_and_tropics_stay():
    wide = gpd.GeoDataFrame(geometry=[box(*mercator(0, 50), *mercator(20, 55))], crs=MERCATOR)
    with pytest.raises(ValueError, match=r'factor of 1\.5\d* to 1\.7.*--analysis-crs'):
        analysis_frame(wide)
    tropics = gpd.GeoDataFrame(geometry=[box(*mercator(0, 0), *mercator(20, 1))], crs=MERCATOR)
    assert analysis_frame(tropics)[0].crs.to_epsg() == 3857
    assert analysis_frame(wide, 'EPSG:3035')[0].crs.to_epsg() == 3035


def test_explicit_analysis_crs_and_joint_layers():
    left = gpd.GeoDataFrame(geometry=[square_at(60)], crs=MERCATOR)
    right = gpd.GeoDataFrame(geometry=[box(10, 60, 10.001, 60.001)], crs='EPSG:4326')
    (a, b), factor = analysis_frames([left, right])
    assert a.crs.to_epsg() == b.crs.to_epsg() == 32632 and factor == 1
    assert analysis_method(left, None, used=a.crs).startswith('automatic local UTM')
    assert analysis_frame(left, MERCATOR)[0].crs.to_epsg() == 3857


def test_buffer_and_add_area_are_true_on_the_ground_in_mercator(tmp_path):
    source = tmp_path/'p.gpkg'
    gpd.GeoDataFrame({'id': [1]}, geometry=[Point(*mercator(10, 60))], crs=MERCATOR).to_file(source)
    output = tmp_path/'b.gpkg'
    done = tool('buffer', source, '--distance', 1000, '--output', output, check=0)
    assert 'automatic local UTM' in done.stdout
    buffered = gpd.read_file(output)
    assert buffered.crs.to_epsg() == 3857
    area = abs(Geod(ellps='WGS84').geometry_area_perimeter(buffered.to_crs('EPSG:4326').geometry.iloc[0])[0])
    assert area == pytest.approx(np.pi*1000**2, rel=0.01)
    polygon = tmp_path/'a.gpkg'
    frame = gpd.GeoDataFrame({'id': [1]}, geometry=[square_at(60)], crs=MERCATOR)
    frame.to_file(polygon)
    tool('field', 'add-area', polygon, '--output', tmp_path/'o.gpkg', check=0)
    measured = gpd.read_file(tmp_path/'o.gpkg')
    column = [c for c in measured.columns if 'area' in c.lower()][0]
    assert measured[column].iloc[0] == pytest.approx(geodesic_area(frame), rel=0.002)


def dem(path, crs, x0, y0, rise_x=0.1, rise_y=0.0, size=20, cell=10.0):
    z = 100 + rise_x*cell*np.arange(size)[None, :] - rise_y*cell*np.arange(size)[:, None]
    with rio.open(path, 'w', driver='GTiff', width=size, height=size, count=1, dtype='float64', crs=crs, nodata=-9999,
                  transform=from_origin(x0, y0, cell, cell)) as out:
        out.write(z, 1)
    return path


TERRAIN = {'vertical_unit': 'metre', 'vertical_datum': 'test datum', 'source_description': 'synthetic plane'}


def run_terrain(tmp_path, name, raster):
    params = tmp_path/f'{name}.json'
    params.write_text(json.dumps(TERRAIN), encoding='utf-8')
    return tool('terrain', raster, '--params', params, '--output', tmp_path/name)


@pytest.mark.parametrize('code,x0,y0', [('EPSG:4547', 500_000, 3_300_000), ('EPSG:3031', 1_000_000, 1_000_000), ('EPSG:3413', 1_000_000, 1_000_000), ('EPSG:32650', 500_000, 3_300_000)])
def test_terrain_accepts_any_right_handed_metre_grid(tmp_path, code, x0, y0):
    done = run_terrain(tmp_path, 'out', dem(tmp_path/'d.tif', code, x0, y0))
    assert done.returncode == 0, done.stderr
    with rio.open(tmp_path/'out'/'slope.tif') as ds:
        assert np.nanmedian(ds.read(1)) == pytest.approx(np.degrees(np.arctan(0.1)), abs=1e-6)
    with rio.open(tmp_path/'out'/'aspect.tif') as ds:
        assert np.nanmedian(ds.read(1)) == pytest.approx(270, abs=1e-6)  # downhill towards -x, measured from grid north
    scale = json.loads((tmp_path/'out'/'record.json').read_text(encoding='utf-8'))['ground_scale']
    assert 0.9 < scale['min'] <= scale['max'] < 1.1


def test_terrain_on_mercator_runs_and_states_the_scale(tmp_path):
    x, y = mercator(10, 60)
    done = run_terrain(tmp_path, 'out', dem(tmp_path/'d.tif', MERCATOR, x, y))
    assert done.returncode == 0, done.stderr
    scale = json.loads((tmp_path/'out'/'record.json').read_text(encoding='utf-8'))['ground_scale']
    assert scale['min'] == pytest.approx(2.0, abs=0.01)


def test_left_handed_and_unknown_unit_grids_are_refused(tmp_path):
    done = run_terrain(tmp_path, 'k', dem(tmp_path/'k.tif', 'EPSG:5513', -700_000, -1_000_000))
    assert done.returncode == 1 and 'left-handed' in done.stderr
    done = run_terrain(tmp_path, 'g', dem(tmp_path/'g.tif', 'EPSG:4326', 10, 50, cell=0.001))
    assert done.returncode == 1 and 'projected' in done.stderr
    assert right_handed('EPSG:4547', (500_000, 3_300_000, 500_100, 3_300_100))
    assert right_handed('EPSG:3413', (1_000_000, 1_000_000, 1_000_100, 1_000_100))
    assert not right_handed('EPSG:5513', (-700_100, -1_000_100, -700_000, -1_000_000))
    assert planar_metres_per_unit('EPSG:2236', (0, 0, 100, 100)) == pytest.approx(0.3048006, abs=1e-6)


def test_backend_validation_uses_the_same_check(tmp_path):
    spec = importlib.util.spec_from_file_location('terrain_backend', SCRIPTS/'terrain-backend.py')
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    params = {'vertical_unit': 'metre', 'vertical_datum': 'test datum', 'source_description': 'synthetic', 'threshold_cells': 5, 'flow_method': 'D8'}
    for code in ('EPSG:4547', 'EPSG:3031'):
        with rio.open(dem(tmp_path/f'{code[5:]}.tif', code, 500_000, 3_300_000)) as ds:
            backend.validate('hydrology', params, ds)
    with rio.open(dem(tmp_path/'feet.tif', 'EPSG:2236', 500_000, 3_300_000)) as ds, pytest.raises(ValueError, match='metre'):
        backend.validate('hydrology', params, ds)
