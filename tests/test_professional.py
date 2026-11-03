import importlib.util
import json
import os
from pathlib import Path
import sys
import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from PIL import Image
from shapely.geometry import Point,LineString,box

S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
def module(name):
    sp=importlib.util.spec_from_file_location(name,S/f'{name}.py');m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m
U=module('suitability');N=module('network');I=module('spatial-inference');G=module('nonlinear-georef');H=module('hydro-review')


def objects():return gpd.GeoDataFrame({'id':['b','a','c'],'a':[10,0,np.nan],'b':[0,10,5]},geometry=[Point(10,0),Point(0,0),Point(5,0)],crs=32645)
def score_params():return dict(id='id',study='synthetic score',criteria={'a':dict(field='a',low=0,high=10,prefer='high'),'b':dict(field='b',low=0,high=10,prefer='high')},scenarios=[dict(id='base',weights={'a':3,'b':1},threshold=.5),dict(id='other',weights={'a':1,'b':3},threshold=.5)])


def test_score_weights_constraints_stability():
    out,d=U.score(objects(),score_params());base=out[out.scenario=='base'].set_index('source_id');assert base.loc['b','score']==.75 and base.loc['a','score']==.25 and np.isnan(base.loc['c','score'])
    assert not any(x['stable_selected'] for x in d['stability'])
    restriction=gpd.GeoDataFrame(geometry=[box(10,0,11,1)],crs=32645)
    out,_=U.score(objects(),score_params(),restriction);assert not out[out.source_id=='b'].eligible.any()
    again,_=U.score(objects().iloc[::-1],score_params());assert out.shape==again.shape
    original,_=U.score(objects(),score_params());assert original.drop(columns='geometry').equals(again.drop(columns='geometry'))


@pytest.mark.parametrize('weights',[{'a':0,'b':0},{'a':-1,'b':2},{'a':1},{'a':float('inf'),'b':1}])
def test_bad_weights(weights):
    p=score_params();p['scenarios'][0]['weights']=weights
    with pytest.raises(ValueError):U.score(objects(),p)


def network_fixture():
    edges=gpd.GeoDataFrame({'id':['ab','ac','cb'],'u':['a','a','c'],'v':['b','c','b'],'direction':['forward','both','both'],'speed':[1,10,10]},geometry=[LineString([(0,0),(10,0)]),LineString([(0,0),(5,5)]),LineString([(5,5),(10,0)])],crs=32645)
    origins=gpd.GeoDataFrame({'id':['start','far']},geometry=[Point(0,0),Point(100,100)],crs=32645)
    facilities=gpd.GeoDataFrame({'id':['end']},geometry=[Point(10,0)],crs=32645)
    p=dict(edge_id='id',**{'from':'u','to':'v'},direction='direction',speed_kmh='speed',mode='shortest',analysis_crs='EPSG:32645',turn_restrictions='not_modelled',cost_assumptions='test',origin_id='id',facility_id='id',snap_m=1,budget=12,facility_sets={'baseline':['end']})
    return edges,origins,facilities,p


def test_shortest_fastest_and_unmatched():
    e,o,f,p=network_fixture();routes,service,d=N.analyze(e,o,f,p);assert d['od'][0]['status']=='origin_unsnapped'
    path=[x for x in d['od'] if x['origin_id']=='start'][0];assert path['cost']==10 and path['edge_keys']==['ab:f']
    p['mode']='fastest';_,_,d=N.analyze(e,o,f,p);path=[x for x in d['od'] if x['origin_id']=='start'][0];assert path['edge_keys']==['ac:f','cb:f'];assert path['cost']==pytest.approx(2*np.sqrt(50)/ (10/3.6))
    p['mode']='shortest';p['budget']=3;_,service,_=N.analyze(e,o,f,p);assert service.length.max()==pytest.approx(3)


def test_network_direction_parallel_ties_and_coordinate_failure():
    e,o,f,p=network_fixture();e=e.iloc[:1].copy();o.geometry=[Point(10,0),Point(100,100)];f.geometry=[Point(0,0)]
    _,_,d=N.analyze(e,o,f,p);assert all(x['cost'] is None for x in d['od'])
    e,o,f,p=network_fixture();e.loc[1,'u']='b'
    with pytest.raises(ValueError,match='inconsistent'):N.analyze(e,o,f,p)


def test_network_reorder():
    e,o,f,p=network_fixture();_,_,a=N.analyze(e,o,f,p);_,_,b=N.analyze(e.iloc[::-1],o.iloc[::-1],f,p);assert a==b


