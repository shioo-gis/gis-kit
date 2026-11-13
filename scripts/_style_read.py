"""Platform style -> map-style v1: what every platform reader shares. The other direction is in _map_style.py and the writers.

One reader per platform: `_qgis_read.py` (.qml) and `_arcgis_read.py` (.lyrx, .stylx); a new one (ArcMap .lyr, .style)
is a module with a `read(path, reading)` function and a row in READERS. A reader only understands its platform and
hands over neutral pieces; nothing below this line knows QML or CIM:

- `reading.symbol(geometry, where, convert)` runs `convert()`, which returns a symbol's parts, bottom first (`fill`,
  `pattern`, `stroke`, `marker`, see merge_parts), and gives back its passes: the map-style symbols that draw it, one
  when one symbol can hold it all. The same call counts how much was lost for the report.
- A renderer is {type, field, classes or rules, default} with `passes` where map-style has `symbol`.
  `rules_renderer` turns first-match rules back into `categorized` or `graduated` when they are exactly that.
- `reading.add_layer(...)` takes a layer with such a renderer and writes as many map-style layers as it has passes.
- `parse_filter` reads a platform's filter text into the structured filter, or keeps it as `{expression, dialect}`.
- `reading.note(kind, code, reason, where, prop)` records what could not be carried. Nothing is dropped silently:
  `untranslated` is left out and `approximated` written approximately, as in the contract; `code` counts it in the report.

`package.py read` calls `add_command` and `run`, which write the map-style document, the SVG images it refers to under
assets/ (dataRoot "file", so the folder can move as a whole), style-read.json (the loss statistics) and record.json.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
from xml.sax.saxutils import escape, quoteattr

from _arcgis_style import CROSS, PATHS, RINGS, turned
from _delivery import bundle, digest, write_json
import _map_style

from _version import VERSION
READERS = {'.qml': ('_qgis_read', 'read'), '.lyrx': ('_arcgis_read', 'read_lyrx'), '.stylx': ('_arcgis_read', 'read_stylx')}
PX_PER_PT = 96 / 72
PX_PER_MM = 96 / 25.4
LEVELS = ('exact', 'svg', 'split', 'approximated', 'lost')
UNBOUND = 'data/unbound'


def tidy(value):
    """A size as written: unit conversions leave noise beyond 1/10000 px."""
    value = round(float(value), 4)
    return int(value) if value.is_integer() else value


def hex_color(red, green, blue):
    return '#%02X%02X%02X' % tuple(max(0, min(255, round(v))) for v in (red, green, blue))


def paint(color, opacity=1.0):
    """A fill or colour reference: {color, opacity}, opacity only when it is not 1."""
    item = {'color': color}
    if opacity < 1:
        item['opacity'] = round(max(0.0, float(opacity)), 4)
    return item


def line(color, width, opacity=1.0, dash=None, cap='butt', join='miter'):
    item = {**paint(color, opacity), 'width': tidy(width)}
    if dash:
        item['dash'] = [tidy(v) for v in dash]
    if cap != 'butt':
        item['cap'] = cap
    if join != 'miter':
        item['join'] = join
    return item


def invisible(geometry):
    """A symbol that draws nothing, for a class whose symbol could not be carried: the class and its value stay."""
    if geometry == 'line':
        return {'stroke': {'color': '#000000', 'width': 0, 'opacity': 0}}
    if geometry == 'point':
        return {'fill': {'color': '#000000', 'opacity': 0}, 'marker': {'shape': 'circle', 'size': 0}}
    return {'fill': {'color': '#000000', 'opacity': 0}}


# --- filters -------------------------------------------------------------------------------------------------------

class Unparsed(ValueError):
    """Filter text that is not plainly the structured filter: kept as an expression in its dialect."""


TOKEN = re.compile(r"""\s*(?:(?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(?P<string>'(?:[^']|'')*')|(?P<quoted>"(?:[^"\\]|\\.|"")*")
                   |(?P<feature>\$feature\[\s*"(?:[^"\\]|\\.)*"\s*\]|\$feature\.\w+)|(?P<word>[^\W\d]\w*)
                   |(?P<op><>|!=|<=|>=|==|&&|\|\||[=<>(),\[\]!]))""", re.X)
COMPARE = {'=': '==', '==': '==', '<>': '!=', '!=': '!=', '<': '<', '<=': '<=', '>': '>', '>=': '>='}


def tokens(text):
    position, out = 0, []
    text = text.rstrip()
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match:
            raise Unparsed(text)
        out.append((match.lastgroup, match.group(match.lastgroup)))
        position = match.end()
    return out


class FilterParser:
    """A boolean expression over `field op value` as QGIS, SQL and Arcade write it, as a small tree."""

    def __init__(self, text, dialect):
        self.items, self.at, self.dialect = tokens(text), 0, dialect

    def peek(self):
        return self.items[self.at] if self.at < len(self.items) else (None, None)

    def word(self, *names):
        kind, value = self.peek()
        if value is not None and ((kind == 'word' and value.upper() in names) or (kind == 'op' and value in names)):
            self.at += 1
            return True
        return False

    def expect(self, *names):
        if not self.word(*names):
            raise Unparsed(f'expected {names[0]}')

    def parse(self):
        tree = self.any_of()
        if self.at != len(self.items):
            raise Unparsed('trailing text')
        return tree

    def any_of(self):
        parts = [self.all_of()]
        while self.word('OR', '||'):
            parts.append(self.all_of())
        return parts[0] if len(parts) == 1 else ('or', parts)

    def all_of(self):
        parts = [self.negated()]
        while self.word('AND', '&&'):
            parts.append(self.negated())
        return parts[0] if len(parts) == 1 else ('and', parts)

    def negated(self):
        if self.word('NOT', '!'):
            return ('not', self.negated())
        return self.atom()

    def atom(self):
        if self.word('('):
            tree = self.any_of()
            self.expect(')')
            return tree
        kind, value = self.peek()
        if kind == 'word' and value.lower() == 'coalesce':
            self.at += 1
            self.expect('(')
            tree = self.any_of()
            self.expect(',')
            self.expect('FALSE')
            self.expect(')')
            return ('safe', tree)
        if kind == 'word' and value == 'IsEmpty':
            self.at += 1
            self.expect('(')
            name = self.field()
            self.expect(')')
            return ('isNull', name)
        if kind == 'word' and value == 'Includes':
            self.at += 1
            self.expect('(')
            self.expect('[')
            values = self.values(']')
            self.expect(',')
            name = self.field()
            self.expect(')')
            return ('in', name, values)
        name = self.field()
        if self.word('IS'):
            negate = self.word('NOT')
            self.expect('NULL')
            return ('notNull' if negate else 'isNull', name)
        if self.word('NOT'):
            self.expect('IN')
            self.expect('(')
            return ('notIn', name, self.values(')'))
        if self.word('IN'):
            self.expect('(')
            return ('in', name, self.values(')'))
        kind, value = self.peek()
        if kind != 'op' or value not in COMPARE:
            raise Unparsed('expected a comparison')
        self.at += 1
        return ('compare', name, COMPARE[value], self.literal())

    def field(self):
        kind, value = self.peek()
        self.at += 1
        if kind == 'feature':
            found = re.fullmatch(r'\$feature\.(\w+)', value)
            return found[1] if found else json.loads(value[value.index('[') + 1:value.rindex(']')].strip())
        if self.dialect == 'arcade':
            raise Unparsed('an Arcade field is $feature')
        if kind == 'quoted':
            return value[1:-1].replace('""', '"')
        if kind == 'word' and value.upper() not in ('NULL', 'TRUE', 'FALSE', 'NOT', 'AND', 'OR', 'IN', 'IS'):
            return value
        raise Unparsed('expected a field')

    def literal(self):
        kind, value = self.peek()
        self.at += 1
        if kind == 'number':
            return float(value) if re.search(r'[.eE]', value) else int(value)
        if kind == 'string':
            return value[1:-1].replace("''", "'")
        if kind == 'quoted' and self.dialect == 'arcade':
            return json.loads(value)
        if kind == 'word' and value.upper() in ('TRUE', 'FALSE'):
            return value.upper() == 'TRUE'
        raise Unparsed('expected a value')

    def values(self, closing):
        out = [self.literal()]
        while self.word(','):
            out.append(self.literal())
        self.expect(closing)
        return out


def structured(tree):
    """(map-style filter, two-valued): two-valued when it is never unknown for NULL, so NOT may be read as map-style's."""
    kind = tree[0]
    if kind == 'safe':
        return structured(tree[1])[0], True
    if kind in ('isNull', 'notNull'):
        return {'field': tree[1], 'op': kind}, True
    if kind == 'compare':
        _, name, op, value = tree
        if op == '!=':  # unknown for NULL here, true for NULL in map-style
            return {'all': [{'field': name, 'op': 'notNull'}, {'field': name, 'op': '!=', 'value': value}]}, False
        return {'field': name, 'op': op, 'value': value}, False
    if kind == 'in':
        return {'field': tree[1], 'op': 'in', 'value': tree[2]}, False
    if kind == 'notIn':
        return {'all': [{'field': tree[1], 'op': 'notNull'}, {'field': tree[1], 'op': 'notIn', 'value': tree[2]}]}, False
    if kind == 'not':
        inner = tree[1]
        if inner[0] == 'isNull':
            return {'field': inner[1], 'op': 'notNull'}, True
        spec, safe = structured(inner)
        if not safe:
            raise Unparsed('NOT over a value that may be NULL')
        return {'not': spec}, True
    parts = tree[1]
    if kind == 'and' and len(parts) == 2 and parts[1][0] in ('compare', 'in') and parts[1][1] == null_guard(parts[0]):
        return structured(parts[1])[0], True
    if kind == 'or' and len(parts) == 2 and parts[0][0] == 'isNull':
        other = parts[1]
        if other[0] == 'compare' and other[2] == '!=' and other[1] == parts[0][1]:
            return {'field': other[1], 'op': '!=', 'value': other[3]}, True
        if other[0] == 'notIn' and other[1] == parts[0][1]:
            return {'field': other[1], 'op': 'notIn', 'value': other[2]}, True
        if other[0] == 'not' and other[1][0] == 'in' and other[1][1] == parts[0][1]:
            return {'field': other[1][1], 'op': 'notIn', 'value': other[1][2]}, True
    results = [structured(part) for part in parts]
    return {'all' if kind == 'and' else 'any': [spec for spec, _ in results]}, all(safe for _, safe in results)


