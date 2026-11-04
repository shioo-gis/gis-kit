#!/usr/bin/env python3
"""Unpack an ArcGIS layer or map package and say what is in it; pack a map-style map for QGIS or ArcGIS Pro.

`package.py unpack` lists the archive (7z through bsdtar or 7-Zip, or zip) and refuses absolute paths, '..',
links and archives larger than --max-bytes before extracting anything; it then checks the extracted sizes against
the listing. It reads the package information (esriinfo), writes a describe card for every dataset (shapefiles,
file geodatabases, rasters) under commondata/ or in a layer version folder (v10/, v107/ ..., one copy of a dataset
repeated there), and matches each layer file to its data: .lyrx (ArcGIS Pro)
is JSON and its renderer fields, label expression and definition query are read; .lyr (ArcMap) is a binary
format, so only its data-source path and the names it mentions are picked out. Layers whose data is not in the
package (packaged by reference, or enterprise data left out) are reported. Symbology is not reproduced.

`package.py pack` reads a map-style v1 document (gis-plugin contracts) and the data it names, and writes one
GeoPackage that QGIS opens with the map's styles: one table per dataset in its own CRS (a dataset drawn by several
layers is stored once), each layer's style as QML in layer_styles, the precomputed label positions as a point layer
(and leader lines), and a QGIS project inside the GeoPackage with the layer order, groups and map extent. The data
kept is chosen by the map extent: whole features that touch it (default), features cut at it (--clip, with a
`clipped` field), or whole datasets (--full); rasters are cut to the extent's window. A manifest lists every
dataset's source, scope and feature count, what could not be expressed in QGIS, and the fonts the map uses.

`package.py pack --format arcgis-pro` keeps the same data selection and writes an ArcGIS Pro layer package
instead: a 7z .lpkx with the vector data in a file geodatabase (rasters as GeoTIFFs) under commondata/, one CIM
layer file in p30/ with a group layer for the map (panels and groups as group layers, renderers, definition
queries, Maplex label classes, the label placements as a labelled point layer), and esriinfo/. --lyrx also keeps
the layer file beside the unpacked data. `package.py pack --format arcmap` writes an ArcMap layer package (.lpk) the same way,
with binary layer files (.lyr, _arcmap_style.py) in place of the CIM one; --lyr keeps one beside the data. Only the style and
container writer differs between the formats.

`package.py read` goes the other way: a platform style (QGIS .qml, ArcGIS Pro .lyrx, .stylx) into a map-style
document, reporting what could not be carried; the readers live in _style_read.py and its platform modules.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from _budget import check_raster, check_vector
from _delivery import bundle, digest, write_json
import _map_style
import _style_read

VERSION = '0.1.0'
SUFFIXES = {'.lpk', '.lpkx', '.mpk', '.mpkx'}
RASTER_SUFFIXES = {'.tif', '.tiff', '.img', '.jp2', '.sid', '.ecw', '.dem', '.asc'}
DEFAULT_MAX_BYTES = 20 * 2**30


def bsdtar():
    """libarchive's tar: `bsdtar`, or a `tar` that is libarchive underneath (Windows' tar.exe); GNU tar reads no 7z."""
    tool = shutil.which('bsdtar')
    if tool:
        return tool
    tool = shutil.which('tar')
    if tool is None:
        return None
    try:
        version = subprocess.run([tool, '--version'], capture_output=True, text=True, errors='replace', timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return tool if 'bsdtar' in version else None


def archive_kind(path):
    with open(path, 'rb') as stream:
        magic = stream.read(6)
    if magic == b"7z\xbc\xaf'\x1c":
        return '7z'
    if magic[:2] == b'PK':
        return 'zip'
    raise ValueError(f'{path} is neither a 7z nor a zip archive')


def unescape(raw):
    """bsdtar listings escape non-ASCII bytes as \\ooo and a backslash as \\\\."""
    out, i = bytearray(), 0
    data = raw.encode('latin-1', 'surrogateescape') if isinstance(raw, str) else raw
    while i < len(data):
        if data[i] == 0x5C and data[i+1:i+4].isdigit() and len(data[i+1:i+4]) == 3:
            out.append(int(data[i+1:i+4], 8))
            i += 4
        elif data[i] == 0x5C and data[i+1:i+2] == b'\\':
            out.append(0x5C)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return out.decode('utf-8', 'replace')


def list_members(path, kind):
    """[(name, size, is_dir, is_link)] and the tool used to read the archive."""
    if kind == 'zip':
        with zipfile.ZipFile(path) as archive:
            rows = []
            for info in archive.infolist():
                mode = info.external_attr >> 16
                rows.append((info.filename, info.file_size, info.is_dir(), (mode & 0o170000) == 0o120000))
        return rows, 'zipfile'
    tool = bsdtar()
    if tool:
        run = subprocess.run([tool, '-tvf', str(path)], capture_output=True)
        if run.returncode != 0:
            raise ValueError(f'bsdtar cannot list {path}: {run.stderr.decode(errors="replace").strip()}')
        rows = []
        for line in run.stdout.decode('latin-1').splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, size, name = parts[0], parts[4], parts[8]
            link = perms.startswith(('l', 'h')) or ' -> ' in name or ' link to ' in name
            rows.append((unescape(name), int(size), perms.startswith('d'), link))
        return rows, 'bsdtar'
    tool = shutil.which('7z') or shutil.which('7za') or shutil.which('7zz')
    if tool:
        run = subprocess.run([tool, 'l', '-slt', '-ba', str(path)], capture_output=True, text=True, errors='replace')
        if run.returncode != 0:
            raise ValueError(f'7-Zip cannot list {path}: {run.stderr.strip()}')
        rows, entry = [], {}
        for line in run.stdout.splitlines() + ['']:
            if not line.strip():
                if 'Path' in entry:
                    attributes = entry.get('Attributes', '')
                    rows.append((entry['Path'].replace('\\', '/'), int(entry.get('Size') or 0), attributes.startswith('D') or 'D' in attributes[:1],
                                 'l' in attributes.split(' ')[-1][:1] if ' ' in attributes else False))
                entry = {}
                continue
            key, _, value = line.partition(' = ')
            entry[key.strip()] = value
        return rows, Path(tool).name
    raise ValueError('Reading a 7z package needs bsdtar (libarchive; tar.exe on Windows) or 7-Zip; neither was found')


def check_members(rows, max_bytes):
    """Refuse members that would land outside the target folder, links, and oversized archives."""
    bad = []
    for name, _, _, link in rows:
        posix = PurePosixPath(name.replace('\\', '/'))
        if posix.is_absolute() or re.match(r'^[A-Za-z]:', name) or '..' in posix.parts:
            bad.append(f'{name} (outside the package folder)')
        elif link:
            bad.append(f'{name} (link)')
    if bad:
        raise ValueError('Refusing to unpack: ' + '; '.join(bad[:5]) + (' …' if len(bad) > 5 else ''))
    total = sum(size for _, size, is_dir, _ in rows if not is_dir)
    if total > max_bytes:
        raise ValueError(f'Package expands to {total/2**30:.2f} GiB, above --max-bytes {max_bytes/2**30:.2f} GiB')
    return total


def extract(path, kind, tool, target):
    target.mkdir()
    if kind == 'zip':
        with zipfile.ZipFile(path) as archive:
            archive.extractall(target)
        return
    if tool == 'bsdtar':
        command = [bsdtar(), '-xf', str(path), '-C', str(target), '--no-same-owner']
    else:
        command = [shutil.which(tool), 'x', '-y', f'-o{target}', str(path)]
    run = subprocess.run(command, capture_output=True, text=True, errors='replace')
    if run.returncode != 0:
        raise ValueError(f'{tool} could not unpack {path}: {run.stderr.strip()[-300:]}')


def check_extracted(target, expected):
    total = 0
    for item in target.rglob('*'):
        if item.is_symlink():
            raise ValueError(f'Unpacked a link: {item.relative_to(target)}')
        if not item.resolve().is_relative_to(target.resolve()):
            raise ValueError(f'Unpacked outside the package folder: {item}')
        if item.is_file():
            total += item.stat().st_size
    if total != expected:
        raise ValueError(f'Unpacked {total} bytes, the listing says {expected}')


def xml_fields(path, names):
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {'unreadable': True}
    values = {name: (root.findtext(name) or '').strip() or None for name in names}
    extent = root.find('extent')
    if extent is not None:
        values['extent'] = [float(extent.findtext(k) or 'nan') for k in ('xmin', 'ymin', 'xmax', 'ymax')]
    keywords = [k.text for k in root.iter('typekeyword') if k.text]
    if keywords:
        values['typekeywords'] = keywords
    return values


def dataset_kind(item):
    suffix = item.suffix.lower()
    return ('filegdb' if item.is_dir() and suffix == '.gdb'
            else 'shapefile' if item.is_file() and suffix == '.shp'
            else 'raster' if item.is_file() and suffix in RASTER_SUFFIXES else None)


def dataset_key(path, versions):
    """A dataset's path as layer files name it: relative to commondata/, or to its layer version folder."""
    posix = PurePosixPath(path)
    if posix.parts[0] == 'commondata':
        return str(posix.relative_to('commondata'))
    folder = next((v for v in versions if PurePosixPath(v) in posix.parents), None)
    return str(posix.relative_to(folder)) if folder else path


def fingerprint(item):
    """Names, sizes and digests of a dataset's files (a geodatabase folder, or a shapefile and its sidecars)."""
    files = (sorted(p for p in item.rglob('*') if p.is_file()) if item.is_dir()
             else sorted(p for p in item.parent.iterdir() if p.is_file() and (p == item or p.name.startswith(item.stem + '.'))))
    return [(p.relative_to(item if item.is_dir() else item.parent).as_posix(), p.stat().st_size, digest(p)) for p in files]


