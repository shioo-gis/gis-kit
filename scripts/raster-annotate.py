#!/usr/bin/env python3
# /// script
# dependencies = ['pillow>=9.1', 'shapely>=2.0', 'geopandas>=1.0', 'pyogrio>=0.10']
# ///
"""Pixel-space annotation workbench. Model inference is performed by the caller."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import time
import re
import os
import tempfile
from pathlib import Path
import shutil
import uuid
from datetime import datetime, timezone
from html import escape

from PIL import Image, ImageDraw
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely import STRtree, make_valid
from shapely.validation import explain_validity
from _model_actor import POLICY, validate_actor

GROUPS = ('nodes', 'edges', 'points', 'faces')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    with tempfile.NamedTemporaryFile(mode='w', dir=Path(path).parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(payload)
    try:
        os.link(temporary, path)  # atomic publication; refuses existing paths
    finally:
        temporary.unlink()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load(run, revision=None):
    run = Path(run)
    if revision is None:
        revision = max(int(p.stem) for p in (run / 'revisions').glob('*.json'))
    return read(run / 'revisions' / f'{revision:06d}.json')


def source(run):
    meta = read(Path(run) / 'manifest.json')
    path = Path(run) / meta['image']
    if digest(path) != meta['sha256']:
        raise ValueError('Source image hash changed')
    return meta, path


def init(image, run, task):
    run = Path(run)
    with Image.open(image) as im:
        if getattr(im, 'n_frames', 1) != 1:
            raise ValueError('Select one raster page before initialization')
        width, height = im.size
    run.mkdir(parents=True, exist_ok=False)
    for folder in ('revisions', 'requests', 'views', 'checks', 'exports', 'sessions'):
        (run / folder).mkdir()
    name = 'source' + Path(image).suffix.lower()
    shutil.copyfile(image, run / name)
    write(run / 'manifest.json', {'image': name, 'sha256': digest(run / name),
          'width': width, 'height': height, 'task': task, 'model_policy': POLICY,
          'coordinates': 'original pixel centers; top-left=(0,0); x right; y down',
          'scope': 'single image; manual local views; no automatic tiling',
          'created_at': datetime.now(timezone.utc).isoformat()})
    state = {g: {} for g in GROUPS}
    state.update(revision=0, crossings=[])
    write(run / 'revisions/000000.json', state)
    return {'run': str(run.resolve()), 'revision': 0, 'size': [width, height], 'model_policy': POLICY}


def xy(p):
    if not isinstance(p, list) or len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p):
        raise ValueError('Coordinates must be finite [x,y] pairs')
    return p


def flatten_cubic(p0, p1, p2, p3, tolerance, depth=0):
    # Convex hull bounds the curve; control-point distances to the chord segment
    # bound positional error and catch collinear overshoot (not just line distance).
    dx, dy = p3[0]-p0[0], p3[1]-p0[1]
    length2 = dx*dx+dy*dy
    def distance2(p):
        t = max(0, min(1, ((p[0]-p0[0])*dx+(p[1]-p0[1])*dy)/length2)) if length2 else 0
        return (p[0]-p0[0]-t*dx)**2+(p[1]-p0[1]-t*dy)**2
    if max(distance2(p1), distance2(p2)) <= tolerance*tolerance:
        return [p0, p3]
    if depth >= 24:
        raise ValueError('Curve subdivision limit reached; tolerance not achieved')
    mid = lambda a, b: [(a[0]+b[0])/2, (a[1]+b[1])/2]
    a, b, c = mid(p0,p1), mid(p1,p2), mid(p2,p3)
    d, e = mid(a,b), mid(b,c)
    f = mid(d,e)
    return flatten_cubic(p0,a,d,f,tolerance,depth+1)[:-1] + flatten_cubic(f,e,c,p3,tolerance,depth+1)


def edge_coords(edge, nodes, tolerance=0.25):
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('Tolerance must be positive and finite')
    start, end = xy(nodes[edge['start']]['xy']), xy(nodes[edge['end']]['xy'])
    geom = edge['geometry']
    if geom['type'] == 'polyline':
        return [start] + [xy(p) for p in geom.get('vertices', [])] + [end]
    if geom['type'] != 'cubic' or not geom.get('segments'):
        raise ValueError('Expected polyline or nonempty native cubic geometry')
    result = [start]
    for index, segment in enumerate(geom['segments']):
        p1, p2 = xy(segment['c1']), xy(segment['c2'])
        p3 = xy(segment['end']) if 'end' in segment else (end if index == len(geom['segments'])-1 else xy(None))
        result.extend(flatten_cubic(result[-1], p1, p2, p3, tolerance)[1:])
    if result[-1] != end:
        raise ValueError('Last cubic endpoint must equal its shared end node')
    return result


def ring_coords(refs, state, tolerance, cache=None):
    if not refs:
        raise ValueError('Empty ring')
    coords, first, last = [], None, None
    for ref in refs:
        edge = state['edges'][ref['edge']]
        reverse = ref.get('reverse', False)
        if not isinstance(reverse, bool):
            raise ValueError('reverse must be boolean')
        a,b = (edge['end'],edge['start']) if reverse else (edge['start'],edge['end'])
        if last is not None and last != a:
            raise ValueError('Ring edges must share node IDs; no snapping')
        part = cache[ref['edge']] if cache is not None else edge_coords(edge, state['nodes'], tolerance)
        if reverse:
            part = part[::-1]
        coords.extend(part if not coords else part[1:])
        first = a if first is None else first
        last = b
    if last != first:
        raise ValueError('Ring is open; no automatic closure')
    return coords


def face_geom(face, state, tolerance, cache=None):
    return Polygon(ring_coords(face['outer'], state, tolerance, cache),
                   [ring_coords(r, state, tolerance, cache) for r in face.get('holes', [])])


def validate_structure(state, tolerance=0.25):
    cache = {}
    for group in GROUPS:
        if not isinstance(state[group], dict):
            raise ValueError('Object groups must be ID-keyed dictionaries')
        for key in state[group]:
            if not isinstance(key, str) or not key:
                raise ValueError('Object IDs must be nonempty strings')
    for item in state['nodes'].values():
        xy(item['xy'])
    for item in state['points'].values():
        xy(item['xy'])
    for key, edge in state['edges'].items():
        if edge['status'] not in ('visible', 'inferred', 'uncertain'):
            raise ValueError('Invalid edge status')
        if not isinstance(edge['kind'], str) or not edge['kind']:
            raise ValueError('Edge kind is required')
        cache[key] = edge_coords(edge, state['nodes'], tolerance)
    for face in state['faces'].values():
        if face['status'] not in ('candidate', 'incomplete', 'uncertain'):
            raise ValueError('Invalid face status')
        for ring in [face.get('outer', [])] + face.get('holes', []):
            for ref in ring:
                if ref['edge'] not in state['edges']:
                    raise ValueError('Unknown face edge reference')
        if face['status'] != 'incomplete':
            face_geom(face, state, tolerance, cache)
    for crossing in state.get('crossings', []):
        if len(crossing['edges']) != 2 or len(set(crossing['edges'])) != 2:
            raise ValueError('Crossing requires two distinct edge IDs')
        for key in crossing['edges']:
            if key not in state['edges']:
                raise ValueError('Unknown crossing edge')
        xy(crossing['xy'])
        if crossing['relation'] not in ('disconnected', 'uncertain'):
            raise ValueError('Connected junctions must use shared node IDs')
    return cache


def apply_patch(run, patch_path):
    run = Path(run)
    # Keep even rejected requests byte-for-byte; never overwrite a model draft.
    request = uuid.uuid4().hex
    shutil.copyfile(patch_path, run / 'requests' / (request + '.json'))
    patch, old = read(patch_path), load(run)
    validate_actor(patch.get('actor'))
    if patch['base_revision'] != old['revision']:
        raise ValueError('Stale base revision')
    source(run)
    if patch.get('view_id'):
        ident = patch['view_id']
        if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('Invalid view ID')
        record = read(run / 'views' / ident / 'view.json')
        if record['revision'] != old['revision']:
            raise ValueError('View belongs to a stale revision; request a fresh view')
        sx, sy = record['view_to_original']['scale']
        ox, oy = record['view_to_original']['offset']
        def convert(p):
            x, y = xy(p)
            return [x*sx+ox, y*sy+oy]
        for group in ('nodes', 'points'):
            for item in patch.get('put', {}).get(group, {}).values():
                item['xy'] = convert(item['xy'])
        for edge in patch.get('put', {}).get('edges', {}).values():
            geom = edge['geometry']
            if geom['type'] == 'polyline':
                geom['vertices'] = [convert(p) for p in geom.get('vertices', [])]
            elif geom['type'] == 'cubic':
                for segment in geom['segments']:
                    for key in ('c1', 'c2', 'end'):
                        if key in segment:
                            segment[key] = convert(segment[key])
        for edit in patch.get('splice', []):
            edit['vertices'] = [convert(p) for p in edit['vertices']]
        for crossing in patch.get('crossings', []):
            crossing['xy'] = convert(crossing['xy'])
    if patch.get('mode') not in ('draft', 'append', 'shape', 'topology'):
        raise ValueError('mode must be draft, append, shape or topology')
    if patch['mode'] == 'draft' and old['revision'] != 0:
        raise ValueError('draft only applies to revision 0')
    if patch['mode'] == 'append':
        if patch.get('delete') or patch.get('splice') or 'crossings' in patch:
            raise ValueError('Append only adds objects; use shape/topology for revisions or crossings')
        if not any(patch.get('put', {}).values()):
            raise ValueError('Append requires new objects; record empty observations in the session')
        for group, objects in patch['put'].items():
            if group not in GROUPS:
                raise ValueError('Unknown object group')
            if old[group].keys() & objects.keys():
                raise ValueError('Append cannot replace existing objects; reference their IDs without putting them again')
    state = copy.deepcopy(old)
    for group, ids in patch.get('delete', {}).items():
        if group not in GROUPS:
            raise ValueError('Unknown object group')
        for key in ids:
            del state[group][key]
    for group, objects in patch.get('put', {}).items():
        if group not in GROUPS:
            raise ValueError('Unknown object group')
        state[group].update(objects)
    edits_by_edge = {}
    for edit in patch.get('splice', []):
        edits_by_edge.setdefault(edit['edge'], []).append(edit)
    for key, edits in edits_by_edge.items():
        if key in patch.get('put', {}).get('edges', {}):
            raise ValueError('Cannot combine splice with put on the same edge')
        geom = state['edges'][key]['geometry']
        if geom['type'] != 'polyline':
            raise ValueError('splice only supports polyline interior vertices')
        vertices = geom.setdefault('vertices', [])
        original_length = len(vertices)
        for edit in edits:
            start, count = edit['start'], edit['delete_count']
            if type(start) is not int or type(count) is not int or not (0 <= start <= original_length and 0 <= count <= original_length-start):
                raise ValueError('Invalid splice range')
        previous_start = original_length+1
        for edit in sorted(edits,key=lambda e:e['start'],reverse=True):
            start,count=edit['start'],edit['delete_count']
            if start >= previous_start or start+count > previous_start:
                raise ValueError('Overlapping splices; indices must refer to the base revision')
            previous_start = start
            vertices[start:start+count] = [xy(p) for p in edit['vertices']]
    if 'crossings' in patch:
        state['crossings'] = patch['crossings']
    if patch['mode'] == 'shape':
        if any(state[g] != old[g] for g in ('nodes','points','faces')) or state['crossings'] != old['crossings'] or state['edges'].keys() != old['edges'].keys():
            raise ValueError('Shape revisions may only change edge geometry')
        for key, edge in state['edges'].items():
            if {k:v for k,v in edge.items() if k != 'geometry'} != {k:v for k,v in old['edges'][key].items() if k != 'geometry'}:
                raise ValueError('Shape revisions must preserve topology and metadata')
    validate_structure(state)
    state['revision'] += 1
    state['request_id'] = request
    write(run / 'revisions' / f"{state['revision']:06d}.json", state)
    return summary(run)


def related_ids(state, ids):
    wanted = set(ids)
    for key, face in state['faces'].items():
        if key in wanted:
            wanted.update(ref['edge'] for ring in [face.get('outer', [])] + face.get('holes', []) for ref in ring)
    for key, edge in state['edges'].items():
        if key in wanted:
            wanted.update((edge['start'], edge['end']))
    return wanted


def summary(run, ids=None):
    state = load(run)
    result = {'revision': state['revision'], 'counts': {g:len(state[g]) for g in GROUPS}}
    if ids:
        wanted = related_ids(state, ids)
        result['objects'] = {g:{k:v for k,v in state[g].items() if k in wanted} for g in GROUPS}
    return result


def candidate_pairs(geometries):
    """Deterministic pairs; avoids all-pairs GEOS calls for sparse linework."""
    if not geometries:
        return
    tree = STRtree(geometries)
    for i, geom in enumerate(geometries):
        for j in sorted(int(j) for j in tree.query(geom) if j > i):
            yield i, j


def self_crossings(coords, limit=8):
    segments = [LineString([a,b]) for a,b in zip(coords,coords[1:])]
    hits = []
    for i,j in candidate_pairs(segments):
        intersection = segments[i].intersection(segments[j])
        if intersection.is_empty:
            continue
        adjacent = j == i+1 or (i == 0 and j == len(segments)-1 and coords[0] == coords[-1])
        if adjacent and intersection.geom_type == 'Point':
            continue
        hits.append({'segments':[i,j], 'bounds':list(intersection.bounds)})
        if len(hits) >= limit:
            break
    return hits


def diagnose(state, width, height, tolerance=0.25):
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('Tolerance must be positive and finite')
    cache = validate_structure(state, tolerance)
    issues = []
    def add(kind, ids, **extra):
        issues.append({'kind':kind, 'objects':ids, **extra})
    extent = box(-0.5,-0.5,width-0.5,height-0.5)
    lines = {}
    for group in ('nodes','points'):
        seen = {}
        for key, item in state[group].items():
            pos = tuple(item['xy'])
            if not extent.covers(Point(pos)):
                add('out_of_image', [key])
            if pos in seen:
                add('coincident_' + group, [seen[pos],key])
            seen[pos] = key
    for key, edge in state['edges'].items():
        coords = cache[key]
        line = lines[key] = LineString(coords)
        if not line.is_simple or line.length == 0:
            hits = self_crossings(coords)
            add('self_intersection_or_degenerate', [key], locations=hits, bounds=hits[0]['bounds'] if hits else list(line.bounds))
        if not extent.covers(line):
            add('out_of_image', [key])
        if any(a == b for a,b in zip(coords,coords[1:])):
            add('duplicate_vertex', [key])
        if edge['status'] != 'visible':
            add('unresolved_evidence', [key])
    for crossing in state['crossings']:
        if crossing['relation'] == 'uncertain':
            add('uncertain_crossing', crossing['edges'])
        if any(Point(crossing['xy']).distance(lines[key]) > tolerance for key in crossing['edges']):
            add('stale_crossing_location', crossing['edges'])
    keys = list(lines)
    line_pairs = 0
    for i,j in candidate_pairs(list(lines.values())):
        a,b = keys[i],keys[j]
        line_pairs += 1
        intersection = lines[a].intersection(lines[b])
        if intersection.is_empty:
            continue
        if intersection.length > 0:
            add('overlap', [a,b], bounds=list(intersection.bounds)); continue
        shared = set((state['edges'][a]['start'],state['edges'][a]['end'])) & set((state['edges'][b]['start'],state['edges'][b]['end']))
        allowed = [Point(state['nodes'][n]['xy']) for n in shared]
        for c in state['crossings']:
            if set(c['edges']) == {a,b} and c['relation'] == 'disconnected':
                allowed.append(Point(c['xy']))
        points = list(intersection.geoms) if hasattr(intersection,'geoms') else [intersection]
        if any(not any(p.distance(q) <= tolerance for q in allowed) for p in points):
            add('unresolved_crossing', [a,b], bounds=list(intersection.bounds))
    polygons = {}
    for key, face in state['faces'].items():
        if face['status'] == 'incomplete':
            add('incomplete_face', [key]); continue
        polygon = face_geom(face,state,tolerance,cache)
        if not polygon.is_valid or polygon.area == 0:
            reason = explain_validity(polygon)
            match = re.search(r'\[([-+0-9.eE]+) ([-+0-9.eE]+)\]', reason)
            bounds = list(polygon.bounds)
            if match:
                x,y = map(float, match.groups()); bounds = [x,y,x,y]
            add('invalid_face', [key], reason=reason, bounds=bounds); continue
        polygons[key] = polygon
        if not extent.covers(polygon):
            add('out_of_image', [key])
        if face['status'] == 'uncertain':
            add('unresolved_evidence', [key])
    keys = list(polygons)
    face_pairs = 0
    for i,j in candidate_pairs(list(polygons.values())):
        a,b = keys[i],keys[j]
        face_pairs += 1
        intersection = polygons[a].intersection(polygons[b])
        if intersection.area > tolerance*tolerance:
            add('face_overlap', [a,b], bounds=list(intersection.bounds))
    for i, issue in enumerate(issues):
        issue['id'] = f'I{i+1:04d}'
    return {'revision':state['revision'], 'sampling_tolerance_pixels':tolerance,
            'issues':issues, 'accuracy':None,
            'candidate_pairs':{'edges':line_pairs,'faces':face_pairs},
            'limitations':['sampled geometry diagnostics only; no semantic accuracy claim',
                          'small intersections below sampling tolerance may be missed',
                          'no automatic tiling, seam repair, snapping or gap filling']}


def check(run, tolerance):
    meta, _ = source(run)
    result = diagnose(load(run),meta['width'],meta['height'],tolerance)
    path = Path(run) / 'checks' / (uuid.uuid4().hex + '.json')
    write(path,result)
    return {'report':str(path.resolve()), 'revision':result['revision'], 'issue_count':len(result['issues']), 'issues':result['issues'][:20]}


def view(run, bounds=None, max_size=1600, overlay=False, ids=None, labels=False, zoom=1.0, preprocess="none", kernel=21,
         overlay_width=1, overlay_visible_color='#d7191c'):
    meta, path = source(run)
    state = load(run)
    wanted = related_ids(state, ids) if ids else None
    if max_size <= 0 or not math.isfinite(zoom) or not 0 < zoom <= 8:
        raise ValueError('max-size must be positive; zoom must be in (0,8]')
    if type(overlay_width) is not int or not 1 <= overlay_width <= 8:
        raise ValueError('Overlay width must be an integer from 1 to 8 display pixels')
    bounds = bounds or [0,0,meta['width'],meta['height']]
    left,top,right,bottom = bounds
    if not (0 <= left < right <= meta['width'] and 0 <= top < bottom <= meta['height']):
        raise ValueError('Crop must be within source image; integer pixel-edge bounds')
    w,h = right-left,bottom-top
    scale = min(zoom,max_size/max(w,h))
    size = [max(1,round(w*scale)),max(1,round(h*scale))]
    sx,sy = w/size[0],h/size[1]
    offset = [left+(sx-1)/2,top+(sy-1)/2]
    from _raster_preprocess import crop_aid, MODES
    if preprocess not in MODES:
        raise ValueError('Unknown preprocessing mode')
    with Image.open(path) as im:
        clean = im.crop(bounds).convert('RGB').resize(size, Image.Resampling.LANCZOS)
        aid = crop_aid(im,bounds,preprocess,kernel).resize(size, Image.Resampling.LANCZOS) if preprocess != 'none' else None
    ident = uuid.uuid4().hex
    folder = Path(run) / 'views' / ident
    folder.mkdir()
    clean.save(folder / 'original.png')
    if aid is not None:
        aid.save(folder / 'enhanced.png')
    label_map = {}
    if overlay:
        picture = clean.copy()
        draw = ImageDraw.Draw(picture)
        def screen(p):
            return ((p[0]-offset[0])/sx,(p[1]-offset[1])/sy)
        def label(key, position, status):
            if labels:
                marker = str(len(label_map)+1)
                label_map[marker] = {'id':key, 'status':status}
                x,y = screen(position)
                draw.text((x+3,y+3),marker,fill='black',stroke_width=1,stroke_fill='white')
        for key, edge in state['edges'].items():
            if wanted is not None and key not in wanted:
                continue
            coords = edge_coords(edge,state['nodes'], min(sx,sy)*0.15)
            color = overlay_visible_color if edge['status']=='visible' else '#e69500'
            draw.line([screen(p) for p in coords],fill=color,width=overlay_width)
            label(key,LineString(coords).interpolate(0.5,normalized=True).coords[0],edge['status'])
        for key, point in state['points'].items():
            if wanted is not None and key not in wanted:
                continue
            x,y = screen(point['xy'])
            draw.ellipse([x-3,y-3,x+3,y+3],outline='#005dff',width=1)
            label(key,point['xy'],point.get('status','unknown'))
        picture.save(folder / 'overlay.png')
    record = {'view_id':ident,'revision':state['revision'],'bounds_pixel_edges':bounds,
              'size':size,'requested_zoom':zoom,
              'preprocess':{'mode':preprocess,'background_kernel':kernel,'generative':False,'coordinate_change':False,
                            'local_contrast_parameters':{'clip_limit':2.0,'tile_grid':[8,8],'original_weight':0.5,'enhanced_weight':0.5,'channel':'Lab L'} if preprocess=='local-contrast' else None,
                            'implementation_sha256':digest(Path(__file__).with_name('_raster_preprocess.py')),
                            'enhanced_sha256':digest(folder/'enhanced.png') if aid is not None else None,
                            'warning':'Viewing aid only; may suppress colored boundaries or distort faint strokes. Original remains authoritative.'},'view_to_original':{'scale':[sx,sy],'offset':offset},
              'formula':'original_xy = view_xy * scale + offset; both are pixel-center coordinates',
              'overlay':overlay, 'overlay_style':{'width_display_pixels':overlay_width,
                  'visible_color':overlay_visible_color, 'other_color':'#e69500'},
              'selected_ids':ids, 'label_map':label_map,
              'unresolved_faces':[k for k,f in state['faces'].items() if f['status'] in ('incomplete','uncertain') and (wanted is None or k in wanted)], 'source_sha256':meta['sha256'],
              'actually_viewed':None,
              'note':'View generated; caller must separately record actual model viewing'}
    write(folder / 'view.json',record)
    return {**record, 'directory':str(folder.resolve())}


def review_pack(run, bounds_list):
    """Numbered original/overlay pairs for human review, not automatic acceptance."""
    if not 1 <= len(bounds_list) <= 6:
        raise ValueError('Select 1 to 6 local review regions')
    run = Path(run)
    meta, _ = source(run)
    revision = load(run)['revision']
    for bounds in bounds_list:
        if (len(bounds) != 4 or any(type(v) is not int for v in bounds)
                or not 0 <= bounds[0] < bounds[2] <= meta['width']
                or not 0 <= bounds[1] < bounds[3] <= meta['height']):
            raise ValueError('Review bounds must be integer pixel edges within the source')
    ident = uuid.uuid4().hex
    folder = run/'checks'/ident
    folder.mkdir()
    (folder/'feedback').mkdir()
    (folder/'requests').mkdir()
    sheet = Image.new('RGB', (960, 280*len(bounds_list)), 'white')
    draw = ImageDraw.Draw(sheet)
    regions = []
    for i, bounds in enumerate(bounds_list):
        observation = view(run, bounds, max_size=1000, overlay=True,
                           overlay_width=3, overlay_visible_color='#007bc4')
        key = f'R{i+1:02d}'
        region = {'id':key, 'view_id':observation['view_id'],
                  'bounds_pixel_edges':bounds, 'status':'pending',
                  'directory':observation['directory'],
                  'view_to_original':observation['view_to_original']}
        regions.append(region)
        for j, name in enumerate(('original.png', 'overlay.png')):
            path = Path(observation['directory'])/name
            region[name.replace('.png', '_sha256')] = digest(path)
            with Image.open(path) as image:
                thumb = image.copy()
                thumb.thumbnail((460, 240), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (j*480+10, i*280+32))
            draw.text((j*480+10, i*280+8), f'{key} - {name[:-4]}', fill='black')
    sheet.save(folder/'index.png')
    write(folder/'pack.json', {'pack_id':ident, 'revision':revision,
          'source_sha256':meta['sha256'], 'regions':regions,
          'scope':'Selected regions only; pending until explicit human feedback',
          'note':'Blue lines are visible candidates; amber lines are uncertain/inferred, not accuracy ratings. Contact sheet is for selection; inspect full local original/overlay. Circles require visual interpretation, not automatic coordinate tracing.'})
    return {'pack_id':ident, 'revision':revision, 'directory':str(folder.resolve()),
            'index':str((folder/'index.png').resolve()), 'regions':regions}


def feedback(run, pack_id, record_path):
    """Record user-supplied regional decisions; never edit annotation geometry."""
    if (not isinstance(pack_id, str) or len(pack_id) != 32
            or any(c not in '0123456789abcdef' for c in pack_id)):
        raise ValueError('Invalid review pack ID')
    run = Path(run)
    folder = run/'checks'/pack_id
    pack = read(folder/'pack.json')
    request = uuid.uuid4().hex
    shutil.copyfile(record_path, folder/'requests'/(request+'.json'))
    record = read(record_path)
    meta, _ = source(run)
    if pack['revision'] != load(run)['revision'] or pack['source_sha256'] != meta['sha256']:
        raise ValueError('Stale review pack; regenerate for the current revision')
    if record.get('reviewer') != 'human':
        raise ValueError('Feedback must transcribe explicit human decisions, not model self-review')
    decisions = record.get('decisions')
    if not isinstance(decisions, dict) or not decisions:
        raise ValueError('Provide region-keyed human decisions')
    states = {r['id']:{'status':'pending'} for r in pack['regions']}
    versions = sorted((folder/'feedback').glob('*.json'))
    if versions:
        states = read(versions[-1])['decisions']
    attachments = []
    for key, decision in decisions.items():
        if key not in states or not isinstance(decision, dict):
            raise ValueError('Unknown region or invalid decision')
        if decision.get('status') not in ('accepted', 'rejected', 'pending'):
            raise ValueError('Status must be accepted, rejected or pending')
        note = decision.get('note', '')
        if not isinstance(note, str):
            raise ValueError('Feedback note must be text')
        value = {'status':decision['status'], 'note':note}
        if decision.get('marked_image'):
            path = Path(record_path).resolve().parent/decision['marked_image']
            with Image.open(path) as image:
                kind, size = image.format, list(image.size)
                image.verify()
            if kind not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Marked image must be PNG, JPEG or WEBP')
            name = f'{request}-{key}.{kind.lower()}'
            value['marked_image'] = {'file':name, 'sha256':digest(path), 'size':size,
                                    'coordinate_mapping':'unknown; visually relate to the original review view'}
            attachments.append((path, folder/'feedback'/name))
        states[key] = value
    for src, dst in attachments:
        shutil.copyfile(src, dst)
    sequence = int(versions[-1].stem)+1 if versions else 1
    result = {'pack_id':pack_id, 'revision':pack['revision'], 'reviewer':'human',
              'decisions':states, 'request_id':request,
              'recorded_at':datetime.now(timezone.utc).isoformat(),
              'rejected_ids':[key for key, value in states.items() if value['status']=='rejected'],
              'annotation_modified':False}
    path = folder/'feedback'/f'{sequence:06d}.json'
    write(path, result)
    return {**result, 'record':str(path.resolve())}


def normalize(run):
    """Remove only consecutive duplicate polyline vertices, with exact locus preserved."""
    run = Path(run)
    source(run)
    old = load(run)
    state = copy.deepcopy(old)
    changes = []
    for key,edge in state['edges'].items():
        geom = edge['geometry']
        if geom['type'] != 'polyline':
            continue
        vertices = geom.get('vertices', [])
        clean = []
        previous = state['nodes'][edge['start']]['xy']
        for point in vertices:
            if point != previous:
                clean.append(point)
            previous = point
        end = state['nodes'][edge['end']]['xy']
        while clean and clean[-1] == end:
            clean.pop()
        if clean != vertices:
            geom['vertices'] = clean
            changes.append({'edge':key,'removed_vertices':len(vertices)-len(clean)})
    if not changes:
        return {'revision':old['revision'],'changes':[]}
    validate_structure(state)
    request = uuid.uuid4().hex
    write(run/'requests'/(request+'.json'),{'actor':{'program':'raster-annotate','operation':'normalize'},
          'base_revision':old['revision'],'changes':changes,'policy':'exact duplicate removal only; shared nodes and native curves unchanged'})
    state['revision'] += 1
    state['request_id'] = request
    write(run/'revisions'/f"{state['revision']:06d}.json",state)
    return {'revision':state['revision'],'changes':changes}


def padded_bounds(bounds, width, height, padding):
    x0,y0,x1,y1 = bounds
    return [max(0,min(width-1,math.floor(x0-padding))),max(0,min(height-1,math.floor(y0-padding))),
            max(1,min(width,math.ceil(x1+padding+1))),max(1,min(height,math.ceil(y1+padding+1)))]


def review(run, issue_id=None, padding=40, zoom=3, preprocess='none'):
    """One compact diagnostic-and-view packet. All other results remain on disk."""
    started = time.perf_counter()
    if padding < 1:
        raise ValueError('padding must be positive')
    meta,_ = source(run)
    state = load(run)
    report = diagnose(state,meta['width'],meta['height'])
    folder = Path(run)/'checks'/uuid.uuid4().hex
    folder.mkdir()
    write(folder/'report.json',report)
    issues = report['issues']
    if issue_id:
        selected = next((i for i in issues if i['id']==issue_id),None)
        if selected is None:
            raise ValueError('Unknown issue ID for current revision')
    else:
        selected = next((i for i in issues if i.get('bounds')),issues[0] if issues else None)
    result = {'revision':state['revision'],'issue_count':len(issues),'report':str((folder/'report.json').resolve())}
    if selected is None:
        result['next_action'] = 'No geometry issues; final visual coverage review still required'
    else:
        bounds = selected.get('bounds')
        wanted = related_ids(state,selected['objects'])
        if bounds is None:
            positions=[]
            for group in ('nodes','points'):
                positions.extend(v['xy'] for k,v in state[group].items() if k in wanted)
            for key,edge in state['edges'].items():
                if key in wanted:
                    positions.extend(edge_coords(edge,state['nodes']))
            bounds = [min(p[0] for p in positions),min(p[1] for p in positions),max(p[0] for p in positions),max(p[1] for p in positions)] if positions else [0,0,meta['width']-1,meta['height']-1]
        crop = padded_bounds(bounds,meta['width'],meta['height'],padding)
        observation = view(run,crop,1200,True,selected['objects'],True,zoom,preprocess)
        windows=[]
        # Only nearby polyline vertices are returned, with indices for splice.
        for key,edge in state['edges'].items():
            if key not in wanted or edge['geometry']['type']!='polyline':
                continue
            vertices=edge['geometry'].get('vertices',[])
            selected_indices=[i for i,p in enumerate(vertices) if crop[0] <= p[0] < crop[2] and crop[1] <= p[1] < crop[3]]
            if not selected_indices:
                continue
            ranges=[]
            for index in selected_indices:
                lo,hi=max(0,index-1),min(len(vertices),index+2)
                if ranges and lo <= ranges[-1][1]:
                    ranges[-1][1]=max(ranges[-1][1],hi)
                else:
                    ranges.append([lo,hi])
            for lo,hi in ranges:
                windows.append({'edge':key,'start':lo,'delete_count':hi-lo,'vertices':vertices[lo:hi] if hi-lo<=40 else None,
                                'before':vertices[lo-1] if lo else state['nodes'][edge['start']]['xy'],
                                'after':vertices[hi] if hi<len(vertices) else state['nodes'][edge['end']]['xy'],
                                'coordinate_space':'original pixel centers; omit view_id when using these vertices',
                                'note':'request narrower view if vertices is null'})
        # Keep request context bounded even if many lines pass through a dense crop.
        omitted_windows=max(0,len(windows)-6)
        windows=windows[:6]
        result.update(issue=selected,view_id=observation['view_id'],directory=observation['directory'],
                      view_to_original=observation['view_to_original'],label_map=observation['label_map'],windows=windows,omitted_windows=omitted_windows,
                      next_action='Inspect original.png and overlay.png; use enhanced.png only as an optional aid. Submit only a local splice or mark uncertainty.')
    result['cli_seconds']=time.perf_counter()-started
    write(folder/'packet.json',result)
    return result


def repair_candidate(run, face_id):
    """Generate, never auto-adopt, a make_valid candidate; retain all components."""
    meta,_=source(run)
    state=load(run)
    face=state['faces'][face_id]
    if face['status']=='incomplete':
        raise ValueError('Missing boundary evidence: cannot repair an incomplete face')
    original=face_geom(face,state,.25)
    candidate=make_valid(original)
    folder=Path(run)/'checks'/uuid.uuid4().hex
    folder.mkdir()
    components=list(candidate.geoms) if hasattr(candidate,'geoms') else [candidate]
    record={'revision':state['revision'],'face':face_id,'source_sha256':meta['sha256'],
            'algorithm':'shapely.make_valid (linework)', 'coordinate_space':'original pixels; not RFC 7946 GeoJSON',
            'original_validity':explain_validity(original),'geometry':mapping(candidate),
            'component_types':[part.geom_type for part in components],
            'candidate_area_pixels2':candidate.area,'acceptance':'requires visual topology confirmation; never automatically adopted',
            'warning':'May split islands, close bays or change hole interpretation; no components discarded and source graph unchanged'}
    write(folder/'candidate.json',record)
    observation=view(run,padded_bounds(original.bounds,meta['width'],meta['height'],15),1200,True,[face_id],False,2)
    with Image.open(Path(observation['directory'])/'original.png') as im:
        picture=im.convert('RGB')
    draw=ImageDraw.Draw(picture)
    sx,sy=observation['view_to_original']['scale'];ox,oy=observation['view_to_original']['offset']
    def draw_geometry(g):
        if g.geom_type=='Polygon':
            draw_geometry(g.exterior)
            for ring in g.interiors:draw_geometry(ring)
        elif hasattr(g,'geoms'):
            for part in g.geoms:draw_geometry(part)
        elif g.geom_type in ('LineString','LinearRing'):
            draw.line([((x-ox)/sx,(y-oy)/sy) for x,y in g.coords],fill='#ab00ff',width=1)
        elif g.geom_type=='Point':
            x,y=(g.x-ox)/sx,(g.y-oy)/sy
            draw.ellipse((x-2,y-2,x+2,y+2),outline='#ab00ff')
    draw_geometry(candidate)
    picture.save(folder/'candidate-overlay.png')
    return {'candidate':str((folder/'candidate.json').resolve()),'overlay':str((folder/'candidate-overlay.png').resolve()),
            'component_types':record['component_types'],'source_revision_unchanged':state['revision'],'acceptance':record['acceptance']}


def svg_text(state,width,height):
    paths=[]
    for key, edge in state['edges'].items():
        p = state['nodes'][edge['start']]['xy']
        d = f'M {p[0]} {p[1]}'
        geom = edge['geometry']
        if geom['type']=='polyline':
            for p in geom.get('vertices',[])+[state['nodes'][edge['end']]['xy']]:
                d += f' L {p[0]} {p[1]}'
        else:
            for s in geom['segments']:
                endpoint = s.get('end', state['nodes'][edge['end']]['xy'])
                d += ' C '+' '.join(str(v) for p in (s['c1'],s['c2'],endpoint) for v in p)
        paths.append(f'<path id="{escape(key,quote=True)}" d="{d}"/>')
    for key,p in state['points'].items():
        x,y=p['xy']
        paths.append(f'<circle id="{escape(key,quote=True)}" cx="{x}" cy="{y}" r="2"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-0.5 -0.5 {width} {height}" width="{width}" height="{height}"><g fill="none" stroke="red" stroke-width="0.5">'+''.join(paths)+'</g></svg>'


def export(run, tolerance=0.25):
    meta,_ = source(run)
    state = load(run)
    report = diagnose(state,meta['width'],meta['height'],tolerance)
    folder = Path(run)/'exports'/uuid.uuid4().hex
    folder.mkdir()
    write(folder/'annotation.json',state)
    write(folder/'diagnostics.json',report)
    (folder/'curves.svg').write_text(svg_text(state,meta['width'],meta['height']),encoding='utf-8')
    # GeoPackage with undefined CRS; never emit pixel-space RFC 7946 GeoJSON.
    import geopandas as gpd
    rows = {'points':[], 'edges':[], 'faces':[]}
    for key,p in state['points'].items():
        rows['points'].append({'id':key,'properties_json':json.dumps(p,ensure_ascii=False),'geometry':Point(p['xy'])})
    for key,e in state['edges'].items():
        rows['edges'].append({'id':key,'properties_json':json.dumps({k:v for k,v in e.items() if k!='geometry'},ensure_ascii=False),'geometry':LineString(edge_coords(e,state['nodes'],tolerance))})
    omitted=[]
    for key,f in state['faces'].items():
        if f['status']=='incomplete':
            omitted.append(key); continue
        geom=face_geom(f,state,tolerance)
        if not geom.is_valid:
            omitted.append(key); continue
        rows['faces'].append({'id':key,'properties_json':json.dumps(f,ensure_ascii=False),'geometry':geom})
    for layer,items in rows.items():
        if items:
            gpd.GeoDataFrame(items,crs=None).to_file(folder/'pixel-candidates.gpkg',layer=layer,driver='GPKG',engine='pyogrio')
    write(folder/'export.json',{'revision':state['revision'],'source_sha256':meta['sha256'],
          'coordinate_system':'UNREFERENCED PIXEL ENGINEERING SPACE; y down; units pixels',
          'sampling_tolerance_pixels':tolerance,'omitted_faces':omitted,
          'gpkg_sha256':digest(folder/'pixel-candidates.gpkg') if (folder/'pixel-candidates.gpkg').exists() else None,
          'acceptance':'candidate; human confirmation required','issue_count':len(report['issues']),'issues':report['issues']})
    return {'directory':str(folder.resolve()),'omitted_faces':omitted,'issue_count':len(report['issues'])}


def session(run, record_path):
    record=read(record_path)
    validate_actor(record)
    if record.get('status') not in ('completed','failed','aborted'):
        raise ValueError('Invalid session status')
    views=record.get('viewed_ids',[])
    for ident in views:
        if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('Invalid view ID')
        if not (Path(run)/'views'/ident/'view.json').is_file():
            raise ValueError('Unknown actual view ID')
    usage=record.setdefault('usage',{})
    for key in ('input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens'):
        value=usage.setdefault(key,None)
        if value is not None and (type(value) is not int or value<0):
            raise ValueError('Usage must be actual nonnegative integers or null')
    for total,subset in (('input_tokens','cached_input_tokens'),('output_tokens','reasoning_output_tokens')):
        if usage[total] is not None and usage[subset] is not None and usage[subset]>usage[total]:
            raise ValueError('Usage subset exceeds total')
    stages=record.setdefault('stage_seconds',{})
    for key in ('acquisition','image_review_and_annotation','cli','export'):
        value=stages.setdefault(key,None)
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):
            raise ValueError('Stage times must be nonnegative finite seconds or null')
    for key in ('elapsed_seconds','revision_count'):
        record.setdefault(key,None)
    record['source_sha256']=read(Path(run)/'manifest.json')['sha256']
    record['recorded_at']=datetime.now(timezone.utc).isoformat()
    path=Path(run)/'sessions'/(uuid.uuid4().hex+'.json')
    write(path,record)
    return {'session':str(path.resolve()),'status':record['status'],'usage':usage}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('init');p.add_argument('image');p.add_argument('run');p.add_argument('--task',required=True)
    p=sub.add_parser('view');p.add_argument('run');p.add_argument('--bounds',nargs=4,type=int);p.add_argument('--max-size',type=int,default=1600);p.add_argument('--overlay',action='store_true');p.add_argument('--ids',nargs='+');p.add_argument('--labels',action='store_true');p.add_argument('--zoom',type=float,default=1);p.add_argument('--preprocess',choices=['none','gray','autocontrast','neutral-ink','palette-denoise','local-contrast'],default='none');p.add_argument('--kernel',type=int,default=21)
    p=sub.add_parser('normalize');p.add_argument('run')
    p=sub.add_parser('review');p.add_argument('run');p.add_argument('--issue');p.add_argument('--padding',type=int,default=40);p.add_argument('--zoom',type=float,default=3);p.add_argument('--preprocess',choices=['none','gray','autocontrast','neutral-ink','palette-denoise','local-contrast'],default='none')
    p=sub.add_parser('review-pack');p.add_argument('run');p.add_argument('--bounds',nargs=4,type=int,action='append',required=True)
    p=sub.add_parser('feedback');p.add_argument('run');p.add_argument('pack_id');p.add_argument('record')
    p=sub.add_parser('repair-candidate');p.add_argument('run');p.add_argument('--face',required=True)
    p=sub.add_parser('apply');p.add_argument('run');p.add_argument('patch')
    p=sub.add_parser('summary');p.add_argument('run');p.add_argument('--ids',nargs='+')
    for name in ('check','export'):
        p=sub.add_parser(name);p.add_argument('run');p.add_argument('--tolerance',type=float,default=0.25)
    p=sub.add_parser('session');p.add_argument('run');p.add_argument('record')
    args=parser.parse_args()
    try:
        if args.command=='init':result=init(args.image,args.run,args.task)
        elif args.command=='view':result=view(args.run,args.bounds,args.max_size,args.overlay,args.ids,args.labels,args.zoom,args.preprocess,args.kernel)
        elif args.command=='normalize':result=normalize(args.run)
        elif args.command=='review':result=review(args.run,args.issue,args.padding,args.zoom,args.preprocess)
        elif args.command=='review-pack':result=review_pack(args.run,args.bounds)
        elif args.command=='feedback':result=feedback(args.run,args.pack_id,args.record)
        elif args.command=='repair-candidate':result=repair_candidate(args.run,args.face)
        elif args.command=='apply':result=apply_patch(args.run,args.patch)
        elif args.command=='summary':result=summary(args.run,args.ids)
        elif args.command=='check':result=check(args.run,args.tolerance)
        elif args.command=='export':result=export(args.run,args.tolerance)
        else:result=session(args.run,args.record)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')))
    except (ValueError,KeyError,TypeError,OSError) as exc:
        parser.exit(2,f'Error: {exc}\n')

if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
