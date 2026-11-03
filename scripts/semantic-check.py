#!/usr/bin/env python3
"""Compare explicit semantic fields or JSON pointers; detect lost unknown/candidate/hold values."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import sys

STATES = ('unknown', 'candidate', 'spatial_candidate', 'hold')
MISSING = object()


def load_json(path):
    def invalid(value):
        raise ValueError('Non-finite JSON literal is not a semantic state')
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid)


def protected(value, states):
    if value is None:
        return 'unknown'
    if isinstance(value, str):
        text = value.strip().casefold()
        return 'unknown' if not text else text if text in states else None
    if isinstance(value, (dict, list)):
        raise ValueError('Select scalar fields/pointers, not whole objects or arrays')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Non-finite values must be represented explicitly as null')
    return None


def compare_value(before, after, location, states):
    if before is MISSING:
        raise ValueError(f'Upstream selector is missing: {location}')
    state = protected(before, states)
    if state is None:
        return False, None
    if after is MISSING:
        return True, {'location': location, 'issue': 'missing_downstream', 'before': before}
    if protected(after, states) != state:
        return True, {'location': location, 'issue': 'protected_state_changed', 'before': before, 'after': after}
    return True, None


def pointer(document, selector):
    if not selector.startswith('/'):
        raise ValueError('JSON pointers must start with /')
    value = document
    for token in selector.split('/')[1:]:
        # RFC 6901 escaping only; no expressions, globbing or schema inference.
        if '~' in token.replace('~0', '').replace('~1', ''):
            raise ValueError('Invalid JSON pointer escape')
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value.get(token, MISSING)
        elif isinstance(value, list) and token.isdigit() and str(int(token)) == token and int(token) < len(value):
            value = value[int(token)]
        else:
            return MISSING
    return value


def table(path, columns, layer=None, *, require_columns=False):
    def check_columns(available):
        if require_columns and not set(columns) <= set(available):
            raise ValueError('Upstream selector columns are missing: ' +
                             ', '.join(sorted(set(columns) - set(available))))

    path = Path(path)
    if path.suffix.lower() == '.csv':
        if layer:
            raise ValueError('CSV does not have layers')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            header = reader.fieldnames or []
            if len(header) != len(set(header)):
                raise ValueError('Duplicate CSV columns')
            check_columns(header)
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise ValueError('Malformed CSV row')
            return rows
    if path.suffix.lower() in ('.json', '.geojson'):
        if layer:
            raise ValueError('JSON does not have layers')
        data = load_json(path)
        if isinstance(data, dict) and data.get('type') == 'FeatureCollection':
            data = [feature['properties'] for feature in data['features']]
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError('Table mode requires row objects or GeoJSON feature properties')
        return data
    import pandas as pd
    import pyogrio
    layers = pyogrio.list_layers(path)
    if layer is None and len(layers) != 1:
        raise ValueError('Multiple vector layers require an explicit layer name')
    chosen = layer if layer is not None else layers[0][0]
    available = set(pyogrio.read_info(path, layer=chosen)['fields'])
    check_columns(available)
    frame = pyogrio.read_dataframe(path, layer=chosen, columns=[c for c in columns if c in available], read_geometry=False)
    return [{key: None if pd.isna(value) else value for key, value in row.items()}
            for row in frame.to_dict(orient='records')]


def index_rows(rows, identity):
    result = {}
    for row in rows:
        value = row.get(identity)
        if value is None or isinstance(value, (dict, list, bool)) or not str(value).strip():
            raise ValueError('Missing or invalid stable identity')
        key = str(value)
        if key in result:
            raise ValueError('Duplicate stable identity')
        result[key] = row
    return result


def report(comparisons, states=STATES):
    states = set(STATES) | {s.strip().casefold() for s in states}
    checked = 0
    issues = []
    for before, after, location in comparisons:
        matched, issue = compare_value(before, after, location, states)
        checked += int(matched)
        if issue is not None:
            issues.append(issue)
    return {'passed': not issues, 'protected_values_checked': checked,
            'status': 'findings' if issues else 'preserved' if checked else 'no_protected_values',
            'issues': issues, 'scope': 'Explicit selectors only; no automatic approval, geometry or truth validation'}


def check_tables(before, after, identity, fields, states=STATES):
    left, right = index_rows(before, identity), index_rows(after, identity)
    comparisons = ((row.get(field, MISSING), right.get(key, {}).get(field, MISSING),
                    {'id': key, 'field': field}) for key, row in left.items() for field in fields)
    result = report(comparisons, states)
    result.update(upstream_rows=len(left), downstream_rows=len(right), identity=identity, fields=fields)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Null/blank/unknown stay unknown; protected states are compared without ranking. '
        'Exit 0: no findings (inspect checked count); 1: changes found; 2: invalid input.'))
    parser.add_argument('before')
    parser.add_argument('after')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fields', nargs='+', help='Table fields to protect; requires --id')
    mode.add_argument('--pointers', nargs='+', help='Explicit scalar JSON pointers, e.g. /status /review/state')
    parser.add_argument('--id', help='Stable identity field for tables; never compare by row order')
    parser.add_argument('--before-layer')
    parser.add_argument('--after-layer')
    parser.add_argument('--states', nargs='+', default=STATES, help='Additional protected textual states; unknown/candidate/spatial_candidate/hold always protected')
    args = parser.parse_args(argv)
    try:
        if args.fields:
            if not args.id:
                raise ValueError('--fields requires --id')
            columns = list(dict.fromkeys([args.id, *args.fields]))
            result = check_tables(table(args.before, columns, args.before_layer, require_columns=True),
                                  table(args.after, columns, args.after_layer), args.id, args.fields, args.states)
        else:
            if args.id or args.before_layer or args.after_layer:
                raise ValueError('JSON pointer mode does not accept identity or layer arguments')
            before, after = load_json(args.before), load_json(args.after)
            result = report(((pointer(before, p), pointer(after, p), p) for p in args.pointers), args.states)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result['passed'] else 1
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    sys.exit(main())
