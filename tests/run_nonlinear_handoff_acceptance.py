#!/usr/bin/env python3
"""Repeat frozen printed-grid TPS and hand off diagnostic grid segments (no ground truth)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import geopandas as gpd
import numpy as np
from PIL import Image
import rasterio as rio
from shapely.geometry import LineString
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
from _delivery import digest,write_json

def main(old,out,backend):
    out.mkdir();src=out/'source';src.mkdir()
    for name,oldpath in [('source.png',old/'nonlinear-evidence/source.png'),('gcps.json',old/'nonlinear-evidence/gcps.json'),('policy.json',old/'nonlinear-policy.json'),('policy-erratum.md',old/'nonlinear-policy-erratum.md'),('provenance.json',old/'nonlinear-evidence/provenance.json'),('predeclared-policy.md',old/'nonlinear-evidence/predeclared-policy.md')]:shutil.copyfile(oldpath,src/name)
    data=json.loads((src/'gcps.json').read_text(encoding='utf-8'));points=[x for x in data['points'] if x['role']=='fit']
    rows=[]
    for axis in (0,1):
        for value in sorted(set(x['world'][axis] for x in points)):
            group=sorted([x for x in points if x['world'][axis]==value],key=lambda x:x['world'][1-axis])
            if len(group)>1:rows.append(dict(id=f'grid-{axis}-{value}',source_point_ids=','.join(x['id'] for x in group),geometry=LineString([x['pixel'] for x in group])))
    frame=gpd.GeoDataFrame(rows);frame.to_file(src/'pixels.gpkg',layer='printed_grid',driver='GPKG')
    write_json(src/'pixels.json',dict(source_sha256=digest(src/'source.png'),gpkg_sha256=digest(src/'pixels.gpkg'),coordinate_system='UNREFERENCED PIXEL ENGINEERING SPACE; y down; units pixels',sampling_tolerance_pixels=.25,issues=[],omitted_faces=[],scope='Diagnostic straight segments connecting the original frozen printed-grid observations. No newly interpreted curve, historical object or surveyed feature.',provenance='gcps.json, unchanged original points'))
    hashes={x.name:digest(x) for x in src.iterdir()};records=[];commands=[]
    for i in range(2):
        command=[sys.executable,str(S/'nonlinear-georef.py'),str(src/'source.png'),str(src/'gcps.json'),'--params',str(src/'policy.json'),'--backend-gdalwarp',backend,'--pixel-vectors',str(src/'pixels.gpkg'),'--pixel-manifest',str(src/'pixels.json'),'--vector-step-px','1','--output',str(out/f'run{i}')]
        start=time.perf_counter();r=subprocess.run(command,capture_output=True,encoding='utf-8', errors='replace')
        (out/f'run{i}.log').write_text(r.stdout+r.stderr, encoding='utf-8')
        if r.returncode:raise RuntimeError(f'TPS CLI failed: run{i}.log')
        commands.append(dict(command=command,wall_seconds=time.perf_counter()-start));records.append(json.loads((out/f'run{i}/record.json').read_text(encoding='utf-8')))
    raster_hashes=[];vector_hashes=[]
    for i in range(2):
        with rio.open(out/f'run{i}/warped.tif') as d:
            raster_hashes.append(hashlib.sha256(d.read().tobytes()).hexdigest());assert d.crs==rio.crs.CRS.from_epsg(26713)
        g=gpd.read_file(out/f'run{i}/vectors.gpkg');vector_hashes.append(hashlib.sha256(b''.join(g.geometry.to_wkb())+g.drop(columns='geometry').to_json().encode()).hexdigest());assert len(g)==len(frame)
    assert raster_hashes[0]==raster_hashes[1] and vector_hashes[0]==vector_hashes[1]
    assert hashes=={x.name:digest(x) for x in src.iterdir()}
    assert all(r['status']=='pass' and r['preferred_model_by_frozen_check_rmse']=='affine' and not r['automatic_promotion'] for r in records)
    fig,axes=plt.subplots(1,2,figsize=(13,8));axes[0].imshow(Image.open(src/'source.png'));frame.plot(ax=axes[0],color='cyan',linewidth=.8)
    axes[0].set_title('Frozen printed-grid observations / source pixels')
    with rio.open(out/'run0/warped.tif') as d:
        a=d.read(out_shape=(4,1200,900));b=d.bounds;axes[1].imshow(a.transpose(1,2,0),extent=[b.left,b.right,b.bottom,b.top])
    g.plot(ax=axes[1],color='cyan',linewidth=.8);axes[1].set_title('TPS sampled grid segments / EPSG:26713')
    for ax in axes:ax.set_aspect('equal')
    fig.suptitle('Printed-grid diagnostic only; affine preferred; no ground-position validation');fig.tight_layout();fig.savefig(out/'qa.png',dpi=150);plt.close(fig)
    write_json(out/'acceptance.json',dict(status='printed-grid candidate pass; affine preferred; no historical promotion',source_hashes=hashes,commands=commands,decoded_raster_hashes=raster_hashes,normalized_vector_hashes=vector_hashes,check=records[0]['check'],affine_check=records[0]['affine_check'],implementation_sha256=digest(S/'nonlinear-georef.py'),visual_review='pending',limits=['Straight diagnostic grid links; native cubic behavior covered only by synthetic permanent regression','Source curve tolerance and segment length do not certify world-space curve approximation','No surveyed ground truth; C full-sheet hold unchanged']))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--backend',required=True);a=p.parse_args();main(a.evidence,a.output,a.backend)
