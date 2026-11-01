"""QGIS layer style (QML) -> map-style v1: the reader for `package.py read`, see _style_read.py for how readers fit.

Reads what QGIS itself saves and what `_qgis_style.py` writes. QML conventions: symbol layers listed bottom first,
sizes with a unit next to them (default mm), colours "r,g,b,a", angles clockwise, a marker's 0 up except on a marker
line, where +x runs along the line. A QML names no data, so data.path is a placeholder unless --data gives it.
"""
from __future__ import annotations

import base64
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from _map_style import WEIGHT_STYLES, svg_size
from _style_read import PX_PER_MM, PX_PER_PT, circle_segments, hex_color, line, paint, parse_filter, polyline_segments, rules_renderer, shape_ops, tidy, transform_op

UNITS = {'MM': PX_PER_MM, 'Pixel': 1.0, 'Point': PX_PER_PT, 'Inch': 96.0}
CAPS = {'flat': 'butt', 'round': 'round', 'square': 'square'}
JOINS = {'miter': 'miter', 'round': 'round', 'bevel': 'bevel'}
QT_DASHES = {'dash': [4, 2], 'dot': [1, 2], 'dash dot': [4, 2, 1, 2], 'dash dot dot': [4, 2, 1, 2, 1, 2]}
BRUSHES = {'b_diagonal': ('hatch', 45), 'f_diagonal': ('hatch', 135), 'horizontal': ('hatch', 0), 'vertical': ('hatch', 90),
           'cross': ('crossHatch', 0), 'diagonal_x': ('crossHatch', 45)}
GEOMETRY_NUMBERS = {'0': 'point', '1': 'line', '2': 'polygon'}
SYMBOL_GEOMETRY = {'marker': 'point', 'line': 'line', 'fill': 'polygon'}
SHAPES = {'circle': 'circle', 'square': 'square', 'diamond': 'diamond', 'triangle': 'triangle', 'cross': 'cross', 'cross2': 'x'}
STYLE_WEIGHTS = {name.lower(): weight for weight, name in WEIGHT_STYLES.items() if name}
STYLE_WEIGHTS.update({'regular': 400, 'normal': 400, 'book': 400, 'semibold': 600, 'demibold': 600, 'heavy': 900})
# SimpleMarker names map-style cannot name, drawn as an image: regular polygons by their corners (see named_outline)
REGULAR = {'pentagon': 5, 'hexagon': 6, 'octagon': 8, 'decagon': 10, 'equilateral_triangle': 3}


def color_of(text):
    """(#RRGGBB, opacity) from "r,g,b,a", newer QGIS's "r,g,b,a,rgb:..." or "#rrggbb"."""
    if not text:
        return '#000000', 0.0
    if text.startswith('#'):
        return text[:7].upper(), 1.0
    parts = text.split(',')
    try:
        red, green, blue = (float(v) for v in parts[:3])
        alpha = float(parts[3]) if len(parts) > 3 and ':' not in parts[3] else 255.0
    except ValueError:
        return '#000000', 0.0
    return hex_color(red, green, blue), round(alpha / 255, 4)


def paint_of(text):
    value, opacity = color_of(text)
    return paint(value, opacity) if opacity > 0 else None


def properties_of(node):
    """A symbol layer's properties, from <Option> maps (QGIS 3) and the <prop> rows older files hold."""
    out = {prop.get('k'): prop.get('v') for prop in node.findall('prop')}
    for holder in node.findall('Option'):
        if holder.get('type') == 'Map':
            for child in holder.findall('Option'):
                if child.get('name') and child.get('value') is not None:
                    out[child.get('name')] = child.get('value')
    return out


def active_definitions(node, tag='data_defined_properties'):
    """{property: its settings} for the data-defined properties that are on."""
    holder = node.find(tag)
    out = {}
    if holder is None:
        return out
    for prop in holder.iterfind('.//Option[@name="properties"]/Option'):
        values = {child.get('name'): child.get('value') for child in prop.findall('Option')}
        if values.get('active') == 'true':
            out[prop.get('name')] = values
    return out


