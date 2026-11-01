"""Reading a map-style v1 document (gis-plugin contracts/map-style.schema.json), platform-neutral.

What every platform writer shares: the checks a reader must make before trusting the document, resolving each
`data.path` (dataRoot, `@root/...` external data roots), the structured filter, and the unit conversions. The
platform writers (`_qgis_style.py`, `_arcgis_style.py`) turn the layers into their own formats.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re

CONTRACT, SCHEMA_VERSION = 'map-style', 1
MM_PER_PX = 25.4 / 96  # CSS pixel, 1/96 inch: 0.2646 mm
PROJECT_FILE = 'gis-project.json'
ROOT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
ROOT_REFERENCE = re.compile(r'^@([A-Za-z0-9][A-Za-z0-9_-]*)/(.*)$')
GEOMETRIES = {'polygon', 'line', 'point', 'raster'}
DATA_KINDS = {'vector', 'raster', 'vectorTiles', 'rasterTiles'}
VECTOR_RENDERERS = {'single', 'categorized', 'graduated', 'rule'}
RASTER_RENDERERS = {'pseudocolor', 'palette', 'rgb'}
COMPARISONS = {'==', '!=', '<', '<=', '>', '>='}
FILTER_OPS = COMPARISONS | {'in', 'notIn', 'isNull', 'notNull'}


def mm(px):
    return float(px) * MM_PER_PX


def number(value):
    value = float(value)
    return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)


def load(path):
    """The document, after the checks the packer relies on; a reader rejects versions it does not know."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Cannot read map-style {path.name}: {error}') from error
    if not isinstance(document, dict) or document.get('contract') != CONTRACT:
        raise ValueError(f'{path.name} is not a map-style document (contract must be "{CONTRACT}")')
    if document.get('schema_version') != SCHEMA_VERSION:
        raise ValueError(f'{path.name}: map-style schema_version {document.get("schema_version")!r} is not supported (reads {SCHEMA_VERSION})')
    if (document.get('units') or {}).get('size') != 'px':
        raise ValueError(f'{path.name}: units.size must be "px"')
    if document.get('dataRoot', 'project') not in ('project', 'file'):
        raise ValueError(f'{path.name}: dataRoot must be "project" or "file"')
    layers = document.get('layers')
    if not isinstance(layers, list) or not layers:
        raise ValueError(f'{path.name}: layers must be a non-empty list')
    seen = set()
    panels = {p.get('id') for p in (document.get('extent') or {}).get('panels') or [] if isinstance(p, dict)}
    for index, layer in enumerate(layers):
        where = f'{path.name}: layers[{index}]'
        if not isinstance(layer, dict) or not layer.get('id'):
            raise ValueError(f'{where} needs an id')
        if layer['id'] in seen:
            raise ValueError(f'{where}: duplicate layer id {layer["id"]!r}')
        seen.add(layer['id'])
        if layer.get('geometry') not in GEOMETRIES:
            raise ValueError(f'{where} ({layer["id"]}): geometry must be one of {", ".join(sorted(GEOMETRIES))}')
        if not isinstance(layer.get('data'), dict) or not layer['data'].get('path'):
            raise ValueError(f'{where} ({layer["id"]}): data.path is required')
        check_reference(layer['data']['path'], f'{where} ({layer["id"]})')
        if layer['data'].get('kind') is not None and layer['data']['kind'] not in DATA_KINDS:
            raise ValueError(f'{where} ({layer["id"]}): data.kind must be one of {", ".join(sorted(DATA_KINDS))}')
        if layer.get('panel') is not None and panels and layer['panel'] not in panels:
            raise ValueError(f'{where} ({layer["id"]}): panel {layer["panel"]!r} is not one of extent.panels ({", ".join(sorted(panels))})')
        if 'renderer' not in layer:
            raise ValueError(f'{where} ({layer["id"]}): renderer is required (null for a label-only layer)')
        renderer = layer['renderer']
        allowed = RASTER_RENDERERS if layer['geometry'] == 'raster' else VECTOR_RENDERERS
        if renderer is None and layer['geometry'] == 'raster':
            raise ValueError(f'{where} ({layer["id"]}): a raster layer needs a renderer')
        if renderer is not None and (not isinstance(renderer, dict) or renderer.get('type') not in allowed):
            raise ValueError(f'{where} ({layer["id"]}): renderer.type must be one of {", ".join(sorted(allowed))}')
    bbox = (document.get('extent') or {}).get('bbox')
    if bbox is not None:
        if (not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox)
                or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90)):
            raise ValueError(f'{path.name}: extent.bbox must be [west, south, east, north] in degrees with west < east, south < north')
    return document


