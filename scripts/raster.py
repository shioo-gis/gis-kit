#!/usr/bin/env python3
"""Explicit-grid raster operations, immutable verified result bundles."""
import argparse
from contextlib import ExitStack
import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

import numpy as np
import geopandas as gpd
import rasterio as rio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject, calculate_default_transform, transform_bounds
from rasterio.windows import Window, transform as window_transform, from_bounds as window_from_bounds
from rasterio.transform import Affine
from rasterio.merge import merge
from shapely.geometry import box, mapping
from _budget import check_raster, check_vector, raster_bytes, require
from _daily import stable
from daily import fingerprint, clean
import _raster_numeric as numeric
import _raster_cleanup as cleanup


def positive_integer(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value or int(value)<1: raise ValueError('Expected positive integer')
    return int(value)


def windows(width,height,size):
    if size<1: raise ValueError('block_size must be positive')
    for y in range(0,height,size):
        for x in range(0,width,size): yield Window(x,y,min(size,width-x),min(size,height-y))


def band_matches(ds,band,expected,equal_nan=True,size=1024):
    """Compare one decoded band with an in-memory array window by window, without a second full copy."""
    if ds.shape!=expected.shape: return False
    return all(np.array_equal(ds.read(band,window=w),expected[w.toslices()],equal_nan=equal_nan) for w in windows(ds.width,ds.height,size))


def inspect(ds):
    if ds.crs is None: raise ValueError('Raster requires known CRS')
    if not np.isfinite(list(ds.transform)).all() or ds.transform.determinant==0: raise ValueError('Invalid raster transform')
    return {'crs':str(ds.crs),'transform':list(ds.transform)[:6],'width':ds.width,'height':ds.height,'count':ds.count,
            'dtypes':list(ds.dtypes),'nodata':str(ds.nodata) if ds.nodata is not None and not np.isfinite(ds.nodata) else ds.nodata,
            'scales':ds.scales,'offsets':ds.offsets,'units':ds.units,'descriptions':ds.descriptions,'mask_flags':[[x.name for x in v] for v in ds.mask_flag_enums],
            'bounds':list(ds.bounds),'block_shapes':ds.block_shapes}


def warp_window(ds, bounds):
    """Source window (whole pixels, inside the raster) covering lon/lat bounds [west, south, east, north]."""
    if not (isinstance(bounds,list) and len(bounds)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in bounds)
            and -180<=bounds[0]<bounds[2]<=180 and -90<=bounds[1]<bounds[3]<=90):
        raise ValueError('bounds must be lon/lat [west, south, east, north]')
    if ds.transform.b or ds.transform.d: raise ValueError('bounds requires a north-up source; warp the whole raster first')
    left,bottom,right,top=transform_bounds('EPSG:4326',ds.crs,*bounds,densify_pts=21)
    win=window_from_bounds(left,bottom,right,top,ds.transform).round_offsets(op='floor').round_lengths(op='ceil')
    col0,row0=max(0,int(win.col_off)),max(0,int(win.row_off))
    col1,row1=min(ds.width,int(win.col_off+win.width)+1),min(ds.height,int(win.row_off+win.height)+1)
    if col1<=col0 or row1<=row0: raise ValueError('bounds do not intersect the raster')
    return Window(col0,row0,col1-col0,row1-row0)


def band_data(ds, band, window=None):
    if not 1<=band<=ds.count: raise ValueError('Band outside dataset')
    if ds.scales[band-1]!=1 or ds.offsets[band-1]!=0: raise ValueError('Calibrated band scale/offset unsupported; normalize explicitly before analysis')
    raw=ds.read(band,window=window,masked=True)
    if raw.dtype.kind in 'iu' and np.any(np.abs(raw.compressed().astype('longdouble'))>2**53): raise ValueError('Integer values exceed exact float64 range')
    a=raw.astype('float64')
    return np.ma.masked_invalid(a)


