#!/usr/bin/env python3
"""Bounded Gaussian KDE and explicit PySAL Moran/Gi* permutation inference."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import numpy as np
import geopandas as gpd
from scipy.spatial import cKDTree
from scipy.stats import false_discovery_control
from sklearn.neighbors import KernelDensity
import rasterio as rio
from rasterio.transform import from_origin
from _daily import stable,number
from _metric import analysis_frame
from _delivery import bundle,digest,write_json
from _peak_rss import peak_rss_bytes
from _safe_io import iter_vector_chunks, write_vector_atomic
from raster import band_matches
from daily import fingerprint,clean
from _budget import check_vector


def prepare(frame,p):
    stable(frame,p['id'])
    if not frame.geom_type.eq('Point').all() or frame.geometry.is_empty.any() or not frame.geometry.is_valid.all() or frame.geometry.has_z.any() or not isinstance(p.get('study'),str) or not p['study'].strip():raise ValueError('Points and explicit study/source-selection assumptions required')
    frame,factor=analysis_frame(frame,p['analysis_crs']);frame=frame.assign(_key=frame[p['id']].astype(str)).sort_values('_key').drop(columns='_key').reset_index(drop=True)
    return frame,np.column_stack((frame.geometry.x,frame.geometry.y))*factor,factor


def infer(frame,xy,p,stage,backend):
    n=len(frame);radius=number(p['radius_m']);perms=p['permutations'];seed=p['seed']
    if not 4<=n<=2000 or radius<=0 or isinstance(perms,bool) or not isinstance(perms,int) or not 19<=perms<=9999 or isinstance(seed,bool) or not isinstance(seed,int) or not 0<=seed<2**32:raise ValueError('Invalid inferential size, radius, permutations or seed')
    y=__import__('pandas').to_numeric(frame[p['value']],errors='raise').to_numpy(float)
    if not np.isfinite(y).all() or (y<0).any() or np.var(y)==0:raise ValueError('Finite nonnegative nonconstant values required for Moran and Gi* bundle')
    tree=cKDTree(xy);neighbors={i:sorted(j for j in tree.query_ball_point(xy[i],radius) if j!=i) for i in range(n)}
    if sum(map(len,neighbors.values()))>1000000:raise ValueError('Weights edge guard exceeded')
    islands=[i for i,v in neighbors.items() if not v]
    if len(islands)==n:raise ValueError('No spatial neighbors')
    with tempfile.TemporaryDirectory(prefix='.pysal-',dir=stage.parent) as tmp:
        temp=Path(tmp);(temp/'home').mkdir()
        env=dict(os.environ,HOME=str(temp/'home'),PYSALDATA=str(temp/'pysal'),NUMBA_CACHE_DIR=str(temp/'numba'),MPLCONFIGDIR=str(temp/'mpl'),PYTHONDONTWRITEBYTECODE='1',NUMBA_DISABLE_JIT='1')
        if backend.get('site_packages'):env['PYTHONPATH']=backend['site_packages']
        if backend.get('proj_data'):env['PROJ_LIB']=backend['proj_data']
        write_json(temp/'input.json',dict(values=y.tolist(),neighbors=neighbors,permutations=perms,seed=seed))
        result=subprocess.run([backend['python'],str(Path(__file__).with_name('_pysal_worker.py')),str(temp/'input.json'),str(temp/'output.json')],env=env,capture_output=True,text=True)
        if result.returncode:raise ValueError('Explicit PySAL backend failed; no result published')
        stats=json.loads((temp/'output.json').read_text(encoding='utf-8'))
    out=frame.copy()
    for name in ('local_I','local_quadrant','local_p','gi_star','gi_z','gi_p'):out[name]=stats[name]
    for name in ('local_p','gi_p'):
        values=out[name].to_numpy(float);values[islands]=np.nan
        if name=='gi_p':values[~np.isfinite(out['gi_z'].to_numpy(float))]=np.nan
        valid=np.isfinite(values)
        adjusted=np.full(n,np.nan);adjusted[valid]=false_discovery_control(values[valid],method='by')
        out[name]=values;out[name.replace('_p','_q')]=adjusted
    out['island']=[i in islands for i in range(n)]
    write_vector_atomic(out,stage/'statistics.gpkg')
    count=0
    for start,back in iter_vector_chunks(stage/'statistics.gpkg'):
        part=out.iloc[start:start+len(back)]
        if back[p['id']].astype(str).tolist()!=part[p['id']].astype(str).tolist():raise ValueError('Statistics ID readback mismatch')
        for key in ('local_I','local_p','local_q','gi_star','gi_z','gi_p','gi_q'):np.testing.assert_allclose(back[key].to_numpy(float),part[key].to_numpy(float),rtol=0,atol=0,equal_nan=True)
        count+=len(back)
    if count!=len(out):raise ValueError('Statistics ID readback mismatch')
    write_json(stage/'weights.json',dict(ids=frame[p['id']].astype(str).tolist(),neighbors=neighbors,radius_m=radius,edge_rule='distance <= radius; no self',islands=islands))
    return dict(global_I=stats['global_I'],global_p=stats['global_p'],backend={k:stats[k] for k in ('esda','libpysal','numpy')},islands=islands,p_method='min(1,2*PySAL folded-tail p_sim); conditional local permutations; global random labels',adjustment='Benjamini-Yekutieli separately for local Moran and Gi* families; islands and undefined Gi* variance excluded',weights='Moran row standardized; Gi* binary including unit diagonal',execution='Existing PySAL with NUMBA_DISABLE_JIT=1; in-process NumPy boolean alias and esda 2.3.1 empty-neighborhood zero-lag guard; no installed edits or JIT bitwise equivalence claim',global_island_rule='All objects included, islands zero spatial lag',quadrants='PySAL: 1 HH, 2 LH, 3 LL, 4 HL; quadrant alone is not significance')


def kde(frame,xy,factor,p,stage):
    bandwidth=number(p['bandwidth_m']);resolution=number(p['resolution_m']);bounds=list(map(number,p['bounds']))
    if bandwidth<=0 or resolution<=0 or len(bounds)!=4 or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]:raise ValueError('Invalid KDE scale/bounds')
    xmin,ymin,xmax,ymax=np.array(bounds)*factor;w=int(np.ceil((xmax-xmin)/resolution));h=int(np.ceil((ymax-ymin)/resolution))
    if w*h>1000000:raise ValueError('KDE grid exceeds one million cells')
    if not ((xy[:,0]>=xmin)&(xy[:,0]<=xmax)&(xy[:,1]>=ymin)&(xy[:,1]<=ymax)).all():raise ValueError('Study bounds must contain all source points')
    model=KernelDensity(bandwidth=bandwidth,kernel='gaussian',algorithm='ball_tree').fit(xy)
    r,c=np.indices((h,w));query=np.column_stack((xmin+(c.ravel()+.5)*resolution,ymax-(r.ravel()+.5)*resolution))
    values=(np.exp(model.score_samples(query))*len(xy)*1e6).reshape(h,w)
    profile=dict(driver='GTiff',height=h,width=w,count=1,dtype='float64',crs=frame.crs,transform=from_origin(xmin/factor,ymax/factor,resolution/factor,resolution/factor))
    with rio.open(stage/'density.tif','w',**profile) as d:d.write(values,1);d.set_band_unit(1,'records/km2')
    with rio.open(stage/'density.tif') as d:
        if not band_matches(d,1,values,equal_nan=False):raise ValueError('KDE readback mismatch')
    return dict(unit='records per square kilometre',kernel='Gaussian Euclidean, bandwidth in metres',boundary='No edge correction or renormalization; tails outside study omitted; outer grid may extend by <1 cell',interpretation='Record density; not population density or significance')


def execute(source,p,output,backend):
    started=time.perf_counter();before=fingerprint(source);check_vector(source);frame,xy,factor=prepare(gpd.read_file(source),p)
    base={'id','study','analysis_crs','operation'};extra={'value','radius_m','permutations','seed'} if p['operation']=='infer' else {'bandwidth_m','resolution_m','bounds'}
    if p['operation'] not in ('infer','kde') or set(p)-(base|extra):raise ValueError('Unknown inference parameter')
    with bundle(output) as stage:
        details=infer(frame,xy,p,stage,backend) if p['operation']=='infer' else kde(frame,xy,factor,p,stage)
        resources=dict(self_max_rss_bytes=peak_rss_bytes(),completed_child_max_rss_bytes=peak_rss_bytes(children=True))
        if before!=fingerprint(source):raise ValueError('Input changed')
        write_json(stage/'record.json',clean(dict(status='candidate',parameters=p,input_sha256=before,details=details,resources=resources,wall_seconds=time.perf_counter()-started,implementation={n:digest(Path(__file__).with_name(n)) for n in ('spatial-inference.py','_pysal_worker.py')},artifacts={f.name:digest(f) for f in stage.iterdir()})))


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('input');a.add_argument('--params',required=True);a.add_argument('--output',required=True);a.add_argument('--backend-python',default=sys.executable);a.add_argument('--backend-site-packages');a.add_argument('--backend-proj-data');v=a.parse_args()
    try:execute(v.input,json.loads(Path(v.params).read_text(encoding='utf-8')),v.output,dict(python=v.backend_python,site_packages=v.backend_site_packages,proj_data=v.backend_proj_data))
    except (ValueError,KeyError,TypeError,OSError) as e:a.exit(1,f'ERROR: {e}\n')
