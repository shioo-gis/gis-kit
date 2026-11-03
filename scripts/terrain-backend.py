#!/usr/bin/env python3
"""Explicit GRASS backend for bounded hydrology and planar multi-point visibility."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile

import numpy as np
import rasterio as rio
from pyproj import CRS
from _budget import check_raster
from _delivery import bundle, digest, implementation_hashes, write_json
from _grid import inspect, band_data, band_matches, positive_integer
from terrain import summary


def finite(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < minimum:
        raise ValueError('Finite numeric parameter outside allowed range')
    return value


def validate(operation, p, ds):
    common = {'vertical_unit','vertical_datum','source_description','max_pixels'}
    specific = {'flow_method','threshold_cells','outlet'} if operation=='hydrology' else {'observers','target_height_m','max_distance_m'}
    if operation not in ('hydrology','viewshed') or not isinstance(p,dict) or set(p)-(common|specific):
        raise ValueError('Unsupported operation or parameter')
    if p.get('vertical_unit') != 'metre': raise ValueError('Backend requires explicit metre elevations')
    for field in ('vertical_datum','source_description'):
        if not isinstance(p.get(field),str) or not p[field].strip(): raise ValueError(f'Explicit {field} required')
    c=CRS(ds.crs); t=ds.transform
    if not c.is_projected or len(c.axis_info)!=2 or [a.direction for a in c.axis_info]!=['east','north'] or any(a.unit_conversion_factor!=1 for a in c.axis_info):
        raise ValueError('Projected east/north metre CRS required')
    if t.b or t.d or t.a<=0 or t.e>=0: raise ValueError('North-up grid required')
    if ds.count!=1 or min(ds.shape)<3 or ds.width*ds.height>positive_integer(p.get('max_pixels',1000000)):
        raise ValueError('Requires single-band bounded grid of at least 3x3')
    if ds.units[0] not in (None,'m','metre'): raise ValueError('Band unit conflicts with metre')
    # Input grid, read-back outputs, and for viewshed the stacked outputs plus the visible count.
    outputs=4 if operation=='hydrology' else len(p['observers']) if isinstance(p.get('observers'),list) else 0
    check_raster(ds,arrays=1+outputs+(outputs+1 if operation=='viewshed' else 0))
    def point(xy):
        if not isinstance(xy,list) or len(xy)!=2: raise ValueError('Point requires [x,y]')
        for v in xy: finite(v,-1e20)
        r,c=ds.index(*xy)
        if not (0<=r<ds.height and 0<=c<ds.width): raise ValueError('Point outside DEM')
    if operation=='hydrology':
        if p.get('flow_method') not in ('D8','MFD'): raise ValueError('Explicit D8 or MFD required')
        positive_integer(p['threshold_cells'])
        if 'outlet' in p:
            if p['flow_method']!='D8': raise ValueError('Outlet delineation requires D8; MFD drainage gives only dominant direction')
            point(p['outlet'])
    else:
        finite(p['target_height_m']); finite(p['max_distance_m'],1e-9)
        obs=p['observers']
        if not isinstance(obs,list) or not 1<=len(obs)<=32: raise ValueError('Requires 1..32 observers')
        ids=[]
        for o in obs:
            if not isinstance(o,dict) or set(o)!={'id','x','y','height_m'} or not isinstance(o['id'],str) or not o['id']:
                raise ValueError('Observer requires unique string id, x, y, height_m')
            ids.append(o['id']); point([o['x'],o['y']]); finite(o['height_m'])
            rr,cc=ds.index(o['x'],o['y']);xx,yy=ds.xy(rr,cc)
            if abs(xx-o['x'])>abs(t.a)*1e-9 or abs(yy-o['y'])>abs(t.e)*1e-9:
                raise ValueError('Observers must be at pixel centres; snap explicitly and retain original locations')
        if len(set(ids))!=len(ids): raise ValueError('Duplicate observer ID')


def grid_error(actual, expected, shape):
    """Permit only sub-nanopixel GRASS text-region roundoff, never a shifted grid."""
    height,width=shape
    corners=[(0,0),(width,0),(0,height),(width,height)]
    errors=[]
    for col,row in corners:
        x,y=actual*(col,row);c,r=(~expected)*(x,y)
        errors.append(max(abs(c-col),abs(r-row)))
    error=max(errors)
    if not math.isfinite(error) or error>1e-9:raise ValueError('Backend output grid shifted beyond 1e-9 pixel roundoff tolerance')
    return error


def execute(operation, source, p, output, grass):
    started=time.perf_counter(); source=Path(source).resolve(); grass=Path(grass).resolve()
    if not grass.is_file() or not os.access(grass,os.X_OK): raise ValueError('Explicit executable GRASS launcher required')
    if any(Path(str(source)+s).exists() for s in ('.msk','.ovr','.aux.xml')): raise ValueError('External sidecars unsupported')
    before=digest(source)
    code=implementation_hashes(__file__)
    with rio.open(source) as ds:
        meta=inspect(ds); validate(operation,p,ds); values=band_data(ds,1)
        if np.ma.getmaskarray(values).any(): raise ValueError('Backend requires complete DEM; missing cells must not silently become drainage or visibility barriers')
        profile=dict(driver='GTiff',width=ds.width,height=ds.height,count=1,dtype='float64',crs=ds.crs,transform=ds.transform,nodata=np.nan,compress='deflate')
    with bundle(output) as stage:
        with tempfile.TemporaryDirectory(prefix='.grass-work-',dir=stage.parent) as temp:
            work=Path(temp); (work/'home').mkdir()
            env=dict(os.environ,HOME=str(work/'home'),TMPDIR=str(work),PYTHONDONTWRITEBYTECODE='1',GRASS_MESSAGE_FORMAT='plain')
            env.pop('GISRC',None); env.pop('GISBASE',None)
            def run(args):
                result=subprocess.run(args,env=env,capture_output=True,text=True)
                if result.returncode: raise ValueError(f'GRASS session failed (exit {result.returncode}); no result published')
                return result.stdout.strip()
            version=run([str(grass),'--version'])
            # A normalized self-contained raster materializes exactly the validated band.
            dem=work/'dem.tif'
            with rio.open(dem,'w',**profile) as d:d.write(values.filled(np.nan),1)
            run([str(grass),'-c',str(dem),str(work/'location'),'-e'])
            write_json(work/'job.json',dict(operation=operation,input=str(dem),work=str(work),params=p))
            run([str(grass),str(work/'location/PERMANENT'),'--exec',sys.executable,str(Path(__file__).with_name('_grass_worker.py')),str(work/'job.json')])
            names=['accumulation','drainage','basins','streams'] if operation=='hydrology' else [f'view_{i}' for i in range(len(p['observers']))]
            if operation=='hydrology' and 'outlet' in p:names.append('catchment')
            arrays={}; summaries={}; grid_errors={}
            for name in names:
                with rio.open(work/f'{name}.tif') as d:
                    if d.crs!=profile['crs'] or d.shape!=values.shape:raise ValueError('Backend output CRS or dimensions differ')
                    grid_errors[name]=grid_error(d.transform,profile['transform'],values.shape)
                    arrays[name]=d.read(1,masked=True).filled(np.nan)
            if operation=='hydrology':
                direction=arrays['drainage']
                if not np.isfinite(arrays['accumulation']).all() or not np.isfinite(direction).all() or not np.isin(direction,np.arange(-8,9)).all():
                    raise ValueError('Invalid drainage or accumulation result')
            else:
                rr,cc=np.indices(values.shape); t=profile['transform']; xx=t.c+(cc+.5)*t.a; yy=t.f+(rr+.5)*t.e
                for i,o in enumerate(p['observers']):
                    a=arrays[f'view_{i}']
                    if not np.isin(a[np.isfinite(a)],[0,1]).all(): raise ValueError('Expected boolean viewshed')
                    a[np.hypot(xx-o['x'],yy-o['y'])>p['max_distance_m']]=np.nan
                stack=np.stack(list(arrays.values())); count=np.nansum(stack,axis=0); count[~np.isfinite(stack).any(axis=0)]=np.nan
                arrays['visible_count']=count
            for name,a in arrays.items():
                path=stage/f'{name}.tif'
                with rio.open(path,'w',**profile) as d:d.write(a,1)
                with rio.open(path) as d:
                    if not band_matches(d,1,a):raise ValueError('Published raster readback mismatch')
                summaries[name]={'file':path.name,'sha256':digest(path),'summary':summary(a)}
            resource_report=json.loads((work/'worker.json').read_text(encoding='utf-8'))
        if before!=digest(source) or code!=implementation_hashes(__file__):raise ValueError('Input or implementation changed')
        write_json(stage/'record.json',dict(schema_version=1,status='candidate',operation=operation,parameters=p,source_sha256=before,
            source_grid=meta,backend_version=version,implementation=code,backend_grid_roundtrip_error_pixels=grid_errors,artifacts=summaries,resources=dict(cli_work_seconds=time.perf_counter()-started,worker=resource_report),
            assumptions=['GRASS AT least-cost routing; no prior sink filling; negative accumulation retains boundary uncertainty; streams and basins are raster candidates' if operation=='hydrology' else 'Planar visibility, no curvature or refraction; DSM surface plus observer/target heights; outside radius is NoData; count is number of observers seeing pixel',
                         'No event or historical reconstruction; no ground truth validation']))
    return Path(output)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=['hydrology','viewshed']);parser.add_argument('input')
    parser.add_argument('--params',required=True);parser.add_argument('--output',required=True);parser.add_argument('--grass',required=True)
    args=parser.parse_args()
    try:execute(args.operation,args.input,json.loads(Path(args.params).read_text(encoding='utf-8')),args.output,args.grass)
    except (ValueError,KeyError,TypeError,OSError,rio.errors.RasterioError) as exc:parser.exit(1,f'ERROR: {exc}\n')


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