def null_guard(tree):
    """The field that `field IS NOT NULL` (or `!IsEmpty(field)`) guards, or None."""
    if tree[0] == 'notNull':
        return tree[1]
    if tree[0] == 'not' and tree[1][0] == 'isNull':
        return tree[1][1]
    return None


def parse_filter(text, dialect):
    """A map-style filter from platform filter text: structured when it plainly is, else {expression, dialect}."""
    try:
        return structured(FilterParser(text, dialect).parse())[0]
    except (Unparsed, ValueError, IndexError):
        return {'expression': without_outer_parens(text), 'dialect': dialect}


def without_outer_parens(text):
    """The writers wrap a kept expression in one pair of parentheses."""
    text = text.strip()
    if not (text.startswith('(') and text.endswith(')')):
        return text
    depth = 0
    for index, char in enumerate(text):
        depth += {'(': 1, ')': -1}.get(char, 0)
        if depth == 0 and index < len(text) - 1:
            return text
    return text[1:-1]


def rules_renderer(rules, default=None):
    """First-match rules, each {filter, label, visible, passes}, as a neutral renderer: `categorized` or `graduated`
    when the rules are exactly that, else `rule`. default: the passes for what no rule takes."""
    leaves = [rule['filter'] for rule in rules]
    fields = {leaf.get('field') for leaf in leaves}
    if rules and len(fields) == 1 and None not in fields and all(leaf.get('op') == '==' for leaf in leaves):
        classes = []
        for rule, leaf in zip(rules, leaves):
            classes.append({'value': leaf['value'], **carried(rule)})
        return {'type': 'categorized', 'field': fields.pop(), 'classes': classes, 'default': default}
    bands = [band(leaf) for leaf in leaves]
    if rules and all(bands) and len({b[0] for b in bands}) == 1 and all(b[3] == '<' for b in bands[:-1]):
        classes = [{'range': [b[1], b[2]], **carried(rule)} for rule, b in zip(rules, bands)]
        return {'type': 'graduated', 'field': bands[0][0], 'classes': classes, 'default': default}
    return {'type': 'rule', 'rules': [{'filter': rule['filter'], **carried(rule)} for rule in rules], 'default': default}


