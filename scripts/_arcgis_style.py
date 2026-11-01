"""map-style v1 -> ArcGIS Pro: CIM JSON for a layer file (.lyrx), its definition queries and Arcade expressions.

One writer per platform: this module knows the Cartographic Information Model (CIM, github.com/Esri/cim-spec) and
nothing about where the data came from; package.py pack hands it the layers with their file-geodatabase feature
classes (or GeoTIFFs) and the field names as written there. Everything that cannot be expressed is reported
through `notes` ({layer, item, reason}) instead of being dropped silently.

CIM conventions used here: sizes in points (map-style px x 0.75), colours CIMRGBColor [r, g, b, alpha 0-100],
symbol layers listed top first, layers listed top first (map-style lists them bottom first), marker rotation
counter-clockwise. Definition queries are file-geodatabase SQL; renderers that need a first-match rule list use an
Arcade value expression. The document is written as CIM 3.0 (ArcGIS Pro 3.0 and later), as Pro writes the layer
file inside a layer package.
"""
from __future__ import annotations

import json
import math
import re
import uuid

from _map_style import classes_as_rules, label_gap, layer_symbols, line_marker_outline, number, padding_sides, resolve_font

CIM_VERSION, CIM_BUILD = '3.0.0', 36056  # the CIM that Pro 3.x writes into the p30 folder of a .lpkx
PT_PER_PX = 0.75  # CSS pixel 1/96 inch, point 1/72 inch
NAMESPACE = uuid.UUID('9d6c1f7e-2a43-4f0b-8f6e-3c1b5a7d2e90')
CAPS = {'butt': 'Butt', 'round': 'Round', 'square': 'Square'}
JOINS = {'miter': 'Miter', 'round': 'Round', 'bevel': 'Bevel'}
DEFAULT_MARKER_PX = 6
# Maplex and standard-engine positions for a label whose `anchor` side sits on the point (the text lies opposite)
ANCHOR_MAPLEX = {'center': 'CenteredOnPoint', 'left': 'EastOfPoint', 'right': 'WestOfPoint', 'top': 'SouthOfPoint',
                 'bottom': 'NorthOfPoint', 'top-left': 'SouthEastOfPoint', 'top-right': 'SouthWestOfPoint',
                 'bottom-left': 'NorthEastOfPoint', 'bottom-right': 'NorthWestOfPoint'}
ANCHOR_STANDARD = {'left': 'centerRight', 'right': 'centerLeft', 'top': 'belowCenter', 'bottom': 'aboveCenter',
                   'top-left': 'belowRight', 'top-right': 'belowLeft', 'bottom-left': 'aboveRight', 'bottom-right': 'aboveLeft'}
COLOR_SPACE = {'type': 'CIMICCColorSpace', 'url': 'Default RGB'}


def pt(px):
    return float(px) * PT_PER_PX


def rgb(value, opacity=1.0):
    value = value.lstrip('#')
    return {'type': 'CIMRGBColor', 'values': [int(value[i:i + 2], 16) for i in (0, 2, 4)] + [round(100 * (1.0 if opacity is None else opacity), 2)]}


def guid(*parts):
    """A GUID that stays the same for the same parts."""
    return str(uuid.uuid5(NAMESPACE, '/'.join(map(str, parts)))).upper()


# --- filters -------------------------------------------------------------------------------------------------------

class Unusable(ValueError):
    """A filter part in a dialect this target cannot read."""


def sql_field(name):
    return name if re.fullmatch(r'[^\W\d]\w*', name) else '"' + name.replace('"', '""') + '"'


def sql_literal(value):
    if isinstance(value, bool):
        return '1' if value else '0'  # the file geodatabase has no boolean type: GDAL writes 0/1 small integers
    if isinstance(value, (int, float)):
        return number(value)
    return "'" + str(value).replace("'", "''") + "'"


SQL_OPS = {'==': '=', '!=': '<>', '<': '<', '<=': '<=', '>': '>', '>=': '>='}


def sql(spec, names):
    """A map-style filter as file-geodatabase SQL; names maps map-style field names to the written ones.

    Every leaf is two-valued as in MapLibre (a missing value never equals anything: `!=` and `notIn` are true for
    NULL, comparisons false), so NOT, AND and OR never meet SQL's unknown.
    """
    if 'expression' in spec:
        if str(spec.get('dialect')).lower() != 'sql':
            raise Unusable(f'dialect {spec.get("dialect")!r} is not SQL ({spec["expression"]})')
        return f'({spec["expression"]})'
    for joiner in ('all', 'any'):
        if joiner in spec:
            return '(' + (' AND ' if joiner == 'all' else ' OR ').join(sql(item, names) for item in spec[joiner]) + ')'
    if 'not' in spec:
        return f'(NOT {sql(spec["not"], names)})'
    name, op, value = sql_field(names.get(spec['field'], spec['field'])), spec['op'], spec.get('value')
    if op == 'isNull':
        return f'({name} IS NULL)'
    if op == 'notNull':
        return f'({name} IS NOT NULL)'
    if op in ('in', 'notIn'):
        values = ', '.join(sql_literal(v) for v in value)
        if op == 'in':
            return f'({name} IS NOT NULL AND {name} IN ({values}))' if values else '(1 = 0)'
        return f'({name} IS NULL OR {name} NOT IN ({values}))' if values else '(1 = 1)'
    if op == '!=':
        return f'({name} IS NULL OR {name} <> {sql_literal(value)})'
    return f'({name} IS NOT NULL AND {name} {SQL_OPS[op]} {sql_literal(value)})'


def definition_query(spec, where, notes, names):
    """A definition query for a layer filter, or None. A dialect other than SQL filters nothing (reported)."""
    if spec is None:
        return None
    try:
        text = sql(spec, names)
    except Unusable as error:
        notes.append({**where, 'item': 'filter', 'reason': f'{error}: treated as no filter'})
        return None
    return text[1:-1]


def arcade_field(name, names):
    return '$feature[' + json.dumps(names.get(name, name), ensure_ascii=False) + ']'


def arcade_literal(value):
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return number(value)
    return json.dumps(str(value), ensure_ascii=False)


def arcade(spec, names):
    """A map-style filter as an Arcade condition, with the same NULL reading as sql()."""
    if 'expression' in spec:
        if str(spec.get('dialect')).lower() != 'arcade':
            raise Unusable(f'dialect {spec.get("dialect")!r} is not Arcade ({spec["expression"]})')
        return f'({spec["expression"]})'
    for joiner in ('all', 'any'):
        if joiner in spec:
            return '(' + (' && ' if joiner == 'all' else ' || ').join(arcade(item, names) for item in spec[joiner]) + ')'
    if 'not' in spec:
        return f'(!{arcade(spec["not"], names)})'
    name, op, value = arcade_field(spec['field'], names), spec['op'], spec.get('value')
    if op == 'isNull':
        return f'IsEmpty({name})'
    if op == 'notNull':
        return f'(!IsEmpty({name}))'
    if op in ('in', 'notIn'):
        values = '[' + ', '.join(arcade_literal(v) for v in value) + ']'
        if op == 'in':
            return f'(!IsEmpty({name}) && Includes({values}, {name}))'
        return f'(IsEmpty({name}) || !Includes({values}, {name}))'
    if op == '!=':
        return f'(IsEmpty({name}) || {name} != {arcade_literal(value)})'
    return f'(!IsEmpty({name}) && {name} {op} {arcade_literal(value)})'


