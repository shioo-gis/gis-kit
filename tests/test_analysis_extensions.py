"""Analysis-extension numerical, CLI, provenance and failure-path regressions."""
import importlib.util
import json
from pathlib import Path
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from PIL import Image
from shapely.geometry import Point, Polygon, box

from test_daily import frame, cli, run, write, SCRIPTS
import _analysis as a


def module(name):
    spec=importlib.util.spec_from_file_location('analysis_ext_'+name,SCRIPTS/(name+'.py'))
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

raster=module('raster'); coverage=module('coverage')


def tif(path,values=None,transform=None,mask=None,crs='EPSG:26918'):
    values=np.array([[1,2],[3,-99]],dtype='float64') if values is None else np.asarray(values,dtype='float64')
    with rio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rio.open(path,'w',driver='GTiff',width=values.shape[1],height=values.shape[0],count=1,dtype='float64',crs=crs,
                      transform=transform or from_origin(0,2,1,1),nodata=-99) as ds:
            ds.write(values,1)
            if mask is not None: ds.write_mask(np.asarray(mask,dtype='uint8'))
    return path


def rcli(root,name,op,inputs,params,success=True):
    spec=root/(name+'.json');spec.write_text(json.dumps(params), encoding='utf-8');output=root/name
    cli('raster.py',op,*inputs,'--params',spec,'--output',output,success=success)
    if not success: assert not output.exists();return
    return json.loads((output/'record.json').read_text(encoding='utf-8'))


def test_version_order_split_merge():
    left=frame([box(0,0,2,2),box(4,0,5,1),box(5,0,6,1),box(9,0,10,1)],id=['split','m1','m2','same'],value=[1,2,3,4])
    right=frame([box(0,0,1,2),box(1,0,2,2),box(4,0,6,1),Polygon(list(box(9,0,10,1).exterior.coords)[::-1])],id=['s1','s2','merge','same'],value=[1,1,5,4])
    result,details=a.compare(left,right,{'id':'id'})
    assert result.set_index('source_id').loc['same','change']=='unchanged'
    assert [x['proposal'] for x in details['candidates']].count('split')==2
    assert [x['proposal'] for x in details['candidates']].count('merge')==2
    assert all(x['status']=='unknown' for x in details['candidates'])
    again,_=a.compare(left.iloc[::-1],right.iloc[::-1],{'id':'id'})
    pd.testing.assert_frame_equal(result,again)


def test_time_half_open_unknown():
    data=frame([Point(0,0)]*3,id=['a','b','c'],start=['2000-01-01','2001-01-01',None],end=['2001-01-01','2002-01-01',None])
    out,details=a.time_slice(data,{'id':'id','start':'start','end':'end','at':'2001-01-01'})
    assert list(out.id)==['b'];assert details['unknown_ids']==['c']
    out,_=a.time_slice(data,{'id':'id','start':'start','end':'end','at':'2001-01-01','unknown':'open'})
    assert list(out.id)==['b','c']


def test_grid_boundary_and_unmatched():
    points=frame([Point(1,.5),Point(3,3),Point(.5,.5)],id=['p','out','in'])
    cells=frame([box(0,0,1,1),box(1,0,2,1)],id=['a','b'])
    result,details=a.grid_summary(points,cells,{'id':'id','right_id':'id'})
    assert list(result.point_count)==[2,0]; assert details['unmatched']==1
    assert result.density_per_km2.iloc[0]==2e6


def test_profile_distribution_cleanup_cluster():
    data=frame([Point(.01,0),Point(.02,0),Point(10,10)],id=['a','b','c'],v=[1,2,None])
    _,details=a.profile(data,{'required_fields':['speed']});assert details['missing_conditions']==['speed']
    out,_=a.distribution(data,{'value':'v'});assert out['mean'][0]==1.5;assert out['missing'][0]==1
    out,_=a.cleanup(data,None,{'id':'id','method':'near_duplicates','tolerance_m':.1});assert len(out)==1
    out,_=a.cluster(data,{'id':'id','eps_m':1,'min_samples':2}); assert list(out.cluster_id)==['cluster:a','cluster:a','noise']
    again,_=a.cluster(data.iloc[::-1],{'id':'id','eps_m':1,'min_samples':2});assert list(again.cluster_id)==list(out.cluster_id)