def datasets(root, versions=()):
    """Shapefiles, file geodatabases and rasters, with describe cards, and warnings about repeated copies.

    Data sits under commondata/, but ArcMap packages also put data in the layer version folders (v10/, v107/ ...),
    where each folder's .lyr names it by a relative path. A dataset repeated in several version folders is listed
    once, from the newest folder, when every copy is identical; differing copies are each listed.
    """
    from describe import describe
    groups = {}
    for item in sorted(root.rglob('*')):
        relative = item.relative_to(root)
        kind = dataset_kind(item)
        if kind is None or relative.parts[0] == 'esriinfo' or any(p.suffix.lower() == '.gdb' for p in relative.parents):
            continue
        path = relative.as_posix()
        key = path if relative.parts[0] == 'commondata' else dataset_key(path, versions)
        groups.setdefault(key, []).append((item, path, kind))
    rank = {folder: i for i, folder in enumerate(versions)}
    found, warnings = [], []
    for key, copies in sorted(groups.items(), key=lambda pair: (not pair[1][0][1].startswith('commondata/'), pair[0])):
        # newest version folder first; a path outside any version folder ranks as the oldest
        copies.sort(key=lambda copy: -rank.get(next((v for v in versions if copy[1].startswith(v + '/')), None), -1))
        if len(copies) > 1:
            prints = [fingerprint(item) for item, _, _ in copies]
            if any(p != prints[0] for p in prints[1:]):
                warnings.append(f'{len(copies)} copies of {key} differ ({", ".join(c[1] for c in copies)}); each is listed')
                listed = [(copy, []) for copy in copies]
            else:
                listed = [(copies[0], [c[1] for c in copies[1:]])]
        else:
            listed = [(copies[0], [])]
        for (item, path, kind), others in listed:
            row = {'path': path, 'kind': kind, 'name': item.stem}
            if others:
                row['copies'] = others
            try:
                card = describe(item)
                card['path'] = path
                row['card'] = card
            except Exception as error:  # an unreadable dataset is reported, not fatal
                row['error'] = f'{type(error).__name__}: {error}'
            found.append(row)
    return found, warnings


def utf16_strings(data, minimum=3):
    """Readable UTF-16LE runs in a binary file, at both byte alignments."""
    allowed = re.compile(r"[\w\\/.:\[\]()=<>'\"%,+\- 　-〿一-鿿＀-￯]" + f'{{{minimum},}}')
    found = []
    for offset in (0, 1):
        text = data[offset:len(data) - (len(data) - offset) % 2].decode('utf-16le', 'replace')
        found += allowed.findall(text)
    return list(dict.fromkeys(s.strip() for s in found if s.strip()))


def lyr_summary(path):
    """What can be read from an ArcMap .lyr without the ArcObjects format: data path, names, label expression."""
    strings = utf16_strings(path.read_bytes(), minimum=2)  # two-character names (水系, 道路) are common
    sources = [s for s in strings if 'commondata' in s.lower() or re.search(r'\.(gdb|shp|sde)\b', s, re.I)]
    labels = [s for s in strings if re.fullmatch(r'\[[^\]]+\]', s)]
    return {'format': 'lyr (ArcMap binary)', 'data_sources': sources, 'label_expressions': labels, 'strings': strings,
            'symbology': 'not read: binary .lyr'}


def lyrx_summary(path):
    """Layers of an ArcGIS Pro .lyrx (CIM JSON): data connection, renderer fields and classes, labels, query."""
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    layers = []
    for definition in document.get('layerDefinitions', []):
        table = definition.get('featureTable') or {}
        connection = table.get('dataConnection') or definition.get('dataConnection') or {}
        renderer = definition.get('renderer') or {}
        fields = renderer.get('fields') or ([renderer['field']] if renderer.get('field') else [])
        classes = []
        for group in renderer.get('groups', []):
            for item in group.get('classes', []):
                values = [v for value in item.get('values', []) for v in value.get('fieldValues', [])]
                classes.append({'label': item.get('label'), 'values': values})
        for item in renderer.get('breaks', []):
            classes.append({'label': item.get('label'), 'upper_bound': item.get('upperBound')})
        layers.append({'name': definition.get('name'), 'type': definition.get('type'),
                       'workspace': connection.get('workspaceConnectionString'), 'dataset': connection.get('dataset'),
                       'definition_query': table.get('definitionExpression'),
                       'renderer': renderer.get('type'), 'renderer_fields': fields, 'classes': classes,
                       'label_expressions': [c.get('expression') for c in definition.get('labelClasses', []) if c.get('expression')]})
    return {'format': 'lyrx (ArcGIS Pro CIM JSON)', 'layers': layers,
            'symbology': 'classes and labels read; colours and symbols not reproduced'}


def layer_files(root):
    """Layer files by format; ArcMap packages hold one folder per ArcGIS version (v10, v105, ...), newest kept."""
    files = []
    lyr_folders = sorted({p.parent for p in root.rglob('*.lyr')}, key=lambda p: [int(n) for n in re.findall(r'\d+', p.name)] or [0])
    if lyr_folders:
        newest = lyr_folders[-1]
        files += [(p, lyr_summary(p)) for p in sorted(newest.glob('*.lyr'))]
    for path in sorted(root.rglob('*.lyrx')):
        try:
            files.append((path, lyrx_summary(path)))
        except (ValueError, UnicodeDecodeError) as error:
            files.append((path, {'format': 'lyrx', 'error': str(error)}))
    for path in sorted(root.rglob('*.mxd')) + sorted(root.rglob('*.mapx')):
        files.append((path, {'format': path.suffix.lower().lstrip('.'), 'note': 'map document; layers not read'}))
    return files, [p.relative_to(root).as_posix() for p in lyr_folders]


def source_key(text):
    """A .lyr data source as (in commondata, path relative to commondata/ or to the layer's own folder), lower case."""
    path = text.replace('\\', '/').strip('/').lower()
    if path.endswith('commondata'):
        return True, '.'
    if 'commondata/' in path:
        return True, path[path.rindex('commondata/') + len('commondata/'):]
    return False, path.removeprefix('./')


def gdb_layers(row, strings):
    """'<gdb>:<layer>' for the geodatabase layers a .lyr names; a name may run into neighbouring binary characters."""
    names = [layer['name'] for layer in (row.get('card') or {}).get('layers', [])]
    return [f"{row['path']}:{name}" for name in names if len(name) >= 3 and any(name in s for s in strings)]


def match(summary, data, layer_name, versions=(), folder=''):
    """Datasets a layer file points at, by name and data path; empty when its data is not in the package.

    A .lyr path outside commondata/ is relative to the layer file's own folder, so only a dataset there (or
    an identical copy listed for it) can match.
    """
    hits = []
    if summary['format'].startswith('lyrx'):
        for layer in summary['layers']:
            workspace = (layer.get('workspace') or '').replace('\\', '/').lower()
            dataset = layer.get('dataset')
            for row in data:
                inside = row['path'].replace('\\', '/').lower()
                if not dataset:
                    continue
                if row['kind'] == 'filegdb':  # a geodatabase is named by its workspace; the dataset is a layer inside
                    if workspace.endswith(PurePosixPath(inside).name):
                        hits.append(f"{row['path']}:{dataset}")
                elif dataset in (row['name'], PurePosixPath(row['path']).name):  # a raster's dataset is its file name
                    hits.append(row['path'])
        return sorted(set(hits))
    strings = set(summary.get('strings', []))
    sources = {source_key(s) for s in summary.get('data_sources', [])}
    commondata = any(inside for inside, _ in sources)
    for row in data:
        inside, key = row['path'].startswith('commondata/'), dataset_key(row['path'], versions).lower()
        if not inside and folder not in {str(PurePosixPath(p).parent) for p in [row['path'], *row.get('copies', [])]}:
            continue
        named = row['name'] in strings or row['name'] == layer_name
        if (inside, key) in sources or (named and ((inside, str(PurePosixPath(key).parent)) in sources or (inside and not commondata))):
            hits += (gdb_layers(row, strings) if row['kind'] == 'filegdb' else []) or [row['path']]
    return sorted(set(hits))


def unpack(args):
    source = Path(args.source)
    if source.suffix.lower() not in SUFFIXES or not source.is_file():
        raise ValueError(f'Source must be an existing {", ".join(sorted(SUFFIXES))} file')
    kind = archive_kind(source)
    rows, tool = list_members(source, kind)
    total = check_members(rows, args.max_bytes)
    stat = source.stat()
    with bundle(args.output) as stage:
        root = stage / 'package'
        extract(source, kind, tool, root)
        check_extracted(root, total)
        info = xml_fields(root / 'esriinfo' / 'item.pkinfo', ('name', 'version', 'type', 'ID'))
        item = xml_fields(root / 'esriinfo' / 'iteminfo.xml', ('name', 'title', 'summary', 'description', 'tags', 'type'))
        files, versions = layer_files(root)
        data, copy_warnings = datasets(root, versions)
        layers, used = [], set()
        for path, summary in files:
            name = re.sub(r'^\d{4}', '', path.stem)
            linked = match(summary, data, name, versions, path.parent.relative_to(root).as_posix()) if 'error' not in summary and summary['format'][:3] in ('lyr',) else []
            used.update(p.split(':')[0] for p in linked)
            layers.append({'file': path.relative_to(root).as_posix(), 'name': name, **summary, 'data': linked,
                           'data_in_package': bool(linked) if summary['format'][:3] == 'lyr' else None})
            layers[-1].pop('strings', None)
        missing = [layer['name'] for layer in layers if layer['data_in_package'] is False]
        warnings = list(copy_warnings)
        if not data:
            warnings.append('No datasets in the package: it holds layer definitions only (data packaged by reference)')
        if missing:
            warnings.append(f'{len(missing)} layers point at data that is not in the package: {", ".join(missing[:5])}')
        unused = [row['path'] for row in data if row['path'] not in used]
        if unused and layers:
            warnings.append(f'{len(unused)} datasets are not named by any layer file: {", ".join(unused[:5])}')
        for row in data:
            for warning in (row.get('card') or {}).get('warnings', []):
                warnings.append(f'{row["path"]}: {warning["message"]}')
            if 'error' in row:
                warnings.append(f'{row["path"]}: unreadable ({row["error"]})')
        thumbnail = next(iter(sorted((root / 'esriinfo').rglob('thumbnail.*'))), None) if (root / 'esriinfo').is_dir() else None
        manifest = {'manifest': 'esri-package', 'schema_version': 1,
                    'source': {'name': source.name, 'bytes': stat.st_size, 'sha256': digest(source), 'archive': kind,
                               'reader': tool, 'unpacked_bytes': total, 'members': len(rows)},
                    'package': info, 'item': item, 'layer_versions': versions, 'datasets': data, 'layers': layers,
                    'thumbnail': thumbnail.relative_to(stage).as_posix() if thumbnail else None,
                    'warnings': warnings, 'unpacked_to': 'package/'}
        write_json(stage / 'package.json', manifest)
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'package unpack', 'version': VERSION,
                                            'parameters': {k: v for k, v in vars(args).items() if k not in ('output', 'command')},
                                            'source': {'path': str(source.absolute()), 'sha256': manifest['source']['sha256']},
                                            'files': {'package.json': digest(stage / 'package.json')},
                                            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                            'assumptions': ['Archive members are checked before extraction; nothing outside package/ is written',
                                                            'Layer-to-data links in .lyr files are read from embedded strings, not the ArcObjects format',
                                                            'Symbology is not reproduced']})
    return manifest


