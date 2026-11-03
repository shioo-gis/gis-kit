#!/usr/bin/env python3
"""Real reference topology diagnostic; keeps a negative result and frozen policy."""
import argparse
import importlib.util
import json
import sys
import time
import resource
from pathlib import Path
import numpy as np
import geopandas as gpd
import rasterio as rio
from shapely.geometry import Point,LineString
from scipy.spatial import cKDTree
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
from _delivery import digest,write_json
spec=importlib.util.spec_from_file_location('hydro',S/'hydro-review.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)


def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('terrain_delivery');a.add_argument('professional_delivery');a.add_argument('gloric');a.add_argument('output');v=a.parse_args()
    started=time.perf_counter()
    out=Path(v.output);out.mkdir(exist_ok=False,parents=True);root=Path(v.terrain_delivery);rivers=gpd.read_file(root/'inputs/reaches.gpkg')
    # Reuse the already frozen two-cell tolerance; no threshold chosen from new traces.
    p=json.loads((Path(v.professional_delivery)/'inputs/hydro-review.json').read_text(encoding='utf-8'));policy=dict(proximity=p,endpoint_identity_tolerance_m=1,flow_method='GRASS D8',direction_rule='Only a unique endpoint meeting the catalogued Next_down line identifies downstream; otherwise hold',exit_rule='All three reaches must pass proximity and directed upstream-to-downstream endpoint approach within frozen 900m; unknown, sink, cycle or missing holds')
    write_json(out/'policy.json',policy)
    # Read original local catalogue with a spatial filter, preserving Next_down source IDs.
    b=rivers.to_crs(4326).total_bounds+np.array([-.03,-.03,.03,.03]);down=gpd.read_file(v.gloric,bbox=tuple(b)).to_crs(rivers.crs);down=down[down.Reach_ID.isin(rivers.Next_down)].copy();down.to_file(out/'downstream-reference.gpkg',driver='GPKG',index=False)
    stream_path=root/'run0/hydrology/streams.tif';drain_path=root/'run0/hydrology/drainage.tif'
    with rio.open(stream_path) as d:s=d.read(1,masked=True);t=d.transform;crs=d.crs;bounds=d.bounds
    with rio.open(drain_path) as d:
        assert d.transform==t and d.crs==crs and d.shape==s.shape;drain=d.read(1,masked=True)
    rr,cc=np.where((~np.ma.getmaskarray(s))&(s.data>0));x,y=rio.transform.xy(t,rr,cc);tree=cKDTree(np.column_stack((x,y)));records=[];paths=[]
    proximity=json.loads((Path(v.professional_delivery)/'run0/hydro-review/record.json').read_text(encoding='utf-8'));pr={q['source_id']:q for q in proximity['reaches']}
    for row in rivers.itertuples():
        ends=[Point(row.geometry.coords[0]),Point(row.geometry.coords[-1])];following=down[down.Reach_ID==row.Next_down];key=str(row.Reach_ID)
        matches=[i for i,e in enumerate(ends) if not following.empty and following.distance(e).min()<=1]
        if len(matches)!=1:records.append(dict(source_id=key,status='hold',reason='Downstream endpoint ambiguous or missing'));continue
        end=ends[matches[0]];start=ends[1-matches[0]];snap,index=tree.query([start.x,start.y]);path,termination=h.trace_d8(drain,(rr[index],cc[index]));xy=[rio.transform.xy(t,r,c) for r,c in path];distance=min(Point(q).distance(end) for q in xy)
        passed=snap<=p['tolerance_m'] and distance<=p['tolerance_m'] and termination not in ('cycle','missing','sink') and pr[key]['status']=='pass'
        records.append(dict(source_id=key,next_down=str(row.Next_down),downstream_endpoint_index=matches[0],upstream_snap_m=float(snap),flow_approach_to_downstream_m=float(distance),termination=termination,cells=len(path),status='pass' if passed else 'hold'))
        if len(xy)>1:paths.append(dict(source_id=key,geometry=LineString(xy)))
    frame=gpd.GeoDataFrame(paths,geometry='geometry',crs=crs);frame.to_file(out/'flow-paths.gpkg',driver='GPKG',index=False)
    back=gpd.read_file(out/'flow-paths.gpkg');assert back.source_id.tolist()==frame.source_id.tolist() and back.geometry.equals(frame.geometry)
    fig,ax=plt.subplots(figsize=(9,8));ax.scatter(x,y,s=3,color='#aaaaaa',label='Candidate stream pixel centres');rivers.plot(ax=ax,color='blue',linewidth=2,label='GloRiC reference');frame.plot(ax=ax,color='orange',linewidth=1.4,label='Traced D8 flow');down.plot(ax=ax,color='purple',linewidth=1,label='Catalogued downstream reaches')
    samples=gpd.read_file(Path(v.professional_delivery)/'run0/hydro-review/samples.gpkg');bad=samples[~samples.within_tolerance];bad.plot(ax=ax,color='red',markersize=12,label='Reference sample >900m')
    for row in rivers.itertuples():
        middle=row.geometry.interpolate(.5,normalized=True);ax.text(middle.x,middle.y,str(row.Reach_ID),fontsize=8,color='navy')
        record=next(q for q in records if q['source_id']==str(row.Reach_ID))
        if 'downstream_endpoint_index' in record:
            ends=[Point(row.geometry.coords[0]),Point(row.geometry.coords[-1])];end=ends[record['downstream_endpoint_index']];start=ends[1-record['downstream_endpoint_index']]
            ax.scatter([start.x],[start.y],marker='s',s=30,facecolors='none',edgecolors='navy');ax.scatter([end.x],[end.y],marker='v',s=30,color='navy')
    ax.set_xlim(bounds.left,bounds.right);ax.set_ylim(bounds.bottom,bounds.top);ax.legend(loc='best');ax.set_title('Coarse DEM / real river diagnostic; orange paths are candidates\nBlue square: upstream endpoint; triangle: downstream endpoint');ax.set_xlabel('EPSG:32645 easting');ax.set_ylabel('northing');fig.tight_layout();fig.savefig(out/'qa.png',dpi=150);plt.close(fig)
    write_json(out/'record.json',dict(status='pass' if all(x['status']=='pass' for x in records) else 'hold',reaches=records,policy=policy,resources=dict(wall_seconds=time.perf_counter()-started,max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)),inputs={str(f):digest(f) for f in [root/'inputs/reaches.gpkg',stream_path,drain_path]},gloric_sidecar_hashes={f.name:digest(f) for f in Path(v.gloric).parent.glob(Path(v.gloric).stem+'.*')},limitations=['Directed endpoint approach and proximity, not a basin-boundary or surveyed river truth test','Coarse grid and dataset registration can change drainage identity; no snapping threshold adjustment after review']))


if __name__=='__main__':main()