def test_vector_cli_trace(tmp_path):
    old=write(tmp_path,'old',frame([Point(0,0)],id=['a'],value=[1]))
    new=write(tmp_path,'new',frame([Point(1,0)],id=['a'],value=[2]))
    artifact,record=run(tmp_path,'difference','compare',old,{'id':'id'},new)
    assert len(record['inputs'])==2;assert record['implementation_sha256']['_analysis.py']
    assert pd.read_csv(artifact).geometry_changed.iloc[0]
    run(tmp_path,'profile','profile',old,{'required_fields':['x']})
    run(tmp_path,'describe','distribution',old,{'value':'value'})


@pytest.mark.parametrize('method,expected', [('center',2),('all_touched',2),('fractional',2)])
def test_zonal_blocks_and_nodata(tmp_path,method,expected):
    path=tif(tmp_path/'known.tif'); zones=frame([box(0,0,2,2)],id=['all'])
    with rio.open(path) as ds:
        x=raster.zonal(ds,zones,{'id':'id','method':method,'block_size':1,'histogram':True})
        y=raster.zonal(ds,zones,{'id':'id','method':method,'block_size':16,'histogram':True})
    assert x==y; assert x['zones'][0]['mean']==expected; assert x['zones'][0]['valid_pixels']==3


def test_fractional_half_pixel_hole(tmp_path):
    path=tif(tmp_path/'known.tif',[[1,2],[3,4]])
    zones=frame([box(.5,0,1.5,2),Polygon(box(0,0,2,2).exterior.coords,[box(.5,.5,1.5,1.5).exterior.coords])],id=['half','hole'])
    with rio.open(path) as ds:
        result=raster.zonal(ds,zones,{'id':'id','method':'fractional','block_size':1})
    assert result['zones'][0]['sum']==5;assert result['zones'][0]['weight']==2
    assert result['zones'][1]['sum']==7.5;assert result['zones'][1]['weight']==3


def test_internal_mask_zero_and_histogram(tmp_path):
    path=tif(tmp_path/'mask.tif',[[0,1],[2,3]],mask=[[255,0],[255,255]])
    result=rcli(tmp_path,'hist','histogram',[path],{'block_size':1})
    assert result['histogram']=={'0.0':1,'2.0':1,'3.0':1}
    rcli(tmp_path,'copy','copy',[path],{})
    with rio.open(tmp_path/'copy/result.tif') as ds:
        a=ds.read(1,masked=True);assert a[0,0]==0;assert a.mask[0,1]


def test_raster_cli_chain(tmp_path):
    path=tif(tmp_path/'known.tif');mask=write(tmp_path,'zones',frame([box(0,0,1,2)],id=['left']))
    rcli(tmp_path,'clip','clip',[path],{'vector':str(mask),'block_size':1})
    rcli(tmp_path,'calc','calculate',[tmp_path/'clip/result.tif'],{'scale':2,'offset':1})
    record=rcli(tmp_path,'zonal','zonal',[tmp_path/'calc/result.tif'],{'vector':str(mask),'id':'id','method':'fractional'})
    assert record['zones'][0]['sum']==10;assert record['zones'][0]['mean']==5
    rcli(tmp_path,'cog','copy',[path],{'format':'COG'})
    with rio.open(tmp_path/'cog/result.tif') as ds: assert ds.tags(ns='IMAGE_STRUCTURE')['LAYOUT']=='COG'
    points=write(tmp_path,'points',frame([Point(.5,1.5),Point(1.5,.5),Point(2,0)],id=['valid','nodata','outside']))
    record=rcli(tmp_path,'sample','sample',[path],{'vector':str(points),'id':'id'})
    assert [x['value'] for x in record['samples']]==[1,None,None]


