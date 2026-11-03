"""Nonlinear candidate gates and sampled-curve handoff; all fixtures synthetic."""
import json
import os
import shutil
import numpy as np
import pytest
import geopandas as gpd
import rasterio as rio
from PIL import Image
from shapely.geometry import Polygon, LineString, box
from test_professional import G, gcp_fixture, module

@pytest.fixture(autouse=True)
def backend():
    if not os.environ.get('GIS_GDALWARP') and not shutil.which('gdalwarp'):
        pytest.skip('Explicit existing GDAL required')

@pytest.mark.parametrize('gate',['world','inverse','roundtrip','coverage','fold'])
def test_tps_independent_gates(tmp_path,gate):
    image,gcps,p=gcp_fixture(tmp_path);data=json.loads(gcps.read_text(encoding='utf-8'))
    if gate=='world':data['acceptance_policy']['max_check_rmse']=0
    elif gate=='inverse':p['max_inverse_error_px']=1e-12
    elif gate=='roundtrip':p['max_roundtrip_px']=1e-12
    elif gate=='coverage':data['frame']['pixel_region']=box(0,0,49,49).__geo_interface__
    else:
        data['points'][3]['world'][0]-=80
    gcps.write_text(json.dumps(data), encoding='utf-8');r=G.execute(image,gcps,p,tmp_path/'held')
    assert r['status']=='hold' and not (tmp_path/'held/warped.tif').exists()
    expected=dict(world='Frozen world',inverse='Frozen inverse',roundtrip='Inverse roundtrip',coverage='Fit hull',fold='Sampled fold')[gate]
    assert any(expected in reason for reason in r['reasons'])

@pytest.mark.parametrize('resampling',['nearest','bilinear'])
def test_tps_holes_transparent_alpha(tmp_path,resampling):
    image,gcps,p=gcp_fixture(tmp_path);data=json.loads(gcps.read_text(encoding='utf-8'))
    a=np.array(Image.open(image).convert('RGBA'));a[18:23,4:9,3]=0;Image.fromarray(a).save(image)
    data['source_sha256']=G.digest(image)
    data['frame']['pixel_region']=Polygon(box(2,2,47,47).exterior.coords,[box(20,20,30,30).exterior.coords]).__geo_interface__
    gcps.write_text(json.dumps(data), encoding='utf-8');p['resampling']=resampling
    r=G.execute(image,gcps,p,tmp_path/'out');assert r['status']=='pass'
    base=json.loads((tmp_path/'out/affine.json').read_text(encoding='utf-8'))
    with G.transformer(base) as tr:world=G.forward(tr,[[25,25],[6,20],[35,25]])
    with rio.open(tmp_path/'out/warped.tif') as d:
        alpha=[list(d.sample([xy],indexes=4))[0][0] for xy in world]
        assert alpha==[0,0,255]
        assert d.crs==rio.crs.CRS.from_epsg(32645)
        np.testing.assert_array_equal(d.dataset_mask(),d.read(4))
        if resampling=='bilinear':
            rr,cc=np.indices((d.height,d.width));xx,yy=rio.transform.xy(d.transform,rr.ravel(),cc.ravel())
            with G.transformer(base) as tr:pix=G.inverse(tr,np.column_stack((xx,yy)))
            # Interior unmasked 2x2 support only. Predeclared uint8 quantization
            # tolerance is one DN; geometry/pixel location is never loosened.
            keep=(pix[:,0]>10)&(pix[:,0]<40)&(pix[:,1]>5)&(pix[:,1]<40)&((pix[:,0]>32)|(pix[:,1]<15))
            keep &= (np.abs(pix-np.round(pix)).min(axis=1)>1e-5)
            pix=pix[keep];rflat=rr.ravel()[keep];cflat=cc.ravel()[keep];assert len(pix)>100
            ij=np.floor(pix).astype(int);f=pix-ij;x,y=ij.T;fx,fy=f.T
            expected=(a[y,x,:3]*(1-fx)[:,None]*(1-fy)[:,None]+a[y,x+1,:3]*fx[:,None]*(1-fy)[:,None]+a[y+1,x,:3]*(1-fx)[:,None]*fy[:,None]+a[y+1,x+1,:3]*fx[:,None]*fy[:,None])
            actual=d.read()[:3,rflat,cflat].T.astype(float)
            assert np.max(np.abs(actual-expected))<=1.0


