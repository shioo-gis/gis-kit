"""warp reads in pieces and keeps types; memory guards state numbers; EPSG:3857 stays on the world square."""
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
from PIL import Image
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))

import _budget as B
from _grid import WEB_MERCATOR_BOUND, clip_grid_to_world, guard_pixels, warp_can_stream, warp_output_type


def tif(path, values, crs='EPSG:32650', nodata=None, cell=100.0, origin=(500_000, 3_300_000), mask=None):
    values = np.asarray(values)
    with rio.Env(GDAL_TIFF_INTERNAL_MASK=True), rio.open(path, 'w', driver='GTiff', width=values.shape[1], height=values.shape[0], count=1, dtype=values.dtype.name, crs=crs,
                                                         nodata=nodata, transform=from_origin(*origin, cell, cell)) as out:
        out.write(values, 1)
        if mask is not None:
            out.write_mask(mask)
    return path


def raster(operation, source, output, env=None, **params):
    spec = Path(str(output)+'.json')
    spec.write_text(json.dumps(params), encoding='utf-8')
    return subprocess.run([sys.executable, str(SCRIPTS/'raster.py'), operation, str(source), '--params', str(spec), '--output', str(output)],
                          capture_output=True, encoding='utf-8', errors='replace', env={**__import__('os').environ, **(env or {})})


def gradient(dtype, size=40):
    return (np.indices((size, size))[1]*3+np.indices((size, size))[0]*5+100).astype(dtype)


WARP = {'crs': 'EPSG:32650', 'kind': 'continuous', 'resampling': 'nearest', 'resolution': 100}


@pytest.mark.parametrize('dtype,nodata,resampling,expected,expected_nodata', [
    ('float32', None, 'bilinear', 'float32', 'nan'), ('float32', -9999, 'bilinear', 'float32', 'nan'), ('float64', None, 'average', 'float64', 'nan'),
    ('int16', -9999, 'nearest', 'int16', -9999.0), ('int16', -9999, 'bilinear', 'float32', 'nan'), ('uint16', None, 'nearest', 'float32', 'nan'),
    ('uint8', 255, 'mode', 'uint8', 255.0), ('int32', None, 'cubic', 'float64', 'nan'),
])
def test_warp_keeps_the_type_that_can_hold_the_result(tmp_path, dtype, nodata, resampling, expected, expected_nodata):
    source = tif(tmp_path/'s.tif', gradient(dtype), nodata=nodata)
    done = raster('warp', source, tmp_path/'o', **{**WARP, 'resampling': resampling})
    assert done.returncode == 0, done.stderr
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        assert ds.dtypes[0] == expected
        assert (np.isnan(ds.nodata) if expected_nodata == 'nan' else ds.nodata == expected_nodata)
        assert ds.read(1, masked=True).count() > 0


def test_interpolated_integers_keep_their_fractions(tmp_path):
    source = tif(tmp_path/'s.tif', gradient('int16'), nodata=-9999)
    assert raster('warp', source, tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=55).returncode == 0
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        values = ds.read(1, masked=True).compressed()
    assert (values != np.round(values)).any()


@pytest.mark.parametrize('resampling', ['nearest', 'average', 'bilinear'])
def test_streaming_agrees_with_the_array_path(tmp_path, resampling):
    values = gradient('float32', 60)
    streamed = tif(tmp_path/'a.tif', values, nodata=-9999)
    through_array = tif(tmp_path/'b.tif', values, mask=np.full(values.shape, 255, 'uint8'))
    with rio.open(streamed) as a, rio.open(through_array) as b:
        assert warp_can_stream(a, 1, None) and not warp_can_stream(b, 1, None)
    for name, source in (('a', streamed), ('b', through_array)):
        done = raster('warp', source, tmp_path/name, crs='EPSG:3857', kind='continuous', resampling=resampling, resolution=250)
        assert done.returncode == 0, done.stderr
    with rio.open(tmp_path/'a'/'result.tif') as a, rio.open(tmp_path/'b'/'result.tif') as b:
        both = np.isfinite(a.read(1)) & np.isfinite(b.read(1))
        assert both.sum() > 100 and np.abs(a.read(1)[both]-b.read(1)[both]).max() <= (0 if resampling != 'bilinear' else 1)


