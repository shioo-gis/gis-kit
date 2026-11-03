#!/usr/bin/env python3
"""Explicit TPS candidate with frozen affine checks, sampled fold gates and masked warp."""
import argparse
import importlib.util
import json
from pathlib import Path
import time
import os
import shutil
import subprocess
import resource
import sys
import numpy as np
from PIL import Image,ImageDraw
import rasterio as rio
from rasterio.control import GroundControlPoint
from rasterio.transform import GCPTransformer,from_origin
from rasterio.warp import Resampling
from rasterio.enums import ColorInterp
from shapely import intersects_xy,segmentize,transform as transform_geometry
from shapely.geometry import box,Point
from _delivery import bundle,digest,write_json
from _safe_io import iter_vector_chunks

spec=importlib.util.spec_from_file_location('affine_georef',Path(__file__).with_name('raster-georef.py'));affine=importlib.util.module_from_spec(spec);spec.loader.exec_module(affine)
from _budget import check_vector


def transformer(report):
    return GCPTransformer([GroundControlPoint(row=p['pixel'][1]+.5,col=p['pixel'][0]+.5,x=p['target_world'][0],y=p['target_world'][1]) for p in report['points'] if p['role']=='fit'],tps=True)


def forward(tr,xy):
    xy=np.asarray(xy,float);x,y=tr.xy(xy[:,1]+.5,xy[:,0]+.5,offset='ul');return np.column_stack((x,y))


def inverse(tr,xy):
    xy=np.asarray(xy,float);r,c=tr.rowcol(xy[:,0],xy[:,1],op=lambda x:x);return np.column_stack((c,r))-.5


def vector_handoff(pixel_vectors, pixel_manifest, step, base, stage):
    """Transform already curve-sampled annotation exports, never Bezier handles."""
    import geopandas as gpd
    import pyogrio
    from pandas.testing import assert_frame_equal
    manifest_hash=digest(pixel_manifest)
    meta=json.loads(Path(pixel_manifest).read_text(encoding='utf-8'))
    tolerance=meta.get('sampling_tolerance_pixels')
    if (meta.get('source_sha256')!=base['source_sha256'] or
            meta.get('gpkg_sha256')!=digest(pixel_vectors)):
        raise ValueError('Pixel export source or GPKG hash differs')
    if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not np.isfinite(tolerance) or tolerance<=0:
        raise ValueError('Original curve sampling tolerance is required')
    if not str(meta.get('coordinate_system','')).startswith('UNREFERENCED PIXEL ENGINEERING SPACE'):
        raise ValueError('Explicit sampled pixel engineering-space export required')
    omitted=[];summaries=[];area=affine.region(base)
    with transformer(base) as tr:
        for layer,_ in pyogrio.list_layers(pixel_vectors):
            check_vector(pixel_vectors,layer);source=gpd.read_file(pixel_vectors,layer=layer,engine='pyogrio')
            if source.geometry.has_z.any():raise ValueError('Only two-dimensional sampled geometry supported')
            keep=source.geometry.apply(lambda g:g is not None and not g.is_empty and g.is_valid and area.covers(g))
            omitted.extend(dict(layer=layer,row=int(i),id=str(source.loc[i].get('id',i)),reason='outside/crossing frame, empty or invalid geometry') for i in source.index[~keep])
            frame=source.loc[keep].copy()
            if frame.empty:continue
            # Native annotation export already flattened each cubic before this stage.
            # Additional source-space segmentization limits straight chord lengths.
            if sum(frame.geometry.length)/step>1000000:raise ValueError('Vector segmentization exceeds vertex guard')
            frame.geometry=transform_geometry(segmentize(frame.geometry.array,step),lambda xy:forward(tr,xy))
            if not frame.geometry.is_valid.all():raise ValueError('Transformed geometry invalid; candidate withheld')
            frame=frame.set_crs(base['target_crs'],allow_override=True)
            frame.to_file(stage/'vectors.gpkg',layer=layer,driver='GPKG',engine='pyogrio')
            count=0
            for start,read in iter_vector_chunks(stage/'vectors.gpkg',layer=layer):
                part=frame.iloc[start:start+len(read)]
                if read.crs!=frame.crs or not np.array_equal(read.geometry.to_wkb(),part.geometry.to_wkb()):
                    raise ValueError('Vector geometry or CRS readback differs')
                assert_frame_equal(read.drop(columns=read.geometry.name).reset_index(drop=True),part.drop(columns=part.geometry.name).reset_index(drop=True),check_dtype=False)
                count+=len(read)
            if count!=len(frame):raise ValueError('Vector geometry or CRS readback differs')
            summaries.append(dict(layer=layer,count=len(frame)))
    if not summaries:raise ValueError('No sampled objects fully inside frame')
    shutil.copyfile(pixel_vectors,stage/'source-pixels.gpkg');shutil.copyfile(pixel_manifest,stage/'source-pixels.json')
    if digest(stage/'source-pixels.gpkg')!=meta['gpkg_sha256'] or digest(stage/'source-pixels.json')!=manifest_hash:raise ValueError('Pixel export changed during handoff')
    return dict(file='vectors.gpkg',sha256=digest(stage/'vectors.gpkg'),layers=summaries,omitted_objects=omitted,
                source_pixels='source-pixels.gpkg',source_manifest='source-pixels.json',source_manifest_sha256=digest(stage/'source-pixels.json'),
                original_curve_sampling_tolerance_px=tolerance,max_segment_length_source_px=step,
                unresolved_pixel_issues=meta.get('issues'),source_omitted_faces=meta.get('omitted_faces'),
                status='spatial candidate; no ground or historical validation',
                limitation='Source-space chord bound only; no claimed world-space curve tolerance')


