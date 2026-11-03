"""Hand-calculated network-access regressions; no network/download dependency."""
import importlib.util
import json
from pathlib import Path
import sys

import geopandas as gpd
import pytest
from shapely.geometry import Point, LineString, box

S = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(S))
from _network_access import analyze
spec = importlib.util.spec_from_file_location('network', S/'network.py')
N = importlib.util.module_from_spec(spec); spec.loader.exec_module(N)


def fixture():
    edges = gpd.GeoDataFrame(dict(id=['road'], nodes=['["a","b"]'],
        direction=['both'], access=['allowed'], level=['0'], speed=[3.6]),
        geometry=[LineString([(0, 0), (100, 0)])], crs=32630)
    origins = gpd.GeoDataFrame(dict(id=['d1','d2'], level=['0','0'], weight=[10.,20.]),
        geometry=[Point(20, 3), Point(80, 0)], crs=edges.crs)
    facilities = gpd.GeoDataFrame(dict(id=['f1','f2'], level=['0','0']),
        geometry=[Point(60, 4), Point(100, 0)], crs=edges.crs)
    areas = gpd.GeoDataFrame(dict(level=['0'], access=['allowed']), geometry=[box(-1,-10,101,10)], crs=edges.crs)
    barriers = gpd.GeoDataFrame(dict(level=[], access=[]), geometry=[], crs=edges.crs)
    p = dict(topology='source_vertices', edge_id='id', vertex_nodes='nodes', direction='direction',
        access='access', level='level', point_level='level', speed_kmh='speed', travel_mode='walk',
        mode='shortest', analysis_crs=32630, turn_restrictions='not_modelled',
        cost_assumptions='hand calculation', access_assumptions='synthetic reviewed envelope',
        origin_id='id', facility_id='id', demand_weight='weight', connector_speed_kmh=3.6,
        snap_m=10, budget=47, facility_sets={'all':['f1','f2'], 'one':['f2']})
    return edges, origins, facilities, p, areas, barriers


def run(parts):
    return analyze(*parts)


def test_midsegment_cost_coverage_and_export(tmp_path):
    e,o,f,p,a,b = fixture()
    routes,service,d,extra = run((e,o,f,p,a,b))
    first = d['od'][0]
    assert first['cost'] == pytest.approx(47)
    assert first['network_cost'] == pytest.approx(40)
    assert first['connector_cost'] == 7
    assert first['status'] == 'covered'
    assert d['coverage']['all']['weights'] == dict(covered=30, uncovered=0, unknown=0)
    assert d['coverage']['one']['weights'] == dict(covered=20, uncovered=10, unknown=0)
    assert len(extra['graph']) == 8
    # Recompute every route from exported directed arc keys + access costs.
    costs = extra['graph'].set_index('id').cost.to_dict()
    for row in d['od']:
        assert sum(costs[k] for k in row['edge_keys']) + row['connector_cost'] == pytest.approx(row['cost'])
    for name, data in [('edges',e),('origins',o),('facilities',f),('areas',a),('barriers',b)]:
        data.to_file(tmp_path/f'{name}.gpkg',driver='GPKG')
    N.execute(tmp_path/'edges.gpkg',p,tmp_path/'origins.gpkg',tmp_path/'facilities.gpkg',tmp_path/'out',tmp_path/'areas.gpkg',tmp_path/'barriers.gpkg')
    record = json.loads((tmp_path/'out/record.json').read_text(encoding='utf-8'))
    assert record['details'] == d
    assert len(gpd.read_file(tmp_path/'out/graph.gpkg')) == 8


def test_direction_and_threshold():
    e,o,f,p,a,b = fixture(); e.direction='forward';p['budget']=46.9
    _,_,d,_ = run((e,o,f,p,a,b))
    assert [r['status'] for r in d['od']] == ['uncovered','uncovered','uncovered','covered']
    e.direction='reverse'
    _,_,d,_ = run((e,o,f,p,a,b))
    assert d['od'][2]['cost'] == pytest.approx(24)


def test_fastest_includes_connector_speed():
    e,o,f,p,a,b = fixture();p['mode']='fastest';e.speed=7.2;p['connector_speed_kmh']=1.8
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['od'][0]['cost']==pytest.approx(34) # 40/2 + 7/.5


@pytest.mark.parametrize('unknown_where',['road','area','barrier'])
def test_unknown_never_becomes_uncovered(unknown_where):
    e,o,f,p,a,b=fixture()
    if unknown_where=='road':e.access='unknown'
    elif unknown_where=='area':a.access='unknown'
    else:b=gpd.GeoDataFrame(dict(level=['0'],access=['unknown']),geometry=[Point(50,0)],crs=e.crs)
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['coverage']['all']['weights']==dict(covered=0,uncovered=0,unknown=30)


def test_failed_access_retains_denominator():
    e,o,f,p,a,b=fixture();o.loc[0,'geometry']=Point(20,100)
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['coverage']['all']['weights']==dict(covered=20,uncovered=0,unknown=10)
    assert d['coverage']['all']['coverage_rate']==pytest.approx(2/3)


def test_wall_rejects_connector():
    e,o,f,p,a,b=fixture()
    b=gpd.GeoDataFrame(dict(level=['0'],access=['blocked']),geometry=[LineString([(10,1),(30,1)])],crs=e.crs)
    _,_,d,extra=run((e,o,f,p,a,b))
    assert d['origin_snaps'][0]['reason']=='connectors_blocked'
    assert d['coverage']['all']['weights']['unknown']==10
    assert not any(g.intersects(b.geometry.iloc[0]) for g in extra['connectors'].geometry)


