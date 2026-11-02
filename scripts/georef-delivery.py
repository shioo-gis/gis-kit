#!/usr/bin/env python3
"""Affine version comparison and atomic multi-frame spatial handoffs."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil

import numpy as np
from pyproj import CRS
from shapely.geometry import Point
from _delivery import bundle, digest, write_json
from _safe_io import iter_vector_chunks
from daily import fingerprint

spec=importlib.util.spec_from_file_location('georef',Path(__file__).with_name('raster-georef.py'))
from _budget import check_vector
G=importlib.util.module_from_spec(spec);spec.loader.exec_module(G)


def checked(path):
    r=json.loads(Path(path).read_text(encoding='utf-8'));m=np.asarray(r['matrix'],dtype=float)
    if r['method']!='affine' or m.shape!=(2,3) or not np.isfinite(m).all() or abs(np.linalg.det(m[:,:2]))<1e-15:raise ValueError('Invalid affine transform')
    G.region(r)
    if not CRS(r['target_crs']).is_projected:raise ValueError('Projected target CRS required')
    return r,m


def compare(before,after,output):
    a,ma=checked(before);b,mb=checked(after)
    if a['source_sha256']!=b['source_sha256'] or a['frame']['id']!=b['frame']['id'] or not G.region(a).equals(G.region(b)) or CRS(a['target_crs'])!=CRS(b['target_crs']):
        raise ValueError('Comparison requires same source, frame footprint and target CRS')
    def fixed(r):return sorted([(p['id'],p['pixel'],p['target_world']) for p in r['points'] if p['role']=='check'])
    if not fixed(a) or fixed(a)!=fixed(b):raise ValueError('Independent check points must remain fixed across versions')
    area=G.region(a); coordinates=list(area.exterior.coords)
    for ring in area.interiors:coordinates.extend(ring.coords)
    x=np.asarray(coordinates); delta=np.column_stack((x,np.ones(len(x))))@(mb-ma).T
    lengths=np.linalg.norm(delta,axis=1)
    result={'schema_version':1,'status':'candidate','before_sha256':digest(before),'after_sha256':digest(after),'source_sha256':a['source_sha256'],'frame':a['frame'],'target_units':a['target_units'],'fixed_check_ids':[v[0] for v in fixed(a)],'check_before':a['check'],'check_after':b['check'],'maximum_frame_displacement':float(max(lengths)),'maximum_at_pixel':coordinates[int(np.argmax(lengths))],'method':'exact affine displacement maximum over polygon vertices in identical projected CRS','acceptance_before':a['acceptance'],'acceptance_after':b['acceptance'],'historical_validation':False}
    with bundle(output) as out:write_json(out/'comparison.json',result)
    return result


def package(specification,output):
    import geopandas as gpd
    import pyogrio
    import rasterio
    from shapely import intersects_xy
    p=json.loads(Path(specification).read_text(encoding='utf-8'))
    if p.get('schema_version')!=1 or not p.get('transforms'):raise ValueError('Version 1 and transforms required')
    def check_sidecars():
        fingerprint(p['pixel_gpkg'])
        if any(Path(p['image']+suffix).exists() for suffix in ('.msk','.aux.xml','.ovr')):
            raise ValueError('External raster sidecars are not part of the bound source')
    check_sidecars()
    paths=[p['image'],p['pixel_gpkg'],p['pixel_manifest']]+p['transforms']; hashes={str(v):digest(v) for v in paths}
    reports=[checked(t)[0] for t in p['transforms']]; ids=[r['frame']['id'] for r in reports]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate frame IDs')
    areas=[G.region(r) for r in reports]
    if any(a.intersection(b).area>0 for i,a in enumerate(areas) for b in areas[i+1:]):raise ValueError('Frame interiors overlap')
    for r in reports:G.verify_image(p['image'],r); G.export_gate(r,p.get('allow_hold',False))
    records=[]
    for layer,_ in pyogrio.list_layers(p['pixel_gpkg']):
        check_vector(p['pixel_gpkg'],layer);source=gpd.read_file(p['pixel_gpkg'],layer=layer)
        if source.crs is not None or source.geometry.has_z.any():
            raise ValueError('Handoff requires unreferenced 2D pixel geometry')
        import shapely
        if hasattr(shapely,'has_m') and shapely.has_m(source.geometry.to_numpy()).any():
            raise ValueError('Handoff requires unreferenced 2D pixel geometry')
    with bundle(output) as out:
        source_folder=out/'source';source_folder.mkdir()
        for role,name in [('image','image'+Path(p['image']).suffix),('pixel_gpkg','pixels.gpkg'),('pixel_manifest','pixels.manifest.json')]:
            shutil.copyfile(p[role],source_folder/name)
        for i,(transform,r) in enumerate(zip(p['transforms'],reports)):
            folder=out/f'{i+1:04d}';folder.mkdir();write_json(folder/'transform.json',r)
            G.attach(p['image'],transform,folder/'raster.tif',p.get('allow_hold',False))
            G.preview(p['image'],transform,folder/'residuals.png')
            G.vectors(p['pixel_gpkg'],transform,folder/'vectors.gpkg',p['pixel_manifest'],p.get('allow_hold',False))
            with rasterio.open(p['image']) as src,rasterio.open(folder/'raster.tif') as dst:
                if dst.crs!=CRS(r['target_crs']) or dst.shape!=src.shape:raise ValueError('Raster CRS or shape readback mismatch')
                m=np.array(r['matrix'])
                if not np.allclose(dst.transform*(.5,.5),m[:,2],atol=1e-8,rtol=0):raise ValueError('Half-pixel transform mismatch')
                for _,w in src.block_windows(1):
                    if not np.array_equal(src.read(window=w),dst.read(window=w)):raise ValueError('Unresampled pixel readback differs')
                    yy,xx=np.mgrid[int(w.row_off):int(w.row_off+w.height),int(w.col_off):int(w.col_off+w.width)]
                    mask=src.dataset_mask(window=w);mask[~intersects_xy(G.region(r),xx,yy)]=0
                    if not np.array_equal(mask,dst.dataset_mask(window=w)):raise ValueError('Frame mask readback differs')
            layers=[]
            from shapely import transform as transform_geometry
            matrix=np.array(r['matrix']);area=G.region(r)
            for layer,_ in pyogrio.list_layers(folder/'vectors.gpkg'):
                source=gpd.read_file(p['pixel_gpkg'],layer=layer)
                selected=source.loc[source.geometry.map(lambda g:g is not None and not g.is_empty and area.covers(g))]
                expected=transform_geometry(selected.geometry.array,lambda coords:coords@matrix[:,:2].T+matrix[:,2])
                attributes=selected.drop(columns=selected.geometry.name).reset_index(drop=True);rows=0
                for start,f in iter_vector_chunks(folder/'vectors.gpkg',layer=layer):
                    stop=start+len(f)
                    if f.crs!=CRS(r['target_crs']) or not f.geometry.is_valid.all() or not all(a.equals(b) for a,b in zip(f.geometry,expected[start:stop])):raise ValueError('Vector readback failed')
                    if not f.drop(columns=f.geometry.name).reset_index(drop=True).equals(attributes.iloc[start:stop].reset_index(drop=True)):
                        raise ValueError('Vector attribute readback failed')
                    rows=stop
                if rows!=len(selected):raise ValueError('Vector readback failed')
                layers.append({'layer':layer,'rows':rows})
            # Replace temporary absolute output references with portable relative references.
            for name in ('raster.tif','vectors.gpkg'):
                manifest=folder/(name+'.manifest.json');record=json.loads(manifest.read_text(encoding='utf-8'));record['output']['path']=name;record['transform']['path']='transform.json';write_json(manifest,record)
                if 'pixel_export' in record:
                    record['pixel_export']['path']='../source/pixels.gpkg'
                    record['pixel_export']['manifest_path']='../source/pixels.manifest.json'
                    write_json(manifest,record)
            records.append({'frame_id':ids[i],'directory':folder.name,'acceptance':r['acceptance'],'layers':layers,'files':{f.name:digest(f) for f in folder.iterdir() if f.is_file()}})
        check_sidecars()
        if hashes!={str(v):digest(v) for v in paths}:raise ValueError('Handoff input changed')
        write_json(out/'record.json',{'schema_version':1,'status':'hold' if any(r['acceptance']['status']!='pass' for r in reports) else 'spatial_candidate','inputs':hashes,'frames':records,'historical_validation':False,'pixel_geometry_rewritten':False})
    return {'output':str(output),'frames':len(records)}


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    c=s.add_parser('compare');c.add_argument('before');c.add_argument('after');c.add_argument('--output',required=True)
    c=s.add_parser('package');c.add_argument('specification');c.add_argument('--output',required=True)
    a=p.parse_args()
    try:print(json.dumps(compare(a.before,a.after,a.output) if a.command=='compare' else package(a.specification,a.output)))
    except (ValueError,KeyError,TypeError,OSError) as e:p.exit(1,f'ERROR: {e}\n')
if __name__=='__main__':main()
