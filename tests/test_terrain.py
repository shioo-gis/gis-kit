import sys
from pathlib import Path
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import terrain

P = dict(vertical_unit='metre', vertical_datum='unknown', source_description='synthetic regression')


def raster(path, z=None, crs='EPSG:32645', transform=None):
    z = np.ones((7, 7)) if z is None else z
    with rio.open(path, 'w', driver='GTiff', width=z.shape[1], height=z.shape[0], count=1, dtype='float64', crs=crs,
                  transform=transform or from_origin(500000, 3100000, 2, 3), nodata=np.nan) as d:
        d.write(z, 1)
    return path


@pytest.mark.parametrize('gx,gy,aspect', [(1,0,270),(-1,0,90),(0,1,180),(0,-1,0),(1,1,225)])
def test_plane(gx, gy, aspect):
    r,c = np.indices((7, 9)); z = gx*c*2-gy*r*3
    out = terrain.derivatives(z, 2, 3)
    np.testing.assert_allclose(out['slope'][1:-1,1:-1], np.degrees(np.arctan(np.hypot(gx,gy))))
    np.testing.assert_allclose(out['aspect'][1:-1,1:-1], aspect)
    np.testing.assert_allclose(out['relief'][1:-1,1:-1], 4*abs(gx)+6*abs(gy))
    assert np.isnan(out['slope'][0]).all()


def test_flat_and_hole():
    z = np.ones((9,9)); z[4,4] = np.nan
    out = terrain.derivatives(z,1,1)
    assert np.isnan(out['aspect']).all()
    assert np.isfinite(out['slope']).sum() == 40
    assert np.nanmax(out['slope']) == 0
    assert np.isnan(out['relief'][3:6,3:6]).all()


@pytest.mark.parametrize('changes', [{'vertical_unit':'unknown'}, {'surprise':1}, {'max_pixels':1}, {'band':0}, {'band':2}, {'vertical_datum':''}, {'schema_version':2}])
def test_reject_and_cleanup(tmp_path, changes):
    p = raster(tmp_path/'in.tif'); target = tmp_path/'out'
    with pytest.raises((ValueError, IndexError)):
        terrain.execute(p, dict(P, **changes), target)
    assert not target.exists()
    assert sorted(x.name for x in tmp_path.iterdir()) == ['in.tif']


@pytest.mark.parametrize('crs', [None, 'EPSG:4326'])
def test_bad_crs(tmp_path, crs):
    with pytest.raises(ValueError): terrain.execute(raster(tmp_path/'in.tif', crs=crs), P, tmp_path/'out')


def test_feet_and_readback(tmp_path):
    r,c = np.indices((7,7)); p = raster(tmp_path/'in.tif', c.astype(float), 'EPSG:2263', from_origin(1000000,200000,1,1))
    terrain.execute(p, dict(P, vertical_unit='us_survey_foot'), tmp_path/'out')
    with rio.open(tmp_path/'out/slope.tif') as d:
        np.testing.assert_allclose(d.read(1)[1:-1,1:-1],45)
    with pytest.raises(ValueError): terrain.execute(p,P,tmp_path/'out')


def test_mask_calibration_and_no_valid(tmp_path):
    p = raster(tmp_path/'in.tif')
    with rio.open(p,'r+') as d: d.scales = [2]
    with pytest.raises(ValueError, match='Calibrated'): terrain.execute(p,P,tmp_path/'out')
    p = raster(tmp_path/'in2.tif', np.full((7,7),np.nan))
    with pytest.raises(ValueError, match='No complete'): terrain.execute(p,P,tmp_path/'out')


def test_failed_readback_not_published(tmp_path, monkeypatch):
    p = raster(tmp_path/'in.tif'); original = terrain.derivatives
    def broken(*args):
        result = original(*args); result['relief'] = result['relief'][:2,:2]; return result
    monkeypatch.setattr(terrain,'derivatives',broken)
    with pytest.raises(ValueError,match='readback'): terrain.execute(p,P,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_rotated_and_sidecar(tmp_path):
    from affine import Affine
    p = raster(tmp_path/'in.tif', transform=Affine(2,.1,500000,0,-3,3100000))
    with pytest.raises(ValueError,match='North-up'): terrain.execute(p,P,tmp_path/'out')
    Path(str(p)+'.aux.xml').write_text('<PAMDataset/>', encoding='utf-8')
    with pytest.raises(ValueError,match='sidecars'): terrain.execute(p,P,tmp_path/'out')


def test_declared_unit_conflict(tmp_path):
    p=raster(tmp_path/'in.tif')
    with rio.open(p,'r+') as d:d.set_band_unit(1,'ft')
    with pytest.raises(ValueError,match='conflicts'): terrain.execute(p,P,tmp_path/'out')


def test_internal_mask(tmp_path):
    p=raster(tmp_path/'in.tif')
    with rio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rio.open(p,'r+') as d:
            m=np.full((7,7),255,dtype='uint8');m[3,3]=0;d.write_mask(m)
    terrain.execute(p,P,tmp_path/'out')
    with rio.open(tmp_path/'out/slope.tif') as d:
        a=d.read(1); assert np.isfinite(a).sum()==16 and np.isnan(a[2:5,2:5]).all()
