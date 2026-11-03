#!/usr/bin/env python3
"""Explicit directed network routing, bounded OD, service segments and coverage scenarios."""
import argparse
import json
from pathlib import Path
import time
import geopandas as gpd
import numpy as np
import networkx as nx
from shapely.geometry import Point, MultiLineString
from shapely.ops import substring
from _daily import stable, number
from _metric import analysis_frame
from _delivery import bundle, digest, write_json
from _peak_rss import peak_rss_bytes
from _safe_io import iter_vector_chunks, write_vector_atomic
from _core import clean
from _delivery import fingerprint
from _budget import check_vector


def build(edges,p):
    stable(edges,p['edge_id']);work,factor=analysis_frame(edges,p['analysis_crs'])
    if not work.geom_type.eq('LineString').all() or not work.geometry.is_valid.all() or work.geometry.is_empty.any() or work.geometry.has_z.any():raise ValueError('Network requires explicitly noded LineStrings')
    if p['mode'] not in ('shortest','fastest') or p.get('turn_restrictions')!='not_modelled' or not p.get('cost_assumptions'):raise ValueError('Explicit mode, cost assumptions and unmodelled turns acknowledgement required')
    graph=nx.MultiDiGraph();coordinates={};arcs={}
    work=work.assign(_eid=work[p['edge_id']].astype(str)).sort_values('_eid')
    for _,r in work.iterrows():
        u,v=r[p['from']],r[p['to']]
        if __import__('pandas').isna(u) or __import__('pandas').isna(v):raise ValueError('Missing node ID')
        u,v=str(u),str(v);geom=r.geometry
        if not u or not v or geom.length<=0:raise ValueError('Empty nodes or zero-length edge')
        for node,xy in [(u,geom.coords[0]),(v,geom.coords[-1])]:
            xy=tuple(xy[:2])
            if node in coordinates and Point(coordinates[node]).distance(Point(xy))*factor>1e-6:raise ValueError('Node ID has inconsistent endpoint coordinates')
            coordinates[node]=xy
        direction=r[p['direction']]
        if direction not in ('both','forward','reverse'):raise ValueError('Unknown direction; must be explicit')
        cost=geom.length*factor
        if p['mode']=='fastest':
            speed=number(r[p['speed_kmh']])
            if speed<=0:raise ValueError('Positive speed required')
            cost=cost/(speed/3.6)
        for a,b,reverse in ([(u,v,False),(v,u,True)] if direction=='both' else [(u,v,False)] if direction=='forward' else [(v,u,True)]):
            key=r['_eid']+(':r' if reverse else ':f');line=LineStringReverse(geom) if reverse else geom
            graph.add_edge(a,b,key=key,cost=cost);arcs[key]=dict(u=a,v=b,cost=cost,geometry=line,edge_id=r['_eid'])
    return graph,coordinates,arcs,work.crs,factor


def LineStringReverse(g):
    from shapely.geometry import LineString
    return LineString(list(g.coords)[::-1])


def snap(points,key,coordinates,crs,factor,limit):
    stable(points,key)
    if points.crs is None or not points.geom_type.eq('Point').all() or points.geometry.is_empty.any() or not points.geometry.is_valid.all() or points.geometry.has_z.any():raise ValueError('Georeferenced points required')
    points=points.to_crs(crs);nodes=sorted(coordinates);xy=np.array([coordinates[n] for n in nodes]);rows=[]
    if len(points)*len(nodes)>10000000:raise ValueError('Node-snap comparison guard exceeded')
    for _,r in points.assign(_key=points[key].astype(str)).sort_values('_key').iterrows():
        d=np.hypot(xy[:,0]-r.geometry.x,xy[:,1]-r.geometry.y)*factor;i=int(np.argmin(d))
        rows.append(dict(source_id=r['_key'],node=nodes[i] if d[i]<=limit else None,snap_m=float(d[i])))
    return rows


