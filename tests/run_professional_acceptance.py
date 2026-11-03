#!/usr/bin/env python3
"""Real-data acceptance of network, suitability, inference, hydro review and nonlinear georeferencing; no silent downloads.

Inputs are supplied explicitly: RUN holds osm-roads.json, osm-facilities.json, nonlinear-evidence/ and
nonlinear-policy.json; --places is a Natural Earth populated-places GeoPackage; --terrain-root holds
inputs/dem.tif, inputs/reaches.gpkg and run0/hydrology/streams.tif from a prior terrain acceptance run.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import hashlib
import geopandas as gpd
import numpy as np
import rasterio as rio
from shapely.geometry import Point,LineString,box
import networkx as nx
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
from _delivery import digest,write_json
import network as net


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run');parser.add_argument('output');parser.add_argument('--pysal-python',required=True);parser.add_argument('--pysal-site',required=True);parser.add_argument('--pysal-proj',required=True);parser.add_argument('--places',required=True,type=Path);parser.add_argument('--terrain-root',required=True,type=Path);v=parser.parse_args()
    root=Path(v.run);out=Path(v.output).resolve();out.mkdir(exist_ok=False,parents=True);inp=out/'inputs';inp.mkdir();times=[];started=time.perf_counter()
    def cli(script,argv,tag):
        start=time.perf_counter();p=subprocess.run([sys.executable,str(S/script),*map(str,argv)],capture_output=True,encoding='utf-8', errors='replace',env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'));(out/f'{tag}.log').write_text(p.stdout+p.stderr, encoding='utf-8');p.check_returncode();times.append(dict(task=tag,seconds=time.perf_counter()-start))
    roads=json.loads((root/'osm-roads.json').read_text(encoding='utf-8'));nodes={e['id']:(e['lon'],e['lat']) for e in roads['elements'] if e['type']=='node'};rows=[];excluded=[]
    for e in roads['elements']:
        if e['type']!='way':continue
        tags=e.get('tags',{});direction=tags.get('oneway','yes' if tags.get('junction')=='roundabout' else 'no')
        if direction not in ('yes','1','true','-1','no','0','false') or any(tags.get(k) in ('no','private') for k in ('access','motor_vehicle','vehicle')):
            excluded.append(e['id']);continue
        for i,(a,b) in enumerate(zip(e['nodes'],e['nodes'][1:])):
            if a==b or nodes[a]==nodes[b]:continue
            rows.append(dict(id=f"{e['id']}:{i}",u=str(a),v=str(b),direction='forward' if direction in ('yes','1','true') else 'reverse' if direction=='-1' else 'both',speed_kmh=30,osm_way=str(e['id']),geometry=LineString([nodes[a],nodes[b]])))
    edges=gpd.GeoDataFrame(rows,geometry='geometry',crs=4326).to_crs(32645);edges.to_file(inp/'edges.gpkg',driver='GPKG',index=False)
    raw=json.loads((root/'osm-facilities.json').read_text(encoding='utf-8'));rows=[]
    for e in raw['elements']:
        if e.get('tags',{}).get('amenity') not in ('hospital','clinic'):continue
        loc=e.get('center',e)
        if 'lon' in loc:rows.append(dict(id=f"{e['type']}:{e['id']}",name=e.get('tags',{}).get('name','unknown'),location_kind='mapped point' if e['type']=='node' else 'way/relation centre, not entrance',geometry=Point(loc['lon'],loc['lat'])))
    facilities=gpd.GeoDataFrame(rows,geometry='geometry',crs=4326).to_crs(32645);facilities.to_file(inp/'facilities.gpkg',driver='GPKG',index=False)
    p=dict(edge_id='id',**{'from':'u','to':'v'},direction='direction',speed_kmh='speed_kmh',mode='fastest',analysis_crs='EPSG:32645',turn_restrictions='not_modelled',cost_assumptions='Diagnostic uniform 30 km/h scenario; not observed speed, navigation or emergency-response forecast. OSM directional/access tags preserved; turn restrictions unmodelled.',origin_id='id',facility_id='id',snap_m=150,budget=180)
    graph,coord,arcs,crs,factor=net.build(edges,p);component=sorted(max(nx.weakly_connected_components(graph),key=len));chosen=[component[i] for i in np.linspace(0,len(component)-1,8,dtype=int)]
    origins=gpd.GeoDataFrame({'id':chosen},geometry=[Point(coord[k]) for k in chosen],crs=crs);origins.to_file(inp/'origins.gpkg',driver='GPKG',index=False)
    ids=sorted(facilities.id);p['facility_sets']={'baseline':ids[:1],'expanded':ids}
    write_json(inp/'network.json',p)
    citypath=v.places;cities=gpd.read_file(citypath,bbox=(6,45,13,49));cities['source_id']=cities['NE_ID'].astype(str) if 'NE_ID' in cities else cities['NAMEASCII']+'|'+cities['ADM0NAME'];cities=cities[['source_id','NAME','POP_MAX','geometry']];cities.to_file(inp/'cities.gpkg',driver='GPKG',index=False)
    ip=dict(id='source_id',study='Natural Earth catalogue entries in 6..13E/45..49N; POP_MAX snapshot dates mixed/unknown; selection-biased catalogue, not current population census.',analysis_crs='EPSG:3035',operation='infer',value='POP_MAX',radius_m=180000,permutations=199,seed=731)
    write_json(inp/'infer.json',ip);bounds=cities.to_crs(3035).total_bounds;bounds+=np.array([-20000,-20000,20000,20000]);kp=dict(id='source_id',study=ip['study'],analysis_crs='EPSG:3035',operation='kde',bandwidth_m=50000,resolution_m=10000,bounds=bounds.tolist());write_json(inp/'kde.json',kp)
    terrainroot=v.terrain_root;dem=terrainroot/'inputs/dem.tif'
    with rio.open(dem) as d:
        z=d.read(1);t=d.transform;rr,cc=np.indices(z.shape);inside=(rr>0)&(cc>0)&(rr<z.shape[0]-1)&(cc<z.shape[1]-1)
        from terrain import derivatives
        terrain=derivatives(z,t.a,-t.e);flat=np.flatnonzero(inside)[::10];r,c=np.unravel_index(flat,z.shape);xx,yy=rio.transform.xy(t,r,c)
        sites=gpd.GeoDataFrame({'id':[f'{a}:{b}' for a,b in zip(r,c)],'elevation':z[r,c],'slope':terrain['slope'][r,c],'relief':terrain['relief'][r,c]},geometry=[Point(x,y) for x,y in zip(xx,yy)],crs=d.crs)
    sites.to_file(inp/'sites.gpkg',driver='GPKG',index=False)
    sp=dict(id='id',study='Terrain-only low-gradient site-screening demonstration; no land rights, geohazard, ecology or constructability claim. Indicator ranges and scenarios fixed before scoring.',criteria={'slope':dict(field='slope',low=0,high=35,prefer='low'),'relief':dict(field='relief',low=0,high=1200,prefer='low')},constraints=[dict(field='elevation',min=0,max=5000)],scenarios=[dict(id='baseline',weights={'slope':3,'relief':1},threshold=.5),dict(id='relief_priority',weights={'slope':1,'relief':3},threshold=.5),dict(id='strict',weights={'slope':1,'relief':1},threshold=.7)])
    write_json(inp/'suitability.json',sp)
    hp=dict(id='Reach_ID',spacing_m=225,tolerance_m=900,min_matched_fraction=.8,rationale='Exploratory 450m raster versus coarse global river catalogue: two raster cells 900m, >=80% line samples inside. Frozen before computing match fractions. Proximity is not hydrological identity.')
    write_json(inp/'hydro-review.json',hp)
    code_before={f.name:digest(f) for f in S.glob('*.py')};hashes=[];records=[]
    for i in range(2):
        run=out/f'run{i}';run.mkdir()
        cli('network.py',[inp/'edges.gpkg','--origins',inp/'origins.gpkg','--facilities',inp/'facilities.gpkg','--params',inp/'network.json','--output',run/'network'],f'network{i}')
        cli('suitability.py',[inp/'sites.gpkg','--params',inp/'suitability.json','--output',run/'suitability'],f'suitability{i}')
        for name in ['infer','kde']:
            cli('spatial-inference.py',[inp/'cities.gpkg','--params',inp/f'{name}.json','--output',run/name,'--backend-python',v.pysal_python,'--backend-site-packages',v.pysal_site,'--backend-proj-data',v.pysal_proj],f'{name}{i}')
        cli('hydro-review.py',[terrainroot/'inputs/reaches.gpkg',terrainroot/'run0/hydrology/streams.tif','--params',inp/'hydro-review.json','--output',run/'hydro-review'],f'hydro-review{i}')
        cli('nonlinear-georef.py',[root/'nonlinear-evidence/source.png',root/'nonlinear-evidence/gcps.json','--params',root/'nonlinear-policy.json','--output',run/'nonlinear'],f'nonlinear{i}')
        h={}
        for f in sorted(run.glob('*/*.gpkg')):
            g=gpd.read_file(f);attrs=g.drop(columns='geometry').fillna('NULL').astype(str).to_dict('records');h[str(f.relative_to(run))]=hashlib.sha256(json.dumps(attrs,sort_keys=True).encode()+b''.join(g.geometry.normalize().to_wkb())).hexdigest()
        for f in sorted(run.glob('*/*.tif')):
            with rio.open(f) as d:a=d.read();h[str(f.relative_to(run))]=hashlib.sha256(a.tobytes()).hexdigest()
        hashes.append(h);records.append({f.parent.name:json.loads(f.read_text(encoding='utf-8')) for f in run.glob('*/record.json')})
    assert hashes[0]==hashes[1] and code_before=={f.name:digest(f) for f in S.glob('*.py')}
    # Route costs independently checked with Bellman-Ford, not the routing algorithm.
    data=records[0]['network']['details'];snaps={a['source_id']:a['node'] for a in data['origin_snaps']};targets={a['source_id']:a['node'] for a in data['facility_snaps']}
    for o in data['od']:
        if o['cost'] is not None:assert np.isclose(nx.bellman_ford_path_length(graph,snaps[o['origin_id']],targets[o['facility_id']],weight='cost'),o['cost'],rtol=1e-12)
    scores=gpd.read_file(out/'run0/suitability/scores.gpkg')
    for row in scores.itertuples():
        if row.eligible:assert abs(sum(json.loads(row.contributions).values())-row.score)<1e-12
    stats=gpd.read_file(out/'run0/infer/statistics.gpkg');assert stats.source_id.tolist()==sorted(cities.source_id)
    fig,axs=plt.subplots(2,3,figsize=(16,9),layout='constrained')
    edges.plot(ax=axs[0,0],color='#cccccc',linewidth=.5);gpd.read_file(out/'run0/network/routes.gpkg').plot(ax=axs[0,0],color='blue',linewidth=.6);origins.plot(ax=axs[0,0],color='red',markersize=15);facilities.plot(ax=axs[0,0],color='orange',markersize=20);axs[0,0].set_title('Real OSM roads / facilities; 30 km/h scenario')
    from matplotlib.lines import Line2D
    axs[0,0].legend(handles=[Line2D([0],[0],color='blue',label='Routes'),Line2D([0],[0],marker='o',linestyle='',color='red',label='Origins'),Line2D([0],[0],marker='o',linestyle='',color='orange',label='Facilities')],fontsize=7)
    stats.plot(ax=axs[0,1],column='local_I',legend=True,cmap='coolwarm');axs[0,1].set_title('Local Moran I: catalogue POP_MAX')
    stats.plot(ax=axs[0,2],column='gi_q',legend=True,vmin=0,vmax=1,cmap='viridis');axs[0,2].set_title('Gi* BY-adjusted doubled-tail p')
    with rio.open(out/'run0/kde/density.tif') as d:arr=d.read(1);b=d.bounds
    im=axs[1,0].imshow(arr,extent=[b.left,b.right,b.bottom,b.top],origin='upper');fig.colorbar(im,ax=axs[1,0],label='catalogue records / km2');axs[1,0].set_title('Gaussian record density\nNo significance claim')
    for ax,scenario in zip(axs[1,1:],['baseline','strict']):scores[scores.scenario==scenario].plot(ax=ax,column='score',vmin=0,vmax=1,cmap='viridis',legend=True,missing_kwds={'color':'lightgray'});ax.set_title('Terrain score: '+scenario+'; gray excluded')
    for ax in axs.flat:ax.set_xlabel('Projected easting');ax.set_ylabel('Projected northing')
    fig.savefig(out/'qa.png',dpi=130);plt.close(fig)
    write_json(out/'acceptance.json',dict(wall_seconds=time.perf_counter()-started,cli_times=times,repeated_hashes=hashes,osm_roads_sha256=digest(root/'osm-roads.json'),osm_facilities_sha256=digest(root/'osm-facilities.json'),city_source_sha256=digest(citypath),osm_excluded_ways=excluded,counts=dict(edges=len(edges),origins=len(origins),facilities=len(facilities),cities=len(cities),sites=len(sites)),route_bellman_ford_check=True,contribution_check=True,hydro_status=records[0]['hydro-review']['status'],nonlinear_status=records[0]['nonlinear']['status'],nonlinear_preferred=records[0]['nonlinear']['preferred_model_by_frozen_check_rmse'],visual_review='pending',source_notes=['OSM ODbL, openstreetmap.org/copyright; bounded Overpass export','Natural Earth public domain catalogue, snapshot temporal identity unknown','DEM/reference river data retained from D2 evidence; no invented river geometry']))
    print(out)


if __name__=='__main__':main()