def check_reference(raw, where):
    posix = PurePosixPath(raw.replace('\\', '/'))
    if raw.startswith(('/', '~')) or re.match(r'^[A-Za-z]:[\\/]', raw) or '..' in posix.parts:
        raise ValueError(f'{where}: data.path {raw!r} must be relative, without "..": map-style never names absolute paths')


def find_project(start):
    """The nearest folder at or above start that holds gis-project.json, or None."""
    for folder in [start, *start.parents]:
        if (folder / PROJECT_FILE).is_file():
            return folder
    return None


def device_roots_file(env=os.environ):
    """The per-device data-root config shared with gis-composer: $GIS_COMPOSER_CONFIG_DIR/roots.json, else under
    $XDG_CONFIG_HOME/gis-composer, else ~/.config/gis-composer."""
    if env.get('GIS_COMPOSER_CONFIG_DIR'):
        folder = Path(env['GIS_COMPOSER_CONFIG_DIR'])
    else:
        folder = (Path(env['XDG_CONFIG_HOME']) if env.get('XDG_CONFIG_HOME') else Path.home() / '.config') / 'gis-composer'
    return folder / 'roots.json'


class DataRoots:
    """Resolves data.path the way gis-composer does for the same project.

    A plain path is relative to dataRoot: the project root (the folder with gis-project.json, found upwards from the
    map-style file or given with --project) or the folder holding the map-style file. `@name/rest` resolves inside
    the data root `name`: --data-root name=/path first, then the device's roots.json, then GEO_ASSET_ROOT for
    `geo-assets`. When the project is known, the name must be declared in its gis-project.json "external".
    Nothing is guessed: a root that cannot be found is an error naming where it was looked up.
    """

    def __init__(self, style_path, data_root='project', project=None, overrides=None, env=os.environ):
        self.style_path = Path(style_path).absolute()
        self.mode = data_root
        self.overrides = dict(overrides or {})
        self.env = env
        if project is not None:
            self.project = Path(project).absolute()
            if not (self.project / PROJECT_FILE).is_file():
                raise ValueError(f'--project {project} has no {PROJECT_FILE}')
        else:
            self.project = find_project(self.style_path.parent)
        self.declared = None
        if self.project is not None:
            try:
                manifest = json.loads((self.project / PROJECT_FILE).read_text(encoding='utf-8'))
            except (OSError, ValueError) as error:
                raise ValueError(f'Cannot read {PROJECT_FILE}: {error}') from error
            self.declared = sorted((manifest.get('external') or {}).keys())
        for name in self.overrides:
            if not ROOT_NAME.match(name):
                raise ValueError(f'--data-root name {name!r} is not a valid root name')
            if self.declared is not None and name not in self.declared:
                raise ValueError(f'--data-root {name} is not declared in {PROJECT_FILE} "external" (declared: {", ".join(self.declared) or "none"})')
        self._device = None

    def base(self):
        if self.mode == 'file':
            return self.style_path.parent
        if self.project is None:
            raise ValueError(f'dataRoot is "project" but no {PROJECT_FILE} was found above {self.style_path.name}; pass --project <dir>')
        return self.project

    def device(self):
        if self._device is None:
            file = device_roots_file(self.env)
            try:
                self._device = (file, json.loads(file.read_text(encoding='utf-8')).get('roots') or {})
            except FileNotFoundError:
                self._device = (file, {})
            except (OSError, ValueError, AttributeError) as error:
                raise ValueError(f'Cannot read the data-root config {file}: {error}') from error
        return self._device

    def root(self, name):
        if self.declared is not None and name not in self.declared:
            raise ValueError(f'Data root @{name} is not declared in {PROJECT_FILE} "external" (declared: {", ".join(self.declared) or "none"})')
        file, device = self.device()
        configured = self.overrides.get(name) or device.get(name) or (self.env.get('GEO_ASSET_ROOT') if name == 'geo-assets' else None)
        if not configured:
            raise ValueError(f'Data root @{name} has no path on this device: pass --data-root {name}=/path or add it to {file}')
        folder = Path(os.path.expanduser(configured)).absolute()
        if not folder.is_dir():
            raise ValueError(f'Data root @{name} points at {folder}, which is not a folder')
        return folder

    def resolve(self, raw):
        """(absolute path, root label): root label is 'project', 'file' or '@name'."""
        match = ROOT_REFERENCE.match(raw)
        if match:
            name, rest = match.groups()
            folder = self.root(name)
            target = (folder / rest).absolute()
            if not target.resolve().is_relative_to(folder.resolve()):
                raise ValueError(f'{raw} escapes data root @{name}')
            label = '@' + name
        else:
            folder = self.base()
            target = (folder / raw).absolute()
            label = self.mode
        if not target.exists():
            raise ValueError(f'Data {raw} not found (looked for {target})')
        return target, label


