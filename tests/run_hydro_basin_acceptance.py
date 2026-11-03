#!/usr/bin/env python3
"""Frozen-policy real DEM, catalogue topology and independent basin consistency check.
Inputs are prepared reference GPKGs; policy must be frozen before DEM processing.
This evaluates cross-product consistency, never surveyed geographic truth.
"""
import argparse, importlib.util, json, os, subprocess, sys, time
from collections import deque
from pathlib import Path
import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.features import geometry_mask, shapes
from rasterio.transform import from_origin
from shapely.geometry import Point, LineString, shape
from scipy.spatial import cKDTree
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
from _delivery import digest,write_json
from _peak_rss import peak_rss_bytes
spec=importlib.util.spec_from_file_location('hydro',S/'hydro-review.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
D={1:(-1,1),2:(-1,0),3:(-1,-1),4:(0,-1),5:(1,-1),6:(1,0),7:(1,1),8:(0,1)}


def upstream_mask(direction, outlet):
    """Independent reverse-neighbour traversal; no GRASS outlet implementation used."""
    a=np.asarray(direction);mask=np.zeros(a.shape,bool);queue=deque([tuple(outlet)])
    while queue:
        r,c=queue.popleft()
        if mask[r,c]:continue
        mask[r,c]=True
        for code,(dr,dc) in D.items():
            nr,nc=r-dr,c-dc
            if 0<=nr<a.shape[0] and 0<=nc<a.shape[1] and not mask[nr,nc] and a[nr,nc]==code:queue.append((nr,nc))
    return mask


def basin_metrics(candidate, reference, accumulation):
    if candidate.shape!=reference.shape or candidate.shape!=accumulation.shape:raise ValueError('Aligned grids required')
    union=np.count_nonzero(candidate|reference);ref=np.count_nonzero(reference)
    if not ref:raise ValueError('Empty reference basin')
    return dict(iou=float(np.count_nonzero(candidate&reference)/union),relative_area_error=float(abs(np.count_nonzero(candidate)-ref)/ref),candidate_cells=int(candidate.sum()),reference_cells=int(ref),boundary_contact_cells=int(candidate[0].sum()+candidate[-1].sum()+candidate[1:-1,0].sum()+candidate[1:-1,-1].sum()),negative_accumulation_cells=int(np.count_nonzero(candidate&(accumulation<0))))



def reference_identity(basin, following, rivers, policy):
    """Verify catalogue IDs and unique Next_down-connected endpoint, before routing."""
    if len(basin)!=1 or int(basin.iloc[0].HYBAS_ID)!=policy['basin_id']:
        raise ValueError('Frozen reference basin identity mismatch')
    if len(following)!=1 or int(basin.iloc[0].NEXT_DOWN)!=int(following.iloc[0].HYBAS_ID):
        raise ValueError('Reference NEXT_DOWN identity mismatch')
    reach=rivers[rivers.Reach_ID==policy['river_outlet_id']]
    if len(reach)!=1:raise ValueError('Frozen outlet river identity mismatch')
    reach=reach.iloc[0];nxt=rivers[rivers.Reach_ID==reach.Next_down]
    ends=[Point(reach.geometry.coords[0]),Point(reach.geometry.coords[-1])]
    match=[i for i,e in enumerate(ends) if len(nxt)==1 and nxt.distance(e).min()<=policy['old_endpoint_identity_m']]
    if len(match)!=1:raise ValueError('Outlet direction ambiguous')
    point=ends[match[0]];shared=basin.geometry.iloc[0].boundary.intersection(following.geometry.iloc[0].boundary)
    if shared.is_empty or point.distance(shared)>policy['reference_boundary_identity_m'] or point.distance(Point(policy['outlet_reference']))>1e-7:
        raise ValueError('Frozen outlet coordinate or boundary identity mismatch')
    fraction=basin.geometry.iloc[0].intersection(reach.geometry).length/reach.geometry.length
    if fraction<=.5:raise ValueError('Outlet reach does not primarily belong to reference basin')
    return dict(basin_id=int(basin.iloc[0].HYBAS_ID),next_basin_id=int(following.iloc[0].HYBAS_ID),reach_id=int(reach.Reach_ID),next_reach_id=int(reach.Next_down),downstream_endpoint_index=match[0],reference_endpoint_boundary_distance_m=point.distance(shared),reach_inside_fraction=fraction)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for k in ('dem','basin','next_basin','rivers','old_reaches','policy','output'):ap.add_argument(k)
    ap.add_argument('--grass',required=True);a=ap.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=False);started=time.perf_counter();implementation_before=digest(__file__)
    policy=json.loads(Path(a.policy).read_text(encoding='utf-8'));write_json(out/'policy.json',policy)
    inputs={k:digest(getattr(a,k)) for k in ('dem','basin','next_basin','rivers','old_reaches','policy')};basin=gpd.read_file(a.basin).to_crs(32645);rivers=gpd.read_file(a.rivers).to_crs(32645);old=gpd.read_file(a.old_reaches).to_crs(32645)
    next_basin=gpd.read_file(a.next_basin).to_crs(32645);identity=reference_identity(basin,next_basin,rivers,policy);write_json(out/'reference-identity.json',identity);shared_boundary=basin.geometry.iloc[0].boundary.intersection(next_basin.geometry.iloc[0].boundary)
    if shared_boundary.is_empty:raise ValueError('Reference NEXT_DOWN basin has no shared boundary')
    reports=[]
    def cli(args):
        start=time.perf_counter();p=subprocess.run([sys.executable,*map(str,args)],capture_output=True,encoding='utf-8', errors='replace')
        if p.returncode:raise RuntimeError(p.stderr[-1500:])
        return time.perf_counter()-start
    for res in policy['resolutions_m']:
        root=out/str(res);root.mkdir();l,b,r,t=transform_bounds(4326,32645,*policy['DEM_domain_lonlat']);l=np.ceil(l/res)*res;b=np.ceil(b/res)*res;r=np.floor(r/res)*res;t=np.floor(t/res)*res
        transform=from_origin(l,t,res,res);data=np.full((round((t-b)/res),round((r-l)/res)),np.nan)
        with rio.open(a.dem) as src:reproject(rio.band(src,1),data,src_transform=src.transform,src_crs=src.crs,dst_transform=transform,dst_crs=32645,dst_nodata=np.nan,resampling=Resampling.bilinear)
        assert np.isfinite(data).all()
        dem=root/'dem.tif'
        with rio.open(dem,'w',driver='GTiff',count=1,dtype='float64',width=data.shape[1],height=data.shape[0],crs=32645,transform=transform,nodata=np.nan,compress='deflate') as d:d.write(data,1)
        p=dict(vertical_unit='metre',vertical_datum='EGM2008 per Copernicus DEM product',source_description='Copernicus GLO-30 AWS public COG; bilinear projected derivative; modern DSM not ground surveyed river',flow_method='D8',threshold_cells=round(policy['threshold_area_m2']/res**2),max_pixels=10000000)
        write_json(root/'hydro.json',p);elapsed=cli([S/'terrain-backend.py','hydrology',dem,'--params',root/'hydro.json','--output',root/'routing','--grass',a.grass])
        with rio.open(root/'routing/accumulation.tif') as d:acc=d.read(1)
        with rio.open(root/'routing/drainage.tif') as d:drain=d.read(1)
        xx,yy=policy['outlet_reference'];rr,cc=rio.transform.rowcol(transform,xx,yy);raw=upstream_mask(drain,(rr,cc));rows,cols=np.indices(data.shape);xs=transform.c+(cols+.5)*res;ys=transform.f-(rows+.5)*res;dist=np.hypot(xs-xx,ys-yy);eligible=(dist<=policy['max_snap_m'])&(acc>0);assert eligible.any();index=np.argmax(np.where(eligible,acc,-np.inf));snap=np.unravel_index(index,data.shape);snapxy=[float(xs[snap]),float(ys[snap])];p['outlet']=snapxy;write_json(root/'outlet.json',p)
        elapsed+=cli([S/'terrain-backend.py','hydrology',dem,'--params',root/'outlet.json','--output',root/'outlet','--grass',a.grass])
        candidate=upstream_mask(drain,snap)
        with rio.open(root/'outlet/catchment.tif') as d:assert np.array_equal(candidate,d.read(1)==1)
        ref=geometry_mask(basin.geometry,out_shape=data.shape,transform=transform,invert=True)
        metrics=basin_metrics(candidate,ref,acc);raw_metrics=basin_metrics(raw,ref,acc)
        identity_distance=Point(snapxy).distance(shared_boundary)
        identity_pass=identity_distance<=policy['reference_boundary_identity_m']
        passed=identity_pass and metrics['iou']>=policy['min_basin_iou'] and metrics['relative_area_error']<=policy['max_relative_area_error'] and metrics['boundary_contact_cells']<=policy['max_boundary_contact_cells'] and metrics['negative_accumulation_cells']<=policy['max_negative_accumulation_cells_in_basin']
        with rio.open(root/'routing/streams.tif') as d:stream=d.read(1);sr,sc=np.where(np.isfinite(stream)&(stream>0))
        sx,sy=rio.transform.xy(transform,sr,sc);tree=cKDTree(np.column_stack((sx,sy)));reaches=[];lines=[]
        # Retain all old source IDs, and topology-derived endpoint orientation.
        for row in old.itertuples():
            nxt=rivers[rivers.Reach_ID==row.Next_down];ends=[Point(row.geometry.coords[0]),Point(row.geometry.coords[-1])];matches=[i for i,e in enumerate(ends) if len(nxt) and nxt.distance(e).min()<=policy['old_endpoint_identity_m']]
            if len(matches)!=1:reaches.append(dict(id=int(row.Reach_ID),status='hold',reason='Unknown downstream identity'));continue
            end=ends[matches[0]];start=ends[1-matches[0]];distance,i=tree.query([start.x,start.y]);path,term=h.trace_d8(drain,(sr[i],sc[i]));coords=[rio.transform.xy(transform,*q) for q in path];approach=min(Point(x).distance(end) for x in coords);samples=[row.geometry.interpolate(s) for s in np.r_[np.arange(0,row.geometry.length,policy['old_spacing_m']),row.geometry.length]];near=tree.query([[q.x,q.y] for q in samples])[0];sample_rc=[rio.transform.rowcol(transform,q.x,q.y) for q in samples];outside=sum(not (0<=rr<data.shape[0] and 0<=cc<data.shape[1]) for rr,cc in sample_rc);fraction=float((near<=policy['old_tolerance_m']).mean());ok=outside==0 and distance<=policy['old_tolerance_m'] and approach<=policy['old_tolerance_m'] and fraction>=policy['old_min_matched_fraction'] and term not in ('sink','cycle','missing');reaches.append(dict(id=int(row.Reach_ID),status='pass' if ok else 'hold',upstream_snap_m=float(distance),downstream_approach_m=float(approach),matched_fraction=fraction,outside_samples=outside,next_down=int(row.Next_down),downstream_endpoint_index=matches[0],termination=term));lines.append(dict(id=int(row.Reach_ID),geometry=LineString(coords)))
        frame=gpd.GeoDataFrame(lines,crs=32645);frame.to_file(root/'old-flow-paths.gpkg',index=False);assert gpd.read_file(root/'old-flow-paths.gpkg').geometry.equals(frame.geometry)
        polygons=[shape(geom) for geom,v in shapes(candidate.astype('uint8'),mask=candidate,transform=transform) if v==1];polygon_frame=gpd.GeoDataFrame({'role':['D8 candidate']*len(polygons)},geometry=polygons,crs=32645);polygon_frame.to_file(root/'candidate-basin.gpkg',index=False);back=gpd.read_file(root/'candidate-basin.gpkg');assert back.crs==polygon_frame.crs and back.geometry.equals(polygon_frame.geometry) and back.role.tolist()==polygon_frame.role.tolist()
        fig,axs=plt.subplots(1,2,figsize=(14,7));extent=(l,r,b,t)
        axs[0].imshow(data,extent=extent,cmap='terrain');basin.boundary.plot(ax=axs[0],color='cyan',linewidth=2);gpd.GeoSeries(polygons,crs=32645).boundary.plot(ax=axs[0],color='red');rivers.plot(ax=axs[0],color='blue',linewidth=.5);axs[0].scatter([xx,snapxy[0]],[yy,snapxy[1]],c=['black','red'],marker='x');axs[0].set_title(f'{res}m DSM; cyan reference / red D8 basin\nIoU {metrics["iou"]:.4f}; candidate only')
        axs[1].imshow(np.log1p(np.abs(acc)),extent=extent,cmap='Greys');old.plot(ax=axs[1],color='blue');frame.plot(ax=axs[1],color='orange');axs[1].set_title('Old frozen rivers blue / D8 orange\nNegative accumulation remains unknown boundary inflow')
        for ax in axs:ax.set_xlim(l,r);ax.set_ylim(b,t);ax.set_xlabel('EPSG:32645 Easting (m)');ax.set_ylabel('Northing (m)')
        fig.tight_layout();fig.savefig(root/'qa.png',dpi=140);plt.close(fig)
        report=dict(resolution_m=res,status='pass' if passed and all(q['status']=='pass' for q in reaches) else 'hold',basin_gate_status='pass' if passed else 'hold',basin=metrics,raw_unsnapped_basin=raw_metrics,outlet_boundary_identity_distance_m=identity_distance,outlet_identity_status='pass' if identity_pass else 'hold',outlet_reference=policy['outlet_reference'],outlet_snapped=snapxy,snap_m=float(dist[snap]),old_reaches=reaches,cli_seconds=elapsed,dem_sha256=digest(dem),independent_catchment_readback=True);write_json(root/'acceptance.json',report);reports.append(report)
    assert implementation_before==digest(__file__)
    assert inputs=={k:digest(getattr(a,k)) for k in inputs}
    write_json(out/'acceptance.json',dict(status='pass' if all(q['status']=='pass' and all(r['status']=='pass' for r in q['old_reaches']) for q in reports) else 'hold',runs=reports,inputs=inputs,implementation=digest(__file__),wall_seconds=time.perf_counter()-started,max_rss_bytes=peak_rss_bytes(),scope=policy['limits']))

if __name__=='__main__':main()
