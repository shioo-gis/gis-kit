"""map-style v1 -> QGIS: layer styles (QML), filter expressions, and a QGIS project stored inside a GeoPackage.

One writer per platform: this module knows QGIS and nothing about where the data came from; package.py pack
hands it the layers with their GeoPackage tables. Everything that cannot be expressed is reported through
`notes` ({layer, item, reason}) instead of being dropped silently.

QGIS conventions used here: sizes in millimetres (map-style px x 0.2646), colours "r,g,b,a", layer styles in the
GeoPackage `layer_styles` table (the table's default style is what QGIS applies when the table is added), and
projects in the `qgis_projects` table (the zipped .qgs, hex encoded), with data sources relative to the GeoPackage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import math
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
import zipfile

from _map_style import classes_as_rules, mm, number

QGIS_VERSION = '3.34.0-Prizren'  # the oldest QGIS whose QML/project format this writer follows (LTR)
DOCTYPE = "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>"
NAMESPACE = uuid.UUID('6f1b8d1c-4b0e-4a59-9a43-7d2b6c0e2f10')
CAPS = {'butt': 'flat', 'round': 'round', 'square': 'square'}
JOINS = {'miter': 'miter', 'round': 'round', 'bevel': 'bevel'}
# map-style marker shape -> (QGIS SimpleMarker name, extra clockwise rotation, drawn with lines only). QGIS's
# arrowheads point right, map-style's arrow and chevron point up; cross and x are filled shapes when the symbol
# has a fill and lines (the stroke) when it has none.
MARKERS = {'circle': ('circle', 0, False), 'square': ('square', 0, False), 'triangle': ('triangle', 0, False),
           'diamond': ('diamond', 0, False), 'ring': ('circle', 0, False), 'arrow': ('filled_arrowhead', -90, False),
           'chevron': ('arrowhead', -90, True), 'cross': ('cross', 0, True), 'x': ('cross2', 0, True)}
FILLED_CROSSES = {'cross': ('cross_fill', 0), 'x': ('cross_fill', 45)}
SYMBOL_TYPES = {'polygon': 'fill', 'line': 'line', 'point': 'marker'}
GEOMETRY_TYPES = {'point': ('Point', 0), 'line': ('Line', 1), 'polygon': ('Polygon', 2)}
# QGIS label quadrants: where the text sits relative to the point. map-style anchors name the side of the text at the point.
QUADRANTS = {'bottom-right': 0, 'bottom': 1, 'bottom-left': 2, 'right': 3, 'center': 4, 'left': 5,
             'top-right': 6, 'top': 7, 'top-left': 8}
DEFAULT_MARKER_PX = 6


def key(*parts):
    """A stable {uuid} so the same map-style gives the same QML."""
    return '{' + str(uuid.uuid5(NAMESPACE, '/'.join(map(str, parts)))) + '}'


def layer_id(name):
    """A QGIS layer id like the ones QGIS makes (name, underscore, uuid), stable for the same map-style layer id."""
    return re.sub(r'\W', '_', name) + '_' + str(uuid.uuid5(NAMESPACE, name)).replace('-', '_')


def color(value, opacity=1.0):
    value = value.lstrip('#')
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f'{r},{g},{b},{round(255 * (1.0 if opacity is None else opacity))}'


def sub(parent, tag, attrs=None, text=None):
    node = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrs or {}).items()})
    if text is not None:
        node.text = str(text)
    return node


def serialize(root):
    """The XML document text; attribute and text values may be numbers while the tree is built."""
    for node in root.iter():
        node.attrib = {k: str(v) for k, v in node.attrib.items()}
        if node.text is not None:
            node.text = str(node.text)
    return DOCTYPE + '\n' + ET.tostring(root, encoding='unicode')


def options(parent, values):
    node = sub(parent, 'Option', {'type': 'Map'})
    for name, value in values.items():
        sub(node, 'Option', {'name': name, 'type': 'QString', 'value': value})
    return node


def properties(parent, tag, props=None):
    """A data-defined property collection: {name: ('field'|'expression', text)}."""
    holder = sub(parent, tag)
    root = sub(holder, 'Option', {'type': 'Map'})
    sub(root, 'Option', {'name': 'name', 'type': 'QString', 'value': ''})
    if props:
        node = sub(root, 'Option', {'name': 'properties', 'type': 'Map'})
        for name, (kind, text) in sorted(props.items()):
            prop = sub(node, 'Option', {'name': name, 'type': 'Map'})
            sub(prop, 'Option', {'name': 'active', 'type': 'bool', 'value': 'true'})
            sub(prop, 'Option', {'name': kind, 'type': 'QString', 'value': text})
            sub(prop, 'Option', {'name': 'type', 'type': 'int', 'value': 2 if kind == 'field' else 3})
    else:
        sub(root, 'Option', {'name': 'properties'})
    sub(root, 'Option', {'name': 'type', 'type': 'QString', 'value': 'collection'})
    return holder


# --- expressions ---------------------------------------------------------------------------------------------------

def field(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return number(value)
    return "'" + str(value).replace("'", "''") + "'"


OPS = {'==': '=', '!=': '<>', '<': '<', '<=': '<=', '>': '>', '>=': '>='}


class Unusable(ValueError):
    """A filter part in a dialect QGIS cannot read."""


def structured(spec):
    """A map-style filter as a QGIS expression.

    NULL follows the map-style reading of the filter (as MapLibre does): a missing value never equals anything, so
    `!=` and `notIn` are true for NULL, and comparisons with NULL are false rather than unknown.
    """
    if 'expression' in spec:
        if str(spec.get('dialect')).lower() != 'qgis':
            raise Unusable(f'dialect {spec.get("dialect")!r} is not QGIS ({spec["expression"]})')
        return f'({spec["expression"]})'
    for joiner in ('all', 'any'):
        if joiner in spec:
            parts = [structured(item) for item in spec[joiner]]
            return '(' + (' AND ' if joiner == 'all' else ' OR ').join(parts) + ')'
    if 'not' in spec:
        return f'(NOT coalesce({structured(spec["not"])}, FALSE))'
    name, op, value = field(spec['field']), spec['op'], spec.get('value')
    if op == 'isNull':
        return f'({name} IS NULL)'
    if op == 'notNull':
        return f'({name} IS NOT NULL)'
    if op in ('in', 'notIn'):
        values = ', '.join(literal(v) for v in value)
        if op == 'in':
            return f'({name} IN ({values}))' if values else 'FALSE'
        return f'({name} IS NULL OR {name} NOT IN ({values}))' if values else 'TRUE'
    if op == '!=':
        return f'({name} IS NULL OR {name} <> {literal(value)})'
    return f'({name} IS NOT NULL AND {name} {OPS[op]} {literal(value)})'


def expression(spec, where, notes, rule=False):
    """A QGIS expression for a map-style filter, or None when it cannot be used (reported).

    `dialect: "qgis"` is used as written; any other dialect makes the whole filter unusable. As the contract says,
    a layer filter then filters nothing, and a rule's filter skips the rule (it matches nothing).
    """
    if spec is None:
        return None
    try:
        text = structured(spec)
    except Unusable as error:
        effect = 'the rule is skipped (it matches nothing)' if rule else 'treated as no filter'
        notes.append({**where, 'item': 'filter', 'reason': f'{error}: {effect}'})
        return None
    return text[1:-1] if 'expression' in spec else text


# --- symbols -------------------------------------------------------------------------------------------------------

def symbol_layer(parent, cls, values, name, dd=None):
    node = sub(parent, 'layer', {'class': cls, 'enabled': 1, 'locked': 0, 'pass': 0, 'id': key(name, cls)})
    options(node, values)
    properties(node, 'data_defined_properties', dd)
    return node


def line_options(stroke):
    values = {'line_color': color(stroke['color'], stroke.get('opacity', 1)), 'line_width': number(mm(stroke['width'])),
              'line_width_unit': 'MM', 'line_style': 'solid', 'capstyle': CAPS[stroke.get('cap', 'butt')],
              'joinstyle': JOINS[stroke.get('join', 'miter')], 'offset': '0', 'offset_unit': 'MM', 'use_custom_dash': '0',
              'customdash': '5;2', 'customdash_unit': 'MM'}
    if stroke.get('dash'):
        dash = list(stroke['dash'])
        if len(dash) % 2:
            dash = dash * 2  # an odd pattern repeats, as in SVG and canvas
        values.update(use_custom_dash='1', customdash=';'.join(number(mm(v)) for v in dash))
    return values


def symbol(parent, spec, geometry, name, where, notes, size=None):
    """<symbol> for one map-style Symbol on a layer of this geometry; size is a data-defined marker size expression."""
    kind = SYMBOL_TYPES[geometry]
    node = sub(parent, 'symbol', {'type': kind, 'name': name, 'alpha': 1, 'clip_to_extent': 1, 'force_rhr': 0})
    properties(node, 'data_defined_properties')
    fill, stroke, marker = spec.get('fill'), spec.get('stroke'), spec.get('marker')
    if geometry == 'polygon':
        if marker:
            notes.append({**where, 'item': 'symbol.marker', 'reason': 'a marker on a polygon layer is not drawn'})
        if fill:
            symbol_layer(node, 'SimpleFill', {'color': color(fill['color'], fill.get('opacity', 1)), 'style': 'solid',
                                              'outline_style': 'no', 'outline_width': '0', 'outline_width_unit': 'MM',
                                              'outline_color': '0,0,0,0', 'joinstyle': 'miter', 'offset': '0,0', 'offset_unit': 'MM'},
                         f'{name}/fill')
        if stroke:  # an outline line layer keeps dashes, caps and joins that a fill's own border cannot
            symbol_layer(node, 'SimpleLine', line_options(stroke), f'{name}/stroke')
    elif geometry == 'line':
        for part in ('fill', 'marker'):
            if spec.get(part):
                notes.append({**where, 'item': f'symbol.{part}', 'reason': f'{part} on a line layer is not drawn'})
        if stroke:
            symbol_layer(node, 'SimpleLine', line_options(stroke), f'{name}/stroke')
    else:
        if not marker:
            notes.append({**where, 'item': 'symbol.marker', 'reason': f'point symbol without a marker: drawn as a {DEFAULT_MARKER_PX} px circle'})
            marker = {'shape': 'circle', 'size': DEFAULT_MARKER_PX}
        shape = marker['shape']
        qgis_name, turn, lines_only = MARKERS[shape]
        if shape in FILLED_CROSSES and fill:
            (qgis_name, turn), lines_only = FILLED_CROSSES[shape], False
        ring = shape == 'ring'
        outline = stroke
        if outline is None and (ring or lines_only) and fill:  # a hollow or line-only shape takes the fill colour for its lines
            width = 1 if ring else max(1.0, float(marker['size']) * (0.19 if shape == 'chevron' else 0.12))
            outline = {'color': fill['color'], 'width': width, 'opacity': fill.get('opacity', 1)}
        values = {'name': qgis_name, 'size': number(mm(marker['size'])), 'size_unit': 'MM', 'scale_method': 'diameter',
                  'color': '0,0,0,0' if ring or lines_only or not fill else color(fill['color'], fill.get('opacity', 1)),
                  'outline_style': 'solid' if outline else 'no',
                  'outline_color': color(outline['color'], outline.get('opacity', 1)) if outline else '0,0,0,0',
                  'outline_width': number(mm(outline['width'])) if outline else '0', 'outline_width_unit': 'MM',
                  'joinstyle': JOINS[(outline or {}).get('join', 'miter')], 'cap_style': CAPS[(outline or {}).get('cap', 'butt')],
                  'angle': number((float(marker.get('rotation', 0)) + turn) % 360), 'offset': '0,0', 'offset_unit': 'MM',
                  'horizontal_anchor_point': '1', 'vertical_anchor_point': '1'}
        if outline and outline.get('dash'):
            notes.append({**where, 'item': 'symbol.stroke.dash', 'reason': 'dashed marker outlines are drawn solid'})
        symbol_layer(node, 'SimpleMarker', values, f'{name}/marker', {'size': ('expression', size)} if size else None)
    if not len(node.findall('layer')):
        notes.append({**where, 'item': 'symbol', 'reason': f'nothing in this symbol draws on a {geometry} layer'})
    return node


def size_expression(spec):
    """Marker diameter in mm from map-style `size` (field value in domain -> radius in px, linear or sqrt)."""
    (d0, d1), (r0, r1) = spec['domain'], spec['range']
    value = field(spec['field'])
    if spec.get('scale') == 'sqrt':
        if d0 < 0 or d1 < 0:
            raise ValueError(f'size.domain {spec["domain"]} must not be negative for a sqrt scale')
        t = f'(sqrt({value}) - {number(math.sqrt(d0))}) / {number(math.sqrt(d1) - math.sqrt(d0))}'
    else:
        t = f'({value} - {number(d0)}) / {number(d1 - d0)}'
    if spec.get('clamp', True):  # MapLibre's interpolate, which gis-composer draws with, always clamps
        t = f'max(0, min(1, {t}))'
    return f'2 * {number(mm(1))} * ({number(r0)} + {number(r1 - r0)} * {t})'


# --- renderers -----------------------------------------------------------------------------------------------------

def renderer_element(layer, notes, wrap_filter=None):
    """<renderer-v2> for a vector layer, and the QGIS renderer type written."""
    where = {'layer': layer['id']}
    geometry, spec = layer['geometry'], layer['renderer']
    size = layer.get('size')
    size_expr = size_expression(size) if size else None
    if size and geometry != 'point':
        notes.append({**where, 'item': 'size', 'reason': 'size by field applies to point markers only'})
        size_expr = None
    if spec is None:
        node = ET.Element('renderer-v2', {'type': 'nullSymbol'})
        properties(node, 'data-defined-properties')
        return node, 'nullSymbol'
    kind, rules = spec['type'], None
    if wrap_filter is not None:
        rules = [{'filter': wrap_filter, 'label': f'{layer["id"]} filter', 'children': classes_as_rules(spec)}]
    elif kind == 'graduated' and spec.get('default') is not None:
        notes.append({**where, 'item': 'renderer.default', 'reason': 'graduated with a default symbol is written as a rule renderer (QGIS graduated has no fallback class)'})
        rules = classes_as_rules(spec)
    elif kind == 'rule':
        rules = classes_as_rules(spec)
    if rules is not None:
        kind = 'rule'
    attrs = {'forceraster': 0, 'symbollevels': 0, 'referencescale': -1, 'enableorderby': 0}
    node = ET.Element('renderer-v2', attrs)
    symbols = ET.Element('symbols')
    if kind == 'single':
        node.set('type', 'singleSymbol')
        symbol(symbols, spec['symbol'], geometry, '0', where, notes, size_expr)
        node.append(symbols)
    elif kind == 'categorized':
        node.set('type', 'categorizedSymbol')
        node.set('attr', spec['field'])
        categories = sub(node, 'categories')
        for index, item in enumerate(spec['classes']):
            value = item['value']
            text = ('true' if value else 'false') if isinstance(value, bool) else number(value) if isinstance(value, (int, float)) else str(value)
            sub(categories, 'category', {'value': text, 'type': 'bool' if isinstance(value, bool) else 'double' if isinstance(value, float) else 'long' if isinstance(value, int) else 'string',
                                         'label': item.get('label', text), 'symbol': index, 'render': 'false' if item.get('visible') is False else 'true',
                                         'uuid': key(layer['id'], 'category', index)})
            symbol(symbols, item['symbol'], geometry, str(index), {**where, 'class': text}, notes, size_expr)
        if spec.get('default') is not None:  # an empty value is QGIS's "all other values" category (unmatched values and NULL)
            index = len(spec['classes'])
            sub(categories, 'category', {'value': '', 'type': 'string', 'label': 'other', 'symbol': index, 'render': 'true',
                                         'uuid': key(layer['id'], 'category', 'default')})
            symbol(symbols, spec['default'], geometry, str(index), {**where, 'class': 'default'}, notes, size_expr)
        node.append(symbols)
    elif kind == 'graduated':
        node.set('type', 'graduatedSymbol')
        node.set('attr', spec['field'])
        node.set('graduatedMethod', 'GraduatedColor')
        ranges = sub(node, 'ranges')
        classes = spec['classes']
        for index, item in enumerate(classes):
            low, high = map(float, item['range'])
            # map-style ranges are [low, high) except the last; QGIS ranges include both ends and the first match wins,
            # so the upper bound of every class but the last steps just below the next class's lower bound
            upper = math.nextafter(high, -math.inf) if index < len(classes) - 1 and high == float(classes[index + 1]['range'][0]) else high
            sub(ranges, 'range', {'lower': repr(low), 'upper': repr(upper), 'label': item.get('label', f'{number(low)} - {number(high)}'),
                                  'symbol': index, 'render': 'false' if item.get('visible') is False else 'true',
                                  'uuid': key(layer['id'], 'range', index)})
            symbol(symbols, item['symbol'], geometry, str(index), {**where, 'class': f'{number(low)}-{number(high)}'}, notes, size_expr)
        node.append(symbols)
        method = sub(node, 'classificationMethod', {'id': 'Custom'})
        sub(method, 'symmetricMode', {'enabled': 0, 'symmetrypoint': 0, 'astride': 0})
        sub(method, 'labelFormat', {'format': '%1 - %2', 'labelprecision': 4, 'trimtrailingzeroes': 1})
    else:
        node.set('type', 'RuleRenderer')
        holder = sub(node, 'rules', {'key': key(layer['id'], 'rules')})
        write_rules(holder, symbols, rules, layer, notes, size_expr, [0])
        node.append(symbols)
    order = layer.get('sortBy')
    if order:
        node.set('enableorderby', 1)
        clauses = sub(node, 'orderby')
        sub(clauses, 'orderByClause', {'asc': 0 if order.get('order') == 'desc' else 1, 'nullsFirst': 0}, field(order['field']))
    properties(node, 'data-defined-properties')
    return node, node.get('type')


def write_rules(parent, symbols, rules, layer, notes, size_expr, counter):
    """map-style rules take the first match; QGIS draws every matching rule, so each rule also excludes the earlier ones."""
    where = {'layer': layer['id']}
    earlier = []
    for index, rule in enumerate(rules):
        if rule.get('children'):  # a layer filter kept as a parent rule around the converted classes
            node = sub(parent, 'rule', {'key': key(layer['id'], 'parent', rule['label']), 'filter': rule['filter'], 'label': rule['label']})
            write_rules(node, symbols, rule['children'], layer, notes, size_expr, counter)
            continue
        if rule.get('filter') is None:
            this = None
        elif isinstance(rule['filter'], str):
            this = rule['filter']
        else:
            this = expression(rule['filter'], {**where, 'rule': rule.get('label', index)}, notes, rule=True)
            if this is None:
                continue  # skipped, and reported by expression()
        parts = ([f'coalesce({this}, FALSE)'] if this else []) + [f'NOT coalesce({e}, FALSE)' for e in earlier]
        if this:
            earlier.append(this)
        if rule.get('symbol') is None:
            continue  # a default of null: features left over are not drawn
        attrs = {'key': key(layer['id'], 'rule', counter[0]), 'filter': ' AND '.join(parts) if parts else 'TRUE',
                 'label': rule.get('label', 'other' if rule.get('default') else str(index)), 'symbol': counter[0]}
        if rule.get('visible') is False:
            attrs['active'] = 0
        sub(parent, 'rule', attrs)
        symbol(symbols, rule['symbol'], layer['geometry'], str(counter[0]), {**where, 'rule': attrs['label']}, notes, size_expr)
        counter[0] += 1


# --- labels --------------------------------------------------------------------------------------------------------

def template_expression(template):
    parts = []
    for index, piece in enumerate(re.split(r'\{([^{}]+)\}', template)):
        if piece:
            parts.append(field(piece) if index % 2 else literal(piece))
    return parts[0] if len(parts) == 1 else 'concat(' + ', '.join(parts) + ')'


def text_style(parent, style, field_name, is_expression, fonts, where, notes):
    """<text-style>: the font is the installed family and style a theme alias resolves to (fonts: {alias: (family,
    style)}); letter spacing in em becomes mm at the text size; halo blur is a blur effect on the buffer."""
    alias = style.get('font') or ''
    family, named = fonts.get(alias, (alias, ''))
    size = mm(style['size'])
    attrs = {'fontFamily': family, 'namedStyle': named, 'fontSize': number(size), 'fontSizeUnit': 'MM',
             'textColor': color(style['color']), 'textOpacity': number(style.get('opacity', 1)), 'fieldName': field_name,
             'isExpression': int(is_expression), 'fontLetterSpacing': number(float(style.get('letterSpacing', 0)) * size),
             'fontWordSpacing': 0, 'fontWeight': 50, 'fontItalic': 0, 'fontKerning': 1, 'multilineHeight': 1, 'multilineHeightUnit': 'Percentage',
             'blendMode': 0, 'capitalization': 0, 'allowHtml': 0, 'textOrientation': 'horizontal', 'previewBkgrdColor': '255,255,255,255'}
    node = sub(parent, 'text-style', attrs)
    sub(node, 'families')
    halo = style.get('halo')
    buffer = sub(node, 'text-buffer', {'bufferDraw': int(bool(halo)), 'bufferSize': number(mm(halo['width'])) if halo else '1',
                                       'bufferSizeUnits': 'MM', 'bufferColor': color(halo['color']) if halo else '255,255,255,255',
                                       'bufferOpacity': number(halo.get('opacity', 1)) if halo else 1, 'bufferNoFill': 1,
                                       'bufferJoinStyle': 128, 'bufferBlendMode': 0})
    if halo and halo.get('blur'):
        effect = sub(buffer, 'effect', {'type': 'blur'})
        options(effect, {'blend_mode': '0', 'blur_level': number(mm(halo['blur'])), 'blur_method': '0', 'blur_unit': 'MM',
                         'draw_mode': '2', 'enabled': '1', 'opacity': '1'})
    box = style.get('box')
    if box:
        fill, stroke = box.get('fill'), box.get('stroke')
        padding = number(mm(box.get('padding', 0)))
        sub(node, 'background', {'shapeDraw': 1, 'shapeType': 0, 'shapeSizeType': 0, 'shapeSizeX': padding, 'shapeSizeY': padding,
                                 'shapeSizeUnit': 'MM', 'shapeOpacity': 1, 'shapeBlendMode': 0, 'shapeJoinStyle': 64,
                                 'shapeFillColor': color(fill['color'], fill.get('opacity', 1)) if fill else '0,0,0,0',
                                 'shapeBorderColor': color(stroke['color'], stroke.get('opacity', 1)) if stroke else '0,0,0,0',
                                 'shapeBorderWidth': number(mm(stroke['width'])) if stroke else 0, 'shapeBorderWidthUnit': 'MM'})
        if stroke and stroke.get('dash'):
            notes.append({**where, 'item': 'text.box.stroke.dash', 'reason': 'a label box border is drawn solid'})
    else:
        sub(node, 'background', {'shapeDraw': 0})
    sub(node, 'shadow', {'shadowDraw': 0})
    properties(node, 'dd_properties')
    return node


def label_settings(parent, spec, geometry, fonts, where, notes):
    """<settings> for a layer's own labels (platform-placed)."""
    settings = sub(parent, 'settings', {'calloutType': 'simple'})
    if spec.get('template'):
        text, is_expression = template_expression(spec['template']), True
    else:
        text, is_expression = spec['field'], False
    text_style(settings, spec['text'], text, is_expression, fonts, where, notes)
    sub(settings, 'text-format', {'wrapChar': '', 'multilineAlign': 3, 'formatNumbers': 0, 'decimals': 3, 'plussign': 0})
    placement = spec.get('placement', 'auto')
    mode = {'point': {'auto': 0, 'point': 1}, 'line': {'auto': 2, 'line': 3}, 'polygon': {'auto': 1, 'polygon-center': 1}}[geometry].get(placement)
    if mode is None:
        mode = {'point': 1, 'line': 2, 'polygon': 1}[geometry]
    sub(settings, 'placement', {'placement': mode, 'placementFlags': 9 if geometry == 'line' else 10, 'quadOffset': 4, 'dist': 0,
                                'distUnits': 'MM', 'offsetType': 0, 'centroidInside': int(placement == 'polygon-center'), 'centroidWhole': 0,
                                'fitInPolygonOnly': 0, 'overlapHandling': 'PreventOverlap', 'allowDegraded': 0, 'priority': 5,
                                'rotationAngle': 0, 'rotationUnit': 'AngleDegrees', 'preserveRotation': 1, 'repeatDistance': 0,
                                'xOffset': 0, 'yOffset': 0, 'offsetUnits': 'MM', 'maxCurvedCharAngleIn': 25, 'maxCurvedCharAngleOut': -25})
    sub(settings, 'rendering', {'drawLabels': 1, 'obstacle': 1, 'obstacleFactor': 1, 'obstacleType': 1, 'unplacedVisibility': 0,
                                'scaleVisibility': 0, 'upsidedownLabels': 0, 'labelPerPart': 0, 'mergeLines': 0, 'zIndex': 0,
                                'limitNumLabels': 0, 'maxNumLabels': 2000, 'fontLimitPixelSize': 0, 'minFeatureSize': 0})
    properties(settings, 'dd_properties')
    return settings