def carried(rule):
    return {key: rule[key] for key in ('label', 'visible', 'passes') if rule.get(key) is not None}


def band(leaf):
    """(field, low, high, upper op) of `low <= field < high` (or <= high), else None."""
    parts = leaf.get('all') or []
    if len(parts) != 2 or parts[0].get('op') != '>=' or parts[1].get('op') not in ('<', '<=') or parts[0].get('field') != parts[1].get('field'):
        return None
    if not all(isinstance(p.get('value'), (int, float)) and not isinstance(p.get('value'), bool) for p in parts):
        return None
    return parts[0]['field'], parts[0]['value'], parts[1]['value'], parts[1]['op']


# --- drawing -------------------------------------------------------------------------------------------------------
# A marker that map-style cannot name is drawn into an SVG. Drawing operations hold px, y up, the marker's anchor at
# the origin: {d: segments, fill, stroke, rule} or {image: data uri, x, y, width, height, turn} or {text: ...}.
# Segments are ('M', p), ('L', p), ('C', p1, p2, p) and ('Z',).

KAPPA = 0.5522847498


def arc_segments(start, center, end, ccw):
    """A circular arc as cubic curves; start == end is the full circle."""
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if radius == 0:
        return [('L', end)]
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = a1 - a0
    if ccw and sweep <= 1e-9:
        sweep += 2 * math.pi
    elif not ccw and sweep >= -1e-9:
        sweep -= 2 * math.pi
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2) - 1e-9))
    step, out = sweep / count, []
    k = 4 / 3 * math.tan(step / 4)
    for index in range(count):
        t0, t1 = a0 + step * index, a0 + step * (index + 1)
        c0, s0, c1, s1 = math.cos(t0), math.sin(t0), math.cos(t1), math.sin(t1)
        out.append(('C', (center[0] + radius * (c0 - k * s0), center[1] + radius * (s0 + k * c0)),
                    (center[0] + radius * (c1 + k * s1), center[1] + radius * (s1 - k * c1)),
                    (center[0] + radius * c1, center[1] + radius * s1)))
    return out


def circle_segments(radius):
    k = KAPPA * radius
    return [('M', (0, radius)), ('C', (k, radius), (radius, k), (radius, 0)), ('C', (radius, -k), (k, -radius), (0, -radius)),
            ('C', (-k, -radius), (-radius, -k), (-radius, 0)), ('C', (-radius, k), (-k, radius), (0, radius)), ('Z',)]


def polyline_segments(points, closed=False):
    return [('M', tuple(points[0]))] + [('L', tuple(p)) for p in points[1:]] + ([('Z',)] if closed else [])


