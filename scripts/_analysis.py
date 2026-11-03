"""Bounded analysis extensions using the existing daily bundle contract."""
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from _core import reproject
from _daily import stable, metric
from _metric import analysis_frames


def compare(left, right, p):
    key = p['id']; stable(left, key); stable(right, key)
    for data in (left,right):
        if data.geometry.isna().any() or data.geometry.is_empty.any() or not data.geometry.is_valid.all(): raise ValueError('Version comparison requires valid nonempty geometries')
    right = reproject(right, left.crs, purpose='align')
    a = left.set_index(left[key].astype(str)); b = right.set_index(right[key].astype(str))
    fields = p.get('fields', sorted((set(left.columns) & set(right.columns)) - {key, left.geometry.name}))
    rows = []
    for ident in sorted(set(a.index) | set(b.index)):
        old = a.loc[ident] if ident in a.index else None
        new = b.loc[ident] if ident in b.index else None
        changes = {}
        if old is not None and new is not None:
            for field in fields:
                x,y = old[field],new[field]
                if (pd.isna(x) and pd.isna(y)): continue
                if pd.isna(x) or pd.isna(y) or x != y: changes[field] = [x,y]
        kind = 'added' if old is None else 'removed' if new is None else 'modified' if changes or not old.geometry.equals(new.geometry) else 'unchanged'
        rows.append({'source_id':ident,'change':kind,'attribute_changes':json.dumps(changes,default=str,ensure_ascii=False),
                     'geometry_changed':old is not None and new is not None and not old.geometry.equals(new.geometry),
                     'before_wkt':None if old is None else old.geometry.wkt,'after_wkt':None if new is None else new.geometry.wkt})
    # Positive-area overlaps are proposals only; no semantic ID adoption.
    removed = a.loc[sorted(set(a.index)-set(b.index))]; added = b.loc[sorted(set(b.index)-set(a.index))]
    candidates = []
    if len(removed) and len(added):
        (removed,added),_=analysis_frames([removed,added],p.get('analysis_crs'))
    for ident, row in removed.iterrows():
        for j in added.sindex.query(row.geometry, predicate='intersects'):
            other = added.iloc[j]; area = row.geometry.intersection(other.geometry).area
            if area > 0:
                candidates.append({'before':ident,'after':str(other[key]),'before_fraction':area/row.geometry.area,
                                   'after_fraction':area/other.geometry.area,'status':'unknown'})
    for c in candidates:
        n = sum(x['before']==c['before'] for x in candidates); m = sum(x['after']==c['after'] for x in candidates)
        c['proposal'] = 'complex' if n>1 and m>1 else 'split' if n>1 else 'merge' if m>1 else 'match'
    transfers=[]
    if 'category' in p:
        field=p['category']; counts={}
        for ident in sorted(set(a.index)&set(b.index)):
            old,new=a.loc[ident,field],b.loc[ident,field]
            pair=(None if pd.isna(old) else str(old),None if pd.isna(new) else str(new))
            counts[pair]=counts.get(pair,0)+1
        transfers=[{'before':x,'after':y,'count':n} for (x,y),n in counts.items()]
    return pd.DataFrame(rows,columns=['source_id','change','attribute_changes','geometry_changed','before_wkt','after_wkt']), {'crs':left.crs.to_string(),'candidates':candidates,'category_transfers':transfers,'transfer_scope':'stable matched IDs only; null retained','adopted':False,'missing_record_means':'dataset absence only'}


def profile(frame, p):
    rows = []
    for column in frame.columns:
        if column == frame.geometry.name: continue
        rows.append({'field':column,'dtype':str(frame[column].dtype),'missing':int(frame[column].isna().sum()),'unique':int(frame[column].nunique()),'duplicate_nonnull_rows':int(frame[column].dropna().duplicated(keep=False).sum())})
    bad = frame.geometry.isna() | frame.geometry.is_empty | ~frame.geometry.is_valid
    required = p.get('required_fields',[])
    return pd.DataFrame(rows,columns=['field','dtype','missing','unique','duplicate_nonnull_rows']), {'scope':'full','crs':str(frame.crs),'bounds':frame.total_bounds.tolist(),
        'rows':len(frame),'anomaly_rows':np.flatnonzero(bad).tolist(),'anomaly_ids':frame.loc[bad,p['id']].astype(str).tolist() if p.get('id') in frame else None,'missing_conditions':[x for x in required if x not in frame]+(['CRS'] if frame.crs is None else []),
        'task_readiness':'unknown','time_fields':p.get('time_fields',[]),'unchecked_conditions':p.get('conditions',[])}