def zonal(ds,zones,p):
    stable(zones,p['id']); zones=zones.to_crs(ds.crs)
    if not zones.geom_type.isin(['Polygon','MultiPolygon']).all() or not zones.geometry.is_valid.all(): raise ValueError('Valid polygon zones required')
    mode=p.get('method','center')
    if mode not in ('center','all_touched','fractional'): raise ValueError('Unknown pixel coverage method')
    if mode=='fractional' and not ds.crs.is_projected: raise ValueError('Fractional planar coverage requires projected raster CRS')
    records=[]; band=positive_integer(p.get('band',1))
    for _,zone in zones.iterrows():
        total=0.; weight=0.; count=0; low=math.inf; high=-math.inf; hist={}
        for window in windows(ds.width,ds.height,positive_integer(p.get('block_size',256))):
            t=window_transform(window,ds.transform); values=band_data(ds,band,window)
            if mode=='fractional':
                weights=np.zeros(values.shape)
                for r,c in np.ndindex(values.shape):
                    x,y=t*(c,r); x2,y2=t*(c+1,r+1)
                    pixel=box(x,y2,x2,y)
                    weights[r,c]=zone.geometry.intersection(pixel).area/pixel.area
            else:
                weights=geometry_mask([mapping(zone.geometry)],values.shape,t,invert=True,all_touched=mode=='all_touched').astype(float)
            valid=(~np.ma.getmaskarray(values)) & (weights>0)
            v=values.data[valid]; w=weights[valid]
            total+=float(np.sum(v*w)); weight+=float(w.sum()); count+=len(v)
            if len(v): low=min(low,float(v.min())); high=max(high,float(v.max()))
            if p.get('histogram',False):
                for value,amount in zip(v,w):
                    key=str(float(value)); hist[key]=hist.get(key,0.)+float(amount)
        records.append({'zone_id':str(zone[p['id']]),'valid_pixels':count,'weight':weight,'sum':total if weight else None,
                        'mean':total/weight if weight else None,'min':low if weight else None,'max':high if weight else None,'histogram':hist})
    return {'method':mode,'area_unit':'pixel fractions, not square metres','zones':records}


