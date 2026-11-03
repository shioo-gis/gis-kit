#!/usr/bin/env python3
"""Real DEM CLI acceptance; independent vectorized stencil and repeated decoded outputs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import hashlib

import numpy as np
import rasterio as rio
from pyproj import CRS
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from _delivery import digest, write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source'); p.add_argument('output'); p.add_argument('--description',required=True)
    p.add_argument('--vertical-datum',required=True)
    args = p.parse_args(); out=Path(args.output); out.mkdir(parents=True, exist_ok=False)
    source=Path(args.source); before=digest(source)
    params=dict(vertical_unit='metre',vertical_datum=args.vertical_datum,source_description=args.description)
    write_json(out/'params.json',params)
    with rio.open(source) as d:
        z=d.read(1,masked=True).astype(float).filled(np.nan); t=d.transform; crs=d.crs
        unit=CRS(crs).axis_info[0].unit_conversion_factor
        dx,dy=t.a*unit,-t.e*unit
    # Independent direct Horn stencil, no ndimage calls.
    w=np.lib.stride_tricks.sliding_window_view(z,(3,3)); valid=np.isfinite(w).all(axis=(-2,-1))
    east=(w[:,:,0,2]+2*w[:,:,1,2]+w[:,:,2,2]-w[:,:,0,0]-2*w[:,:,1,0]-w[:,:,2,0])/(8*dx)
    north=(w[:,:,0,0]+2*w[:,:,0,1]+w[:,:,0,2]-w[:,:,2,0]-2*w[:,:,2,1]-w[:,:,2,2])/(8*dy)
    expected=dict(slope=np.degrees(np.arctan(np.hypot(east,north))),aspect=np.mod(np.degrees(np.arctan2(-east,-north)),360),relief=w.max(axis=(-2,-1))-w.min(axis=(-2,-1)))
    expected['aspect'][(east==0)&(north==0)]=np.nan
    hashes=[]; times=[]; records=[]
    for i in range(3):
        started=time.perf_counter()
        result=subprocess.run([sys.executable,str(SCRIPTS/'terrain.py'),str(source),'--params',str(out/'params.json'),'--output',str(out/f'run{i}')],capture_output=True,text=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        (out/f'cli{i}.log').write_text(result.stdout+result.stderr, encoding='utf-8'); result.check_returncode(); times.append(time.perf_counter()-started)
        h={}; arrays={}
        for name, exp in expected.items():
            exp[~valid]=np.nan
            with rio.open(out/f'run{i}/{name}.tif') as d:
                a=d.read(1); assert d.transform==t and d.crs==crs
            np.testing.assert_allclose(a[1:-1,1:-1],exp,rtol=0,atol=1e-9,equal_nan=True)
            assert np.isnan(a[[0,-1],:]).all() and np.isnan(a[:,[0,-1]]).all()
            h[name]=hashlib.sha256(a.astype('<f8').tobytes()).hexdigest(); arrays[name]=a
        hashes.append(h); records.append(json.loads((out/f'run{i}/record.json').read_text(encoding='utf-8')))
    assert hashes[0]==hashes[1]==hashes[2] and before==digest(source)
    fig,axes=plt.subplots(2,2,figsize=(12,10),layout='constrained')
    extent=[t.c/1000,(t.c+z.shape[1]*t.a)/1000,(t.f+z.shape[0]*t.e)/1000,t.f/1000]
    for ax,(name,a,cmap,unit) in zip(axes.flat,[('Elevation',z,'terrain','m'),('Slope',arrays['slope'],'magma','degrees'),('Aspect',arrays['aspect'],'twilight','degrees CW from grid north'),('3x3 relief',arrays['relief'],'viridis','m')]):
        kw=dict(vmin=0,vmax=360) if name=='Aspect' else {}
        im=ax.imshow(a,extent=extent,cmap=cmap,origin='upper',**kw); ax.set_title(name); ax.set_xlabel('Easting (km)'); ax.set_ylabel('Northing (km)'); fig.colorbar(im,ax=ax,label=unit,shrink=.8)
    fig.suptitle(f'DEM diagnostics — {crs}\nPre-event DSM; no flow / hazard reconstruction. White = NoData.')
    fig.savefig(out/'qa.png',dpi=150); plt.close(fig)
    write_json(out/'acceptance.json',dict(source_sha256=before,source_unchanged=True,decoded_hashes=hashes,reference_atol=1e-9,cli_wall_seconds=times,median_seconds=float(np.median(times)),process_resources=[r['resources'] for r in records],visual_review='pending',performance_claim='No old/new comparison; no acceleration claim'))
    print(out)


if __name__=='__main__':main()
