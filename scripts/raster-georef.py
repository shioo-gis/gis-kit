#!/usr/bin/env python3
# /// script
# dependencies = ['numpy>=1.24', 'pyproj>=3.6', 'rasterio>=1.3', 'pillow>=9.1', 'geopandas>=1.0', 'pyogrio>=0.10', 'shapely>=2.0']
# ///
"""Fit checked affine pixel-center georeferencing; attach without resampling."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer
from _model_actor import validate_actor
from _budget import check_image, check_vector


def write(path,value):
    payload=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    with Path(path).open('x',encoding='utf-8') as f:
        f.write(payload)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def region(report):
    from shapely.geometry import shape
    frame=report.get('frame')
    if not frame or not isinstance(frame.get('id'),str) or not frame['id'].strip():
        raise ValueError('An explicit frame id and pixel-region Polygon are required')
    geometry=shape(frame['pixel_region'])
    if geometry.geom_type!='Polygon' or geometry.is_empty or not geometry.is_valid or not np.isfinite(geometry.bounds).all():
        raise ValueError('Frame must be a finite valid nonempty Polygon (holes may exclude insets)')
    return geometry


def export_gate(report, allow_hold):
    region(report)
    if report.get('acceptance',{}).get('status')!='pass' and not allow_hold:
        raise ValueError('Georeferencing is on hold; review reasons or explicitly use --allow-hold for diagnostics')


def handoff(output, transform, report, **extra):
    record={'schema_version':1,'status':'spatial candidate; no historical validation',
            'source_sha256':report['source_sha256'],
            'transform':{'path':str(Path(transform).resolve()),'sha256':digest(transform),'report':report},
            'output':{'path':str(Path(output).resolve()),'sha256':digest(output)},**extra}
    path=str(output)+'.manifest.json'
    write(path,record)
    return path


def fit(path, output):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    validate_actor(data.get('actor'))
    if not data.get('coordinate_reference') or not data.get('source_sha256'):
        raise ValueError('Coordinate reference evidence and original image SHA256 are required')
    source_crs=CRS.from_user_input(data['source_crs'])
    target_crs=CRS.from_user_input(data['target_crs'])
    if not target_crs.is_projected:
        raise ValueError('Affine fitting requires an explicit projected target CRS')
    area=region(data)
    points=data['points']
    ids=[p['id'] for p in points]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate GCP IDs')
    if any(p.get('role') not in ('fit','check') for p in points):
        raise ValueError('Each GCP must be assigned fit/check before fitting')
    pixels=np.array([p['pixel'] for p in points],dtype=float)
    worlds=np.array([p['world'] for p in points],dtype=float)
    if pixels.shape != (len(points),2) or worlds.shape != pixels.shape or not np.isfinite(pixels).all() or not np.isfinite(worlds).all():
        raise ValueError('GCP coordinates must be finite pairs')
    if len({tuple(p) for p in pixels})!=len(points):
        raise ValueError('Duplicate pixel coordinates, including fit/check leakage')
    from shapely.geometry import Point, MultiPoint, mapping
    if any(not area.covers(Point(p)) for p in pixels):
        raise ValueError('All control/check points must belong to this frame')
    transform=Transformer.from_crs(source_crs,target_crs,always_xy=True)
    worlds=np.column_stack(transform.transform(worlds[:,0],worlds[:,1]))
    if not np.isfinite(worlds).all():
        raise ValueError('Projection produced nonfinite coordinates')
    selected=np.array([p['role']=='fit' for p in points])
    if sum(selected)<3 or sum(~selected)<1:
        raise ValueError('Need >=3 noncollinear fit points and >=1 independent check point')
    center=pixels[selected].mean(axis=0)
    scale=pixels[selected].std(axis=0)
    if np.any(scale==0):
        raise ValueError('Degenerate control-point distribution')
    design=np.column_stack(((pixels-center)/scale,np.ones(len(points))))
    coefficients,_,rank,_=np.linalg.lstsq(design[selected],worlds[selected],rcond=None)
    if rank!=3 or np.linalg.cond(design[selected])>1e8:
        raise ValueError('Rank-deficient or ill-conditioned control-point geometry')
    linear=coefficients[:2]/scale[:,None]
    offset=coefficients[2]-center@linear
    matrix=np.column_stack((linear.T,offset))
    if abs(np.linalg.det(linear)) < 1e-15:
        raise ValueError('Noninvertible fitted transform')
    predicted=pixels@linear+offset
    errors=np.linalg.norm(predicted-worlds,axis=1)
    def stats(mask):
        return {'count':int(sum(mask)),'rmse':float(np.sqrt(np.mean(errors[mask]**2))), 'max':float(max(errors[mask]))}
    hull=MultiPoint(pixels[selected]).convex_hull
    outside=area.difference(hull)
    coverage=1-outside.area/area.area
    policy=data.get('acceptance_policy')
    reasons=[]
    if policy is None:
        reasons.append('No predeclared acceptance policy')
    else:
        for key in ('max_check_rmse','max_check_error','min_fit_coverage'):
            value=policy.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or value<0:
                raise ValueError('Acceptance thresholds must be finite nonnegative numbers')
        if policy['min_fit_coverage']>1 or not str(policy.get('rationale','')).strip():
            raise ValueError('Coverage must be <=1 and threshold rationale is required')
        if stats(~selected)['rmse']>policy['max_check_rmse']: reasons.append('Check RMSE exceeds threshold')
        if stats(~selected)['max']>policy['max_check_error']: reasons.append('Check maximum exceeds threshold')
        if coverage+1e-12<policy['min_fit_coverage']: reasons.append('Fit-point coverage below threshold')
    result={'method':'affine','pixel_convention':'top-left pixel center=(0,0); x right; y down',
            'matrix':matrix.tolist(),'source_sha256':data['source_sha256'],
            'gcp_path':str(Path(path).resolve()),'gcp_input':data,
            'gcp_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'source_crs':source_crs.to_wkt(),'target_crs':target_crs.to_wkt(),
            'coordinate_reference':data['coordinate_reference'],
            'target_units':target_crs.axis_info[0].unit_name,
            'fit':stats(selected),'check':stats(~selected),
            'points':[{**p,'target_world':w.tolist(),'residual':float(e)} for p,w,e in zip(points,worlds,errors)],
            'frame':data['frame'],'acceptance_policy':policy,
            'coverage':{'fit_fraction':coverage,'fit_hull':mapping(hull),'extrapolation_region':mapping(outside)},
            'acceptance':{'status':'hold' if reasons else 'pass','reasons':reasons,
                          'scope':'declared geometric thresholds only; not historical validation'},
            'limitations':['affine only; not suitable for every historical projection or distorted scan',
                          'check points must be visually verified; more distributed checks are recommended']}
    write(output,result)
    return {k:result[k] for k in ('method','fit','check','target_units','acceptance')}


def verify_image(image, report):
    h=hashlib.sha256()
    with Path(image).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=report['source_sha256']:
        raise ValueError('Georeferencing belongs to a different image')


def attach(image, transform, output, allow_hold=False):
    import rasterio
    from affine import Affine
    report=json.loads(Path(transform).read_text(encoding='utf-8'))
    verify_image(image,report)
    export_gate(report,allow_hold)
    if Path(output).exists() or Path(str(output)+'.manifest.json').exists():
        raise ValueError('Output exists')
    a,b,c=report['matrix'][0]
    d,e,f=report['matrix'][1]
    # GDAL addresses the upper-left pixel corner; our model addresses its center.
    affine=Affine(a,b,c-(a+b)/2,d,e,f-(d+e)/2)
    with rasterio.open(image) as src:
        from shapely.geometry import box
        area=region(report)
        if not box(-.5,-.5,src.width-.5,src.height-.5).covers(area):
            raise ValueError('Frame extends beyond image footprint')
        profile=src.profile.copy()
        profile.update(driver='GTiff',crs=report['target_crs'],transform=affine)
        profile.pop('photometric',None)
        with rasterio.open(output,'w',**profile) as dst:
            for _,window in src.block_windows(1):
                dst.write(src.read(window=window),window=window)
                from shapely import intersects_xy
                yy,xx=np.mgrid[int(window.row_off):int(window.row_off+window.height),int(window.col_off):int(window.col_off+window.width)]
                mask=src.dataset_mask(window=window)
                mask[~intersects_xy(area,xx,yy)]=0
                dst.write_mask(mask,window=window)
            dst.colorinterp=src.colorinterp
            if src.count==1:
                try:dst.write_colormap(1,src.colormap(1))
                except ValueError:pass
            dst.update_tags(annotation_status='candidate',source_sha256=report['source_sha256'],
                            georef_method='affine-no-resampling')
    manifest=handoff(output,transform,report,excluded_pixels='outside frame masked; original pixels retained')
    return {'output':str(Path(output).resolve()),'manifest':manifest,'resampled':False,'acceptance':report['acceptance']}


def preview(image, transform, output, max_size=1800):
    from PIL import Image, ImageDraw
    report=json.loads(Path(transform).read_text(encoding='utf-8'))
    verify_image(image,report)
    if Path(output).exists() or max_size <= 0:
        raise ValueError('Output exists or max-size is not positive')
    with Image.open(image) as im:
        check_image(image,im.width,im.height,bytes_per_pixel=8)  # the decode and its RGB copy
        picture=im.convert('RGB')
    width,height=picture.size
    ratio=min(1,max_size/max(width,height))
    size=(max(1,round(width*ratio)),max(1,round(height*ratio)))
    picture=picture.resize(size,Image.Resampling.LANCZOS)
    sx,sy=width/size[0],height/size[1]
    def screen(p):return ((p[0]+.5)/sx-.5,(p[1]+.5)/sy-.5)
    # Outline declared frame, fit hull and extrapolation separately from residuals.
    from shapely.geometry import shape
    overlay=Image.new('RGBA',picture.size,(0,0,0,0))
    ink=ImageDraw.Draw(overlay)
    def outlines(geometry,color):
        parts=list(geometry.geoms) if hasattr(geometry,'geoms') else [geometry]
        for part in parts:
            if part.is_empty or part.geom_type!='Polygon': continue
            for ring in [part.exterior]+list(part.interiors):
                ink.line([screen(p) for p in ring.coords],fill=color,width=2)
    outlines(region(report),(255,140,0,230))
    outlines(shape(report['coverage']['fit_hull']),(0,180,50,230))
    outlines(shape(report['coverage']['extrapolation_region']),(190,0,190,190))
    picture=Image.alpha_composite(picture.convert('RGBA'),overlay).convert('RGB')
    draw=ImageDraw.Draw(picture)
    matrix=np.array(report['matrix'])
    inverse=np.linalg.inv(matrix[:,:2])
    for p in report['points']:
        x,y=screen(p['pixel'])
        predicted=inverse@(np.array(p['target_world'])-matrix[:,2])
        px,py=screen(predicted)
        color='red' if p['role']=='fit' else 'blue'
        draw.line([(x-4,y),(x+4,y)],fill=color,width=1)
        draw.line([(x,y-4),(x,y+4)],fill=color,width=1)
        draw.ellipse((px-3,py-3,px+3,py+3),outline='cyan',width=1)
        draw.line([(x,y),(px,py)],fill='cyan',width=1)
        draw.text((x+5,y+5),str(p['id']),fill=color)
    picture.save(output)
    return {'output':str(Path(output).resolve()),'legend':'red: fit; blue: check; cyan: reference/residual; orange: frame; green: fit hull; purple: extrapolation boundary',
            'view_to_original':{'scale':[sx,sy],'offset':[(sx-1)/2,(sy-1)/2]},'source_sha256':report['source_sha256']}


def vectors(pixel_gpkg, transform, output, manifest, allow_hold=False):
    import geopandas as gpd
    import pyogrio
    from shapely import transform as transform_geometry
    report=json.loads(Path(transform).read_text(encoding='utf-8'))
    export_gate(report,allow_hold)
    area=region(report)
    meta=json.loads(Path(manifest).read_text(encoding='utf-8'))
    verify_image(pixel_gpkg, {'source_sha256':meta['gpkg_sha256']})
    if meta['source_sha256']!=report['source_sha256']:
        raise ValueError('Vector export and transform source hashes differ')
    if Path(output).exists() or Path(str(output)+'.manifest.json').exists():
        raise ValueError('Output exists')
    a,b,c=report['matrix'][0];d,e,f=report['matrix'][1]
    linear=np.array([[a,d],[b,e]])
    offset=np.array([c,f])
    omitted=[]
    layers=[]
    for layer,_ in pyogrio.list_layers(pixel_gpkg):
        check_vector(pixel_gpkg,layer);frame=gpd.read_file(pixel_gpkg,layer=layer,engine='pyogrio')
        keep=frame.geometry.apply(lambda g:g is not None and not g.is_empty and area.covers(g))
        omitted.extend({'layer':layer,'row':int(i),'id':str(frame.loc[i].get('id',i)),
                        'reason':'outside/crossing frame or empty geometry'} for i in frame.index[~keep])
        frame=frame.loc[keep].copy()
        layers.append((layer,frame))
    if not any(len(frame) for _,frame in layers):
        raise ValueError('No objects fully inside frame')
    for layer,frame in layers:
        if frame.empty: continue
        frame.geometry=transform_geometry(frame.geometry.array,lambda coordinates:coordinates@linear+offset)
        frame=frame.set_crs(report['target_crs'],allow_override=True)
        frame.to_file(output,layer=layer,driver='GPKG',engine='pyogrio')
    final_manifest=handoff(output,transform,report,
        pixel_export={'path':str(Path(pixel_gpkg).resolve()),'sha256':digest(pixel_gpkg),
                      'manifest_path':str(Path(manifest).resolve()),'manifest_sha256':digest(manifest),'manifest':meta},
        omitted_objects=omitted,unresolved_issues={'pixel_issue_count':meta.get('issue_count'),'pixel_issues':meta.get('issues'),
                           'georef_reasons':report['acceptance']['reasons']})
    return {'output':str(Path(output).resolve()),'manifest':final_manifest,'acceptance':report['acceptance'],
            'sampling_tolerance_pixels':meta['sampling_tolerance_pixels']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('fit');p.add_argument('gcps');p.add_argument('output')
    p=sub.add_parser('attach');p.add_argument('image');p.add_argument('transform');p.add_argument('output')
    p=sub.add_parser('preview');p.add_argument('image');p.add_argument('transform');p.add_argument('output');p.add_argument('--max-size',type=int,default=1800)
    p=sub.add_parser('vectors');p.add_argument('pixel_gpkg');p.add_argument('transform');p.add_argument('output');p.add_argument('--manifest',required=True)
    for name in ('attach','vectors'):
        sub.choices[name].add_argument('--allow-hold',action='store_true',help='Export a held candidate for diagnostics only; frame isolation still applies')
    args=parser.parse_args()
    try:
        if args.command=='fit':result=fit(args.gcps,args.output)
        elif args.command=='attach':result=attach(args.image,args.transform,args.output,args.allow_hold)
        elif args.command=='preview':result=preview(args.image,args.transform,args.output,args.max_size)
        else:result=vectors(args.pixel_gpkg,args.transform,args.output,args.manifest,args.allow_hold)
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError,KeyError,TypeError,OSError) as exc:parser.exit(2,f'Error: {exc}\n')

if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