class Style:
    """One layer being read."""

    def __init__(self, reading, layer_id):
        self.reading, self.layer_id = reading, layer_id
        self.size = None

    def px(self, value, unit, where, prop):
        try:
            number = float(value or 0)
        except ValueError:
            return 0.0
        factor = UNITS.get(unit or 'MM')
        if factor is None:
            if number:
                self.reading.note('approximated', 'map-units', f'{prop} is sized in {unit}, which map-style does not have: taken as px', where, prop)
            return number
        return number * factor

    # --- symbols -----------------------------------------------------------------------------------------------

    def symbol_parts(self, node, geometry, where, along=False):
        """The parts of a <symbol>, bottom first."""
        parts = []
        for layer in node.findall('layer'):
            if layer.get('enabled', '1') == '0':
                continue
            cls = layer.get('class')
            props = properties_of(layer)
            definitions = active_definitions(layer)
            effect = layer.find('effect')
            if effect is not None and effect.get('enabled') == '1':
                self.reading.note('untranslated', 'paint-effect', f'the paint effect on the {cls} symbol layer is not carried', where, 'symbol')
            handler = LAYERS.get(cls)
            if handler is None:
                self.reading.note('untranslated', 'symbol-layer', f'symbol layer {cls} has no map-style form: left out', where, 'symbol')
                continue
            parts += handler(self, layer, props, definitions, geometry, where, along)
        alpha = float(node.get('alpha', 1) or 1)
        return [fade(part, alpha, self.reading, where) for part in parts] if alpha < 1 else parts

    def unknown_definitions(self, definitions, handled, where):
        for name in sorted(set(definitions) - set(handled)):
            self.reading.note('untranslated', 'data-defined', f'data-defined {name} is not carried: the static value is used', where, f'symbol.{name}')

    def simple_fill(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, (), where)
        parts = []
        style = props.get('style', 'solid')
        value, opacity = color_of(props.get('color'))
        if style == 'solid':
            if opacity > 0:
                parts.append({'kind': 'fill', 'color': value, 'opacity': opacity})
        elif style in BRUSHES:
            kind, angle = BRUSHES[style]
            self.reading.note('approximated', 'brush-hatch', f'the QGIS brush style {style} has no size of its own: written as lines 1 px wide, 8 px apart', where, 'symbol.fill.pattern')
            parts.append({'kind': 'pattern', 'pattern': {'kind': kind, 'angle': angle, 'spacing': 8, 'width': 1, **paint(value, opacity)}})
        elif style != 'no':
            self.reading.note('approximated', 'brush-hatch', f'the QGIS brush style {style} is written as a solid fill', where, 'symbol.fill')
            parts.append({'kind': 'fill', 'color': value, 'opacity': opacity * 0.5})
        if props.get('offset') not in (None, '0,0'):
            self.reading.note('approximated', 'offset', 'a fill offset is not carried', where, 'symbol.fill')
        outline = props.get('outline_style', 'solid')
        if outline != 'no':
            border, alpha = color_of(props.get('outline_color'))
            if alpha > 0:
                parts.append({'kind': 'stroke', 'stroke': self.stroke_of(border, alpha, props.get('outline_width', 0.26), props.get('outline_width_unit'), outline,
                                                                         None, None, props.get('joinstyle'), None, where)})
        return parts

    def stroke_of(self, value, alpha, width, unit, style, dash, dash_unit, join, cap, where):
        width_px = self.px(width, unit, where, 'symbol.stroke.width')
        pattern = None
        if dash:
            pattern = [self.px(v, dash_unit, where, 'symbol.stroke.dash') for v in dash]
        elif style in QT_DASHES:
            pattern = [v * max(width_px, 1) for v in QT_DASHES[style]]
            self.reading.note('approximated', 'named-dash', f'the {style} line style is written as the dash pattern Qt draws at this width', where, 'symbol.stroke.dash')
        return line(value, width_px, alpha, pattern, CAPS.get(cap, 'butt'), JOINS.get(join, 'miter'))

    def simple_line(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, (), where)
        style = props.get('line_style', 'solid')
        value, alpha = color_of(props.get('line_color') or props.get('color'))
        if style == 'no' or alpha == 0:
            return []
        for key in ('offset',):
            if props.get(key) not in (None, '0', '0.0'):
                self.reading.note('approximated', 'offset', 'a line offset is not carried: the line is drawn on the feature', where, 'symbol.stroke')
        dash = None
        if props.get('use_custom_dash') == '1' and props.get('customdash'):
            dash = re.split(r'[;,]', props['customdash'])
        return [{'kind': 'stroke', 'stroke': self.stroke_of(value, alpha, props.get('line_width', 0.26), props.get('line_width_unit'), style, dash,
                                                            props.get('customdash_unit'), props.get('joinstyle'), props.get('capstyle'), where)}]

    def rotation_field(self, definitions, where):
        """`rotationField` from a data-defined angle: a plain field, or the expression the writer makes of one."""
        spec = definitions.get('angle')
        if not spec:
            return None
        if spec.get('field') and spec.get('type') == '2':
            return spec['field']
        found = re.search(r'to_real\("((?:[^"]|"")+)"\)', spec.get('expression') or '')
        if found:
            return found[1].replace('""', '"')
        self.reading.note('untranslated', 'data-defined', f'data-defined angle {spec.get("expression")!r} is not a field: not carried', where, 'symbol.marker.rotation')
        return None

    def read_size(self, definitions, geometry, where):
        spec = definitions.get('size')
        if not spec:
            return
        found = SIZE_EXPRESSION.search(spec.get('expression') or '')
        if found and geometry == 'point':
            r0, delta, sqrt, name, d0, span = found.groups()
            low, high = float(d0), float(d0) + float(span)
            if sqrt:
                low, high = low ** 2, high ** 2
            self.size = {'field': name.replace('""', '"'), 'scale': 'sqrt' if sqrt else 'linear', 'domain': [tidy(low), tidy(high)],
                         'range': [tidy(float(r0)), tidy(float(r0) + float(delta))]}
            if 'max(0, min(1,' not in spec['expression']:
                self.size['clamp'] = False
        else:
            self.reading.note('untranslated', 'data-defined', 'data-defined marker size is not carried: the static size is used', where, 'size')

    def offset_of(self, props, where):
        parts = (props.get('offset') or '0,0').split(',')
        try:
            x, y = float(parts[0]), float(parts[1]) if len(parts) > 1 else 0.0
        except ValueError:
            return 0.0, 0.0
        return self.px(x, props.get('offset_unit'), where, 'symbol.marker.offset'), self.px(y, props.get('offset_unit'), where, 'symbol.marker.offset')

    def simple_marker(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, ('angle', 'size'), where)
        self.read_size(definitions, geometry, where)
        name = props.get('name', 'circle')
        size = self.px(props.get('size', 2), props.get('size_unit'), where, 'symbol.marker.size')
        fill = paint_of(props.get('color'))
        stroke = None
        if props.get('outline_style', 'solid') != 'no':
            border, alpha = color_of(props.get('outline_color'))
            if alpha > 0:
                style = props.get('outline_style', 'solid')
                if style != 'solid':
                    self.reading.note('approximated', 'named-dash', f'a {style} marker outline is written solid', where, 'symbol.marker')
                stroke = line(border, self.px(props.get('outline_width', 0), props.get('outline_width_unit'), where, 'symbol.marker.stroke'), alpha,
                              None, CAPS.get(props.get('cap_style'), 'butt'), JOINS.get(props.get('joinstyle'), 'miter'))
        angle = float(props.get('angle', 0) or 0)
        x, y = self.offset_of(props, where)
        shape, turn = SHAPES.get(name), 0.0
        if name == 'circle' and fill is None and stroke is not None:
            shape = 'ring'
        elif name == 'cross_fill':
            shape, turn = ('x', -45.0) if abs((angle % 90) - 45) < 1e-6 else ('cross', 0.0)
        elif name == 'arrowhead':
            shape, turn = 'chevron', 90.0
            if abs(math.hypot(x, y) - size / 4) > 1e-3:
                self.reading.note('approximated', 'marker-shape', 'a QGIS arrowhead has its tip on the point: map-style centres its chevron', where, 'symbol.marker')
            x, y = 0.0, 0.0
        if shape in ('cross', 'x', 'chevron') and name != 'cross_fill':
            fill = None
        rotation = angle + turn
        if shape is not None:
            ops = [transform_op(op, turn=-rotation, shift=(x, -y), scale_lines=False) for op in shape_ops(shape, size, fill, stroke)]
            marker = None if x or y else {'shape': shape, 'size': tidy(size), 'rotation': rotation - (90 if along else 0)}
            return [{'kind': 'marker', 'marker': marker, 'fill': fill, 'stroke': stroke, 'ops': ops, 'rotationField': self.rotation_field(definitions, where)}]
        segments = named_outline(name, size)
        if segments is None:
            self.reading.note('approximated', 'marker-shape', f'the QGIS marker shape {name} is drawn as a circle', where, 'symbol.marker')
            segments = circle_segments(size / 2)
        ops = [transform_op({'d': segments, 'fill': fill, 'stroke': stroke}, turn=-angle, shift=(x, -y), scale_lines=False)]
        return [{'kind': 'marker', 'marker': None, 'ops': ops, 'rotationField': self.rotation_field(definitions, where)}]

    def svg_data(self, name, where):
        """The bytes of an SVG marker's image: inside the style (base64:), or a file beside the style, never anywhere else."""
        if name.startswith('base64:'):
            try:
                return base64.b64decode(name[len('base64:'):])
            except ValueError:
                return None
        folder = self.reading.source.parent.resolve()
        target = (folder / name).resolve()
        if target.suffix.lower() == '.svg' and target.is_relative_to(folder) and target.is_file():
            return target.read_bytes()
        self.reading.note('untranslated', 'svg-not-found', f'the SVG {Path(name).name} is not inside the style or beside it (a QGIS built-in image, or a file elsewhere): a circle stands in', where, 'symbol.marker.svg')
        return None

    def svg_marker(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, ('angle', 'size'), where)
        self.read_size(definitions, geometry, where)
        size = self.px(props.get('size', 2), props.get('size_unit'), where, 'symbol.marker.size')
        angle = float(props.get('angle', 0) or 0)
        x, y = self.offset_of(props, where)
        field = self.rotation_field(definitions, where)
        data = self.svg_data(props.get('name') or '', where)
        fill = paint_of(props.get('color'))
        if data is not None:
            data = fill_parameters(data, props)
            try:
                width, height = svg_size(data.decode('utf-8'), 'SVG marker')
            except (ValueError, UnicodeDecodeError) as error:
                self.reading.note('untranslated', 'svg-not-found', f'the SVG cannot be read ({error}): a circle stands in', where, 'symbol.marker.svg')
            else:
                share = min(1.0, width / height)  # QGIS's size is the width, map-style's the longer side
                side = size / share
                marker = {'shape': 'svg', 'size': tidy(side), 'svg': self.reading.store_svg(data), 'rotation': angle - (90 if along else 0)}
                if x or y:
                    self.reading.note('approximated', 'offset', 'an SVG marker offset is not carried', where, 'symbol.marker')
                image = {'image': 'data:image/svg+xml;base64,' + base64.b64encode(data).decode('ascii'), 'x': x, 'y': -y, 'width': side * width / max(width, height),
                         'height': side * height / max(width, height), 'turn': -angle}
                return [{'kind': 'marker', 'marker': marker, 'ops': [image], 'rotationField': field}]
        ops = [transform_op(op, turn=-angle, shift=(x, -y), scale_lines=False) for op in shape_ops('circle', size, fill or paint('#808080'), None)]
        return [{'kind': 'marker', 'marker': {'shape': 'circle', 'size': tidy(size)}, 'fill': fill or paint('#808080'), 'ops': ops, 'rotationField': field}]

    def font_marker(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, ('angle', 'size'), where)
        size = self.px(props.get('size', 2), props.get('size_unit'), where, 'symbol.marker.size')
        x, y = self.offset_of(props, where)
        fill = paint_of(props.get('color')) or paint('#000000')
        ops = self.reading.text_ops(props.get('chr') or '?', props.get('font'), props.get('font_style'), size, x, -y, fill, where, turn=-float(props.get('angle', 0) or 0))
        return [{'kind': 'marker', 'marker': None, 'ops': ops, 'rotationField': self.rotation_field(definitions, where)}]

    def marker_line(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, (), where)
        placement = props.get('placements') or props.get('placement') or 'interval'
        if placement.lower() not in ('interval',):
            self.reading.note('untranslated', 'line-marker-position', f'markers at {placement} have no map-style form: left out', where, 'symbol.marker')
            return []
        node = layer.find('symbol')
        if node is None:
            return []
        spacing = self.px(props.get('interval', 3), props.get('interval_unit'), where, 'symbol.marker.spacing')
        if spacing <= 0:
            self.reading.note('untranslated', 'line-marker-position', 'markers along a line with no interval: left out', where, 'symbol.marker')
            return []
        offset = self.px(props.get('offset_along_line', 0), props.get('offset_along_line_unit'), where, 'symbol.marker.spacing')
        if abs(offset - spacing / 2) > 1e-3:
            self.reading.note('approximated', 'line-marker-start', 'map-style starts markers half a spacing from the line\'s start: the offset along the line is not kept', where, 'symbol.marker')
        if props.get('offset') not in (None, '0', '0.0'):
            self.reading.note('approximated', 'offset', 'a marker line offset is not carried', where, 'symbol.marker')
        rotate = props.get('rotate', '1') != '0'
        parts = self.symbol_parts(node, 'marker-on-line', where, along=rotate)
        for part in parts:
            if part['kind'] == 'marker':
                part['place'] = {'placement': 'line', 'spacing': tidy(spacing), 'alongLine': rotate}
        return parts

    def line_pattern(self, layer, props, definitions, geometry, where, along):
        self.unknown_definitions(definitions, (), where)
        node = layer.find('symbol')
        strokes = [p['stroke'] for p in self.symbol_parts(node, 'line', where) if p['kind'] == 'stroke'] if node is not None else []
        if not strokes:
            return []
        stroke = strokes[-1]
        if len(strokes) > 1 or stroke.get('dash'):
            self.reading.note('approximated', 'hatch-line', 'the lines of a line pattern fill are written as one solid line', where, 'symbol.fill.pattern')
        if props.get('offset') not in (None, '0', '0.0'):
            self.reading.note('approximated', 'offset', 'a line pattern offset is not carried', where, 'symbol.fill.pattern')
        return [{'kind': 'pattern', 'pattern': {'kind': 'hatch', 'angle': tidy(float(props.get('angle', 45) or 0)), 'spacing': tidy(self.px(props.get('distance', 5), props.get('distance_unit'), where, 'symbol.fill.pattern.spacing')),
                                                'width': stroke['width'], 'color': stroke['color'], **({'opacity': stroke['opacity']} if 'opacity' in stroke else {})}}]

    def gradient_fill(self, layer, props, definitions, geometry, where, along):
        first, opacity = color_of(props.get('color') or props.get('color1'))
        self.reading.note('approximated', 'gradient', f'a {layer.get("class")} is written as a solid fill of its first colour', where, 'symbol.fill')
        return [{'kind': 'fill', 'color': first, 'opacity': opacity}]

    def centroid_fill(self, layer, props, definitions, geometry, where, along):
        self.reading.note('untranslated', 'polygon-center-marker', 'a marker at the polygon\'s centre has no map-style form: left out', where, 'symbol.marker')
        return []