def template_arcade(template, names):
    parts = []
    for index, piece in enumerate(re.split(r'\{([^{}]+)\}', template)):
        if piece:
            parts.append(f'DefaultValue({arcade_field(piece, names)}, "")' if index % 2 else json.dumps(piece, ensure_ascii=False))
    return ' + '.join(parts) or '""'


def expression_info(title, expression):
    return {'type': 'CIMExpressionInfo', 'title': title, 'expression': expression, 'returnType': 'Default'}


# --- symbols -------------------------------------------------------------------------------------------------------

def stroke_layer(stroke):
    layer = {'type': 'CIMSolidStroke', 'enable': True, 'capStyle': CAPS[stroke.get('cap', 'butt')], 'joinStyle': JOINS[stroke.get('join', 'miter')],
             'lineStyle3D': 'Strip', 'miterLimit': 10, 'width': pt(stroke['width']), 'height3D': 1, 'anchor3D': 'Center',
             'color': rgb(stroke['color'], stroke.get('opacity', 1))}
    if stroke.get('dash'):
        dash = list(stroke['dash'])
        if len(dash) % 2:
            dash = dash * 2  # an odd pattern repeats, as in SVG and canvas
        layer['effects'] = [{'type': 'CIMGeometricEffectDashes', 'dashTemplate': [pt(v) for v in dash], 'lineDashEnding': 'NoConstraint'}]
    return layer


def fill_layer(fill):
    return {'type': 'CIMSolidFill', 'enable': True, 'color': rgb(fill['color'], fill.get('opacity', 1))}


def hatch_layers(pattern):
    """`fill.pattern`: a hatch fill per set of lines, top first; its rotation is counter-clockwise like map-style's angle."""
    angles = [float(pattern.get('angle', 45))]
    if pattern['kind'] == 'crossHatch':
        angles.append(angles[0] + 90)
    line = {'color': pattern['color'], 'width': pattern['width'], 'opacity': pattern.get('opacity', 1)}
    return [{'type': 'CIMHatchFill', 'enable': True, 'lineSymbol': line_symbol(line), 'rotation': angle % 360, 'separation': pt(pattern['spacing'])}
            for angle in reversed(angles)]


def polygon_symbol(fill=None, stroke=None):
    """A CIMPolygonSymbol: the outline is listed first because CIM draws the first symbol layer on top."""
    layers = ([stroke_layer(stroke)] if stroke else []) + (hatch_layers(fill['pattern']) if fill and fill.get('pattern') else []) + ([fill_layer(fill)] if fill else [])
    return {'type': 'CIMPolygonSymbol', 'symbolLayers': layers, 'angleAlignment': 'Map'}


def line_symbol(stroke):
    return {'type': 'CIMLineSymbol', 'symbolLayers': [stroke_layer(stroke)] if stroke else []}


def circle():
    return {'curveRings': [[[0, 2], {'a': [[0, 2], [0, 0], 0, 1]}]]}


def turned(points, degrees):
    a = math.radians(degrees)
    return [[round(x * math.cos(a) - y * math.sin(a), 6), round(x * math.sin(a) + y * math.cos(a), 6)] for x, y in points]


CROSS = [[-0.4, 2], [0.4, 2], [0.4, 0.4], [2, 0.4], [2, -0.4], [0.4, -0.4], [0.4, -2], [-0.4, -2], [-0.4, -0.4], [-2, -0.4], [-2, 0.4], [-0.4, 0.4], [-0.4, 2]]
# map-style marker shapes in a frame of -2..2 (the marker size is the frame's height), outer rings clockwise, 0 pointing up
RINGS = {'square': [[-2, -2], [-2, 2], [2, 2], [2, -2], [-2, -2]], 'triangle': [[-2, -2], [0, 2], [2, -2], [-2, -2]],
         'diamond': [[0, -2], [-2, 0], [0, 2], [2, 0], [0, -2]], 'arrow': [[-2, -2], [0, 2], [2, -2], [-2, -2]]}  # as gis-composer draws its arrow
PATHS = {'cross': [[[0, -2], [0, 2]], [[-2, 0], [2, 0]]], 'x': [[[-2, -2], [2, 2]], [[-2, 2], [2, -2]]], 'chevron': [[[-2, -1], [0, 1], [2, -1]]]}


def rotation_override(marker, primitive, turn, names):
    """`rotationField` as a property connection of one symbol layer (a visual variable would rule the whole renderer,
    and each class has its own marker): counter-clockwise like CIM, NULL or non-numeric adding nothing."""
    if not marker.get('rotationField'):
        return []
    value = f'Number({arcade_field(marker["rotationField"], names)})'
    expression = f'var v = {value};\nreturn -({number(float(marker.get("rotation", 0)) + turn)} + IIf(IsNan(v), 0, v))'
    return [{'type': 'CIMPrimitiveOverride', 'primitiveName': primitive, 'propertyName': 'Rotation',
             'valueExpressionInfo': expression_info('map-style rotationField', expression)}]


def along_line_placement(marker):
    """`placement: line`: every spacing, the first half a spacing from the start, whatever the line's length."""
    spacing = pt(marker['spacing'])
    return {'type': 'CIMMarkerPlacementAlongLineSameSize', 'angleToLine': bool(marker.get('alongLine', True)), 'endings': 'NoConstraint',
            'offsetAlongLine': spacing / 2, 'placementTemplate': [spacing]}


