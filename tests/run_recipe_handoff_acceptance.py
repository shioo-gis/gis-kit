#!/usr/bin/env python3
"""Two-run real CLI/file-chain C acceptance; synthetic data is labeled explicitly."""
import copy
import hashlib
import json
import os
import resource
from pathlib import Path
import sys
import time

import geopandas as gpd
from PIL import Image
import shapely
from test_recipe_handoff import cli, recipe_fixture, atlas_fixture, network_fixture, georef_fixture, write_json, digest, A


def pixels(path):
    with Image.open(path) as im:
        return hashlib.sha256(im.convert('RGBA').tobytes()).hexdigest()


def canonical(path):
    data=gpd.read_file(path)
    return hashlib.sha256(json.dumps([(list(row.drop(data.geometry.name)),shapely.normalize(row.geometry).wkb_hex) for _,row in data.iterrows()],sort_keys=True).encode()).hexdigest()


def execute(root):
    repo=Path(__file__).resolve().parents[1]
    if root==repo or repo in root.parents:raise ValueError('Acceptance directory must be outside repository')
    root.mkdir(parents=True,exist_ok=False);start=time.perf_counter();results=[]
    for number in range(2):
        work=root/f'run{number}';work.mkdir()
        source,recipe=recipe_fixture(work)
        recipe['variants']=[{'id':'fine'},{'id':'coarse','params':{'grid':{'size_m':10}}}]
        write_json(work/'recipe.json',recipe)
        first=cli('recipe.py',work/'recipe.json','--cache',work/'cache','--output',work/'recipe-first')
        second=cli('recipe.py',work/'recipe.json','--cache',work/'cache','--output',work/'recipe-cached')
        assert json.loads(second.stdout)['cache_hits']==4
        bad=copy.deepcopy(recipe);bad['steps'][1]['validate']={'min_rows':99999};write_json(work/'recipe-bad.json',bad)
        cli('recipe.py',work/'recipe-bad.json','--cache',work/'resume-cache','--output',work/'failed',ok=False);assert not (work/'failed').exists()
        resumed=cli('recipe.py',work/'recipe.json','--cache',work/'resume-cache','--output',work/'recipe-resumed')
        assert json.loads(resumed.stdout)['cache_hits']>=1
        for variant in ('fine','coarse'):
            assert canonical(work/f'recipe-first/{variant}/grid/result.gpkg')==canonical(work/f'recipe-resumed/{variant}/grid/result.gpkg')
        # Content substitution and parameter variants are run via the public CLI.
        replacement=work/'replacement.gpkg';gpd.GeoDataFrame({'id':['new']},geometry=[shapely.box(0,0,30,10)],crs=32631).to_file(replacement)
        write_json(work/'bindings.json',{'study':str(replacement)})
        changed=cli('recipe.py',work/'recipe.json','--bindings',work/'bindings.json','--cache',work/'cache','--output',work/'recipe-replaced')
        assert json.loads(changed.stdout)['cache_hits']==0
        objects=atlas_fixture(work);original=digest(objects)
        cli('atlas.py',objects,'--params',work/'atlas.json','--output',work/'atlas')
        atlas_record=json.loads((work/'atlas/record.json').read_text(encoding='utf-8'));assert len(atlas_record['objects'])==3
        assert sum(o['status']=='omitted' for o in atlas_record['objects'])==1
        assert digest(objects)==original
        run,state,p=network_fixture(work)
        cli('candidates.py',run,'--confirmed',work/'confirmed.json','--output',work/'candidates')
        report=json.loads((work/'candidates/candidates.json').read_text(encoding='utf-8'));assert len(report['candidates'])==5 and report['status']=='hold'
        overview=A.view(run,max_size=1000,overlay=True,labels=True)
        (work/'annotation-view.json').write_text(json.dumps(overview,indent=2), encoding='utf-8')
        geo=georef_fixture(work)
        cli('georef-delivery.py','package',work/'package.json','--output',work/'spatial')
        # A separately fitted transform uses identical check points, not a modified report.
        gcps=json.loads((work/'main-gcps.json').read_text(encoding='utf-8'))
        for point in gcps['points']:
            if point['role']=='fit':point['world'][0]+=3;point['world'][1]+=4
        write_json(work/'shifted-gcps.json',gcps)
        cli('raster-georef.py','fit',work/'shifted-gcps.json',work/'shifted.json')
        cli('georef-delivery.py','compare',work/'main.json',work/'shifted.json','--output',work/'displacement')
        comparison=json.loads((work/'displacement/comparison.json').read_text(encoding='utf-8'))
        assert abs(comparison['maximum_frame_displacement']-5)<1e-7 and comparison['acceptance_after']['status']=='hold'
        maps={p.name:pixels(p) for p in (work/'atlas').glob('*.png')}
        results.append({'grid':canonical(work/'recipe-first/fine/grid/result.gpkg'),'maps_rgba':maps,'candidate_geometry':[c['geometry'] for c in report['candidates']],'displacement':comparison['maximum_frame_displacement']})
    assert results[0]==results[1]
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import rasterio
    fig,axes=plt.subplots(1,3,figsize=(12,4))
    for ax,name,title in zip(axes,['pixels.png','spatial/0001/raster.tif','spatial/0002/raster.tif'],['Source pixels','Main frame (masked outside)','Inset frame (masked outside)']):
        with rasterio.open(root/'run0'/name) as raster:
            ax.imshow(raster.read(1,masked=True),cmap='gray',vmin=0,vmax=255,interpolation='nearest')
        ax.set_title(title);ax.set_xlabel('source pixel x');ax.set_ylabel('source pixel y')
    fig.tight_layout();fig.savefig(root/'raster-qa.png',dpi=140);plt.close(fig)
    write_json(root/'acceptance.json',{'status':'passed','data':'synthetic file-backed fixtures; not real historical sheet acceptance','runs':results,'elapsed_seconds':time.perf_counter()-start,'resource':{'self_maxrss':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'children_maxrss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,'units':'bytes on macOS; KiB on Linux','scope':'separate process maxima, not summed process-tree peak'},'checks':['cache hit','input replacement','parameter variants','failure no publication','resume equals fresh','all IDs accounted','holes and inset candidates','native cubic preservation','two-frame GeoTIFF masks and GPKG readback','fixed-check affine displacement','decoded atlas RGBA equality']})
    print(json.dumps({'output':str(root),'status':'passed','seconds':time.perf_counter()-start}))

if __name__=='__main__':execute(Path(sys.argv[1]).resolve())
