#!/usr/bin/env python3
"""Class scheme for one vector field: codes or breaks, labels, groups and counts, no colours.

Writes a new directory with scheme.json (contract class-scheme v1, see
references/display-contracts.md) and record.json. The renderer colours the classes by theme.

  scheme.py analysis.gpkg --layer means --field mean --params params.json --output DIR
  params: {"unit": "m", "status": "candidate", "breaks": [1000, 2000]}           manual breaks
          {"unit": "人", "status": "hold", "method": "quantile", "classes": 5}   computed breaks
          {"unit": "dimensionless", "status": "candidate", "categories": [{"code": 1, "label": "林地", "group": "自然"}]}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from _sandbox import configure_writable_caches

configure_writable_caches()

import geopandas as gpd
import numpy as np
import pandas as pd

from _budget import check_vector
from _delivery import bundle, fingerprint, write_json
from _scheme import build


def scheme_for(path, layer, field, p):
    check_vector(path, layer)
    frame = gpd.read_file(path, layer=layer, engine='pyogrio')
    if field not in frame.columns:
        raise ValueError(f'Unknown field {field!r}')
    column = frame[field]
    numeric = 'categories' not in p
    if numeric and not pd.api.types.is_numeric_dtype(column):
        raise ValueError(f'Field {field!r} is not numeric; give categories for a categorical scheme')
    valid = column.notna() & (np.isfinite(column.astype('float64')) if numeric else True)
    values = column[valid].to_numpy(dtype='float64' if numeric else object)
    scheme = build(values, p, field=field)
    scheme['nodata']['count'] = int((~valid).sum())
    return scheme, {'features': len(frame), 'valid': int(valid.sum())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input')
    parser.add_argument('--layer')
    parser.add_argument('--field', required=True)
    parser.add_argument('--params', required=True)
    parser.add_argument('--output', required=True, help='New directory')
    args = parser.parse_args(argv)
    p = json.loads(Path(args.params).read_text(encoding='utf-8'))
    source = Path(args.input)
    before = fingerprint(source)
    with bundle(args.output) as stage:
        scheme, counts = scheme_for(source, args.layer, args.field, p)
        write_json(stage / 'scheme.json', scheme)
        write_json(stage / 'record.json', {'schema_version': 1, 'operation': 'scheme', 'parameters': p,
                                           'input': {'name': source.name, 'layer': args.layer, 'field': args.field, 'sha256': before},
                                           'features': counts, 'class_scheme': 'scheme.json'})
        if fingerprint(source) != before:
            raise ValueError('Input changed during processing')
    print(json.dumps({'output': str(Path(args.output)), 'class_scheme': 'scheme.json', 'record': 'record.json'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