def marker_layer(spec, marker, where, notes, names, primitive, turn=0):
    """The symbol layer that draws a marker in the symbol's fill and stroke, and its property connections. turn is added to the
    rotation (an image or shape drawn pointing up, along a line). An SVG is a picture marker with the image inside the style."""
    fill, stroke = spec.get('fill'), spec.get('stroke')
    shape = marker['shape']
    size = pt(marker['size'])
    if shape == 'svg':
        image = marker['_svg']
        # CIM's size is the height and map-style's the longer side, so a wide image is written lower; it keeps its own colours
        layer = {'type': 'CIMPictureMarker', 'enable': True, 'anchorPointUnits': 'Relative', 'dominantSizeAxis3D': 'Y',
                 'size': size * min(1.0, image['height'] / image['width']), 'billboardMode3D': 'FaceNearPlane', 'invertBackfaceTexture': True,
                 'scaleX': 1, 'textureFilter': 'Picture', 'tintColor': {'type': 'CIMRGBColor', 'values': [255, 255, 255, 100]},
                 'url': 'data:image/svg+xml;base64,' + image['base64']}
    else:
        lines_only = shape == 'chevron' or (shape in ('cross', 'x') and not fill)
        outline = stroke
        if outline is None and (shape == 'ring' or lines_only) and fill:  # a hollow or line-only shape takes the fill colour for its lines
            outline = line_marker_outline(shape, marker['size'], fill)
        plain = {'color': fill['color'], 'opacity': fill.get('opacity', 1)} if fill else None  # a marker cannot take a pattern
        if lines_only:
            graphic = {'type': 'CIMMarkerGraphic', 'geometry': {'paths': PATHS[shape]}, 'symbol': line_symbol(outline)}
        else:
            if shape in ('circle', 'ring'):
                geometry = circle()
            elif shape in ('cross', 'x'):
                geometry = {'rings': [turned(CROSS, 45 if shape == 'x' else 0)]}
            else:
                geometry = {'rings': [RINGS[shape]]}
            graphic = {'type': 'CIMMarkerGraphic', 'geometry': geometry,
                       'symbol': polygon_symbol(None if shape == 'ring' else plain, outline)}
        if not graphic['symbol']['symbolLayers']:
            notes.append({**where, 'item': 'symbol', 'reason': 'the marker has neither fill nor stroke: nothing draws'})
        layer = {'type': 'CIMVectorMarker', 'enable': True, 'anchorPointUnits': 'Relative', 'dominantSizeAxis3D': 'Z',
                 'size': size, 'billboardMode3D': 'FaceNearPlane', 'frame': {'xmin': -2, 'ymin': -2, 'xmax': 2, 'ymax': 2},
                 # stroke widths stay in points: with scaleSymbolsProportionally Pro multiplies them by size / frame height
                 'markerGraphics': [graphic], 'respectFrame': True}
    rotation = float(marker.get('rotation', 0)) + turn
    if rotation:
        layer['rotation'] = -rotation  # map-style turns clockwise, CIM counter-clockwise
    overrides = rotation_override(marker, primitive, turn, names)
    if overrides:
        layer['primitiveName'] = primitive
    return layer, overrides


def point_symbol(spec, where, notes, names=None, overrides=None):
    """A CIMPointSymbol with one marker in the map-style shape, fill and stroke; its property connections go into overrides."""
    marker = spec.get('marker')
    if not marker:
        notes.append({**where, 'item': 'symbol.marker', 'reason': f'point symbol without a marker: drawn as a {DEFAULT_MARKER_PX} px circle'})
        marker = {'shape': 'circle', 'size': DEFAULT_MARKER_PX}
    layer, connections = marker_layer(spec, marker, where, notes, names or {}, guid(json.dumps(where, sort_keys=True, ensure_ascii=False), 'marker'))
    if overrides is not None:
        overrides += connections
    return {'type': 'CIMPointSymbol', 'symbolLayers': [layer], 'haloSize': 1, 'scaleX': 1, 'angleAlignment': 'Display'}


def along_marker(spec, where, notes, names):
    """The symbol layer for `placement: line` on a line or an outline. CIM turns a marker with the line by the line's
    angle, so a marker drawn pointing up would stand across the line; map-style's 0 points along it."""
    marker = spec['marker']
    if spec.get('stroke'):  # the dash belongs to the line the marker sits on
        spec = {**spec, 'stroke': {k: v for k, v in spec['stroke'].items() if k != 'dash'}}
    layer, overrides = marker_layer(spec, marker, where, notes, names, guid(json.dumps(where, sort_keys=True, ensure_ascii=False), 'along'),
                                    turn=90 if marker.get('alongLine', True) else 0)
    layer['markerPlacement'] = along_line_placement(marker)
    return layer, overrides


def symbol(spec, geometry, where, notes, names=None):
    """A CIMSymbolReference for one map-style Symbol on a layer of this geometry."""
    fill, stroke, marker = spec.get('fill'), spec.get('stroke'), spec.get('marker')
    names = names or {}
    along = bool(marker) and marker.get('placement') == 'line'
    overrides = []
    if geometry != 'polygon' and fill and fill.get('pattern'):
        notes.append({**where, 'item': 'symbol.fill.pattern', 'reason': 'a pattern fills polygons only: not drawn on this layer'})
    if geometry == 'polygon':
        if marker and not along:
            notes.append({**where, 'item': 'symbol.marker', 'reason': 'a marker on a polygon layer is not drawn unless it runs along the outline (placement: line)'})
        body = polygon_symbol(fill, stroke)
    elif geometry == 'line':
        if fill and not along:
            notes.append({**where, 'item': 'symbol.fill', 'reason': 'fill on a line layer is not drawn'})
        if marker and not along:
            notes.append({**where, 'item': 'symbol.marker', 'reason': 'a marker on a line layer is not drawn unless it runs along the line (placement: line)'})
        body = line_symbol(stroke)
    else:
        if along:
            notes.append({**where, 'item': 'symbol.marker.placement', 'reason': 'a point layer draws one marker at each point: placement line does not apply'})
        body = point_symbol(spec, where, notes, names, overrides)
    if along and geometry != 'point':
        layer, overrides = along_marker(spec, where, notes, names)
        body['symbolLayers'].insert(0, layer)  # CIM draws the first symbol layer on top
    if not body['symbolLayers']:
        notes.append({**where, 'item': 'symbol', 'reason': f'nothing in this symbol draws on a {geometry} layer'})
    reference = {'type': 'CIMSymbolReference', 'symbol': body}
    if overrides:
        reference['primitiveOverrides'] = overrides
    return reference


def empty_symbol(geometry):
    body = {'polygon': {'type': 'CIMPolygonSymbol', 'symbolLayers': []}, 'line': {'type': 'CIMLineSymbol', 'symbolLayers': []},
            'point': {'type': 'CIMPointSymbol', 'symbolLayers': [], 'haloSize': 1, 'scaleX': 1, 'angleAlignment': 'Display'}}[geometry]
    return {'type': 'CIMSymbolReference', 'symbol': body}


# --- renderers -----------------------------------------------------------------------------------------------------

def value_text(value):
    """A unique value as CIM writes it (strings); booleans are the 0/1 small integers GDAL writes."""
    if isinstance(value, bool):
        return '1' if value else '0'
    return number(value) if isinstance(value, (int, float)) else str(value)


def unique_class(label, values, symbol_ref, visible=True):
    return {'type': 'CIMUniqueValueClass', 'editable': True, 'label': label, 'patch': 'Default', 'symbol': symbol_ref,
            'values': [{'type': 'CIMUniqueValue', 'fieldValues': [v]} for v in values], 'visible': visible}


