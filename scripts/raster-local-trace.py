#!/usr/bin/env python3
# /// script
# dependencies = ['pillow>=9.1', 'numpy>=1.24', 'opencv-python', 'scikit-image', 'shapely>=2.0']
# ///
"""Local preprocessing -> skeleton candidates. Never edits the annotation graph."""
from __future__ import annotations
import argparse
import hashlib
import heapq
import json
import math
from pathlib import Path
import time

from _sandbox import configure_writable_caches
configure_writable_caches()

import cv2
import numpy as np
from PIL import Image, ImageDraw
from skimage.morphology import skeletonize
from shapely.geometry import LineString
from _raster_preprocess import crop_aid


def graph(skeleton):
    pixels={tuple(map(int,p)) for p in np.argwhere(skeleton)}
    adjacent={}
    for y,x in sorted(pixels):
        neighbors=[]
        for dy,dx in ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)):
            q=(y+dy,x+dx)
            if q not in pixels:continue
            # Don't create triangular shortcuts across an existing cardinal turn.
            if dy and dx and ((y+dy,x) in pixels or (y,x+dx) in pixels):continue
            neighbors.append(q)
        adjacent[(y,x)]=neighbors
    return adjacent


def paths(adjacency):
    nodes={p for p,n in adjacency.items() if len(n)!=2}
    visited=set();result=[]
    def edge(a,b):return tuple(sorted((a,b)))
    def walk(a,b):
        line=[a,b];visited.add(edge(a,b))
        while b not in nodes:
            candidates=[c for c in adjacency[b] if c!=a and edge(b,c) not in visited]
            if not candidates:break
            c=candidates[0];visited.add(edge(b,c));line.append(c);a,b=b,c
        return line
    for a in sorted(nodes):
        for b in adjacency[a]:
            if edge(a,b) not in visited:result.append(walk(a,b))
    for a in sorted(adjacency):
        for b in adjacency[a]:
            if edge(a,b) not in visited:result.append(walk(a,b))
    return result


def route(adjacency, start, end, snap):
    if not adjacency:raise ValueError('No skeleton pixels')
    def nearest(p):
        distances=heapq.nsmallest(2,((math.hypot(y-p[1],x-p[0]),(y,x)) for y,x in adjacency))
        distance,node=distances[0]
        if distance>snap:raise ValueError('Seed too far from skeleton; no gap bridging')
        if len(distances)>1 and abs(distances[1][0]-distance)<1e-9:
            raise ValueError('Ambiguous seed; specify a nearer skeleton pixel')
        return node,distance
    a,da=nearest(start);b,db=nearest(end)
    distance={a:0};parent={};queue=[(0,a)]
    while queue:
        cost,p=heapq.heappop(queue)
        if cost!=distance[p]:continue
        if p==b:break
        for q in adjacency[p]:
            total=cost+math.hypot(q[0]-p[0],q[1]-p[1])
            if total<distance.get(q,math.inf):
                distance[q]=total;parent[q]=p;heapq.heappush(queue,(total,q))
    if b not in distance:raise ValueError('Seeds are disconnected; no inferred connection added')
    result=[b]
    while result[-1]!=a:result.append(parent[result[-1]])
    return result[::-1],{'start_distance':da,'end_distance':db,'warning':'Shortest skeleton route only; junction choices need visual confirmation'}