def shape_segments(shape, size):
    """A map-style marker shape, as the writers draw it: (segments, drawn with lines only)."""
    scale = size / 4  # the writers' frame is -2..2
    if shape in ('circle', 'ring'):
        return circle_segments(size / 2), False
    if shape in RINGS:
        return polyline_segments([(x * scale, y * scale) for x, y in RINGS[shape][:-1]], True), False
    if shape in PATHS:
        segments = []
        for path in PATHS[shape]:
            segments += polyline_segments([(x * scale, y * scale) for x, y in path])
        return segments, True
    raise ValueError(f'unknown marker shape {shape}')


def filled_cross(shape, size):
    points = turned(CROSS, 45 if shape == 'x' else 0)
    return polyline_segments([(x * size / 4, y * size / 4) for x, y in points[:-1]], True)


def map_points(segments, change):
    return [(seg[0], *[change(p) for p in seg[1:]]) for seg in segments]


def transform_op(op, scale=1.0, turn=0.0, shift=(0.0, 0.0), scale_lines=True):
    """The operation turned counter-clockwise by turn degrees about the origin, then scaled and moved."""
    a = math.radians(turn)
    cos, sin = math.cos(a), math.sin(a)

    def change(point):
        x, y = point[0] * scale, point[1] * scale
        return (x * cos - y * sin + shift[0], x * sin + y * cos + shift[1])

    out = dict(op)
    if 'd' in op:
        out['d'] = map_points(op['d'], change)
        if op.get('stroke') and scale_lines:
            stroke = dict(op['stroke'])
            stroke['width'] = stroke['width'] * scale
            if stroke.get('dash'):
                stroke['dash'] = [v * scale for v in stroke['dash']]
            out['stroke'] = stroke
    else:
        out['x'], out['y'] = change((op['x'], op['y']))
        for key in ('size', 'width', 'height'):
            if key in op:
                out[key] = op[key] * scale
        out['turn'] = op.get('turn', 0) + turn
    return out


def shape_ops(shape, size, fill, stroke):
    """A named shape as drawing operations; a line-only shape or a ring takes the fill colour for its lines, as the writers do."""
    segments, lines_only = shape_segments(shape, size)
    if shape in ('cross', 'x') and fill:
        segments, lines_only = filled_cross(shape, size), False
    outline = stroke
    if outline is None and (lines_only or shape == 'ring') and fill:
        outline = _map_style.line_marker_outline(shape, size, fill)
    return [{'d': segments, 'fill': None if lines_only or shape == 'ring' else fill, 'stroke': outline}]


def number_text(value):
    text = f'{value:.3f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def path_text(segments, half):
    def point(p):
        return f'{number_text(p[0] + half)} {number_text(half - p[1])}'

    out = []
    for seg in segments:
        if seg[0] == 'Z':
            out.append('Z')
        else:
            out.append(seg[0] + ' '.join(point(p) for p in seg[1:]))
    return ' '.join(out)


def op_extent(op):
    """The op's largest distance from the anchor along either axis."""
    if 'd' in op:
        pad = (op.get('stroke') or {}).get('width', 0) / 2
        return max((abs(v) for seg in op['d'] for p in seg[1:] for v in p), default=0) + pad
    if 'image' in op:
        return max(abs(op['x']) + op['width'] / 2, abs(op['y']) + op['height'] / 2)
    return max(abs(op['x']) + op['size'] * max(1, len(op['text'])) / 2, abs(op['y']) + op['size'] / 2)


def paint_attrs(fill, stroke):
    attrs = []
    if fill:
        attrs.append(f'fill="{fill["color"]}"')
        if fill.get('opacity', 1) != 1:
            attrs.append(f'fill-opacity="{number_text(fill["opacity"])}"')
    else:
        attrs.append('fill="none"')
    if stroke and stroke['width'] > 0:
        attrs += [f'stroke="{stroke["color"]}"', f'stroke-width="{number_text(stroke["width"])}"']
        if stroke.get('opacity', 1) != 1:
            attrs.append(f'stroke-opacity="{number_text(stroke["opacity"])}"')
        if stroke.get('dash'):
            attrs.append('stroke-dasharray="' + ' '.join(number_text(v) for v in stroke['dash']) + '"')
        if stroke.get('cap', 'butt') != 'butt':
            attrs.append(f'stroke-linecap="{stroke["cap"]}"')
        if stroke.get('join', 'miter') != 'miter':
            attrs.append(f'stroke-linejoin="{stroke["join"]}"')
    return ' '.join(attrs)