def unique_renderer(fields, classes, default, default_label, heading, geometry, where, notes, expression=None, names=None):
    renderer = {'type': 'CIMUniqueValueRenderer', 'sampleSize': 10000, 'defaultLabel': default_label, 'defaultSymbolPatch': 'Default',
                'fields': fields, 'groups': [{'type': 'CIMUniqueValueGroup', 'classes': classes, 'heading': heading}],
                'useDefaultSymbol': default is not None, 'isDefaultSymbolVisible': default is not None, 'polygonSymbolColorTarget': 'Fill'}
    if default is not None:
        renderer['defaultSymbol'] = symbol(default, geometry, {**where, 'class': 'default'}, notes, names)
    if expression:
        renderer['valueExpressionInfo'] = expression_info('map-style rules', expression)
    return renderer


def below(value):
    """The largest single-precision number below value, as a double."""
    import numpy as np
    return float(np.nextafter(np.float32(value), np.float32(-np.inf)))


def histogram(low, high):
    return {'type': 'StatsHistogram', 'min': float(low), 'max': float(high)}


def contiguous(spec):
    """Graduated classes that a class-breaks renderer holds exactly: ascending, touching, all shown, no default."""
    classes = spec['classes']
    return (spec.get('default') is None and all(c.get('visible', True) for c in classes)
            and all(float(a['range'][1]) == float(b['range'][0]) for a, b in zip(classes, classes[1:]))
            and all(float(c['range'][0]) < float(c['range'][1]) for c in classes))


def rule_renderer(layer, rules, names, where, notes):
    """First-match rules as a unique-value renderer whose value is an Arcade expression returning the rule's number.

    One layer keeps one legend with every rule and the map-style reading exactly: the first rule that matches draws
    the feature, a rule in a dialect Pro cannot read is skipped (it matches nothing), and the rest return "default",
    a class of its own (without a default they return null, which no class holds, and are not drawn).
    """
    lines, classes, default = [], [], None
    for index, rule in enumerate(rules):
        if rule.get('default'):
            default = rule.get('symbol')
            continue
        label = rule.get('label', str(index))
        if rule.get('filter') is None:
            condition = 'true'
        else:
            try:
                condition = arcade(rule['filter'], names)
            except Unusable as error:
                notes.append({**where, 'item': 'filter', 'rule': label, 'reason': f'{error}: the rule is skipped (it matches nothing)'})
                continue
        key = str(len(classes) + 1)
        lines.append(f'if ({condition}) {{ return "{key}" }}')
        classes.append(unique_class(label, [key], symbol(rule['symbol'], layer['geometry'], {**where, 'rule': label}, notes, names),
                                    rule.get('visible', True) is not False))
    if default is not None:
        classes.append(unique_class('other', ['default'], symbol(default, layer['geometry'], {**where, 'rule': 'default'}, notes, names)))
    tail = 'return "default"' if default is not None else 'return null'
    expression = '\n'.join(['// map-style rules: the first that matches draws the feature'] + lines + [tail])
    return unique_renderer([], classes, None, 'other', layer.get('title') or layer['id'], layer['geometry'], where, notes, expression, names)


def size_variable(spec, names):
    """map-style `size` (radius from a field, linear or sqrt) as a graduated size visual variable: Arcade gives the
    value (its square root for sqrt), min/max value and size are the domain and the diameters in points."""
    (d0, d1), (r0, r1) = spec['domain'], spec['range']
    value = arcade_field(spec['field'], names)
    if spec.get('scale') == 'sqrt':
        if d0 < 0 or d1 < 0:
            raise ValueError(f'size.domain {spec["domain"]} must not be negative for a sqrt scale')
        value, d0, d1 = f'Sqrt({value})', math.sqrt(d0), math.sqrt(d1)
    return {'type': 'CIMSizeVisualVariable',
            'authoringInfo': {'type': 'CIMVisualVariableAuthoringInfo', 'minSliderValue': d0, 'maxSliderValue': d1, 'heading': spec['field']},
            'randomMax': 1, 'minSize': 2 * pt(r0), 'maxSize': 2 * pt(r1), 'minValue': d0, 'maxValue': d1,
            'valueRepresentation': 'Radius', 'variableType': 'Graduated', 'valueShape': 'Unknown', 'axis': 'HeightAxis',
            'normalizationType': 'Nothing', 'valueExpressionInfo': expression_info('map-style size', value)}


def renderer(layer, names, notes):
    """The CIM renderer for a vector layer, and its type name."""
    where = {'layer': layer['id']}
    geometry, spec = layer['geometry'], layer['renderer']
    if spec is None:
        return {'type': 'CIMSimpleRenderer', 'patch': 'Default', 'symbol': empty_symbol(geometry)}, 'CIMSimpleRenderer'
    kind = spec['type']
    field = names.get(spec.get('field'), spec.get('field'))
    if kind == 'single':
        result = {'type': 'CIMSimpleRenderer', 'sampleSize': 10000, 'patch': 'Default', 'symbol': symbol(spec['symbol'], geometry, where, notes, names)}
    elif kind == 'categorized':
        classes = [unique_class(item.get('label', value_text(item['value'])), [value_text(item['value'])],
                                symbol(item['symbol'], geometry, {**where, 'class': value_text(item['value'])}, notes, names), item.get('visible', True) is not False)
                   for item in spec['classes']]
        if spec.get('default') is not None:  # Pro's "all other values" leaves NULL out: NULL gets the default symbol as a class of its own
            classes.append(unique_class('other (no value)', ['<Null>'], symbol(spec['default'], geometry, {**where, 'class': 'default'}, notes, names)))
        result = unique_renderer([field], classes, spec.get('default'), 'other', layer.get('title') or spec['field'], geometry, where, notes, names=names)
    elif kind == 'graduated' and contiguous(spec):
        classes = spec['classes']
        breaks = []
        for index, item in enumerate(classes):
            low, high = map(float, item['range'])
            # map-style ranges are [low, high) except the last; a CIM class holds (previous bound, upper bound], so every
            # upper bound but the last steps just below the next class's lower bound, in single precision, which is how
            # Pro compares (a double one step below still takes the bound into the lower class; checked in Pro 3.4)
            upper = below(high) if index < len(classes) - 1 else high
            breaks.append({'type': 'CIMClassBreak', 'label': item.get('label', f'{number(low)} - {number(high)}'), 'patch': 'Default',
                           'symbol': symbol(item['symbol'], geometry, {**where, 'class': f'{number(low)}-{number(high)}'}, notes, names), 'upperBound': upper})
        result = {'type': 'CIMClassBreaksRenderer', 'barrierWeight': 'High', 'classBreakType': 'GraduatedColor', 'field': field,
                  'heading': layer.get('title') or spec['field'], 'polygonSymbolColorTarget': 'Fill', 'classificationMethod': 'Manual',
                  'breaks': breaks, 'minimumBreak': float(classes[0]['range'][0]), 'showInAscendingOrder': True, 'sampleSize': 10000,
                  'normalizationType': 'Nothing', 'useExclusionSymbol': False}
    else:
        if kind == 'graduated':
            notes.append({**where, 'item': 'renderer', 'reason': 'graduated classes with gaps, hidden classes or a default symbol are written '
                                                                  'as first-match rules (a CIM class-breaks renderer has none of these)'})
        result = rule_renderer(layer, classes_as_rules(spec), names, where, notes)
    size = layer.get('size')
    if size:
        if geometry != 'point':
            notes.append({**where, 'item': 'size', 'reason': 'size by field applies to point markers only'})
        else:
            result['visualVariables'] = [size_variable(size, names)]
            for mark in (s.get('marker') or {} for s in layer_symbols(layer)):
                if '_svg' in mark and mark['_svg']['width'] > mark['_svg']['height']:
                    notes.append({**where, 'item': 'size', 'kind': 'approximated',
                                  'reason': 'a graduated size sets a picture marker\'s height: a wide SVG is drawn with its height, not its longer side, at the size'})
            if size.get('clamp', True) is False:
                notes.append({**where, 'item': 'size.clamp', 'reason': 'values outside the domain are held at the end sizes (a graduated size variable always clamps)'})
    return result, result['type']


