#!/usr/bin/env python3
"""Pixel-centred DEM contours and source-linked along-line elevation profiles."""
import argparse
import json
from pathlib import Path
import time

import contourpy
import geopandas as gpd
import numpy as np
import rasterio as rio
from pyproj import CRS
from shapely.geometry import LineString
from _daily import geometry
from _budget import check_raster, check_vector
from _delivery import bundle, digest, implementation_hashes, write_json
from _peak_rss import peak_rss_bytes
from _safe_io import iter_vector_chunks, write_vector_atomic
from _grid import inspect, band_data, positive_integer


def contours(z, transform, crs, levels, limit):
    if not isinstance(levels,list) or not 1<=len(levels)<=1000 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not np.isfinite(x) for x in levels) or levels!=sorted(set(levels)):
        raise ValueError('Requires 1..1000 increasing unique finite levels in metres')
    x=transform.c+(np.arange(z.shape[1])+.5)*transform.a
    y=transform.f+(np.arange(z.shape[0])+.5)*transform.e
    gen=contourpy.contour_generator(x=x,y=y,z=np.ma.masked_invalid(z),name='serial',corner_mask=False,line_type='Separate')
    rows=[]
    for level in levels:
        for part,points in enumerate(gen.lines(level)):
            if len(points)<2:continue
            line=LineString(points)
            if line.is_empty or not line.is_valid or line.length==0:raise ValueError('Invalid contour generated')
            rows.append(dict(contour_id=f'{float(level):.17g}:{part}',elevation_m=float(level),geometry=line))
            if len(rows)>limit:raise ValueError('Contour count exceeds max_features')
    return gpd.GeoDataFrame(rows,geometry='geometry',crs=crs) if rows else gpd.GeoDataFrame({'contour_id':[],'elevation_m':[]},geometry=[],crs=crs)


def profile_points(frame, p, z, transform, crs):
    sampled,report=geometry(frame,None,dict(id=p['id'],method='sample',distance_m=p['distance_m'],analysis_crs=str(crs),max_features=p.get('max_features',100000)))
    reserved={'elevation_m','sample_status','sample_id'}
    if reserved.intersection(sampled.columns):raise ValueError('Profile reserved field collision')
    elevations=[]; statuses=[]
    for point in sampled.geometry:
        col,row=(~transform)*(point.x,point.y); row=int(np.floor(row)); col=int(np.floor(col))
        if 0<=row<z.shape[0] and 0<=col<z.shape[1]:
            v=z[row,col];status='valid' if np.isfinite(v) else 'nodata'
        else:v=np.nan;status='outside'
        elevations.append(v);statuses.append(status)
    sampled['sample_id']=[json.dumps([str(row.source_id),int(row.source_part),int(row.part_id)],separators=(',',':')) for row in sampled.itertuples()]
    sampled['elevation_m']=elevations; sampled['sample_status']=statuses
    return sampled,report


def execute(source,p,output):
    started=time.perf_counter()
    allowed={'vertical_unit','vertical_datum','source_description','levels_m','profile','max_pixels','max_features'}
    if not isinstance(p,dict) or set(p)-allowed or p.get('vertical_unit')!='metre':raise ValueError('Explicit metre elevations and known parameters required')
    if not any(k in p for k in ('levels_m','profile')):raise ValueError('Request contours and/or profile')
    for field in ('vertical_datum','source_description'):
        if not isinstance(p.get(field),str) or not p[field].strip():raise ValueError(f'Explicit {field} required')
    source=Path(source).resolve(); inputs=[source]
    if 'profile' in p:
        q=p['profile']
        if not isinstance(q,dict) or set(q)-{'input','layer','id','distance_m','max_features'}:raise ValueError('Unknown profile parameter')
        inputs.append(Path(q['input']).resolve())
    for f in inputs:
        if any(Path(str(f)+s).exists() for s in ('.msk','.aux.xml','.ovr','-wal','-shm')):raise ValueError('External sidecars unsupported')
    hashes=[digest(f) for f in inputs]
    code=implementation_hashes(__file__)
    with rio.open(source) as ds:
        meta=inspect(ds);t=ds.transform;c=CRS(ds.crs)
        if not c.is_projected or t.b or t.d or t.a<=0 or t.e>=0:raise ValueError('North-up projected grid required')
        if ds.count!=1 or min(ds.shape)<2 or ds.width*ds.height>positive_integer(p.get('max_pixels',5000000)):raise ValueError('Invalid or oversized single-band grid')
        if ds.units[0] not in (None,'m','metre'):raise ValueError('Band unit conflicts with metre')
        check_raster(ds,arrays=3)  # masked float64 decode, its mask and the filled z
        z=band_data(ds,1).filled(np.nan)
    with bundle(output) as stage:
        artifacts={}; reports={}
        if 'levels_m' in p:
            lines=contours(z,t,c,p['levels_m'],positive_integer(p.get('max_features',100000)))
            write_vector_atomic(lines,stage/'contours.gpkg')
            artifacts['contours']={'file':'contours.gpkg','rows':len(lines),'sha256':digest(stage/'contours.gpkg')}
        if 'profile' in p:
            q=p['profile']; check_vector(inputs[1],q.get('layer')); frame=gpd.read_file(inputs[1],layer=q.get('layer'))
            points,reports['profile']=profile_points(frame,q,z,t,c)
            write_vector_atomic(points,stage/'profile.gpkg')
            count=0
            for start,decoded in iter_vector_chunks(stage/'profile.gpkg'):
                part=points.iloc[start:start+len(decoded)]
                np.testing.assert_allclose(decoded['elevation_m'],part['elevation_m'],rtol=0,atol=0,equal_nan=True)
                if decoded['sample_status'].tolist()!=part['sample_status'].tolist():raise ValueError('Profile status readback differs')
                count+=len(decoded)
            if count!=len(points):raise ValueError('Profile status readback differs')
            artifacts['profile']={'file':'profile.gpkg','rows':len(points),'sha256':digest(stage/'profile.gpkg'),'status_counts':points['sample_status'].value_counts().to_dict()}
        if hashes!=[digest(f) for f in inputs] or code!=implementation_hashes(__file__):raise ValueError('Input or implementation changed')
        write_json(stage/'record.json',dict(schema_version=1,status='candidate',source_grid=meta,inputs=[dict(name=f.name,sha256=h) for f,h in zip(inputs,hashes)],parameters=p,
            implementation=code,environment={'contourpy':contourpy.__version__,'rasterio':rio.__version__},artifacts=artifacts,reports=reports,
            assumptions=['Contours interpolate pixel centres; masked corners exclude whole quad; no extrapolation to outer raster boundary',
                         'Profile uses existing along-line sampling and containing-pixel elevations; source part measures restart; missing samples remain explicit; not surveyed observations'],
            resources={'wall_seconds':time.perf_counter()-started,'max_rss_bytes':peak_rss_bytes(),'rss_scope':'process lifetime high-water mark'}))
    return Path(output)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('input');parser.add_argument('--params',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:execute(args.input,json.loads(Path(args.params).read_text(encoding='utf-8')),args.output)
    except (ValueError,KeyError,TypeError,OSError,rio.errors.RasterioError) as exc:parser.exit(1,f'ERROR: {exc}\n')


if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
