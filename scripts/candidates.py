#!/usr/bin/env python3
"""Polygonize explicitly confirmed shared-edge networks, without semantic snapping."""
import argparse
import json
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, shape, mapping
from shapely.ops import polygonize_full
from shapely.strtree import STRtree
from coverage import annotation as A
from _delivery import bundle, digest, write_json


def ring_refs(ring, lines, state, tolerance):
    boundary=LineString(ring.coords)
    selected={k for k,g in lines.items() if boundary.covers(g)}
    if not selected: raise ValueError('Ring cannot be mapped to native edges')
    first=min(selected); edge=state['edges'][first]
    refs=[{'edge':first,'reverse':False}]; selected.remove(first)
    start=edge['start']; end=edge['end']
    while selected:
        possible=sorted(k for k in selected if end in (state['edges'][k]['start'],state['edges'][k]['end']))
        if len(possible)!=1: raise ValueError('Ring has ambiguous shared-node connectivity')
        k=possible[0]; e=state['edges'][k]; reverse=e['end']==end
        refs.append({'edge':k,'reverse':reverse}); end=e['start'] if reverse else e['end']; selected.remove(k)
    if start!=end: raise ValueError('Ring coordinates close without shared node IDs')
    if not LineString(A.ring_coords(refs,state,tolerance)).equals(boundary): raise ValueError('Native edge reconstruction differs')
    return refs


def execute(run, specification, output):
    meta,_=A.source(run); state=A.load(run); A.validate_structure(state)
    revision=Path(run)/'revisions'/f"{state['revision']:06d}.json"; before=digest(revision)
    specification_hash=digest(specification)
    p=json.loads(Path(specification).read_text(encoding='utf-8'))
    if p.get('schema_version')!=1 or p.get('source_sha256')!=meta['sha256'] or p.get('revision_sha256')!=before:
        raise ValueError('Confirmation must bind current source and revision hashes')
    tolerance=float(p.get('tolerance',.25)); confirmed=p['confirmed_edges']
    if not p.get('reviewer') or not p.get('evidence'): raise ValueError('Explicit network confirmation evidence required')
    if len(confirmed)!=len(set(confirmed)) or not set(confirmed)<=set(state['edges']):raise ValueError('Invalid confirmed edge IDs')
    frames=p['frames']; frame_ids=[f['id'] for f in frames]
    if not frames or len(frame_ids)!=len(set(frame_ids)):raise ValueError('Unique frame IDs required')
    areas=[shape(f['pixel_region']) for f in frames]
    from shapely.geometry import box
    for area in areas:
        if area.geom_type!='Polygon' or area.is_empty or not area.is_valid or not box(-.5,-.5,meta['width']-.5,meta['height']-.5).covers(area):raise ValueError('Invalid frame footprint')
    if any(a.intersection(b).area>0 for i,a in enumerate(areas) for b in areas[i+1:]):raise ValueError('Frames overlap; exclude inset with a main-frame hole')
    sampled={k:LineString(A.edge_coords(state['edges'][k],state['nodes'],tolerance)) for k in sorted(confirmed)}
    omitted=[{'edge':k,'reason':'not explicitly confirmed'} for k in state['edges'] if k not in confirmed]
    assignments={}; issues=[]
    for k,line in sampled.items():
        matches=[f['id'] for f,area in zip(frames,areas) if area.covers(line)]
        if len(matches)!=1 or state['edges'][k]['status']!='visible' or not line.is_simple or line.length==0:
            omitted.append({'edge':k,'reason':'cross-frame, uncertain/inferred, non-simple or zero-length edge'})
        else:assignments[k]=matches[0]
    all_faces=[]; diagnostics=[]
    for f in frames:
        lines={k:sampled[k] for k in assignments if assignments[k]==f['id']}
        keys=list(lines); geometries=list(lines.values()); tree=STRtree(geometries)
        bad=set()
        for i,line in enumerate(geometries):
            for j in tree.query(line,predicate='intersects'):
                j=int(j)
                if j<=i:continue
                a,b=keys[i],keys[j]; ea,eb=state['edges'][a],state['edges'][b]
                shared=set((ea['start'],ea['end'])) & set((eb['start'],eb['end']))
                intersection=line.intersection(geometries[j])
                allowed=intersection.geom_type in ('Point','MultiPoint') and all(any(Point(state['nodes'][n]['xy']).equals(pt) for n in shared) for pt in (list(intersection.geoms) if intersection.geom_type=='MultiPoint' else [intersection]))
                if not allowed:
                    bad.update((a,b));issues.append({'edges':[a,b],'frame':f['id'],'reason':'geometric intersection lacks explicit shared-node topology; no noding'})
        lines={k:v for k,v in lines.items() if k not in bad}
        polygons,cuts,dangles,invalid=polygonize_full(list(lines.values()))
        for kind,collection in [('cuts',cuts),('dangles',dangles),('invalid_rings',invalid)]:
            for geom in collection.geoms:diagnostics.append({'frame':f['id'],'kind':kind,'geometry':mapping(geom),'edges':[k for k,v in lines.items() if geom.covers(v)]})
        for polygon in sorted(polygons.geoms,key=lambda g:(g.bounds,g.wkb_hex)):
            try:
                face={'status':'candidate','outer':ring_refs(polygon.exterior,lines,state,tolerance),'holes':[ring_refs(r,lines,state,tolerance) for r in polygon.interiors]}
                rebuilt=A.face_geom(face,state,tolerance)
                if not rebuilt.is_valid or not rebuilt.equals(polygon):raise ValueError('Candidate reconstruction failed')
                all_faces.append({'id':f'candidate-{len(all_faces)+1:06d}','frame':f['id'],**face,'geometry':mapping(polygon)})
            except ValueError as e:issues.append({'frame':f['id'],'reason':str(e),'geometry':mapping(polygon)})
    result={'schema_version':1,'status':'hold','source_sha256':meta['sha256'],'revision':state['revision'],'revision_sha256':before,'confirmation':p,'confirmation_sha256':digest(specification),'sampling_tolerance_pixels':tolerance,'coordinate_space':'pixel centers, y down; no geographic CRS','candidates':all_faces,'diagnostics':diagnostics,'issues':issues,'omitted_edges':omitted,'native_edges':{k:state['edges'][k] for k in confirmed},'native_nodes':state['nodes'],'acceptance_scope':'geometric candidates only; semantic names, adoption and full-sheet visual acceptance require review'}
    with bundle(output) as out:
        write_json(out/'candidates.json',result)
        write_json(out/'pixel-features.json',{'type':'FeatureCollection','coordinate_space':result['coordinate_space'],'features':[{'type':'Feature','properties':{'id':c['id'],'frame':c['frame'],'status':'candidate'},'geometry':c['geometry']} for c in all_faces]})
        # GeoJSON is diagnostic pixel coordinates, never EPSG:4326.
        if digest(revision)!=before or A.load(run)['revision']!=state['revision'] or digest(specification)!=specification_hash:raise ValueError('Revision or confirmation changed')
        A.source(run)
    return {'output':str(output),'status':'hold','candidates':len(all_faces),'issues':len(issues)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run');p.add_argument('--confirmed',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:print(json.dumps(execute(a.run,a.confirmed,a.output)))
    except (ValueError,KeyError,TypeError,OSError) as e:p.exit(1,f'ERROR: {e}\n')
if __name__=='__main__':main()