def labeling(spec, geometry, fonts, notes, condition=None, rule_key=''):
    """<labeling> for a layer's own labels: simple, or one rule when the layer has a filter."""
    where = {'layer': rule_key}
    if condition is None:
        node = ET.Element('labeling', {'type': 'simple'})
        label_settings(node, spec, geometry, fonts, where, notes)
    else:
        node = ET.Element('labeling', {'type': 'rule-based'})
        rule = sub(sub(node, 'rules', {'key': key(rule_key, 'label-rules')}), 'rule', {'key': key(rule_key, 'label-rule'), 'filter': condition})
        label_settings(rule, spec, geometry, fonts, where, notes)
    return node


def placement_labeling(styles, fonts, notes, name):
    """Labels exactly where the map-style put them: one rule per distinct text style (field style_id), so font,
    size, colours, halo, spacing and box are static settings; anchor (quadrant) and rotation come from fields.
    They may overlap and always show, as in the PNG."""
    node = ET.Element('labeling', {'type': 'rule-based'})
    rules = sub(node, 'rules', {'key': key(name, 'placement-rules')})
    for index, style in enumerate(styles, 1):
        rule = sub(rules, 'rule', {'key': key(name, 'placement-rule', index), 'filter': f'"style_id" = {index}', 'description': f'style {index}'})
        settings = sub(rule, 'settings', {'calloutType': 'simple'})
        text_style(settings, style, 'text', False, fonts, {'layer': 'labelPlacements', 'style': index}, notes)
        sub(settings, 'text-format', {'wrapChar': '', 'multilineAlign': 3, 'formatNumbers': 0, 'decimals': 3, 'plussign': 0})
        sub(settings, 'placement', {'placement': 1, 'quadOffset': 4, 'dist': 0, 'distUnits': 'MM', 'offsetType': 0, 'xOffset': 0,
                                    'yOffset': 0, 'offsetUnits': 'MM', 'overlapHandling': 'AllowOverlapWithoutPenalty', 'allowDegraded': 1,
                                    'priority': 10, 'rotationAngle': 0, 'rotationUnit': 'AngleDegrees', 'preserveRotation': 1, 'placementFlags': 10})
        sub(settings, 'rendering', {'drawLabels': 1, 'obstacle': 0, 'obstacleFactor': 1, 'obstacleType': 1, 'unplacedVisibility': 1,
                                    'scaleVisibility': 0, 'upsidedownLabels': 2, 'labelPerPart': 0, 'mergeLines': 0, 'zIndex': 0,
                                    'limitNumLabels': 0, 'maxNumLabels': 2000, 'fontLimitPixelSize': 0, 'minFeatureSize': 0})
        properties(settings, 'dd_properties', {'OffsetQuad': ('field', 'quadrant'), 'LabelRotation': ('expression', 'coalesce("rotation", 0)')})
    return node