def fade(part, alpha, reading, where):
    """A part under the symbol's opacity."""
    out = dict(part)
    if part['kind'] == 'fill':
        out['opacity'] = part.get('opacity', 1) * alpha
    elif part['kind'] == 'pattern':
        out['pattern'] = {**part['pattern'], 'opacity': round(part['pattern'].get('opacity', 1) * alpha, 4)}
    elif part['kind'] == 'stroke':
        out['stroke'] = {**part['stroke'], 'opacity': round(part['stroke'].get('opacity', 1) * alpha, 4)}
    else:
        for key in ('fill', 'stroke'):
            if part.get(key):
                out[key] = {**part[key], 'opacity': round(part[key].get('opacity', 1) * alpha, 4)}
        ops = []
        for op in part.get('ops') or []:
            op = dict(op)
            for key in ('fill', 'stroke'):
                if op.get(key):
                    op[key] = {**op[key], 'opacity': round(op[key].get('opacity', 1) * alpha, 4)}
            ops.append(op)
        out['ops'] = ops
        if (part.get('marker') or {}).get('shape') == 'svg':
            reading.note('approximated', 'svg-opacity', 'an image is drawn with its own colours: the symbol\'s opacity is not applied', where, 'symbol.marker')
    return out


def fill_parameters(data, props):
    """QGIS's parametric SVG: param(fill), param(outline) and the like take the symbol's values, as QGIS does."""
    if b'param(' not in data:
        return data
    text = data.decode('utf-8', 'replace')
    fill, fill_alpha = color_of(props.get('color'))
    outline, outline_alpha = color_of(props.get('outline_color'))
    values = {'fill': fill, 'fill-opacity': str(fill_alpha), 'outline': outline, 'outline-opacity': str(outline_alpha),
              'outline-width': str(tidy(float(props.get('outline_width', 0) or 0) * PX_PER_MM))}
    return re.sub(r'param\(([\w-]+)\)(?:\s[^"\';]*)?', lambda m: values.get(m[1], m[0]), text).encode('utf-8')