def drawing_svg(ops):
    """(SVG text, side in px): a square image with the anchor at its centre, so it stays on the point when drawn."""
    half = max((op_extent(op) for op in ops), default=0) or 1.0
    half = math.ceil(half * 1000) / 1000
    body = []
    for op in ops:
        if 'd' in op:
            rule = ' fill-rule="evenodd"' if op.get('rule', 'evenodd') == 'evenodd' else ''
            body.append(f'<path d="{path_text(op["d"], half)}" {paint_attrs(op.get("fill"), op.get("stroke"))}{rule}/>')
        else:
            x, y = op['x'] + half, half - op['y']
            turn = f' transform="rotate({number_text(-op.get("turn", 0))} {number_text(x)} {number_text(y)})"' if op.get('turn') else ''
            if 'image' in op:
                body.append(f'<image x="{number_text(x - op["width"] / 2)}" y="{number_text(y - op["height"] / 2)}" width="{number_text(op["width"])}" '
                            f'height="{number_text(op["height"])}" href={quoteattr(op["image"])} xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href={quoteattr(op["image"])}{turn}/>')
            else:  # 0.35 em below the point centres the line on it; dominant-baseline is not read everywhere
                body.append(f'<text x="{number_text(x)}" y="{number_text(y + op["size"] * 0.35)}" text-anchor="{op.get("anchor", "middle")}" '
                            f'font-family={quoteattr(op["font"])} font-size="{number_text(op["size"])}" {paint_attrs(op.get("fill"), None)}{turn}>{escape(op["text"])}</text>')
    side = number_text(2 * half)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" width="{side}" height="{side}">' + ''.join(body) + '</svg>\n', 2 * half)


def glyph_segments(path):
    """A matplotlib Path as segments: quadratic curves raised to cubic."""
    segments, codes, last = [], list(path.codes), (0.0, 0.0)
    vertices = [tuple(map(float, v)) for v in path.vertices]
    index = 0
    while index < len(codes):
        code, point = codes[index], vertices[index]
        if code == 1:
            segments.append(('M', point))
            last, index = point, index + 1
        elif code == 2:
            segments.append(('L', point))
            last, index = point, index + 1
        elif code == 3:
            control, end = point, vertices[index + 1]
            segments.append(('C', (last[0] + 2 / 3 * (control[0] - last[0]), last[1] + 2 / 3 * (control[1] - last[1])),
                             (end[0] + 2 / 3 * (control[0] - end[0]), end[1] + 2 / 3 * (control[1] - end[1])), end))
            last, index = end, index + 2
        elif code == 4:
            segments.append(('C', point, vertices[index + 1], vertices[index + 2]))
            last, index = vertices[index + 2], index + 3
        else:
            segments.append(('Z',))
            index += 1
    return segments


# --- symbols -------------------------------------------------------------------------------------------------------

def same_line(one, other):
    return (one['color'].upper() == other['color'].upper() and one.get('opacity', 1) == other.get('opacity', 1)
            and abs(float(one['width']) - float(other['width'])) < 1e-3)


def marker_colours(part):
    """(fill, stroke) of a marker part. The writers draw a line-only shape or a ring without a stroke in the fill colour
    at a width of their own (line_marker_outline): that outline reads back as the fill."""
    fill, stroke, marker = part.get('fill'), part.get('stroke'), part.get('marker') or {}
    if stroke and not fill and marker.get('shape') in ('chevron', 'cross', 'x', 'ring'):
        expected = _map_style.line_marker_outline(marker['shape'], marker['size'], {'color': stroke['color'], 'opacity': stroke.get('opacity', 1)})
        if abs(float(expected['width']) - float(stroke['width'])) < 1e-3:
            return paint(stroke['color'], stroke.get('opacity', 1)), None
    return fill, stroke


def crossing(one, other):
    return (one['spacing'] == other['spacing'] and one['width'] == other['width'] and one['color'] == other['color']
            and one.get('opacity', 1) == other.get('opacity', 1) and abs((float(one.get('angle', 45)) - float(other.get('angle', 45))) % 180 - 90) < 1e-6)


