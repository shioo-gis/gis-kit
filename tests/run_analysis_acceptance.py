#!/usr/bin/env python3
"""Rerunnable analysis-extension actual CLI/GeoTIFF/GPKG chain, output outside repository."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import Point, box
from PIL import Image
from test_analysis_extensions import tif, rcli, ledger_fixture, coverage
from test_daily import frame, write, run, cli, SCRIPTS


def main(destination):
    root=Path(destination).resolve()
    repo=Path(__file__).resolve().parents[1]
    if root==repo or repo in root.parents: raise ValueError('Acceptance output must be outside repository')
    root.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter();summaries=[];canonical=[]
    for iteration in range(2):
        work=root/f'run{iteration}';work.mkdir()
        source=tif(work/'source.tif',[[1,2,3,4],[5,-99,7,8],[9,10,11,12],[13,14,15,16]],from_origin(500000,4400004,1,1))
        zones=write(work,'zones',frame([box(500000,4400000,500002,4400004),box(500002,4400000,500004,4400004)],id=['west','east']))
        points=write(work,'points',frame([Point(500000.5,4400000.5),Point(500002,4400001),Point(500003.5,4400003.5)],id=['p1','p2','p3'],value=[1.,2.,3.]))
        mask=write(work,'mask',frame([box(500000,4400000,500003,4400004)],id=['mask']))
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,zones,points)}
        rcli(work,'inspect','inspect',[source],{})
        rcli(work,'clip','clip',[source],{'vector':str(mask),'block_size':2})
        rcli(work,'scaled','calculate',[work/'clip/result.tif'],{'scale':2,'offset':1,'block_size':2})
        stats=rcli(work,'zonal','zonal',[work/'scaled/result.tif'],{'vector':str(zones),'id':'id','method':'fractional','block_size':2,'histogram':True})
        # West raw sum 54 => 115; clipped east column sum 36 => 76.
        assert [x['sum'] for x in stats['zones']]==[115,76]
        rcli(work,'cog','copy',[work/'scaled/result.tif'],{'format':'COG','block_size':2})
        sample=rcli(work,'sample','sample',[source],{'vector':str(points),'id':'id'})
        assert [x['value'] for x in sample['samples']]==[13,15,4]
        grid_artifact,_=run(work,'grid','grid',zones,{'method':'square','size_m':2,'origin':[500000,4400000]})
        summary_artifact,_=run(work,'grid_summary','grid_summary',points,{'id':'id','right_id':'cell_id'},grid_artifact)
        assert gpd.read_file(summary_artifact).point_count.sum()==3
        run(work,'distribution','distribution',points,{'value':'value','study_area':'declared four-metre square'})
        run(work,'cluster','cluster',points,{'id':'id','eps_m':2,'min_samples':2,'study_area':'declared four-metre square'})
        run(work,'profile','profile',zones,{'required_fields':['valid_from'],'conditions':['historical validity unchecked']})
        new=write(work,'new',frame([box(500000,4400000,500001,4400004),box(500001,4400000,500002,4400004),box(500002,4400000,500004,4400004)],id=['w1','w2','east']))
        _,diff=run(work,'compare','compare',zones,{'id':'id'},new)
        assert len(diff['details']['candidates'])==2
        assert all(x['proposal']=='split' and x['status']=='unknown' for x in diff['details']['candidates'])
        annotation,ledger=ledger_fixture(work)
        ledger_path=work/'coverage-ledger.json';ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        cli('coverage.py',annotation,'--ledger',ledger_path,'--output',work/'coverage-hold.json')
        assert json.loads((work/'coverage-hold.json').read_text(encoding='utf-8'))['status']=='hold'
        ledger['observations'][0].update(review='accepted',reviewer='synthetic test fixture, not a production review')
        ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        cli('coverage.py',annotation,'--ledger',ledger_path,'--output',work/'coverage-reviewed.json')
        assert json.loads((work/'coverage-reviewed.json').read_text(encoding='utf-8'))['status']=='complete'
        # Existing production plotting CLI, no new renderer.
        cli('plot.py','--inputs',summary_artifact,'--output',work/'grid.png','--title','Fixed grid: stable cell IDs','--label-field','cell_id','--dpi','120')
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,zones,points)}
        assert before==after
        with rio.open(work/'cog/result.tif') as ds:
            pixels=ds.read(1);assert np.isnan(pixels[1,1]);assert pixels[0,0]==3 and np.isnan(pixels[3,3]) and pixels[3,2]==31
            canonical.append(hashlib.sha256(pixels.astype('<f8').tobytes()).hexdigest())
        summaries.append({'inputs_unchanged':True,'zonal_sums':[x['sum'] for x in stats['zones']],'decoded_raster_sha256':canonical[-1]})
    assert canonical[0]==canonical[1]
    # Diagnostic figure is acceptance evidence, not another GIS production renderer.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    def preview(path):
        fig,axes=plt.subplots(1,3,figsize=(12,4))
        for ax,name in zip(axes,['source.tif','clip/result.tif','cog/result.tif']):
            with rio.open(root/'run0'/name) as ds: data=ds.read(1,masked=True)
            im=ax.imshow(data,cmap='viridis',interpolation='nearest')
            for r,c in np.ndindex(data.shape): ax.text(c,r,'ND' if np.ma.is_masked(data[r,c]) else f'{data[r,c]:g}',ha='center',va='center',color='black' if np.ma.is_masked(data[r,c]) else 'white',fontsize=11)
            ax.set_title(name); ax.set_xticks(range(4));ax.set_yticks(range(4));fig.colorbar(im,ax=ax,shrink=.7)
        fig.suptitle('Known pixels / retained grid mask / COG: west sum 115, east sum 76')
        fig.tight_layout();fig.savefig(path,dpi=150);plt.close(fig)
    preview(root/'qa-1.png');preview(root/'qa-2.png')
    decoded=[]
    for name in ('qa-1.png','qa-2.png'):
        with Image.open(root/name) as image: decoded.append(hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest())
    assert decoded[0]==decoded[1]
    (root/'acceptance.json').write_text(json.dumps({'runs':summaries,'elapsed_seconds':time.perf_counter()-started,'performance_claim':False,
        'preview_rgba_sha256':decoded,'visual_review':'requires actual inspection','data_scope':'hand-calculated synthetic inputs written as real GeoTIFF/GPKG, no production data supplied'},indent=2), encoding='utf-8')
    print(str(root))

if __name__=='__main__': main(sys.argv[1])