def execute(image,gcps,p,output,backend=None,pixel_vectors=None,pixel_manifest=None,vector_step_px=None):
    backend=backend or os.environ.get("GIS_GDALWARP") or shutil.which("gdalwarp")
    if not backend:raise ValueError("Explicit existing gdalwarp required for exact TPS warp")
    if set(p)!={'grid_step_px','max_inverse_error_px','max_roundtrip_px','max_condition','resolution','resampling','rationale'} or not p['rationale']:raise ValueError('Explicit predeclared nonlinear policy required')
    for k in ('grid_step_px','max_inverse_error_px','max_roundtrip_px','max_condition','resolution'):
        if isinstance(p[k],bool) or not isinstance(p[k],(int,float)) or not np.isfinite(p[k]) or p[k]<=0:raise ValueError('Positive finite nonlinear policy values required')
    if p['resampling'] not in ('nearest','bilinear'):raise ValueError('Choose explicit nearest/bilinear')
    if any(x is not None for x in (pixel_vectors,pixel_manifest,vector_step_px)):
        if pixel_vectors is None or pixel_manifest is None or isinstance(vector_step_px,bool) or not isinstance(vector_step_px,(int,float)) or not np.isfinite(vector_step_px) or vector_step_px<=0:
            raise ValueError('Vector handoff requires sampled GPKG, manifest and positive source-pixel step')
    before=[digest(image),digest(gcps)];started=time.perf_counter()
    with bundle(output) as stage:
        affine.fit(gcps,stage/'affine.json');base=json.loads((stage/'affine.json').read_text(encoding='utf-8'));affine.verify_image(image,base);area=affine.region(base)
        if base['fit']['count']<6 or base['check']['count']<4:raise ValueError('TPS requires >=6 fit and >=4 frozen check points')
        pixels=np.array([x['pixel'] for x in base['points']]);worlds=np.array([x['target_world'] for x in base['points']]);checks=np.array([x['role']=='check' for x in base['points']])
        with Image.open(image) as im:rgba=np.array(im.convert('RGBA'))
        height,width=rgba.shape[:2]
        if not box(-.5,-.5,width-.5,height-.5).covers(area):raise ValueError('Frame outside image')
        xmin,ymin,xmax,ymax=area.bounds;xs=np.arange(xmin,xmax,p['grid_step_px']);ys=np.arange(ymin,ymax,p['grid_step_px'])
        if len(xs)*len(ys)>1000000:raise ValueError('Fold diagnostic grid exceeds guard')
        yy,xx=np.meshgrid(ys,xs,indexing='ij');sample=np.column_stack((xx.ravel(),yy.ravel()));sample=sample[intersects_xy(area,sample[:,0],sample[:,1])]
        sample=np.vstack((sample,pixels))
        with transformer(base) as tr:
            pred=forward(tr,pixels);err=np.linalg.norm(pred-worlds,axis=1);reverse=inverse(tr,worlds);inv_err=np.linalg.norm(reverse-pixels,axis=1)
            f=forward(tr,sample);roundtrip=np.linalg.norm(inverse(tr,f)-sample,axis=1)
            dx=forward(tr,sample+[.5,0])-forward(tr,sample-[.5,0]);dy=forward(tr,sample+[0,.5])-forward(tr,sample-[0,.5]);jac=np.stack((dx,dy),axis=2)
            if not np.isfinite(jac).all():raise ValueError('Nonfinite TPS Jacobian; candidate withheld')
            determinants=np.linalg.det(jac);condition=np.linalg.cond(jac);sign=np.sign(np.linalg.det(np.array(base['matrix'])[:,:2]))
            dense=np.array(segmentize(area.exterior,1).coords);boundary=forward(tr,dense)
        if not all(np.isfinite(x).all() for x in (pred,reverse,roundtrip,jac,boundary)):raise ValueError('Nonfinite TPS result')
        rmse=float(np.sqrt(np.mean(err[checks]**2)));maximum=float(err[checks].max());policy=base['acceptance_policy'];reasons=[]
        if not policy:reasons.append('Missing source acceptance policy')
        else:
            if rmse>policy['max_check_rmse'] or maximum>policy['max_check_error']:reasons.append('Frozen world check threshold exceeded')
            if base['coverage']['fit_fraction']+1e-12<policy['min_fit_coverage']:reasons.append('Fit hull coverage below policy')
        if inv_err[checks].max()>p['max_inverse_error_px']:reasons.append('Frozen inverse check threshold exceeded')
        if roundtrip.max()>p['max_roundtrip_px']:reasons.append('Inverse roundtrip threshold exceeded')
        if (determinants*sign<=0).any():reasons.append('Sampled fold or singularity detected')
        if condition.max()>p['max_condition']:reasons.append('Sampled local anisotropy threshold exceeded')
        report=dict(method='GDAL TPS',status='hold' if reasons else 'pass',reasons=reasons,source_sha256=before[0],gcps_sha256=before[1],source_gcp_input=base['gcp_input'],affine_check=base['check'],check=dict(rmse=rmse,max=maximum,inverse_max_px=float(inv_err[checks].max())),diagnostics=dict(sample_count=len(sample),roundtrip_max_px=float(roundtrip.max()),max_condition=float(condition.max()),fold_count=int((determinants*sign<=0).sum())),policy=p,coverage=base['coverage'],points=[dict(id=x['id'],role=x['role'],pixel=x['pixel'],world=x['target_world'],predicted=w.tolist(),world_error=float(e),inverse_pixel=q.tolist()) for x,w,e,q in zip(base['points'],pred,err,reverse)],limitations=['Sampled Jacobian is not a proof against sub-grid folding','GCP inverse is GDAL approximate reverse transform, independently checked','Printed-grid geometric fit, not surveyed ground accuracy','No automatic promotion of historical objects; no Bezier handle-only transformation'])
        # Overlay source and inverse references, with the same frozen check points.
        picture=Image.fromarray(rgba).convert('RGB');draw=ImageDraw.Draw(picture)
        draw.line(list(area.exterior.coords),fill='orange',width=3)
        for hole in area.interiors:draw.line(list(hole.coords),fill='orange',width=3)
        from shapely.geometry import shape
        draw.line(list(shape(base['coverage']['fit_hull']).exterior.coords),fill='green',width=2)
        outside=shape(base['coverage']['extrapolation_region'])
        for part in getattr(outside,'geoms',[outside]):
            if not part.is_empty and part.geom_type=='Polygon':draw.line(list(part.exterior.coords),fill='purple',width=2)
        for record in report['points']:
            x,y=record['pixel'];u,v=record['inverse_pixel'];colour='blue' if record['role']=='check' else 'red'
            draw.ellipse((x-5,y-5,x+5,y+5),outline=colour,width=2);draw.line((x,y,u,v),fill='cyan',width=2);draw.text((x+7,y),record['id'],fill=colour)
        picture.thumbnail((1500,1500));picture.save(stage/'check-overlay.png')
        if not reasons:
            # Strict frame alpha; original image stays immutable. Warp actual samples, not curve handles.
            yy,xx=np.indices((height,width));rgba[:,:,3][~intersects_xy(area,xx,yy)]=0
            minx,miny=boundary.min(axis=0);maxx,maxy=boundary.max(axis=0);res=p['resolution'];w=int(np.ceil((maxx-minx)/res));h=int(np.ceil((maxy-miny)/res))
            if w*h>20000000:raise ValueError('Warp target exceeds pixel guard')
            target=from_origin(minx,maxy,res,res)
            controls=[GroundControlPoint(row=x['pixel'][1]+.5,col=x['pixel'][0]+.5,x=x['target_world'][0],y=x['target_world'][1]) for x in base['points'] if x['role']=='fit']
            # Rasterio 1.4 reproject hardcodes a 0.125 px approximation for GCPs.
            # Native gdalwarp -et 0 disables this approximation; never weaken pixel checks.
            source=stage/'warp-source.tif'
            with rio.open(source,'w',driver='GTiff',width=width,height=height,count=4,dtype='uint8',gcps=controls,crs=base['target_crs']) as src:
                src.write(rgba.transpose(2,0,1));src.colorinterp=(ColorInterp.red,ColorInterp.green,ColorInterp.blue,ColorInterp.alpha)
            command=[backend,'-tps','-et','0','-t_srs',base['target_crs'],'-te',str(minx),str(maxy-h*res),str(minx+w*res),str(maxy),'-ts',str(w),str(h),'-r',p['resampling'],'-srcalpha','-dstalpha','-co','COMPRESS=DEFLATE',str(source),str(stage/'warped.tif')]
            run=subprocess.run(command,capture_output=True,text=True)
            if run.returncode:raise ValueError('Explicit gdalwarp failed; no result published')
            source.unlink()
            report['warp_backend']=dict(version=subprocess.check_output([backend,'--version'],text=True).strip(),approximation_error_px=0)
            with rio.open(stage/'warped.tif') as d:
                if d.count!=4 or d.dtypes!=('uint8',)*4 or d.crs!=rio.crs.CRS.from_user_input(base['target_crs']) or d.colorinterp!=(ColorInterp.red,ColorInterp.green,ColorInterp.blue,ColorInterp.alpha):
                    raise ValueError('Warp CRS, bands, dtype or alpha contract differs')
                valid=0
                for _,win in d.block_windows(4):
                    alpha=d.read(4,window=win)
                    if not np.array_equal(d.dataset_mask(window=win),alpha):raise ValueError('Warp dataset mask differs from alpha')
                    valid+=int((alpha>0).sum())
                actual=d.transform
                for corner in ((0,0),(w,0),(0,h),(w,h)):
                    pixel=(~target)*(actual*corner)
                    if max(abs(pixel[i]-corner[i]) for i in (0,1))>1e-9:raise ValueError('Warp grid shifted beyond 1e-9 pixel roundoff')
                target=actual
            if not valid:raise ValueError('Warp produced no valid pixels')
            report['warp']=dict(file='warped.tif',sha256=digest(stage/'warped.tif'),valid_pixels=valid,shape=[h,w],transform=list(target)[:6])
        if pixel_vectors is not None and not reasons:
            report['vector_handoff']=vector_handoff(pixel_vectors,pixel_manifest,vector_step_px,base,stage)
        report['pixel_convention']=base['pixel_convention'];report['target_crs']=base['target_crs'];report['target_units']=base['target_units']
        report['transform_record']='affine.json';report['transform_record_sha256']=digest(stage/'affine.json');report['affine_implementation_sha256']=digest(affine.__file__)
        if before!=[digest(image),digest(gcps)]:raise ValueError('Source or frozen control points changed')
        report['preferred_model_by_frozen_check_rmse']='affine' if base['check']['rmse']<=rmse else 'tps';report['automatic_promotion']=False
        report['completed_child_max_rss_bytes']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*(1 if sys.platform=='darwin' else 1024);report['max_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024);report['wall_seconds']=time.perf_counter()-started;report['implementation']=digest(__file__);write_json(stage/'record.json',report)
    return report


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('image');a.add_argument('gcps');a.add_argument('--backend-gdalwarp');a.add_argument('--params',required=True);a.add_argument('--output',required=True);a.add_argument('--pixel-vectors');a.add_argument('--pixel-manifest');a.add_argument('--vector-step-px',type=float);v=a.parse_args()
    try:execute(v.image,v.gcps,json.loads(Path(v.params).read_text(encoding='utf-8')),v.output,v.backend_gdalwarp,v.pixel_vectors,v.pixel_manifest,v.vector_step_px)
    except (ValueError,KeyError,TypeError,OSError) as e:a.exit(1,f'ERROR: {e}\n')