def test_alignment_categorical_mosaic(tmp_path):
    path=tif(tmp_path/'known.tif',[[1,2],[3,4]])
    reference=tif(tmp_path/'reference.tif',transform=from_origin(.5,2.5,1,1))
    rcli(tmp_path,'align','align',[path],{'reference':str(reference),'kind':'categorical'})
    with rio.open(tmp_path/'align/result.tif') as ds:
        assert ds.transform==from_origin(.5,2.5,1,1);assert set(ds.read(1,masked=True).compressed())<={1,2,3,4}
    rcli(tmp_path,'bad','align',[path],{'reference':str(reference),'kind':'categorical','resampling':'bilinear'},success=False)
    rcli(tmp_path,'badmosaic','mosaic',[path,reference],{'overlap':'first'},success=False)
    rcli(tmp_path,'mosaic','mosaic',[path,path],{'overlap':'first'})
    with rio.open(tmp_path/'mosaic/result.tif') as ds: np.testing.assert_array_equal(ds.read(1),[[1,2],[3,4]])
    rcli(tmp_path,'reclass','reclassify',[path],{'mapping':{'1':10,'2':20,'3':30,'4':40},'unmapped':'error'})
    rcli(tmp_path,'badclass','reclassify',[path],{'mapping':{'1':10},'unmapped':'error'},success=False)


def ledger_fixture(tmp_path):
    image=tmp_path/'source.png';Image.new('RGB',(10,10),'white').save(image)
    run=tmp_path/'annotation';coverage.annotation.init(image,run,'synthetic ledger validation')
    meta,_=coverage.annotation.source(run)
    ledger={'schema_version':1,'source_sha256':meta['sha256'],'revision':0,'revision_sha256':coverage.annotation.digest(run/'revisions/000000.json'),
            'regions':[{'id':'main','kind':'main','bounds':[0,0,10,10],'categories':['border']}],
            'observations':[{'region':'main','category':'border','bounds':[0,0,10,10],'evidence':'synthetic fixture','drawn':True,'empty_reason':'blank fixture'}],
            'scope_review':{'status':'accepted','reviewer':'test fixture','evidence':'synthetic'}}
    return run,ledger


def test_coverage_not_observation_count(tmp_path):
    run,ledger=ledger_fixture(tmp_path)
    assert coverage.audit(run,ledger)['status']=='hold'
    ledger['observations'][0].update(review='accepted',reviewer='test fixture')
    assert coverage.audit(run,ledger)['status']=='complete'
    state=coverage.annotation.load(run);state['revision']=1
    coverage.annotation.write(run/'revisions/000001.json',state)
    result=coverage.audit(run,ledger);assert result['status']=='hold';assert result['regions'][0]['reviewed']==0


def test_coverage_cli_inset_pending_and_source(tmp_path):
    run,ledger=ledger_fixture(tmp_path)
    ledger['regions'].append({'id':'inset','kind':'inset','bounds':[0,0,2,2],'categories':['border']})
    ledger['observations'][0].update(review='accepted',reviewer='fixture')
    path=tmp_path/'ledger.json';path.write_text(json.dumps(ledger), encoding='utf-8');out=tmp_path/'coverage.json'
    cli('coverage.py',run,'--ledger',path,'--output',out)
    assert json.loads(out.read_text(encoding='utf-8'))['status']=='hold'
    Image.new('RGB',(10,10),'black').save(run/'source.png')
    with pytest.raises(ValueError,match='hash'): coverage.audit(run,ledger)