def named_outline(name, size):
    """Segments for a QGIS marker shape map-style cannot name; None when this reader does not know it."""
    radius = size / 2
    if name in REGULAR:
        count = REGULAR[name]
        return polyline_segments([(radius * math.sin(2 * math.pi * i / count), radius * math.cos(2 * math.pi * i / count)) for i in range(count)], True)
    if name == 'star':
        return polyline_segments([((radius if i % 2 == 0 else radius * 0.382) * math.sin(math.pi * i / 5),
                                   (radius if i % 2 == 0 else radius * 0.382) * math.cos(math.pi * i / 5)) for i in range(10)], True)
    if name == 'line':
        return polyline_segments([(0, radius), (0, -radius)])
    return None


SIZE_EXPRESSION = re.compile(r'2 \* [\d.e-]+ \* \(([-\d.e]+) \+ ([-\d.e]+) \* (?:max\(0, min\(1, )?\((sqrt\()?"((?:[^"]|"")+)"\)? - ([-\d.e]+)\) / ([-\d.e]+)')
LAYERS = {'SimpleFill': Style.simple_fill, 'SimpleLine': Style.simple_line, 'SimpleMarker': Style.simple_marker, 'SvgMarker': Style.svg_marker,
          'FontMarker': Style.font_marker, 'MarkerLine': Style.marker_line, 'LinePatternFill': Style.line_pattern, 'GradientFill': Style.gradient_fill,
          'ShapeburstFill': Style.gradient_fill, 'CentroidFill': Style.centroid_fill}


# --- renderers -----------------------------------------------------------------------------------------------------

def plain_field(attr):
    """The field a renderer's attribute names, or None when it is an expression."""
    text = (attr or '').strip()
    if re.fullmatch(r'"(?:[^"]|"")+"', text):
        return text[1:-1].replace('""', '"')
    return text if text and re.fullmatch(r'[\w. -]+', text) else None


def typed_value(raw, kind):
    if kind == 'bool':
        return raw == 'true'
    try:
        if kind in ('double', 'float'):
            return float(raw)
        if kind in ('long', 'int', 'integer', 'qlonglong', 'longlong'):
            return int(raw)
    except (TypeError, ValueError):
        pass
    return raw


def wider_bound(value, up):
    """A range end as it was meant: the writers step just below the next class's start."""
    candidate = math.nextafter(value, math.inf if up else -math.inf)
    return candidate if len(repr(candidate)) < len(repr(value)) else value


