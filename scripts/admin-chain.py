#!/usr/bin/env python3
"""Administrative chain of a site: the unit containing it at each requested level, for location maps.

Reads a GADM-style GeoPackage (layers ADM_0..ADM_n with GID_n / NAME_n fields), finds the unit that
contains the site at every level, and the next level's units inside each chosen unit as internal
boundaries. Geometries are simplified for display relative to each level's extent. Writes
chain.geojson, context.geojson, chain.json (contract admin-chain v1) and record.json to a new
directory. Units come from the source as they are; no boundary is drawn, merged or reviewed here.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from _budget import check_vector
from _delivery import bundle, digest, write_json

VERSION = '0.1.0'
MAX_CHILDREN = 5000


def parse_point(text):
    try:
        lon, lat = (float(part) for part in text.split(','))
    except ValueError as error:
        raise ValueError('--point must be LON,LAT in degrees') from error
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('--point must be LON,LAT in degrees')
    return lon, lat


def parse_levels(text):
    try:
        levels = [int(part) for part in text.split(',')]
    except ValueError as error:
        raise ValueError('--levels must list integers such as 0,1,2') from error
    if not levels or levels != sorted(set(levels)) or levels[0] < 0:
        raise ValueError('--levels must be distinct, ascending and non-negative')
    return levels


def load_site(args):
    from shapely.geometry import Point
    from _core import read_vector, reproject
    if args.point:
        lon, lat = parse_point(args.point)
        return Point(lon, lat)
    frame = read_vector(args.site, args.site_layer)
    if frame.crs is None:
        raise ValueError('Site file has no CRS')
    geometry = reproject(frame, 'EPSG:4326', purpose='locate').geometry.union_all()
    if geometry.is_empty:
        raise ValueError('Site file has no geometry')
    return geometry


def first(row, fields):
    for field in fields:
        value = row.get(field)
        # GADM writes "NA" where a name is unknown.
        if isinstance(value, str) and value.strip() and value.strip() != 'NA':
            return value.split('|')[0].strip()
    return None


class Level:
    """One administrative level: its layer, id field, parent id field and name fields."""

    def __init__(self, source, level, layer, fields, name_field):
        self.level, self.layer, self.key = level, layer, f'adm{level}'
        if 'GID_' + str(level) in fields:
            self.gid = f'GID_{level}'
        else:
            self.gid = next((f for f in ('country_gid', 'iso3', 'GID') if f in fields), None)
        if self.gid is None:
            raise ValueError(f'{layer} has no GID_{level} (or country_gid) field')
        own = [f'{name_field}_{level}', f'NAME_{level}'] if name_field else [f'NAME_{level}']
        self.names = [f for f in own + ['display_country_name', 'COUNTRY', 'country_name', 'NAME'] if f in fields]
        self.source = source

    def parent_field(self, parent):
        field = f'GID_{parent.level}'
        return field if field != self.gid else None


def open_level(source, level, layer, name_field):
    import pyogrio
    layers = {name for name, _ in pyogrio.list_layers(source)}
    if layer not in layers:
        raise ValueError(f'Layer {layer} not in source; available: {", ".join(sorted(layers))}')
    fields = list(pyogrio.read_info(source, layer=layer)['fields'])
    return Level(source, level, layer, fields, name_field)


def containing(level, site, probe):
    import pyogrio
    x, y = probe.x, probe.y
    box = (x - 1e-7, y - 1e-7, x + 1e-7, y + 1e-7)
    check_vector(level.source, level.layer, box)
    frame = pyogrio.read_dataframe(level.source, layer=level.layer, bbox=box)
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        raise ValueError(f'{level.layer} must be EPSG:4326')
    hits = frame[frame.geometry.covers(probe)]
    if len(hits) != 1:
        raise ValueError(f'{len(hits)} units of {level.layer} contain the site; expected exactly one')
    return hits.iloc[0]


def children(level, parent, parent_gid, bounds):
    import pyogrio
    field = level.parent_field(parent)
    if field is None:
        return None
    escaped = parent_gid.replace("'", "''")
    check_vector(level.source, level.layer, bounds)
    frame = pyogrio.read_dataframe(level.source, layer=level.layer, bbox=tuple(bounds), where=f"{field} = '{escaped}'")
    if len(frame) > MAX_CHILDREN:
        raise ValueError(f'{len(frame)} units of {level.layer} under {parent_gid} exceed {MAX_CHILDREN}')
    return frame.sort_values(level.gid).reset_index(drop=True)


def display(geometries, extent, ratio):
    """Simplify for display at a tolerance relative to the panel's extent, then round to 1e-6 degrees."""
    import numpy as np
    import shapely
    tolerance = extent * ratio
    try:
        simplified = shapely.coverage_simplify(np.asarray(geometries, dtype=object), tolerance)
    except shapely.errors.GEOSException:
        simplified = shapely.simplify(np.asarray(geometries, dtype=object), tolerance, preserve_topology=True)
    return [shapely.transform(geometry, lambda coords: np.round(coords, 6)) for geometry in simplified]