def test_kde_manual(tmp_path):
    g=gpd.GeoDataFrame({'id':['a','b']},geometry=[Point(1,1),Point(3,1)],crs=32645)
    p=dict(id='id',study='synthetic',analysis_crs=32645,operation='kde',bandwidth_m=2,resolution_m=1,bounds=[0,0,4,4]);g,xy,factor=I.prepare(g,p);I.kde(g,xy,factor,p,tmp_path)
    with rio.open(tmp_path/'density.tif') as d:a=d.read(1)
    expected=sum(np.exp(-np.sum((q-np.array([.5,3.5]))**2)/(2*4))/(2*np.pi*4)*1e6 for q in xy)
    assert a[0,0]==pytest.approx(expected)


@pytest.fixture
def pysal_backend():
    path=os.environ.get('GIS_TEST_PYSAL_PYTHON')
    if not path:pytest.skip('Explicit existing PySAL backend required')
    return dict(python=path,site_packages=os.environ.get('GIS_TEST_PYSAL_SITE'),proj_data=os.environ.get('GIS_TEST_PYSAL_PROJ'))


def test_pysal_numerical_and_seed(tmp_path,pysal_backend):
    g=gpd.GeoDataFrame({'id':list('abcdef'),'value':[2,3,3.2,5,8,7]},geometry=[Point(*p) for p in [(10,10),(20,10),(40,10),(15,20),(30,20),(30,30)]],crs=32645)
    p=dict(id='id',value='value',study='synthetic reference',analysis_crs=32645,operation='infer',radius_m=15,permutations=99,seed=10)
    for j in range(2):
        stage=tmp_path/str(j);stage.mkdir();frame,xy,_=I.prepare(g if j==0 else g.iloc[::-1],p);d=I.infer(frame,xy,p,stage,pysal_backend)
    a=gpd.read_file(tmp_path/'0/statistics.gpkg');b=gpd.read_file(tmp_path/'1/statistics.gpkg');assert a.drop(columns='geometry').equals(b.drop(columns='geometry'))
    y=np.array(g.value);z=y-y.mean();w=(np.linalg.norm(np.array([[p.x,p.y] for p in g.geometry])[:,None,:]-np.array([[p.x,p.y] for p in g.geometry])[None,:,:],axis=2)<=15).astype(float);np.fill_diagonal(w,0);w/=w.sum(axis=1)[:,None]
    assert d['global_I']==pytest.approx(len(y)/w.sum()*z@w@z/(z@z))
    np.testing.assert_allclose(a.local_I,(len(y)-1)*z*(w@z)/(z@z))
    assert np.all(a.local_q>=a.local_p)


def gcp_fixture(tmp_path):
    image=tmp_path/'source.png';yy,xx=np.indices((50,50));Image.fromarray(np.stack((xx*5,yy*5,(xx+yy)*2),axis=-1).astype('uint8')).save(image)
    def world(x,y):return [400000+2*x+.002*y*y,3000000-2*y]
    fit=[(2,2),(25,2),(47,2),(47,25),(47,47),(25,47),(2,47),(2,25)];check=[(12,12),(37,12),(12,37),(37,37)]
    data=dict(actor={'model':'gpt-6-astra','reasoning_effort':'low'},source_sha256=G.digest(image),source_crs='EPSG:32645',target_crs='EPSG:32645',coordinate_reference='SYNTHETIC regression, no real model observation',frame=dict(id='test',pixel_region=box(2,2,47,47).__geo_interface__),acceptance_policy=dict(max_check_rmse=2,max_check_error=3,min_fit_coverage=.95,rationale='Synthetic bounded polynomial test'),points=[dict(id=str(i),pixel=list(p),world=world(*p),role='fit' if i<len(fit) else 'check') for i,p in enumerate(fit+check)])
    gcps=tmp_path/'gcps.json';gcps.write_text(json.dumps(data), encoding='utf-8');policy=dict(grid_step_px=4,max_inverse_error_px=2,max_roundtrip_px=1,max_condition=10,resolution=1.7,resampling='nearest',rationale='Synthetic regression')
    return image,gcps,policy