class Renderers:
    """The renderers of one layer."""

    def __init__(self, style, node, geometry, where):
        self.style, self.node, self.geometry, self.where = style, node, geometry, where
        self.reading = style.reading
        holder = node.find('symbols')
        self.symbols = {s.get('name'): s for s in holder.findall('symbol')} if holder is not None else {}

    def passes(self, name, where):
        node = self.symbols.get(name)
        if node is None:
            return self.reading.symbol(self.geometry, where, lambda: [])
        return self.reading.symbol(self.geometry, where, lambda: self.style.symbol_parts(node, self.geometry, where))

    def build(self):
        kind = self.node.get('type')
        if self.node.get('symbollevels') == '1':
            self.reading.note('approximated', 'symbol-levels', 'symbol levels are not carried: symbols draw layer by layer', self.where, 'renderer')
        if kind == 'singleSymbol':
            return {'type': 'single', 'passes': self.passes('0', self.where)}
        if kind == 'categorizedSymbol':
            return self.categorized()
        if kind == 'graduatedSymbol':
            return self.graduated()
        if kind == 'RuleRenderer':
            return self.rules()
        if kind == 'nullSymbol':
            return None
        self.reading.note('untranslated', 'renderer', f'the QGIS renderer {kind} has no map-style form: the features are not drawn', self.where, 'renderer')
        return None

    def categorized(self):
        attr = self.node.get('attr')
        field = plain_field(attr)
        holder = self.node.find('categories')
        rules, default = [], None
        for item in (holder.findall('category') if holder is not None else []):
            label = item.get('label') or None
            where = {**self.where, 'class': label or item.get('value')}
            values = item.findall('val')
            raw = [(v.get('value'), v.get('type')) for v in values] or [(item.get('value'), item.get('type'))]
            passes = self.passes(item.get('symbol'), where)
            if raw == [('', 'string')] or raw[0][0] in ('', None) or raw[0][1] == 'NULL':
                default = passes
                continue
            for text, kind in raw:
                value = typed_value(text, kind)
                spec = {'field': field, 'op': '==', 'value': value} if field else {'expression': f'({attr}) = {sql_literal(value)}', 'dialect': 'qgis'}
                rules.append({'filter': spec, 'label': label, 'visible': False if item.get('render') == 'false' else None, 'passes': passes})
        if not rules:
            if default:
                return {'type': 'single', 'passes': default}
            return None
        return rules_renderer(rules, default)

    def graduated(self):
        attr = self.node.get('attr')
        field = plain_field(attr)
        holder = self.node.find('ranges')
        ranges = sorted(holder.findall('range') if holder is not None else [], key=lambda r: float(r.get('lower', 0)))
        classes, rules = [], []
        for index, item in enumerate(ranges):
            low, high = float(item.get('lower')), float(item.get('upper'))
            following = float(ranges[index + 1].get('lower')) if index + 1 < len(ranges) else None
            if following == high:
                self.reading.note('approximated', 'range-boundary', 'a value on a class boundary falls in the lower class in QGIS and in the upper one in map-style', self.where, 'renderer')
            elif following is not None and math.nextafter(high, math.inf) == following:  # the writer's way of saying [low, high)
                high = following
            label = item.get('label') or None
            where = {**self.where, 'class': label or f'{low}-{high}'}
            passes = self.passes(item.get('symbol'), where)
            visible = False if item.get('render') == 'false' else None
            classes.append({'range': [tidy_bound(low), tidy_bound(high)], 'label': label, 'visible': visible, 'passes': passes})
            rules.append({'label': label, 'visible': visible, 'passes': passes, 'filter': {'expression': f'({attr}) >= {low} AND ({attr}) <= {high}', 'dialect': 'qgis'}})
        if not ranges:
            return None
        if not field:
            return rules_renderer(rules)
        out = {'type': 'graduated', 'field': field, 'classes': [{k: v for k, v in c.items() if v is not None} for c in classes], 'default': None}
        method = {'Custom': 'manual', 'EqualInterval': 'equal', 'Quantile': 'quantile', 'Jenks': 'jenks'}.get((self.node.find('classificationMethod').get('id') if self.node.find('classificationMethod') is not None else None))
        if method:
            out['method'] = method
        return out

    def rules(self):
        """Rules: map-style takes the first match, QGIS draws every match. The writer's rules exclude the earlier ones to say
        the same; when a file's rules do not, the reading may differ where they overlap, which is reported."""
        holder = self.node.find('rules')
        if holder is None:
            return None
        top = holder.findall('rule')
        condition = None
        if len(top) == 1 and top[0].get('symbol') is None and top[0].findall('rule') and top[0].get('filter'):
            condition = top[0].get('filter')  # the writer's layer filter, around the classes
            top = top[0].findall('rule')
        rules, default, first_match = [], None, True
        earlier = []
        for item, parent in self.flatten(top, None):
            label = item.get('label') or item.get('description') or None
            where = {**self.where, 'class': label or item.get('filter') or 'rule'}
            if item.get('scalemaxdenom') or item.get('scalemindenom'):
                self.reading.note('untranslated', 'scale-range', 'a rule\'s scale range is not carried: the rule draws at every scale', where, 'renderer')
            text, stripped = strip_exclusions(item.get('filter') or '', earlier)
            first_match = first_match and stripped
            passes = self.passes(item.get('symbol'), where)
            visible = False if item.get('active') == '0' else None
            if text.upper() in ('', 'TRUE', 'ELSE') and not parent:
                default = passes
                continue
            if text:
                earlier.append(text)
            spec = parse_filter(text, 'qgis')
            if parent:
                parent_spec = parse_filter(parent, 'qgis')
                spec = {'all': [parent_spec, spec]} if text else parent_spec
            rules.append({'filter': spec, 'label': label, 'visible': visible, 'passes': passes})
        if not rules:
            if default:
                return ({'type': 'single', 'passes': default}, condition)
            return (None, condition)
        result = rules_renderer(rules, default)
        if not first_match and result['type'] == 'rule' and len(rules) > 1:
            self.reading.note('approximated', 'rule-overlap', 'QGIS draws every rule a feature matches, map-style only the first: where these rules overlap, only the first draws', self.where, 'renderer')
        return (result, condition)

    def flatten(self, rules, parent):
        """Each rule that draws, with the filter of the rules around it joined."""
        for rule in rules:
            own = rule.get('filter')
            joined = ' AND '.join(f'({t})' for t in (parent, own) if t) if parent and own and rule.findall('rule') else (parent or own if rule.findall('rule') else parent)
            if rule.get('symbol') is not None:
                yield rule, parent
            elif not rule.findall('rule'):
                self.reading.note('untranslated', 'renderer', 'a rule without a symbol draws nothing: left out', self.where, 'renderer')
            yield from self.flatten(rule.findall('rule'), joined)


