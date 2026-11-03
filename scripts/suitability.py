#!/usr/bin/env python3
"""Explicit object scoring, hard exclusions and finite scenario sensitivity."""
import argparse
import json
from pathlib import Path
import time
import resource
import sys
import numpy as np
import geopandas as gpd
from _daily import stable, number
from _delivery import bundle, digest, write_json
from _safe_io import iter_vector_chunks, write_vector_atomic
from daily import fingerprint, clean
from _budget import check_vector


def score(frame,p,restricted=None):
    if set(p)-{'id','criteria','scenarios','study','constraints','restriction'}:raise ValueError('Unknown parameter')
    stable(frame,p['id'])
    if not isinstance(p.get('study'),str) or not p['study'].strip():raise ValueError('Explicit study and scenario assumptions required')
    if frame.crs is None or frame.empty or not frame.geometry.is_valid.all() or frame.geometry.is_empty.any():raise ValueError('Valid georeferenced objects required')
    frame=frame.assign(_key=frame[p['id']].astype(str)).sort_values('_key').drop(columns='_key').reset_index(drop=True)
    criteria=p['criteria'];names=list(criteria)
    if not names or any(not isinstance(k,str) or not k for k in names):raise ValueError('Named criteria required')
    scaled={};missing=np.zeros(len(frame),dtype=bool)
    for name,c in criteria.items():
        if set(c)!={'field','low','high','prefer'} or c['prefer'] not in ('high','low'):raise ValueError('Criterion requires field/low/high/prefer')
        lo,hi=number(c['low']),number(c['high'])
        if lo>=hi:raise ValueError('Criterion high must exceed low')
        values=__import__('pandas').to_numeric(frame[c['field']],errors='raise').to_numpy(dtype=float,na_value=np.nan)
        if np.isinf(values).any():raise ValueError('Infinite indicator')
        missing|=~np.isfinite(values);v=np.clip((values-lo)/(hi-lo),0,1);scaled[name]=v if c['prefer']=='high' else 1-v
    reasons=[[] for _ in range(len(frame))]
    for name,c in criteria.items():
        for i in np.flatnonzero(~np.isfinite(scaled[name])):reasons[i].append('missing:'+name)
    for c in p.get('constraints',[]):
        if set(c)!={'field','min','max'}:raise ValueError('Constraint requires field/min/max')
        lo,hi=number(c['min']),number(c['max'])
        if lo>hi:raise ValueError('Constraint range reversed')
        v=__import__('pandas').to_numeric(frame[c['field']],errors='raise').to_numpy(dtype=float,na_value=np.nan)
        for i in np.flatnonzero(~np.isfinite(v)|(v<lo)|(v>hi)):reasons[i].append('constraint:'+c['field'])
    if restricted is not None:
        if restricted.crs is None or not restricted.geom_type.isin(['Polygon','MultiPolygon']).all() or not restricted.geometry.is_valid.all() or restricted.geometry.is_empty.any():raise ValueError('Valid restriction polygons required')
        area=restricted.to_crs(frame.crs).geometry.union_all()
        for i in np.flatnonzero(frame.geometry.intersects(area)):reasons[i].append('restricted_intersection')
    scenarios=p['scenarios'];ids=[s['id'] for s in scenarios]
    if not 1<=len(scenarios)<=100 or any(not isinstance(i,str) or not i for i in ids) or len(ids)!=len(set(ids)):raise ValueError('Unique nonempty scenario IDs required')
    rows=[];weights={}
    for s in scenarios:
        if set(s)!={'id','weights','threshold'} or set(s['weights'])!=set(names):raise ValueError('Each scenario must explicitly weight every criterion')
        w=np.array([number(s['weights'][n]) for n in names]);threshold=number(s['threshold'])
        if (w<0).any() or not np.isfinite(w.sum()) or w.sum()<=0 or not 0<=threshold<=1:raise ValueError('Invalid weights or score threshold')
        w=w/w.sum();weights[s['id']]=dict(zip(names,w.tolist()))
        total=sum(scaled[n]*v for n,v in zip(names,w));eligible=np.array([not r for r in reasons]);total[~eligible]=np.nan
        ranks=__import__('pandas').Series(total).rank(ascending=False,method='min').to_numpy()
        for i,row in frame.iterrows():
            rows.append(dict(source_id=str(row[p['id']]),scenario=s['id'],score=total[i],rank=ranks[i],eligible=bool(eligible[i]),selected=bool(eligible[i] and total[i]>=threshold),reasons=json.dumps(reasons[i]+(['below_threshold'] if eligible[i] and total[i]<threshold else [])),contributions=json.dumps({n:clean(scaled[n][i]*v) for n,v in zip(names,w)}),geometry=row.geometry))
    result=gpd.GeoDataFrame(rows,geometry='geometry',crs=frame.crs)
    stability=[]
    for ident,g in result.groupby('source_id',sort=True):
        stability.append(dict(source_id=ident,selected_scenarios=int(g.selected.sum()),stable_selected=bool(g.selected.all()),never_selected=bool((~g.selected).all()),score_min=clean(g.score.min()),score_max=clean(g.score.max()),rank_min=clean(g['rank'].min()),rank_max=clean(g['rank'].max())))
    # Pair every supplied scenario with the first, retaining changed parameters and
    # affected object IDs. This is finite scenario comparison, not a derivative or
    # a claim about untested parameter ranges.
    baseline=result[result.scenario==ids[0]].set_index('source_id')
    comparisons=[]
    for s in scenarios[1:]:
        other=result[result.scenario==s['id']].set_index('source_id').loc[baseline.index]
        score_changed=~np.isclose(baseline.score,other.score,rtol=0,atol=1e-12,equal_nan=True)
        rank_changed=~np.isclose(baseline['rank'],other['rank'],rtol=0,atol=0,equal_nan=True)
        comparisons.append(dict(baseline=ids[0],scenario=s['id'],
            normalized_weight_delta={n:weights[s['id']][n]-weights[ids[0]][n] for n in names},
            threshold_delta=s['threshold']-scenarios[0]['threshold'],
            score_changed_ids=baseline.index[score_changed].tolist(),
            rank_changed_ids=baseline.index[rank_changed].tolist(),
            newly_selected_ids=baseline.index[~baseline.selected & other.selected].tolist(),
            lost_selected_ids=baseline.index[baseline.selected & ~other.selected].tolist()))
    return result,dict(normalized_weights=weights,stability=stability,scenario_comparisons=comparisons,
        comparison_rule='First supplied scenario is baseline; score differences >1e-12, exact ranks/selections; only supplied weights and thresholds tested',
        data_insufficient_ids=[str(frame.iloc[i][p['id']]) for i in np.flatnonzero(missing)],
        restriction_rule='Any intersection excludes whole object, including boundary contact; no implicit clipping',missing_rule='Any missing criterion excludes, even with zero weight',tie_rank='competition rank; stable source order')


