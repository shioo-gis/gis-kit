#!/usr/bin/env python3
"""Object dossiers and a portable HTML/PNG atlas using the existing plot renderer."""
import argparse
import html
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
from PIL import Image
import shapely
from shapely.ops import orient
from _daily import stable, relations
from _metric import analysis_frame
from _delivery import bundle, digest, write_json
from _core import clean, environment
from _delivery import fingerprint
from _budget import check_vector


def execute(source, params, output):
    p=json.loads(Path(params).read_text(encoding='utf-8')); identity=p['id']
    if not set(p)<= {'schema_version','id','layer','analysis_crs','span_m','k','max_objects','group'} or p.get('schema_version',1)!=1:
        raise ValueError('Unknown atlas parameter or schema')
    original=fingerprint(source)
    check_vector(source,p.get('layer'));data=gpd.read_file(source,layer=p.get('layer'),engine='pyogrio').reset_index(drop=True)
    stable(data,identity)
    if len(data)>int(p.get('max_objects',200)): raise ValueError('Atlas exceeds max_objects')
    if data.crs is None: raise ValueError('Atlas requires known CRS')
    if p.get('group') and p['group'] not in data: raise ValueError('Unknown group field')
    order=sorted(range(len(data)),key=lambda i:(str(data.iloc[i].get(p.get('group'),'')),str(data.iloc[i][identity])))
    data=data.iloc[order].reset_index(drop=True)
    invalid=data.geometry.isna() | data.geometry.is_empty | ~data.geometry.is_valid | data.geometry.has_z
    if hasattr(shapely,'has_m'): invalid |= shapely.has_m(data.geometry.to_numpy())
    valid=data.loc[~invalid].copy()
    metric=None; near=[]; relation_info={'unmatched':[]}
    if len(valid):
        metric,factor=analysis_frame(valid,p.get('analysis_crs'))
        # Orient rings for the existing Matplotlib winding fill rule.
        metric.geometry=metric.geometry.map(orient)
        table,relation_info=relations(metric,metric,{'id':identity,'k':p.get('k',1),'exclude_self':True})
        near=clean(table.to_dict('records'))
        sizes=metric.geometry.bounds
        needed=max(float((sizes.maxx-sizes.minx).max()),float((sizes.maxy-sizes.miny).max()))*1.4
        span=float(p.get('span_m',max(needed*factor,100)))/factor
        if not math.isfinite(span) or span<=0 or span<needed: raise ValueError('span_m must fit every object with padding')
    spec=importlib.util.spec_from_file_location('atlas_plot',Path(__file__).with_name('plot.py'))
    plot=importlib.util.module_from_spec(spec);spec.loader.exec_module(plot)
    with bundle(output) as out:
        pages=[]; dossiers=[]
        if metric is not None: metric.to_file(out/'objects.gpkg',driver='GPKG',engine='pyogrio')
        for i,row in data.iterrows():
            name=f'{i+1:06d}'; oid=clean(row[identity]); attrs=clean(row.drop(data.geometry.name).to_dict())
            record={'id':oid,'source_row':int(order[i]),'attributes':attrs,'source_sha256':original,'neighbors':[r for r in near if r['source_id']==oid]}
            record['status']='omitted' if bool(invalid.iloc[i]) else 'complete'
            if record['status']=='omitted':
                record['reason']='null, empty, invalid or Z/M geometry'
            else:
                selected=metric.loc[metric[identity]==row[identity]].iloc[0]; geom=selected.geometry
                xmin,ymin,xmax,ymax=geom.bounds; cx=(xmin+xmax)/2;cy=(ymin+ymax)/2
                bounds=[cx-span/2,cy-span/2,cx+span/2,cy+span/2]
                record.update(location={'representative_xy':list(geom.representative_point().coords)[0],'bounds':list(geom.bounds),'crs':metric.crs.to_string()},metrics={'area_m2':geom.area*factor**2,'length_m':geom.length*factor}, anomalies=[], map_bounds=bounds)
                template={'layers':{'province':{'facecolor':'#dce3e8','edgecolor':'#647480'},'highlight':{'facecolor':'#e87744','edgecolor':'#9c3924','alpha':1,'label':{'enabled':True,'fontsize':10}}},'layout':{'bounds':bounds,'scale_bar':{'length_units':span/5,'label':f'{span*factor/5:g} m (projected)'},'legend':[{'color':'#dce3e8','label':'Context'},{'color':'#e87744','label':'Selected object'}]}}
                def short(value):
                    text=str(value).replace('\n',' ')
                    return text if len(text)<=36 else text[:33]+'...'
                summary=[[short(k),short(v)] for k,v in list(attrs.items())[:4]]
                summary += [['area_m2',f'{geom.area*factor**2:.6g}'],['length_m',f'{geom.length*factor:.6g}']]
                template['layout']['table_rows']=summary
                args=SimpleNamespace(figsize='8,11',label_field=identity,highlight=str(list(metric[identity]).index(row[identity])),title=f'Object {short(oid)}',output=str(out/(name+'.png')),dpi=120)
                plot.plot_location([(str(out/'objects.gpkg'),None)],template,args)
                with Image.open(out/(name+'.png')) as im:
                    im.load(); record['map']={'path':name+'.png','size':list(im.size),'sha256':digest(out/(name+'.png'))}
            write_json(out/(name+'.json'),record); dossiers.append({'id':oid,'path':name+'.json','status':record['status']})
            def esc(v):return html.escape(str(v),quote=True)
            table=''.join(f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k,v in attrs.items())
            image=f'<img src="{name}.png" alt="Object {esc(oid)}">' if 'map' in record else '<p>Omitted: '+esc(record['reason'])+'</p>'
            neighbors=''.join(f"<tr><td>{esc(r['target_id'])}</td><td>{r['distance_m']:.4g} m</td></tr>" for r in record['neighbors'])
            metrics=''.join(f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k,v in record.get('metrics',{}).items())
            pages.append(f'<section id="object-{name}"><h1>{esc(oid)}</h1>{image}<h2>Attributes</h2><table>{table}</table><h2>Planar geometry metrics</h2><table>{metrics}</table><h2>Nearest objects</h2><table>{neighbors}</table><p>Geometry diagnostics only; business conditions unverified.</p><p><a href="{name}.json">Dossier</a> · Source SHA256: {original}</p></section>')
        (out/'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Object atlas</title><style>body{font:16px sans-serif;max-width:960px;margin:auto}section{break-before:page;margin:2em 0}img{max-width:100%}td,th{border:1px solid #ccc;padding:.4em;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}p{overflow-wrap:anywhere}</style>'+''.join(pages)+'</html>',encoding='utf-8')
        write_json(out/'record.json',{'schema_version':1,'status':'complete','source':{'path':str(Path(source).resolve()),'sha256':original},'parameters':p,'environment':environment(),'objects':dossiers,'relations':relation_info,'scale_rule':'same square projected extent for every valid object; no fixed physical print scale','validation_scope':'geometry and references checked; visual review separate; no business or historical validation'})
        if fingerprint(source)!=original: raise ValueError('Input changed during atlas rendering')
    return {'output':str(output),'objects':len(dossiers),'omitted':sum(x['status']=='omitted' for x in dossiers)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('--params',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:print(json.dumps(execute(a.input,a.params,a.output)))
    except (ValueError,TypeError,KeyError,OSError) as e:p.exit(1,f'ERROR: {e}\n')
if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