def test_coverage_seams_require_both_observations(tmp_path):
    run,ledger=ledger_fixture(tmp_path)
    state=coverage.annotation.load(run)
    state['nodes']={'n':{'xy':[5,0]},'m':{'xy':[5,10]}}
    state['edges']={'e':{'start':'n','end':'m','kind':'border','status':'visible','geometry':{'type':'polyline','vertices':[]}}}
    state['revision']=1; coverage.annotation.write(run/'revisions/000001.json',state)
    ledger['revision']=1;ledger['revision_sha256']=coverage.annotation.digest(run/'revisions/000001.json')
    ledger['regions'].append({'id':'right','kind':'main','bounds':[5,0,10,10],'categories':['border']})
    ledger['seams']=[{'node':'n','edge':'e','regions':['main','right'],'status':'confirmed','direction':'south','reviewer':'fixture','observations':{'main':'visible'}}]
    assert not coverage.audit(run,ledger)['seams'][0]['confirmed']
    ledger['seams'][0]['observations']['right']='visible'
    assert coverage.audit(run,ledger)['seams'][0]['confirmed']
    ledger['seams'][0]['regions']=['main','main']
    with pytest.raises(ValueError): coverage.audit(run,ledger)


@pytest.mark.parametrize('mode',['center','all_touched','fractional'])
def test_zonal_large_crossblock_hole_and_edges(tmp_path,mode):
    values=np.arange(35*37,dtype=float).reshape(35,37);values[3:6,9:14]=-99
    path=tif(tmp_path/'large.tif',values,from_origin(0,35,1,1))
    zones=frame([Polygon(box(2.2,1.4,30.1,34.7).exterior.coords,[box(8.5,9.5,15.5,17.5).exterior.coords]),box(100,100,101,101)],id=['hole','outside'])
    with rio.open(path) as ds:
        x=raster.zonal(ds,zones,{'id':'id','method':mode,'block_size':8})['zones']
        y=raster.zonal(ds,zones,{'id':'id','method':mode,'block_size':128})['zones']
    assert x[0]['sum']==pytest.approx(y[0]['sum'],abs=1e-8)
    assert x[0]['weight']==pytest.approx(y[0]['weight'],abs=1e-10)
    assert x[1]['mean'] is None


def test_raster_failure_guards_and_conditional(tmp_path):
    path=tif(tmp_path/'known.tif')
    rcli(tmp_path,'conditional','calculate',[path],{'threshold':2,'true':10,'false':0})
    with rio.open(tmp_path/'conditional/result.tif') as ds:
        out=ds.read(1); np.testing.assert_array_equal(out,[[0,10],[10,np.nan]])
    rcli(tmp_path,'overflow','calculate',[path],{'threshold':2,'true':float('inf'),'false':0},success=False)
    rcli(tmp_path,'size','copy',[path],{'max_pixels':1},success=False)
    rcli(tmp_path,'band','copy',[path],{'band':2},success=False)
    rcli(tmp_path,'warp','warp',[path],{'kind':'continuous','crs':'EPSG:26918','resampling':'bilinear','resolution':.5})
    with rio.open(tmp_path/'warp/result.tif') as ds: assert ds.width==4 and ds.height==4
    rcli(tmp_path,'extra','copy',[path,path],{},success=False)
    assert not list(tmp_path.glob('.raster-*'))


def test_precision_snap_hold_and_collapse(tmp_path):
    data=frame([box(.01,.01,1.01,1.01)],id=['a'])
    out,details=a.cleanup(data,None,{'id':'id','method':'precision','tolerance_m':.1})
    assert out.geometry.iloc[0].equals(box(0,0,1,1));assert details['impacts'][0]['before_holes']==0
    with pytest.raises(ValueError,match='collapsed'): a.cleanup(data,None,{'id':'id','method':'precision','tolerance_m':10})
    source=write(tmp_path,'source',data)
    _,record=run(tmp_path,'candidate','cleanup',source,{'id':'id','method':'precision','tolerance_m':.1})
    assert record['status']=='hold'