# --- labels --------------------------------------------------------------------------------------------------------

def text_symbol(style, fonts, where, notes):
    """A CIMTextSymbol: the font is the installed family and style a theme alias and weight resolve to (fonts:
    _map_style.font_table); the halo is a solid polygon around the glyphs; `box` is a background callout."""
    family, named = resolve_font(style, fonts)
    body = {'type': 'CIMTextSymbol', 'blockProgression': 'TTB', 'depth3D': 1, 'extrapolateBaselines': True, 'fontEffects': 'Normal',
            'fontEncoding': 'Unicode', 'fontFamilyName': family or 'Arial', 'fontStyleName': named or 'Regular', 'fontType': 'Unspecified',
            'height': pt(style['size']), 'hinting': 'Default', 'horizontalAlignment': 'Center', 'kerning': True, 'letterWidth': 100,
            'ligatures': True, 'lineGapType': 'ExtraLeading', 'symbol': polygon_symbol({'color': style['color'], 'opacity': style.get('opacity', 1)}),
            'textCase': 'Normal', 'textDirection': 'LTR', 'verticalAlignment': 'Center', 'verticalGlyphOrientation': 'Right',
            'wordSpacing': 100, 'billboardMode3D': 'FaceNearPlane'}
    halo = style.get('halo')
    if halo:
        body['haloSize'] = pt(halo['width'])
        body['haloSymbol'] = polygon_symbol({'color': halo['color'], 'opacity': halo.get('opacity', 1)})
        if halo.get('blur'):
            notes.append({**where, 'item': 'text.halo.blur', 'reason': 'a CIM halo has no blur: drawn as a sharp halo'})
    if style.get('letterSpacing'):
        body['letterSpacing'] = float(style['letterSpacing']) * 100  # em -> CIM's percentage of the glyph width (approximate)
        notes.append({**where, 'item': 'text.letterSpacing', 'reason': 'letter spacing in em is written as a percentage of the glyph width (approximate)'})
    box = style.get('box')
    if box:
        top, right, bottom, left = (pt(v) for v in padding_sides(box.get('padding')))
        if (box.get('fill') or {}).get('pattern'):
            notes.append({**where, 'item': 'text.box.fill.pattern', 'reason': 'a label box takes a plain fill: the pattern is not drawn'})
        plain = {k: v for k, v in box['fill'].items() if k != 'pattern'} if box.get('fill') else None
        body['callout'] = {'type': 'CIMBackgroundCallout', 'backgroundSymbol': polygon_symbol(plain, box.get('stroke')),
                           'margin': {'type': 'CIMTextMargin', 'left': left, 'right': right, 'top': top, 'bottom': bottom}}
    return {'type': 'CIMSymbolReference', 'symbol': body}


def maplex(geometry, **changes):
    """Maplex placement properties as Pro writes them by default, with changes."""
    zones = {'type': 'CIMMaplexExternalZonePriorities', 'aboveLeft': 4, 'aboveCenter': 2, 'aboveRight': 1, 'centerRight': 3,
             'belowRight': 5, 'belowCenter': 7, 'belowLeft': 8, 'centerLeft': 6}
    props = {'type': 'CIMMaplexLabelPlacementProperties', 'featureType': {'point': 'Point', 'line': 'Line', 'polygon': 'Polygon'}[geometry],
             'avoidPolygonHoles': True, 'canOverrunFeature': True, 'canPlaceLabelOutsidePolygon': True, 'canRemoveOverlappingLabel': True,
             'canStackLabel': True, 'centerLabelAnchorType': 'Symbol', 'connectionType': 'Unambiguous', 'constrainOffset': 'NoConstraint',
             'contourAlignmentType': 'Page', 'contourLadderType': 'Straight', 'contourMaximumAngle': 90, 'enableConnection': True,
             'featureWeight': 0, 'fontHeightReductionLimit': 4, 'fontHeightReductionStep': 0.5, 'fontWidthReductionLimit': 90,
             'fontWidthReductionStep': 5, 'graticuleAlignmentType': 'Straight', 'keyNumberGroupName': 'Default', 'labelBuffer': 15,
             'labelLargestPolygon': True, 'labelPriority': -1,
             'labelStackingProperties': {'type': 'CIMMaplexLabelStackingProperties', 'stackAlignment': 'ChooseBest', 'maximumNumberOfLines': 3,
                                         'minimumNumberOfCharsPerLine': 3, 'maximumNumberOfCharsPerLine': 24,
                                         'separators': [{'type': 'CIMMaplexStackingSeparator', 'separator': ' ', 'splitAfter': True},
                                                        {'type': 'CIMMaplexStackingSeparator', 'separator': ',', 'visible': True, 'splitAfter': True}],
                                         'trimStackingSeparators': True},
             'lineFeatureType': 'General', 'linePlacementMethod': 'OffsetCurvedFromLine', 'maximumLabelOverrun': 80,
             'maximumLabelOverrunUnit': 'Point', 'measureFromClippedFeatureGeometry': True, 'minimumFeatureSizeUnit': 'Map',
             'multiPartOption': 'OneLabelPerPart',
             'offsetAlongLineProperties': {'type': 'CIMMaplexOffsetAlongLineProperties', 'placementMethod': 'BestPositionAlongLine',
                                           'labelAnchorPoint': 'CenterOfLabel', 'distanceUnit': 'Percentage', 'useLineDirection': True},
             'pointExternalZonePriorities': dict(zones), 'pointPlacementMethod': 'AroundPoint', 'polygonAnchorPointType': 'GeometricCenter',
             'polygonBoundaryWeight': 0, 'polygonExternalZones': dict(zones), 'polygonFeatureType': 'General',
             'polygonInternalZones': {'type': 'CIMMaplexInternalZonePriorities', 'center': 1}, 'polygonPlacementMethod': 'HorizontalInPolygon',
             'primaryOffset': 1, 'primaryOffsetUnit': 'Point', 'removeAmbiguousLabels': 'All', 'removeExtraWhiteSpace': True,
             'repetitionIntervalUnit': 'Point',
             'rotationProperties': {'type': 'CIMMaplexRotationProperties', 'rotationType': 'Arithmetic', 'alignmentType': 'Straight'},
             'secondaryOffset': 100, 'secondaryOffsetUnit': 'Percentage',
             'strategyPriorities': {'type': 'CIMMaplexStrategyPriorities', 'stacking': 1, 'overrun': 2, 'fontCompression': 3,
                                    'fontReduction': 4, 'abbreviation': 5},
             'thinningDistanceUnit': 'Point', 'truncationMarkerCharacter': '.', 'truncationMinimumLength': 1,
             'truncationPreferredCharacters': 'aeiou', 'truncationExcludedCharacters': '0123456789', 'polygonAnchorPointPerimeterInsetUnit': 'Point'}
    props.update(changes)
    return props