def distribution(frame, p):
    values = pd.to_numeric(frame[p['value']],errors='raise').to_numpy(dtype=float)
    if np.isinf(values).any(): raise ValueError('Infinite observations')
    good = values[np.isfinite(values)]
    if not len(good): raise ValueError('No finite observations')
    quantiles = np.quantile(good,[0,.25,.5,.75,1])
    return pd.DataFrame([{'count':len(good),'missing':len(values)-len(good),'mean':float(good.mean()),'std_population':float(good.std()),
                          **dict(zip(['min','q25','median','q75','max'],quantiles))}]), {'method':'descriptive only; no significance inference','quantile_method':'linear','study_area':p.get('study_area','unknown')}


def grid_summary(left, right, p):
    stable(left,p['id']); stable(right,p['right_id'])
    (points,cells),factor = analysis_frames([left,right],p.get('analysis_crs'))
    if not points.geom_type.eq('Point').all() or not cells.geom_type.isin(['Polygon','MultiPolygon']).all(): raise ValueError('Requires points and polygon cells')
    assignments=[]; counts={str(x):0 for x in cells[p['right_id']]}
    for _,row in points.iterrows():
        hits = cells.iloc[cells.sindex.query(row.geometry,predicate='intersects')]
        owners = sorted(hits[p['right_id']].astype(str))
        owner = owners[0] if owners else None
        if owner is not None: counts[owner]+=1
        assignments.append({'source_id':str(row[p['id']]),'cell_id':owner,'candidate_count':len(owners)})
    result=cells.copy()
    if 'point_count' in result or 'density_per_km2' in result: raise ValueError('Output field collision')
    result['point_count']=[counts[str(x)] for x in cells[p['right_id']]]
    result['density_per_km2']=result.point_count/(result.area*factor**2/1e6)
    return result,{'assignments':assignments,'boundary_policy':'lexicographically smallest cell ID, once only','analysis_crs':str(cells.crs),'unmatched':sum(x['cell_id'] is None for x in assignments)}


def time_slice(frame,p):
    stable(frame,p['id'])
    start=pd.to_datetime(frame[p['start']],utc=True,errors='raise'); end=pd.to_datetime(frame[p['end']],utc=True,errors='raise')
    if ((start.notna() & end.notna()) & (start>=end)).any(): raise ValueError('Invalid time interval')
    at=pd.to_datetime(p['at'],utc=True,errors='raise')
    if pd.isna(at): raise ValueError('Missing instant')
    unknown=start.isna() | end.isna()
    policy=p.get('unknown','hold')
    if policy not in ('hold','open'): raise ValueError('unknown must be hold or open')
    keep=(start.isna() | (start<=at)) & (end.isna() | (at<end))
    if policy=='hold': keep &= ~unknown
    return frame.loc[keep].copy(),{'interval':'[start,end)','unknown_policy':policy,'unknown_ids':frame.loc[unknown,p['id']].astype(str).tolist()}


def validate_coverage(geometries, gap_width=0.0):
    """Fail closed before GEOS coverage operations (which otherwise trust inputs)."""
    if (not all(callable(getattr(shapely, name, None)) for name in
                ('coverage_simplify', 'coverage_is_valid', 'coverage_invalid_edges'))
            or tuple(shapely.geos_version) < (3, 12, 0)):
        raise ValueError('coverage_simplify requires Shapely 2.1 / GEOS 3.12; no per-polygon fallback')
    if len(geometries) == 0 or any(g is None or g.is_empty or not g.is_valid
                                  or g.geom_type not in ('Polygon', 'MultiPolygon') for g in geometries):
        raise ValueError('Coverage requires nonempty valid Polygon/MultiPolygon inputs')
    if any(g.has_z for g in geometries) or (hasattr(shapely, 'has_m') and np.any(shapely.has_m(geometries))):
        raise ValueError('Coverage requires explicit 2D geometries; Z/M is not discarded')
    if not np.isfinite(gap_width) or gap_width < 0:
        raise ValueError('gap_width_m must be finite and nonnegative')
    if not bool(shapely.coverage_is_valid(geometries, gap_width=gap_width)):
        invalid = shapely.coverage_invalid_edges(geometries, gap_width=gap_width)
        positions = [i for i, edge in enumerate(invalid) if not edge.is_empty]
        raise ValueError(f'Invalid coverage: overlaps, unmatched edges or narrow gaps at rows {positions}')