def test_nodata_and_stray_values_do_not_reach_the_result(tmp_path):
    values = gradient('float32', 30)
    values[10:15, 10:15] = -9999
    source = tif(tmp_path/'s.tif', values, nodata=-9999)
    assert raster('warp', source, tmp_path/'o', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=50).returncode == 0
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        assert np.nanmin(ds.read(1)) >= 100
    stray = gradient('float32', 30)
    stray[3, 3] = np.inf
    stray[20, 20] = np.nan
    source = tif(tmp_path/'i.tif', stray, nodata=-9999)
    with rio.open(source) as ds:
        assert not warp_can_stream(ds, 1, None)
    assert raster('warp', source, tmp_path/'i', crs='EPSG:32650', kind='continuous', resampling='bilinear', resolution=50).returncode == 0
    with rio.open(tmp_path/'i'/'result.tif') as ds:
        assert not np.isinf(ds.read(1)).any() and np.nanmax(ds.read(1)) < 1000


def test_calibrated_band_is_still_refused(tmp_path):
    source = tif(tmp_path/'s.tif', gradient('float32'))
    with rio.open(source, 'r+') as ds:
        ds.scales = (0.1,)
    done = raster('warp', source, tmp_path/'o', **WARP)
    assert done.returncode == 1 and 'scale/offset' in done.stderr


def test_warp_budget_follows_the_target_not_the_source(tmp_path):
    values = gradient('float32', 1500)
    streamed = tif(tmp_path/'a.tif', values, nodata=-9999)
    masked = tif(tmp_path/'b.tif', values, mask=np.full(values.shape, 255, 'uint8'))
    budget = {B.ENV: '100MiB'}
    coarse = dict(crs='EPSG:32650', kind='continuous', resampling='average', resolution=5000)
    assert raster('warp', streamed, tmp_path/'a', env=budget, **coarse).returncode == 0
    refused = raster('warp', masked, tmp_path/'b', env=budget, **coarse)
    assert refused.returncode == B.EXIT_CODE
    assert raster('warp', streamed, tmp_path/'c', max_pixels=1000, **coarse).returncode == 0


def test_memory_guards_state_numbers_and_a_way_out(tmp_path):
    source = tif(tmp_path/'s.tif', gradient('float32'))
    done = raster('copy', source, tmp_path/'o', max_pixels=100)
    assert done.returncode == 1 and '1,600 pixels' in done.stderr and 'max_pixels=100' in done.stderr and 'GiB' in done.stderr and 'raise max_pixels' in done.stderr
    done = raster('warp', source, tmp_path/'t', max_pixels=100, **WARP)
    assert done.returncode == 1 and 'Target grid (40 x 40)' in done.stderr and 'coarser resolution' in done.stderr
    done = raster('focal', source, tmp_path/'f', kind='continuous', statistic='mean', window=3, max_elements=10)
    assert done.returncode == 1 and 'max_elements=10' in done.stderr
    with pytest.raises(ValueError, match=r'2,000 pixels.*max_elements=1,000.*Do this'):
        guard_pixels(2000, 1000, 'Grid', param='max_elements', way_out='Do this')
    guard_pixels(1000, 1000, 'Grid')


def world_grid(tmp_path, top=90, cell=1.0):
    height = int(2*top/cell)
    return tif(tmp_path/'w.tif', np.full((height, int(360/cell)), 7, 'float32'), crs='EPSG:4326', cell=cell, origin=(-180, top))


