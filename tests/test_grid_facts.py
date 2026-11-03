"""Findings about a grid reach the record: NoData, scale, world square, pole, type changes."""
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))

from _grid import grid_facts, nodata_unset, pole_outside_target


def tif(path, values, crs='EPSG:32650', nodata=None, cell=100.0, origin=(500_000, 3_300_000), mask=None):
    values = np.asarray(values)
    with rio.Env(GDAL_TIFF_INTERNAL_MASK=True), rio.open(path, 'w', driver='GTiff', width=values.shape[1], height=values.shape[0], count=1, dtype=values.dtype.name, crs=crs,
                                                         nodata=nodata, transform=from_origin(*origin, cell, cell)) as out:
        out.write(values, 1)
        if mask is not None:
            out.write_mask(mask)
    return path


def raster(operation, source, output, **params):
    spec = Path(str(output)+'.json')
    spec.write_text(json.dumps(params), encoding='utf-8')
    done = subprocess.run([sys.executable, str(SCRIPTS/'raster.py'), operation, str(source), '--params', str(spec), '--output', str(output)],
                          capture_output=True, encoding='utf-8', errors='replace')
    assert done.returncode == 0, done.stderr
    return json.loads((Path(output)/'record.json').read_text(encoding='utf-8')), done


def codes(items):
    return [item['code'] for item in items]


def sea_ice(size=30):
    """Valid values with land and coast markers inside the value range: nothing declares them missing."""
    values = np.full((size, size), 500, 'int16')
    values[10:20, 10:20] = 2530
    return values


def test_warp_with_interpolation_warns_and_still_blends(tmp_path):
    source = tif(tmp_path/'s.tif', sea_ice())
    record, done = raster('warp', source, tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=70)
    assert 'nodata_unset' in codes(record['warnings'])
    warning = next(w for w in record['warnings'] if w['code'] == 'nodata_unset')
    assert warning['severity'] == 'warning' and warning['detail']['band'] == 1 and 'invalidate' in warning['message']
    assert 'WARNING' in done.stderr and 'no NoData' in done.stderr
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        values = ds.read(1, masked=True).compressed()
    assert ((values > 500) & (values < 2530)).any()  # a warning for now, not a refusal: the blend is still there


def test_the_warning_names_a_way_that_works(tmp_path):
    source = tif(tmp_path/'s.tif', sea_ice())
    raster('transform', source, tmp_path/'marked', method='invalidate', min=0, max=1000, operands=[{'input': 0, 'band': 1}])
    record, done = raster('warp', tmp_path/'marked'/'result.tif', tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=70)
    assert 'nodata_unset' not in codes(record['warnings']) and 'WARNING' not in done.stderr
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        np.testing.assert_allclose(ds.read(1, masked=True).compressed(), 500)


@pytest.mark.parametrize('resampling,severity', [('nearest', 'info'), ('average', 'warning')])
def test_sampling_does_not_blend_so_it_only_notes(tmp_path, resampling, severity):
    source = tif(tmp_path/'s.tif', sea_ice())
    record, _ = raster('warp', source, tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling=resampling, resolution=70)
    assert ('nodata_unset' in codes(record['warnings' if severity == 'warning' else 'facts']))


def test_declared_missing_values_raise_no_warning(tmp_path):
    with_nodata = tif(tmp_path/'n.tif', sea_ice(), nodata=2530)
    masked = tif(tmp_path/'m.tif', sea_ice(), mask=np.full((30, 30), 255, 'uint8'))
    with rio.open(with_nodata) as a, rio.open(masked) as b:
        assert not nodata_unset(a) and not nodata_unset(b)
    assert 'nodata_unset' not in codes(raster('warp', with_nodata, tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=70)[0]['warnings'])


def test_every_raster_record_has_warnings_and_facts(tmp_path):
    source = tif(tmp_path/'s.tif', sea_ice(), nodata=-9999)
    for operation, params in (('inspect', {}), ('copy', {}), ('histogram', {}), ('calculate', {'scale': 2}), ('fill_holes', {'kind': 'continuous', 'method': 'idw', 'max_hole_pixels': 1, 'max_distance_m': 1}),
                              ('bands', {'operands': [{'input': 0, 'band': 1}]})):
        record, _ = raster(operation, source, tmp_path/operation, **params)
        assert isinstance(record['warnings'], list) and record['schema_version'] == 1 and record['operation'] == operation
        assert record['parameters'] == params and record['inputs'][0]['name'] == 's.tif' and record['implementation_sha256']
        assert any(f['code'] == 'ground_scale_range' for f in record['facts'])
        assert 'status' not in record  # recipes read the absence of a status as a legacy record, so the skeleton does not invent one