def execute(source,p,output):
    start=time.perf_counter();paths=[Path(source)]+([Path(p['restriction'])] if 'restriction' in p else []);hashes=[fingerprint(x) for x in paths]
    for x in paths:check_vector(x)
    frame=gpd.read_file(paths[0]);restricted=gpd.read_file(paths[1]) if len(paths)>1 else None
    result,details=score(frame,p,restricted)
    with bundle(output) as stage:
        write_vector_atomic(result,stage/'scores.gpkg')
        count=0
        for start,back in iter_vector_chunks(stage/'scores.gpkg'):
            part=result.iloc[start:start+len(back)];np.testing.assert_allclose(back.score,part.score,rtol=0,atol=0,equal_nan=True)
            if back.selected.tolist()!=part.selected.tolist() or back.reasons.tolist()!=part.reasons.tolist():raise ValueError('Scenario readback mismatch')
            count+=len(back)
        if count!=len(result):raise ValueError('Scenario readback mismatch')
        if hashes!=[fingerprint(x) for x in paths]:raise ValueError('Input changed')
        rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        write_json(stage/'record.json',clean(dict(status='candidate',parameters=p,inputs=[dict(name=x.name,sha256=h) for x,h in zip(paths,hashes)],details=details,artifact_sha256=digest(stage/'scores.gpkg'),resources={'wall_seconds':time.perf_counter()-start,'max_rss_bytes':rss if sys.platform=='darwin' else rss*1024},implementation=digest(__file__))))


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('input');a.add_argument('--params',required=True);a.add_argument('--output',required=True);v=a.parse_args()
    try:execute(v.input,json.loads(Path(v.params).read_text(encoding='utf-8')),v.output)
    except (ValueError,KeyError,TypeError,OSError) as e:a.exit(1,f'ERROR: {e}\n')