def test_tps_frozen_checks_mask_and_source(tmp_path):
    if not os.environ.get('GIS_GDALWARP') and not __import__('shutil').which('gdalwarp'):pytest.skip('Explicit existing gdalwarp required')
    image,gcps,p=gcp_fixture(tmp_path);report=G.execute(image,gcps,p,tmp_path/'out');assert report['status']=='pass'
    with rio.open(tmp_path/'out/warped.tif') as d:
        a=d.read();assert d.count==4 and np.any(a[3]==0) and np.any(a[3]>0)
        # Independent nearest-pixel check using GDAL inverse and stored target pixel centres.
        rr,cc=np.where(a[3]>0);rr,cc=rr[::20],cc[::20];xx,yy=rio.transform.xy(d.transform,rr,cc)
        base=json.loads((tmp_path/'out/affine.json').read_text(encoding='utf-8'))
        with G.transformer(base) as tr:pixel=G.inverse(tr,np.column_stack((xx,yy)))
        keep=(np.abs(pixel+.5-np.round(pixel+.5)).min(axis=1)>1e-6);assert keep.sum()>20
        pixel=pixel[keep];rr=rr[keep];cc=cc[keep]
        expected=np.array(Image.open(image));rc=np.floor(pixel+.5).astype(int)
        np.testing.assert_allclose(a[:3,rr,cc].T,expected[rc[:,1],rc[:,0]],atol=0)
    p['max_condition']=.5;held=G.execute(image,gcps,p,tmp_path/'held');assert held['status']=='hold' and not (tmp_path/'held/warped.tif').exists()
    data=json.loads(gcps.read_text(encoding='utf-8'));data['points'][-1]['pixel']=data['points'][0]['pixel'];gcps.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError,match='Duplicate pixel'):G.execute(image,gcps,p,tmp_path/'bad')


def test_suitability_real_cli_file(tmp_path):
    source=tmp_path/'input.gpkg';objects().to_file(source,driver='GPKG');U.execute(source,score_params(),tmp_path/'out');assert len(gpd.read_file(tmp_path/'out/scores.gpkg'))==6


def test_hydro_frozen_tolerance_and_outside(tmp_path):
    source=tmp_path/'rivers.gpkg';streams=tmp_path/'streams.tif'
    rivers=gpd.GeoDataFrame({'id':['match','far','outside']},geometry=[LineString([(5,5),(5,25)]),LineString([(25,5),(25,25)]),LineString([(45,5),(45,25)])],crs=32645)
    rivers.to_file(source,driver='GPKG');a=np.zeros((4,4),dtype='uint8');a[:,0]=1
    with rio.open(streams,'w',driver='GTiff',width=4,height=4,count=1,dtype='uint8',transform=rio.transform.from_origin(0,40,10,10),crs=32645) as d:d.write(a,1)
    p=dict(id='id',spacing_m=10,tolerance_m=0,min_matched_fraction=1,rationale='Exact synthetic stream centres')
    H.execute(source,streams,p,tmp_path/'out');r=json.loads((tmp_path/'out/record.json').read_text(encoding='utf-8'));by={v['source_id']:v for v in r['reaches']}
    assert r['status']=='hold' and by['match']['status']=='pass' and by['far']['matched_fraction']==0 and by['outside']['outside']>0
    p['tolerance_m']=-1
    with pytest.raises(ValueError):H.execute(source,streams,p,tmp_path/'invalid')
    assert not (tmp_path/'invalid').exists()


def test_pysal_islands_and_degenerate_gi(tmp_path,pysal_backend):
    g=gpd.GeoDataFrame({'id':list('abcde'),'v':[1,2,3,4,5]},geometry=[Point(x,0) for x in (0,1,2,3,100)],crs=32645)
    p=dict(id='id',value='v',study='Synthetic island',analysis_crs=32645,operation='infer',radius_m=4,permutations=19,seed=8)
    frame,xy,_=I.prepare(g,p);I.infer(frame,xy,p,tmp_path,pysal_backend);out=gpd.read_file(tmp_path/'statistics.gpkg')
    assert out.loc[4,'island'] and np.isnan(out.loc[4,'local_q']) and np.isnan(out.loc[4,'gi_q'])
    p['radius_m']=200;stage=tmp_path/'complete';stage.mkdir();I.infer(frame,xy,p,stage,pysal_backend);out=gpd.read_file(stage/'statistics.gpkg')
    assert out.gi_q.isna().all()


def test_coverage_increment_and_same_node():
    e,o,f,p=network_fixture();f=gpd.GeoDataFrame({'id':['end','at_start']},geometry=[Point(10,0),Point(0,0)],crs=32645)
    p['budget']=0;p['facility_sets']={'baseline':['end'],'expanded':['end','at_start']}
    routes,_,d=N.analyze(e,o,f,p);assert d['coverage']['expanded']['new_vs_baseline']==['start']
    row=next(x for x in d['od'] if x['origin_id']=='start' and x['facility_id']=='at_start');assert row['cost']==0 and row['edge_keys']==[]


def test_grass_d8_trace():
    a=np.array([[8,8,6],[2,0,6],[2,4,-6]])
    path,status=H.trace_d8(a,(0,0));assert path==[(0,0),(0,1),(0,2),(1,2),(2,2)] and status=='region_exit'
    assert H.trace_d8(a,(1,1))[1]=='sink'
    a[0,1]=4;assert H.trace_d8(a,(0,0))[1]=='cycle'
    a[0,0]=9
    with pytest.raises(ValueError):H.trace_d8(a,(0,0))