def run(image,output,bounds,preprocess='gray',threshold=None,min_length=8,simplify=.5,start=None,end=None,snap=3):
    t=time.perf_counter()
    if min_length<0 or not math.isfinite(min_length) or simplify<0 or not math.isfinite(simplify) or snap<0 or not math.isfinite(snap):
        raise ValueError('Length, simplification and snap must be finite nonnegative values')
    if (start is None)!=(end is None):raise ValueError('Both route seeds are required')
    if start is not None and any(not math.isfinite(v) for p in (start,end) for v in p):raise ValueError('Seeds must be finite')
    if threshold is not None and not 0<=threshold<=255:raise ValueError('threshold must be in [0,255]')
    left,top,right,bottom=bounds
    with Image.open(image) as im:
        if not (0<=left<right<=im.width and 0<=top<bottom<=im.height):raise ValueError('Invalid source-pixel crop')
        if (right-left)*(bottom-top)>1000000:raise ValueError('Local tool limited to one million pixels')
        original=im.crop(bounds).convert('RGB')
        aid=crop_aid(im,bounds,preprocess).convert('L')
    gray=np.asarray(aid)
    actual,mask=cv2.threshold(gray,0 if threshold is None else threshold,255,cv2.THRESH_BINARY_INV | (cv2.THRESH_OTSU if threshold is None else 0))
    skeleton=skeletonize(mask>0)
    adjacent=graph(skeleton)
    candidates=paths(adjacent);route_info=None
    if start is not None:
        selected,route_info=route(adjacent,[start[0]-left,start[1]-top],[end[0]-left,end[1]-top],snap)
        candidates=[selected]
    items=[];omitted=0
    for points in candidates:
        if len(points)<2:continue
        coords=[(x+left,y+top) for y,x in points]
        line=LineString(coords)
        if line.length<min_length and start is None:omitted+=1;continue
        reduced=line.simplify(simplify,preserve_topology=True)
        # Closed and open candidates retain endpoint identities; no contour fitting.
        items.append({'id':f'L{len(items)+1:03d}','geometry':{'type':'polyline','coordinates':list(reduced.coords)},
                      'length_pixels':line.length,'start_degree':len(adjacent[points[0]]),'end_degree':len(adjacent[points[-1]]),
                      'status':'program_trace_candidate'})
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    original.save(out/'original.png');aid.save(out/'enhanced.png')
    Image.fromarray(mask).save(out/'mask.png');Image.fromarray(np.uint8(skeleton)*255).save(out/'skeleton.png')
    # Render all candidate IDs locally, but return no coordinate dump to the agent.
    overlay=original.copy();draw=ImageDraw.Draw(overlay)
    labeled=original.copy();label_draw=ImageDraw.Draw(labeled)
    for item in items:
        coords=[(x-left,y-top) for x,y in item['geometry']['coordinates']]
        draw.line(coords,fill='#00a0ff',width=1)
        label_draw.line(coords,fill='#00a0ff',width=1)
        x,y=coords[len(coords)//2];label_draw.text((x+2,y+2),item['id'],fill='red',stroke_width=1,stroke_fill='white')
    overlay.resize((overlay.width*3,overlay.height*3),Image.Resampling.NEAREST).save(out/'overlay-3x.png')
    labeled.resize((labeled.width*3,labeled.height*3),Image.Resampling.NEAREST).save(out/'labels-3x.png')
    original.resize((original.width*3,original.height*3),Image.Resampling.LANCZOS).save(out/'original-3x.png')
    h=hashlib.sha256()
    with Path(image).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    record={'source_sha256':h.hexdigest(),'bounds_pixel_edges':bounds,'coordinate_space':'original pixel centers; not geographic coordinates',
            'preprocess':preprocess,'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'preprocess_implementation_sha256':hashlib.sha256(Path(__file__).with_name('_raster_preprocess.py').read_bytes()).hexdigest(),
            'mask_sha256':hashlib.sha256((out/'mask.png').read_bytes()).hexdigest(),
            'skeleton_sha256':hashlib.sha256((out/'skeleton.png').read_bytes()).hexdigest(),
            'threshold':actual,'threshold_method':'otsu' if threshold is None else 'fixed',
            'simplification_pixels':simplify,'min_length_pixels':min_length,'omitted_short_paths':omitted,
            'route':route_info,'seeds_original_pixels':{'start':start,'end':end},
            'paths':items,'cli_seconds':time.perf_counter()-t,
            'warning':'Skeleton can follow hatching/text or merge nearby shores. No density closing, hole filling, endpoint bridging or automatic patch adoption.',
            'provenance':'Adapted graph tracing pattern from user atlas extract_boundaries.py; atlas-specific semantic masks and density thresholds excluded'}
    (out/'candidates.json').write_text(json.dumps(record,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    return {'directory':str(out.resolve()),'paths':len(items),'threshold':actual,'cli_seconds':record['cli_seconds'],'route':route_info}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('image');p.add_argument('output');p.add_argument('--bounds',nargs=4,type=int,required=True)
    p.add_argument('--preprocess',choices=['gray','autocontrast','neutral-ink','palette-denoise'],default='gray')
    p.add_argument('--threshold',type=float);p.add_argument('--min-length',type=float,default=8);p.add_argument('--simplify',type=float,default=.5)
    p.add_argument('--start',nargs=2,type=float);p.add_argument('--end',nargs=2,type=float);p.add_argument('--snap',type=float,default=3)
    args=p.parse_args()
    try:print(json.dumps(run(args.image,args.output,args.bounds,args.preprocess,args.threshold,args.min_length,args.simplify,args.start,args.end,args.snap)))
    except (ValueError,OSError) as exc:p.exit(2,f'Error: {exc}\n')

if __name__=='__main__':main()
