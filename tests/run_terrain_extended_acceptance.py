#!/usr/bin/env python3
"""Local geo-assets -> bounded raster -> real CLIs -> readback and visual evidence."""
import argparse
import hashlib
import json
import os
import resource
from pathlib import Path
import shutil
import subprocess
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.windows import from_bounds, Window
from shapely.geometry import box
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(SCRIPTS))
from _delivery import digest,write_json


def main():
    started=time.perf_counter()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('assets');p.add_argument('output');p.add_argument('--grass',required=True);args=p.parse_args()
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False);assets=Path(args.assets).resolve()
    (out/'inputs').mkdir();run_times=[]
    def cli(script,argv,tag):
        start=time.perf_counter();result=subprocess.run([sys.executable,str(SCRIPTS/script),*map(str,argv)],capture_output=True,encoding='utf-8', errors='replace',env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        (out/f'{tag}.log').write_text(result.stdout+result.stderr, encoding='utf-8');result.check_returncode();run_times.append({'task':tag,'seconds':time.perf_counter()-start})
    src=assets/'datasets/terrain/gebco/2023/curated/gebco_2023_Global.tif'
    before=digest(src)
    manifest=assets/'datasets/terrain/gebco/2023/manifest.json';shutil.copy2(manifest,out/'inputs/gebco-manifest.json')
    with rio.open(src) as ds:
        w=from_bounds(85.3,28.2,85.6,28.4,ds.transform).round_offsets().round_lengths()
        z=ds.read(1,window=w,masked=True).astype(float).filled(np.nan);profile=ds.profile.copy();profile.update(width=z.shape[1],height=z.shape[0],transform=ds.window_transform(w),dtype='float64',nodata=np.nan,driver='GTiff')
        with rio.open(out/'inputs/geographic.tif','w',**profile) as d:d.write(z,1)
    write_json(out/'warp.json',dict(crs='EPSG:32645',kind='continuous',resampling='bilinear',resolution=450))
    cli('raster.py',['warp',out/'inputs/geographic.tif','--params',out/'warp.json','--output',out/'warped'],'warp')
    with rio.open(out/'warped/result.tif') as ds:
        a=ds.read(1,masked=True).filled(np.nan)
        # Conservative rectangular inset: crop away incomplete reprojection margins, never fill them.
        border=0
        while border*2<min(a.shape)-3:
            b=a[border:a.shape[0]-border, border:a.shape[1]-border]
            if np.isfinite(b).all():break
            border+=1
        if not np.isfinite(b).all():raise ValueError('No complete interior raster')
        w=Window(border,border,b.shape[1],b.shape[0]);profile=ds.profile.copy();profile.update(width=b.shape[1],height=b.shape[0],transform=ds.window_transform(w))
        dem=out/'inputs/dem.tif'
        with rio.open(dem,'w',**profile) as d:d.write(b,1);d.set_band_unit(1,'metre')
        t=profile['transform'];crs=profile['crs'];bounds=rio.transform.array_bounds(*b.shape,t)
    shp=assets/'datasets/hydrography/gloric/1.0/curated/GloRiC_v10_shapefile/GloRiC_v10.shp'
    shutil.copy2(assets/'datasets/hydrography/gloric/1.0/manifest.json',out/'inputs/gloric-manifest.json')
    river_files=sorted(shp.parent.glob(shp.stem+'.*'));river_hashes={f.name:digest(f) for f in river_files if f.is_file()}
    rivers=gpd.read_file(shp,bbox=(85.3,28.2,85.6,28.4)).to_crs(crs)
    rivers=rivers[rivers.geometry.within(box(*bounds))].copy()
    # Longest three full stored reaches, deterministic ties; no hand-drawn river geometry.
    rivers['selection_length_m']=rivers.length;rivers=rivers.sort_values(['selection_length_m','Reach_ID'],ascending=[False,True]).head(3)
    if len(rivers)!=3:raise ValueError('Insufficient fully contained real reaches')
    rivers.to_file(out/'inputs/reaches.gpkg',driver='GPKG',index=False)
    common=dict(vertical_unit='metre',vertical_datum='unknown: local GEBCO manifest upstream identity not verified',source_description='Local GEBCO 2023 labelled raster; bilinear 450m UTM crop. No ground truth or hazard interpretation.')
    levels=list(np.arange(np.ceil(b.min()/500)*500,b.max(),500).astype(float))
    write_json(out/'detail.json',dict(common,levels_m=levels,profile=dict(input=str(out/'inputs/reaches.gpkg'),id='Reach_ID',distance_m=225)))
    h,w=b.shape;observer=[dict(id='centre',x=t.c+(w//2+.5)*t.a,y=t.f+(h//2+.5)*t.e,height_m=2),dict(id='east',x=t.c+(w*3//4+.5)*t.a,y=t.f+(h//2+.5)*t.e,height_m=2)]
    write_json(out/'views.json',dict(common,observers=observer,target_height_m=0,max_distance_m=10000))
    # Explicitly snap a stored endpoint of the longest real reach; orientation is not assumed.
    reach=rivers.iloc[0].geometry
    if reach.geom_type=='MultiLineString':reach=list(reach.geoms)[-1]
    original_outlet=list(reach.coords[-1])[:2];oc,orr=(~t)*tuple(original_outlet)
    outlet=[t.c+(int(np.floor(oc))+.5)*t.a,t.f+(int(np.floor(orr))+.5)*t.e]
    write_json(out/'hydro.json',dict(common,flow_method='D8',threshold_cells=20,outlet=outlet))
    hashes=[];reports=[]
    for i in range(2):
        run=out/f'run{i}';run.mkdir()
        cli('terrain-detail.py',[dem,'--params',out/'detail.json','--output',run/'detail'],f'detail{i}')
        for op,param in [('hydrology','hydro.json'),('viewshed','views.json')]:
            cli('terrain-backend.py',[op,dem,'--params',out/param,'--output',run/op,'--grass',args.grass],f'{op}{i}')
        current={}
        for f in sorted(run.glob('*/*.tif')):
            with rio.open(f) as ds:
                a=ds.read(1);assert ds.crs==crs and ds.transform==t and a.shape==b.shape
            current[str(f.relative_to(run))]=hashlib.sha256(a.astype('<f8').tobytes()).hexdigest()
        for f in sorted((run/'detail').glob('*.gpkg')):
            g=gpd.read_file(f);assert g.crs==crs and g.geometry.is_valid.all()
            canonical=g.drop(columns=g.geometry.name).fillna('NULL').astype(str).to_dict('records')
            current[f.name]=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()+b''.join(g.geometry.normalize().to_wkb())).hexdigest()
        hashes.append(current);reports.extend(json.loads(f.read_text(encoding='utf-8'))['resources'] for f in run.glob('*/record.json'))
    assert hashes[0]==hashes[1]
    points=gpd.read_file(out/'run0/detail/profile.gpkg')
    for row in points.itertuples():
        c,r=(~t)*(row.geometry.x,row.geometry.y);v=b[int(np.floor(r)),int(np.floor(c))]
        assert row.sample_status=='valid' and row.elevation_m==v
    lines=gpd.read_file(out/'run0/detail/contours.gpkg')
    contour_error=0.
    for line,level in zip(lines.geometry,lines.elevation_m):
        for x,y in line.coords:
            c,r=(~t)*(x,y);c-=.5;r-=.5
            ci=min(max(int(np.floor(c)),0),w-2);ri=min(max(int(np.floor(r)),0),h-2)
            fx,fy=c-ci,r-ri
            value=(1-fy)*((1-fx)*b[ri,ci]+fx*b[ri,ci+1])+fy*((1-fx)*b[ri+1,ci]+fx*b[ri+1,ci+1])
            contour_error=max(contour_error,abs(value-level))
    assert contour_error<1e-7
    def read(name):
        with rio.open(out/f'run0/{name}.tif') as d:return d.read(1)
    drainage=read('hydrology/drainage');catchment=read('hydrology/catchment')
    # Trace each cell independently to the declared outlet, using the documented direction codes.
    moves={1:(-1,1),2:(-1,0),3:(-1,-1),4:(0,-1),5:(1,-1),6:(1,0),7:(1,1),8:(0,1)}
    oc,orr=(~t)*tuple(outlet);target=(int(orr),int(oc));expected=np.zeros(b.shape,dtype=bool)
    for r,c in np.ndindex(b.shape):
        pos=(r,c);seen=set()
        while pos not in seen:
            if pos==target:expected[r,c]=True;break
            seen.add(pos);code=int(drainage[pos])
            if code<=0:break
            dr,dc=moves[code];pos=(pos[0]+dr,pos[1]+dc)
            if not (0<=pos[0]<h and 0<=pos[1]<w):break
        else:raise AssertionError('Drainage cycle')
    assert np.array_equal(np.isfinite(catchment)&(catchment==1),expected)
    a,bv=read('viewshed/view_0'),read('viewshed/view_1');count=read('viewshed/visible_count')
    union=np.nansum(np.stack([a,bv]),axis=0);union[~(np.isfinite(a)|np.isfinite(bv))]=np.nan
    np.testing.assert_array_equal(union,count)
    fig,axes=plt.subplots(2,3,figsize=(16,9),layout='constrained');extent=[bounds[0],bounds[2],bounds[1],bounds[3]]
    for ax,arr,title,cmap in [(axes[0,0],b,'450 m DEM / contours / real GloRiC reaches','terrain'),(axes[0,1],np.log1p(np.abs(read('hydrology/accumulation'))),'log(1 + abs(accumulation)); cells','viridis'),(axes[0,2],read('hydrology/streams'),'Candidate stream segments; IDs','tab20'),(axes[1,0],catchment,'D8 catchment at diagnostic outlet','Blues'),(axes[1,1],count,'Planar visibility: observer count','viridis')]:
        kw=dict(vmin=0,vmax=2) if 'observer count' in title else dict(vmin=0,vmax=1) if 'catchment' in title else {}
        im=ax.imshow(arr,extent=extent,origin='upper',cmap=cmap,interpolation='nearest',**kw)
        cb=fig.colorbar(im,ax=ax,shrink=.75)
        if 'observer count' in title:cb.set_ticks([0,1,2])
        if 'catchment' in title:cb.set_ticks([1]);cb.set_label('Included = 1; outside = NoData')
        ax.set_title(title);ax.ticklabel_format(style='plain');ax.set_xlabel('Easting (m)');ax.set_ylabel('Northing (m)')
    lines.plot(ax=axes[0,0],color='black',linewidth=.35);rivers.plot(ax=axes[0,0],color='cyan',linewidth=1)
    axes[1,0].plot(*outlet,'rx')
    for o in observer:axes[1,1].plot(o['x'],o['y'],'rx')
    for ident,g in points.groupby('source_id'):axes[1,2].plot(g.measure_m,g.elevation_m,label=str(ident))
    axes[1,2].set(xlabel='Distance along stored reach (m)',ylabel='Containing-pixel elevation (m)',title='Real reach profiles (not surveyed)');axes[1,2].legend(fontsize=8)
    fig.suptitle('Local GEBCO + GloRiC | EPSG:32645 | Coarse terrain candidates; no hazard / historical claims')
    fig.savefig(out/'qa.png',dpi=130);plt.close(fig)
    assert digest(src)==before and river_hashes=={f.name:digest(f) for f in river_files if f.is_file()}
    write_json(out/'acceptance.json',dict(local_sources_only=True,acceptance_wall_seconds=time.perf_counter()-started,orchestrator_max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),dem_source_sha256=before,river_source_hashes=river_hashes,crop_bounds_wgs84=[85.3,28.2,85.6,28.4],inset_pixels=border,dem_shape=list(b.shape),source_unchanged=True,repeated_hashes=hashes,profile_readback=True,contour_vertex_elevation_max_error_m=contour_error,outlet_selection=dict(reach_id=int(rivers.iloc[0].Reach_ID),original=original_outlet,snapped=outlet,interpretation='stored reach endpoint; not surveyed outlet'),catchment_direction_trace=True,visibility_count_readback=True,cli_times=run_times,resources=reports,visual_review='pending',limits=['GEBCO upstream identity and vertical datum not independently verified','450m interpolated grid cannot validate river alignment or fine-scale visibility','No acceleration claim']))
    print(out)


if __name__=='__main__':main()