def standard(geometry, **changes):
    """Standard label engine properties as Pro writes them by default, with changes."""
    props = {'type': 'CIMStandardLabelPlacementProperties', 'featureType': {'point': 'Point', 'line': 'Line', 'polygon': 'Polygon'}[geometry],
             'featureWeight': 'None', 'labelWeight': 'High', 'numLabelsOption': 'OneLabelPerName',
             'lineLabelPosition': {'type': 'CIMStandardLineLabelPosition', 'above': True, 'inLine': True, 'parallel': True},
             'lineLabelPriorities': {'type': 'CIMStandardLineLabelPriorities', **{k: 3 for k in (
                 'aboveStart', 'aboveAlong', 'aboveEnd', 'centerStart', 'centerAlong', 'centerEnd', 'belowStart', 'belowAlong', 'belowEnd')}},
             'pointPlacementMethod': 'AroundPoint',
             'pointPlacementPriorities': {'type': 'CIMStandardPointPlacementPriorities', 'aboveLeft': 2, 'aboveCenter': 2, 'aboveRight': 1,
                                          'centerLeft': 3, 'centerRight': 2, 'belowLeft': 3, 'belowCenter': 3, 'belowRight': 2},
             'rotationType': 'Arithmetic', 'polygonPlacementMethod': 'AlwaysHorizontal'}
    props.update(changes)
    return props


def label_class(name, expression, text, geometry, maplex_props, standard_props, where_clause=None, priority=-1):
    cls = {'type': 'CIMLabelClass', 'expressionTitle': 'map-style', 'expression': expression, 'expressionEngine': 'Arcade',
           'featuresToLabel': 'AllVisibleFeatures', 'maplexLabelPlacementProperties': maplex_props, 'name': name, 'priority': priority,
           'standardLabelPlacementProperties': standard_props, 'textSymbol': text, 'useCodedValue': True, 'visibility': True, 'iD': -1}
    if where_clause:
        cls['whereClause'] = where_clause
    return cls


def layer_labels(spec, geometry, names, fonts, where, notes):
    """A layer's own labels (placed by Pro) as one label class."""
    expression = template_arcade(spec['template'], names) if spec.get('template') else arcade_field(spec['field'], names)
    placement = spec.get('placement', 'auto')
    if geometry == 'point':
        m = maplex(geometry, pointPlacementMethod='CenteredOnPoint') if placement == 'point' else maplex(
            geometry, pointPlacementMethod='AroundPoint', primaryOffset=label_gap(spec['text']) * PT_PER_PX)
        s = standard(geometry, pointPlacementMethod='OnTopPoint' if placement == 'point' else 'AroundPoint')
    elif geometry == 'line':
        # Pro draws no background box for curved labels, so labels with a box stay straight along the line
        curved = placement == 'line' and not spec['text'].get('box')
        m = maplex(geometry, linePlacementMethod='CenteredCurvedOnLine' if curved else 'CenteredStraightOnLine')
        s = standard(geometry, lineLabelPosition={'type': 'CIMStandardLineLabelPosition', 'inLine': True, 'onTop': True, 'parallel': not curved,
                                                  'produceCurvedLabels': curved})
    else:
        inside = placement == 'polygon-center'
        m = maplex(geometry, canPlaceLabelOutsidePolygon=not inside)
        s = standard(geometry, placeOnlyInsidePolygon=inside)
    return label_class('map-style labels', expression, text_symbol(spec['text'], fonts, where, notes), geometry, m, s)


def placement_classes(groups, fonts, names, notes):
    """Labels exactly where the map-style put them: one label class per (text style, anchor, rotated or not) in use,
    selected by the style_id, anchor and angle fields, at the point with no offset and never removed.

    Unrotated labels take Maplex's fixed position for their anchor. Rotated ones turn by the `angle` field
    (counter-clockwise, the map-style rotation negated); with rotation by field Maplex ignores fixed positions and
    starts every label at its point along the angle, which is the `left` anchor (`center` stays centred), so other
    anchors are approximated and reported. A text symbol's own angle is ignored by Maplex. (Checked in ArcGIS Pro 3.4.)
    groups: (style_id, text style, anchor, rotated).
    """
    classes = []
    angle = names.get('angle', 'angle')
    for (index, style, anchor, rotated) in groups:
        where_clause = (f'{sql_field(names.get("style_id", "style_id"))} = {index} AND {sql_field(names.get("anchor", "anchor"))} = {sql_literal(anchor)}'
                        f' AND {sql_field(angle)} {"<>" if rotated else "="} 0')
        method = ANCHOR_MAPLEX[anchor]
        changes = {}
        if rotated:
            method = 'CenteredOnPoint' if anchor == 'center' else 'EastOfPoint'
            changes['rotationProperties'] = {'type': 'CIMMaplexRotationProperties', 'enable': True, 'rotationField': angle,
                                             'rotationType': 'Arithmetic', 'alignmentType': 'Straight'}
            if anchor not in ('center', 'left'):
                notes.append({'layer': 'labelPlacements', 'style': index, 'item': 'anchor',
                              'reason': f'rotated labels with anchor {anchor} start at their point along the angle (as anchor left): '
                                        'Maplex keeps no fixed position for labels rotated by a field'})
        m = maplex('point', pointPlacementMethod=method, primaryOffset=0, neverRemoveLabel=True, canRemoveOverlappingLabel=False,
                   canStackLabel=False, canReduceFontSize=False, canAbbreviateLabel=False, canTruncateLabel=False,
                   labelBuffer=0, featureWeight=0, enablePointPlacementPriorities=False, **changes)
        extra = {'rotationField': angle} if rotated else {}
        if anchor == 'center':
            s = standard('point', pointPlacementMethod='OnTopPoint', allowOverlappingLabels=True, **extra)
        else:
            priorities = {'type': 'CIMStandardPointPlacementPriorities', **{k: 0 for k in ANCHOR_STANDARD.values()}, ANCHOR_STANDARD[anchor]: 1}
            s = standard('point', pointPlacementPriorities=priorities, allowOverlappingLabels=True, **extra)
        text = text_symbol(style, fonts, {'layer': 'labelPlacements', 'style': index}, notes)
        classes.append(label_class(f'style {index} {anchor}' + (' rotated' if rotated else ''), arcade_field('text', names), text, 'point',
                                   m, s, where_clause, priority=1))
    return classes


