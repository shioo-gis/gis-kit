#!/usr/bin/env python3
"""Display vector at one map's scale: areas, land boundaries and coastline generalised for that view.

Reads polygons (a GeoPackage layer or GeoJSON) inside the view of one map — its centre, zoom, width and
height in Web Mercator with 512-px tiles, as gis-composer's inspect reports it — and generalises them
for that scale only: parts smaller than --min-area-px square pixels are dropped, the coverage is
simplified to --tolerance-px pixels with shared edges kept shared, land boundaries are the edges two
units share, and the coastline is the outline of their union. Pixels are the view's CSS pixels; a map
printed at a set dpi has finer ones (--profile print). Without --id-field units are named by their
source feature id (FID), unique within that source only. Writes areas.geojson,
boundaries.geojson, coast.geojson, display-vector.json (contract display-vector v1) and record.json
to a new directory. Display only: never measure or overlay these geometries.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil

from _budget import check_vector
from _delivery import bundle, digest, write_json

from _version import VERSION
R = 6378137.0
MAX_LAT = 85.05112878
FILES = ('areas', 'boundaries', 'coast')
# Half a pixel and 4 square pixels hide what is dropped on screen, where a CSS pixel is a device pixel.
SCREEN = {'tolerance_px': 0.5, 'min_area_px': 4.0}
# gis-composer prints a map at dpi / 96 device pixels per CSS pixel; --profile print applies the same rule to those.
CSS_DPI = 96
DEFAULT_DPI = 300
GENERATED_ID = 'fid'


def thresholds(args):
    """The dpi (None on screen), --tolerance-px and --min-area-px in view (CSS) pixels: given explicitly, else from the profile."""
    if args.dpi is not None and args.profile != 'print':
        raise ValueError('--dpi needs --profile print')
    if args.dpi is not None and not (CSS_DPI <= args.dpi <= 960):
        raise ValueError('--dpi must be 96..960')
    dpi = (args.dpi or DEFAULT_DPI) if args.profile == 'print' else None
    dpi = int(dpi) if dpi is not None and float(dpi).is_integer() else dpi
    ratio = CSS_DPI / dpi if dpi else 1
    tolerance = args.tolerance_px if args.tolerance_px is not None else round(SCREEN['tolerance_px'] * ratio, 4)
    min_area = args.min_area_px if args.min_area_px is not None else round(SCREEN['min_area_px'] * ratio ** 2, 4)
    return dpi, tolerance, min_area


def parse_view(text):
    """A view object (inline JSON or a file): center [lon, lat], zoom, width, height, optional tileSize (512)."""
    path = Path(text)
    view = json.loads(path.read_text(encoding='utf-8') if path.is_file() else text)
    view = view.get('view', view)
    try:
        lon, lat = (float(value) for value in view['center'])
        zoom, width, height = float(view['zoom']), int(view['width']), int(view['height'])
        tile = int(view.get('tileSize', view.get('tile_size', 512)))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('--view needs center [lon, lat], zoom, width and height') from error
    if not (-180 <= lon <= 180 and -MAX_LAT <= lat <= MAX_LAT) or not (-2 <= zoom <= 24) or width < 16 or height < 16 or tile not in (256, 512):
        raise ValueError('--view is out of range (lon/lat in degrees, zoom -2..24, size ≥ 16 px, tileSize 256 or 512)')
    return {'center': [lon, lat], 'zoom': zoom, 'width': width, 'height': height, 'tileSize': tile}


def mercator(lon, lat):
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def geographic(x, y):
    return math.degrees(x / R), math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)


def frame_of(view, margin):
    """Metres per pixel and the view rectangle (with margin) in Web Mercator metres."""
    mpp = 2 * math.pi * R / (view['tileSize'] * 2 ** view['zoom'])
    cx, cy = mercator(*view['center'])
    half_w, half_h = view['width'] / 2 * (1 + 2 * margin) * mpp, view['height'] / 2 * (1 + 2 * margin) * mpp
    return mpp, (cx - half_w, max(cy - half_h, mercator(0, -MAX_LAT)[1]), cx + half_w, min(cy + half_h, mercator(0, MAX_LAT)[1]))


def to_mercator(geometries):
    import numpy as np
    import shapely

    def forward(coords):
        lat = np.clip(coords[:, 1], -MAX_LAT, MAX_LAT)
        return np.column_stack([R * np.radians(coords[:, 0]), R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))])
    return shapely.transform(geometries, forward)


def decimals_for(mpp):
    """Decimal places of a degree that resolve about a twentieth of a pixel (at most 6)."""
    return max(1, min(6, math.ceil(-math.log10(mpp / 20 / 111320))))


def to_geographic(geometry, decimals):
    import numpy as np
    import shapely

    def inverse(coords):
        return np.round(np.column_stack([np.degrees(coords[:, 0] / R), np.degrees(2 * np.arctan(np.exp(coords[:, 1] / R)) - np.pi / 2)]), decimals)
    return shapely.transform(geometry, inverse)


def polygons(geometry):
    """Polygon parts of any geometry (GeometryCollections from validity repair included)."""
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [part for child in getattr(geometry, 'geoms', []) for part in polygons(child)]


def lines(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == 'LineString':
        return [geometry]
    return [part for child in getattr(geometry, 'geoms', []) for part in lines(child)]


def read_source(source, layer, id_field, fields, bbox):
    """Units inside bbox, ordered by id. Without id_field the id is the source feature id (FID), the
    same for a feature in every view of the source, in the GENERATED_ID column."""
    import pyogrio
    info = pyogrio.read_info(source, layer=layer)
    available = list(info['fields'])
    missing = [field for field in [id_field, *fields] if field is not None and field not in available]
    if missing:
        raise ValueError(f'Fields not in source: {", ".join(missing)}; available: {", ".join(available)}')
    if id_field is None and GENERATED_ID in fields:
        raise ValueError(f'--fields has {GENERATED_ID}, the name of the generated id; give it as --id-field instead')
    crs = info.get('crs') or ''
    if crs and crs not in ('EPSG:4326', 'OGC:CRS84'):
        raise ValueError(f'Source must be EPSG:4326 longitude/latitude, not {crs}')
    check_vector(source, layer, bbox)
    if id_field is None:
        frame = pyogrio.read_dataframe(source, layer=layer, bbox=bbox, columns=fields, fid_as_index=True)
        frame = frame[frame.geometry.notna()]
        if frame.index.duplicated().any():
            raise ValueError('Source feature ids repeat; give --id-field')
        frame = frame.rename_axis(GENERATED_ID).reset_index().astype({GENERATED_ID: 'int64'})
        return frame.sort_values(GENERATED_ID).reset_index(drop=True)
    frame = pyogrio.read_dataframe(source, layer=layer, bbox=bbox, columns=[id_field, *fields])
    frame = frame[frame.geometry.notna()]
    if frame[id_field].isna().any() or frame[id_field].duplicated().any():
        raise ValueError(f'{id_field} must be present and unique within the view')
    return frame.sort_values(id_field, key=lambda column: column.astype(str)).reset_index(drop=True)


def generalise(frame, rect, mpp, tolerance_px, min_area_px, keep_rows=()):
    """Clip, drop small parts, coverage-simplify, then derive land boundaries and the coastline (all in metres).
    Units in keep_rows that would vanish keep their largest part: a required small unit (Malta in an
    EU map) is shown exaggerated rather than silently left out."""
    import numpy as np
    import shapely
    min_area = min_area_px * mpp ** 2
    clipped = shapely.clip_by_rect(to_mercator(np.asarray(frame.geometry.values, dtype=object)), *rect)
    counts = {'source_vertices': int(shapely.get_num_coordinates(clipped).sum()), 'parts_dropped': 0}
    # Drop small parts first: they are most of the vertices, and no other unit shares their edges.
    kept = []
    for row, geometry in enumerate(shapely.make_valid(clipped)):
        parts = polygons(geometry)
        large = [part for part in parts if part.area >= min_area]
        if not large and parts and row in keep_rows:
            large = [max(parts, key=lambda part: part.area)]
        counts['parts_dropped'] += len(parts) - len(large)
        kept.append(shapely.MultiPolygon(large) if large else shapely.MultiPolygon())
    tolerance = tolerance_px * mpp
    try:
        simplified = shapely.coverage_simplify(np.asarray(kept, dtype=object), tolerance)
    except shapely.errors.GEOSException:
        simplified = shapely.simplify(np.asarray(kept, dtype=object), tolerance, preserve_topology=True)
    grid = mpp / 20
    areas = []
    for row, geometry in enumerate(simplified):
        candidates = polygons(shapely.make_valid(shapely.set_precision(geometry, grid)))
        parts = [part for part in candidates if part.area >= min_area or (row in keep_rows and part.area > 0)]
        counts['parts_dropped'] += len(polygons(geometry)) - len(parts)
        areas.append(shapely.MultiPolygon(parts) if parts else None)
    # The union, closed over sub-pixel gaps between neighbours; its outline is the coastline. Closed in
    # floating precision: on the 1/20-pixel grid the inward buffer can collapse to nothing for a snap of
    # a few cells (0.16 px, the print profile at 300 dpi, did on two squares).
    snap = tolerance
    present = [area for area in areas if area is not None]
    union = shapely.set_precision(shapely.union_all(present), 0).buffer(snap, join_style='mitre').buffer(-snap, join_style='mitre') if present else shapely.Polygon()
    outline = union.boundary
    # Land boundaries: the part of each unit's outline lying along another unit (a sub-pixel gap or
    # overlap between neighbours still counts), taken once per pair, without the coast near its ends.
    along_coast = outline.buffer(mpp / 10)
    tree = shapely.STRtree([area if area is not None else shapely.Point() for area in areas])
    boundaries = []
    for i, area in enumerate(areas):
        if area is None:
            continue
        for j in sorted(int(k) for k in tree.query(area.buffer(snap))):
            if j <= i or areas[j] is None:
                continue
            shared = shapely.difference(shapely.intersection(area.boundary, areas[j].buffer(snap)), along_coast)
            segments = [segment for segment in lines(shapely.line_merge(shapely.union_all(lines(shared)))) if segment.length >= mpp] if lines(shared) else []
            if segments:
                boundaries.append((i, j, shapely.MultiLineString(segments)))
    # Coastline: that outline without the clipping frame.
    frame_edge = shapely.box(*rect).boundary.buffer(mpp / 2)
    coast = [segment for segment in lines(shapely.line_merge(shapely.difference(outline, frame_edge))) if segment.length >= mpp]
    counts['display_vertices'] = int(sum(shapely.get_num_coordinates(area) for area in present))
    return areas, boundaries, coast, counts


def feature(geometry, properties, decimals):
    from shapely.geometry import mapping
    return {'type': 'Feature', 'properties': properties, 'geometry': mapping(to_geographic(geometry, decimals))}


def write_geojson(path, features):
    path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')


def plain(value):
    """JSON-safe attribute values (numpy scalars, NaN as null)."""
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def cache_key(identity, parameters, view):
    text = json.dumps({'tool': 'display-vector', 'version': VERSION, 'source': identity, 'parameters': parameters, 'view': view}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def execute(args):
    view = parse_view(args.view)
    fields = [field.strip() for field in args.fields.split(',') if field.strip()] if args.fields else []
    source = Path(args.source).resolve()
    stat = source.stat()
    layer = args.layer
    identity = {'name': source.name, 'layer': layer, 'bytes': stat.st_size, 'modified_ns': stat.st_mtime_ns}
    keep = sorted({item.strip() for item in args.keep.split(',') if item.strip()}) if args.keep else []
    if keep and args.id_field is None:
        raise ValueError('--keep names units by --id-field; give --id-field (generated ids are source feature ids, not names)')
    dpi, tolerance_px, min_area_px = thresholds(args)
    id_name = args.id_field or GENERATED_ID
    parameters = {'id_field': args.id_field, 'fields': fields, 'profile': args.profile, 'dpi': dpi,
                  'tolerance_px': tolerance_px, 'min_area_px': min_area_px, 'margin': args.margin, 'keep': keep}
    mpp, rect = frame_of(view, args.margin)
    west, south = geographic(rect[0], rect[1])
    east, north = geographic(rect[2], rect[3])
    bbox = [round(value, 6) for value in (west, south, east, north)]
    key = cache_key(identity, parameters, view)
    cached = Path(args.cache) / f'display-vector-{key}' if args.cache else None
    with bundle(args.output) as stage:
        if cached is not None and all((cached / f'{name}.geojson').is_file() for name in FILES) and (cached / 'counts.json').is_file():
            for name in FILES:
                shutil.copyfile(cached / f'{name}.geojson', stage / f'{name}.geojson')
            stored = json.loads((cached / 'counts.json').read_text(encoding='utf-8'))
            counts, warnings, cache_status = stored['counts'], stored['warnings'], 'hit'
        else:
            frame = read_source(str(source), layer, args.id_field, fields, tuple(bbox))
            if frame.empty:
                raise ValueError('No features inside the view')
            keep_rows = {i for i, value in enumerate(frame[id_name]) if str(plain(value)) in keep}
            areas, boundaries, coast, counts = generalise(frame, rect, mpp, tolerance_px, min_area_px, keep_rows)
            counts['kept_small'] = sorted(str(plain(frame.at[i, id_name])) for i in keep_rows if areas[i] is not None and areas[i].area < min_area_px * mpp ** 2)
            ids = [plain(value) for value in frame[id_name]]
            decimals = decimals_for(mpp)
            area_features = [feature(area, {id_name: ids[i], **{field: plain(frame.at[i, field]) for field in fields}}, decimals)
                             for i, area in enumerate(areas) if area is not None]
            write_geojson(stage / 'areas.geojson', area_features)
            write_geojson(stage / 'boundaries.geojson', [feature(geometry, {'a': ids[i], 'b': ids[j]}, decimals) for i, j, geometry in boundaries])
            write_geojson(stage / 'coast.geojson', [feature(segment, {}, decimals) for segment in coast])
            dropped = [ids[i] for i, area in enumerate(areas) if area is None]
            counts = {**counts, 'areas': len(area_features), 'units_dropped': dropped, 'boundaries': len(boundaries), 'coast': len(coast)}
            warnings = [f'{len(dropped)} units are smaller than {min_area_px} px² at this scale and were left out: {", ".join(map(str, dropped))}'] if dropped else []
            cache_status = 'miss' if cached is not None else 'off'
            if cached is not None:
                partial = cached.with_name(cached.name + '.partial')
                shutil.rmtree(partial, ignore_errors=True)
                partial.mkdir(parents=True)
                for name in FILES:
                    shutil.copyfile(stage / f'{name}.geojson', partial / f'{name}.geojson')
                write_json(partial / 'counts.json', {'counts': counts, 'warnings': warnings})
                partial.rename(cached) if not cached.exists() else shutil.rmtree(partial)
        files = {name: {'path': f'{name}.geojson', 'sha256': digest(stage / f'{name}.geojson'),
                        'features': len(json.loads((stage / f'{name}.geojson').read_text(encoding='utf-8'))['features'])} for name in FILES}
        manifest = {'contract': 'display-vector', 'schema_version': 1,
                    'view': {**view, 'meters_per_pixel': round(mpp, 3), 'bbox': bbox},
                    'display': {'profile': args.profile, **({'dpi': dpi} if dpi else {}), 'tolerance_px': tolerance_px, 'min_area_px': min_area_px,
                                'margin': args.margin, 'precision_degrees': 10 ** -decimals_for(mpp)},
                    'files': files, 'counts': counts,
                    'source': {'name': source.name, 'layer': layer, 'id_field': id_name, 'id_generated': args.id_field is None, 'fields': fields, 'attribution': args.attribution,
                               'bytes': stat.st_size, 'modified': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds')},
                    'warnings': warnings, 'record': 'record.json'}
        write_json(stage / 'display-vector.json', manifest)
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'display-vector', 'version': VERSION,
                                            'parameters': {**parameters, 'layer': layer, 'view': view}, 'cache': {'status': cache_status, 'key': key},
                                            'source': {'path': str(source), 'bytes': stat.st_size, 'modified': manifest['source']['modified']},
                                            'files': files, 'counts': counts, 'warnings': warnings,
                                            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                            'assumptions': ['Generalised for the stated view only: zoom, size and centre fix the pixel size',
                                                            'Land boundaries are edges two units share (within the simplification tolerance); the source boundaries are not reviewed here',
                                                            'Display geometry; do not measure, overlay or analyse it',
                                                            *([f'No --id-field: units and both sides of each boundary are named by {GENERATED_ID}, the source feature id, '
                                                               'unique within this source only; do not join other data on it'] if args.id_field is None else [])]})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Polygons in EPSG:4326: a GeoPackage (with --layer) or GeoJSON')
    parser.add_argument('--layer', help='Layer name in a multi-layer source, e.g. ADM_0_cn_review_full')
    parser.add_argument('--id-field', help='Unique id per unit, kept on areas and naming both sides of each boundary; '
                        f'without it units are named by {GENERATED_ID}, the source feature id (unique within this source only)')
    parser.add_argument('--fields', help='Comma-separated attributes to keep on areas')
    parser.add_argument('--view', required=True, help='The map view as JSON or a JSON file: center, zoom, width, height (gis-composer inspect)')
    parser.add_argument('--profile', choices=('screen', 'print'), default='screen',
                        help='screen: --tolerance-px 0.5, --min-area-px 4; print: the same in printed pixels, 0.5 x 96/dpi and 4 x (96/dpi)² (0.16 and 0.41 at 300 dpi)')
    parser.add_argument('--dpi', type=float, help=f'With --profile print: the dpi the map is printed at (gis-composer layout.dpi or print.dpi; default {DEFAULT_DPI})')
    parser.add_argument('--tolerance-px', type=float, help='Simplification tolerance in view pixels (default from --profile; 0.5 on screen)')
    parser.add_argument('--min-area-px', type=float, help='Drop parts smaller than this many square view pixels (default from --profile; 4 on screen)')
    parser.add_argument('--keep', help='Comma-separated ids that must stay even when smaller than --min-area-px (their largest part is kept)')
    parser.add_argument('--margin', type=float, default=0.05, help='Read this share of the view beyond each edge (default 0.05)')
    parser.add_argument('--attribution', default='GADM 4.1')
    parser.add_argument('--cache', help='Directory reused across runs; keyed by source identity, parameters and view')
    parser.add_argument('--output', required=True, help='New directory, e.g. <project>/inputs/europe-display')
    args = parser.parse_args()
    try:
        _, tolerance_px, min_area_px = thresholds(args)
        if not (0.05 <= tolerance_px <= 5) or not (0 <= min_area_px <= 400) or not (0 <= args.margin <= 0.5):
            raise ValueError('--tolerance-px must be 0.05..5, --min-area-px 0..400 and --margin 0..0.5')
        manifest = execute(args)
        print(json.dumps({'output': str(Path(args.output).absolute()), 'counts': manifest['counts'],
                          'meters_per_pixel': manifest['view']['meters_per_pixel'], 'warnings': manifest['warnings']}, ensure_ascii=False))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