# --- pack: map-style -> a platform package (QGIS GeoPackage, ArcGIS Pro layer package) -------------------------------

RESERVED_TABLES = {'layer_styles', 'qgis_projects', 'map_labels', 'map_label_leaders', 'map_background'}
TILE_KINDS = {'vectorTiles', 'rasterTiles'}
GPKG_RASTER_TYPES = {'uint8', 'int16', 'uint16', 'float32'}
LARGE_BYTES = 500 * 2**20
DIMENSIONS = {'point': 0, 'line': 1, 'polygon': 2}


def gpkg_version(gpkg):
    """Create the GeoPackage as version 1.2, which the GDAL in older QGIS builds reads without warnings."""
    return {} if gpkg.exists() else {'dataset_options': {'VERSION': '1.2'}}


def parse_margin(text):
    """--margin as a share of the extent's width and height: 0.1 or 10%."""
    if text is None:
        return 0.0
    value = float(text[:-1]) / 100 if text.endswith('%') else float(text)
    if not 0 <= value <= 1:
        raise ValueError('--margin must be between 0 and 100% of the extent')
    return value


def extent_bbox(document, margin):
    bbox = (document.get('extent') or {}).get('bbox')
    if bbox is None:
        return None
    west, south, east, north = bbox
    dx, dy = (east - west) * margin, (north - south) * margin
    return [max(-180.0, west - dx), max(-90.0, south - dy), min(180.0, east + dx), min(90.0, north + dy)]


def extent_in(bbox, crs):
    """The lon/lat extent as a polygon in crs, edges densified so a projected extent keeps its curved sides."""
    import numpy as np
    import shapely
    from pyproj import CRS, Transformer
    polygon = shapely.segmentize(shapely.box(*bbox), max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 64)
    if crs.equals(CRS.from_epsg(4326)):
        return polygon
    transformer = Transformer.from_crs(4326, crs, always_xy=True)
    return shapely.transform(polygon, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])))


def table_name(path, layer, taken):
    base = re.sub(r'\W+', '_', layer or path.stem).strip('_').lower() or 'data'
    if base[0].isdigit() or base.startswith(('gpkg_', 'rtree_', 'sqlite_')) or base in RESERVED_TABLES:
        base = 't_' + base
    name, n = base, 2
    while name in taken:
        name, n = f'{base}_{n}', n + 1
    taken.add(name)
    return name


def keep_dimension(geometry, dimension):
    """Parts of a clipped geometry with the layer's dimension (a cut polygon can leave lines and points on the edge)."""
    import shapely
    if geometry is None or geometry.is_empty:
        return None
    if shapely.get_dimensions(geometry) == dimension and geometry.geom_type != 'GeometryCollection':
        return geometry
    parts, stack = [], [geometry]
    while stack:
        item = stack.pop()
        if item.geom_type.startswith('Multi') or item.geom_type == 'GeometryCollection':
            stack.extend(item.geoms)
        elif not item.is_empty and shapely.get_dimensions(item) == dimension:
            parts.append(item)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {0: shapely.multipoints, 1: shapely.multilinestrings, 2: shapely.multipolygons}[dimension](parts)


def vector_layer_name(path, layer):
    import pyogrio
    layers = [row[0] for row in pyogrio.list_layers(path)]
    if layer is None:
        if len(layers) != 1:
            raise ValueError(f'{path.name} holds {len(layers)} layers ({", ".join(layers[:8])}); name one with data.layer')
        return layers[0]
    if layer not in layers:
        raise ValueError(f'{path.name} has no layer {layer!r} (layers: {", ".join(layers[:8])})')
    return layer


def select(geometries, selector, scope, geometry, ref):
    """(features kept, their geometries, which were cut) for one scope; geometries are shapely objects."""
    import numpy as np
    import shapely
    if scope == 'full':
        return np.ones(len(geometries), bool), geometries, None
    keep = ~shapely.is_missing(geometries) & shapely.intersects(geometries, selector)
    geometries = geometries[keep]
    if scope != 'clip':
        return keep, geometries, None
    cut = ~shapely.covered_by(geometries, selector)
    invalid = int((~shapely.is_valid(geometries[cut])).sum())
    if invalid:
        raise ValueError(f'{ref}: {invalid} invalid geometries cross the map extent and cannot be cut; repair them first (fix.py) '
                         'or pack without --clip')
    geometries = geometries.copy()
    geometries[cut] = [keep_dimension(g, DIMENSIONS[geometry]) for g in shapely.intersection(geometries[cut], selector)]
    survived = ~shapely.is_missing(geometries)  # a piece that only touches the edge leaves nothing of the layer's dimension
    keep[np.flatnonzero(keep)[~survived]] = False
    return keep, geometries[survived], cut[survived]


def declared_type(declared, geometries):
    """The geometry type to declare after cutting: multi when cutting split a single part (singles are promoted)."""
    import shapely
    base = declared.replace(' Z', '').replace(' M', '')
    if base.startswith('Multi') or base == 'Unknown' or not len(geometries):
        return declared, geometries
    if not any(g.geom_type.startswith('Multi') for g in geometries):
        return declared, geometries
    promote = {'Point': shapely.multipoints, 'LineString': shapely.multilinestrings, 'Polygon': shapely.multipolygons}.get(base)
    if promote is None:
        return 'Unknown', geometries
    geometries = geometries.copy()
    for index, item in enumerate(geometries):
        if not item.geom_type.startswith('Multi'):
            geometries[index] = promote([item])
    return 'Multi' + declared, geometries


def pack_vector(entry, gpkg, scope, bbox, geometry, driver='GPKG'):
    """Copy one vector dataset into the table entry['table'] of a GeoPackage (or, with driver OpenFileGDB, a file
    geodatabase); returns its row for the manifest.

    With pyarrow the attributes go through Arrow untouched (integer fields with NULLs stay integers, boolean
    subtypes stay boolean); without it they go through pandas, which can widen such fields, and this is warned.
    A file geodatabase stores 64-bit integers as doubles and booleans as 0/1 small integers (readable by every
    ArcGIS Pro 3.x) and renames fields that are SQL keywords; `renames` maps the source names to the written ones.
    """
    import warnings as python_warnings
    import numpy as np
    import pyogrio
    import shapely
    from pyproj import CRS
    path = entry['path']
    layer = vector_layer_name(path, entry['data_layer'])
    info = pyogrio.read_info(path, layer=layer)
    crs = CRS.from_user_input(info['crs']) if info.get('crs') else None
    warnings = []
    if scope != 'full' and crs is None:
        raise ValueError(f'{entry["ref"]} has no CRS, so the map extent cannot be placed on it; assign a CRS or use --full')
    selector = None if scope == 'full' else extent_in(bbox, crs)
    window = {} if selector is None else {'bbox': tuple(selector.bounds)}
    check_vector(path, layer, window.get('bbox'))
    if scope == 'clip' and 'clipped' in list(info['fields']):
        raise ValueError(f'{entry["ref"]} already has a field named "clipped"; --clip adds that field')
    try:
        import pyarrow as pa
        from pyogrio.raw import read_arrow, write_arrow
    except ImportError:
        pa = None
    options = gpkg_version(gpkg) if driver == 'GPKG' else {}
    with python_warnings.catch_warnings(record=True) as caught:
        python_warnings.simplefilter('always')
        if pa is not None:
            meta, table = read_arrow(path, layer=layer, **window)
            column = meta['geometry_name'] or 'wkb_geometry'
            geometries = shapely.from_wkb(np.asarray(table[column].to_numpy(zero_copy_only=False), dtype=object))
            keep, geometries, cut = select(geometries, selector, scope, geometry, entry['ref'])
            table = table.filter(pa.array(keep))
            kind = meta['geometry_type']
            if cut is not None:
                kind, geometries = declared_type(kind, geometries)
                position = table.schema.get_field_index(column)
                table = table.set_column(position, table.schema.field(column), pa.array(shapely.to_wkb(geometries), pa.binary()))
                table = table.append_column('clipped', pa.array(cut, pa.bool_()))
            source_fields = [name for name in table.column_names if name != column]
            write_arrow(table, gpkg, layer=entry['table'], driver=driver, geometry_name='geom' if driver == 'GPKG' else column,
                        geometry_type=kind, crs=meta['crs'], **options)
        else:
            import geopandas as gpd
            frame = pyogrio.read_dataframe(path, layer=layer, **window)
            keep, geometries, cut = select(frame.geometry.to_numpy(), selector, scope, geometry, entry['ref'])
            frame = frame[keep]
            if cut is not None:
                frame = frame.assign(clipped=cut).set_geometry(gpd.GeoSeries(geometries, index=frame.index, crs=frame.crs))
            source_fields = [name for name in frame.columns if name != frame.geometry.name]
            pyogrio.write_dataframe(frame.reset_index(drop=True), gpkg, layer=entry['table'], driver=driver,
                                    geometry_type=info['geometry_type'] if not len(frame) else None, **options)
            warnings.append(f'{entry["ref"]}: copied without pyarrow; integer and boolean fields with NULLs may be widened to real numbers')
    for item in caught:
        if issubclass(item.category, RuntimeWarning) and 'Integer64' in str(item.message):
            warnings.append(f'{entry["ref"]}: {item.message}'.split('. To get')[0] + ' (ArcGIS Pro before 3.2 has no 64-bit integers)')
    if not len(geometries):
        warnings.append(f'{entry["ref"]}: no features inside the map extent; an empty table is written')
    else:
        found = {g.geom_type.replace('Multi', '') for g in geometries if g is not None}
        expected = {'point': 'Point', 'line': 'LineString', 'polygon': 'Polygon'}[geometry]
        if found - {expected}:
            warnings.append(f'{entry["ref"]}: geometry {", ".join(sorted(found))} does not match the layer geometry {geometry}')
    written = pyogrio.read_info(gpkg, layer=entry['table'])
    fields = list(written['fields'])
    renames = {a: b for a, b in zip(source_fields, fields) if a != b} if len(fields) == len(source_fields) else {}
    if renames:
        warnings.append(f'{entry["ref"]}: fields renamed on writing: ' + ', '.join(f'{a} -> {b}' for a, b in renames.items()))
    return {'kind': 'vector', 'source_layer': layer, 'crs': crs, 'wkb': written['geometry_type'], 'fields': fields, 'renames': renames,
            'geometry_column': written.get('geometry_name') or 'geom', 'source_features': info.get('features'),
            'features': int(written['features']), 'clipped_features': None if cut is None else int(cut.sum()), 'warnings': warnings,
            'bounds': list(shapely.total_bounds(geometries)) if len(geometries) else None}


