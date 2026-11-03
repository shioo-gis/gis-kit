#!/usr/bin/env python3
"""Real DEM CLI chain; parameters frozen before calculation, no route-driven point choice."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import numpy as np
import geopandas as gpd
import rasterio as rio
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
from _delivery import digest,write_json
from terrain import derivatives


def decoded(path):
    with rio.open(path) as ds:
        a=ds.read();a[np.isnan(a)]=np.nan
        return hashlib.sha256(a.tobytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('dem');p.add_argument('output');p.add_argument('--grass',required=True);p.add_argument('--source-description',required=True);v=p.parse_args()
    out=Path(v.output);out.mkdir(parents=True,exist_ok=False);inputs=out/'inputs';inputs.mkdir();started=time.perf_counter()
    source=Path(v.dem).resolve();before=digest(source)
    with rio.open(source) as ds:
        z=ds.read(1,masked=True).filled(np.nan);t=ds.transform;crs=ds.crs;profile=ds.profile.copy()
        start=list(ds.xy(ds.height//4,ds.width//4));end=list(ds.xy(3*ds.height//4,3*ds.width//4));bounds=ds.bounds
    slope=derivatives(z,t.a,-t.e)['slope']
    common=dict(vertical_unit='metre',vertical_datum='source declaration; no ground survey validation',source_description=v.source_description,start=start,end=end,walk_coeff=[.72,6,1.9998,-1.9998],slope_factor=-.2125,friction_lambda=1,corridor_extra_seconds=1800,cost_assumptions='Terrain-only demonstration; endpoints fixed at grid quartiles before routing. Eight-neighbour point-cell graph, diagonal corner contact allowed. No observed trail, hazard or safety validation.')
    policy=dict(source=str(source),source_sha256=before,endpoint_rule='row/col floor(shape/4), floor(3*shape/4), pixel centres; frozen before any route',scenarios={'uniform':'0.05 seconds/metre added everywhere; no declared obstacles','slope_barrier':'0.05+0.02*slope_degrees s/m; slope >=40 degrees or unavailable 3x3 slope is an explicitly assumed barrier'},corridor_extra_seconds=1800,numeric_tolerance=dict(absolute_seconds=1e-7,relative=1e-10))
    write_json(inputs/'frozen-policy.json',policy)
    for name in policy['scenarios']:
        f=np.full(z.shape,.05) if name=='uniform' else np.where(np.isfinite(slope)&(slope<40),.05+.02*slope,np.nan)
        profile.update(count=1,dtype='float64',nodata=np.nan,compress='deflate')
        with rio.open(inputs/f'{name}.tif','w',**profile) as ds:ds.write(f,1);ds.set_band_unit(1,'s/m')
        write_json(inputs/f'{name}.json',dict(common,cost_assumptions=common['cost_assumptions']+' '+policy['scenarios'][name]))
    records={};signatures={}
    for repeat in range(2):
        for name in policy['scenarios']:
            dest=out/f'run{repeat}'/name
            command=[sys.executable,str(S/'terrain-cost.py'),str(source),'--friction',str(inputs/f'{name}.tif'),'--params',str(inputs/f'{name}.json'),'--grass',v.grass,'--output',str(dest)]
            result=subprocess.run(command,capture_output=True,encoding='utf-8', errors='replace',env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
            if result.returncode:raise RuntimeError(f'CLI failed: {result.stderr}')
            record=json.loads((dest/'record.json').read_text(encoding='utf-8'));records[f'{repeat}/{name}']=record
            routes=gpd.read_file(dest/'routes.gpkg')
            signature=dict(rasters={f.name:decoded(f) for f in dest.glob('*.tif')},routes=[dict(wkb=g.wkb_hex,cost=float(c)) for g,c in zip(routes.geometry,routes.cost_seconds)])
            if repeat:assert signatures[name]==signature
            else:signatures[name]=signature
    assert before==digest(source)
    fig,axes=plt.subplots(2,3,figsize=(15,10));extent=(bounds.left,bounds.right,bounds.bottom,bounds.top)
    for i,name in enumerate(policy['scenarios']):
        dest=out/'run0'/name;record=records[f'0/{name}'];route=gpd.read_file(dest/'routes.gpkg')
        for j,(file,title) in enumerate([('slope','Horn slope (degrees)'),('from_start','Directed travel time (seconds)'),('excess_seconds','Through-cell excess (seconds)')]):
            with rio.open(dest/f'{file}.tif') as ds:a=ds.read(1,masked=True)
            ax=axes[i,j];im=ax.imshow(a,extent=extent,origin='upper',cmap='terrain' if j==0 else 'viridis');fig.colorbar(im,ax=ax,shrink=.7)
            with rio.open(dest/'friction.tif') as ds:barrier=~np.isfinite(ds.read(1))
            ax.imshow(np.ma.masked_where(~barrier,np.ones_like(barrier,dtype=float)),extent=extent,origin='upper',cmap='gray',vmin=0,vmax=3,alpha=.8)
            if not route.empty:route.plot(ax=ax,color='red',linewidth=1.2)
            with rio.open(dest/'corridor.tif') as ds:c=ds.read(1)
            if np.any(c==1):
                ax.imshow(np.ma.masked_where(c!=1,np.ones_like(c)),extent=extent,origin='upper',cmap='autumn',vmin=0,vmax=1,alpha=.25)
            ax.scatter(*start,c='white',edgecolors='black',marker='o',s=35);ax.scatter(*end,c='white',edgecolors='black',marker='s',s=35)
            ax.set_title(f'{name}: {title}\nreachable={record["reachable"]}, corridor <=1800s excess');ax.ticklabel_format(style='plain',useOffset=False)
    fig.suptitle(f'Real DSM / hypothetical walking scenarios / {crs.to_string()} (metres)\nNo trail, safety or ground-truth claim; costs use signed edge slope, not Horn slope.');fig.legend(handles=[Line2D([],[],color='red',label='Candidate path'),Patch(color='orange',alpha=.4,label='Excess <=1800 s corridor'),Patch(color='#555555',label='Assumed barrier'),Patch(facecolor='white',edgecolor='black',label='No value / unreachable'),Line2D([],[],marker='o',color='black',linestyle='',label='Start'),Line2D([],[],marker='s',color='black',linestyle='',label='End')],loc='lower center',ncol=6,fontsize=9);fig.tight_layout(rect=[0,.05,1,.95]);fig.savefig(out/'qa.png',dpi=150);plt.close(fig)
    write_json(out/'acceptance.json',dict(status='pass',scope='Real terrain engineering chain only, not route or safety validation',source_sha256=before,parameters_sha256={f.name:digest(f) for f in inputs.glob('*.json')},decoded_and_vector_repeat=signatures,scenarios={name:{k:records[f'0/{name}'][k] for k in ('reachable','cost_seconds','path_recomputed_seconds')} for name in policy['scenarios']},resources={key:rec['resources'] for key,rec in records.items()},wall_seconds=time.perf_counter()-started))


if __name__=='__main__':main()