class Reading:
    """One read: the notes, the images, the layers and the counts for the report."""

    def __init__(self, source, *, data=None, data_layer=None, field=None, match=None, font_dir=None):
        self.source, self.data, self.data_layer, self.field, self.match = Path(source), data, data_layer, field, match
        self.font_dir = font_dir
        self.notes, self.layers, self.assets, self.seen_ids = [], [], {}, set()
        self.symbols, self.losses, self.extra = Counter(), Counter(), {}
        self.fonts_outlined, self.fonts_missing = set(), set()
        self._font_files = {}
        self._fonts_added = False
        self._noted = set()
        self._symbol_kinds = None  # what the symbol being read has lost so far

    # --- notes ---------------------------------------------------------------------------------------------------

    def note(self, kind, code, reason, where=None, prop=None):
        where = where or {}
        text = f'{where["class"]}: {reason}' if where.get('class') is not None else reason
        self.losses[code] += 1
        if self._symbol_kinds is not None:
            self._symbol_kinds.add(kind)
        row = {'kind': kind}
        if where.get('layer'):
            row['layer'] = where['layer']
        if prop:
            row['property'] = prop
        row['reason'] = text
        identity = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if identity not in self._noted:
            self._noted.add(identity)
            self.notes.append(row)

    # --- assets --------------------------------------------------------------------------------------------------

    def store_svg(self, text):
        """An image in assets/ by its hash, as marker.svg refers to it."""
        data = text.encode('utf-8') if isinstance(text, str) else text
        sha = hashlib.sha256(data).hexdigest()
        name = f'assets/{sha[:16]}.svg'
        self.assets[name] = data
        return {'path': name, 'sha256': sha}

    def svg_marker(self, ops):
        """A marker from drawing operations: a square SVG with the anchor at the centre."""
        text, side = drawing_svg(ops)
        return {'shape': 'svg', 'size': tidy(side), 'svg': self.store_svg(text)}

    # --- text in markers -----------------------------------------------------------------------------------------

    def _font_file(self, family, style):
        """The installed font file for a family that has it exactly, or None; --font-dir comes first."""
        key = (family, style)
        if key in self._font_files:
            return self._font_files[key]
        from matplotlib import font_manager
        if self.font_dir and not self._fonts_added:
            self._fonts_added = True
            for path in sorted(Path(self.font_dir).rglob('*')):
                if path.suffix.lower() in ('.ttf', '.otf', '.ttc'):
                    font_manager.fontManager.addfont(str(path))
        try:
            weight = 'bold' if 'bold' in (style or '').lower() else 'normal'
            found = font_manager.findfont(font_manager.FontProperties(family=family, weight=weight), fallback_to_default=False)
        except ValueError:
            found = None
        self._font_files[key] = found
        return found

    def text_ops(self, text, family, style, size, x, y, fill, where, anchor='middle', turn=0.0):
        """Text in a marker. With the font installed it becomes outlines, and the image needs no font from then on;
        without it the SVG keeps a <text> naming the font, which only draws right where that font is installed."""
        lines = re.split(r'\r\n|\r|\n', text)
        if len(lines) > 1:  # a block centred on the point, 1.2 em from line to line
            a = math.radians(turn)
            ops = []
            for index, row in enumerate(lines):
                drop = ((len(lines) - 1) / 2 - index) * size * 1.2
                ops += self.text_ops(row, family, style, size, x - drop * math.sin(a), y + drop * math.cos(a), fill, where, anchor, turn)
            return ops
        font = self._font_file(family, style) if family else None
        if font:
            from matplotlib.font_manager import FontProperties
            from matplotlib.ft2font import FT2Font
            from matplotlib.textpath import TextPath
            face = FT2Font(font)
            if all(face.get_char_index(ord(c)) for c in text if not c.isspace()):
                path = TextPath((0, 0), text, size=size, prop=FontProperties(fname=font))
                if len(path.vertices):
                    box = path.get_extents()
                    dx = {'middle': -(box.x0 + box.x1) / 2, 'start': 0, 'end': -box.x1}[anchor]
                    op = {'d': map_points(glyph_segments(path), lambda p: (p[0] + dx, p[1] - (box.y0 + box.y1) / 2)), 'fill': fill, 'stroke': None, 'rule': 'nonzero'}
                    self.fonts_outlined.add(family)
                    return [transform_op(op, turn=turn, shift=(x, y))]
        self.fonts_missing.add(family or 'unnamed')
        self.note('approximated', 'text-needs-font', f'text in a marker needs the font {family or "(unnamed)"}, which is not installed here: '
                                                     'the SVG names the font, and draws differently where it is missing (--font-dir turns it into outlines)', where, 'symbol.marker')
        return [{'text': text, 'font': family or 'sans-serif', 'size': size, 'x': x, 'y': y, 'fill': fill, 'anchor': anchor, 'turn': turn}]

    # --- symbols -------------------------------------------------------------------------------------------------

    def symbol(self, geometry, where, convert):
        """The passes of one symbol: convert() gives its parts, bottom first. Counts what was lost for the report."""
        self._symbol_kinds = set()
        try:
            passes, used_svg = self.merge_parts(convert(), geometry, where)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, ZeroDivisionError) as error:  # a symbol this reader cannot follow costs that symbol only
            self.note('untranslated', 'unreadable-symbol', f'this symbol could not be read ({type(error).__name__}: {error}): a class that draws nothing is kept', where, 'symbol')
            passes, used_svg = [invisible(geometry)], False
        kinds, self._symbol_kinds = self._symbol_kinds, None
        level = 'lost' if 'untranslated' in kinds else 'approximated' if 'approximated' in kinds else 'split' if len(passes) > 1 else 'svg' if used_svg else 'exact'
        self.symbols[level] += 1
        return passes

    def merge_parts(self, parts, geometry, where):
        """Parts, bottom first, into as few map-style symbols as keep the drawing order within a feature: a symbol
        holds one fill, its pattern, then one stroke, then one marker, in that order. A part that cannot join starts
        a new pass (a new layer). Parts:
        {kind: fill, color, opacity}; {kind: pattern, pattern}; {kind: stroke, stroke};
        {kind: marker, marker (a map-style marker, or None to draw ops), fill, stroke, ops, place, rotationField}."""
        passes, current, used_svg = [], {}, False

        def close():
            nonlocal current
            if current:
                passes.append(current)
            current = {}

        for part in self.combine_markers(parts):
            kind = part['kind']
            if kind == 'fill':
                close()
                current['fill'] = paint(part['color'], part.get('opacity', 1))
            elif kind == 'pattern':
                pattern = dict(part['pattern'])
                if current.get('stroke') or current.get('marker'):
                    close()
                fill = current.get('fill')
                if fill and fill.get('pattern'):
                    if fill['pattern']['kind'] == 'hatch' and pattern['kind'] == 'hatch' and crossing(fill['pattern'], pattern):
                        fill['pattern']['kind'] = 'crossHatch'
                        fill['pattern']['angle'] = min(float(fill['pattern'].get('angle', 45)) % 180, float(pattern.get('angle', 45)) % 180)
                        continue
                    close()
                    fill = None
                if not fill:
                    fill = current['fill'] = {'color': pattern['color'], 'opacity': 0}
                fill['pattern'] = pattern
            elif kind == 'stroke':
                if current.get('stroke') or current.get('marker'):
                    close()
                current['stroke'] = part['stroke']
            else:
                if current.get('marker'):
                    close()
                marker, fill, stroke = part.get('marker'), *marker_colours(part)
                if geometry == 'point':
                    if marker is None:
                        marker, fill, stroke = self.svg_marker(part['ops']), None, None
                else:
                    # on a line or an outline `stroke` is the line and, on a polygon, `fill` the area: a marker in
                    # other colours (or with an outline where there is no line) is drawn as an image in its own colours
                    clash = (stroke is not None and (current.get('stroke') is None or not same_line(stroke, current['stroke']))) or (
                        fill is not None and ((current.get('fill') is not None and current['fill'].get('color') != fill['color'])
                                              or (geometry == 'polygon' and current.get('fill') is None)))
                    if marker is None or (marker['shape'] != 'svg' and clash):
                        marker, fill, stroke = self.svg_marker(part['ops']), None, None
                        marker['rotation'] = 270 if (part.get('place') or {}).get('alongLine', True) else 0
                used_svg = used_svg or marker['shape'] == 'svg'
                final = dict(marker)
                final.update(part.get('place') or {})
                if part.get('rotationField'):
                    final['rotationField'] = part['rotationField']
                if final.get('rotation') is not None:
                    final['rotation'] = tidy(float(final['rotation']) % 360)
                    if final['rotation'] == 0:
                        del final['rotation']
                if geometry == 'point':
                    close()
                    current.update({k: v for k, v in (('fill', fill), ('stroke', stroke)) if v})
                else:
                    if fill and not current.get('fill'):
                        current['fill'] = fill
                    if stroke and not current.get('stroke'):
                        current['stroke'] = stroke
                current['marker'] = final
        close()
        if not passes:
            self.note('untranslated', 'nothing-drawn', 'nothing in this symbol can be drawn in map-style: a class of its own that draws nothing is kept', where, 'symbol')
            passes = [invisible(geometry)]
        return passes, used_svg

    def combine_markers(self, parts):
        """Markers next to each other at the same place become one image: map-style has one marker per symbol."""
        out = []
        for part in parts:
            last = out[-1] if out else None
            if part['kind'] == 'marker' and last and last['kind'] == 'marker' and (part.get('place') or {}) == (last.get('place') or {}):
                out[-1] = {'kind': 'marker', 'marker': None, 'ops': last['ops'] + part['ops'], 'place': part.get('place'),
                           'rotationField': part.get('rotationField') or last.get('rotationField')}
            else:
                out.append(part)
        return out

    # --- layers --------------------------------------------------------------------------------------------------

    def unique_id(self, wanted):
        base = re.sub(r'[^\w.-]+', '-', str(wanted)).strip('-') or 'layer'
        candidate, index = base, 1
        while candidate in self.seen_ids:
            index += 1
            candidate = f'{base}-{index}'
        self.seen_ids.add(candidate)
        return candidate

    def unbound_data(self, layer_id, what):
        """A source that names no data of its own: the given --data, or a path that says so (`pack` refuses it until it is set)."""
        if self.data:
            return {'path': self.data, **({'layer': self.data_layer} if self.data_layer else {})}
        self.note('untranslated', 'no-data', f'{what} names no data: data.path is a placeholder to replace (or give --data)', {'layer': layer_id}, 'data')
        return {'path': f'{UNBOUND}/{layer_id}'}

    def add_layer(self, layer_id, geometry, renderer, data, *, title=None, labels=None, **extra):
        """Write a layer and, when a symbol needed more passes, one more layer per pass, above it."""
        extra = {k: v for k, v in extra.items() if v is not None}
        for depth in range(max(pass_count(renderer), 1)):
            drawn = render_pass(renderer, depth)
            if drawn is None and renderer is not None:
                self.note('untranslated', 'renderer', f'pass {depth + 1} holds only the default symbol, which map-style cannot hang on a layer of its own: left out', {'layer': layer_id}, 'renderer.default')
                continue
            out = {'id': layer_id if depth == 0 else self.unique_id(f'{layer_id}-{depth + 1}'), **({'title': title} if title else {}),
                   'geometry': geometry, 'data': data, 'renderer': drawn, **extra}
            if depth:
                out['legend'] = {'show': False}
            self.layers.append(out)
        if labels:
            self.layers[-1]['labels'] = labels
        return self.layers[-1]

    def document(self):
        document = {'contract': _map_style.CONTRACT, 'schema_version': _map_style.SCHEMA_VERSION,
                    'id': re.sub(r'[^\w.-]+', '-', self.source.stem).strip('-.') or 'style', 'title': self.source.stem,
                    'producer': {'engine': 'gis-kit', 'version': VERSION, 'map': self.source.name}, 'units': {'size': 'px'}, 'dataRoot': 'file'}
        document['layers'] = self.layers
        document['notes'] = self.notes
        return document