def pack_raster(entry, gpkg, stage, scope, bbox, tiles=True):
    """Cut a raster to the map extent's window and store it as a GeoPackage raster table when that is lossless.

    GeoPackage tiles hold 1-4 band Byte rasters (PNG) and single-band Int16, UInt16 or Float32 grids; anything else
    (and Byte rasters with a nodata value, which PNG tiles cannot keep) is written as a GeoTIFF beside the GeoPackage.
    Without tiles (gpkg None: a file geodatabase, which GDAL cannot write rasters into) every raster is a GeoTIFF.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds
    with rasterio.open(entry['path']) as source:
        if source.crs is None:
            raise ValueError(f'{entry["ref"]} has no CRS')
        if source.transform.b or source.transform.d:
            raise ValueError(f'{entry["ref"]} is a rotated raster; only north-up rasters are packed')
        full = Window(0, 0, source.width, source.height)
        if scope == 'full':
            window = full
        else:
            cells = from_bounds(*transform_bounds('EPSG:4326', source.crs, *bbox, densify_pts=21), transform=source.transform)
            col0, row0 = max(0, math.floor(cells.col_off)), max(0, math.floor(cells.row_off))
            col1 = min(source.width, math.ceil(cells.col_off + cells.width))
            row1 = min(source.height, math.ceil(cells.row_off + cells.height))
            if col1 <= col0 or row1 <= row0:
                raise ValueError(f'{entry["ref"]} does not reach the map extent')
            window = Window(col0, row0, col1 - col0, row1 - row0)
        # The read itself plus the per-band statistics, in the band's own type.
        check_raster(source, arrays=source.count+1, itemsize=np.dtype(source.dtypes[0]).itemsize, width=window.width, height=window.height)
        data = source.read(window=window, masked=False)
        transform = source.window_transform(window)
        dtype, count, nodata = source.dtypes[0], source.count, source.nodata
        stats = {}
        for band in range(1, count + 1):
            values = data[band - 1]
            values = values[values != nodata] if nodata is not None else values
            values = values[np.isfinite(values)] if values.dtype.kind == 'f' else values
            if values.size:
                stats[band] = (float(values.min()), float(values.max()))
        profile = {'width': int(window.width), 'height': int(window.height), 'count': count, 'dtype': dtype, 'crs': source.crs,
                   'transform': transform, 'nodata': nodata}
        colormap = None
        if count == 1 and dtype == 'uint8':
            try:
                colormap = source.colormap(1)
            except ValueError:
                colormap = None
        crs = source.crs
    lossless = (dtype == 'uint8' and count <= 4 and nodata is None) or (count == 1 and dtype in GPKG_RASTER_TYPES - {'uint8'})
    if lossless and tiles:
        options = {'RASTER_TABLE': entry['table'], 'APPEND_SUBDATASET': 'YES', **gpkg_version(gpkg).get('dataset_options', {})}
        if dtype == 'uint8':
            options['TILE_FORMAT'] = 'PNG'
        with rasterio.open(gpkg, 'w', driver='GPKG', **profile, **options) as target:
            target.write(data)
            if colormap:
                target.write_colormap(1, colormap)
        storage, file = 'geopackage', None
    else:
        folder = stage / 'rasters'
        folder.mkdir(exist_ok=True)
        file = f'rasters/{entry["table"]}.tif'
        with rasterio.open(stage / file, 'w', driver='GTiff', compress='deflate', tiled=True, **profile) as target:
            target.write(data)
            if colormap:
                target.write_colormap(1, colormap)
        storage = 'geotiff'
    from pyproj import CRS
    return {'kind': 'raster', 'crs': CRS.from_user_input(crs.to_wkt()), 'storage': storage, 'file': file,
            'size': [profile['width'], profile['height']], 'bands': count, 'dtype': dtype, 'stats': stats,
            'window': None if scope == 'full' else [int(window.col_off), int(window.row_off), int(window.width), int(window.height)],
            'bounds': list(rasterio.transform.array_bounds(profile['height'], profile['width'], transform)),
            'warnings': [] if lossless or not tiles else [f'{entry["ref"]}: {dtype} x {count} band{"s" if count > 1 else ""}'
                                              f'{" with nodata" if nodata is not None else ""} is not lossless as GeoPackage tiles; written to {file}']}


def placement_tables(document, gpkg, fonts, driver='GPKG', extra=None, crs=None):
    """labelPlacements as a point table (map_labels) and their leaders as a line table (map_label_leaders).

    Each label carries its panel (its own, else that of the layer it belongs to) and a style_id (its text style among the distinct ones),
    each leader a stroke_id (its leaderStroke, or the text colour at 1 px); the platform draws one rule (or label
    class) per style and stroke. extra(placement) adds a writer's own fields. The tables are in EPSG:4326, the
    positions as given, unless crs names another CRS to project them to. Field renames on writing are returned.
    """
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import LineString, Point
    from _core import reproject
    panels = {layer['id']: layer.get('panel') for layer in document['layers']}
    labels, leaders, styles, strokes = [], [], [], []

    def index_of(items, value):
        if value not in items:
            items.append(value)
        return items.index(value) + 1

    for index, item in enumerate(document.get('labelPlacements') or []):
        style = item['style']
        halo = style.get('halo')
        family, named = _map_style.resolve_font(style, fonts)
        panel = item.get('panel') or panels.get(item.get('layer'))
        labels.append({'label_id': index + 1, 'panel': panel, 'layer': item.get('layer'),
                       'feature_id': None if item.get('featureId') is None else str(item['featureId']), 'text': item['text'],
                       'style_id': index_of(styles, style), 'font': family or style.get('font'), 'font_style': named or None, 'font_name': style.get('font'),
                       'size_px': float(style['size']), **(extra(item) if extra else {}), 'color': style['color'],
                       'halo_color': halo['color'] if halo else None, 'halo_px': float(halo['width']) if halo else None,
                       'anchor': item.get('anchor', 'center'), 'rotation': float(item.get('rotation', 0)), 'has_leader': bool(item.get('leader')),
                       'geometry': Point(item['position'])})
        if item.get('leader'):
            stroke = item.get('leaderStroke') or {'color': style['color'], 'width': 1}
            leaders.append({'label_id': index + 1, 'panel': panel, 'layer': item.get('layer'), 'text': item['text'],
                            'stroke_id': index_of(strokes, stroke), 'geometry': LineString(item['leader'])})
    options = gpkg_version(gpkg) if driver == 'GPKG' else {}
    renames = {}
    for table, rows in (('map_labels', labels), ('map_label_leaders', leaders)):
        if rows:
            frame = gpd.GeoDataFrame(rows, crs='EPSG:4326')
            frame = frame.astype({c: 'int32' for c in ('label_id', 'style_id', 'stroke_id') if c in frame})  # a geodatabase has no int64 before Pro 3.2
            if crs is not None:
                frame = reproject(frame, crs, purpose='publish')
            pyogrio.write_dataframe(frame, gpkg, layer=table, driver=driver, **options)
            written = pyogrio.read_info(gpkg, layer=table)['fields']
            renames.update({a: b for a, b in zip([c for c in frame.columns if c != 'geometry'], written) if a != b})
    return {'labels': labels, 'leaders': leaders, 'styles': styles, 'strokes': strokes, 'renames': renames}


def panel_order(document):
    """Panel ids, main first: extent.panels in order, then any other panel a layer or a placed label names."""
    order = [p['id'] for p in (document.get('extent') or {}).get('panels') or []]
    for item in document['layers'] + (document.get('labelPlacements') or []):
        if item.get('panel') and item['panel'] not in order:
            order.append(item['panel'])
    return order


def panel_frame(document, panel):
    """The panel's place on the page, for the manifest: nothing uses it until there is a page layout."""
    item = next((p for p in (document.get('extent') or {}).get('panels') or [] if p['id'] == panel), {})
    return {'frame': item['frame']} if item.get('frame') else {}