def leader_renderer(strokes, notes, name):
    """Leader lines: one rule per distinct leader stroke (field stroke_id)."""
    node = ET.Element('renderer-v2', {'type': 'RuleRenderer', 'forceraster': 0, 'symbollevels': 0, 'referencescale': -1, 'enableorderby': 0})
    rules = sub(node, 'rules', {'key': key(name, 'leader-rules')})
    symbols = ET.Element('symbols')
    for index, stroke in enumerate(strokes, 1):
        sub(rules, 'rule', {'key': key(name, 'leader-rule', index), 'filter': f'"stroke_id" = {index}', 'symbol': index - 1,
                            'label': f'leader {index}'})
        symbol(symbols, {'stroke': stroke}, 'line', str(index - 1), {'layer': 'labelPlacements', 'leader': index}, notes)
    node.append(symbols)
    properties(node, 'data-defined-properties')
    return node


# --- rasters -------------------------------------------------------------------------------------------------------

def raster_renderer(spec, stats, opacity, notes, layer):
    """<pipe> for a raster layer. stats: {band: (min, max)} of the packed window, for rgb stretches."""
    pipe = ET.Element('pipe')
    kind = spec['type']
    where = {'layer': layer}
    if kind == 'pseudocolor':
        band = spec.get('band', 1)
        stops = sorted(spec['stops'], key=lambda s: s['value'])
        mode = spec.get('interpolation', 'linear')
        lo, hi = stops[0]['value'], stops[-1]['value']
        node = sub(pipe, 'rasterrenderer', {'type': 'singlebandpseudocolor', 'band': band, 'opacity': number(opacity),
                                            'alphaBand': -1, 'classificationMin': number(lo), 'classificationMax': number(hi),
                                            'nodataColor': color(spec['nodata']['color']) if (spec.get('nodata') or {}).get('color') else ''})
        shader = sub(sub(node, 'rastershader'), 'colorrampshader', {
            'colorRampType': {'linear': 'INTERPOLATED', 'discrete': 'DISCRETE', 'exact': 'EXACT'}[mode], 'classificationMode': 1,
            'clip': 0, 'minimumValue': number(lo), 'maximumValue': number(hi), 'labelPrecision': 4})
        if mode == 'discrete':
            # a stop's colour holds from its value up to the next stop; QGIS discrete items hold up to and including
            # their value, so each colour is written at just below the next stop, the last one open-ended
            for index, stop in enumerate(stops):
                upper = repr(math.nextafter(float(stops[index + 1]['value']), -math.inf)) if index + 1 < len(stops) else 'inf'
                sub(shader, 'item', {'value': upper, 'color': '#' + stop['color'].lstrip('#').lower(), 'alpha': 255, 'label': stop.get('label', number(stop['value']))})
        else:
            for stop in stops:
                sub(shader, 'item', {'value': number(stop['value']), 'color': '#' + stop['color'].lstrip('#').lower(), 'alpha': 255,
                                     'label': stop.get('label', number(stop['value']))})
        if (spec.get('stretch') or {}).get('method') not in (None, 'manual'):
            notes.append({**where, 'item': 'renderer.stretch', 'reason': f'stretch {spec["stretch"]["method"]} is not recomputed; stop values are used as data values'})
    elif kind == 'palette':
        node = sub(pipe, 'rasterrenderer', {'type': 'paletted', 'band': spec.get('band', 1), 'opacity': number(opacity), 'alphaBand': -1})
        palette = sub(node, 'colorPalette')
        for entry in spec['classes']:
            sub(palette, 'paletteEntry', {'value': number(entry['value']), 'color': '#' + entry['color'].lstrip('#').lower(), 'alpha': 255,
                                          'label': entry.get('label', number(entry['value']))})
    else:
        red, green, blue = spec['bands']
        node = sub(pipe, 'rasterrenderer', {'type': 'multibandcolor', 'redBand': red, 'greenBand': green, 'blueBand': blue,
                                            'opacity': number(opacity), 'alphaBand': -1})
        for name, band in (('red', red), ('green', green), ('blue', blue)):
            low, high = stats.get(band, (0, 255))
            enhance = sub(node, f'{name}ContrastEnhancement')
            sub(enhance, 'minValue', text=number(low))
            sub(enhance, 'maxValue', text=number(high))
            sub(enhance, 'algorithm', text='StretchToMinimumMaximum')
    sub(pipe, 'brightnesscontrast', {'brightness': 0, 'contrast': 0, 'gamma': 1})
    sub(pipe, 'huesaturation', {'saturation': 0, 'grayscaleMode': 0, 'colorizeOn': 0, 'invertColors': 0})
    sub(pipe, 'rasterresampler', {'maxOversampling': 2})
    return pipe


