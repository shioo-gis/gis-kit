#!/usr/bin/env python3
"""Frozen-tolerance comparison of reference river lines and candidate stream pixels."""
import argparse
import json
import time
from pathlib import Path
import numpy as np
import geopandas as gpd
import rasterio as rio
from scipy.spatial import cKDTree
from _daily import geometry,number
from _delivery import bundle,digest,write_json
from _peak_rss import peak_rss_bytes
from _safe_io import write_vector_atomic
from _metric import _horizontal_meters_per_unit
from pyproj import CRS
from _core import clean
from _delivery import fingerprint
from _budget import check_raster, check_vector


def trace_d8(drainage, start):
    """Trace GRASS D8 drainage; negative codes leave the computational region."""
    directions={1:(-1,1),2:(-1,0),3:(-1,-1),4:(0,-1),5:(1,-1),6:(1,0),7:(1,1),8:(0,1)}
    row,col=map(int,start);path=[];seen=set();a=np.ma.asarray(drainage)
    while 0<=row<a.shape[0] and 0<=col<a.shape[1]:
        if (row,col) in seen:return path,'cycle'
        seen.add((row,col));path.append((row,col));value=a[row,col]
        if np.ma.is_masked(value) or not np.isfinite(value):return path,'missing'
        code=int(value)
        if code!=value or abs(code)>8:raise ValueError('Invalid GRASS D8 direction')
        if code<0:return path,'region_exit'
        if code==0:return path,'sink'
        dr,dc=directions[code];row+=dr;col+=dc
    return path,'region_exit'


def execute(rivers,streams,p,output):
    started=time.perf_counter()
    if set(p)!={'id','spacing_m','tolerance_m','min_matched_fraction','rationale'} or not p['rationale']:raise ValueError('Frozen review policy required')
    spacing=number(p['spacing_m']);tolerance=number(p['tolerance_m']);minimum=number(p['min_matched_fraction'])
    if spacing<=0 or tolerance<0 or not 0<=minimum<=1:raise ValueError('Invalid review threshold')
    hashes=[fingerprint(rivers),digest(streams)]
    with rio.open(streams) as d:
        if d.crs is None:raise ValueError('Stream raster CRS missing')
        check_raster(d,arrays=3)  # masked band, validity mask and the candidate pixel indices
        factor=_horizontal_meters_per_unit(CRS(d.crs));a=d.read(1,masked=True);valid=(~np.ma.getmaskarray(a))&np.isfinite(a.data)&(a.data>0)
        rr,cc=np.where(valid)
        if not len(rr):raise ValueError('No candidate stream pixels')
        xx,yy=rio.transform.xy(d.transform,rr,cc);tree=cKDTree(np.column_stack((xx,yy))*factor)
        check_vector(rivers);samples,_=geometry(gpd.read_file(rivers),None,dict(id=p['id'],method='sample',distance_m=spacing,analysis_crs=str(d.crs)))
        distances,_=tree.query(np.column_stack((samples.geometry.x,samples.geometry.y))*factor)
        rows,cols=rio.transform.rowcol(d.transform,samples.geometry.x,samples.geometry.y)
        inside=(np.array(rows)>=0)&(np.array(rows)<d.height)&(np.array(cols)>=0)&(np.array(cols)<d.width)
        samples['stream_distance_m']=np.where(inside,distances,np.nan);samples['within_tolerance']=inside&(distances<=tolerance);samples['inside_extent']=inside
    reports=[]
    for key,g in samples.groupby('source_id',sort=True):
        fraction=float(g.within_tolerance.mean());reports.append(dict(source_id=str(key),sample_count=len(g),outside=int((~g.inside_extent).sum()),matched_fraction=fraction,max_distance_m=clean(g.stream_distance_m.max()),p95_distance_m=clean(g.stream_distance_m.quantile(.95)),status='pass' if g.inside_extent.all() and fraction>=minimum else 'hold'))
    with bundle(output) as stage:
        write_vector_atomic(samples,stage/'samples.gpkg')
        if hashes!=[fingerprint(rivers),digest(streams)]:raise ValueError('Input changed')
        write_json(stage/'record.json',dict(status='pass' if all(r['status']=='pass' for r in reports) else 'hold',policy=p,reaches=reports,input_hashes=hashes,scope='Sample-to-stream-cell-centre proximity only. Does not establish outlet identity, flow topology, historical river location or ground truth.',resources=dict(wall_seconds=time.perf_counter()-started,max_rss_bytes=peak_rss_bytes()),implementation=digest(__file__)))


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('rivers');a.add_argument('streams');a.add_argument('--params',required=True);a.add_argument('--output',required=True);v=a.parse_args()
    try:execute(v.rivers,v.streams,json.loads(Path(v.params).read_text(encoding='utf-8')),v.output)
    except (ValueError,KeyError,TypeError,OSError) as e:a.exit(1,f'ERROR: {e}\n')