@pytest.mark.parametrize('params', [{}, {'bounds': [-180, -90, 180, 90]}])
def test_mercator_target_stays_on_the_world_square(tmp_path, params):
    done = raster('warp', world_grid(tmp_path), tmp_path/'o', crs='EPSG:3857', kind='continuous', resampling='nearest', **params)
    assert done.returncode == 0, done.stderr
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        assert all(abs(v) <= WEB_MERCATOR_BOUND+1e-6 for v in ds.bounds)
        assert ds.height > 10 and abs(ds.bounds.top) > WEB_MERCATOR_BOUND-2*abs(ds.transform.e)
    record = json.loads((tmp_path/'o'/'record.json').read_text(encoding='utf-8'))
    assert record['target_grid_trimmed_to_world']['requested_bounds'][3] > 2e8
    display = raster('display', tmp_path/'o'/'result.tif', tmp_path/'d', breaks=[5], colors=['#000000', '#ffffff'], unit='dimensionless', status='candidate')
    assert display.returncode == 0, display.stderr


def test_polar_disc_reaches_mercator_inside_the_square(tmp_path):
    source = tif(tmp_path/'p.tif', np.ones((80, 80), 'float32'), crs='EPSG:3031', cell=100_000, origin=(-4_000_000, 4_000_000))
    done = raster('warp', source, tmp_path/'o', crs='EPSG:3857', kind='continuous', resampling='nearest', resolution=200_000)
    assert done.returncode == 0, done.stderr
    with rio.open(tmp_path/'o'/'result.tif') as ds:
        assert ds.bounds.bottom >= -WEB_MERCATOR_BOUND


def test_trim_leaves_inner_grids_and_other_crs_alone():
    inner = from_origin(0, 1000, 10, 10)
    assert clip_grid_to_world('EPSG:3857', inner, 10, 10) == (inner, 10, 10, None)
    outer = from_origin(0, 3e8, 1e6, 1e6)
    assert clip_grid_to_world('EPSG:32650', outer, 10, 600)[3] is None
    transform, width, height, requested = clip_grid_to_world('EPSG:3857', outer, 10, 600)
    assert requested[3] == 3e8 and width == 10 and transform.f <= WEB_MERCATOR_BOUND and transform.f-height*1e6 >= -WEB_MERCATOR_BOUND
    assert transform.f == outer.f-1e6*round((outer.f-transform.f)/1e6)  # stays on the original pixel lattice
    with pytest.raises(ValueError, match='outside'):
        clip_grid_to_world('EPSG:3857', from_origin(3e7, 1000, 10, 10), 10, 10)


def test_display_names_the_actual_and_allowed_range(tmp_path):
    source = tif(tmp_path/'s.tif', np.ones((4, 4), 'float32'), crs='EPSG:3857', cell=1e7, origin=(0, 3e7))
    done = raster('display', source, tmp_path/'d', breaks=[5], colors=['#000000', '#ffffff'], unit='dimensionless', status='candidate')
    assert done.returncode == 1 and '30000000.0' in done.stderr and '20037508.3' in done.stderr


def test_budget_defaults_to_available_memory(monkeypatch):
    monkeypatch.delenv(B.ENV, raising=False)
    B.default_budget.cache_clear()
    monkeypatch.setattr(B, 'available_memory', lambda: 1000)
    assert B.budget() == 800
    with pytest.raises(B.BudgetExceeded) as caught:
        B.require(801, basis='decoded_pixels', input='x.tif')
    assert caught.value.record['budget_source'] == 'available_memory'
    B.require(800, basis='decoded_pixels', input='x.tif')
    B.default_budget.cache_clear()
    monkeypatch.setattr(B, 'available_memory', lambda: None)
    assert B.budget() is None
    B.default_budget.cache_clear()
    monkeypatch.setenv(B.ENV, 'OFF')
    assert B.budget() is None
    monkeypatch.setenv(B.ENV, '1KiB')
    assert B.budget() == 1024
    monkeypatch.undo()
    B.default_budget.cache_clear()