# --- rasters -------------------------------------------------------------------------------------------------------

def linear_ramp(start, end):
    return {'type': 'CIMLinearContinuousColorRamp', 'colorSpace': COLOR_SPACE, 'fromColor': rgb(start), 'toColor': rgb(end)}


def colorizer(spec, stats, notes, layer):
    """The CIM colorizer for a raster layer. stats: {band: (min, max)} of the packed raster, for rgb and open classes."""
    kind = spec['type']
    where = {'layer': layer}
    nodata = rgb(spec['nodata']['color']) if (spec.get('nodata') or {}).get('color') else {'type': 'CIMRGBColor', 'values': [255, 255, 255, 0]}
    base = {'resamplingType': 'NearestNeighbor', 'noDataColor': nodata}
    if kind == 'pseudocolor':
        band = spec.get('band', 1)
        stops = sorted(spec['stops'], key=lambda s: s['value'])
        mode = spec.get('interpolation', 'linear')
        if (spec.get('stretch') or {}).get('method') not in (None, 'manual'):
            notes.append({**where, 'item': 'renderer.stretch', 'reason': f'stretch {spec["stretch"]["method"]} is not recomputed; stop values are used as data values'})
        if mode == 'linear':
            lo, hi = float(stops[0]['value']), float(stops[-1]['value'])
            parts = [linear_ramp(a['color'], b['color']) for a, b in zip(stops, stops[1:])] or [linear_ramp(stops[0]['color'], stops[0]['color'])]
            weights = [float(b['value']) - float(a['value']) for a, b in zip(stops, stops[1:])] or [1.0]
            total = sum(weights) or 1.0
            ramp = {'type': 'CIMMultipartColorRamp', 'colorSpace': COLOR_SPACE, 'colorRamps': parts, 'weights': [w / total for w in weights]}
            return {'type': 'CIMRasterStretchColorizer', **base, 'backgroundColor': {'type': 'CIMRGBColor', 'values': [255, 255, 255, 0]},
                    'bandIndex': band - 1, 'colorRamp': ramp, 'customStretchMin': lo, 'customStretchMax': hi, 'useCustomStretchMinMax': True,
                    'gammaValue': 1, 'maxPercent': 0.5, 'minPercent': 0.5, 'standardDeviationParam': 2,
                    # Pro stretches by the statistics it is given; the custom min/max alone are ignored (checked in Pro 3.4)
                    'statsType': 'GlobalStats', 'stretchStats': histogram(lo, hi), 'stretchType': 'MinimumMaximum',
                    'stretchClasses': [{'type': 'CIMRasterStretchClass', 'label': stops[0].get('label', number(lo)), 'value': lo},
                                       {'type': 'CIMRasterStretchClass', 'label': stops[-1].get('label', number(hi)), 'value': hi}]}, 'CIMRasterStretchColorizer'
        if mode == 'discrete':
            # a stop's colour holds from its value up to the next stop, and below the first stop too; a CIM class holds
            # up to and including its upper bound, so each bound sits just below the next stop and the last is open
            high = max([float(stops[-1]['value'])] + [v[1] for v in stats.values()])
            breaks = []
            for index, stop in enumerate(stops):
                upper = below(stops[index + 1]['value']) if index + 1 < len(stops) else max(high, float(stop['value']))
                breaks.append({'type': 'CIMRasterClassBreak', 'upperBound': upper, 'label': stop.get('label', number(stop['value'])), 'color': rgb(stop['color'])})
            if band != 1:
                notes.append({**where, 'item': 'renderer.band', 'reason': 'classified colours use the first band'})
            return {'type': 'CIMRasterClassifyColorizer', **base, 'normalizationType': 'Nothing', 'classBreaks': breaks,
                    'classificationMethod': 'Manual', 'field': 'Value', 'showInAscendingOrder': True}, 'CIMRasterClassifyColorizer'
        classes = [{'value': s['value'], 'color': s['color'], 'label': s.get('label')} for s in stops]
        notes.append({**where, 'item': 'renderer.interpolation', 'reason': 'exact colours are written as unique values (integer rasters only)'})
    else:
        if kind == 'rgb':
            red, green, blue = spec['bands']
            result = {'type': 'CIMRasterRGBColorizer', **base, 'redBandIndex': red - 1, 'greenBandIndex': green - 1, 'blueBandIndex': blue - 1,
                      'stretchType': 'MinimumMaximum', 'useCustomStretchMinMax': True, 'stretchStatsType': 'GlobalStats', 'gammaValue': 1,
                      'maxPercent': 0.5, 'minPercent': 0.5, 'standardDeviationParam': 2,
                      'backgroundColor': {'type': 'CIMRGBColor', 'values': [0, 0, 0, 0]}, 'displayBackgroundValue': False}
            for name, band in (('Red', red), ('Green', green), ('Blue', blue)):
                low, high = stats.get(band, (0, 255))
                result[f'customStretchMin{name}'], result[f'customStretchMax{name}'] = low, high
                result[f'stretchStats{name}'] = histogram(low, high)
            return result, 'CIMRasterRGBColorizer'
        classes = spec['classes']
        if spec.get('band', 1) != 1:
            notes.append({**where, 'item': 'renderer.band', 'reason': 'unique-value colours use the first band'})
    group = {'type': 'CIMRasterUniqueValueGroup', 'heading': 'Value',
             'classes': [{'type': 'CIMRasterUniqueValueClass', 'values': [number(c['value'])], 'label': c.get('label') or number(c['value']),
                          'color': rgb(c['color']), 'editable': True, 'visible': True} for c in classes]}
    return {'type': 'CIMRasterUniqueValueColorizer', **base, 'fieldName': 'Value', 'groups': [group], 'useDefaultColor': False}, 'CIMRasterUniqueValueColorizer'


# --- layers --------------------------------------------------------------------------------------------------------