def test_water_excluded_from_envelope():
    e,o,f,p,a,b=fixture();a.geometry=[box(-1,-10,101,10).difference(box(10,1,30,2))]
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['od'][0]['status']=='unknown'


def test_bridge_tunnel_and_disconnected_ids():
    e,o,f,p,a,b=fixture()
    e.loc[1]=['bridge','["x","y"]','both','allowed','1',3.6,LineString([(50,-10),(50,10)])]
    o=o.iloc[:1].copy();o.geometry=[Point(20,0)]
    f=f.iloc[:1].copy();f.geometry=[Point(50,5)];f.level='1';p['facility_sets']={'all':['f1']}
    a.loc[1]=['1','allowed',box(-1,-11,101,11)]
    e.set_crs(32630,inplace=True);a.set_crs(32630,inplace=True)
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['od'][0]['status']=='uncovered'
    e.loc[1,'level']='-1';f.level='-1';a.loc[1,'level']='-1'
    assert run((e,o,f,p,a,b))[2]['od'][0]['status']=='uncovered'


def test_shared_vertex_connects_without_planar_noding():
    e,o,f,p,a,b=fixture();e.loc[0,'nodes']='["a","junction","b"]';e.loc[0,'geometry']=LineString([(0,0),(50,0),(100,0)])
    e.loc[1]=['side','["junction","c"]','both','allowed','0',3.6,LineString([(50,0),(50,10)])]
    e.set_crs(32630,inplace=True)
    o=o.iloc[:1].copy();o.geometry=[Point(20,0)]
    f=f.iloc[:1].copy();f.geometry=[Point(50,10)];p['facility_sets']={'all':['f1']}
    assert run((e,o,f,p,a,b))[2]['od'][0]['cost']==pytest.approx(40)


def test_reorder_stable():
    e,o,f,p,a,b=fixture()
    first=run((e,o,f,p,a,b));second=run((e.iloc[::-1],o.iloc[::-1],f.iloc[::-1],p,a,b))
    assert first[2]==second[2]
    assert first[3]['graph'].equals(second[3]['graph'])


def test_no_demand_and_zero_weights():
    parts=list(fixture());parts[3].pop('demand_weight')
    d=run(parts)[2];assert d['demand_status']=='not_provided'
    assert 'coverage_rate' not in d['coverage']['all']
    parts=list(fixture());parts[1].weight=0
    assert run(parts)[2]['coverage']['all']['coverage_rate'] is None


@pytest.mark.parametrize('key,value',[('budget',-1),('snap_m',float('nan')),('connector_speed_kmh',0),('mode','bad'),('max_pairs',1),('max_comparisons',1),('typo',1)])
def test_invalid_parameters(key,value):
    parts=list(fixture());parts[3][key]=value
    with pytest.raises(ValueError):run(parts)


@pytest.mark.parametrize('error',['weight','nodes','direction','access','coordinates'])
def test_invalid_source(error):
    e,o,f,p,a,b=fixture()
    if error=='weight':o.weight=-1
    elif error=='nodes':e.nodes='["a"]'
    elif error=='direction':e.direction='yes'
    elif error=='access':e.access='private'
    else:e.nodes='["a","a"]'
    with pytest.raises(ValueError):run((e,o,f,p,a,b))


def test_projected_feet():
    e,o,f,p,a,b=fixture();p['analysis_crs']=2277
    for g in (e,o,f,a,b):g.set_crs(2277,allow_override=True,inplace=True)
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['od'][0]['cost']==pytest.approx(47*1200/3937)


def test_blocked_area_intersection_overrides_allowed():
    e,o,f,p,a,b=fixture()
    a.loc[1]=['0','blocked',box(15,1,25,2)];a.set_crs(e.crs,inplace=True)
    assert run((e,o,f,p,a,b))[2]['origin_snaps'][0]['reason']=='connectors_blocked'


def test_empty_facility_set_is_definitively_uncovered():
    parts=list(fixture());parts[3]['facility_sets']={'none':[]}
    assert run(parts)[2]['coverage']['none']['weights']==dict(covered=0,uncovered=30,unknown=0)


def test_gate_blocks_road_and_retains_graph_evidence():
    e,o,f,p,a,b=fixture()
    e.loc[0,'nodes']='["a","gate","b"]';e.loc[0,'geometry']=LineString([(0,0),(50,0),(100,0)])
    b=gpd.GeoDataFrame(dict(level=['0'],access=['blocked']),geometry=[Point(50,0)],crs=e.crs)
    _,_,d,extra=run((e,o,f,p,a,b))
    assert extra['graph'].access.eq('blocked').all()
    assert d['coverage']['all']['weights']['unknown']==30


def test_known_detour_unknown_shortcut():
    e,o,f,p,a,b=fixture();e.access='unknown'
    e.loc[1]=['detour','["a","z","b"]','both','allowed','0',3.6,LineString([(0,0),(50,40),(100,0)])]
    e.set_crs(32630,inplace=True)
    o=o.iloc[:1].copy();o.geometry=[Point(0,0)]
    f=f.iloc[:1].copy();f.geometry=[Point(100,0)]
    p['facility_sets']={'all':['f1']};p['budget']=110
    a.geometry=[box(-1,-1,101,41)]
    _,_,d,_=run((e,o,f,p,a,b))
    assert d['od'][0]['status']=='unknown'
    assert d['od'][0]['cost']>110 and d['od'][0]['possible_cost']==100
    p['budget']=130
    assert run((e,o,f,p,a,b))[2]['od'][0]['status']=='covered'
