"""Helpers shared by the vector tools, kept apart from any one tool's CLI."""
from __future__ import annotations

import datetime
import importlib.metadata
from pathlib import Path
import platform
import sys

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

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