def test_adoption_rechecks_approval_and_preserves_attributes(tmp_path):
    from daily import fingerprint
    source=write(tmp_path,'source',frame([box(.01,.01,1.01,1.01)],id=['a'],v=[1]))
    candidate=write(tmp_path,'candidate',frame([box(0,0,1,1)],id=['a'],v=[999]))
    params={'id':'id','approved_by':'test fixture','source_sha256':fingerprint(source),'candidate_sha256':fingerprint(candidate),
            'max_displacement_m':.02,'max_area_change_m2':.001,'rules':[{'id':'positive','kind':'compare','field':'v','op':'gt','value':0}]}
    artifact,record=run(tmp_path,'adopt','cleanup_adopt',source,params,candidate)
    assert record['details']['adopted']; assert gpd.read_file(artifact).v.iloc[0]==1
    params['candidate_sha256']='stale'
    run(tmp_path,'stale','cleanup_adopt',source,params,candidate,success=False)


def test_split_edges_reconstruction():
    from shapely.geometry import LineString
    data=frame([LineString([(0,0),(2,2)]),LineString([(0,2),(2,0)])],id=['a','b'])
    result,details=a.cleanup(data,None,{'id':'id','method':'split_edges','tolerance_m':.1})
    assert len(result)==4; assert result.geometry.union_all().equals(data.geometry.union_all())
    assert details['status']=='candidate'


def test_rotation_warp_and_geographic_fraction_guard(tmp_path):
    from affine import Affine
    source=tif(tmp_path/'rotated.tif',transform=from_origin(0,2,1,1)*Affine.rotation(10))
    rcli(tmp_path,'inspectrot','inspect',[source],{})
    rcli(tmp_path,'warprot','warp',[source],{'crs':'EPSG:26918','kind':'categorical'})
    with rio.open(tmp_path/'warprot/result.tif') as ds: assert ds.transform.b==0 and ds.transform.d==0
    geographic=tif(tmp_path/'geo.tif',crs='EPSG:4326')
    with rio.open(geographic) as ds:
        with pytest.raises(ValueError,match='projected'): raster.zonal(ds,frame([box(0,0,1,1)],id=['x']),{'id':'id','method':'fractional'})


def test_profile_without_crs_and_category_transfers(tmp_path):
    data=frame([Point(0,0),None],id=['a','b'],category=['A',None]);data=data.set_crs(None,allow_override=True)
    source=write(tmp_path,'unknown',data)
    _,record=run(tmp_path,'profile_unknown','profile',source,{'id':'id'})
    assert record['details']['anomaly_ids']==['b'];assert 'CRS' in record['details']['missing_conditions']
    before=frame([Point(0,0),Point(1,1)],id=['a','b'],category=['A',None])
    after=before.copy();after['category']=['B',None]
    _,details=a.compare(before,after,{'id':'id','category':'category'})
    assert details['category_transfers']==[{'before':'A','after':'B','count':1},{'before':None,'after':None,'count':1}]


def test_positive_integer_parameters():
    for value in (0,-1,1.5,True):
        with pytest.raises(ValueError): raster.positive_integer(value)
    assert raster.positive_integer(2)==2


def test_shared_simplification_capability_gate(monkeypatch):
    monkeypatch.setattr(a.shapely, "coverage_simplify", None, raising=False)
    data=frame([box(0,0,1,1)],id=['a'])
    with pytest.raises(ValueError,match='coverage_simplify|Coverage simplification'):
        a.cleanup(data,None,{'id':'id','method':'coverage_simplify','tolerance_m':.1})


def test_nodata_metadata_and_calibration_guard(tmp_path):
    source=tif(tmp_path/'source.tif')
    rcli(tmp_path,'copy_nan','copy',[source],{})
    with rio.open(tmp_path/'copy_nan/result.tif') as ds: assert raster.inspect(ds)['nodata']=='nan'
    with rio.open(source,'r+') as ds: ds.scales=(.1,)
    with rio.open(source) as ds:
        assert raster.inspect(ds)['scales']==(.1,)
        with pytest.raises(ValueError,match='Calibrated'): raster.band_data(ds,1)
