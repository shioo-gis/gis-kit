#!/usr/bin/env python3
"""Replay existing professional inputs via fixed recipe adapters, never acquire data."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio as rio

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(SCRIPTS))
from _delivery import digest,write_json


def ref(name):return {'input_ref':name}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','dem','output'):p.add_argument(name)
    for name in ('grass','pysal-python','pysal-site','pysal-proj'):p.add_argument('--'+name,required=True)
    a=p.parse_args();root=Path(a.output).resolve()
    if SCRIPTS.parents[1] in root.parents:raise ValueError('Repository-external output required')
    root.mkdir(parents=True,exist_ok=False);source=Path(a.inputs);inputs=root/'inputs';inputs.mkdir()
    identities={}
    for name in ('edges.gpkg','origins.gpkg','facilities.gpkg','sites.gpkg','cities.gpkg','network.json','suitability.json','kde.json','infer.json'):
        identities[str(source/name)]=digest(source/name);shutil.copy2(source/name,inputs/name)
    identities[str(Path(a.dem))]=digest(a.dem);shutil.copy2(a.dem,inputs/'dem.tif')
    with rio.open(a.dem) as ds:
        profile=ds.profile.copy();z=ds.read(1,masked=True)
        if np.ma.getmaskarray(z).any():raise ValueError('Complete diagnostic DEM required')
        start=list(ds.xy(ds.height//4,ds.width//4));end=list(ds.xy(3*ds.height//4,3*ds.width//4))
        area=abs(ds.transform.determinant)
        profile.update(dtype='float64',nodata=np.nan,count=1)
        with rio.open(inputs/'friction.tif','w',**profile) as out:out.write(np.full(z.shape,.05),1)
    roles={n:str(inputs/(n+'.gpkg')) for n in ('edges','origins','facilities','sites','cities')}
    roles.update(dem=str(inputs/'dem.tif'),friction=str(inputs/'friction.tif'),grass=a.grass,pysal=a.pysal_python)
    steps=[]
    def step(ident,runner,files,params,artifact,operation='run',backend=None):
        s=dict(id=ident,runner=runner,operation=operation,files={k:ref(v) for k,v in files.items()},params=params,artifact=artifact,validate=dict(allow_candidate=True))
        if backend:s['backend']=backend
        steps.append(s)
    def params(n):return json.loads((inputs/(n+'.json')).read_text(encoding='utf-8'))
    step('network','network',dict(input='edges',origins='origins',facilities='facilities'),params('network'),'routes.gpkg')
    step('suitability','suitability',dict(input='sites'),params('suitability'),'scores.gpkg')
    step('kde','spatial-inference',dict(input='cities'),params('kde'),'density.tif')
    step('infer','spatial-inference',dict(input='cities',**{'backend-python':'pysal'}),params('infer'),'statistics.gpkg',backend={'backend-site-packages':a.pysal_site,'backend-proj-data':a.pysal_proj})
    common=dict(vertical_unit='metre',vertical_datum='unknown',source_description='Existing stored DEM; upstream provenance unverified; numerical diagnostic only')
    step('hydrology','terrain-backend',dict(input='dem',grass='grass'),dict(**common,flow_method='D8',threshold_cells=20),'accumulation.tif',operation='hydrology')
    step('cost','terrain-cost',dict(input='dem',friction='friction',grass='grass'),dict(**common,start=start,end=end,walk_coeff=[.72,6,1.9998,-1.9998],slope_factor=-.2125,friction_lambda=1,corridor_extra_seconds=1800,cost_assumptions='Synthetic uniform friction and quartile endpoints; no trail or safety observations'),'corridor.tif')
    recipe=dict(schema_version=1,inputs=roles,steps=steps);write_json(root/'recipe.json',recipe);runs=[]
    for index in (0,1):
        start_time=time.perf_counter()
        proc=subprocess.run([sys.executable,str(SCRIPTS/'recipe.py'),str(root/'recipe.json'),'--output',str(root/f'run{index}'),'--cache',str(root/'cache')],capture_output=True,encoding='utf-8', errors='replace',env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        (root/f'run{index}.log').write_text(proc.stdout+proc.stderr, encoding='utf-8')
        if proc.returncode:raise RuntimeError(proc.stderr)
        result=json.loads(proc.stdout);runs.append(dict(seconds=time.perf_counter()-start_time,**result))
        assert result['cache_hits']==(0 if index==0 else 3)
    hashes={}
    for step_info in steps:
        ident=step_info['id'];filename=step_info['artifact'];one=root/'run0/default'/ident/filename;two=root/'run1/default'/ident/filename
        if filename.endswith('.tif'):
            with rio.open(one) as x,rio.open(two) as y:
                assert x.transform==y.transform and x.crs==y.crs
                np.testing.assert_equal(x.read(),y.read())
        else:
            x=gpd.read_file(one);y=gpd.read_file(two)
            assert x.equals(y)
        hashes[ident]=dict(first=digest(one),second=digest(two),decoded_or_frame_equal=True)
    assert identities=={name:digest(name) for name in identities}
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,3,figsize=(14,9),layout='constrained');folder=root/'run1/default'
    for ax,ident,title in zip(axes.flat,['network','suitability','kde','infer','hydrology','cost'],['OSM directed route candidates','Baseline scenario scores','Catalogue KDE (records/km2)','Catalogue local Moran I (not significance)','D8 accumulation (boundary signs retained)','Directed cost corridor candidate']):
        info=next(s for s in steps if s['id']==ident);path=folder/ident/info['artifact']
        if path.suffix=='.tif':
            with rio.open(path) as ds:
                values=ds.read(1,masked=True);b=ds.bounds
                im=ax.imshow(values,extent=(b.left,b.right,b.bottom,b.top),interpolation='nearest',cmap='viridis');fig.colorbar(im,ax=ax,shrink=.8,ticks=[0,1] if ident=='cost' else None)
        else:
            data=gpd.read_file(path)
            if ident=='suitability':data=data[data.scenario=='baseline']
            column='score' if ident=='suitability' else 'local_I' if ident=='infer' else None
            data.plot(ax=ax,column=column,legend=bool(column),markersize=12)
        from matplotlib.ticker import MaxNLocator
        ax.xaxis.set_major_locator(MaxNLocator(4))
        ax.set_title(title,fontsize=11);ax.set_xlabel('Projected easting');ax.set_ylabel('Projected northing')
    fig.suptitle('Existing professional inputs through recipes — all outputs retain candidate status\nNo ground truth, present population, observed travel time or safety validation')
    fig.savefig(root/'qa.png',dpi=150);plt.close(fig)
    write_json(root/'acceptance.json',dict(runs=runs,comparison=hashes,source_unchanged=True,external_backend_cache='disabled for inference/GRASS, rerun both times',visual_review='pending',provenance='Reused prior D input files and policies; no new source authentication or acquisition'))
    shutil.rmtree(root/'cache')


if __name__=='__main__':main()