# --- layer styles --------------------------------------------------------------------------------------------------

def style_elements(layer, notes, fonts):
    """The style children shared by a QML document and a project <maplayer>, and the QGIS renderer type.

    A layer written from labelPlacements carries `_placement` ('labels' or 'leaders') with its `_styles` or `_strokes`.
    """
    elements = []
    labels_enabled = False
    placement = layer.get('_placement')
    if placement == 'labels':
        null = ET.Element('renderer-v2', {'type': 'nullSymbol'})
        properties(null, 'data-defined-properties')
        elements += [null, placement_labeling(layer['_styles'], fonts, notes, layer['id'])]
        kind, labels_enabled = 'nullSymbol', True
    elif placement == 'leaders':
        elements.append(leader_renderer(layer['_strokes'], notes, layer['id']))
        kind = 'RuleRenderer'
    else:
        # a layer filter becomes a parent rule around the renderer's classes (and around the labels), so the style
        # alone shows the same features in the project and when the table is added on its own
        condition = expression(layer.get('filter'), {'layer': layer['id']}, notes)
        renderer, kind = renderer_element(layer, notes, condition if layer.get('renderer') else None)
        elements.append(renderer)
        labels = layer.get('labels')
        if labels:
            elements.append(labeling(labels, layer['geometry'], fonts, notes, condition, layer['id']))
            labels_enabled = labels.get('enabled', True)
        elif condition is not None and not layer.get('renderer'):
            notes.append({'layer': layer['id'], 'item': 'filter', 'reason': 'a layer that draws nothing and has no labels: the filter has no effect'})
    for tag, text in (('blendMode', 0), ('featureBlendMode', 0), ('layerOpacity', number(layer.get('opacity', 1)))):
        elements.append(ET.Element(tag))
        elements[-1].text = str(text)
    return elements, kind, labels_enabled


