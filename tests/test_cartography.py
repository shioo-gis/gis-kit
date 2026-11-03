"""Stage G hand-calculated, failure, source-preservation and rendered-layout regressions."""
import importlib.util
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import Point,LineString,Polygon,MultiPoint,box
from PIL import Image
from test_daily import frame,run,cli,SCRIPTS
import _cartography as c
import _conflation as m


def module(name):
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/(name+'.py'));obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj


def params(**kw):return {'id':'id','radius_m':2,'max_angle_deg':15,'min_overlap_m':0,**kw}


@pytest.mark.parametrize('method',['metrics','convex_hull','concave_hull','oriented_envelope'])
def test_morphology(method):
    f=frame([box(0,0,4,2),box(10,0,12,2),Point(20,0)])
    out,d=c.morphology(f,{'id':'id','method':method})
    assert out.long_m.iloc[0]==4 and out.short_m.iloc[0]==2 and out.aspect.iloc[0]==2
    assert out.direction_deg.iloc[0]==0 and pd.isna(out.direction_deg.iloc[1])
    assert out.compactness.iloc[0]==pytest.approx(2*np.pi/9)
    assert f.geometry.iloc[0].equals(box(0,0,4,2))


def test_feet_and_degenerate():
    f=frame([LineString([(0,0),(10,0)])]).set_crs(2263,allow_override=True)
    out,_=c.morphology(f,{'id':'id'});assert out.long_m.iloc[0]==pytest.approx(3.048006096)
    assert pd.isna(out.aspect.iloc[0])


def test_weighted_summary():
    f=pd.DataFrame({'g':['a','a','b'],'v':[.2,.8,.5],'w':[1,3,0]})
    out,_=c.weighted_summary(f,{'group':'g','value':'v','weight':'w','quantity_type':'ratio','weight_meaning':'denominator'})
    assert out.weighted_ratio.iloc[0]==pytest.approx(.65);assert pd.isna(out.weighted_ratio.iloc[1])
    f.v=['red','blue','red'];out,_=c.weighted_summary(f,{'group':'g','value':'v','weight':'w','quantity_type':'category','weight_meaning':'area'})
    assert out[out.category=='blue'].fraction.iloc[0]==.75


@pytest.mark.parametrize('bad',[-1,float('nan'),float('inf')])
def test_invalid_weights(bad):
    with pytest.raises(ValueError):c.weighted_summary(pd.DataFrame({'g':['a'],'v':[1],'w':[bad]}),{'group':'g','value':'v','weight':'w','quantity_type':'ratio','weight_meaning':'x'})


def test_ambiguous_points_and_unmatched():
    a=frame([Point(0,0),Point(10,0)],id=['a','b']);b=frame([Point(1,0),Point(-1,0)],id=['x','y'])
    out,d=m.match(a,b,params());assert len(out)==2 and out.ambiguous.all();assert d['unmatched']==['b']
    out2,_=m.match(a.iloc[::-1],b.iloc[::-1],params());pd.testing.assert_frame_equal(out,out2)


def test_segment_correspondence_and_reconstruction():
    a=frame([LineString([(0,0),(10,0)])],id=['a']);b=frame([LineString([(4,1),(0,1)]),LineString([(6,1),(9,1)]),LineString([(2,-1),(3,-1)])],id=['x','y','z'])
    out,d=m.match_split(a,b,params());assert out.length.sum()==10
    assert 'unmatched' in set(out.match_status) and 'ambiguous' in set(out.match_status)
    assert out.geometry.union_all().equals(a.geometry.iloc[0])
    table,_=m.match(a,b,params());assert table[table.target_id=='x'].source_end_m.iloc[0]==4


def test_angle_and_attribute_evidence():
    a=frame([LineString([(0,0),(10,0)])],id=['0'],name=['A']);b=frame([LineString([(0,1),(10,1)])],id=['0'],name=['B'])
    out,_=m.match(a,b,params(attributes={'name':'name'}));assert json.loads(out.attributes.iloc[0])=={'name':False}
    out,_=m.match(a,b,params(attributes={'name':'name'},require_attributes=True));assert out.empty
    with pytest.raises(ValueError):m.match(a,b,params(max_pairs=0))


def test_partial_transfer_and_stale_approval():
    a=frame([LineString([(0,0),(10,0)])]);b=frame([LineString([(0,1),(5,1)])],id=['0'],name=['X'])
    p=params(approved_by='fixture reviewer',source_sha256='a',reference_sha256='b',selections={'0':'0'},transfer_fields={'new':'name'})
    with pytest.raises(ValueError,match='Partial'):m.match_transfer(a,b,p,['a','b'])
    with pytest.raises(ValueError,match='stale'):m.match_transfer(a,b,p,['x','b'])