def test_scale_warning_on_mercator_areas(tmp_path):
    source = tif(tmp_path/'m.tif', sea_ice(), crs='EPSG:3857', origin=(1_113_000, 8_400_000))
    record, done = raster('regions', source, tmp_path/'o')
    scale = next(w for w in record['warnings'] if w['code'] == 'ground_scale_range')
    assert scale['detail']['min'] == pytest.approx(2, abs=0.05) and 'off by that factor' in scale['message']


def test_type_change_and_world_trim_are_stated(tmp_path):
    source = tif(tmp_path/'u.tif', np.arange(360*180, dtype='uint16').reshape(180, 360)%100, crs='EPSG:4326', cell=1.0, origin=(-180, 90))
    record, _ = raster('warp', source, tmp_path/'o', crs='EPSG:3857', kind='continuous', resampling='nearest')
    assert {'dtype_promoted', 'nodata_unset', } <= set(codes(record['facts']))
    assert 'target_grid_trimmed' in codes(record['warnings']) and record['output']['dtypes'] == ['float32']


def test_polar_cap_lost_in_the_default_extent_is_stated(tmp_path):
    source = tif(tmp_path/'p.tif', np.ones((60, 60), 'float32'), crs='EPSG:3031', cell=50_000, origin=(-1_500_000, 1_500_000), nodata=-1)
    record, _ = raster('warp', source, tmp_path/'o', crs='EPSG:4326', kind='continuous', resampling='nearest')
    assert 'polar_cap_outside_extent' in codes(record['warnings'])
    assert pole_outside_target('EPSG:3031', (-10, -10, 10, 10), 'EPSG:4326', (-180, -90, 180, -60)) is None
    assert pole_outside_target('EPSG:3031', (1000, 1000, 2000, 2000), 'EPSG:4326', (0, -89, 1, -88)) is None
    assert pole_outside_target('EPSG:3031', (-10, -10, 10, 10), 'EPSG:3413', (0, 0, 1, 1)) is None


def test_mask_densification_reaches_the_record(tmp_path):
    source = tif(tmp_path/'p.tif', np.ones((60, 60), 'float32'), crs='EPSG:3031', cell=50_000, origin=(-1_500_000, 1_500_000), nodata=-1)
    cap = tmp_path/'cap.gpkg'
    gpd.GeoDataFrame({'id': [1]}, geometry=[box(-180, -90, 180, -75)], crs='EPSG:4326').to_file(cap)
    record, _ = raster('zonal', source, tmp_path/'o', vector=str(cap), id='id')
    assert 'edges_densified' in codes(record['facts'])


def test_terrain_and_detail_records_state_findings(tmp_path):
    z = (np.indices((20, 20))[1]*10.).astype('float32')
    params = {'vertical_unit': 'metre', 'vertical_datum': 'test datum', 'source_description': 'synthetic'}
    source = tif(tmp_path/'d.tif', z, crs='EPSG:3857', origin=(1_113_000, 8_400_000), cell=10.0)
    (tmp_path/'p.json').write_text(json.dumps(params), encoding='utf-8')
    done = subprocess.run([sys.executable, str(SCRIPTS/'terrain.py'), str(source), '--params', str(tmp_path/'p.json'), '--output', str(tmp_path/'t')], capture_output=True, encoding='utf-8', errors='replace')
    assert done.returncode == 0, done.stderr
    record = json.loads((tmp_path/'t'/'record.json').read_text(encoding='utf-8'))
    assert {'nodata_unset', 'ground_scale_range'} <= set(codes(record['warnings'])) and 'WARNING' in done.stderr
    (tmp_path/'q.json').write_text(json.dumps({**params, 'levels_m': [50]}), encoding='utf-8')
    done = subprocess.run([sys.executable, str(SCRIPTS/'terrain-detail.py'), str(source), '--params', str(tmp_path/'q.json'), '--output', str(tmp_path/'td')], capture_output=True, encoding='utf-8', errors='replace')
    assert done.returncode == 0, done.stderr
    assert 'ground_scale_range' in codes(json.loads((tmp_path/'td'/'record.json').read_text(encoding='utf-8'))['warnings'])


def test_describe_does_not_call_a_masked_raster_unset(tmp_path):
    masked = tif(tmp_path/'m.tif', sea_ice(), mask=np.full((30, 30), 255, 'uint8'))
    plain = tif(tmp_path/'p.tif', sea_ice())
    def warnings(path):
        done = subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), 'describe', str(path), '--json'], capture_output=True, encoding='utf-8', errors='replace')
        return codes(json.loads(done.stdout)['warnings'])
    assert warnings(masked) == [] and warnings(plain) == ['nodata_unset']


def test_grid_facts_in_process(tmp_path):
    with rio.open(tif(tmp_path/'s.tif', sea_ice())) as ds:
        found = grid_facts(ds, 1, 'copy')
    assert codes(found) == ['nodata_unset', 'ground_scale_range'] and found[0]['severity'] == 'info'