def export_curve(tmp_path,image):
    annotate=module('raster-annotate')
    nodes={'s':{'xy':[5,8]},'t':{'xy':[43,42]}}
    edge={'start':'s','end':'t','geometry':{'type':'cubic','segments':[{'c1':[40,5],'c2':[8,45]}]}}
    coords=annotate.edge_coords(edge,nodes,.01)
    frame=gpd.GeoDataFrame({'id':['cubic','crossing','hole'], 'note':['synthetic']*3},geometry=[LineString(coords),LineString([(1,1),(15,15)]),Polygon(box(7,7,42,42).exterior.coords,[box(20,20,30,30).exterior.coords])])
    path=tmp_path/'pixels.gpkg';frame.to_file(path,layer='objects',driver='GPKG')
    manifest=tmp_path/'pixels.json';manifest.write_text(json.dumps(dict(source_sha256=G.digest(image),gpkg_sha256=G.digest(path),sampling_tolerance_pixels=.01,coordinate_system='UNREFERENCED PIXEL ENGINEERING SPACE; y down; units pixels',issues=[],omitted_faces=[])), encoding='utf-8')
    return path,manifest,coords


def test_sampled_cubic_handoff_readback_and_relocation(tmp_path):
    image,gcps,p=gcp_fixture(tmp_path);path,manifest,coords=export_curve(tmp_path,image)
    original=G.digest(path);r=G.execute(image,gcps,p,tmp_path/'out',pixel_vectors=path,pixel_manifest=manifest,vector_step_px=.5)
    assert r['status']=='pass';v=r['vector_handoff'];assert len(v['omitted_objects'])==1
    assert G.digest(path)==original and v['original_curve_sampling_tolerance_px']==.01
    base=json.loads((tmp_path/'out/affine.json').read_text(encoding='utf-8'))
    out=gpd.read_file(tmp_path/'out/vectors.gpkg');assert list(out.id)==['cubic','hole']
    assert len(out.geometry.iloc[1].interiors)==1
    from shapely import segmentize
    with G.transformer(base) as tr:
        expected=G.forward(tr,np.array(segmentize(LineString(coords),.5).coords))
        controls=G.forward(tr,[[5,8],[40,5],[8,45],[43,42]])
    np.testing.assert_array_equal(np.array(out.geometry.iloc[0].coords),expected)
    # Transforming four Bezier controls does not reproduce a non-affine transformed curve.
    t=np.linspace(0,1,1001)[:,None];wrong=(1-t)**3*controls[0]+3*(1-t)**2*t*controls[1]+3*(1-t)*t*t*controls[2]+t**3*controls[3]
    assert out.geometry.iloc[0].hausdorff_distance(LineString(wrong))>.02
    shutil.move(tmp_path/'out',tmp_path/'moved');root=tmp_path/'moved'
    for file_key,hash_key in [('file','sha256'),('source_manifest','source_manifest_sha256')]:assert G.digest(root/v[file_key])==v[hash_key]
    assert G.digest(root/v['source_pixels'])==original
    assert G.digest(root/r['transform_record'])==r['transform_record_sha256']
    # A different export is rejected, not silently rebound to an image.
    meta=json.loads(manifest.read_text(encoding='utf-8'));meta['source_sha256']='0'*64;manifest.write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError,match='source or GPKG hash'):
        G.execute(image,gcps,p,tmp_path/'bad',pixel_vectors=path,pixel_manifest=manifest,vector_step_px=.5)
    assert not (tmp_path/'bad').exists()