def qml(layer, notes, fonts):
    """A complete QML document for one vector layer, and the QGIS renderer type."""
    elements, kind, labels_enabled = style_elements(layer, notes, fonts)
    root = ET.Element('qgis', {'version': QGIS_VERSION, 'styleCategories': 'AllStyleCategories', 'labelsEnabled': int(labels_enabled),
                               'hasScaleBasedVisibilityFlag': 0, 'minScale': 100000000, 'maxScale': 0, 'readOnly': 0})
    root.extend(elements)
    sub(root, 'layerGeometryType', text=GEOMETRY_TYPES[layer['geometry']][1])
    return serialize(root), kind


# --- project -------------------------------------------------------------------------------------------------------

def spatialrefsys(parent, crs):
    node = sub(parent, 'spatialrefsys', {'nativeFormat': 'Wkt'})
    if crs is None:
        return node
    from pyproj.enums import WktVersion
    authority = crs.to_authority(min_confidence=90)
    sub(node, 'wkt', text=crs.to_wkt(WktVersion.WKT2_2019))
    sub(node, 'srsid', text=0)
    sub(node, 'srid', text=authority[1] if authority and authority[0] == 'EPSG' else 0)
    sub(node, 'authid', text=':'.join(authority) if authority else '')
    sub(node, 'description', text=crs.name)
    sub(node, 'geographicflag', text='true' if crs.is_geographic else 'false')
    return node


