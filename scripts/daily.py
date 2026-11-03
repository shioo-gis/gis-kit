#!/usr/bin/env python3
"""Versioned, atomic result bundles for daily GIS operations (no recipe engine)."""
from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

from _cartography import morphology, weighted_summary, generalize
from _conflation import match, match_split, match_transfer, edge_match
from _analysis import compare, profile, distribution, grid_summary, time_slice, cleanup, cluster, cleanup_adopt
from _safe_io import iter_vector_chunks, write_vector_atomic
from _daily import normalize, update, relations, overlay, allocate, rules, geometry, grid, select, attribute_join
from _recipe_operations import DAILY_OPERATIONS
from _delivery import fingerprint
from _budget import check_vector


def read(path, layer=None, encoding='utf-8', sheet=0):
    suffix = Path(path).suffix.lower()
    if suffix == '.csv':
        import csv
        with open(path,encoding=encoding,newline='') as stream: header=next(csv.reader(stream),[])
        if len(header)!=len(set(header)): raise ValueError('Duplicate CSV headers')
        return pd.read_csv(path,dtype=str,keep_default_na=False,encoding=encoding)
    if suffix in ('.xlsx','.xls'): return pd.read_excel(path,dtype=str,keep_default_na=False,sheet_name=sheet)
    check_vector(path,layer)
    return gpd.read_file(path,layer=layer,engine='pyogrio').reset_index(drop=True)


def environment():
    return {'python':platform.python_version(), 'python_executable':sys.executable,
            'capabilities':{'coverage_simplify':callable(getattr(shapely,'coverage_simplify',None)) and shapely.geos_version >= (3,12,0)},
            'packages':{p:importlib.metadata.version(p) for p in ('geopandas','shapely','pyogrio','pyproj','pandas','numpy')},
            'geos':shapely.geos_version_string,'gdal':pyogrio.__gdal_version_string__,
            'drivers':pyogrio.list_drivers(), 'optional':{p:bool(__import__('importlib.util',fromlist=['find_spec']).find_spec(p)) for p in ('openpyxl','rasterio','qgis','pysal')}}


def clean(value):
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    if value is None or value is pd.NA: return None
    if isinstance(value,(datetime.date,datetime.datetime)): return value.isoformat()
    if hasattr(value,'item'): value=value.item()
    if isinstance(value,float) and not __import__('math').isfinite(value): return None
    return value


