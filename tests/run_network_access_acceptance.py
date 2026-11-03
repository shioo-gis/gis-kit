#!/usr/bin/env python3
"""Offline network-access real-source CLI/readback check. Supply a previously acquired OSM XML.

This is a bounded acceptance adapter, not a production OSM importer. It keeps raw
node identities/tags, records normalization assumptions and synthetic demand.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from shapely.geometry import Point, LineString
from shapely.ops import unary_union

S = Path(__file__).resolve().parents[1]/'scripts'


def main(source, output):
    output.mkdir(parents=True, exist_ok=False)
    root = ET.parse(source).getroot()
    nodes = {n.get('id'): n for n in root.findall('node')}
    def tags(element):
        return {t.get('k'): t.get('v') for t in element.findall('tag')}
    def point(node):
        n = nodes[node];return (float(n.get('lon')),float(n.get('lat')))
    roads, obstacles, rejected = [], [], []
    for way in root.findall('way'):
        t = tags(way);ids = [n.get('ref') for n in way.findall('nd')]
        if not all(n in nodes for n in ids) or len(ids)<2:
            continue
        line = LineString([point(n) for n in ids])
        if t.get('barrier') in {'wall','city_wall','fence','hedge','guard_rail','retaining_wall'}:
            obstacles.append(dict(id='way:'+way.get('id'),level=t.get('layer','0'),access='blocked',raw_tags=json.dumps(t),geometry=line))
        if 'highway' not in t:
            continue
        if any(point(a)==point(b) for a,b in zip(ids,ids[1:])):
            rejected.append(dict(id=way.get('id'),reason='duplicate consecutive coordinates'));continue
        permission = t.get('foot',t.get('access'))
        access = ('blocked' if permission in {'no','private'} else
                  'allowed' if permission in {'yes','designated','permissive'} else
                  'unknown' if permission is not None or t['highway'] in {'trunk','trunk_link','service'} else 'allowed')
        # Walking scenario: vehicle oneway remains in raw tags and is not applied
        # to walking; only explicitly tagged oneway:foot changes direction.
        oneway = t.get('oneway:foot','no')
        if oneway not in {'no','yes','-1'}:access='unknown';oneway='no'
        roads.append(dict(id=way.get('id'),nodes=json.dumps(ids),level=t.get('layer','0'),
                          access=access,direction={'yes':'forward','-1':'reverse','no':'both'}[oneway],
                          speed=4.8,raw_tags=json.dumps(t),geometry=line))
    for nid,node in nodes.items():
        t=tags(node)
        if t.get('barrier') in {'gate','lift_gate','chain','block'}:
            obstacles.append(dict(id='node:'+nid,level=t.get('layer','0'),access='unknown',
                                  raw_tags=json.dumps(t),geometry=Point(point(nid))))
    roads=gpd.GeoDataFrame(roads,geometry='geometry',crs=4326).to_crs(32630)
    # Mixed point/line obstacles use GeoPackage Unknown geometry support.
    obstacles=gpd.GeoDataFrame(obstacles,geometry='geometry',crs=4326).to_crs(roads.crs)
    envelopes=gpd.GeoDataFrame([dict(level=level,access='allowed',geometry=unary_union(list(group.geometry)).buffer(.02))
                               for level,group in roads.groupby('level')],crs=roads.crs)
    eligible=roads[(roads.access=='allowed') & (roads.geometry.length>10)].sort_values('id')
    selected=eligible.iloc[[round(i*(len(eligible)-1)/11) for i in range(12)]]
    demand=gpd.GeoDataFrame([dict(id=f'd{i:02}',level=r.level,weight=float(10*(i+1)),
                                 geometry=r.geometry.interpolate(.5,normalized=True))
                            for i,(_,r) in enumerate(selected.iterrows())],crs=roads.crs)
    facilities=demand.iloc[[0,3,6]].drop(columns='weight').copy();facilities['id']=['f0','f1','f2']
    # An off-network point must survive as unknown; no permissive blanket area.
    demand.loc[11,'geometry']=Point(demand.geometry.iloc[11].x+3000,demand.geometry.iloc[11].y)
    for name,gdf in [('roads',roads),('demand',demand),('facilities',facilities),('areas',envelopes),('barriers',obstacles)]:
        gdf.to_file(output/f'{name}.gpkg',driver='GPKG')
    p=dict(topology='source_vertices',edge_id='id',vertex_nodes='nodes',level='level',access='access',
           direction='direction',speed_kmh='speed',mode='shortest',travel_mode='walking scenario',analysis_crs=32630,
           origin_id='id',facility_id='id',point_level='level',demand_weight='weight',snap_m=10,
           budget=100,connector_speed_kmh=4.8,turn_restrictions='not_modelled',
           cost_assumptions='OSM centreline metres; explicit walking direction; no observed travel times',
           access_assumptions='Scenario only: footway/steps/pedestrian/unclassified presumed walkable unless tagged otherwise; 2cm centreline envelope for on-road points; walls blocked; gates unknown; missing layer=0; no field verification',
           facility_sets={'all':['f0','f1','f2'],'single':['f0']})
    summaries={}
    for budget in (100,400):
        p['budget']=budget
        params=output/f'params-{budget}.json';params.write_text(json.dumps(p,indent=2), encoding='utf-8')
        dest=output/f'budget-{budget}'
        command=[sys.executable,str(S/'network.py'),str(output/'roads.gpkg'),'--origins',str(output/'demand.gpkg'),
                 '--facilities',str(output/'facilities.gpkg'),'--params',str(params),'--access-areas',str(output/'areas.gpkg'),
                 '--barriers',str(output/'barriers.gpkg'),'--output',str(dest)]
        completed=subprocess.run(command,encoding='utf-8', errors='replace',capture_output=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        (output/f'cli-{budget}.json').write_text(json.dumps(dict(command=command,returncode=completed.returncode,
                                                              stdout=completed.stdout,stderr=completed.stderr),indent=2), encoding='utf-8')
        if completed.returncode:raise RuntimeError(completed.stderr)
        details=json.loads((dest/'record.json').read_text(encoding='utf-8'))['details']
        graph=gpd.read_file(dest/'graph.gpkg');arcs=graph.set_index('id').cost.to_dict()
        for row in details['od']:
            if row['cost'] is not None:
                assert abs(sum(arcs[k] for k in row['edge_keys'])+row['connector_cost']-row['cost'])<1e-7
        # Independent demand aggregation, one row per origin, all denominator retained.
        for name,ids in p['facility_sets'].items():
            totals=dict(covered=0.,uncovered=0.,unknown=0.)
            for _,d in demand.iterrows():
                od=[r for r in details['od'] if r['origin_id']==d.id and r['facility_id'] in ids]
                status=('covered' if any(r['cost'] is not None and r['cost']<=budget for r in od) else
                        'unknown' if any(r['connector_cost'] is None or (r['possible_cost'] is not None and r['possible_cost']<=budget) for r in od) else 'uncovered')
                totals[status]+=d.weight
            assert totals==details['coverage'][name]['weights']
            assert sum(totals.values())==details['coverage'][name]['total_weight']
        summaries[str(budget)]={name:entry['weights'] for name,entry in details['coverage'].items()}
    fig,axes=plt.subplots(1,2,figsize=(14,7),layout='constrained')
    colors={'covered':'#16804a','uncovered':'#b63a38','unknown':'#d99000'}
    for ax,budget in zip(axes,(100,400)):
        roads.plot(ax=ax,color='#cccccc',linewidth=.6)
        obstacles[obstacles.geom_type=='LineString'].plot(ax=ax,color='#433459',linewidth=1)
        route=gpd.read_file(output/f'budget-{budget}/routes.gpkg')
        if len(route):route.plot(ax=ax,color='#2378ad',linewidth=1.1,alpha=.65)
        det=json.loads((output/f'budget-{budget}/record.json').read_text(encoding='utf-8'))['details']
        states={r['origin_id']:r['status'] for r in det['coverage']['all']['demand']}
        for _,d in demand.iloc[:11].iterrows():
            ax.scatter(d.geometry.x,d.geometry.y,color=colors[states[d.id]],s=48,zorder=5)
            ax.annotate(d.id,(d.geometry.x,d.geometry.y),xytext={'d02':(-35,18),'d03':(12,8),'d07':(-26,-18)}.get(d.id,(3,4)),textcoords='offset points',fontsize=8)
        facilities.plot(ax=ax,marker='*',color='black',markersize=90,zorder=6)
        bounds=selected.total_bounds;ax.set_xlim(bounds[0]-30,bounds[2]+30);ax.set_ylim(bounds[1]-30,bounds[3]+30)
        ax.set_title(f'{budget} metres | synthetic demand weights\n{summaries[str(budget)]["all"]}',fontsize=10)
        ax.set_axis_off()
    fig.suptitle('Network-access candidate: Tower Bridge / St Katharine streets (OSM)\nGreen=covered; red=uncovered; amber=unknown; black stars=facilities; blue=known OD paths (all costs); d11 outside map remains unknown',fontsize=11)
    fig.savefig(output/'qa.png',dpi=160);plt.close(fig)
    result=dict(status='candidate',source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                source_note='OSM XML supplied by caller; ODbL attribution: OpenStreetMap contributors',
                source_bounds=root.find('bounds').attrib if root.find('bounds') is not None else None,
                roads=len(roads),barriers=len(obstacles),rejected_roads=rejected,
                bridge_ways=sum('bridge' in json.loads(t) for t in roads.raw_tags),
                tunnel_ways=sum('tunnel' in json.loads(t) for t in roads.raw_tags),
                scenarios=summaries,verified='CLI, GPKG geometry/attributes/CRS, every OD cost, demand aggregation',
                limits='Weights/facilities synthetic; access rules scenario assumptions; walls/gates OSM-labelled, no field verification; geometry source is real; off-network d11 intentionally unknown')
    (output/'acceptance.json').write_text(json.dumps(result,indent=2), encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--osm-xml',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();main(args.osm_xml,args.output)