def tree_nodes(parent, nodes, by_id):
    """Layer tree: nodes are ('group', name, checked, children) or ('layer', id), top first."""
    for node in nodes:
        if node[0] == 'group':
            _, name, checked, children = node
            group = sub(parent, 'layer-tree-group', {'name': name, 'checked': 'Qt::Checked' if checked else 'Qt::Unchecked', 'expanded': 1})
            sub(sub(group, 'customproperties'), 'Option')
            tree_nodes(group, children, by_id)
        else:
            row = by_id[node[1]]
            sub(parent, 'layer-tree-layer', {'id': layer_id(row['id']), 'name': row['name'], 'source': row['source'], 'providerKey': row['provider'],
                                             'checked': 'Qt::Unchecked' if row['layer'].get('visible') is False else 'Qt::Checked',
                                             'expanded': 1})


def project(name, title, layers, tree, crs, extent, notes, fonts, background=None, themes=(), bookmarks=()):
    """The .qgs XML.

    layers: [{id, name, layer (map-style layer or a placement layer), source, provider, kind ('vector'|'raster'), crs,
    wkb, raster}] in drawing order, bottom first; tree: tree_nodes() nodes, top first; extent: (xmin, ymin, xmax, ymax)
    in crs; background: '#RRGGBB'; themes: [(name, visible layer ids, checked group paths)]; bookmarks: [(name,
    (xmin, ymin, xmax, ymax) in crs)].
    """
    root = ET.Element('qgis', {'version': QGIS_VERSION, 'projectname': title or name, 'saveUser': 'gis-kit'})
    sub(root, 'homePath', {'path': ''})
    sub(root, 'title', text=title or name)
    spatialrefsys(sub(root, 'projectCrs'), crs)
    ordered = list(reversed(layers))  # QGIS lists the top layer first
    group = sub(root, 'layer-tree-group')
    sub(sub(group, 'customproperties'), 'Option')
    tree_nodes(group, tree, {row['id']: row for row in layers})
    order = sub(group, 'custom-order', {'enabled': 1})
    for row in ordered:
        sub(order, 'item', text=layer_id(row['id']))
    canvas = sub(root, 'mapcanvas', {'name': 'theMapCanvas', 'annotationsVisible': 1})
    sub(canvas, 'units', text='degrees' if crs is not None and crs.is_geographic else 'meters')
    box = sub(canvas, 'extent')
    for tag, value in zip(('xmin', 'ymin', 'xmax', 'ymax'), extent):
        sub(box, tag, text=repr(float(value)))
    sub(canvas, 'rotation', text=0)
    spatialrefsys(sub(canvas, 'destinationsrs'), crs)
    projectlayers = sub(root, 'projectlayers')
    for row in ordered:
        layer = row['layer']
        if row['kind'] == 'raster':
            node = sub(projectlayers, 'maplayer', {'type': 'raster', 'styleCategories': 'AllStyleCategories', 'hasScaleBasedVisibilityFlag': 0})
        else:
            geometry, _ = GEOMETRY_TYPES[layer['geometry']]
            node = sub(projectlayers, 'maplayer', {'type': 'vector', 'geometry': geometry, 'wkbType': row['wkb'],
                                                   'styleCategories': 'AllStyleCategories', 'hasScaleBasedVisibilityFlag': 0})
        sub(node, 'id', text=layer_id(row['id']))
        sub(node, 'datasource', text=row['source'])
        sub(node, 'layername', text=row['name'])
        spatialrefsys(sub(node, 'srs'), row['crs'])
        sub(node, 'provider', {'encoding': 'UTF-8'} if row['kind'] == 'vector' else {}, row['provider'])
        if row['kind'] == 'raster':
            node.append(raster_renderer(layer['renderer'], row.get('raster', {}).get('stats', {}), layer.get('opacity', 1), notes, layer['id']))
            sub(node, 'blendMode', text=0)
        else:
            elements, _, labels_enabled = style_elements(layer, [], fonts)  # notes were collected by qml()
            node.set('labelsEnabled', int(labels_enabled))
            node.extend(elements)
    legacy = sub(root, 'layerorder')
    for row in ordered:
        sub(legacy, 'layer', {'id': layer_id(row['id'])})
    props = sub(root, 'properties')
    sub(sub(props, 'Paths'), 'Absolute', {'type': 'bool'}, 'false')
    if crs is not None:  # QGIS reads <projectCrs> only when projections are enabled
        sub(sub(props, 'SpatialRefSys'), 'ProjectionsEnabled', {'type': 'int'}, 1)
    if background:
        gui = sub(props, 'Gui')
        for part, value in zip(('Red', 'Green', 'Blue'), color(background).split(',')[:3]):
            sub(gui, f'CanvasColor{part}Part', {'type': 'int'}, value)
    presets = sub(root, 'visibility-presets')
    for theme, visible, checked in themes:
        preset = sub(presets, 'visibility-preset', {'name': theme, 'has-checked-group-info': 1, 'has-expanded-info': 0})
        for row in ordered:
            sub(preset, 'layer', {'id': layer_id(row['id']), 'visible': int(row['id'] in visible), 'style': 'default', 'expanded': 1})
        nodes = sub(preset, 'checked-group-nodes')
        for path in checked:
            sub(nodes, 'checked-group-node', {'id': path})
    marks = sub(root, 'Bookmarks')
    for mark, (xmin, ymin, xmax, ymax) in bookmarks:
        node = sub(marks, 'Bookmark', {'id': key(name, 'bookmark', mark), 'name': mark, 'group': '', 'rotation': 0,
                                       'extent': f'Polygon (({xmin!r} {ymin!r}, {xmax!r} {ymin!r}, {xmax!r} {ymax!r}, {xmin!r} {ymax!r}, {xmin!r} {ymin!r}))'})
        spatialrefsys(node, crs)
    settings = sub(root, 'ProjectViewSettings', {'UseProjectScales': 0, 'rotation': 0})
    sub(settings, 'Scales')
    view = sub(settings, 'DefaultViewExtent', dict(zip(('xmin', 'ymin', 'xmax', 'ymax'), map(repr, map(float, extent)))))
    spatialrefsys(view, crs)
    return serialize(root)