def pass_count(renderer):
    if renderer is None or renderer['type'] in _map_style.RASTER_RENDERERS:
        return 1
    if renderer['type'] == 'single':
        return len(renderer['passes'])
    items = renderer.get('classes') or renderer.get('rules') or []
    return max([len(item['passes']) for item in items] + [len(renderer.get('default') or [])])


def render_pass(renderer, depth):
    """The map-style renderer of one pass; on a later pass only the classes that have one (the rest draw nothing there)."""
    if renderer is None or renderer['type'] in _map_style.RASTER_RENDERERS:
        return renderer
    if renderer['type'] == 'single':
        return {'type': 'single', 'symbol': renderer['passes'][depth]}
    out = {k: v for k, v in renderer.items() if k not in ('classes', 'rules', 'default')}
    holder = 'rules' if renderer['type'] == 'rule' else 'classes'
    out[holder] = [{**{k: v for k, v in item.items() if k != 'passes'}, 'symbol': item['passes'][depth]}
                   for item in renderer[holder] if depth < len(item['passes'])]
    default = renderer.get('default') or []
    out['default'] = default[depth] if depth < len(default) else None
    return out if out[holder] else None


# --- the command ---------------------------------------------------------------------------------------------------

def add_command(commands):
    sub = commands.add_parser('read', help='Read a platform style (.qml, .lyrx, .stylx) into a map-style document')
    sub.add_argument('source', help='Style file: QGIS .qml, ArcGIS Pro .lyrx or .stylx')
    sub.add_argument('--output', required=True, help='New directory for the map-style, assets/, style-read.json and record.json')
    sub.add_argument('--data', help='Data path relative to the output folder or @root/..., for a source that names none (.qml, .stylx) or one layer of a .lyrx')
    sub.add_argument('--data-layer', help='Layer inside --data')
    sub.add_argument('--field', help='.stylx: the data field that holds the symbol names (default: name)')
    sub.add_argument('--match', choices=('name', 'key'), default='name', help='.stylx: match features by the symbol name or its key (default: name)')
    sub.add_argument('--font-dir', help='Folder of fonts that text in markers may be outlined with, besides the installed ones')


