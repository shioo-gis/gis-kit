#!/usr/bin/env python3
"""Revision-bound sheet coverage and seam ledger; never infers semantic links."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
from shapely.geometry import box
from shapely.ops import unary_union

_spec=importlib.util.spec_from_file_location('annotation',Path(__file__).with_name('raster-annotate.py'))
annotation=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(annotation)


def audit(run,ledger):
    meta,_=annotation.source(run); state=annotation.load(run)
    annotation.validate_structure(state)
    revision_hash=annotation.digest(Path(run)/'revisions'/f"{state['revision']:06d}.json")
    if ledger.get('schema_version')!=1: raise ValueError('Unsupported ledger schema')
    current=ledger.get('source_sha256')==meta['sha256'] and ledger.get('revision')==state['revision'] and ledger.get('revision_sha256')==revision_hash
    regions=ledger['regions']; ids=[x['id'] for x in regions]
    declared_categories={r['id']:r['categories'] for r in regions}
    for entry in ledger.get('observations',[]):
        if entry['region'] not in declared_categories or entry['category'] not in declared_categories[entry['region']]: raise ValueError('Undeclared observation region/category')
    if not regions or len(ids)!=len(set(ids)): raise ValueError('Unique nonempty region IDs required')
    image=box(0,0,meta['width'],meta['height']); records=[]
    for region in regions:
        bounds=region['bounds']
        if len(bounds)!=4 or not all(math.isfinite(float(v)) for v in bounds) or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]: raise ValueError('Invalid region bounds')
        extent=box(*bounds)
        if not image.covers(extent): raise ValueError('Region outside image')
        categories=region['categories']
        if not categories or len(categories)!=len(set(categories)): raise ValueError('Unique target categories required')
        if region['kind'] not in ('main','inset'): raise ValueError('Region kind must be main or inset')
        for category in categories:
            entries=[x for x in ledger.get('observations',[]) if x['region']==region['id'] and x['category']==category]
            areas={k:[] for k in ('observed','drawn','reviewed')}
            for entry in entries:
                b=entry['bounds']
                if len(b)!=4 or not all(math.isfinite(float(v)) for v in b) or b[0]>=b[2] or b[1]>=b[3]: raise ValueError('Invalid observation bounds')
                area=box(*b)
                if area.area<=0 or not extent.covers(area): raise ValueError('Observation outside region')
                objects=entry.get('objects',[])
                if any(not any(i in state[g] for g in ('nodes','edges','faces','points')) for i in objects): raise ValueError('Unknown annotation object')
                if not entry.get('evidence'): raise ValueError('Observation evidence required')
                if current:
                    areas['observed'].append(area)
                    if entry.get('drawn') and (objects or entry.get('empty_reason')): areas['drawn'].append(area)
                    if entry.get('review')=='accepted' and entry.get('reviewer') and entry.get('drawn') and (objects or entry.get('empty_reason')):
                        areas['reviewed'].append(area)
            fractions={k:unary_union(v).intersection(extent).area/extent.area if v else 0. for k,v in areas.items()}
            records.append({'region':region['id'],'kind':region['kind'],'category':category,**fractions,'complete':current and fractions['reviewed']>=1-1e-12})
    seams=[]
    for seam in ledger.get('seams',[]):
        node=seam['node']; edge=seam['edge']
        if node not in state['nodes'] or edge not in state['edges']: raise ValueError('Unknown seam node/edge')
        if node not in (state['edges'][edge]['start'],state['edges'][edge]['end']): raise ValueError('Seam node is not edge endpoint')
        neighbors=seam['regions']
        if len(neighbors)!=2 or len(set(neighbors))!=2 or any(r not in ids for r in neighbors): raise ValueError('Seam requires two declared regions')
        confirmed=current and seam.get('status')=='confirmed' and bool(seam.get('reviewer')) and all(seam.get('observations',{}).get(r) for r in neighbors) and bool(seam.get('direction'))
        seams.append({'node':node,'edge':edge,'confirmed':bool(confirmed),'status':'confirmed' if confirmed else 'hold'})
    unresolved=ledger.get('uncertainties',[])+ledger.get('pending_faces',[])
    unresolved+= [{'group':g,'id':k,'status':v.get('status')} for g in ('faces','edges','points') for k,v in state[g].items() if v.get('status') in ('incomplete','uncertain')]
    unresolved+= [c for c in state.get('crossings',[]) if c.get('relation')=='uncertain']
    declared=ledger.get('scope_review',{})
    scope_confirmed=current and declared.get('status')=='accepted' and bool(declared.get('reviewer')) and bool(declared.get('evidence'))
    complete=bool(scope_confirmed and all(r['complete'] for r in records) and all(s['confirmed'] for s in seams) and not unresolved)
    return {'schema_version':1,'revision':state['revision'],'source_sha256':meta['sha256'],'revision_sha256':revision_hash,'ledger_current':current,
            'regions':records,'seams':seams,'unresolved':unresolved,'status':'complete' if complete else 'hold',
            'acceptance_scope':'declared coverage/review ledger only; not historical truth or full-sheet acceptance',
            'invalidation':'any annotation revision or source change invalidates all ledger approvals conservatively'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run');p.add_argument('--ledger',required=True);p.add_argument('--output',required=True)
    args=p.parse_args()
    try:
        result=audit(args.run,json.loads(Path(args.ledger).read_text(encoding='utf-8')))
        result['ledger_sha256']=annotation.digest(args.ledger)
        meta,_=annotation.source(args.run)
        if annotation.load(args.run)['revision']!=result['revision']: raise ValueError('Annotation changed during audit')
        annotation.write(args.output,result)
        print(json.dumps({'output':args.output,'status':result['status']}))
    except (ValueError,KeyError,TypeError,OSError) as exc: p.exit(1,f'ERROR: {exc}\n')
if __name__=='__main__': main()