def tidy_bound(value):
    value = float(value)
    return int(value) if value.is_integer() and abs(value) < 1e15 else value


def sql_literal(value):
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def strip_exclusions(text, earlier):
    """A rule as `_qgis_style.write_rules` writes it, `coalesce(this, FALSE) AND NOT coalesce(e, FALSE)...`, back to `this`.
    (rule's own filter, whether it said first-match in those words). A first rule has nothing to exclude and counts as saying it."""
    rest = text.strip()
    if not earlier and not rest.startswith('coalesce('):
        return rest, True
    for previous in reversed(earlier):
        tail = f'NOT coalesce({previous}, FALSE)'
        if rest == tail:
            return '', True
        if rest.endswith(' AND ' + tail):
            rest = rest[:-len(' AND ' + tail)]
        else:
            return text.strip(), False
    if rest.startswith('coalesce(') and rest.endswith(', FALSE)'):
        return rest[len('coalesce('):-len(', FALSE)')], True
    return rest, not earlier or rest.upper() == 'TRUE'


# --- labels --------------------------------------------------------------------------------------------------------

def weight_of(named, qt_weight):
    named = (named or '').lower()
    for word, weight in sorted(STYLE_WEIGHTS.items(), key=lambda kv: -len(kv[0])):
        if word in named.replace(' ', '').replace('-', ''):
            return weight
    if named:
        return None
    try:
        qt_weight = int(qt_weight)
    except (TypeError, ValueError):
        return None
    if qt_weight < 100:  # Qt 5: 50 regular, 75 bold
        return {25: 300, 50: 400, 57: 500, 63: 600, 75: 700, 87: 800, 99: 900}.get(qt_weight)
    return qt_weight


def label_text(node, style, where):
    """A map-style text style from <text-style>."""
    reading = style.reading
    unit = node.get('fontSizeUnit') or 'Point'
    size = style.px(node.get('fontSize') or 10, unit, where, 'labels.text.size')
    value, alpha = color_of(node.get('textColor'))
    out = {'size': tidy(size), 'color': value}
    if node.get('fontFamily'):
        out['font'] = node.get('fontFamily')
    weight = weight_of(node.get('namedStyle'), node.get('fontWeight'))
    if weight and weight != 400:
        out['weight'] = weight
    if node.get('fontItalic') == '1' or 'italic' in (node.get('namedStyle') or '').lower():
        reading.note('untranslated', 'font-style', 'map-style has no italic: the label is written upright', where, 'labels.text')
    opacity = alpha * float(node.get('textOpacity') or 1)
    if opacity < 1:
        out['opacity'] = round(opacity, 4)
    spacing = float(node.get('fontLetterSpacing') or 0)
    if spacing:
        out['letterSpacing'] = round(spacing / float(node.get('fontSize') or 10), 4)
    buffer = node.find('text-buffer')
    if buffer is not None and buffer.get('bufferDraw') == '1':
        tone, tone_alpha = color_of(buffer.get('bufferColor'))
        halo = {'color': tone, 'width': tidy(style.px(buffer.get('bufferSize') or 1, buffer.get('bufferSizeUnits') or 'MM', where, 'labels.text.halo.width'))}
        strength = tone_alpha * float(buffer.get('bufferOpacity') or 1)
        if strength < 1:
            halo['opacity'] = round(strength, 4)
        blur = buffer.find('effect')
        if blur is not None and blur.get('type') == 'blur':
            level = properties_of(blur).get('blur_level')
            if level:
                halo['blur'] = tidy(float(level) * PX_PER_MM)
        out['halo'] = halo
    box = node.find('background')
    if box is not None and box.get('shapeDraw') == '1':
        out['box'] = label_box(box, style, where)
    shadow = node.find('shadow')
    if shadow is not None and shadow.get('shadowDraw') == '1':
        reading.note('untranslated', 'label-shadow', 'a label shadow is not carried', where, 'labels.text')
    return out


def label_box(node, style, where):
    reading = style.reading
    box = {}
    fill = paint_of(node.get('shapeFillColor'))
    if fill:
        box['fill'] = fill
    tone, alpha = color_of(node.get('shapeBorderColor'))
    width = float(node.get('shapeBorderWidth') or 0)
    if alpha > 0 and width > 0:
        box['stroke'] = line(tone, style.px(width, node.get('shapeBorderWidthUnit') or 'MM', where, 'labels.text.box.stroke'), alpha)
    if node.get('shapeSizeType', '0') == '0':
        unit = node.get('shapeSizeUnit') or 'MM'
        box['padding'] = [tidy(style.px(node.get('shapeSizeY') or 0, unit, where, 'labels.text.box.padding')), tidy(style.px(node.get('shapeSizeX') or 0, unit, where, 'labels.text.box.padding'))]
    else:
        reading.note('approximated', 'label-box', 'a fixed-size label background is written as a box around the text', where, 'labels.text.box')
    if node.get('shapeType', '0') != '0':
        reading.note('approximated', 'label-box', 'a label background that is not a rectangle is written as a box', where, 'labels.text.box')
    if (node.get('shapeRadiiX') or '0') not in ('0', '0.0') or (node.get('shapeRadiiY') or '0') not in ('0', '0.0'):
        reading.note('untranslated', 'label-box', 'map-style has no rounded box: the corners are square', where, 'labels.text.box')
    return box