def test_available_memory_is_plausible():
    available = B.available_memory()
    assert available is None or 2**20 < available < 2**50


def test_cli_off_and_default_run(tmp_path, monkeypatch):
    source = tif(tmp_path/'s.tif', gradient('float32'))
    spec = tmp_path/'p.json'
    spec.write_text(json.dumps(WARP), encoding='utf-8')
    monkeypatch.delenv(B.ENV, raising=False)
    for name, flags in (('default', []), ('off', ['--memory-budget', 'off'])):
        done = subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), *flags, 'raster', 'warp', str(source), '--params', str(spec), '--output', str(tmp_path/name)],
                              capture_output=True, encoding='utf-8', errors='replace')
        assert done.returncode == 0, done.stderr


def test_unreadable_source_reaches_the_tool_not_the_estimate(tmp_path, monkeypatch):
    monkeypatch.setenv(B.ENV, '1KiB')
    B.check_vector(tmp_path/'missing.gpkg')
    plain = tmp_path/'plain.txt'
    plain.write_text('not a dataset', encoding='utf-8')
    B.check_vector(plain)


def test_bbox_estimates_only_what_the_read_touches(tmp_path, monkeypatch):
    source = tmp_path/'v.gpkg'
    gpd.GeoDataFrame({'id': range(3000)}, geometry=[box(i, 0, i+1, 1) for i in range(3000)], crs='EPSG:4326').to_file(source)
    whole, part = B.vector_bytes(source), B.vector_bytes(source, bbox=(0, 0, 3, 1))
    assert whole and part and part < whole/100
    monkeypatch.setenv(B.ENV, str((whole+part)//2))
    B.check_vector(source, bbox=(0, 0, 3, 1))
    with pytest.raises(B.BudgetExceeded):
        B.check_vector(source)


def gis(budget, tool, *arguments):
    return subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), '--memory-budget', budget, tool, *map(str, arguments)],
                          capture_output=True, encoding='utf-8', errors='replace')


def test_display_vector_and_image_tools_are_budgeted(tmp_path):
    source = tmp_path/'v.gpkg'
    gpd.GeoDataFrame({'gid': range(2000)}, geometry=[box(i*.001, 0, (i+1)*.001, 1) for i in range(2000)], crs='EPSG:4326').to_file(source)
    view = json.dumps({'center': [1, .5], 'zoom': 3, 'width': 600, 'height': 300})
    done = gis('1KiB', 'display-vector', '--source', source, '--id-field', 'gid', '--view', view, '--output', tmp_path/'dv')
    assert done.returncode == B.EXIT_CODE, done.stderr
    image = tmp_path/'i.png'
    Image.fromarray(np.zeros((400, 400, 3), 'uint8')).save(image)
    spec = tmp_path/'crop.json'
    spec.write_text(json.dumps({'center_xy': [200, 200], 'size_px': [10, 10], 'angle_deg': 0}), encoding='utf-8')
    done = gis('1KiB', 'oriented-crop', image, '--params', spec, '--output', tmp_path/'crop')
    assert done.returncode == B.EXIT_CODE, done.stderr


def test_package_and_admin_chain_are_budgeted(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    import pack_fixture
    fixture = pack_fixture.build(tmp_path/'project')
    done = gis('1', 'package', 'pack', fixture['style'], '--output', tmp_path/'pack', '--data-root', f'basemap={fixture["basemap"]}')
    assert done.returncode == B.EXIT_CODE, done.stderr
    levels = tmp_path/'levels.gpkg'
    gpd.GeoDataFrame({'GID_0': ['A'], 'COUNTRY': ['Alpha']}, geometry=[box(0, 0, 10, 10)], crs='EPSG:4326').to_file(levels, layer='ADM_0')
    done = gis('1', 'admin-chain', '--source', levels, '--point', '7,8', '--levels', 0, '--output', tmp_path/'chain')
    assert done.returncode == B.EXIT_CODE, done.stderr