def run(args):
    source = Path(args.source)
    if not source.is_file():
        raise ValueError(f'Style file {source} not found')
    entry = READERS.get(source.suffix.lower())
    if entry is None:
        raise ValueError(f'{source.name}: reads {", ".join(sorted(READERS))}')
    if args.data:
        _map_style.check_reference(args.data, '--data', '--data')
    if args.field and source.suffix.lower() != '.stylx':
        raise ValueError('--field belongs to a .stylx: other sources name their own fields')
    reading = Reading(source, data=args.data, data_layer=args.data_layer, field=args.field, match=args.match, font_dir=args.font_dir)
    getattr(importlib.import_module(entry[0]), entry[1])(source, reading)
    if not reading.layers:
        raise ValueError(f'{source.name}: nothing in it could be read as a map-style layer')
    document = reading.document()
    with bundle(args.output) as stage:
        style_file = stage / f'{document["id"]}.map-style.json'
        for name, data in reading.assets.items():
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
            (stage / name).write_bytes(data)
        write_json(style_file, document)
        _map_style.load(style_file)
        report = report_of(reading, source, document, style_file)
        write_json(stage / 'style-read.json', report)
        write_json(stage / 'record.json', {
            'schema_version': 1, 'status': 'complete', 'tool': 'package read', 'version': VERSION,
            'parameters': {k: v for k, v in vars(args).items() if k not in ('output', 'command', 'source')},
            'source': {'path': str(source.absolute()), 'sha256': digest(source)},
            'files': {p.relative_to(stage).as_posix(): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()},
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    return {'output': str(Path(args.output).absolute()), 'map_style': style_file.name, 'layers': len(document['layers']),
            'symbols': report['symbols'], 'losses': report['losses'], 'notes': len(document['notes']), 'images': len(reading.assets)}


def report_of(reading, source, document, style_file):
    """style-read.json: what was read, and how much of it survived, as counts."""
    total = sum(reading.symbols.values())
    return {'manifest': 'style-read', 'schema_version': 1, 'source': {'file': source.name, 'format': source.suffix.lower().lstrip('.'), 'sha256': digest(source)},
            'map_style': {'file': style_file.name, 'sha256': digest(style_file), 'id': document['id']},
            'symbols': {'total': total, **{level: reading.symbols.get(level, 0) for level in LEVELS}},
            'losses': dict(sorted(reading.losses.items(), key=lambda item: -item[1])), **reading.extra,
            'layers': [{'id': layer['id'], 'geometry': layer['geometry'], 'renderer': (layer['renderer'] or {}).get('type'),
                        'classes': len((layer['renderer'] or {}).get('classes') or (layer['renderer'] or {}).get('rules') or (layer['renderer'] or {}).get('stops') or [])}
                       for layer in document['layers']],
            'images': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in sorted(reading.assets.items())],
            'fonts': {'outlined': sorted(reading.fonts_outlined), 'missing': sorted(reading.fonts_missing)},
            'notes': len(document['notes'])}