def connection(workspace, dataset, raster=False):
    """A data connection relative to the layer file: workspace like '..\\commondata\\map.gdb'."""
    return {'type': 'CIMStandardDataConnection', 'workspaceConnectionString': f'DATABASE={workspace}',
            'workspaceFactory': 'Raster' if raster else 'FileGDB', 'dataset': dataset,
            'datasetType': 'esriDTAny' if raster else 'esriDTFeatureClass'}


def layer_base(kind, name, uri, visible, opacity=1.0, description=''):
    return {'type': kind, 'name': name, 'uRI': uri, 'sourceModifiedTime': {'type': 'TimeInstant'}, 'useSourceMetadata': True,
            'description': description or name, 'expanded': kind == 'CIMGroupLayer', 'layerType': 'Operational', 'showLegends': True,
            'transparency': round(100 * (1 - float(opacity)), 4), 'visibility': visible, 'displayCacheType': 'Permanent',
            'maxDisplayCacheAge': 5, 'showPopups': True, 'serviceLayerID': -1, 'refreshRate': -1,
            'refreshRateUnit': 'esriTimeUnitsSeconds', 'blendingMode': 'Alpha'}


def feature_body(name, uri, data, query, render, visible=True, opacity=1.0, description='', display_field=None):
    body = layer_base('CIMFeatureLayer', name, uri, visible, opacity, description)
    table = {'type': 'CIMFeatureTable', 'editable': True, 'dataConnection': data, 'studyAreaSpatialRel': 'esriSpatialRelUndefined',
             'searchOrder': 'esriSearchOrderSpatial'}
    if display_field:
        table['displayField'] = display_field
    if query:
        table.update(definitionExpression=query, definitionExpressionName='map-style filter',
                     definitionFilterChoices=[{'type': 'CIMDefinitionFilter', 'name': 'map-style filter', 'definitionExpression': query}])
    body.update(autoGenerateFeatureTemplates=True, featureTable=table, htmlPopupEnabled=True, selectable=True, featureCacheType='Session',
                displayFiltersType='ByScale', featureBlendingMode='Alpha', layerEffectsMode='Layer', renderer=render, scaleSymbols=True,
                snappable=True)
    return body


def feature_layer(name, uri, layer, data, names, fonts, notes, display_field=None):
    """A CIMFeatureLayer for a map-style vector layer; the filter is its definition query."""
    where = {'layer': layer['id']}
    query = definition_query(layer.get('filter'), where, notes, names)
    render, kind = renderer(layer, names, notes)
    body = feature_body(name, uri, data, query, render, layer.get('visible') is not False, layer.get('opacity', 1),
                        f'map-style layer {layer["id"]}', display_field)
    labels = layer.get('labels')
    if labels:
        body['labelClasses'] = [layer_labels(labels, layer['geometry'], names, fonts, where, notes)]
        body['labelVisibility'] = labels.get('enabled', True) is not False
    elif query and layer.get('renderer') is None:
        notes.append({**where, 'item': 'filter', 'reason': 'a layer that draws nothing and has no labels: the filter has no effect'})
    order = layer.get('sortBy')
    if order:
        body['featureSortInfos'] = [{'type': 'CIMFeatureSortInfo', 'fieldName': names.get(order['field'], order['field']),
                                     'sortDirection': 'Descending' if order.get('order') == 'desc' else 'Ascending'}]
    return body, kind


def placement_layer(name, uri, data, geometry, query, label_classes=None, render=None):
    """A layer of the label placement tables: labels that draw no symbol, or leader lines."""
    body = feature_body(name, uri, data, query, render or {'type': 'CIMSimpleRenderer', 'patch': 'Default', 'symbol': empty_symbol(geometry)},
                        description='labels placed by the map-style producer' if label_classes else 'label leader lines')
    if label_classes:
        body['labelClasses'] = label_classes
        body['labelVisibility'] = True
    return body


def raster_layer(name, uri, layer, data, stats, notes):
    body = layer_base('CIMRasterLayer', name, uri, layer.get('visible') is not False, layer.get('opacity', 1), f'map-style layer {layer["id"]}')
    body['dataConnection'] = data
    body['colorizer'], kind = colorizer(layer['renderer'], stats, notes, layer['id'])
    return body, kind


def group_layer(name, uri, children, visible=True):
    body = layer_base('CIMGroupLayer', name, uri, visible)
    body.update(sublayerVisibilityMode='Independent', layers=children)
    return body


def document(top, definitions):
    """The .lyrx: top-level layer URIs (top first) and every layer's definition."""
    return {'type': 'CIMLayerDocument', 'version': CIM_VERSION, 'build': CIM_BUILD, 'layers': top, 'layerDefinitions': definitions}


# --- package information -------------------------------------------------------------------------------------------

def xml_text(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def pkinfo(item_id, name):
    """esriinfo/item.pkinfo, as Pro writes it for a layer package."""
    return ('<?xml version="1.0" encoding="utf-8" ?>\n'
            "<pkinfo Culture='en-US'>\n"
            f'<ID>{item_id}</ID>\n<name>{xml_text(name)}</name>\n<version>13.0</version>\n<size>-1</size>\n<created></created>\n'
            '<type>Layer Package</type>\n<servable>false</servable>\n<packagelocation></packagelocation>\n<pkinfolocation></pkinfolocation>\n'
            '</pkinfo>\n')


def iteminfo(item_id, name, title, summary, tags, bbox, crs_name):
    """esriinfo/iteminfo.xml: the item description ArcGIS shows for the package; bbox in degrees."""
    extent = ''.join(f'<{k}>{v!r}</{k}>\n' for k, v in zip(('xmin', 'ymin', 'xmax', 'ymax'), bbox)) if bbox else ''
    keywords = ''.join(f'<typekeyword>{k}</typekeyword>\n' for k in ('Layer Package', 'Data', 'ArcGIS Pro', 'lpkx'))
    return ('<?xml version="1.0" encoding="utf-8" ?>\n'
            "<ESRI_ItemInformation Culture='en-US'>\n"
            f'<name>{xml_text(name)}</name>\n<guid>{item_id}</guid>\n<version>1.0</version>\n<created></created>\n<modified></modified>\n'
            f'<catalogpath></catalogpath>\n<snippet>{xml_text(summary)}</snippet>\n<description>{xml_text(summary)}</description>\n'
            f'<summary>{xml_text(summary)}</summary>\n<title>{xml_text(title)}</title>\n<tags>{xml_text(", ".join(tags))}</tags>\n'
            f'<type>Layer Package</type>\n<typekeywords>\n{keywords}</typekeywords>\n<documentation></documentation>\n<url></url>\n'
            f'<extent>\n{extent}</extent>\n<spatialreference>{xml_text(crs_name or "")}</spatialreference>\n'
            '<accessinformation></accessinformation>\n<licenseinfo></licenseinfo>\n</ESRI_ItemInformation>\n')