def test_attribute_transfer_preserves_geometry_and_unselected():
    a=frame([Point(0,0),Point(10,0)],id=['a','b']);b=frame([Point(1,0)],id=['x'],name=['Y'])
    p=params(approved_by='fixture reviewer',source_sha256='a',reference_sha256='b',selections={'a':'x'},transfer_fields={'new':'name'})
    out,d=m.match_transfer(a,b,p,['a','b']);assert out.new.iloc[0]=='Y' and pd.isna(out.new.iloc[1]);assert out.geometry.equals(a.geometry)
    with pytest.raises(ValueError):m.match_transfer(a,b,{**p,'transfer_fields':{'id':'name'}},['a','b'])


def test_edge_match_shared_junction():
    a=frame([LineString([(0,0),(5,0)]),LineString([(5,0),(10,0)])]);b=frame([LineString([(5,1),(5,5)])])
    p={'id':'id','max_displacement_m':1,'endpoint_pairs':[{'source_id':'0','target_id':'0','source_end':-1,'target_end':0}]}
    out,d=m.edge_match(a,b,p);assert out.geometry.iloc[0].coords[-1]==out.geometry.iloc[1].coords[0]==(5,1)
    assert a.geometry.iloc[0].coords[-1]==(5,0)
    with pytest.raises(ValueError):m.edge_match(a,b,{**p,'max_displacement_m':.9})


def test_edge_conflicting_targets():
    a=frame([LineString([(0,0),(5,0)]),LineString([(5,0),(10,0)])]);b=frame([LineString([(5,1),(5,2)])])
    with pytest.raises(ValueError):m.edge_match(a,b,{'id':'id','max_displacement_m':3,'endpoint_pairs':[{'source_id':'0','target_id':'0','source_end':-1,'target_end':0},{'source_id':'1','target_id':'0','source_end':0,'target_end':-1}]})


def test_point_aggregate_protection_order():
    a=frame([Point(0,0),Point(1,0),Point(2,0)],id=['a','b','c'])
    p={'id':'id','method':'aggregate_points','distance_m':1,'protected_ids':['c']}
    out,d=c.generalize(a,p);assert len(out)==2 and out.geometry.iloc[0].x==.5
    other,_=c.generalize(a.iloc[::-1],p);assert out.geometry.equals(other.geometry)


def test_polygon_aggregation_hole_and_gaps():
    hole=Polygon([(0,0),(4,0),(4,4),(0,4)],holes=[[(1,1),(1,2),(2,2),(2,1)]])
    a=frame([hole,box(5,0,6,1)])
    out,d=c.generalize(a,{'id':'id','method':'aggregate_polygons','distance_m':1})
    assert len(out)==1 and out.area.iloc[0]==16 and out.geometry.iloc[0].geom_type=='MultiPolygon'
    assert sum(len(g.interiors) for g in out.geometry.iloc[0].geoms)==1


def test_building_limits_and_hole():
    a=frame([Polygon([(0,0),(4,0),(4,2),(3.9,2.1),(0,2)]),Polygon([(10,0),(14,0),(14,4),(10,4)],holes=[[(11,1),(12,1),(12,2),(11,2)]])])
    out,d=c.generalize(a,{'id':'id','method':'regularize_buildings','max_displacement_m':.2,'max_area_change_m2':1})
    assert out.geometry.iloc[1].equals(a.geometry.iloc[1]);assert d['impacts'][0]['displacement_m']<=.2
    limited,_=c.generalize(a,{'id':'id','method':'regularize_buildings','max_displacement_m':0,'max_area_change_m2':0});assert limited.geometry.equals(a.geometry)


def test_network_thinning_priority_protection():
    a=frame([LineString([(0,0),(1,0)]),LineString([(1,0),(0,1)]),LineString([(0,1),(0,0)]),LineString([(0,0),(-1,0)])],id=['a','b','c','d'],u=['x','y','z','x'],v=['y','z','x','w'],priority=[3,2,1,0])
    p={'id':'id','method':'thin_network','from':'u','to':'v','priority':'priority','keep_priority_at_least':3}
    out,d=c.generalize(a,p);assert set(out.id)=={'a','b','d'};assert d['components_before']==d['components_after'];assert d['excluded'][0]['id']=='c'
    out,_=c.generalize(a,{**p,'protected_ids':['c']});assert len(out)==4
    a.loc[1,'u']='bad';out,_=c.generalize(a,p);assert len(out)>=3


def test_node_coordinate_mismatch():
    a=frame([LineString([(0,0),(1,0)]),LineString([(2,0),(3,0)])],id=['0','1'],u=['a','b'],v=['b','c'],priority=[1,2])
    with pytest.raises(ValueError,match='coordinate'):c.generalize(a,{'id':'id','method':'thin_network','from':'u','to':'v','priority':'priority','keep_priority_at_least':5})


def test_color_shared_and_point_contact():
    a=frame([box(0,0,1,1),box(1,0,2,1),box(1,1,2,2)])
    out,d=c.generalize(a,{'id':'id','method':'color'});assert len(set(out.color_index))==3
    out,d=c.generalize(a,{'id':'id','method':'color','point_touch':False});assert len(set(out.color_index))==2
    with pytest.raises(ValueError):c.generalize(frame([box(0,0,2,2),box(1,1,3,3)]),{'id':'id','method':'color'})


