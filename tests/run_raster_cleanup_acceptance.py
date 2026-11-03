#!/usr/bin/env python3
"""Real local DEM, explicitly synthetic missingness/policies, full F CLI recipe and QA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio as rio
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
from _delivery import digest, write_json


def ref(name):
    return {'input_ref': name}


def decode(path):
    with rio.open(path) as ds:
        return ds.read(1, masked=True).astype(float).filled(np.nan)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source');p.add_argument('output');p.add_argument('--source-description',required=True)
    args=p.parse_args();root=Path(args.output).resolve();repo=SCRIPTS.parents[1]
    if root==repo or repo in root.parents:
        raise ValueError('Output must be repository-external')
    root.mkdir(parents=True,exist_ok=False);inputs=root/'inputs';inputs.mkdir()
    started=time.perf_counter();original=Path(args.source).resolve();before=digest(original)
    shutil.copy2(original,inputs/'source.tif')
    with rio.open(original) as ds:
        a=ds.read(1,masked=True).astype(float).filled(np.nan);profile=ds.profile.copy()
        if min(a.shape)<15 or a.size>100000 or not ds.crs.is_projected:
            raise ValueError('Bounded projected DEM at least 15x15 required')
        bounds=ds.bounds;transform=ds.transform;crs=ds.crs;res=max(ds.res)
    damaged=a.copy();damaged[5,5]=np.nan;damaged[8,8]=np.nan;damaged[10:13,10:13]=np.nan;damaged[0,3]=np.nan
    protect=np.zeros(a.shape);protect[8,8]=1
    profile.update(dtype='float64',nodata=np.nan,count=1)
    for name,data in [('diagnostic-missing',damaged),('protected',protect)]:
        with rio.open(inputs/f'{name}.tif','w',**profile) as ds:ds.write(data,1)
    left,bottom,right,top=bounds;mid=(left+right)/2+.23*res
    shapes=[box(left,bottom,mid,top).difference(box(left+res,bottom+res,left+3*res,bottom+3*res)),box(mid,bottom,right,top)]
    gpd.GeoDataFrame({'id':['west','east']},geometry=shapes,crs=crs).to_file(inputs/'zones.gpkg',driver='GPKG')
    # Fixed before processing; all policies/voids/zones are diagnostic, no temporal observation claim.
    policy=dict(source_sha256=before,source_description=args.source_description,
                original_evidence_modified=False,source_identity_reverified=False,
                scope='Real stored elevation; injected voids, zones and classification/smoothing scenarios are synthetic; changes are processing-policy differences, not dated observations.',
                class_breaks=[-20000,2000,3000,4000,5000,20000],absolute_tolerance=1e-9,relative_tolerance=1e-12,
                expected_fill_cell=[5,5],protected_cell=[8,8],large_hole=[10,13,10,13],blocks=[1,256])
    write_json(inputs/'frozen-policy.json',policy)
    rules=[dict(min=lo,max=hi,output=i) for i,(lo,hi) in enumerate(zip(policy['class_breaks'][:-1],policy['class_breaks'][1:]))]
    steps=[]
    def step(ident,op,sources,params,artifact='result.tif'):
        steps.append(dict(id=ident,runner='raster',operation=op,files=dict(inputs=[ref(s) for s in sources]),params=params,artifact=artifact))
    step('repair','fill_holes',['damaged'],dict(kind='continuous',method='idw',max_hole_pixels=1,max_distance_m=res*1.5,protected=ref('protected')))
    step('smooth','focal',['repair'],dict(kind='continuous',statistic='mean',size=3,boundary='partial',min_coverage=.5,protected=ref('protected')))
    step('baseline','reclassify',['damaged'],dict(rules=rules,unmapped='error'))
    step('classes','reclassify',['smooth'],dict(rules=rules,unmapped='error'))
    step('areas','class_area',['classes'],dict(vector=ref('zones'),id='id',method='fractional'),'table.json')
    step('change','transition',['baseline','classes'],{})
    step('instances','regions',['classes'],dict(connectivity=8))
    steps.append(dict(id='terrain',runner='terrain',operation='run',files=dict(input=ref('source')),params=dict(vertical_unit='metre',vertical_datum='unknown',source_description=args.source_description),artifact='slope.tif'))
    base=dict(schema_version=1,inputs=dict(source=str(inputs/'source.tif'),damaged=str(inputs/'diagnostic-missing.tif'),protected=str(inputs/'protected.tif'),zones=str(inputs/'zones.gpkg')),steps=steps)
    hashes={f.name:digest(f) for f in inputs.iterdir()};runs=[];signatures=[]
    def run(name,recipe,cache):
        spec=root/(name+'.json');write_json(spec,recipe);start=time.perf_counter()
        result=subprocess.run([sys.executable,str(SCRIPTS/'recipe.py'),str(spec),'--output',str(root/name),'--cache',str(root/cache)],capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        (root/(name+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        if result.returncode:raise RuntimeError(result.stderr)
        answer=json.loads(result.stdout);runs.append(dict(name=name,seconds=time.perf_counter()-start,**answer));return root/name/'default'
    for index,block in enumerate((1,256)):
        data=json.loads(json.dumps(base))
        for s in data['steps']:
            if s['runner']=='raster':s['params']['block_size']=block
        folder=run('run'+str(index),data,'cache'+str(index))
        repair=decode(folder/'repair/result.tif')
        assert np.isfinite(repair[5,5]) and np.isnan(repair[8,8]) and np.isnan(repair[10:13,10:13]).all() and np.isnan(repair[0,3])
        modified=decode(folder/'repair/modified.tif');assert modified.sum()==1
        np.testing.assert_equal(repair[modified==0],damaged[modified==0])
        record=json.loads((folder/'change/record.json').read_text(encoding='utf-8'));old=decode(folder/'baseline/result.tif');new=decode(folder/'classes/result.tif')
        joint=np.isfinite(old)&np.isfinite(new); pixel=abs(transform.determinant)
        assert abs(record['joint_valid_area_m2']-joint.sum()*pixel)<1e-9
        assert abs(sum(row['area_m2'] for row in record['codebook'])-joint.sum()*pixel)<1e-9
        np.testing.assert_equal(decode(folder/'change/change.tif'),np.where(joint,(old!=new).astype(float),np.nan))
        areas=json.loads((folder/'areas/table.json').read_text(encoding='utf-8'))
        for row in areas:np.testing.assert_allclose(sum(x['area_m2'] for x in row['classes']),row['valid_area_m2'],atol=1e-9,rtol=1e-12)
        sig={}
        for name in ('repair','smooth','baseline','classes','change','instances','terrain'):
            path=folder/name/('slope.tif' if name=='terrain' else 'result.tif')
            values=decode(path); sig[name]=hashlib.sha256(values.astype('<f8').tobytes()).hexdigest()
        signatures.append(sig)
        repeat=run('cached'+str(index),data,'cache'+str(index));assert runs[-1]['cache_hits']==len(steps)
        for name in sig:
            filename='slope.tif' if name=='terrain' else 'result.tif'
            np.testing.assert_equal(decode(folder/name/filename),decode(repeat/name/filename))
    assert signatures[0]==signatures[1]
    assert digest(original)==before and hashes=={f.name:digest(f) for f in inputs.iterdir()}
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap,BoundaryNorm
    folder=root/'run1/default'
    fig,axes=plt.subplots(2,3,figsize=(14,9),layout='constrained')
    panels=[(damaged,'Diagnostic missing DEM (m)','terrain',None),
            (decode(folder/'repair/modified.tif'),'Interpolation mask (1 = repaired)','binary',[0,1]),
            (decode(folder/'classes/result.tif'),'Derived elevation classes','viridis',list(range(5))),
            (decode(folder/'change/change.tif'),'Policy difference: 0 same / 1 changed','coolwarm',[0,1]),
            (decode(folder/'instances/result.tif'),'Connected instance IDs (8-neighbour)','tab20',None),
            (decode(folder/'terrain/slope.tif'),'Original DEM Horn slope (degrees)','magma',None)]
    for ax,(values,title,cmap,ticks) in zip(axes.flat,panels):
        norm=None
        if ticks is not None:
            cmap=ListedColormap(['#eeeeee','#111111']) if 'Interpolation' in title else ListedColormap(['#c9dceb','#c34c36']) if 'Policy' in title else plt.get_cmap(cmap,len(ticks))
            norm=BoundaryNorm(np.arange(min(ticks)-.5,max(ticks)+1.5),len(ticks))
        im=ax.imshow(values,extent=(left,right,bottom,top),cmap=cmap,norm=norm,interpolation='nearest')
        fig.colorbar(im,ax=ax,ticks=ticks,shrink=.8);ax.set_title(title);ax.set_xlabel('Easting (m)');ax.set_ylabel('Northing (m)')
        if 'classes' in title:
            gpd.read_file(inputs/'zones.gpkg').boundary.plot(ax=ax,color='black',linewidth=.6)
    fig.suptitle('Real elevation / diagnostic policies — no dated change or ground-truth claim\nWhite = unknown; source preserved; one interpolated cell, protected and large holes retained')
    fig.savefig(root/'qa.png',dpi=160);plt.close(fig)
    usage={k:resource.getrusage(w).ru_maxrss*(1 if sys.platform=='darwin' else 1024) for k,w in [('self_max_rss_bytes',resource.RUSAGE_SELF),('completed_child_max_rss_bytes',resource.RUSAGE_CHILDREN)]}
    write_json(root/'acceptance.json',dict(policy=policy,runs=runs,decoded_hashes=signatures,source_unchanged=True,wall_seconds=time.perf_counter()-started,resources=usage,resource_scope='Separate lifetime high-water marks, not summed process-tree peak; no speedup benchmark',visual_review='pending actual image inspection'))
    # Cache is reproducible working state, not final evidence; retain only published outputs.
    for index in (0,1):shutil.rmtree(root/('cache'+str(index)))


if __name__=='__main__':main()