def layer_tree(rows, panels):
    """Tree nodes for _qgis_style.project, top first: a group per panel (the main panel checked, the others not),
    inside it a group per map-style group; layers without a panel follow in their groups."""
    def groups(members):
        nodes, seen = [], {}
        for row in reversed(members):
            group = row['layer'].get('group')
            if group is None:
                nodes.append(('layer', row['id']))
                continue
            if group not in seen:
                seen[group] = []
                nodes.append(('group', group, True, seen[group]))
            seen[group].append(('layer', row['id']))
        return nodes
    tree = [('group', panel, index == 0, groups([r for r in rows if r['layer'].get('panel') == panel])) for index, panel in enumerate(panels)]
    return tree + groups([r for r in rows if not r['layer'].get('panel')])


def map_themes(rows, panels):
    """One map theme per panel: its layers (as the map-style shows them) and the layers without a panel."""
    themes = []
    for panel in panels:
        visible = {r['id'] for r in rows if r['layer'].get('panel') in (panel, None) and r['layer'].get('visible') is not False}
        groups = {r['layer'].get('group') for r in rows if r['layer'].get('panel') == panel} - {None}
        loose = {r['layer'].get('group') for r in rows if not r['layer'].get('panel')} - {None}
        themes.append((panel, visible, [panel] + [f'{panel}/{g}' for g in sorted(groups)] + sorted(loose)))
    return themes


def project_crs(layers, datasets):
    """The CRS most of the map's own layers use (ground layers count only when nothing else has a CRS); ties go to
    the higher layer."""
    from collections import Counter
    for keep in (lambda l: l.get('group') not in ('ground', 'groundLines'), lambda l: True):
        found = [datasets[l['_key']]['crs'] for l in layers if keep(l) and l.get('_key') in datasets and datasets[l['_key']].get('crs')]
        if found:
            counts = Counter(c.to_wkt() for c in found)
            best = max(counts.values())
            return next(c for c in reversed(found) if counts[c.to_wkt()] == best)
    return None


PLATFORMS = {'qgis': 'QGIS', 'arcgis-pro': 'ArcGIS Pro', 'arcmap': 'ArcMap'}


def prepare(args, platform):
    """What every writer shares: the checked map-style, the extent and scope, each dataset resolved once (a dataset
    drawn by several layers is one entry), tile layers set aside, and the unsupported notes found so far."""
    style_path = Path(args.map_style)
    document = _map_style.load(style_path)
    scope = 'clip' if args.clip else 'full' if args.full else 'extent'
    margin = parse_margin(args.margin)
    bbox = extent_bbox(document, margin)
    if bbox is None and scope != 'full':
        raise ValueError('The map-style has no extent.bbox, so there is no map extent to select by; pass --full to pack whole datasets')
    overrides = {}
    for item in args.data_root or []:
        name, sep, folder = item.partition('=')
        if not sep or not name or not folder:
            raise ValueError(f'--data-root expects name=/path, got {item!r}')
        overrides[name] = folder
    roots = _map_style.DataRoots(style_path, document.get('dataRoot', 'project'), args.project, overrides)
    notes = []
    for layer in document['layers']:
        where = f'layer {layer["id"]}'
        if layer.get('filter') is not None:
            _map_style.check_filter(layer['filter'], where)
        renderer = layer.get('renderer') or {}
        for rule in renderer.get('rules', []) if renderer.get('type') == 'rule' else []:
            _map_style.check_filter(rule['filter'], where)
    fonts = _map_style.font_table(document)
    for row in _map_style.fonts_used(document):
        if row.get('unresolved'):
            notes.append({'layer': ', '.join(row['used_by']), 'item': 'font',
                          'reason': f'{row["name"]!r} has no fonts[] entry with names; {platform} is given it as written'})
        if 'substituted_weight' in row:
            notes.append({'layer': ', '.join(row['used_by']), 'item': 'font.weight', 'kind': 'approximated',
                          'reason': f'{row["name"]!r} has no weight {_map_style.number(row.get("weight", 400))}: '
                                    f'the closest, {_map_style.number(row["substituted_weight"])}, is used'})
    if any(p.get('frame') for p in (document.get('extent') or {}).get('panels') or []):
        notes.append({'layer': None, 'item': 'extent.panels.frame', 'reason': 'there is no page layout in the package yet: '
                                                                               'the panels\' positions on the page are only recorded (panels[].frame)'})
    datasets, tiles, icons = {}, [], {}
    for layer in document['layers']:
        data = layer['data']
        if data.get('kind') not in TILE_KINDS:
            for symbol in _map_style.layer_symbols(layer):
                marker = symbol.get('marker')
                if not marker:
                    continue
                _map_style.check_marker(marker, f'layer {layer["id"]}')
                if marker.get('shape') == 'svg':
                    row = _map_style.load_svg(marker, roots, f'layer {layer["id"]}')
                    icon = icons.setdefault(row['ref'], {**row, 'layers': []})
                    if layer['id'] not in icon['layers']:
                        icon['layers'].append(layer['id'])
        if data.get('kind') in TILE_KINDS:  # tiles cannot be cut to the map extent or stored as a table
            tiles.append({'ref': data['path'], 'layers': [layer['id']], 'reason': f'{data["kind"]}: tiles are not packed'})
            continue
        path, root = roots.resolve(data['path'])
        dataset_key = (str(path.resolve()), data.get('layer'))
        layer['_key'] = dataset_key
        entry = datasets.get(dataset_key)
        kind = 'raster' if layer['geometry'] == 'raster' else 'vector'
        if entry is None:
            entry = datasets[dataset_key] = {'ref': data['path'], 'root': root, 'path': path, 'data_layer': data.get('layer'),
                                             'kind': kind, 'geometry': layer['geometry'], 'layers': [], 'refs': {data['path']}}
            if data.get('sha256'):
                if not path.is_file():
                    notes.append({'layer': layer['id'], 'item': 'data.sha256', 'reason': f'{data["path"]} is a folder; its sha256 is not checked'})
                elif digest(path) != data['sha256']:
                    raise ValueError(f'{data["path"]} has changed since the map-style was written (sha256 differs); export the map-style again')
        elif entry['kind'] != kind:
            raise ValueError(f'{data["path"]} is used both as a raster and as a vector layer')
        entry['refs'].add(data['path'])
        entry['layers'].append(layer['id'])
    return {'style_path': style_path, 'document': document, 'scope': scope, 'margin': margin, 'bbox': bbox, 'notes': notes,
            'fonts': fonts, 'datasets': datasets, 'tiles': tiles, 'platform': platform, 'icons': list(icons.values())}


def store(ctx, stage, target, driver, tiles=True):
    """Every dataset into target (a GeoPackage, or a file geodatabase with driver OpenFileGDB) and rasters as
    GeoPackage tables (tiles) or GeoTIFFs under stage/rasters; returns (packed, not_packed, warnings)."""
    not_packed, warnings, taken = [], [], set()
    for row in ctx['tiles']:
        known = next((r for r in not_packed if r['ref'] == row['ref']), None)
        if known:
            known['layers'] += row['layers']
        else:
            not_packed.append(row)
    for entry in ctx['datasets'].values():
        entry['table'] = table_name(entry['path'], entry['data_layer'], taken)
        try:
            if entry['kind'] == 'raster':
                result = pack_raster(entry, target, stage, ctx['scope'], ctx['bbox'], tiles=tiles)
            else:
                result = pack_vector(entry, target, ctx['scope'], ctx['bbox'], entry['geometry'], driver)
        except Exception as error:  # data GDAL cannot open (a tile folder, say) is listed, not fatal
            if type(error).__module__.split('.')[0] not in ('pyogrio', 'rasterio'):
                raise
            not_packed.append({'ref': entry['ref'], 'layers': entry['layers'], 'reason': f'cannot be read: {error}'})
            entry['failed'] = True
            continue
        entry.update(result)
        warnings += result.pop('warnings')
    packed = {k: v for k, v in ctx['datasets'].items() if not v.get('failed')}
    if not packed:
        raise ValueError('None of the map-style data could be packed: ' + '; '.join(f'{r["ref"]}: {r["reason"]}' for r in not_packed))
    return packed, not_packed, warnings


def unique_notes(notes):
    unique, out = set(), []
    for note in notes:
        mark = json.dumps(note, sort_keys=True, ensure_ascii=False)
        if mark not in unique:
            unique.add(mark)
            out.append(note)
    return out


def shared_warnings(ctx, packed, size):
    """Warnings every writer gives: a large package, fonts to install, producer notes, external data."""
    warnings = []
    if size > LARGE_BYTES:
        warnings.append(f'The package is {size / 2**20:.0f} MiB; --clip keeps less, or check what the map really needs')
    font_rows = _map_style.fonts_used(ctx['document'])
    if font_rows:
        families = sorted({' '.join(filter(None, (row['family'], row['style']))) for row in font_rows})
        warnings.append(f'The map uses the fonts {", ".join(families)}; {ctx["platform"]} substitutes another font where they are not installed')
    if ctx['document'].get('notes'):
        warnings.append(f'The map-style producer reports {len(ctx["document"]["notes"])} thing(s) it could not express exactly (see producer_notes)')
    if any(entry['root'].startswith('@') for entry in [*packed.values(), *ctx['icons']]):
        warnings.append('Some data comes from external data roots (see datasets[].root); check its licence before sending the package')
    return warnings, font_rows


