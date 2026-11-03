#!/usr/bin/env python3
"""Fixed print-scale PNG with actual label-box audit, using the existing plot backend."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
import geopandas as gpd
from PIL import Image
from shapely.ops import orient
from _cartography import prepare,positive
from _delivery import bundle,digest,write_json
from _safe_io import iter_vector_chunks
from _delivery import fingerprint
from _budget import check_vector


def execute(source,params,output):
    p=json.loads(Path(params).read_text(encoding='utf-8'));before=fingerprint(source)
    check_vector(source,p.get('layer'));data=gpd.read_file(source,layer=p.get('layer'),engine='pyogrio');work,factor=prepare(data,p)
    if p.get('label',p['id']) not in work:raise ValueError('Unknown label field')
    if p.get('label_collision_policy','report') not in ('report','omit'):raise ValueError('Unknown label collision policy')
    if int(p.get('max_labels',100000))!=p.get('max_labels',100000) or p.get('max_labels',100000)<0:raise ValueError('Invalid max_labels')
    scale=positive(p,'scale');width=positive(p,'map_width_mm');height=positive(p,'map_height_mm');dpi=positive(p,'dpi')
    if width>1000 or height>1000 or dpi>600:raise ValueError('Map exceeds bounded page dimensions')
    spanx=width/1000*scale/factor;spany=height/1000*scale/factor
    bounds=work.total_bounds;cx,cy=p.get('center_xy',[(bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2])
    if not math.isfinite(cx) or not math.isfinite(cy):raise ValueError('Invalid center')
    extent=[cx-spanx/2,cy-spany/2,cx+spanx/2,cy+spany/2]
    omitted=[str(r[p['id']]) for _,r in work.iterrows() if not __import__('shapely').box(*extent).covers(r.geometry)]
    if omitted and not p.get('allow_clipping',False):raise ValueError('Target print scale clips source objects')
    spec=importlib.util.spec_from_file_location('cartographic_plot',Path(__file__).with_name('plot.py'));plot=importlib.util.module_from_spec(spec);spec.loader.exec_module(plot)
    with bundle(output) as out:
        work.geometry=work.geometry.map(orient)
        work.to_file(out/'objects.gpkg',driver='GPKG',engine='pyogrio')
        count=0
        for start,check in iter_vector_chunks(out/'objects.gpkg'):
            if not all(a.equals(b) for a,b in zip(work.geometry.iloc[start:start+len(check)],check.geometry)):raise ValueError('Map vector readback differs')
            count+=len(check)
        if count!=len(work):raise ValueError('Map vector readback differs')
        # 10 mm margins; explicit axes bypass tight bounding box and preserve physical scale.
        pagew,pageh=width+20,height+20
        template={'layers':{'province':{'facecolor':'#c8d8e4','edgecolor':'#364757','color_field':p.get('color_field'),'label':{'enabled':True,'fontsize':p.get('font_size',8)}}},
                  'layout':{'label_max_count':int(p.get('max_labels',100000)),'label_collision_policy':p.get('label_collision_policy','report'),'bounds':extent,'fixed_map_axes':[10/pagew,10/pageh,width/pagew,height/pageh]}}
        args=SimpleNamespace(figsize=f'{pagew/25.4},{pageh/25.4}',label_field=p.get('label',p['id']),highlight=None,title=p.get('title',f'1:{scale:g} | derived geometry'),output=str(out/'map.png'),dpi=dpi)
        audit=plot.plot_location([(str(out/'objects.gpkg'),None)],template,args)
        actual=spanx*factor/(audit['map_width_inches']*.0254)
        if not math.isclose(actual,scale,rel_tol=1e-9):raise ValueError('Rendered scale differs')
        with Image.open(out/'map.png') as image:image.load();size=list(image.size)
        write_json(out/'record.json',{'source_sha256':before,'parameters':p,'scale_denominator':actual,'print_rule':'print PNG at embedded DPI, no fit-to-page; projected linear scale only','label_audit':audit,'clipped_ids':omitted,'status':'hold' if omitted or audit['overlaps'] or any(not x['inside_map'] for x in audit['labels']) else 'complete','visual_review':'required separately','image_size':size,'map_sha256':digest(out/'map.png')})
        (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Cartographic candidate</title><p>Derived geometry. Display colors are not semantic classes. Print at embedded DPI; no fit-to-page.</p><img src="map.png" alt="Candidate map"><p>Actual label-box audit and source identity: <a href="record.json">record.json</a></p>',encoding='utf-8')
        if fingerprint(source)!=before:raise ValueError('Source changed')
    return {'output':str(output),'scale':actual}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('--params',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:print(json.dumps(execute(a.input,a.params,a.output)))
    except (ValueError,KeyError,TypeError,OSError) as e:p.exit(1,f'ERROR: {e}\n')
if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