def label_source(text, is_expression, where, reading):
    """(field or None, template or None) for a label's text."""
    if not is_expression:
        return text, None
    plain = plain_field(text) if text.strip().startswith('"') else None
    if plain:
        return plain, None
    found = re.fullmatch(r'\s*concat\((.*)\)\s*', text, re.S)
    pieces = split_arguments(found[1]) if found else re.split(r'\s*\|\|\s*', text)
    template = ''
    for piece in pieces:
        piece = piece.strip()
        if re.fullmatch(r"'(?:[^']|'')*'", piece):
            template += piece[1:-1].replace("''", "'").replace('{', '').replace('}', '')
        elif re.fullmatch(r'"(?:[^"]|"")+"', piece):
            template += '{' + piece[1:-1].replace('""', '"') + '}'
        else:
            template = None
            break
    if template and len(pieces) > 1:
        return None, template
    fields = re.findall(r'"((?:[^"]|"")+)"', text)
    if fields:
        reading.note('approximated', 'label-expression', f'the label expression {text!r} is not a field or a template: the labels show {fields[0]} alone', where, 'labels.field')
        return fields[0].replace('""', '"'), None
    reading.note('untranslated', 'label-expression', f'the label expression {text!r} names no field: the labels are left out', where, 'labels')
    return None, None


def split_arguments(text):
    parts, depth, current, quote = [], 0, '', None
    for char in text:
        if quote:
            quote = None if char == quote else quote
        elif char in '\'"':
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(current)
            current = ''
            continue
        current += char
    return parts + [current]


def label_settings(settings, geometry, style, where):
    reading = style.reading
    node = settings.find('text-style')
    if node is None:
        return None
    field, template = label_source(node.get('fieldName') or '', node.get('isExpression') == '1', where, reading)
    if not field and not template:
        return None
    out = {'field': field} if field else {'template': template}
    out['text'] = label_text(node, style, where)
    placement = settings.find('placement')
    if placement is not None:
        mode = placement.get('placement')
        if geometry == 'point':
            out['placement'] = {'0': 'auto', '1': 'point'}.get(mode, 'auto')
            known = mode in ('0', '1')
        elif geometry == 'line':
            out['placement'] = {'2': 'auto', '3': 'line'}.get(mode, 'auto')
            known = mode in ('2', '3')
        else:
            out['placement'] = 'polygon-center' if placement.get('centroidInside') == '1' else 'auto'
            known = mode in ('0', '1', '4')
        if not known:
            reading.note('approximated', 'label-placement', f'QGIS label placement {mode} is written as {out["placement"]}', where, 'labels.placement')
        if any(float(placement.get(k) or 0) for k in ('xOffset', 'yOffset')) or (geometry != 'point' and float(placement.get('dist') or 0)):
            reading.note('untranslated', 'label-offset', 'a label offset is not carried', where, 'labels.placement')
    rendering = settings.find('rendering')
    if rendering is not None and rendering.get('scaleVisibility') == '1':
        reading.note('untranslated', 'scale-range', 'the labels\' scale range is not carried', where, 'labels')
    for name in sorted(active_definitions(settings, 'dd_properties')):
        reading.note('untranslated', 'data-defined', f'data-defined label {name} is not carried', where, 'labels')
    return out


def read_labels(root, geometry, style, where, layer_filter):
    """The labels of the layer. Map-style has one label style per layer: of several rules the first is kept and reported."""
    node = root.find('labeling')
    if node is None:
        return None
    reading = style.reading
    if node.get('type') == 'simple':
        settings = node.find('settings')
        spec = label_settings(settings, geometry, style, where) if settings is not None else None
    elif node.get('type') == 'rule-based':
        rules = [r for r in node.iter('rule') if r.find('settings') is not None]
        if not rules:
            return None
        if rules[0].get('filter', '').startswith('"style_id"'):
            reading.note('untranslated', 'placed-labels', 'labels placed at fixed positions live in the data table, not the style: left out', where, 'labels')
            return None
        spec = label_settings(rules[0].find('settings'), geometry, style, where)
        if len(rules) > 1:
            reading.note('untranslated', 'label-rules', f'map-style has one label style per layer: the first of {len(rules)} label rules is kept', where, 'labels')
        if rules[0].get('filter') and rules[0].get('filter') != layer_filter:
            reading.note('untranslated', 'label-rules', f'the label rule\'s filter {rules[0].get("filter")!r} is not carried: every feature gets a label', where, 'labels')
    else:
        reading.note('untranslated', 'label-rules', f'labeling type {node.get("type")} is not carried', where, 'labels')
        return None
    if spec and root.get('labelsEnabled') == '0':
        spec['enabled'] = False
    return spec


# --- rasters -------------------------------------------------------------------------------------------------------