def manifest_head(ctx, kind):
    document, style_path = ctx['document'], ctx['style_path']
    return {'manifest': kind, 'schema_version': 1,
            'map_style': {'file': style_path.name, 'sha256': digest(style_path), 'id': document['id'], 'title': document.get('title'),
                          'producer': document.get('producer'), 'schema_version': document['schema_version']},
            'scope': ctx['scope'], 'margin': ctx['margin'],
            'extent': {'bbox': ctx['bbox'], 'map_style_bbox': (document.get('extent') or {}).get('bbox')}}


def write_record(ctx, stage, args, packed, manifest, assumptions):
    write_json(stage / 'package.json', manifest)
    files = {p.relative_to(stage).as_posix(): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
    write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'package pack', 'version': VERSION,
                                        'parameters': {k: v for k, v in vars(args).items() if k not in ('output', 'command')},
                                        'source': {'path': str(ctx['style_path'].absolute()), 'sha256': manifest['map_style']['sha256']},
                                        'inputs': [{'ref': e['ref'], 'path': str(e['path']), 'sha256': e.get('sha256')} for e in packed.values()],
                                        'files': files,
                                        'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                        'assumptions': assumptions})


def pack(args):
    """map-style -> one platform's package: the data selection is shared, the style and container writer is not."""
    if args.lyrx and args.format != 'arcgis-pro':
        raise ValueError('--lyrx belongs to --format arcgis-pro')
    if args.lyr and args.format != 'arcmap':
        raise ValueError('--lyr belongs to --format arcmap')
    ctx = prepare(args, PLATFORMS[args.format])
    with bundle(args.output) as stage:
        return {'qgis': pack_qgis, 'arcgis-pro': pack_arcgis_pro, 'arcmap': pack_arcmap}[args.format](ctx, stage, args)


def pack_qgis(ctx, stage, args):
    """A GeoPackage with the data, QML styles in layer_styles, label placement tables and an embedded QGIS project."""
    import pyproj
    import _qgis_style as qgis
    document, notes, fonts, bbox, scope = ctx['document'], ctx['notes'], ctx['fonts'], ctx['bbox'], ctx['scope']
    name = re.sub(r'[^\w.-]+', '_', args.name or document['id']).strip('._') or 'map'
    gpkg = stage / f'{name}.gpkg'
    packed, not_packed, warnings = store(ctx, stage, gpkg, 'GPKG')
    placed = placement_tables(document, gpkg, fonts, extra=lambda item: {'size_mm': _map_style.mm(item['style']['size']),
                                                                         'quadrant': qgis.QUADRANTS[item.get('anchor', 'center')]})
    label_count, leader_count = len(placed['labels']), len(placed['leaders'])
    project_layers, style_rows, layer_rows = [], [], []
    defaults = set()
    for layer in document['layers']:
        entry = packed.get(layer.get('_key'))
        if entry is None:
            continue
        title = layer.get('title') or (layer.get('legend') or {}).get('title') or layer['id']
        row = {'id': layer['id'], 'name': title, 'layer': layer, 'kind': entry['kind'], 'crs': entry['crs']}
        manifest_row = {'id': layer['id'], 'title': title, 'panel': layer.get('panel'), 'group': layer.get('group'),
                        'table': None if entry.get('storage') == 'geotiff' else entry['table'],
                        'map_style_renderer': (layer['renderer'] or {}).get('type')}
        if entry['kind'] == 'raster':
            row.update(provider='gdal', raster={'stats': entry['stats']},
                       source=f'GPKG:./{gpkg.name}:{entry["table"]}' if entry['storage'] == 'geopackage' else f'./{entry["file"]}')
            manifest_row['qgis_renderer'] = {'pseudocolor': 'singlebandpseudocolor', 'palette': 'paletted', 'rgb': 'multibandcolor'}[layer['renderer']['type']]
            manifest_row['style'] = 'project only (QGIS keeps layer_styles for vector tables)'
        else:
            text, kind = qgis.qml(layer, notes, fonts)
            default = entry['table'] not in defaults
            defaults.add(entry['table'])
            style_rows.append({'table': entry['table'], 'geometry_column': entry['geometry_column'], 'name': layer['id'], 'qml': text,
                               'default': default, 'description': f'map-style layer {layer["id"]}: {title}'})
            row.update(provider='ogr', wkb=entry['wkb'], source=f'./{gpkg.name}|layername={entry["table"]}')
            labels = layer.get('labels')
            manifest_row.update(qgis_renderer=kind, default_style=default,
                                labels=None if not labels else 'on' if labels.get('enabled', True) else 'off',
                                filter=qgis.expression(layer.get('filter'), {}, []))
        project_layers.append(row)
        layer_rows.append(manifest_row)
    crs = project_crs(document['layers'], packed)
    placement_crs = pyproj.CRS.from_epsg(4326)
    panels = panel_order(document)
    taken_ids = {l['id'] for l in document['layers']}
    for table, rows, placement, geometry, wkb in (('map_label_leaders', placed['leaders'], 'leaders', 'line', 'LineString'),
                                                   ('map_labels', placed['labels'], 'labels', 'point', 'Point')):
        if not rows:
            continue
        title = 'labels (placed)' if placement == 'labels' else 'label leaders'
        extra = {'_styles': placed['styles']} if placement == 'labels' else {'_strokes': placed['strokes']}
        base_id = table if table not in taken_ids else f'{table}_placements'
        pseudo = {'id': base_id, 'geometry': geometry, 'group': 'labels', '_placement': placement, **extra}
        text, kind = qgis.qml(pseudo, notes, fonts)
        style_rows.append({'table': table, 'geometry_column': 'geom', 'name': base_id, 'qml': text, 'default': True,
                           'description': 'labels placed by the map-style producer' if placement == 'labels' else 'label leader lines'})
        # one QGIS layer per panel, filtered on the panel field, so an inset's labels sit with the inset
        for panel in [p for p in panels if any(r['panel'] == p for r in rows)] + ([None] if any(r['panel'] is None for r in rows) else []):
            layer_id = base_id if not panels else f'{base_id}.{panel or "none"}'
            subset = '' if not panels else '|subset=' + ('"panel" IS NULL' if panel is None else f'"panel" = {qgis.literal(panel)}')
            project_layers.append({'id': layer_id, 'name': title, 'layer': {**pseudo, 'id': layer_id, 'panel': panel},
                                   'kind': 'vector', 'crs': placement_crs, 'provider': 'ogr', 'wkb': wkb,
                                   'source': f'./{gpkg.name}|layername={table}{subset}'})
            layer_rows.append({'id': layer_id, 'title': title, 'panel': panel, 'group': 'labels', 'table': table,
                               'map_style_renderer': None, 'qgis_renderer': kind, 'default_style': True,
                               'labels': 'on' if placement == 'labels' else None,
                               'filter': None if panel is None and not panels else subset.removeprefix('|subset=') or None,
                               'features': sum(r['panel'] == panel for r in rows)})
    if bbox is not None and crs is not None:
        extent = tuple(extent_in(bbox, crs).bounds)
    else:
        extent = project_extent(packed, crs)
    bookmarks = []
    if crs is not None:
        for item in (document.get('extent') or {}).get('panels') or []:
            bookmarks.append((item['id'], tuple(extent_in(item['bbox'], crs).bounds)))
    background = document.get('background')
    if background and background.get('opacity', 1) < 1:
        notes.append({'layer': None, 'item': 'background.opacity', 'reason': 'the QGIS canvas colour is opaque'})
    qgs = qgis.project(name, document.get('title') or document['id'], project_layers, layer_tree(project_layers, panels), crs, extent,
                       notes, fonts, background=(background or {}).get('color'), themes=map_themes(project_layers, panels),
                       bookmarks=bookmarks)
    qgis.write_styles(gpkg, style_rows)
    qgis.write_project(gpkg, name, qgs)
    size = gpkg.stat().st_size + sum(f.stat().st_size for f in stage.glob('rasters/*'))
    more, font_rows = shared_warnings(ctx, packed, size)
    warnings += more
    manifest = {
        **manifest_head(ctx, 'qgis-package'),
        'geopackage': {'file': gpkg.name, 'bytes': gpkg.stat().st_size, 'project': name,
                       'tables': sorted({e['table'] for e in packed.values() if e.get('storage') != 'geotiff'}
                                        | ({'map_labels'} if label_count else set()) | ({'map_label_leaders'} if leader_count else set()))},
        'project': {'name': name, 'crs': crs_label(crs), 'extent': [float(v) for v in extent] if extent else None,
                    'open': f'QGIS: Project > Open From > GeoPackage, or open {gpkg.name} in the Browser and double-click project {name}'},
        'datasets': [dataset_row(entry, scope) for entry in packed.values()],
        'layers': layer_rows,
        'panels': [{'id': panel, 'shown': index == 0, 'bookmark': any(b[0] == panel for b in bookmarks), **panel_frame(document, panel)}
                   for index, panel in enumerate(panels)],
        'background': (background or {}).get('color'),
        'label_placements': {'labels': label_count, 'leaders': leader_count, 'table': 'map_labels' if label_count else None,
                             'leader_table': 'map_label_leaders' if leader_count else None,
                             'text_styles': len(placed['styles']), 'leader_strokes': len(placed['strokes'])},
        'fonts': font_rows,
        'icons': ctx['icons'],
        'producer_notes': document.get('notes') or [],
        'not_packed': not_packed,
        'unsupported': unique_notes(notes),
        'bytes': size,
        'warnings': warnings,
    }
    write_record(ctx, stage, args, packed, manifest, ['Sizes: 1 px = 1/96 inch = 0.2646 mm',
                                                      'Data keep their own CRS; label placements and leaders are stored in EPSG:4326',
                                                      'Layer sources in the embedded project are relative to the GeoPackage file: keep its name'])
    return manifest


def seven_zip(folder, target, members):
    """Archive members of folder as a 7z file (a .lpk or .lpkx is a 7z archive): bsdtar (libarchive) or 7-Zip."""
    tool = bsdtar()
    if tool:
        run = subprocess.run([tool, '--format', '7zip', '-cf', str(target), '-C', str(folder), *members], capture_output=True)
        if run.returncode == 0:
            return 'bsdtar'
        target.unlink(missing_ok=True)  # a libarchive built without the 7z writer: try 7-Zip
    tool = shutil.which('7z') or shutil.which('7za') or shutil.which('7zz')
    if tool is None:
        raise ValueError('Writing a layer package (a 7z archive) needs bsdtar (libarchive; tar.exe on Windows) or 7-Zip; neither could write 7z here')
    run = subprocess.run([tool, 'a', '-t7z', str(target), *members], cwd=folder, capture_output=True)
    if run.returncode:
        raise ValueError(f'{Path(tool).name} could not write {target.name}: {run.stderr.decode(errors="replace").strip()[-300:]}')
    return Path(tool).name


def relink(node, mapping):
    """The same CIM with data connections pointed elsewhere (the .lyrx beside the data instead of inside the package)."""
    if isinstance(node, dict):
        out = {k: relink(v, mapping) for k, v in node.items()}
        if node.get('type') == 'CIMStandardDataConnection':
            out['workspaceConnectionString'] = mapping.get(node['workspaceConnectionString'], node['workspaceConnectionString'])
        return out
    if isinstance(node, list):
        return [relink(v, mapping) for v in node]
    return node


def folder_digest(folder):
    """One SHA-256 over every file under folder, names included."""
    combined = hashlib.sha256()
    for path in sorted(p for p in folder.rglob('*') if p.is_file()):
        combined.update(f'{path.relative_to(folder).as_posix()} {digest(path)}\n'.encode())
    return combined.hexdigest()


def background_polygon(document, bbox, packed, crs):
    """The area the map-style background covers: the map extent and every panel's extent, in crs."""
    import shapely
    boxes = [b for b in [bbox] + [p['bbox'] for p in (document.get('extent') or {}).get('panels') or []] if b]
    if boxes and crs is not None:
        return shapely.union_all([extent_in(b, crs) for b in boxes])
    extent = project_extent(packed, crs)
    return shapely.box(*extent) if extent else None


def pack_arcgis_pro(ctx, stage, args):
    """A layer package (.lpkx) for ArcGIS Pro: the data in a file geodatabase (rasters as GeoTIFFs), one layer file
    (CIM) with a group layer for the map, and the package information; with --lyrx also the layer file beside an
    unpacked copy of the data."""
    import geopandas as gpd
    import pyogrio
    import _arcgis_style as cim
    document, notes, fonts, bbox, scope = ctx['document'], ctx['notes'], ctx['fonts'], ctx['bbox'], ctx['scope']
    name = re.sub(r'[^\w.-]+', '_', args.name or document['id']).strip('._') or 'map'
    build = stage / '_lpkx'
    common = build / 'commondata'
    common.mkdir(parents=True)
    gdb = common / f'{name}.gdb'
    packed, not_packed, warnings = store(ctx, common, gdb, 'OpenFileGDB', tiles=False)
    crs = project_crs(document['layers'], packed)
    placed = placement_tables(document, gdb, fonts, driver='OpenFileGDB', crs=crs,
                              extra=lambda item: {'size_pt': cim.pt(item['style']['size']), 'angle': -float(item.get('rotation', 0))})
    label_count, leader_count = len(placed['labels']), len(placed['leaders'])
    background = document.get('background')
    if background:
        area = background_polygon(document, bbox, packed, crs)
        if area is not None:
            pyogrio.write_dataframe(gpd.GeoDataFrame({'color': [background['color']]}, geometry=[area], crs=crs or 'EPSG:4326'),
                                    gdb, layer='map_background', driver='OpenFileGDB')
    workspace = {'gdb': f'..\\commondata\\{gdb.name}', 'rasters': '..\\commondata\\rasters'}
    uris = set()

    def uri(key):
        base = 'CIMPATH=map/' + (re.sub(r'[^\w-]+', '_', key).strip('_') or 'layer')
        candidate, n = base, 2
        while candidate in uris:
            candidate, n = f'{base}_{n}', n + 1
        uris.add(candidate)
        return candidate + '.json'

    rows, definitions, layer_rows = [], [], []
    if background and gdb.exists() and 'map_background' in {r[0] for r in pyogrio.list_layers(gdb)}:
        spec = {'id': 'map_background', 'title': 'background', 'geometry': 'polygon', 'opacity': 1,
                'renderer': {'type': 'single', 'symbol': {'fill': {'color': background['color'], 'opacity': background.get('opacity', 1)}}}}
        body, kind = cim.feature_layer('background', uri('background'), spec, cim.connection(workspace['gdb'], 'map_background'), {}, fonts, notes)
        definitions.append(body)
        rows.append({'id': 'map_background', 'layer': {'id': 'map_background'}, 'uri': body['uRI']})
        layer_rows.append({'id': 'map_background', 'title': 'background', 'panel': None, 'group': None, 'table': 'map_background',
                           'map_style_renderer': None, 'arcgis_renderer': kind, 'labels': None, 'filter': None})
    for layer in document['layers']:
        entry = packed.get(layer.get('_key'))
        if entry is None:
            continue
        title = layer.get('title') or (layer.get('legend') or {}).get('title') or layer['id']
        manifest_row = {'id': layer['id'], 'title': title, 'panel': layer.get('panel'), 'group': layer.get('group'),
                        'table': None if entry['kind'] == 'raster' else entry['table'], 'map_style_renderer': (layer['renderer'] or {}).get('type')}
        if entry['kind'] == 'raster':
            data = cim.connection(workspace['rasters'], Path(entry['file']).name, raster=True)
            body, kind = cim.raster_layer(title, uri(layer['id']), layer, data, entry['stats'], notes)
            manifest_row['file'] = f'commondata/{entry["file"]}'
        else:
            labels = layer.get('labels') or {}
            names = entry['renames']
            body, kind = cim.feature_layer(title, uri(layer['id']), layer, cim.connection(workspace['gdb'], entry['table']), names, fonts, notes,
                                           names.get(labels.get('field'), labels.get('field')))
            manifest_row.update(labels=None if not labels else 'on' if labels.get('enabled', True) else 'off',
                                filter=cim.definition_query(layer.get('filter'), {}, [], names))
        manifest_row['arcgis_renderer'] = kind
        definitions.append(body)
        rows.append({'id': layer['id'], 'layer': layer, 'uri': body['uRI']})
        layer_rows.append(manifest_row)
    panels = panel_order(document)
    taken_ids = {l['id'] for l in document['layers']}
    names = placed['renames']
    for table, items, geometry in (('map_label_leaders', placed['leaders'], 'line'), ('map_labels', placed['labels'], 'point')):
        if not items:
            continue
        title = 'labels (placed)' if table == 'map_labels' else 'label leaders'
        base_id = table if table not in taken_ids else f'{table}_placements'
        # one layer per panel, with a definition query on the panel field, so an inset's labels sit with the inset
        for panel in [p for p in panels if any(r['panel'] == p for r in items)] + ([None] if any(r['panel'] is None for r in items) else []):
            layer_id = base_id if not panels else f'{base_id}.{panel or "none"}'
            mine = [r for r in items if r['panel'] == panel] if panels else items
            field = cim.sql_field(names.get('panel', 'panel'))
            query = None if not panels else f'{field} IS NULL' if panel is None else f'{field} = {cim.sql_literal(panel)}'
            if table == 'map_labels':
                groups = sorted({(r['style_id'], r['anchor'], bool(r['angle'])) for r in mine})
                classes = cim.placement_classes([(i, placed['styles'][i - 1], a, rotated) for i, a, rotated in groups], fonts, names, notes)
                render = None
            else:
                strokes = sorted({r['stroke_id'] for r in mine})
                classes = None
                render = cim.unique_renderer([names.get('stroke_id', 'stroke_id')],
                                             [cim.unique_class(f'leader {i}', [str(i)], cim.symbol({'stroke': placed['strokes'][i - 1]}, 'line',
                                                                                                  {'layer': 'labelPlacements', 'leader': i}, notes))
                                              for i in strokes], None, 'other', title, 'line', {'layer': 'labelPlacements'}, notes)
            body = cim.placement_layer(title, uri(layer_id), cim.connection(workspace['gdb'], table), geometry, query, classes, render)
            definitions.append(body)
            rows.append({'id': layer_id, 'layer': {'id': layer_id, 'panel': panel, 'group': 'labels'}, 'uri': body['uRI']})
            layer_rows.append({'id': layer_id, 'title': title, 'panel': panel, 'group': 'labels', 'table': table, 'map_style_renderer': None,
                               'arcgis_renderer': body['renderer']['type'], 'labels': 'on' if table == 'map_labels' else None,
                               'label_classes': len(classes) if classes else None, 'filter': query, 'features': len(mine)})
    by_id = {row['id']: row for row in rows}

    def build_tree(nodes, path):
        out = []
        for node in nodes:
            if node[0] == 'group':
                _, group, checked, children = node
                body = cim.group_layer(group, uri('/'.join(path + [group])), build_tree(children, path + [group]), checked)
                definitions.append(body)
                out.append(body['uRI'])
            else:
                out.append(by_id[node[1]]['uri'])
        return out

    top = cim.group_layer(document.get('title') or document['id'], uri(name), build_tree(layer_tree(rows, panels), []))
    definitions.append(top)
    layer_document = cim.document([top['uRI']], definitions)
    (build / 'p30').mkdir()
    (build / 'p30' / f'{name}.lyrx').write_text(json.dumps(layer_document, ensure_ascii=False, indent=2), encoding='utf-8')
    # Pro unpacks a package once per ID (under Documents\\ArcGIS\\Packages) and reuses that copy, so new content needs a new ID
    item_id = cim.guid(document['id'], name, folder_digest(build))
    (build / 'esriinfo').mkdir()
    (build / 'esriinfo' / 'item.pkinfo').write_text(cim.pkinfo(item_id, name), encoding='utf-8')
    (build / 'esriinfo' / 'iteminfo.xml').write_text(
        cim.iteminfo(item_id, name, document.get('title') or document['id'], f'map-style {document["id"]}, packed by gis-kit',
                     ['map-style', 'gis-kit'], bbox, crs.name if crs is not None else None), encoding='utf-8')
    lpkx = stage / f'{name}.lpkx'
    tool = seven_zip(build, lpkx, ['commondata', 'esriinfo', 'p30'])
    if args.lyrx:  # the same layer file beside the data, opened without unpacking
        shutil.move(str(gdb), str(stage / gdb.name))
        if (common / 'rasters').exists():
            shutil.move(str(common / 'rasters'), str(stage / 'rasters'))
        direct = relink(layer_document, {f'DATABASE={workspace["gdb"]}': f'DATABASE=.\\{gdb.name}',
                                         f'DATABASE={workspace["rasters"]}': 'DATABASE=.\\rasters'})
        (stage / f'{name}.lyrx').write_text(json.dumps(direct, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.rmtree(build)
    for entry in packed.values():  # the manifest names files as they sit in the package
        if entry.get('file'):
            entry['file'] = f'commondata/{entry["file"]}'
    size = lpkx.stat().st_size
    more, font_rows = shared_warnings(ctx, packed, size)
    warnings += more
    if any(e.get('renames') for e in packed.values()) or names:
        warnings.append('Some fields were renamed in the file geodatabase (see datasets[].renamed_fields); the layer file uses the new names')
    feature_classes = sorted({e['table'] for e in packed.values() if e['kind'] == 'vector'}
                             | ({'map_labels'} if label_count else set()) | ({'map_label_leaders'} if leader_count else set())
                             | ({'map_background'} if any(r['id'] == 'map_background' for r in rows) else set()))
    manifest = {
        **manifest_head(ctx, 'arcgis-pro-package'),
        'layer_package': {'file': lpkx.name, 'bytes': size, 'archive': '7z', 'tool': tool, 'layer_file': f'p30/{name}.lyrx',
                          'cim_version': cim.CIM_VERSION, 'geodatabase': f'commondata/{gdb.name}',
                          'rasters': sorted(e['file'] for e in packed.values() if e.get('file'))},
        'layer_file': {'file': f'{name}.lyrx', 'geodatabase': gdb.name, 'rasters': 'rasters' if (stage / 'rasters').exists() else None} if args.lyrx else None,
        'geodatabase': {'name': gdb.name, 'feature_classes': feature_classes},
        'map': {'name': name, 'title': document.get('title') or document['id'], 'crs': crs_label(crs),
                'open': f'ArcGIS Pro: drag {lpkx.name} onto a map (or Add Data); it unpacks and adds the group layer'
                        + (f'. Or Add Data > {name}.lyrx, kept beside {gdb.name}' if args.lyrx else '')},
        'datasets': [dataset_row(entry, scope) for entry in packed.values()],
        'layers': layer_rows,
        'panels': [{'id': panel, 'shown': index == 0, **panel_frame(document, panel)} for index, panel in enumerate(panels)],
        'background': {'color': background['color'], 'layer': 'map_background'} if background else None,
        'label_placements': {'labels': label_count, 'leaders': leader_count, 'table': 'map_labels' if label_count else None,
                             'leader_table': 'map_label_leaders' if leader_count else None,
                             'text_styles': len(placed['styles']), 'leader_strokes': len(placed['strokes'])},
        'fonts': font_rows,
        'icons': ctx['icons'],
        'producer_notes': document.get('notes') or [],
        'not_packed': not_packed,
        'unsupported': unique_notes(notes),
        'bytes': size,
        'warnings': warnings,
    }
    write_record(ctx, stage, args, packed, manifest, ['Sizes: 1 px = 0.75 pt',
                                                      'Data keep their own CRS; label placements, leaders and the background are '
                                                      'stored in the CRS most map layers use',
                                                      'Layer sources are relative to the layer file (p30/ inside the package, the '
                                                      '.lyrx beside its geodatabase)'])
    return manifest


def pack_arcmap(ctx, stage, args):
    """A layer package (.lpk) for ArcMap, written by _arcmap_style with the helpers here."""
    import sys
    import _arcmap_style
    return _arcmap_style.write_package(ctx, stage, args, sys.modules[__name__])


def crs_label(crs):
    if crs is None:
        return None
    authority = crs.to_authority(min_confidence=90)
    return ':'.join(authority) if authority else crs.name


def project_extent(packed, crs):
    """Without a map extent: the union of the packed data's bounds, in the project CRS."""
    from pyproj import Transformer
    boxes = []
    for entry in packed.values():
        if not entry.get('bounds') or entry.get('crs') is None:
            continue
        xmin, ymin, xmax, ymax = entry['bounds']
        if crs is not None and not entry['crs'].equals(crs):
            xmin, ymin, xmax, ymax = Transformer.from_crs(entry['crs'], crs, always_xy=True).transform_bounds(xmin, ymin, xmax, ymax, densify_pts=21)
        boxes.append((xmin, ymin, xmax, ymax))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def dataset_row(entry, scope):
    path = entry['path']
    row = {'ref': entry['ref'], 'root': entry['root'], 'table': None if entry.get('storage') == 'geotiff' else entry['table'], 'kind': entry['kind'],
           'layers': entry['layers'], 'crs': crs_label(entry['crs']),
           'scope': ('window' if scope != 'full' else 'full') if entry['kind'] == 'raster' else scope,
           'source_bytes': path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob('*') if p.is_file())}
    if len(entry['refs']) > 1:
        row['refs'] = sorted(entry['refs'])
    if entry.get('renames'):
        row['renamed_fields'] = entry['renames']
    if entry['data_layer'] or entry.get('source_layer'):
        row['source_layer'] = entry['data_layer'] or entry.get('source_layer')
    if path.is_file():
        entry['sha256'] = row['sha256'] = digest(path)
    if entry['kind'] == 'vector':
        row.update(source_features=entry['source_features'], features=entry['features'])
        if entry['clipped_features'] is not None:
            row['clipped_features'] = entry['clipped_features']
    else:
        row.update(storage=entry['storage'], size=entry['size'], bands=entry['bands'], dtype=entry['dtype'])
        if entry['window']:
            row['window'] = entry['window']
        if entry['file']:
            row['file'] = entry['file']
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('unpack', help='Unpack a .lpk / .lpkx / .mpk / .mpkx and inventory it')
    sub.add_argument('source', help='ArcGIS package file')
    sub.add_argument('--output', required=True, help='New directory for package/, package.json and record.json')
    sub.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES, help='Refuse packages that expand beyond this (default 20 GiB)')
    sub = commands.add_parser('pack', help='Pack a map-style map and its data for QGIS (styled GeoPackage), ArcGIS Pro (.lpkx) or ArcMap (.lpk)')
    sub.add_argument('map_style', help='map-style v1 JSON file')
    sub.add_argument('--output', required=True, help='New directory for the package, package.json and record.json')
    scope = sub.add_mutually_exclusive_group()
    scope.add_argument('--clip', action='store_true', help='Cut features at the map extent and mark them in a "clipped" field')
    scope.add_argument('--full', action='store_true', help='Keep whole datasets (default: whole features that touch the map extent)')
    sub.add_argument('--margin', help='Widen the map extent by this share of its width and height before selecting, e.g. 10%% or 0.1')
    sub.add_argument('--project', help='Project root for dataRoot "project" (default: the folder with gis-project.json above the map-style)')
    sub.add_argument('--data-root', action='append', metavar='NAME=PATH', help='Path of an external data root @NAME on this device (repeatable)')
    sub.add_argument('--name', help='Package name: the GeoPackage and project, or the layer package, layer file and geodatabase (default: the map-style id)')
    sub.add_argument('--format', choices=sorted(PLATFORMS), default='qgis',
                     help='qgis: a GeoPackage with styles and a project (default); arcgis-pro: a layer package (.lpkx); arcmap: a layer package (.lpk)')
    sub.add_argument('--lyrx', action='store_true', help='arcgis-pro: also write the .lyrx beside an unpacked file geodatabase')
    sub.add_argument('--lyr', action='store_true', help='arcmap: also write the .lyr beside an unpacked file geodatabase')
    _style_read.add_command(commands)
    args = parser.parse_args()
    try:
        if args.command == 'read':
            print(json.dumps(_style_read.run(args), ensure_ascii=False))
            return
        if args.command == 'pack':
            manifest = pack(args)
            package = {'geopackage': manifest['geopackage']['file'], 'project': manifest['project']['name']} if args.format == 'qgis' else \
                {'layer_package': manifest['layer_package']['file'], 'layer_file': (manifest['layer_file'] or {}).get('file')}
            print(json.dumps({'output': str(Path(args.output).absolute()), **package, 'scope': manifest['scope'],
                              'datasets': [{'ref': d['ref'], 'table': d['table'], 'features': d.get('features')} for d in manifest['datasets']],
                              'unsupported': len(manifest['unsupported']), 'not_packed': [d['ref'] for d in manifest['not_packed']],
                              'warnings': manifest['warnings']}, ensure_ascii=False))
            return
        manifest = unpack(args)
        print(json.dumps({'output': str(Path(args.output).absolute()), 'package': (manifest['package'] or {}).get('name'),
                          'datasets': [d['path'] for d in manifest['datasets']],
                          'layers': [{'name': l['name'], 'data': l['data']} for l in manifest['layers']],
                          'warnings': manifest['warnings']}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