def test_real_cli_bundles_and_no_overwrite(tmp_path):
    a=tmp_path/'a.gpkg';b=tmp_path/'b.gpkg';frame([Point(0,0)]).to_file(a);frame([Point(1,0)]).to_file(b)
    run(tmp_path,'match','match',a,params(),b)
    rec=json.loads((tmp_path/'match/record.json').read_text(encoding='utf-8'));assert rec['status']=='hold'
    assert '_conflation.py' in rec['implementation_sha256']
    cli('daily.py','match',a,'--right',b,'--params',tmp_path/'match.json','--output',tmp_path/'match',success=False)
    assert (tmp_path/'match/record.json').exists()


def test_crop_mapping_pixels_and_bounds(tmp_path):
    src=tmp_path/'source.png';data=np.arange(12*12*3,dtype='uint8').reshape(12,12,3);Image.fromarray(data).save(src)
    spec=tmp_path/'p.json';spec.write_text(json.dumps({'center_xy':[6,6],'size_px':[4,4],'angle_deg':0}), encoding='utf-8')
    crop=module('oriented-crop');crop.execute(src,spec,tmp_path/'out')
    assert np.array_equal(np.asarray(Image.open(tmp_path/'out/crop.png')),data[4:8,4:8])
    rec=json.loads((tmp_path/'out/record.json').read_text(encoding='utf-8'));assert np.allclose(np.array(rec['output_to_source'])@rec['source_to_output'],np.eye(3))
    spec.write_text(json.dumps({'center_xy':[0,0],'size_px':[4,4],'angle_deg':45}), encoding='utf-8')
    with pytest.raises(ValueError):crop.execute(src,spec,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_render_scale_and_collision(tmp_path):
    src=tmp_path/'data.gpkg';frame([Point(0,0),Point(.01,0)],id=['LONG_LABEL_A','LONG_LABEL_B']).to_file(src)
    p=tmp_path/'p.json';p.write_text(json.dumps({'id':'id','scale':1000,'map_width_mm':100,'map_height_mm':100,'dpi':100}), encoding='utf-8')
    module('cartographic-map').execute(src,p,tmp_path/'map')
    rec=json.loads((tmp_path/'map/record.json').read_text(encoding='utf-8'));assert rec['scale_denominator']==pytest.approx(1000);assert rec['label_audit']['overlaps'] and rec['status']=='hold'


def test_cli_attribute_transfer_readback(tmp_path):
    from _delivery import digest
    a=tmp_path/'a.gpkg';b=tmp_path/'b.gpkg'
    frame([Point(0,0),Point(10,0)],id=['a','b']).to_file(a)
    frame([Point(1,0)],id=['x'],value=[12]).to_file(b)
    run(tmp_path,'transfer','match_transfer',a,params(approved_by='test fixture',source_sha256=digest(a),reference_sha256=digest(b),selections={'a':'x'},transfer_fields={'value':'value'}),b)
    import geopandas as gpd
    out=gpd.read_file(tmp_path/'transfer/result.gpkg');assert float(out.value.iloc[0])==12 and pd.isna(out.value.iloc[1])


def test_write_failure_does_not_publish(tmp_path,monkeypatch):
    import daily
    from types import SimpleNamespace
    a=tmp_path/'a.gpkg';frame([box(0,0,1,1)]).to_file(a)
    spec=tmp_path/'p.json';spec.write_text(json.dumps({'id':'id','method':'metrics'}), encoding='utf-8')
    real=daily.write_vector_atomic
    def fail(*args,**kwargs):real(*args,**kwargs);raise ValueError('injected after write')
    monkeypatch.setattr(daily,'write_vector_atomic',fail)
    with pytest.raises(ValueError,match='injected'):daily.execute(SimpleNamespace(operation='morphology',input=str(a),right=None,params=str(spec),output=str(tmp_path/'out')))
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.daily-*'))


def test_rotated_crop_exact(tmp_path):
    src=tmp_path/'image.png';a=np.arange(8*8*3,dtype='uint8').reshape(8,8,3);Image.fromarray(a).save(src)
    p=tmp_path/'p.json';p.write_text(json.dumps({'center_xy':[4,4],'size_px':[4,4],'angle_deg':90}), encoding='utf-8')
    module('oriented-crop').execute(src,p,tmp_path/'out')
    assert np.array_equal(np.asarray(Image.open(tmp_path/'out/crop.png')),np.rot90(a[2:6,2:6]))


def test_label_omissions_are_recorded(tmp_path):
    src=tmp_path/'data.gpkg';frame([Point(0,0),Point(.01,0)],id=['LONG_LABEL_A','LONG_LABEL_B']).to_file(src)
    p=tmp_path/'p.json';p.write_text(json.dumps({'id':'id','scale':1000,'map_width_mm':100,'map_height_mm':100,'dpi':100,'label_collision_policy':'omit'}), encoding='utf-8')
    module('cartographic-map').execute(src,p,tmp_path/'out');r=json.loads((tmp_path/'out/record.json').read_text(encoding='utf-8'))
    assert not r['label_audit']['overlaps'] and len(r['label_audit']['suppressed'])==1 and r['status']=='complete'