def walk_filter(spec):
    """Every leaf of a structured filter, for collecting the fields it names."""
    if not isinstance(spec, dict):
        return
    for key in ('all', 'any'):
        if key in spec:
            for item in spec[key]:
                yield from walk_filter(item)
            return
    if 'not' in spec:
        yield from walk_filter(spec['not'])
        return
    yield spec


def check_filter(spec, where):
    """Structured filters must have a known op and a value of the right shape; the dialect form is passed through."""
    if not isinstance(spec, dict):
        raise ValueError(f'{where}: filter must be an object')
    if 'expression' in spec:
        if not spec.get('dialect') or not isinstance(spec['expression'], str):
            raise ValueError(f'{where}: an expression filter needs "expression" and "dialect"')
        return
    for key in ('all', 'any'):
        if key in spec:
            if not isinstance(spec[key], list) or not spec[key]:
                raise ValueError(f'{where}: "{key}" needs a non-empty list')
            for item in spec[key]:
                check_filter(item, where)
            return
    if 'not' in spec:
        check_filter(spec['not'], where)
        return
    op = spec.get('op')
    if not spec.get('field') or op not in FILTER_OPS:
        raise ValueError(f'{where}: filter needs a field and an op in {", ".join(sorted(FILTER_OPS))}')
    if op in ('in', 'notIn') and not isinstance(spec.get('value'), list):
        raise ValueError(f'{where}: "{op}" needs a list value')
    if op in COMPARISONS and not isinstance(spec.get('value'), (str, int, float, bool)):
        raise ValueError(f'{where}: "{op}" needs a string, number or boolean value')


def font_names(document):
    """{name used in a text style: (family, style)} from fonts[]: a theme alias resolves through its entry's `names`
    (the installed family and its subfamily). An alias with several entries (weights) takes the first; one
    without `names` stays as written and is reported by fonts_used."""
    resolved = {}
    for entry in document.get('fonts') or []:
        if not isinstance(entry, dict) or not entry.get('family') or entry['family'] in resolved:
            continue
        names = entry.get('names') or {}
        if names.get('family'):
            resolved[entry['family']] = (names['family'], names.get('subfamily') or '')
    return resolved


def fonts_used(document):
    """Fonts named by layer labels, label placements and fonts[]: who uses each, and the installed name QGIS is given."""
    used = {}
    for layer in document['layers']:
        font = ((layer.get('labels') or {}).get('text') or {}).get('font')
        if font:
            used.setdefault(font, set()).add(f'layer {layer["id"]}')
    for placement in document.get('labelPlacements') or []:
        font = (placement.get('style') or {}).get('font')
        if font:
            used.setdefault(font, set()).add('labelPlacements')
    entries = {}
    for entry in document.get('fonts') or []:
        if isinstance(entry, dict) and entry.get('family'):
            entries.setdefault(entry['family'], []).append(entry)
    resolved = font_names(document)
    rows = []
    for alias in sorted(set(used) | set(entries)):
        entry = (entries.get(alias) or [{}])[0]
        names = entry.get('names') or {}
        row = {'name': alias, 'used_by': sorted(used.get(alias, []))}
        if alias in resolved:
            row['family'], row['style'] = resolved[alias]
            localized = [n['family'] for n in names.get('localized') or [] if n.get('family')]
            if localized:
                row['localized_family'] = localized
            if names.get('postScriptName'):
                row['postScriptName'] = names['postScriptName']
        else:
            row['family'], row['style'] = alias, ''
            row['unresolved'] = True
        if len(entries.get(alias, [])) > 1:
            row['entries'] = len(entries[alias])
        for key in ('file', 'sha256'):
            if entry.get(key):
                row[key] = entry[key]
        rows.append(row)
    return rows


def classes_as_rules(spec):
    """Categorized, graduated and single renderers as first-match rules."""
    if spec['type'] == 'single':
        return [{'filter': None, 'default': True, 'label': 'all', 'symbol': spec['symbol']}]
    rules = []
    if spec['type'] == 'categorized':
        for item in spec['classes']:
            rules.append({'filter': {'field': spec['field'], 'op': '==', 'value': item['value']}, 'label': item.get('label', str(item['value'])),
                          'symbol': item['symbol'], 'visible': item.get('visible', True)})
    elif spec['type'] == 'graduated':
        classes = spec['classes']
        for index, item in enumerate(classes):
            low, high = item['range']
            upper = {'field': spec['field'], 'op': '<=' if index == len(classes) - 1 else '<', 'value': high}
            rules.append({'filter': {'all': [{'field': spec['field'], 'op': '>=', 'value': low}, upper]},
                          'label': item.get('label', f'{number(low)} - {number(high)}'), 'symbol': item['symbol'],
                          'visible': item.get('visible', True)})
    else:
        rules = [dict(rule) for rule in spec['rules']]
    rules.append({'filter': None, 'default': True, 'label': 'other', 'symbol': spec.get('default')})
    return rules