def process(operation,paths,p,stage):
    with ExitStack() as stack:
        datasets=[stack.enter_context(rio.open(path)) for path in paths]
        ds=datasets[0]; meta=inspect(ds)
        if operation not in ('mosaic', *numeric.OPERATIONS, *cleanup.OPERATIONS) and len(datasets)!=1: raise ValueError('This operation accepts one raster')
        limit=positive_integer(p.get('max_pixels',50000000))
        # warp with lon/lat bounds reads only the covering source window, so the guard applies to it.
        window=warp_window(ds,p.get('bounds')) if operation=='warp' and p.get('bounds') is not None else None
        sizes=[(window.width,window.height)] if window is not None else [(x.width,x.height) for x in datasets]
        if limit<1 or any(w*h>limit for w,h in sizes): raise ValueError('Raster exceeds max_pixels memory guard')
        for other in datasets[1:]: inspect(other)
        if operation not in ('inspect','warp') and any(x.transform.b or x.transform.d or x.transform.a<=0 or x.transform.e>=0 for x in datasets): raise ValueError('Operation requires north-up grid; use warp first')
        if operation in cleanup.OPERATIONS:
            return cleanup.process(operation,datasets,p,stage,stack)
        if operation in numeric.OPERATIONS:
            return numeric.process(operation,datasets,p,stage,stack)
        band=positive_integer(p.get('band',1)); size=positive_integer(p.get('block_size',256))
        if operation=='inspect': return {'raster':meta}
        if operation=='display':
            from _raster_display import display
            check_raster(ds,arrays=2)  # raw band plus its float64 decode
            return dict(display(ds,band_data(ds,band),p,stage), source=meta)
        if operation in ('zonal','sample'):
            check_vector(p['vector'],p.get('layer')); vectors=gpd.read_file(p['vector'],layer=p.get('layer')); stable(vectors,p['id'])
            if vectors.crs is None: raise ValueError('Vector requires CRS')
            if operation=='zonal': return zonal(ds,vectors,p)
            vectors=vectors.to_crs(ds.crs)
            if not vectors.geom_type.eq('Point').all(): raise ValueError('Sampling requires points')
            rows=[]
            for ident,point in zip(vectors[p['id']],vectors.geometry):
                r,c=ds.index(point.x,point.y); value=None
                if 0<=r<ds.height and 0<=c<ds.width:
                    a=band_data(ds,band,Window(c,r,1,1)); value=None if a.mask[0,0] else float(a[0,0])
                rows.append({'source_id':str(ident),'value':value})
            return {'samples':rows,'method':'containing pixel; bottom/right outside'}
        if operation=='histogram':
            hist={}
            for win in windows(ds.width,ds.height,size):
                values,counts=np.unique(band_data(ds,band,win).compressed(),return_counts=True)
                for v,c in zip(values,counts): hist[str(float(v))]=hist.get(str(float(v)),0)+int(c)
            return {'histogram':hist,'band':band}
        profile=ds.profile.copy(); profile.update(driver='GTiff',dtype='float64',nodata=float('nan'),count=1,compress='deflate')
        profile.pop('photometric',None)
        # Single selected band is explicit; masks are materialized as NaN NoData.
        if operation in ('warp','align'):
            category=p.get('kind')
            if category not in ('categorical','continuous'): raise ValueError('kind must be categorical or continuous')
            method=p.get('resampling','nearest' if category=='categorical' else None)
            if method not in ('nearest','bilinear','cubic','average','mode'): raise ValueError('Explicit supported resampling required')
            if category=='categorical' and method not in ('nearest','mode'): raise ValueError('Categorical interpolation forbidden')
            if operation=='align':
                reference=stack.enter_context(rio.open(p['reference'])); inspect(reference)
                if reference.transform.b or reference.transform.d or reference.transform.a<=0 or reference.transform.e>=0: raise ValueError('Reference must be north-up')
                transform,width,height,crs=reference.transform,reference.width,reference.height,reference.crs
            else:
                crs=p['crs']
                src_width,src_height=(window.width,window.height) if window is not None else (ds.width,ds.height)
                src_bounds=rio.windows.bounds(window,ds.transform) if window is not None else ds.bounds
                transform,width,height=calculate_default_transform(ds.crs,crs,src_width,src_height,*src_bounds,resolution=p.get('resolution'))
            if width*height>limit: raise ValueError('Target exceeds max_pixels memory guard')
            src_width,src_height=(window.width,window.height) if window is not None else (ds.width,ds.height)
            # Source float64 decode and its NaN-filled copy, plus the target grid.
            require(raster_bytes(src_width,src_height,arrays=2)+raster_bytes(width,height),basis='decoded_pixels',input=ds.name)
            profile.update(crs=crs,transform=transform,width=width,height=height)
            output=np.full((height,width),np.nan)
            reproject(band_data(ds,band,window).filled(np.nan),output,src_transform=window_transform(window,ds.transform) if window is not None else ds.transform,
                      src_crs=ds.crs,src_nodata=np.nan,dst_transform=transform,dst_crs=crs,dst_nodata=np.nan,resampling=Resampling[method])
        elif operation=='mosaic':
            if p.get('overlap') not in ('first','last','min','max'): raise ValueError('Explicit mosaic overlap rule required')
            for other in datasets:
                if other.crs!=ds.crs or other.res!=ds.res: raise ValueError('Mosaic inputs must share CRS and resolution; align first')
                offset=(~ds.transform)*(other.transform.c,other.transform.f)
                if any(abs(v-round(v))>1e-8 for v in offset): raise ValueError('Mosaic inputs have misaligned origins')
            union_width=math.ceil((max(x.bounds.right for x in datasets)-min(x.bounds.left for x in datasets))/ds.res[0])
            union_height=math.ceil((max(x.bounds.top for x in datasets)-min(x.bounds.bottom for x in datasets))/ds.res[1])
            if union_width*union_height>limit: raise ValueError('Mosaic exceeds max_pixels memory guard')
            # Largest input decode plus merge's union output and its validity mask.
            require(max(raster_bytes(x.width,x.height,arrays=2) for x in datasets)+raster_bytes(union_width,union_height,arrays=2),
                    basis='decoded_pixels',input=ds.name)
            # Materialize source masks so valid zero and internal masks remain distinguishable.
            copies=[]
            for i,other in enumerate(datasets):
                tmp=stage/f'mosaic-input-{i}.tif'; pr=other.profile.copy(); pr.update(driver='GTiff',count=1,dtype='float64',nodata=np.nan)
                with rio.open(tmp,'w',**pr) as out: out.write(band_data(other,band).filled(np.nan),1)
                copies.append(stack.enter_context(rio.open(tmp)))
            values,transform=merge(copies,method=p['overlap'],nodata=np.nan,dtype='float64'); output=values[0]
            profile.update(transform=transform,height=output.shape[0],width=output.shape[1])
        elif operation in ('clip','calculate','reclassify','copy'):
            output=None
        else: raise ValueError('Unknown operation')
        target=stage/'result.tif'
        expected=hashlib.sha256()
        with rio.open(target,'w',**profile) as out:
            if output is not None:
                out.write(output,1)
                for win in windows(profile['width'],profile['height'],size):
                    expected.update(output[win.toslices()].astype('<f8').tobytes())
            else:
                geometries=None
                if operation=='clip':
                    check_vector(p['vector'],p.get('layer')); vector=gpd.read_file(p['vector'],layer=p.get('layer'))
                    if vector.crs is None or not vector.geometry.is_valid.all(): raise ValueError('Valid mask CRS/geometries required')
                    geometries=[mapping(g) for g in vector.to_crs(ds.crs).geometry]
                for win in windows(ds.width,ds.height,size):
                    values=band_data(ds,band,win); data=values.filled(np.nan)
                    if operation=='clip':
                        inside=geometry_mask(geometries,values.shape,window_transform(win,ds.transform),invert=True,all_touched=p.get('all_touched',False))
                        data[~inside]=np.nan
                    elif operation=='calculate':
                        # No eval: finite affine expression and optional explicit threshold conditional.
                        scale=float(p.get('scale',1)); offset=float(p.get('offset',0))
                        if not np.isfinite([scale,offset]).all(): raise ValueError('Non-finite calculation parameter')
                        data=data*scale+offset
                        if 'threshold' in p:
                            if not np.isfinite([float(p[x]) for x in ('threshold','true','false')]).all(): raise ValueError('Non-finite conditional parameter')
                            condition=values.data>=float(p['threshold'])
                            data=np.where(condition,float(p['true']),float(p['false'])); data[np.ma.getmaskarray(values)]=np.nan
                        if np.isinf(data).any(): raise ValueError('Calculation overflow')
                    elif operation=='reclassify':
                        data=numeric.classify(data,p)
                    out.write(data,1,window=win)
                    expected.update(data.astype('<f8').tobytes())
        if p.get('format','GTiff')=='COG':
            from rasterio.shutil import copy
            cog=stage/'cog.tif'; copy(target,cog,driver='COG',compress='DEFLATE',overview_resampling='nearest')
            os.replace(cog,target)
        elif p.get('format','GTiff')!='GTiff': raise ValueError('Unsupported output format')
        with rio.open(target) as result:
            validation=inspect(result)
            if result.count!=1 or result.width!=profile['width'] or result.height!=profile['height'] or result.crs!=rio.crs.CRS.from_user_input(profile['crs']) or result.transform!=profile['transform']: raise ValueError('Raster readback metadata mismatch')
            if p.get('format')=='COG' and result.tags(ns='IMAGE_STRUCTURE').get('LAYOUT')!='COG': raise ValueError('COG layout missing')
            # Force actual decode of every output block; NaN is the only missing-value encoding.
            valid=0; actual=hashlib.sha256()
            for win in windows(result.width,result.height,size):
                decoded=band_data(result,1,win); valid+=int(decoded.count())
                actual.update(decoded.filled(np.nan).astype('<f8').tobytes())
            if expected.hexdigest()!=actual.hexdigest(): raise ValueError('Decoded raster readback differs')
        return {'source':meta,'output':validation,'valid_pixels':valid,'decoded_sha256':actual.hexdigest(),'hash_block_size':size,'artifact':'result.tif','clip_extent':'source grid retained' if operation=='clip' else None}


