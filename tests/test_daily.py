"""Hand-calculated regressions and three rerunnable CLI/file-chain acceptance cases."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, LineString, Polygon, MultiLineString, box

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import _daily as d
from _safe_io import write_vector_atomic


def frame(geoms, **attrs):
    return gpd.GeoDataFrame(attrs or {'id':[str(i) for i in range(len(geoms))]},geometry=geoms,crs=26918)


def cli(script,*args,success=True):
    result=subprocess.run([sys.executable,str(SCRIPTS/script),*map(str,args)],capture_output=True,encoding='utf-8', errors='replace',env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
    assert (result.returncode==0)==success,result.stdout+result.stderr
    return result


def run(root,name,operation,source,params,right=None,success=True):
    spec=root/(name+'.json');spec.write_text(json.dumps(params), encoding='utf-8')
    output=root/name
    args=[operation,source,'--params',spec,'--output',output]
    if right is not None: args+=['--right',right]
    cli('daily.py',*args,success=success)
    if not success:
        assert not output.exists();return
    record=json.loads((output/'record.json').read_text(encoding='utf-8'))
    artifact=output/record['artifact']['name']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==record['artifact']['sha256']
    return artifact,record


def write(root,name,gdf):
    path=root/(name+'.gpkg');write_vector_atomic(gdf,path);return path


@pytest.mark.parametrize('text,value',[('12',12),('12°30\'00"N',12.5),('12:30:0W',-12.5),('-12°30\'0"',-12.5)])
def test_dms(text,value): assert d.coordinate(text)==pytest.approx(value)


@pytest.mark.parametrize('text',['12°60\'0"','12°0\'60"','nan','inf','-12°0\'0"N'])
def test_invalid_dms(text):
    with pytest.raises(ValueError): d.coordinate(text)


def test_normalization_failures():
    data=pd.DataFrame({'id':['001','002','003','004','005','005'],'x':['1','bad','2','3','4','5'],'y':['2']*6,
                       'value':['4','4','1.1','4','4','4'],'when':['2024-02-29']*6,'empty':['']*6})
    result,report=d.normalize(data,{'id':'id','x':'x','y':'y','crs':4326,'fields':{'n':{'source':'value','type':'int'},'date':{'source':'when','type':'date','format':'%Y-%m-%d'},'nil':{'source':'empty'}}})
    assert list(result.id)==['001','004'];assert result['nil'].isna().all()
    assert report['rejected_rows']==4
    with pytest.raises(ValueError,match='collision'): d.normalize(data,{'fields':{'id':{'source':'id'}}})


def test_codes_dates_and_xy_diagnosis():
    data=pd.DataFrame({'id':['a','b','c'],'x':['200','1','2'],'y':['20','20','20'],'code':['x','bad','x'],'date':['2024-01-01','2024-01-01','2023-02-29']})
    result,report=d.normalize(data,{'id':'id','x':'x','y':'y','crs':4326,'fields':{'mapped':{'source':'code','codes':{'x':'X'}},'parsed':{'source':'date','type':'date','format':'%Y-%m-%d'}}})
    assert len(result)==0;assert report['rejected_rows']==3


def test_update():
    a=frame([Point(0,0),Point(1,0)],id=['01','02'],v=[1,2])
    b=frame([Point(2,0),Point(3,0)],id=['01','03'],v=[3,4])
    out,report=d.update(a,b,{'id':'id','fields':['v'],'append':True})
    assert list(out.id)==['01','02','03'];assert out.geometry.iloc[0].equals(a.geometry.iloc[0])
    assert out.v.iloc[0]==3;assert report['unmatched_left']==['02']
    with pytest.raises(ValueError,match='Duplicate'): d.update(a,pd.concat([b,b]),{'id':'id'})


@pytest.mark.parametrize('method',['intersection','difference','union','identity','symmetric_difference'])
def test_overlay(method):
    a=frame([box(0,0,2,2)],id=['a']);b=frame([box(1,0,3,2)],id=['b'])
    out,report=d.overlay(a,b,{'id':'id','method':method})
    assert out.area_m2.sum()==pytest.approx({'intersection':2,'difference':2,'union':6,'identity':4,'symmetric_difference':4}[method])
    assert 'left_id' in out;assert report['semantics'].startswith('global')


def test_overlay_invalid_and_dimension():
    a=frame([box(0,0,1,1)]);b=frame([box(1,0,2,1)])
    out,_=d.overlay(a,b,{'id':'id'})
    assert out.geom_type.iloc[0]=='LineString'
    a.geometry=[Polygon([(0,0),(2,2),(2,0),(0,2),(0,0)])]
    with pytest.raises(ValueError,match='invalid'): d.overlay(a,b,{'id':'id'})


def test_allocation_balance_overlap_and_rates():
    a=frame([box(0,0,10,10)],id=['a'],pop=[100.])
    b=frame([box(0,0,4,10),box(4,0,8,10)],id=['b','c'])
    params={'id':'id','value':'pop','quantity_type':'total','assumption':'uniform'}
    out,report=d.allocate(a,b,params)
    assert out.allocated.sum()==80; assert report['balances'][0]['unallocated']==20
    assert report['balances'][0]['maximum_overlap_id']=='b'
    with pytest.raises(ValueError,match='requires'):d.allocate(a,b,{**params,'quantity_type':'rate'})
    b.geometry=[box(0,0,5,10),box(4,0,8,10)]
    with pytest.raises(ValueError,match='coverage conflict'):d.allocate(a,b,params)


def test_nearest_tie_radius_self_and_order():
    a=frame([Point(0,0),Point(20,0)],id=['a','missing'])
    b=frame([Point(1,0),Point(-1,0),Point(0,0)],id=['z','b','a'])
    params={'id':'id','k':2,'radius_m':1,'exclude_self':True}
    out,report=d.relations(a,b,params)
    assert list(out.target_id)==['b','z'];assert list(out.distance_m)==[1,1];assert report['unmatched']==['missing']
    reverse,_=d.relations(a,b.iloc[::-1],params)
    pd.testing.assert_frame_equal(out,reverse)
    with pytest.raises(ValueError,match='max_pairs'): d.relations(a,b,{**params,'max_pairs':1})


def test_adjacency_and_bands():
    a=frame([box(0,0,1,1)],id=['a'])
    b=frame([box(1,0,2,1),box(1,1,2,2),box(.5,0,1.5,1)],id=['edge','point','overlap'])
    out,_=d.relations(a,b,{'id':'id','method':'adjacency'})
    assert set(out.contact)=={'edge','point','overlap'}
    assert out.set_index('target_id').loc['edge','shared_m']==1
    a=frame([Point(0,0)]);b=frame([Point(1,0),Point(2,0)])
    out,_=d.relations(a,b,{'id':'id','method':'bands','bands_m':[1,2]})
    assert list(out.band_max_m)==[1,2]
    out,_=d.relations(a,b,{'id':'id','method':'bands','bands_m':[1,2],'cumulative':True})
    assert len(out)==3


@pytest.mark.parametrize('method',['segment','sample','locate','cross','offset','explode'])
def test_along_line(method):
    source=frame([LineString([(0,0),(10,0)])],id=['a'])
    out,report=d.geometry(source,None,{'id':'id','method':method,'distance_m':3,'measures_m':[0,5,10],'width_m':4})
    assert set(out.source_id)=={'a'}
    if method=='segment': assert out.length.sum()==pytest.approx(10)
    if method=='sample':assert list(out.measure_m)==[0,3,6,9,10]
    if method=='locate':assert list(out.geometry.x)==[0,5,10]
    if method=='cross':assert np.allclose(out.length,4)
    if method=='offset':assert out.geometry.iloc[0].bounds==(0,3,10,3)


def test_polygon_geometry_and_split():
    poly=Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(4,2),(4,4),(2,4)]])
    source=frame([poly],id=['a'])
    for method in ('rings','boundary','representative','label'):
        out,_=d.geometry(source,None,{'id':'id','method':method})
        if method in ('representative','label'):assert poly.contains(out.geometry.iloc[0])
        if method=='rings':assert list(out.ring)==['outer','hole']
    out,_=d.geometry(source,frame([LineString([(5,-1),(5,11)])]),{'id':'id','method':'split'})
    assert out.area.sum()==96;assert out.geometry.union_all().equals(poly)
    line=frame([LineString([(0,0),(10,0)])])
    out,_=d.geometry(line,frame([Point(5,0)]),{'id':'id','method':'split'})
    assert len(out)==2;assert out.length.sum()==10
    with pytest.raises(ValueError): d.geometry(line,line,{'id':'id','method':'split'})


def test_multipart_connect_and_z():
    source=frame([MultiLineString([[(0,0),(3,0)],[(4,0),(6,0)]])])
    out,_=d.geometry(source,None,{'id':'id','method':'sample','distance_m':2})
    assert list(out.measure_m)==[0,2,3,0,2]
    points=frame([Point(2,0),Point(0,0),Point(1,0)],id=['b','a','c'],group=['g']*3,order=[2,0,1])
    out,_=d.geometry(points,None,{'id':'id','method':'connect','group':'group','order':'order'})
    assert list(out.geometry.iloc[0].coords)==[(0,0),(1,0),(2,0)]
    with pytest.raises(ValueError,match='Z/M'):d.geometry(frame([Point(0,0,1)]),None,{'id':'id','method':'explode'})


@pytest.mark.parametrize('method',['square','hex','stratified','spaced'])
def test_grids(method):
    source=frame([box(0,0,10,10)])
    params={'method':method,'size_m':2,'origin':[0,0],'seed':8,'minimum_m':2}
    out,_=d.grid(source,params); repeat,_=d.grid(source,params)
    assert list(out.geometry.to_wkb())==list(repeat.geometry.to_wkb())
    if method in ('square','hex'): assert out.area.sum()==pytest.approx(100)
    else:
        assert out.geometry.within(source.geometry.iloc[0]).all()
        if method=='spaced':
            assert all(a.distance(b)>=2 for i,a in enumerate(out.geometry) for b in out.geometry.iloc[i+1:])


def test_voronoi():
    sites=frame([Point(2,5),Point(8,5)],id=['west','east'])
    out,_=d.grid(sites,{'id':'id','method':'voronoi','size_m':1,'origin':[0,0],'bounds':[0,0,10,10]})
    assert sorted(out.area)==[50,50];assert set(out.cell_id)=={'west','east'}


def test_rules():
    source=frame([Point(0,0),Point(10,10)],id=['a','a'],v=[None,2],code=['X','bad'])
    other=frame([box(-1,-1,1,1)],code=['X'])
    specs=[{'id':'u','kind':'unique','field':'id'},{'id':'r','kind':'required','field':'v'},
           {'id':'e','kind':'enum','field':'code','values':['X']},{'id':'c','kind':'compare','field':'v','op':'lt','value':1},
           {'id':'f','kind':'foreign_key','field':'code','right_field':'code'},{'id':'s','kind':'within'}]
    out,report=d.rules(source,other,{'id':'id','rules':specs})
    assert set(out.rule_id)=={'u','r','e','c','f','s'};assert not report['passed'];assert out.location_wkt.notna().all()


def test_field_cli_units_and_failure_protection(tmp_path):
    source=write(tmp_path,'feet',frame([box(1000000,200000,1000010,200010)]).set_crs(2263,allow_override=True))
    before=source.read_bytes()
    for command,field,expected in [('add-area','area_m2',9.2903411613),('add-length','length_m',12.192024384)]:
        out=tmp_path/(command+'.gpkg');cli('field.py',command,source,'--output',out)
        assert gpd.read_file(out)[field].iloc[0]==pytest.approx(expected)
        saved=out.read_bytes();cli('field.py',command,source,'--output',out,success=False);assert out.read_bytes()==saved
    cli('field.py','add-area',source,'--inplace',success=False);assert source.read_bytes()==before
    cli('convert.py',source,'--output',source,success=False);assert source.read_bytes()==before
    cli('fix.py',source,'--output',source,'--overwrite',success=False);assert source.read_bytes()==before


def test_multilayer_and_failed_write(tmp_path,monkeypatch):
    source=frame([Point(0,0)]);path=tmp_path/'many.gpkg'
    source.to_file(path,layer='a');source.to_file(path,layer='b')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='layers'):write_vector_atomic(source,path,overwrite=True)
    assert path.read_bytes()==before
    import _safe_io
    monkeypatch.setattr(_safe_io.pyogrio,'write_dataframe',lambda *a,**kw:(_ for _ in ()).throw(OSError('injected disk failure')))
    with pytest.raises(OSError):write_vector_atomic(source,tmp_path/'broken.gpkg')
    assert not (tmp_path/'broken.gpkg').exists();assert not list(tmp_path.glob('.*.tmp.gpkg'))


def test_three_end_to_end_cases(tmp_path):
    # Run the full three cases twice, checking semantic identity, not GPKG byte identity.
    source=tmp_path/'parcels.csv'
    pd.DataFrame({'code':['001','002'],'shape':['POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))','POLYGON ((10 0, 20 0, 20 10, 10 10, 10 0))'],'population':['100','200']}).to_csv(source,index=False)
    excel=tmp_path/'updates.xlsx'
    pd.DataFrame({'code':['001','003'],'shape':['POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))','POLYGON ((20 0, 30 0, 30 10, 20 10, 20 0))'],'population':['120','300']}).to_excel(excel,index=False)
    restrictions=write(tmp_path,'restriction',frame([box(0,0,2,10)],id=['r']))
    zones=write(tmp_path,'zones',frame([box(0,0,15,10),box(15,0,30,10)],id=['west','east']))
    points=tmp_path/'points.csv';points.write_text('code,x,y\n001,0,0\n002,10,0\n', encoding='utf-8')
    facilities=write(tmp_path,'facilities',frame([Point(3,0),Point(7,0)],id=['f1','f2']))
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,excel,restrictions,zones,points,facilities)}
    results=[]
    for iteration in range(2):
        root=tmp_path/f'run{iteration}';root.mkdir()
        spec={'id':'code','wkt':'shape','crs':26918,'fields':{'total':{'source':'population','type':'float'}}}
        a,_=run(root,'standard','normalize',source,spec)
        b,_=run(root,'extra','normalize',excel,spec)
        merged,change=run(root,'multi_source','update',a,{'id':'code','fields':['total'],'append':True},b)
        assert len(gpd.read_file(merged))==3;assert change['details']['matched']==1
        clipped,_=run(root,'subtract','overlay',merged,{'id':'code','right_id':'id','method':'difference'},restrictions)
        # Allocate from original sources to remaining candidates: quantities retain original density.
        candidates,exclusions=run(root,'candidates','select',merged,{'id':'code','right_id':'id','min_area_m2':1},restrictions)
        allocated,balances=run(root,'allocation','allocate',merged,{'id':'code','right_id':'candidate_id','value':'total','quantity_type':'total','assumption':'uniform'},candidates)
        assert sum(v['unallocated'] for v in balances['details']['balances'])==pytest.approx(24)
        # Distribute candidate totals to zones after explicit update from the allocation table.
        enriched,_=run(root,'enriched','attribute_join',candidates,{'id':'candidate_id','right_key':'target_id','fields':['allocated']},allocated)
        zones_table,_=run(root,'zones_allocation','allocate',enriched,{'id':'candidate_id','right_id':'id','value':'joined_allocated','quantity_type':'total','assumption':'uniform'},zones)
        summary,_=run(root,'summary','summarize',zones_table,{'group':'target_id','value':'allocated','quantity_type':'total'})
        assert pd.read_csv(summary)['sum'].sum()==pytest.approx(596)
        checked,record=run(root,'rules','rules',enriched,{'id':'candidate_id','rules':[{'id':'unique','kind':'unique','field':'candidate_id'},{'id':'area','kind':'compare','field':'area_m2','op':'gt','value':0},{'id':'zone','kind':'within'}]},zones)
        assert record['details']['passed'];assert len(pd.read_csv(checked))==0
        imported,_=run(root,'points','normalize',points,{'id':'code','x':'x','y':'y','crs':26918})
        near,record=run(root,'neighbors','relations',imported,{'id':'code','right_id':'id','k':2,'radius_m':8},facilities)
        assert list(pd.read_csv(near).distance_m)==[3,7,3,7]
        result=gpd.read_file(candidates);results.append((result.geometry.to_wkb().tolist(),pd.read_csv(summary).to_dict(),pd.read_csv(near).to_dict()))
        assert gpd.read_file(clipped).area.sum()==pytest.approx(result.area.sum())
    assert results[0]==results[1]
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items())
    run(tmp_path,'bad','relations',facilities,{'id':'id','k':0},facilities,success=False)


def test_attribute_join_cardinality_and_collisions():
    left=frame([Point(0,0),Point(1,0)],id=['001','002'])
    right=pd.DataFrame({'code':['001','001','003'],'v':['a','b','c']})
    params={'id':'id','right_key':'code','fields':['v']}
    with pytest.raises(ValueError,match='cardinality'):d.attribute_join(left,right,params)
    out,report=d.attribute_join(left,right,{**params,'cardinality':'one_to_many'})
    assert len(out)==3;assert report['unmatched_left']==['002'];assert report['duplicate_right_keys']==['001','001']
    left['joined_v']='old'
    with pytest.raises(ValueError,match='collision'):d.attribute_join(left,right,{**params,'cardinality':'one_to_many'})


def test_empty_normalization_bundle_and_failed_publication(tmp_path):
    source=tmp_path/'bad.csv';source.write_text('id,x,y\na,bad,0\n', encoding='utf-8')
    artifact,record=run(tmp_path,'empty','normalize',source,{'id':'id','x':'x','y':'y','crs':4326})
    assert len(gpd.read_file(artifact))==0;assert record['details']['rejected_rows']==1
    spec=tmp_path/'empty.json'
    before=artifact.read_bytes()
    cli('daily.py','normalize',source,'--params',spec,'--output',artifact.parent,success=False)
    assert artifact.read_bytes()==before


def test_approval_invalidation():
    source=frame([Point(0,0)],id=['a'],v=[0])
    p={'id':'id','rules':[{'id':'positive','kind':'compare','field':'v','op':'gt','value':0}]}
    _,report=d.rules(source,None,p,['hash1'])
    approval={'rule_id':'positive','source_id':'a','row':0,'approved_by':'reviewer','rules_sha256':report['rules_sha256'],'input_sha256':'hash1'}
    errors,report=d.rules(source,None,{**p,'approvals':[approval]},['hash1'])
    assert report['passed'];assert errors.review.iloc[0]=='approved_exception'
    _,report=d.rules(source,None,{**p,'approvals':[approval]},['changed'])
    assert not report['passed'];assert len(report['stale_approvals'])==1


@pytest.mark.parametrize('operation,params',[
    ('geometry',{'method':'representative'}),('geometry',{'method':'rings'}),
    ('grid',{'method':'hex','origin':[0,0],'size_m':2}),
    ('grid',{'method':'spaced','origin':[0,0],'size_m':2,'seed':5}),
])
def test_geometry_grid_cli(tmp_path,operation,params):
    source=write(tmp_path,'source',frame([box(0,0,10,10)],id=['001']))
    artifact,record=run(tmp_path,'out',operation,source,{'id':'id',**params})
    out=gpd.read_file(artifact)
    assert len(out)>0;assert out.crs.to_epsg()==26918;assert out.is_valid.all()


def test_legacy_success_and_crs_failures(tmp_path):
    source=write(tmp_path,'source',frame([Point(1,2)],id=['001'],_wkt=['original']))
    for script,args in [('convert.py',[]),('fix.py',['--remove-duplicates'])]:
        output=tmp_path/(script+'.gpkg');cli(script,source,'--output',output,*args)
        assert gpd.read_file(output)._wkt.iloc[0]=='original'
    geographic=write(tmp_path,'broad',frame([box(-73,39,-70,40)]).set_crs(4326,allow_override=True))
    cli('field.py','add-area',geographic,'--output',tmp_path/'bad.gpkg',success=False)
    assert not (tmp_path/'bad.gpkg').exists()
    missing=tmp_path/'unknown.gpkg';frame([Point(1,2)]).set_crs(None,allow_override=True).to_file(missing)
    cli('field.py','add-length',missing,'--output',tmp_path/'bad.gpkg',success=False)


def test_table_export_and_encoding(tmp_path):
    source=write(tmp_path,'source',frame([Point(1,2)],id=['001']))
    dest=tmp_path/'export.csv'
    cli('export-table.py',source,'--include-coords','--no-index','--output',dest)
    out=pd.read_csv(dest,dtype={'id':str});assert out.id.iloc[0]=='001';assert out.X.iloc[0]==1
    before=dest.read_bytes();cli('export-table.py',source,'--output',dest,success=False);assert dest.read_bytes()==before
    raw=tmp_path/'gbk.csv';raw.write_bytes('id,x,y,label\n001,1,2,地块\n'.encode('gbk'))
    artifact,_=run(tmp_path,'gbk','normalize',raw,{'id':'id','x':'x','y':'y','crs':26918,'encoding':'gbk'})
    assert gpd.read_file(artifact).label.iloc[0]=='地块'


def test_bundle_publication_failure(tmp_path,monkeypatch):
    import daily
    from types import SimpleNamespace
    source=tmp_path/'source.csv';source.write_text('id,x,y\n001,1,2\n', encoding='utf-8')
    spec=tmp_path/'params.json';spec.write_text(json.dumps({'id':'id','x':'x','y':'y','crs':26918}), encoding='utf-8')
    before=source.read_bytes()
    def fail(*args,**kwargs): raise OSError('injected rename failure')
    monkeypatch.setattr(daily.os,'rename',fail)
    with pytest.raises(OSError,match='injected'):
        daily.execute(SimpleNamespace(input=str(source),right=None,params=str(spec),output=str(tmp_path/'out'),operation='normalize'))
    assert not (tmp_path/'out').exists();assert not list(tmp_path.glob('.daily-*'));assert not list(tmp_path.glob('*.publish-lock'))
    assert source.read_bytes()==before