def extent_of(bounds):
    return max(bounds[2] - bounds[0], bounds[3] - bounds[1])


def rounded(bounds):
    return [round(float(value), 6) for value in bounds]


def feature(geometry, properties):
    from shapely.geometry import mapping
    return {'type': 'Feature', 'properties': properties, 'geometry': mapping(geometry)}


def write_geojson(path, features):
    path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')


def execute(args):
    levels = parse_levels(args.levels)
    labels = [part.strip() for part in args.labels.split(',')] if args.labels else None
    if labels is not None and len(labels) != len(levels):
        raise ValueError(f'--labels needs {len(levels)} names, one per level')
    source = str(Path(args.source).resolve())
    site = load_site(args)
    probe = site if site.geom_type == 'Point' else site.representative_point()
    ratio = 1 / args.detail

    chain, warnings = [], []
    for index, number in enumerate(levels):
        layer = args.adm0_layer if number == 0 and args.adm0_layer else args.layer_pattern.format(level=number)
        level = open_level(source, number, layer, args.name_field)
        row = containing(level, site, probe)
        name = first(row, level.names) or str(row[level.gid])
        parent = chain[-1] if chain else None
        if parent is not None:
            field = level.parent_field(parent['level'])
            if field and row.get(field) != parent['gid']:
                message = f'{layer} unit {row[level.gid]} has {field} {row.get(field)}, not {parent["gid"]}'
                if parent['level'].layer == args.adm0_layer:
                    warnings.append(message)
                else:
                    raise ValueError(message)
        chain.append({'level': level, 'row': row, 'gid': str(row[level.gid]), 'name': name,
                      'label': labels[index] if labels else name, 'bounds': rounded(row.geometry.bounds)})
    if site.geom_type != 'Point' and not chain[-1]['row'].geometry.buffer(1e-9).covers(site):
        warnings.append(f'The site extends past {chain[-1]["name"]}')

    chain_features, context_features, rows = [], [], []
    for index, unit in enumerate(chain):
        level = unit['level']
        geometry = display([unit['row'].geometry], extent_of(unit['bounds']), ratio)[0]
        chain_features.append(feature(geometry, {'id': level.key, 'key': level.key, 'level': level.level, 'gid': unit['gid'],
                                                 'name': unit['name'], 'label': unit['label']}))
        entry = {'key': level.key, 'level': level.level, 'layer': level.layer, 'gid': unit['gid'], 'name': unit['name'],
                 'label': unit['label'], 'bounds': unit['bounds'], 'children': None}
        if index + 1 < len(chain):
            below = chain[index + 1]['level']
            frame = children(below, level, unit['gid'], unit['bounds'])
            if frame is None:
                warnings.append(f'{below.layer} has no GID_{level.level} field; no internal boundaries for {unit["name"]}')
            else:
                shapes = display(list(frame.geometry), extent_of(unit['bounds']), ratio)
                for (_, child), geometry in zip(frame.iterrows(), shapes):
                    context_features.append(feature(geometry, {'key': below.key, 'parent': level.key, 'parent_gid': unit['gid'],
                                                               'gid': str(child[below.gid]), 'name': first(child, below.names)}))
                entry['children'] = {'key': below.key, 'count': len(frame)}
        rows.append(entry)

    site_geometry = site if site.geom_type == 'Point' else display([site], extent_of(site.bounds) or 1e-6, ratio)[0]
    chain_features.append(feature(site_geometry, {'id': 'site', 'key': 'site', 'level': None, 'gid': None,
                                                  'name': args.site_label, 'label': args.site_label}))
    stat = Path(source).stat()
    with bundle(args.output) as stage:
        write_geojson(stage / 'chain.geojson', chain_features)
        write_geojson(stage / 'context.geojson', context_features)
        files = {name: {'path': f'{name}.geojson', 'sha256': digest(stage / f'{name}.geojson'), 'features': count}
                 for name, count in (('chain', len(chain_features)), ('context', len(context_features)))}
        manifest = {'contract': 'admin-chain', 'schema_version': 1,
                    'site': {'kind': 'point' if site.geom_type == 'Point' else 'area', 'label': args.site_label,
                             'point': [round(probe.x, 6), round(probe.y, 6)], 'bounds': rounded(site.bounds)},
                    'levels': rows, 'files': files,
                    'source': {'name': Path(source).name, 'attribution': args.attribution, 'bytes': stat.st_size,
                               'modified': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
                               'adm0_layer': args.adm0_layer},
                    'display': {'detail': args.detail, 'precision_degrees': 1e-6},
                    'warnings': warnings, 'record': 'record.json'}
        write_json(stage / 'chain.json', manifest)
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'admin-chain', 'version': VERSION,
                                            'parameters': {key: value for key, value in vars(args).items() if key != 'output'},
                                            'source': {'path': source, **{k: manifest['source'][k] for k in ('bytes', 'modified')}},
                                            'files': files, 'warnings': warnings,
                                            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                            'assumptions': ['Each unit contains the site point (or the area\'s representative point)',
                                                            'Boundaries are the source\'s; GADM is not an authoritative boundary set',
                                                            f'Geometries are simplified for display at 1/{args.detail} of each level\'s extent']})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    site = parser.add_mutually_exclusive_group(required=True)
    site.add_argument('--point', help='Site as LON,LAT in degrees')
    site.add_argument('--site', help='Site area file (any CRS); its union is the site')
    parser.add_argument('--site-layer')
    parser.add_argument('--site-label', default='Site')
    parser.add_argument('--source', required=True, help='GeoPackage with one layer per level, e.g. GADM levels')
    parser.add_argument('--levels', default='0,1,2', help='Levels to chain, ascending (default 0,1,2)')
    parser.add_argument('--layer-pattern', default='ADM_{level}')
    parser.add_argument('--adm0-layer', help='Alternative level-0 layer, e.g. ADM_0_cn_review_full')
    parser.add_argument('--name-field', help='Name field prefix tried before NAME_n, e.g. NL_NAME for local names')
    parser.add_argument('--labels', help='Comma-separated display labels, one per level')
    parser.add_argument('--attribution', default='GADM 4.1')
    parser.add_argument('--detail', type=int, default=2500, help='Simplify to 1/detail of each level\'s extent (default 2500)')
    parser.add_argument('--output', required=True, help='New directory, e.g. <project>/inputs/location-chain')
    args = parser.parse_args()
    try:
        if args.detail < 100:
            raise ValueError('--detail must be at least 100')
        manifest = execute(args)
        print(json.dumps({'output': str(Path(args.output).absolute()), 'levels': [f'{row["key"]} {row["name"]}' for row in manifest['levels']],
                          'context': manifest['files']['context']['features'], 'warnings': manifest['warnings']}, ensure_ascii=False))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