def analyze(edges,origins,facilities,p):
    if p.get("topology") == "source_vertices":
        raise ValueError("source_vertices requires access layers; use execute or _network_access.analyze")
    allowed={'edge_id','from','to','direction','speed_kmh','mode','analysis_crs','turn_restrictions','cost_assumptions','origin_id','facility_id','snap_m','budget','facility_sets','max_pairs'}
    if set(p)-allowed:raise ValueError('Unknown network parameter')
    graph,coords,arcs,crs,factor=build(edges,p);limit=number(p['snap_m']);budget=number(p['budget'])
    if limit<0 or budget<0:raise ValueError('Negative snap or budget')
    osnap=snap(origins,p['origin_id'],coords,crs,factor,limit);fsnap=snap(facilities,p['facility_id'],coords,crs,factor,limit)
    if len(osnap)*len(fsnap)>int(p.get('max_pairs',10000)):raise ValueError('OD guard exceeded')
    routes=[];od=[];segments=[]
    for o in osnap:
        dist,paths=nx.single_source_dijkstra(graph,o['node'],weight='cost') if o['node'] is not None else ({},{})
        for f in fsnap:
            cost=dist.get(f['node']);keys=[]
            if cost is not None:
                path=paths[f['node']]
                keys=[min(graph[a][b],key=lambda k:(graph[a][b][k]['cost'],k)) for a,b in zip(path,path[1:])]
                if not np.isclose(sum(arcs[k]['cost'] for k in keys),cost,rtol=1e-12,atol=1e-9):raise ValueError('Route cost sum mismatch')
                if keys:routes.append(dict(origin_id=o['source_id'],facility_id=f['source_id'],cost=cost,edge_keys=json.dumps(keys),geometry=MultiLineString([arcs[k]['geometry'] for k in keys])))
            reason='origin_unsnapped' if o['node'] is None else 'facility_unsnapped' if f['node'] is None else 'unreachable' if cost is None else 'reachable'
            od.append(dict(origin_id=o['source_id'],facility_id=f['source_id'],cost=cost,status=reason,edge_keys=keys,within_budget=cost is not None and cost<=budget))
        for key,a in sorted(arcs.items()):
            remain=budget-dist.get(a['u'],float('inf'))
            if remain>0:
                fraction=min(1,remain/a['cost']);line=substring(a['geometry'],0,fraction,normalized=True)
                if line.length:segments.append(dict(origin_id=o['source_id'],edge_key=key,fraction=fraction,geometry=line))
    table={}
    facility_ids={f['source_id'] for f in fsnap}
    for name,ids in p.get('facility_sets',{}).items():
        if not ids or len(ids)!=len(set(ids)) or not set(ids)<=facility_ids:raise ValueError('Facility sets require known unique IDs')
        selected=[r for r in od if r['facility_id'] in ids]
        served=sorted({r['origin_id'] for r in selected if r['within_budget']})
        table[name]={'served_origin_ids':served,'served_count':len(served)}
    if 'baseline' in table:
        base=set(table['baseline']['served_origin_ids'])
        for entry in table.values():
            entry['new_vs_baseline']=sorted(set(entry['served_origin_ids'])-base);entry['lost_vs_baseline']=sorted(base-set(entry['served_origin_ids']))
    nearest=[]
    for o in osnap:
        found=sorted([r for r in od if r['origin_id']==o['source_id'] and r['cost'] is not None],key=lambda r:(r['cost'],r['facility_id']))
        nearest.append(dict(origin_id=o['source_id'],facility_id=found[0]['facility_id'] if found else None,cost=found[0]['cost'] if found else None))
    return (gpd.GeoDataFrame(routes,geometry='geometry',crs=crs) if routes else gpd.GeoDataFrame({'origin_id':[]},geometry=[],crs=crs),gpd.GeoDataFrame(segments,geometry='geometry',crs=crs) if segments else gpd.GeoDataFrame({'origin_id':[]},geometry=[],crs=crs),dict(od=od,nearest=nearest,coverage=table,origin_snaps=osnap,facility_snaps=fsnap,units='metres' if p['mode']=='shortest' else 'seconds',weak_components=nx.number_weakly_connected_components(graph),snap_rule='Nearest endpoint node, inclusive threshold, node-ID tie; access distance excluded from route cost',turns='not modelled',service_geometry='directed reachable line segments, not isochrone polygons'))


def execute(source,p,origins,facilities,output,access_areas=None,barriers=None):
    started=time.perf_counter();paths=[Path(source),Path(origins),Path(facilities)];hashes=[fingerprint(x) for x in paths]
    extras={}
    if p.get('topology') == 'source_vertices':
        if access_areas is None or barriers is None:raise ValueError('Explicit access areas and barriers layers required (empty permitted)')
        from _network_access import analyze as accessible
        paths.extend([Path(access_areas),Path(barriers)]);hashes=[fingerprint(x) for x in paths]
        for x in paths:check_vector(x)
        routes,segments,details,extras=accessible(*(gpd.read_file(x) for x in paths[:3]),p,*(gpd.read_file(x) for x in paths[3:]))
    else:
        if access_areas is not None or barriers is not None:raise ValueError('Access layers require source_vertices topology')
        for x in paths:check_vector(x)
        routes,segments,details=analyze(*(gpd.read_file(x) for x in paths),p)
    with bundle(output) as stage:
        write_vector_atomic(routes,stage/'routes.gpkg');write_vector_atomic(segments,stage/'service.gpkg')
        for name,extra in extras.items():write_vector_atomic(extra,stage/(name+'.gpkg'))
        for name,expected in [('routes',routes),('service',segments),*extras.items()]:
            count=0
            for start,actual in iter_vector_chunks(stage/(name+'.gpkg')):
                part=expected.iloc[start:start+len(actual)]
                if actual.crs!=expected.crs or not actual.geometry.equals(part.geometry):raise ValueError('Network geometry readback mismatch')
                for column in expected.columns.drop('geometry'):
                    if actual[column].astype(str).tolist()!=part[column].astype(str).tolist():raise ValueError('Network attribute readback mismatch')
                count+=len(actual)
            if count!=len(expected):raise ValueError('Network geometry readback mismatch')
        if hashes!=[fingerprint(x) for x in paths]:raise ValueError('Input changed')
        write_json(stage/'record.json',clean(dict(status='candidate',parameters=p,details=details,input_hashes=hashes,input_files=[str(x) for x in paths],implementation=digest(__file__),access_implementation=digest(Path(__file__).with_name('_network_access.py')) if extras else None,networkx=nx.__version__,resources={'wall_seconds':time.perf_counter()-started,'max_rss_bytes':peak_rss_bytes()})))


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('input');a.add_argument('--params',required=True);a.add_argument('--origins',required=True);a.add_argument('--facilities',required=True);a.add_argument('--output',required=True);a.add_argument('--access-areas');a.add_argument('--barriers');v=a.parse_args()
    try:execute(v.input,json.loads(Path(v.params).read_text(encoding='utf-8')),v.origins,v.facilities,v.output,v.access_areas,v.barriers)
    except (ValueError,KeyError,TypeError,OSError) as e:a.exit(1,f'ERROR: {e}\n')