# --- GeoPackage tables ---------------------------------------------------------------------------------------------

LAYER_STYLES = ('CREATE TABLE IF NOT EXISTS "layer_styles" ("id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, "f_table_catalog" TEXT(256), '
                '"f_table_schema" TEXT(256), "f_table_name" TEXT(256), "f_geometry_column" TEXT(256), "styleName" TEXT(30), '
                '"styleQML" TEXT, "styleSLD" TEXT, "useAsDefault" BOOLEAN, "description" TEXT, "owner" TEXT(30), "ui" TEXT(30), '
                '"update_time" DATETIME DEFAULT (strftime(\'%Y-%m-%dT%H:%M:%fZ\',\'now\')))')


def write_styles(gpkg, rows):
    """rows: [{table, geometry_column, name, qml, default, description}] into layer_styles, as QGIS writes it."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    with sqlite3.connect(gpkg) as db:
        db.execute(LAYER_STYLES)
        db.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description, last_change) "
                   "VALUES ('layer_styles', 'attributes', 'layer_styles', '', ?)", (now,))
        for row in rows:
            db.execute('INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name, f_geometry_column, styleName, styleQML, '
                       'styleSLD, useAsDefault, description, owner, ui, update_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('', '', row['table'], row['geometry_column'], row['name'][:30], row['qml'], '', int(row['default']),
                        row.get('description', ''), '', '', now))
    db.close()


def write_project(gpkg, name, qgs):
    """The project into qgis_projects: content is the zipped .qgs (a .qgz) as hex text, as QGIS stores it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f'{name}.qgs', qgs)
    metadata = json.dumps({'last_modified_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'last_modified_user': 'gis-kit'})
    with sqlite3.connect(gpkg) as db:
        db.execute('CREATE TABLE IF NOT EXISTS qgis_projects(name TEXT PRIMARY KEY, metadata BLOB, content BLOB)')
        db.execute('INSERT INTO qgis_projects (name, metadata, content) VALUES (?, ?, ?)', (name, metadata, buffer.getvalue().hex()))
    db.close()


def read_project(gpkg, name):
    """The .qgs text stored under name (for checks)."""
    with sqlite3.connect(gpkg) as db:
        content, = db.execute('SELECT content FROM qgis_projects WHERE name = ?', (name,)).fetchone()
    db.close()
    data = bytes.fromhex(content.decode() if isinstance(content, bytes) else content)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(next(n for n in archive.namelist() if n.endswith('.qgs'))).decode('utf-8')