def execute(args):
    code_hashes={name:fingerprint(Path(__file__).with_name(name)) for name in ('daily.py','_daily.py','_metric.py','_safe_io.py','_analysis.py','_cartography.py','_conflation.py','_recipe_operations.py','_delivery.py')}
    params=json.loads(Path(args.params).read_text(encoding='utf-8'))
    if params.pop('schema_version',1) != 1: raise ValueError('Unsupported parameter schema_version')
    destination=Path(args.output)
    if destination.exists() or destination.is_symlink(): raise ValueError('Bundle output must be a new directory; no overwrite')
    paths=[args.input]+([args.right] if args.right else [])
    hashes=[fingerprint(p) for p in paths]
    left=read(args.input,params.get('layer'),params.get('encoding','utf-8'),params.get('sheet',0))
    right=read(args.right,params.get('right_layer'),params.get('encoding','utf-8')) if args.right else None
    for data in (left, right):
        if isinstance(data,gpd.GeoDataFrame) and args.operation != 'profile':
            if data.crs is None: raise ValueError('Input geometry requires known CRS')
            if data.geometry.has_z.any() or (hasattr(shapely,'has_m') and shapely.has_m(data.geometry.to_numpy()).any()): raise ValueError('Daily operations require explicit 2D input; Z/M is not discarded')
    if args.operation in ('morphology','weighted_summary','generalize'): result,details=globals()[args.operation](left,params)
    elif args.operation == 'match_transfer':
        if right is None: raise ValueError('Transfer requires --right')
        result,details=match_transfer(left,right,params,hashes)
    elif args.operation in ('profile','distribution','time_slice','cluster'): result,details=globals()[args.operation](left,params)
    elif args.operation == 'cleanup_adopt':
        if right is None: raise ValueError('Adoption requires --right candidate')
        result,details=cleanup_adopt(left,right,params,hashes)
    elif args.operation == 'cleanup': result,details=cleanup(left,right,params)
    elif args.operation == 'normalize': result,details=normalize(left,params)
    elif args.operation == 'grid': result,details=grid(left,params)
    elif args.operation == 'rules': result,details=rules(left,right,params,hashes)
    elif args.operation == 'geometry': result,details=geometry(left,right,params)
    elif args.operation == 'summarize':
        value=params['value']; group=params['group']
        if params.get('quantity_type') != 'total': raise ValueError('Summation requires quantity_type=total')
        if left[group].isna().any() or left[group].eq('').any(): raise ValueError('Missing summary group')
        values=pd.to_numeric(left[value],errors='raise')
        if values.isna().any() or not __import__('numpy').isfinite(values).all(): raise ValueError('Invalid summary values')
        result=left.assign(**{value:values}).groupby(group,dropna=False)[value].agg(['sum','count']).reset_index()
        details={'source_total':float(values.sum()),'output_total':float(result['sum'].sum())}
    else:
        if right is None: raise ValueError('Operation requires --right')
        result,details=globals()[args.operation](left,right,params)
    if args.operation=='profile':
        layers=pyogrio.list_layers(args.input).tolist()
        details['layers']=[{'name':name,'geometry_type':kind,'inspected':name==params.get('layer',layers[0][0])} for name,kind in layers]
    if hashes != [fingerprint(p) for p in paths]: raise ValueError('Input changed while processing')
    destination.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.daily-',dir=destination.parent))
    try:
        # Change/error records are serialized before any result is published.
        record={'schema_version':1,'operation':args.operation,'parameters':params,'inputs':[{'name':Path(p).name,'sha256':h} for p,h in zip(paths,hashes)],
                'environment':environment(),'counts':{'input':len(left),'output':len(result)},'details':details,
                'status':'hold' if (args.operation=='rules' and not details['passed']) or details.get('status')=='candidate' else 'complete',
                'implementation_sha256':code_hashes}
        if isinstance(result,gpd.GeoDataFrame):
            if len(result) and (result.geometry.isna().any() or not result.geometry.is_valid.all()): raise ValueError('Invalid output geometry')
            record['validation']=write_vector_atomic(result,staging/'result.gpkg')
            for start,reread in iter_vector_chunks(staging/'result.gpkg'):
                part=result.iloc[start:start+len(reread)]
                if len(part) and not all(a.equals(b) for a,b in zip(part.geometry,reread.geometry)): raise ValueError('Geometry readback differs')
                for column in result.columns:
                    if column==result.geometry.name: continue
                    for expected,actual in zip(part[column],reread[column]):
                        if pd.isna(expected) and pd.isna(actual): continue
                        if pd.isna(expected) or pd.isna(actual) or expected != actual:
                            raise ValueError(f'Attribute readback differs: {column}')
            artifact='result.gpkg'
        else:
            artifact='result.csv'; result.to_csv(staging/artifact,index=False)
            columns=list(pd.read_csv(staging/artifact,dtype=str,keep_default_na=False,nrows=0).columns)
            rows=sum(len(chunk) for chunk in pd.read_csv(staging/artifact,dtype=str,keep_default_na=False,chunksize=100_000))
            if rows!=len(result) or columns!=list(result.columns): raise ValueError('Table readback differs')
            record['validation']={'rows':rows,'columns':columns}
        record['artifact']={'name':artifact,'sha256':fingerprint(staging/artifact)}
        (staging/'record.json').write_text(json.dumps(clean(record),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        if hashes != [fingerprint(p) for p in paths]: raise ValueError('Input changed before publication')
        if code_hashes != {name:fingerprint(Path(__file__).with_name(name)) for name in code_hashes}: raise ValueError('Implementation changed during execution')
        # Publish the complete directory in one rename. Cooperative writers share a lock.
        lock=destination.with_name('.'+destination.name+'.publish-lock')
        lock.mkdir()
        try:
            if destination.exists() or destination.is_symlink(): raise ValueError('Output appeared during processing')
            os.rename(staging,destination)
        finally:
            lock.rmdir()
    finally:
        shutil.rmtree(staging,ignore_errors=True)
    print(json.dumps({'output':str(destination),'rows':len(result),'record':'record.json'}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=[*DAILY_OPERATIONS,'environment'])
    parser.add_argument('input',nargs='?')
    parser.add_argument('--right')
    parser.add_argument('--params',help='Version 1 JSON operation parameters')
    parser.add_argument('--output',help='New result bundle directory (must not exist)')
    args=parser.parse_args()
    try:
        if args.operation=='environment': print(json.dumps(environment(),indent=2)); return
        if not args.input or not args.params or not args.output: parser.error('input, --params and --output required')
        execute(args)
    except (ValueError,KeyError,TypeError,OSError) as error:
        print(f'ERROR: {error}',file=sys.stderr); raise SystemExit(1)

if __name__=='__main__':
    import _stdio
    _stdio.utf8()
    main()