def cleanup(frame,right,p):
    if hasattr(shapely, 'has_m') and np.any(shapely.has_m(frame.geometry.to_numpy())): raise ValueError('Cleanup requires explicit 2D input')
    key=p['id']; stable(frame,key); work,factor=metric(frame,p.get('analysis_crs'))
    coverage_details = None
    method=p['method']; tolerance=float(p['tolerance_m'])/factor
    if not np.isfinite(tolerance) or tolerance<=0: raise ValueError('Positive finite tolerance required')
    if method=='near_duplicates':
        rows=[]
        for i,g in enumerate(work.geometry):
            for j in work.sindex.query(g.buffer(tolerance)):
                if j>i and g.hausdorff_distance(work.geometry.iloc[j])<=tolerance:
                    if len(rows)>=int(p.get('max_pairs',100000)): raise ValueError('Near-duplicate output exceeds max_pairs')
                    rows.append({'left_id':str(work.iloc[i][key]),'right_id':str(work.iloc[j][key]),'distance_m':g.hausdorff_distance(work.geometry.iloc[j])*factor,'status':'candidate'})
        return pd.DataFrame(rows,columns=['left_id','right_id','distance_m','status']),{'adopted':False}
    if method=='split_edges':
        if not work.geom_type.isin(['LineString','MultiLineString']).all(): raise ValueError('split_edges requires lines')
        parts=list(shapely.get_parts(shapely.union_all(work.geometry.to_numpy())))
        parts.sort(key=lambda g:shapely.normalize(g).wkb_hex)
        rows=[]
        for i,g in enumerate(parts):
            owners=[str(work.iloc[j][key]) for j in work.sindex.query(g) if work.geometry.iloc[j].covers(g)]
            if not owners: raise ValueError('Cannot map noded edge to sources')
            rows.append({'edge_id':str(i),'source_ids':json.dumps(sorted(owners)),'geometry':g})
        return gpd.GeoDataFrame(rows,geometry='geometry',crs=work.crs),{'status':'candidate','adopted':False,'semantics':'geometric noding only; no semantic connection approval'}
    if method=='precision': geometries=shapely.set_precision(work.geometry.to_numpy(),tolerance)
    elif method=='snap':
        if right is None: raise ValueError('snap requires reference')
        reference=reproject(right,work.crs,purpose='align')
        if not reference.geometry.is_valid.all(): raise ValueError('Invalid reference geometry')
        geometries=shapely.snap(work.geometry.to_numpy(),reference.geometry.union_all(),tolerance)
    elif method=='coverage_simplify':
        gap_width = float(p.get('gap_width_m', 0)) / factor
        simplify_boundary = p.get('simplify_boundary', False)
        if not isinstance(simplify_boundary, bool): raise ValueError('simplify_boundary must be boolean')
        original = work.geometry.to_numpy()
        validate_coverage(original, gap_width)
        # Stable ordering also makes input row order independent of backend traversal.
        order = np.argsort(work[key].astype(str).to_numpy(), kind='stable')
        simplified = shapely.coverage_simplify(original[order], tolerance,
                                               simplify_boundary=simplify_boundary)
        geometries = simplified[np.argsort(order)]
        validate_coverage(geometries, gap_width)
        before_union = shapely.union_all(original)
        after_union = shapely.union_all(geometries)
        footprint_preserved = before_union.equals(after_union)
        if not simplify_boundary and not footprint_preserved:
            raise ValueError('Coverage simplification changed protected outer/hole boundaries')
        coverage_details = {'input_valid': True, 'output_valid': True,
                            'simplify_boundary': simplify_boundary,
                            'gap_width_m': gap_width * factor,
                            'footprint_preserved': footprint_preserved,
                            'footprint_change_m2': before_union.symmetric_difference(after_union).area * factor**2,
                            'vertices_before': int(shapely.get_num_coordinates(original).sum()),
                            'vertices_after': int(shapely.get_num_coordinates(geometries).sum()),
                            'analysis_crs': str(work.crs), 'shapely': shapely.__version__,
                            'geos': shapely.geos_version_string,
                            'tolerance_semantics': 'Visvalingam-Whyatt area-derived scale; not a displacement limit'}
    else: raise ValueError('Unknown cleanup method')
    result=work.copy(); result.geometry=geometries
    if result.geometry.is_empty.any() or not result.geometry.is_valid.all(): raise ValueError('Candidate collapsed or invalid')
    impacts=[]
    for ident,a,b in zip(work[key],work.geometry,result.geometry):
        impacts.append({'id':str(ident),'before_wkt':a.wkt,'after_wkt':b.wkt,'displacement_m':a.hausdorff_distance(b)*factor,
                        'area_delta_m2':(b.area-a.area)*factor**2,'before_parts':int(shapely.get_num_geometries(a)),'after_parts':int(shapely.get_num_geometries(b)),
                        'before_holes':sum(len(g.interiors) for g in shapely.get_parts(a) if g.geom_type=='Polygon'),
                        'after_holes':sum(len(g.interiors) for g in shapely.get_parts(b) if g.geom_type=='Polygon')})
    return result,{'adopted':False,'status':'candidate','impacts':impacts,'business_rules':'not revalidated; approval required', **({'coverage': coverage_details} if coverage_details is not None else {})}