def execute(args):
    p=json.loads(Path(args.params).read_text(encoding='utf-8'))
    if p.pop('schema_version',1)!=1: raise ValueError('Unsupported schema')
    destination=Path(args.output)
    if destination.exists() or destination.is_symlink(): raise ValueError('Output must be a new directory')
    inputs=list(args.input)+[p[x] for x in ('vector','reference','table','exclude','protected','scope') if x in p]
    for x in args.input+[p[k] for k in ('reference','protected','scope') if k in p]:
        if any(Path(str(x)+suffix).exists() for suffix in ('.msk','.aux.xml','.ovr')): raise ValueError('External raster sidecars unsupported; prepare self-contained input')
    identities=[fingerprint(x) for x in inputs]
    implementation={n:fingerprint(Path(__file__).with_name(n)) for n in ('raster.py','_raster_display.py','_scheme.py','_raster_numeric.py','_raster_cleanup.py','daily.py','_daily.py','_analysis.py','_metric.py','_safe_io.py')}
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.raster-',dir=destination.parent))
    try:
        result=process(args.operation,args.input,p,stage)
        result.update(schema_version=1,operation=args.operation,parameters=p,inputs=[{'name':Path(x).name,'sha256':h} for x,h in zip(inputs,identities)],
                      environment={'rasterio':rio.__version__,'gdal':rio.__gdal_version__,'numpy':np.__version__},implementation_sha256=implementation)
        if (stage/'result.tif').exists(): result['artifact_sha256']=fingerprint(stage/'result.tif')
        (stage/'record.json').write_text(json.dumps(clean(result),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        for path in stage.glob('mosaic-input-*'): path.unlink()
        if implementation!={n:fingerprint(Path(__file__).with_name(n)) for n in implementation}: raise ValueError('Implementation changed during processing')
        if identities!=[fingerprint(x) for x in inputs]: raise ValueError('Inputs changed during processing')
        lock=destination.with_name('.'+destination.name+'.publish-lock'); lock.mkdir()
        try:
            if destination.exists() or destination.is_symlink(): raise ValueError('Output appeared during processing')
            os.rename(stage,destination)
        finally: lock.rmdir()
    finally: shutil.rmtree(stage,ignore_errors=True)
    print(json.dumps({'output':str(destination),'record':'record.json'}))


def operation_help(operation):
    """What the references say about one operation's --params: the table rows, bullets and paragraphs that start with its name."""
    starts=re.compile(r'(\|\s*|- )?\*{0,2}`'+re.escape(operation)+'`')
    found=[]
    for name in ('analysis-extensions.md','raster-numeric.md','raster-cleanup.md','composer-display.md'):
        path=Path(__file__).resolve().parents[1]/'references'/name
        lines=[line.strip() for line in path.read_text(encoding='utf-8').splitlines() if starts.match(line)]
        if lines: found+=[f'references/{name}:',*lines]
    return '\n'.join([f'Parameters of {operation} (--params is a JSON file with schema_version 1):',*found]) if found else None


def main():
    wanted=next((word for word in sys.argv[1:] if not word.startswith('-')),None) if {'-h','--help'}&set(sys.argv[1:]) else None
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(wanted and operation_help(wanted)) or 'raster.py OPERATION --help lists the --params of that operation.')
    parser.add_argument('operation',choices=['inspect','display','clip','warp','align','mosaic','copy','calculate','reclassify','sample','histogram','zonal',*numeric.OPERATIONS,*cleanup.OPERATIONS])
    parser.add_argument('input',nargs='+'); parser.add_argument('--params',required=True); parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try: execute(args)
    except (ValueError,KeyError,TypeError,OSError,rio.errors.RasterioError) as exc: parser.exit(1,f'ERROR: {exc}\n')

if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