def raster_renderer(node, where, reading):
    kind = node.get('type')
    band = node.get('band')
    if kind == 'singlebandpseudocolor':
        shader = node.find('rastershader/colorrampshader')
        if shader is None:
            return None
        items = [(float('inf') if i.get('value') == 'inf' else float(i.get('value')), i) for i in shader.findall('item')]
        mode = {'INTERPOLATED': 'linear', 'DISCRETE': 'discrete', 'EXACT': 'exact'}.get(shader.get('colorRampType'), 'linear')
        stops = []
        for index, (value, item) in enumerate(items):
            if mode == 'discrete':  # a QGIS item holds up to and including its value; a map-style stop from its value up
                value = float(node.get('classificationMin') or shader.get('minimumValue') or (items[0][0] if items else 0)) if index == 0 else wider_bound(items[index - 1][0], True)
            stop = {'value': tidy_bound(value), 'color': item.get('color', '#000000')[:7].upper()}
            if item.get('label'):
                stop['label'] = item.get('label')
            if item.get('alpha') not in (None, '255'):
                reading.note('untranslated', 'raster-alpha', 'a colour stop\'s own alpha is not carried', where, 'renderer.stops')
            stops.append(stop)
        if not stops:
            return None
        out = {'type': 'pseudocolor', 'interpolation': mode, 'stops': stops}
        tone = (color_of(node.get('nodataColor'))[0] if node.get('nodataColor') else None)
        if tone:
            out['nodata'] = {'color': tone}
    elif kind == 'paletted':
        holder = node.find('colorPalette')
        entries = holder.findall('paletteEntry') if holder is not None else []
        if not entries:
            return None
        classes = []
        for entry in entries:
            row = {'value': tidy_bound(entry.get('value')), 'color': entry.get('color', '#000000')[:7].upper()}
            if entry.get('label'):
                row['label'] = entry.get('label')
            if entry.get('alpha') not in (None, '255'):
                reading.note('untranslated', 'raster-alpha', 'a palette entry\'s own alpha is not carried', where, 'renderer.classes')
            classes.append(row)
        out = {'type': 'palette', 'classes': classes}
    elif kind == 'multibandcolor':
        out = {'type': 'rgb', 'bands': [int(node.get(k) or i + 1) for i, k in enumerate(('redBand', 'greenBand', 'blueBand'))]}
        reading.note('approximated', 'raster-stretch', 'the contrast stretch of each band is not carried: map-style\'s rgb has none', where, 'renderer')
        return out
    elif kind == 'singlebandgray':
        low = node.find('contrastEnhancement/minValue')
        high = node.find('contrastEnhancement/maxValue')
        if low is None or high is None:
            return None
        reading.note('approximated', 'raster-gray', 'a gray band is written as a black to white pseudocolor between its stretch limits', where, 'renderer')
        band = node.get('grayBand') or band
        out = {'type': 'pseudocolor', 'interpolation': 'linear', 'stops': [{'value': tidy_bound(low.text), 'color': '#000000'}, {'value': tidy_bound(high.text), 'color': '#FFFFFF'}]}
    else:
        return None
    if band and band != '1':
        out['band'] = int(band)
    return out


# --- the file ------------------------------------------------------------------------------------------------------

def read(path, reading):
    raw = path.read_bytes()
    if b'<!ENTITY' in raw:
        raise ValueError(f'{path.name} declares XML entities, which are not read')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f'{path.name} is not readable XML: {error}') from error
    if root.tag != 'qgis':
        raise ValueError(f'{path.name} is not a QGIS style (the root element is {root.tag})')
    layer_id = reading.unique_id(path.stem)
    where = {'layer': layer_id}
    node = root.find('renderer-v2')
    pipe = root.find('pipe')
    raster = pipe.find('rasterrenderer') if pipe is not None else None
    if root.get('hasScaleBasedVisibilityFlag') == '1':
        reading.note('untranslated', 'scale-range', 'the layer\'s scale range is not carried: it draws at every scale', where, 'visible')
    if node is None and raster is None:
        raise ValueError(f'{path.name} holds no renderer: map-style needs the symbology category of the style')
    if node is None:
        renderer = raster_renderer(raster, where, reading)
        if renderer is None:
            raise ValueError(f'{path.name}: the QGIS raster renderer {raster.get("type")} has no map-style form')
        opacity = float(raster.get('opacity') or 1)
        for tag in ('brightnesscontrast', 'huesaturation'):
            item = pipe.find(tag)
            if item is not None and any(item.get(k) not in (None, '0', '1') for k in ('brightness', 'contrast', 'saturation', 'grayscaleMode', 'colorizeOn', 'invertColors')):
                reading.note('untranslated', 'raster-adjust', f'the raster {tag} settings are not carried', where, 'renderer')
        data = reading.unbound_data(layer_id, 'a QML')
        reading.add_layer(layer_id, 'raster', renderer, {**data, 'kind': 'raster'}, title=path.stem, opacity=opacity if opacity < 1 else None)
        return
    geometry = GEOMETRY_NUMBERS.get((root.findtext('layerGeometryType') or '').strip())
    if geometry is None:
        first = next(node.iter('symbol'), None)
        geometry = SYMBOL_GEOMETRY.get(first.get('type')) if first is not None else None
    if geometry is None:
        raise ValueError(f'{path.name} does not say its geometry type and has no symbol to tell it by')
    style = Style(reading, layer_id)
    built = Renderers(style, node, geometry, where).build()
    renderer, condition = built if isinstance(built, tuple) else (built, None)
    layer_filter = None
    if condition:
        layer_filter = parse_filter(condition, 'qgis')
    extra = {}
    opacity = float(root.findtext('layerOpacity') or 1)
    if opacity < 1:
        extra['opacity'] = opacity
    if style.size:
        extra['size'] = style.size
    order = node.find('orderby')
    if node.get('enableorderby') == '1' and order is not None and order.find('orderByClause') is not None:
        clauses = order.findall('orderByClause')
        name = plain_field(clauses[0].text)
        if name:
            extra['sortBy'] = {'field': name, 'order': 'asc' if clauses[0].get('asc') == '1' else 'desc'}
        if len(clauses) > 1 or not name:
            reading.note('untranslated', 'sort', 'feature order by more than one field or an expression is not carried', where, 'sortBy')
    for tag in ('blendMode', 'featureBlendMode'):
        if (root.findtext(tag) or '0').strip() not in ('0', ''):
            reading.note('untranslated', 'blend-mode', f'{tag} is not carried: the layer draws in normal mode', where, 'renderer')
    effect = node.find('effect')
    if effect is not None and effect.get('enabled') == '1':
        reading.note('untranslated', 'paint-effect', 'the layer\'s paint effect is not carried', where, 'renderer')
    if root.find('edittypes') is None and root.find('diagrams') is not None:
        reading.note('untranslated', 'diagrams', 'diagrams are not carried', where, 'renderer')
    labels = read_labels(root, geometry, style, where, condition)
    data = reading.unbound_data(layer_id, 'a QML')
    reading.add_layer(layer_id, geometry, renderer, {**data, 'kind': 'vector'}, title=path.stem, labels=labels, filter=layer_filter, **extra)