def cluster(frame,p):
    from sklearn.cluster import DBSCAN
    import sklearn
    key=p['id']; stable(frame,key); work,factor=metric(frame,p.get('analysis_crs'))
    if not work.geom_type.eq('Point').all(): raise ValueError('DBSCAN requires points')
    work=work.iloc[np.argsort(work[key].astype(str).to_numpy(),kind='stable')].copy()
    if 'cluster_id' in work: raise ValueError('cluster_id collision')
    eps=float(p['eps_m'])/factor; minimum=int(p['min_samples'])
    if not np.isfinite(eps) or eps<=0 or minimum<1 or minimum!=p['min_samples'] or isinstance(p['min_samples'],bool): raise ValueError('Invalid DBSCAN parameters')
    labels=DBSCAN(eps=eps,min_samples=minimum,algorithm='kd_tree',n_jobs=1).fit_predict(np.column_stack([work.geometry.x,work.geometry.y]))
    # Stable source anchor names; border points follow sorted-ID traversal.
    anchors={label:min(work.loc[labels==label,key].astype(str)) for label in set(labels) if label!=-1}
    work['cluster_id']=[('cluster:'+anchors[x]) if x in anchors else 'noise' for x in labels]
    return work,{'method':'DBSCAN','sklearn_version':sklearn.__version__,'analysis_crs':str(work.crs),'eps_m':p['eps_m'],'min_samples':minimum,'border_policy':'sorted source-ID traversal','noise_count':int((labels==-1).sum()),'inference':'none','study_area':p.get('study_area','unknown')}


def cleanup_adopt(left,right,p,hashes):
    """Adopt geometry only after fingerprint-bound approval and bounded rechecks."""
    from _daily import rules
    if not p.get('approved_by') or p.get('source_sha256')!=hashes[0] or p.get('candidate_sha256')!=hashes[1]: raise ValueError('Missing or stale adoption approval')
    key=p['id']; stable(left,key);stable(right,key)
    if set(left[key].astype(str))!=set(right[key].astype(str)): raise ValueError('Adoption requires identical stable IDs; split-edge candidates need separate review')
    right=right.set_index(right[key].astype(str)).loc[left[key].astype(str)].reset_index(drop=True)
    (before,after),factor=analysis_frames([left,right],p.get('analysis_crs'))
    displacement=float(p['max_displacement_m']); area=float(p['max_area_change_m2'])
    if not np.isfinite([displacement,area]).all() or min(displacement,area)<0: raise ValueError('Invalid impact limits')
    def structure(g):
        return (g.geom_type,int(shapely.get_num_geometries(g)),sum(len(x.interiors) for x in shapely.get_parts(g) if x.geom_type=='Polygon'))
    for a,b in zip(before.geometry,after.geometry):
        if a.hausdorff_distance(b)*factor>displacement or abs(a.area-b.area)*factor**2>area: raise ValueError('Candidate exceeds approved impact limits')
        if not p.get('allow_structure_change',False) and structure(a)!=structure(b): raise ValueError('Unapproved structure change')
    coverage_report = None
    if p.get('require_coverage', False):
        if p['require_coverage'] is not True: raise ValueError('require_coverage must be boolean')
        gap = float(p.get('gap_width_m', 0)) / factor
        validate_coverage(before.geometry.to_numpy(), gap)
        validate_coverage(after.geometry.to_numpy(), gap)
        preserve = p.get('preserve_coverage_boundary', True)
        if not isinstance(preserve, bool): raise ValueError('preserve_coverage_boundary must be boolean')
        footprint_equal = before.geometry.union_all().equals(after.geometry.union_all())
        if preserve and not footprint_equal: raise ValueError('Candidate changed protected coverage footprint')
        coverage_report = {'input_valid': True, 'output_valid': True, 'footprint_preserved': footprint_equal}
    result=left.copy();result.geometry=reproject(right,left.crs,purpose='align').geometry.to_numpy()
    checks={'id':key,'rules':p['rules']}
    _,old_report=rules(left,None,checks);errors,new_report=rules(result,None,checks)
    if not new_report['passed']: raise ValueError('Candidate fails business rules')
    return result,{'adopted':True,'approved_by':p['approved_by'],'before_rules':old_report,'after_rules':new_report,'adoption_scope':'geometry only; original attributes preserved', **({'coverage': coverage_report} if coverage_report is not None else {})}
