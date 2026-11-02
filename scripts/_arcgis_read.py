"""ArcGIS Pro style (.lyrx, .stylx: CIM JSON) -> map-style v1: the reader for `package.py read`, see _style_read.py for how readers fit.

Reads what Pro saves and what `_arcgis_style.py` writes. CIM conventions: sizes in points, symbol layers and layers
listed top first (the graphics inside a marker bottom first), colours with alpha 0-100 (or [r, g, b, a] with alpha 0-255), marker rotation counter-clockwise, a marker's size the height of its
frame, geometry in frame units; in a marker placed along a line +x runs along the line. A .stylx is a SQLite file:
CLASSES names the kinds of item, ITEMS holds them with the CIM JSON in CONTENT, each with a NUL after it.

A .stylx is a library, not a map, and map-style has no library. It is read as what the libraries are used for: one
`categorized` layer per geometry, each symbol a class whose value is its name (or key), so a layer whose field holds
those names draws with the library's symbols.
"""
from __future__ import annotations

import base64
import colorsys
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
from urllib.parse import quote

import numpy as np

from _arcgis_style import CROSS, PATHS, RINGS, turned
from _map_style import svg_size
from _style_read import PX_PER_PT, arc_segments, hex_color, line, paint, parse_filter, rules_renderer, tidy, transform_op

CAPS = {'Butt': 'butt', 'Round': 'round', 'Square': 'square'}
JOINS = {'Miter': 'miter', 'Round': 'round', 'Bevel': 'bevel'}
SYMBOL_GEOMETRY = {'CIMPolygonSymbol': 'polygon', 'CIMLineSymbol': 'line', 'CIMPointSymbol': 'point'}
GROUPS = {'ground', 'groundLines', 'fills', 'lines', 'reference', 'points', 'labels'}
METHODS = {'Manual': 'manual', 'EqualInterval': 'equal', 'Quantile': 'quantile', 'NaturalBreaks': 'jenks'}
STYLE_WEIGHTS = {'thin': 100, 'extralight': 200, 'light': 300, 'regular': 400, 'normal': 400, 'medium': 500, 'semibold': 600, 'demibold': 600, 'bold': 700,
                 'extrabold': 800, 'black': 900, 'heavy': 900}
SYMBOL_CLASSES = {3, 4, 5}


def px(points):
    return float(points) * PX_PER_PT


def color_of(node, where, reading):
    """(#RRGGBB, opacity) from any CIM colour; other models than RGB and gray are converted, which is reported."""
    if not node:
        return '#000000', 0.0
    if isinstance(node, list):  # the short form in Pro's own style files: [r, g, b, a], alpha 0-255
        return hex_color(*node[:3]), round(float(node[3]) / 255, 4) if len(node) > 3 else 1.0
    kind, values = node.get('type'), list(node.get('values') or [])
    opacity = lambda index: float(values[index]) / 100 if len(values) > index else 1.0  # noqa: E731
    if kind == 'CIMRGBColor' and len(values) >= 3:
        return hex_color(*values[:3]), opacity(3)
    if kind == 'CIMGrayColor' and values:
        return hex_color(values[0], values[0], values[0]), opacity(1)
    if kind == 'CIMHSVColor' and len(values) >= 3:
        rgb = colorsys.hsv_to_rgb(values[0] / 360, values[1] / 100, values[2] / 100)
        return hex_color(*(v * 255 for v in rgb)), opacity(3)
    if kind == 'CIMHSLColor' and len(values) >= 3:
        rgb = colorsys.hls_to_rgb(values[0] / 360, values[2] / 100, values[1] / 100)
        return hex_color(*(v * 255 for v in rgb)), opacity(3)
    if kind == 'CIMCMYKColor' and len(values) >= 4:
        c, m, y, k = (v / 100 for v in values[:4])
        reading.note('approximated', 'color-model', 'a CMYK colour is converted to RGB without a colour profile', where, 'symbol')
        return hex_color(255 * (1 - c) * (1 - k), 255 * (1 - m) * (1 - k), 255 * (1 - y) * (1 - k)), opacity(4)
    if kind == 'CIMLABColor' and len(values) >= 3:
        reading.note('approximated', 'color-model', 'a Lab colour is converted to sRGB without a colour profile', where, 'symbol')
        return lab_to_hex(*values[:3]), opacity(3)
    reading.note('approximated', 'color-model', f'the colour {kind} is not read: black stands in', where, 'symbol')
    return '#000000', 1.0


def lab_to_hex(lightness, a, b):
    fy = (lightness + 16) / 116
    x, y, z = (0.95047 * cube(fy + a / 500), cube(fy), 1.08883 * cube(fy - b / 200))
    linear = (3.2406 * x - 1.5372 * y - 0.4986 * z, -0.9689 * x + 1.8758 * y + 0.0415 * z, 0.0557 * x - 0.2040 * y + 1.0570 * z)
    return hex_color(*(255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055) for c in (max(0, v) for v in linear)))


def cube(t):
    return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)


# --- geometry ------------------------------------------------------------------------------------------------------

def ring_segments(ring, closed):
    """A ring or path of an Esri JSON curve geometry: points, and {a: arc}, {c: three-point arc}, {b: cubic}."""
    segments, last = [], None
    for item in ring:
        if isinstance(item, dict):
            if 'b' in item:
                end, one, two = item['b'][:3]
                segments.append(('C', tuple(one[:2]), tuple(two[:2]), tuple(end[:2])))
                last = tuple(end[:2])
            elif 'c' in item:
                end, through = item['c'][:2]
                segments += three_point_arc(last, tuple(through[:2]), tuple(end[:2]))
                last = tuple(end[:2])
            elif 'a' in item:
                arc = item['a']
                end, center = tuple(arc[0][:2]), tuple(arc[1][:2])
                clockwise = bool(arc[3]) if len(arc) > 3 else False
                if len(arc) > 6 and arc[6] not in (1, 1.0):  # an elliptic arc: its ends are kept, the curve is a straight run
                    segments.append(('L', end))
                else:
                    segments += arc_segments(last, center, end, not clockwise)
                last = end
            else:
                raise ValueError(f'unknown curve segment {sorted(item)}')
        else:
            point = tuple(item[:2])
            segments.append(('M' if last is None else 'L', point))
            last = point
    if closed:
        segments.append(('Z',))
    return segments


def three_point_arc(start, through, end):
    ax, ay = start
    bx, by = through
    cx, cy = end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [('L', end)]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    center = ((a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d, (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d)
    ccw = ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) > 0
    return arc_segments(start, center, end, ccw)


def geometry_segments(geometry):
    """Segments of a marker graphic's geometry, or None for a point."""
    out = []
    for key, closed in (('rings', True), ('curveRings', True), ('paths', False), ('curvePaths', False)):
        for ring in geometry.get(key) or []:
            if ring:
                out += ring_segments(ring, closed)
    return out or None


def flat_points(segments):
    """Points along the segments, curves sampled, to recognise a shape by."""
    points, last = [], None
    for seg in segments:
        if seg[0] in ('M', 'L'):
            points.append(seg[1])
            last = seg[1]
        elif seg[0] == 'C':
            for t in (0.25, 0.5, 0.75, 1.0):
                u = 1 - t
                points.append(tuple(u ** 3 * last[i] + 3 * u * u * t * seg[1][i] + 3 * u * t * t * seg[2][i] + t ** 3 * seg[3][i] for i in (0, 1)))
            last = seg[3]
    return points


def near(points, template, tolerance):
    if len(points) != len(template):
        return False
    return any(all(math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance for a, b in zip(points, template[start:] + template[:start])) for start in range(len(template)))


def plain_points(ring):
    """A ring without curves, without the repeated last point."""
    if any(isinstance(p, dict) for p in ring):
        return None
    points = [tuple(p[:2]) for p in ring]
    return points[:-1] if len(points) > 1 and points[0] == points[-1] else points


def recognise(geometry, frame):
    """(map-style shape, how much of the frame's height it fills) for a graphic that plainly is one of the named shapes, else None.

    A shape is judged by its corners, or for a circle by its distance from the centre. What the writers make (frame -2..2) is also
    compared as written, since a cross turned into an x does not reach the frame."""
    rings, paths = geometry.get('rings') or [], geometry.get('paths') or []
    height = frame['ymax'] - frame['ymin']
    if height <= 0:
        return None
    if paths and not rings and not geometry.get('curveRings'):
        as_written = [[tuple(p[:2]) for p in path] for path in paths]
        for shape, template in PATHS.items():
            template = [[tuple(p) for p in path] for path in template]
            if len(as_written) == len(template) and all(near(a, t, 1e-3) or near(a[::-1], t, 1e-3) for a, t in zip(as_written, template)):
                return shape, 1.0
        return None
    if len(rings) == 1 and not geometry.get('curveRings'):
        points = plain_points(rings[0])
        if points is None:
            return None
        for shape, angle in (('cross', 0), ('x', 45)):
            if near(points, [tuple(p) for p in turned(CROSS, angle)[:-1]], 1e-2):
                return shape, 1.0
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        cx, cy, w, h = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)
        frame_cx, frame_cy = (frame['xmin'] + frame['xmax']) / 2, (frame['ymin'] + frame['ymax']) / 2
        if h <= 0 or abs(w - h) > 0.02 * h or math.hypot(cx - frame_cx, cy - frame_cy) > 0.02 * h:
            return None
        normal = [((x - cx) * 4 / h, (y - cy) * 4 / h) for x, y in points]
        for shape in ('square', 'triangle', 'diamond'):
            if near(normal, [tuple(p) for p in RINGS[shape][:-1]], 0.08) or near(normal[::-1], [tuple(p) for p in RINGS[shape][:-1]], 0.08):
                return shape, h / height
        return None
    curves = geometry.get('curveRings') or []
    if len(curves) == 1 and not rings:
        try:
            points = flat_points(ring_segments(curves[0], True))
        except (ValueError, TypeError, IndexError):
            return None
        if len(points) < 3:
            return None
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        distances = [math.hypot(p[0] - cx, p[1] - cy) for p in points]
        radius = sum(distances) / len(distances)
        frame_cx, frame_cy = (frame['xmin'] + frame['xmax']) / 2, (frame['ymin'] + frame['ymax']) / 2
        if radius > 0 and max(abs(d - radius) for d in distances) <= 0.015 * radius and math.hypot(cx - frame_cx, cy - frame_cy) <= 0.02 * radius * 2:
            return 'circle', round(2 * radius / height, 3)  # sampled, so good to a little
    return None


# --- symbol layers -------------------------------------------------------------------------------------------------

class Symbols:
    """Turns CIM symbols into parts for one reading."""

    def __init__(self, reading):
        self.reading = reading

    def effects(self, layer, where, keep_dash=True):
        """A stroke's dash pattern; other geometric effects are reported, and the line drawn plainly."""
        dash = None
        for effect in layer.get('effects') or []:
            kind = effect.get('type')
            if kind == 'CIMGeometricEffectDashes' and keep_dash and dash is None:
                dash = [px(v) for v in effect.get('dashTemplate') or []] or None
                if effect.get('lineDashEnding', 'NoConstraint') not in ('NoConstraint',) or effect.get('offsetAlongLine'):
                    self.reading.note('approximated', 'dash-ending', 'map-style dashes start at the line\'s start: how the pattern fits the ends is not kept', where, 'symbol.stroke.dash')
            else:
                self.reading.note('untranslated', 'geometric-effect', f'the geometric effect {kind} is not carried: the shape is drawn without it', where, 'symbol')
        return dash

    def stroke(self, layer, where):
        kind = layer.get('type')
        if kind != 'CIMSolidStroke':
            value, opacity = self.ramp_colour(layer, where)
            self.reading.note('approximated', 'gradient', f'the {kind} stroke is written as one solid colour', where, 'symbol.stroke')
        else:
            value, opacity = color_of(layer.get('color'), where, self.reading)
        return line(value, px(layer.get('width', 1)), opacity, self.effects(layer, where), CAPS.get(layer.get('capStyle'), 'butt'), JOINS.get(layer.get('joinStyle'), 'miter'))

    def ramp_colour(self, layer, where):
        ramp = layer.get('colorRamp') or {}
        first = ramp.get('fromColor') or ramp.get('color1') or layer.get('color') or next(iter(ramp.get('colors') or []), None)
        return color_of(first, where, self.reading)

    def hatch(self, layer, where):
        strokes = [item for item in (layer.get('lineSymbol') or {}).get('symbolLayers') or [] if item.get('enable', True) and 'Stroke' in item.get('type', '')]
        if not strokes:
            return None
        stroke = self.stroke(strokes[0], where)
        if len(strokes) > 1 or stroke.get('dash'):
            self.reading.note('approximated', 'hatch-line', 'the lines of a hatch fill are written as one solid line', where, 'symbol.fill.pattern')
        if layer.get('offsetX') or layer.get('offsetY'):
            self.reading.note('approximated', 'offset', 'a hatch offset is not carried', where, 'symbol.fill.pattern')
        pattern = {'kind': 'hatch', 'angle': tidy(float(layer.get('rotation', 0))), 'spacing': tidy(px(layer.get('separation', 5))), 'width': stroke['width'], 'color': stroke['color']}
        if 'opacity' in stroke:
            pattern['opacity'] = stroke['opacity']
        return pattern

    def parts(self, symbol, geometry, where, overrides=None):
        """A CIM symbol's parts, bottom first. overrides: a symbol reference's property connections."""
        out = []
        field = self.rotation_override(overrides, where)
        for layer in reversed(symbol.get('symbolLayers') or []):
            if not layer.get('enable', True):
                continue
            kind = layer.get('type', '')
            if kind == 'CIMSolidFill':
                value, opacity = color_of(layer.get('color'), where, self.reading)
                self.effects({'effects': layer.get('effects')}, where, keep_dash=False)
                out.append({'kind': 'fill', 'color': value, 'opacity': opacity})
            elif kind == 'CIMHatchFill':
                pattern = self.hatch(layer, where)
                if pattern:
                    out.append({'kind': 'pattern', 'pattern': pattern})
            elif kind in ('CIMGradientFill', 'CIMWaterFill'):
                value, opacity = self.ramp_colour(layer, where)
                self.reading.note('approximated', 'gradient', f'the {kind} is written as a solid fill', where, 'symbol.fill')
                out.append({'kind': 'fill', 'color': value, 'opacity': opacity})
            elif kind == 'CIMPictureFill':
                self.reading.note('untranslated', 'picture-fill', 'a picture fill has no map-style form: left out', where, 'symbol.fill')
            elif 'Stroke' in kind:
                out.append({'kind': 'stroke', 'stroke': self.stroke(layer, where)})
            elif kind in ('CIMVectorMarker', 'CIMPictureMarker', 'CIMCharacterMarker'):
                part = self.marker(layer, geometry, where)
                if part:
                    part['rotationField'] = field
                    out.append(part)
            else:
                self.reading.note('untranslated', 'symbol-layer', f'symbol layer {kind} has no map-style form: left out', where, 'symbol')
        if symbol.get('haloSymbol') and symbol.get('haloSize'):
            self.reading.note('untranslated', 'marker-halo', 'a halo around a point symbol is not carried', where, 'symbol')
        return out

    def rotation_override(self, overrides, where):
        found = None
        for item in overrides or []:
            text = (item.get('valueExpressionInfo') or {}).get('expression') or ''
            match = re.search(r'\$feature\["((?:[^"\\]|\\.)+)"\]|\$feature\.(\w+)', text)
            if item.get('propertyName') == 'Rotation' and match:
                found = json.loads('"' + match[1] + '"') if match[1] else match[2]
            else:
                self.reading.note('untranslated', 'data-defined', f'the connection of {item.get("propertyName")} to the data is not carried: the static value is used', where, 'symbol')
        return found

    def placement(self, layer, geometry, where):
        """(place for the part, or None; False to leave the marker out)."""
        placement = layer.get('markerPlacement')
        kind = (placement or {}).get('type')
        if geometry == 'point':
            if placement:
                self.reading.note('approximated', 'line-marker-position', f'{kind} on a point symbol is ignored', where, 'symbol.marker')
            return None
        if kind == 'CIMMarkerPlacementAlongLineSameSize':
            template = placement.get('placementTemplate') or []
            if not template or sum(template) <= 0:
                self.reading.note('untranslated', 'line-marker-position', 'markers along a line with no spacing: left out', where, 'symbol.marker')
                return False
            spacing = sum(template)
            if len(template) > 1:
                self.reading.note('approximated', 'line-marker-position', f'an uneven marker template {template} is written with one spacing, its sum', where, 'symbol.marker.spacing')
            if placement.get('endings', 'NoConstraint') != 'NoConstraint' or abs(float(placement.get('offsetAlongLine', 0)) - spacing / 2) > 1e-3:
                self.reading.note('approximated', 'line-marker-start', 'map-style starts markers half a spacing from the line\'s start: how they meet the ends is not kept', where, 'symbol.marker')
            if placement.get('offset') or placement.get('perpendicularOffset'):
                self.reading.note('approximated', 'offset', 'markers beside a line are drawn on it', where, 'symbol.marker')
            return {'placement': 'line', 'spacing': tidy(px(spacing)), 'alongLine': bool(placement.get('angleToLine', True))}
        if kind == 'CIMMarkerPlacementPolygonCenter':
            self.reading.note('untranslated', 'polygon-center-marker', 'a marker at the polygon\'s centre has no map-style form: left out, the fill is kept', where, 'symbol.marker')
        elif kind == 'CIMMarkerPlacementInsidePolygon':
            self.reading.note('untranslated', 'marker-fill', 'a fill of repeated markers has no map-style form: left out', where, 'symbol.marker')
        else:
            self.reading.note('untranslated', 'line-marker-position', f'markers placed by {kind} have no map-style form: left out', where, 'symbol.marker')
        return False

    def marker(self, layer, geometry, where):
        """A marker part, with its drawing operations; None when it cannot be placed."""
        place = self.placement(layer, geometry, where)
        if place is False:
            return None
        along = bool(place and place.get('alongLine'))
        kind = layer.get('type')
        rotation = float(layer.get('rotation', 0) or 0)
        ops = self.marker_ops(layer, where, top=True)
        if not ops:
            self.reading.note('untranslated', 'nothing-drawn', f'a {kind} with nothing to draw is left out', where, 'symbol.marker')
            return None
        part = {'kind': 'marker', 'place': place, 'ops': ops, 'marker': None}
        if kind == 'CIMPictureMarker':
            marker = self.picture_as_svg(layer, where)
            if marker:
                part['marker'] = {**marker, 'rotation': -rotation - (90 if along else 0)}
            return part
        simple = self.simple_shape(layer, where)
        if simple:
            shape, size, fill, stroke = simple
            part.update(marker={'shape': shape, 'size': tidy(size), 'rotation': -rotation - (90 if along else 0)}, fill=fill, stroke=stroke)
        return part

    def simple_shape(self, layer, where):
        """(shape, size px, fill, stroke) of a vector marker that one map-style marker draws exactly, else None."""
        graphics = layer.get('markerGraphics') or []
        frame = layer.get('frame')
        if layer.get('type') != 'CIMVectorMarker' or len(graphics) != 1 or not frame or layer.get('offsetX') or layer.get('offsetY') or marker_anchor(layer) != (0.0, 0.0):
            return None
        symbol = graphics[0].get('symbol') or {}
        if symbol.get('type') not in ('CIMPolygonSymbol', 'CIMLineSymbol') or graphics[0].get('textString') is not None:
            return None
        layers = [item for item in symbol.get('symbolLayers') or [] if item.get('enable', True)]
        if any(item.get('type') not in ('CIMSolidFill', 'CIMSolidStroke') or item.get('effects') for item in layers):
            return None
        fills = [item for item in layers if item['type'] == 'CIMSolidFill']
        strokes = [item for item in layers if item['type'] == 'CIMSolidStroke']
        if len(fills) > 1 or len(strokes) > 1 or (fills and strokes and layers.index(strokes[0]) > layers.index(fills[0])):
            return None
        found = recognise(graphics[0].get('geometry') or {}, frame)
        if not found:
            return None
        shape, share = found
        scale = float(layer.get('size', 6)) / (frame['ymax'] - frame['ymin'])
        stroke = None
        if strokes:
            value, opacity = color_of(strokes[0].get('color'), where, self.reading)
            width = float(strokes[0].get('width', 1)) * (scale if layer.get('scaleSymbolsProportionally') else 1)
            stroke = line(value, px(width), opacity, None, CAPS.get(strokes[0].get('capStyle'), 'butt'), JOINS.get(strokes[0].get('joinStyle'), 'miter')) if opacity > 0 else None
        fill = None
        if fills:
            value, opacity = color_of(fills[0].get('color'), where, self.reading)
            fill = paint(value, opacity) if opacity > 0 else None
        size = px(float(layer.get('size', 6)) * share)
        if shape == 'circle' and not fill and stroke:
            return 'ring', size, None, stroke
        if shape == 'chevron' and fill:
            return None
        return shape, size, fill, stroke

    def picture_data(self, layer, where):
        """(media type, bytes) of a picture marker whose image is inside the style; anything else is reported."""
        url = layer.get('url') or ''
        found = re.match(r'data:([\w/+.-]+);base64,', url)
        if not found:
            self.reading.note('untranslated', 'picture-marker', 'the marker\'s picture is outside the style: left out', where, 'symbol.marker')
            return None, None
        try:
            return found[1], base64.b64decode(url[found.end():])
        except ValueError:
            self.reading.note('untranslated', 'picture-marker', 'the marker\'s picture cannot be decoded: left out', where, 'symbol.marker')
            return None, None

    def picture_as_svg(self, layer, where):
        """A picture marker holding an SVG, as the writer makes it: the same image as marker.svg."""
        kind, data = self.picture_data(layer, where)
        if kind != 'image/svg+xml' or layer.get('offsetX') or layer.get('offsetY'):
            return None
        try:
            width, height = svg_size(data.decode('utf-8'), 'picture marker')
        except (ValueError, UnicodeDecodeError) as error:
            self.reading.note('untranslated', 'picture-marker', f'the marker\'s SVG cannot be read: {error}', where, 'symbol.marker')
            return None
        share = min(1.0, height / width)  # CIM's size is the height, map-style's the longer side
        return {'shape': 'svg', 'size': tidy(px(layer.get('size', 6)) / share), 'svg': self.reading.store_svg(data)}

    def marker_ops(self, layer, where, top=False):
        """A marker layer as drawing operations in its parent's units (points at the top, then px), the anchor at the origin."""
        kind = layer.get('type')
        if kind == 'CIMVectorMarker':
            ops = self.vector_ops(layer, where)
        elif kind == 'CIMPictureMarker':
            ops = self.picture_ops(layer, where)
        elif kind == 'CIMCharacterMarker':
            ops = self.character_ops(layer, where)
        else:
            self.reading.note('untranslated', 'symbol-layer', f'marker layer {kind} has no map-style form: left out', where, 'symbol.marker')
            return []
        turn = float(layer.get('rotation', 0) or 0)
        shift = (float(layer.get('offsetX', 0) or 0), float(layer.get('offsetY', 0) or 0))
        moved = [transform_op(op, turn=turn, shift=shift, scale_lines=False) for op in ops]
        return [transform_op(op, scale=PX_PER_PT) for op in moved] if top else moved

    def vector_ops(self, layer, where):
        frame = layer.get('frame') or {'xmin': -2, 'ymin': -2, 'xmax': 2, 'ymax': 2}
        height = frame['ymax'] - frame['ymin'] or 4
        scale = float(layer.get('size', 6)) / height
        center = ((frame['xmin'] + frame['xmax']) / 2, (frame['ymin'] + frame['ymax']) / 2)
        anchor = marker_anchor(layer)
        relative = layer.get('anchorPointUnits') != 'Absolute'
        width = frame['xmax'] - frame['xmin']
        offset = (anchor[0] * width * scale, anchor[1] * height * scale) if relative else anchor
        ops = []
        proportional = bool(layer.get('scaleSymbolsProportionally'))
        for graphic in layer.get('markerGraphics') or []:  # the first is drawn first, unlike symbol layers (checked in Pro 3.4)
            local = self.graphic_ops(graphic, where)
            for op in local:
                moved = transform_op(op, shift=(-center[0], -center[1]), scale_lines=False)
                ops.append(transform_op(transform_op(moved, scale=scale, scale_lines=proportional), shift=(-offset[0], -offset[1]), scale_lines=False))
        clip = layer.get('clippingPath')
        if clip:
            self.check_clip(clip, ops, center, scale, offset, where)
        return ops

    def check_clip(self, clip, ops, center, scale, offset, where):
        """Clipping is not drawn: every renderer of SVG markers would treat it differently. It only matters when it cuts something."""
        rings = (clip.get('path') or {}).get('rings') or []
        if clip.get('clippingType', 'Intersect') == 'Intersect' and len(rings) == 1:
            xs = [p[0] for p in rings[0]]
            ys = [p[1] for p in rings[0]]
            if len(rings[0]) == 5 and len(set(xs)) == 2 and len(set(ys)) == 2:
                low = ((min(xs) - center[0]) * scale - offset[0], (min(ys) - center[1]) * scale - offset[1])
                high = ((max(xs) - center[0]) * scale - offset[0], (max(ys) - center[1]) * scale - offset[1])
                margin = 0.01 * max(high[0] - low[0], high[1] - low[1])
                points = [point for op in ops for point in flat_points(op.get('d') or [])]
                if all(low[0] - margin <= x <= high[0] + margin and low[1] - margin <= y <= high[1] + margin for x, y in points):
                    return
        self.reading.note('approximated', 'marker-clip', 'a clipping path that cuts the marker is not applied: the marker is drawn whole', where, 'symbol.marker')

    def graphic_ops(self, graphic, where):
        symbol = graphic.get('symbol') or {}
        geometry = graphic.get('geometry') or {}
        kind = symbol.get('type')
        if kind == 'CIMTextSymbol' or graphic.get('textString') is not None:
            return self.text_graphic(graphic, symbol, geometry, where)
        if kind == 'CIMPointSymbol':  # a marker inside a marker, at a point of the frame
            at = (float(geometry.get('x', 0) or 0), float(geometry.get('y', 0) or 0))
            ops = []
            for layer in reversed(symbol.get('symbolLayers') or []):
                if layer.get('enable', True):
                    if layer.get('markerPlacement'):
                        self.reading.note('approximated', 'line-marker-position', 'a placement inside a marker is ignored', where, 'symbol.marker')
                    ops += [transform_op(op, shift=at, scale_lines=False) for op in self.marker_ops(layer, where)]
            return ops
        try:
            segments = geometry_segments(geometry)
        except (ValueError, TypeError, IndexError) as error:
            self.reading.note('untranslated', 'marker-geometry', f'a marker graphic\'s geometry cannot be read ({error}): left out', where, 'symbol.marker')
            return []
        if not segments:
            return []
        ops = []
        for layer in reversed(symbol.get('symbolLayers') or []):
            if not layer.get('enable', True):
                continue
            kind = layer.get('type', '')
            if kind == 'CIMSolidFill':
                value, opacity = color_of(layer.get('color'), where, self.reading)
                ops.append({'d': segments, 'fill': paint(value, opacity), 'stroke': None})
            elif 'Stroke' in kind:
                ops.append({'d': segments, 'fill': None, 'stroke': self.marker_stroke(layer, where)})
            elif kind in ('CIMVectorMarker', 'CIMPictureMarker', 'CIMCharacterMarker'):
                self.reading.note('untranslated', 'marker-geometry', 'markers on a marker graphic\'s outline are left out', where, 'symbol.marker')
            else:
                self.reading.note('approximated', 'gradient', f'the {kind} in a marker graphic is left out', where, 'symbol.marker')
        return ops

    def marker_stroke(self, layer, where):
        """A stroke in a marker, in points for now (the marker scales it): the dash stays in the same units."""
        value, opacity = color_of(layer.get('color'), where, self.reading)
        dash = None
        for effect in layer.get('effects') or []:
            if effect.get('type') == 'CIMGeometricEffectDashes':
                dash = list(effect.get('dashTemplate') or []) or None
            else:
                self.reading.note('untranslated', 'geometric-effect', f'the geometric effect {effect.get("type")} in a marker is not carried', where, 'symbol.marker')
        return {**paint(value, opacity), 'width': float(layer.get('width', 1)), **({'dash': dash} if dash else {}), 'cap': CAPS.get(layer.get('capStyle'), 'butt'),
                'join': JOINS.get(layer.get('joinStyle'), 'miter')}

    def text_graphic(self, graphic, symbol, geometry, where):
        text = graphic.get('textString') or ''
        if not text.strip():
            return []
        at = (float(geometry.get('x', 0) or 0), float(geometry.get('y', 0) or 0))
        fill = None
        for layer in (symbol.get('symbol') or {}).get('symbolLayers') or []:
            if layer.get('type') == 'CIMSolidFill':
                value, opacity = color_of(layer.get('color'), where, self.reading)
                fill = paint(value, opacity)
        if symbol.get('haloSize') and symbol.get('haloSymbol'):
            self.reading.note('untranslated', 'marker-halo', 'a halo on text in a marker is not carried', where, 'symbol.marker')
        anchor = {'Left': 'start', 'Right': 'end'}.get(symbol.get('horizontalAlignment'), 'middle')
        size = float(symbol.get('height', 10) or 10)
        return self.reading.text_ops(text, symbol.get('fontFamilyName'), symbol.get('fontStyleName'), size, at[0], at[1], fill or paint('#000000'), where, anchor=anchor, turn=float(symbol.get('angle', 0) or 0))

    def picture_ops(self, layer, where):
        kind, data = self.picture_data(layer, where)
        if data is None:
            return []
        height = float(layer.get('size', 6))
        ratio = float(layer.get('scaleX', 1) or 1)
        if kind == 'image/png' and data[:8] == b'\x89PNG\r\n\x1a\n':
            width_px, height_px = struct.unpack('>II', data[16:24])
            ratio *= width_px / max(height_px, 1)
        elif kind == 'image/svg+xml':
            try:
                width_px, height_px = svg_size(data.decode('utf-8'), 'picture marker')
                ratio *= width_px / height_px
            except (ValueError, UnicodeDecodeError):
                pass
        tint = (layer.get('tintColor') or {}).get('values')
        if tint and list(tint[:3]) != [255, 255, 255]:
            self.reading.note('untranslated', 'picture-marker', 'a tint on a picture marker is not carried', where, 'symbol.marker')
        return [{'image': f'data:{kind};base64,' + base64.b64encode(data).decode('ascii'), 'x': 0.0, 'y': 0.0, 'width': height * ratio, 'height': height}]

    def character_ops(self, layer, where):
        fill = None
        for item in (layer.get('symbol') or {}).get('symbolLayers') or []:
            if item.get('type') == 'CIMSolidFill':
                value, opacity = color_of(item.get('color'), where, self.reading)
                fill = paint(value, opacity)
        return self.reading.text_ops(chr(int(layer.get('characterIndex', 63))), layer.get('fontFamilyName'), layer.get('fontStyleName'), float(layer.get('size', 10)), 0.0, 0.0,
                                     fill or paint('#000000'), where)


def marker_anchor(layer):
    point = layer.get('anchorPoint') or {}
    return float(point.get('x', 0) or 0), float(point.get('y', 0) or 0)


# --- renderers -----------------------------------------------------------------------------------------------------

def first_symbol(renderer):
    """The first CIM symbol in a renderer, which tells the layer's geometry."""
    if not isinstance(renderer, dict):
        return None
    reference = renderer.get('symbol') or renderer.get('defaultSymbol')
    if reference:
        return reference.get('symbol') or {}
    for group in renderer.get('groups') or []:
        for item in group.get('classes') or []:
            return (item.get('symbol') or {}).get('symbol') or {}
    for item in renderer.get('breaks') or []:
        return (item.get('symbol') or {}).get('symbol') or {}
    return None


def below_single(value):
    return float(np.nextafter(np.float32(value), np.float32(-np.inf)))


def meant_bound(upper):
    """A class's upper bound as it was meant: the writer steps it one single-precision number below the next class's start."""
    candidate = float(f'{float(np.nextafter(np.float32(upper), np.float32(np.inf))):.7g}')
    return candidate if below_single(candidate) == upper else upper


def tidy_number(value):
    value = float(value)
    return int(value) if value.is_integer() and abs(value) < 1e15 else value


class Layer:
    """One CIM layer definition being read."""

    def __init__(self, reading, definition, layer_id):
        self.reading, self.definition, self.layer_id = reading, definition, layer_id
        self.where = {'layer': layer_id}
        self.symbols = Symbols(reading)

    def passes(self, reference, geometry, where):
        reference = reference or {}
        symbol = reference.get('symbol') or {}

        def convert():
            if reference.get('minScale') or reference.get('maxScale'):
                self.reading.note('untranslated', 'scale-range', 'a symbol\'s scale range is not carried', where, 'symbol')
            return self.symbols.parts(symbol, geometry, where, reference.get('primitiveOverrides'))

        return self.reading.symbol(geometry, where, convert)

    def renderer(self, geometry):
        spec = self.definition.get('renderer') or {}
        kind = spec.get('type')
        if kind == 'CIMSimpleRenderer':
            symbol = (spec.get('symbol') or {}).get('symbol') or {}
            if not symbol.get('symbolLayers'):
                return None  # the writer's label-only layer
            return {'type': 'single', 'passes': self.passes(spec.get('symbol'), geometry, self.where)}
        if kind == 'CIMUniqueValueRenderer':
            return self.unique(spec, geometry)
        if kind == 'CIMClassBreaksRenderer':
            return self.breaks(spec, geometry)
        if kind == 'CIMProportionalRenderer':
            self.reading.note('approximated', 'renderer', 'proportional symbols are written as one symbol: the sizes by value are not carried', self.where, 'renderer')
            return {'type': 'single', 'passes': self.passes(spec.get('minSymbol') or spec.get('symbol'), geometry, self.where)}
        self.reading.note('untranslated', 'renderer', f'the renderer {kind} has no map-style form: the features are not drawn', self.where, 'renderer')
        return None

    def unique(self, spec, geometry):
        fields = spec.get('fields') or []
        expression = (spec.get('valueExpressionInfo') or {}).get('expression')
        written = writer_rules(expression) if expression else None
        default = None
        if spec.get('useDefaultSymbol') and spec.get('defaultSymbol') and spec.get('isDefaultSymbolVisible', True) is not False:
            default = self.passes(spec['defaultSymbol'], geometry, {**self.where, 'class': spec.get('defaultLabel') or 'default'})
        rules, takes_null = [], False
        for group in spec.get('groups') or []:
            for item in group.get('classes') or []:
                label = item.get('label') or None
                values = item.get('values') or []
                if not values:
                    continue
                keys = [v.get('fieldValues') or [] for v in values]
                takes_null = takes_null or any('<Null>' in key for key in keys)
                if default is not None and keys == [['<Null>']] and item.get('symbol') == spec.get('defaultSymbol'):
                    continue  # the writer's way of letting the default take NULL
                if keys == [['default']] and written is not None:
                    default = self.passes(item.get('symbol'), geometry, {**self.where, 'class': label or 'default'})
                    continue
                passes = self.passes(item.get('symbol'), geometry, {**self.where, 'class': label or (keys[0][0] if keys[0] else 'class')})
                visible = False if item.get('visible') is False else None
                for key in keys:
                    spec_filter = self.class_filter(fields, key, expression, written)
                    if spec_filter is None:
                        continue
                    rules.append({'filter': spec_filter, 'label': label, 'visible': visible, 'passes': passes})
        if default is not None and written is None and not takes_null:
            self.reading.note('approximated', 'default-null', 'Pro\'s "all other values" leaves NULL out and map-style\'s default takes it: features with no value draw here and not in Pro', self.where, 'renderer.default')
        if not rules:
            return {'type': 'single', 'passes': default} if default else None
        return rules_renderer(rules, default)

    def class_filter(self, fields, key, expression, written):
        if written is not None:
            text = written.get(key[0]) if key else None
            if text is None:
                self.reading.note('untranslated', 'renderer', f'no rule in the value expression returns {key}: the class is left out', self.where, 'renderer')
                return None
            return parse_filter(text, 'arcade')
        if expression:
            return {'expression': f'({expression}) == {json.dumps(key[0], ensure_ascii=False)}', 'dialect': 'arcade'}
        leaves = []
        for field, value in zip(fields, key):
            leaves.append({'field': field, 'op': 'isNull'} if value == '<Null>' else {'field': field, 'op': '==', 'value': value})
        if not leaves:
            return None
        return leaves[0] if len(leaves) == 1 else {'all': leaves}

    def breaks(self, spec, geometry):
        field = spec.get('field')
        if not field:
            self.reading.note('untranslated', 'renderer', 'a class-breaks renderer with no field is not carried', self.where, 'renderer')
            return None
        if spec.get('normalizationField') or spec.get('normalizationType', 'Nothing') not in ('Nothing', None):
            self.reading.note('untranslated', 'renderer', 'normalisation is not carried: the classes apply to the field itself', self.where, 'renderer.field')
        if spec.get('useExclusionSymbol'):
            self.reading.note('untranslated', 'renderer', 'the exclusion symbol is not carried', self.where, 'renderer')
        low = float(spec.get('minimumBreak', 0) or 0)
        items = spec.get('breaks') or []
        classes = []
        for index, item in enumerate(items):
            upper = float(item.get('upperBound', low))
            high = meant_bound(upper) if index < len(items) - 1 else upper
            label = item.get('label') or None
            where = {**self.where, 'class': label or f'{low}-{high}'}
            row = {'range': [tidy_number(low), tidy_number(high)], **({'label': label} if label else {}),
                   'passes': self.passes(item.get('symbol'), geometry, where)}
            classes.append(row)
            low = high
        if not classes:
            return None
        out = {'type': 'graduated', 'field': field, 'classes': classes, 'default': None}
        method = METHODS.get(spec.get('classificationMethod'))
        if method:
            out['method'] = method
        return out

    def visual_variables(self, geometry, renderer):
        """`size` from a graduated size variable; rotation variables to rotationField; others are reported."""
        size = None
        for variable in (self.definition.get('renderer') or {}).get('visualVariables') or []:
            kind = variable.get('type')
            if kind == 'CIMSizeVisualVariable' and variable.get('variableType', 'Graduated') == 'Graduated' and geometry == 'point':
                size = self.size_of(variable)
            elif kind == 'CIMRotationVisualVariable':
                self.rotation_variable(variable, renderer)
            else:
                self.reading.note('untranslated', 'visual-variable', f'the visual variable {kind} is not carried', self.where, 'renderer')
        return size

    def size_of(self, variable):
        expression = (variable.get('valueExpressionInfo') or {}).get('expression') or ''
        found = re.search(r'(Sqrt\()?\$feature\["((?:[^"\\]|\\.)+)"\]', expression) or re.search(r'(Sqrt\()?\$feature\.(\w+)', expression)
        name = (json.loads('"' + found[2] + '"') if '"' in (found[0] if found else '') else found[2]) if found else variable.get('field')
        if not name:
            self.reading.note('untranslated', 'visual-variable', 'a size variable with no field is not carried', self.where, 'size')
            return None
        low, high = float(variable.get('minValue', 0)), float(variable.get('maxValue', 1))
        sqrt = bool(found and found[1])
        if sqrt:
            low, high = low ** 2, high ** 2
        representation = variable.get('valueRepresentation', 'Radius')
        if representation not in ('Radius', 'Diameter', 'Area'):
            self.reading.note('approximated', 'visual-variable', f'a size variable by {representation} is written by radius', self.where, 'size')
        return {'field': name, 'scale': 'sqrt' if sqrt else 'linear', 'domain': [tidy(low), tidy(high)],
                'range': [tidy(px(float(variable.get('minSize', 0)) / 2)), tidy(px(float(variable.get('maxSize', 0)) / 2))]}

    def rotation_variable(self, variable, renderer):
        info = variable.get('visualVariableInfoZ') or {}
        expression = (info.get('valueExpressionInfo') or {}).get('expression') or ''
        found = re.search(r'\$feature\.(\w+)', expression) or re.search(r'\$feature\["([^"]+)"\]', expression)
        if not found or renderer is None:
            self.reading.note('untranslated', 'visual-variable', 'a rotation variable with no field is not carried', self.where, 'renderer')
            return
        if variable.get('rotationTypeZ', 'Geographic') != 'Geographic':
            self.reading.note('approximated', 'visual-variable', 'a rotation variable counted counter-clockwise is written clockwise, as map-style has it', self.where, 'renderer')
        for passes in renderer_passes(renderer):
            for symbol in passes:
                if symbol.get('marker'):
                    symbol['marker']['rotationField'] = found[1]

    def labels(self, geometry):
        classes = self.definition.get('labelClasses') or []
        visible = [item for item in classes if item.get('visibility', True) is not False]
        if not visible:
            return None
        if any(str(item.get('name', '')).startswith('style ') and item.get('whereClause', '').find('style_id') >= 0 for item in visible):
            self.reading.note('untranslated', 'placed-labels', 'labels placed at fixed positions live in the data table, not the style: left out', self.where, 'labels')
            return None
        item = visible[0]
        drawn = bool(self.definition.get('labelVisibility'))  # Pro leaves it out when off, and keeps a default class with no text
        if not drawn and not (item.get('expression') or '').strip():
            return None
        if len(visible) > 1:
            self.reading.note('untranslated', 'label-rules', f'map-style has one label style per layer: the first of {len(visible)} label classes is kept', self.where, 'labels')
        if item.get('whereClause'):
            self.reading.note('untranslated', 'label-rules', f'the label class\'s filter {item["whereClause"]!r} is not carried: every feature gets a label', self.where, 'labels')
        field, template = label_source(item.get('expression') or '', item.get('expressionEngine') or 'Arcade', self.where, self.reading)
        if not field and not template:
            return None
        out = {'field': field} if field else {'template': template}
        out['text'] = self.text_style((item.get('textSymbol') or {}).get('symbol') or {})
        placement = self.label_placement(item, geometry)
        if placement:
            out['placement'] = placement
        if not drawn:
            out['enabled'] = False
        return out

    def label_placement(self, item, geometry):
        props = item.get('maplexLabelPlacementProperties') or {}
        if geometry == 'point':
            method = props.get('pointPlacementMethod')
            if method not in ('AroundPoint', 'CenteredOnPoint', None):
                self.reading.note('approximated', 'label-placement', f'label placement {method} is written as auto', self.where, 'labels.placement')
            return 'point' if method == 'CenteredOnPoint' else 'auto'
        if geometry == 'line':
            return 'line' if props.get('linePlacementMethod') == 'CenteredCurvedOnLine' else 'auto'
        return 'polygon-center' if props.get('canPlaceLabelOutsidePolygon') is False else 'auto'

    def text_style(self, symbol):
        where, reading = self.where, self.reading
        out = {'size': tidy(px(symbol.get('height', 10) or 10))}
        fill = next((layer for layer in (symbol.get('symbol') or {}).get('symbolLayers') or [] if layer.get('type') == 'CIMSolidFill'), None)
        value, opacity = color_of((fill or {}).get('color') or {'type': 'CIMRGBColor', 'values': [0, 0, 0, 100]}, where, reading)
        out['color'] = value
        if opacity < 1:
            out['opacity'] = opacity
        if symbol.get('fontFamilyName'):
            out['font'] = symbol['fontFamilyName']
        style = (symbol.get('fontStyleName') or '').lower().replace(' ', '').replace('-', '')
        weight = next((w for name, w in sorted(STYLE_WEIGHTS.items(), key=lambda kv: -len(kv[0])) if name in style), None)
        if weight and weight != 400:
            out['weight'] = weight
        if 'italic' in style or 'oblique' in style:
            reading.note('untranslated', 'font-style', 'map-style has no italic: the label is written upright', where, 'labels.text')
        halo = symbol.get('haloSymbol')
        if halo and symbol.get('haloSize'):
            tone = next((layer for layer in halo.get('symbolLayers') or [] if layer.get('type') == 'CIMSolidFill'), None)
            if tone:
                color, alpha = color_of(tone.get('color'), where, reading)
                out['halo'] = {'color': color, 'width': tidy(px(symbol['haloSize'])), **({'opacity': alpha} if alpha < 1 else {})}
        if symbol.get('letterSpacing'):
            out['letterSpacing'] = round(float(symbol['letterSpacing']) / 100, 4)
            reading.note('approximated', 'letter-spacing', 'CIM letter spacing is a percentage of the glyph width: written as that share of an em', where, 'labels.text.letterSpacing')
        callout = symbol.get('callout')
        if callout:
            if callout.get('type') != 'CIMBackgroundCallout':
                reading.note('approximated', 'label-box', f'the callout {callout.get("type")} is written as a plain box', where, 'labels.text.box')
            out['box'] = self.label_box(callout)
        if symbol.get('shadowColor') and (symbol.get('shadowOffsetX') or symbol.get('shadowOffsetY')):
            reading.note('untranslated', 'label-shadow', 'a label shadow is not carried', where, 'labels.text')
        return out

    def label_box(self, callout):
        box = {}
        for layer in reversed((callout.get('backgroundSymbol') or {}).get('symbolLayers') or []):
            if layer.get('type') == 'CIMSolidFill':
                value, opacity = color_of(layer.get('color'), self.where, self.reading)
                box['fill'] = paint(value, opacity)
            elif 'Stroke' in layer.get('type', ''):
                box['stroke'] = self.symbols.stroke(layer, self.where)
        margin = callout.get('margin') or {}
        sides = [tidy(px(margin.get(k, 0) or 0)) for k in ('top', 'right', 'bottom', 'left')]
        if sides[0] == sides[2] and sides[1] == sides[3]:
            box['padding'] = sides[0] if sides[0] == sides[1] else [sides[0], sides[1]]
        else:
            box['padding'] = sides
        return box

    def common(self):
        """What every layer may carry; what map-style has no field for is reported."""
        definition, where, extra = self.definition, self.where, {}
        if definition.get('visibility') is False:
            extra['visible'] = False
        transparency = float(definition.get('transparency', 0) or 0)
        if transparency:
            extra['opacity'] = round(1 - transparency / 100, 4)
        if definition.get('minScale') or definition.get('maxScale'):
            self.reading.note('untranslated', 'scale-range', 'the layer\'s scale range is not carried: it draws at every scale', where, 'visible')
        for key in ('blendingMode', 'featureBlendingMode'):
            if definition.get(key, 'Alpha') not in ('Alpha', 'Normal', None):
                self.reading.note('untranslated', 'blend-mode', f'{key} {definition[key]} is not carried: the layer draws in normal mode', where, 'renderer')
        return extra


def renderer_passes(renderer):
    if renderer.get('passes'):
        yield renderer['passes']
    for item in (renderer.get('classes') or []) + (renderer.get('rules') or []):
        yield item['passes']
    if renderer.get('default'):
        yield renderer['default']


def writer_rules(expression):
    """{class value: condition} of a value expression written as `if (condition) { return "1" }` lines, else None."""
    found = {}
    for raw in expression.splitlines():
        text = raw.strip()
        if not text or text.startswith('//') or text in ('return null', 'return "default"'):
            continue
        match = re.fullmatch(r'if \((.*)\) \{ return "([^"]*)" \}', text)
        if not match:
            return None
        found[match[2]] = match[1]
    return found or None


def label_source(expression, engine, where, reading):
    """(field or None, template or None) from a label class's expression."""
    text = expression.strip()
    if engine != 'Arcade':  # Python, VBScript, JScript: [field]
        found = re.findall(r'\[([^\]]+)\]', text)
        if len(found) == 1 and text == f'[{found[0]}]':
            return found[0], None
        if found:
            reading.note('approximated', 'label-expression', f'the {engine} label expression {text!r} is not a field: the labels show {found[0]} alone', where, 'labels.field')
            return found[0], None
        reading.note('untranslated', 'label-expression', f'the {engine} label expression {text!r} names no field: the labels are left out', where, 'labels')
        return None, None
    template, single = '', None
    pieces = split_plus(text)
    for piece in pieces:
        piece = re.sub(r'^DefaultValue\((.*),\s*""\)$', r'\1', piece.strip())
        found = re.fullmatch(r'\$feature\["((?:[^"\\]|\\.)+)"\]', piece) or re.fullmatch(r'\$feature\.(\w+)', piece)
        if found:
            single = json.loads('"' + found[1] + '"')
            template += '{' + single + '}'
        elif re.fullmatch(r'"(?:[^"\\]|\\.)*"', piece):
            template += json.loads(piece).replace('{', '').replace('}', '')
        else:
            template = None
            break
    if template is not None and single is not None:
        return (single, None) if len(pieces) == 1 else (None, template)
    fields = re.findall(r'\$feature\["((?:[^"\\]|\\.)+)"\]|\$feature\.(\w+)', text)
    if fields:
        name = fields[0][0] or fields[0][1]
        reading.note('approximated', 'label-expression', f'the label expression {text!r} is not a field or a template: the labels show {name} alone', where, 'labels.field')
        return name, None
    reading.note('untranslated', 'label-expression', f'the label expression {text!r} names no field: the labels are left out', where, 'labels')
    return None, None


def split_plus(text):
    parts, depth, current, quote = [], 0, '', False
    for index, char in enumerate(text):
        if char == '"' and (index == 0 or text[index - 1] != '\\'):
            quote = not quote
        elif not quote and char in '([':
            depth += 1
        elif not quote and char in ')]':
            depth -= 1
        elif not quote and depth == 0 and char == '+':
            parts.append(current)
            current = ''
            continue
        current += char
    return parts + [current]


# --- rasters -------------------------------------------------------------------------------------------------------

def ramp_stops(ramp, low, high, where, reading):
    """Colour stops along a CIM colour ramp between two data values."""
    kind = (ramp or {}).get('type')
    if kind == 'CIMMultipartColorRamp':
        parts = ramp.get('colorRamps') or []
        weights = ramp.get('weights') or [1] * len(parts)
        total = sum(weights) or 1
        stops, at = [], low
        for part, weight in zip(parts, weights):
            end = at + (high - low) * weight / total
            for stop in ramp_stops(part, at, end, where, reading):
                if not stops or stops[-1]['value'] != stop['value']:
                    stops.append(stop)
            at = end
        return stops
    if kind in ('CIMLinearContinuousColorRamp', 'CIMPolarContinuousColorRamp'):
        if kind == 'CIMPolarContinuousColorRamp':
            reading.note('approximated', 'color-ramp', 'a polar colour ramp is written as a straight blend between its ends', where, 'renderer.stops')
        return [{'value': tidy_number(low), 'color': color_of(ramp.get('fromColor'), where, reading)[0]}, {'value': tidy_number(high), 'color': color_of(ramp.get('toColor'), where, reading)[0]}]
    if kind == 'CIMFixedColorRamp':
        colors = ramp.get('colors') or []
        steps = max(len(colors) - 1, 1)
        return [{'value': tidy_number(low + (high - low) * i / steps), 'color': color_of(c, where, reading)[0]} for i, c in enumerate(colors)]
    reading.note('untranslated', 'color-ramp', f'the colour ramp {kind} has no map-style form: gray stands in', where, 'renderer.stops')
    return [{'value': tidy_number(low), 'color': '#000000'}, {'value': tidy_number(high), 'color': '#FFFFFF'}]


def raster_renderer(definition, where, reading):
    colorizer = definition.get('colorizer') or {}
    kind = colorizer.get('type')
    if kind == 'CIMRasterStretchColorizer':
        stats = colorizer.get('stretchStats') or {}
        classes = colorizer.get('stretchClasses') or []
        low = stats.get('min', colorizer.get('customStretchMin', classes[0].get('value') if classes else None))
        high = stats.get('max', colorizer.get('customStretchMax', classes[-1].get('value') if classes else None))
        if low is None or high is None:
            reading.note('untranslated', 'raster-stretch', 'a stretch with no limits saved in the file cannot give data values to colours', where, 'renderer')
            return None
        if colorizer.get('stretchType') not in ('MinimumMaximum', None):
            reading.note('approximated', 'raster-stretch', f'the {colorizer["stretchType"]} stretch is written as a straight ramp between its saved limits', where, 'renderer')
        stops = ramp_stops(colorizer.get('colorRamp'), float(low), float(high), where, reading)
        for row, name in ((classes[0] if classes else None, 0), (classes[-1] if classes else None, -1)):
            if row and row.get('label') and stops:
                stops[name]['label'] = row['label']
        out = {'type': 'pseudocolor', 'interpolation': 'linear', 'stops': stops}
        if colorizer.get('bandIndex'):
            out['band'] = int(colorizer['bandIndex']) + 1
    elif kind == 'CIMRasterClassifyColorizer':
        breaks = colorizer.get('classBreaks') or []
        if not breaks:
            return None
        stops, value = [], None
        for item in breaks:
            value = float(item.get('upperBound', 0)) if value is None else meant_bound(value)
            stop = {'value': tidy_number(value), 'color': color_of(item.get('color'), where, reading)[0]}
            if item.get('label'):
                stop['label'] = item['label']
            stops.append(stop)
            value = float(item.get('upperBound', 0))
        reading.note('approximated', 'raster-classes', 'the first class has no lower bound of its own in CIM: its stop is its upper bound', where, 'renderer.stops')
        out = {'type': 'pseudocolor', 'interpolation': 'discrete', 'stops': stops}
    elif kind == 'CIMRasterUniqueValueColorizer':
        classes = []
        for group in colorizer.get('groups') or []:
            for item in group.get('classes') or []:
                for raw in item.get('values') or []:
                    try:
                        value = tidy_number(raw)
                    except ValueError:
                        reading.note('untranslated', 'raster-classes', f'the class value {raw!r} is not a number: left out', where, 'renderer.classes')
                        continue
                    row = {'value': value, 'color': color_of(item.get('color'), where, reading)[0]}
                    if item.get('label'):
                        row['label'] = item['label']
                    classes.append(row)
        if not classes:
            return None
        out = {'type': 'palette', 'classes': classes}
    elif kind == 'CIMRasterRGBColorizer':
        out = {'type': 'rgb', 'bands': [int(colorizer.get(f'{name}BandIndex', index)) + 1 for index, name in enumerate(('red', 'green', 'blue'))]}
        reading.note('approximated', 'raster-stretch', 'the contrast stretch of each band is not carried: map-style\'s rgb has none', where, 'renderer')
        return out
    else:
        return None
    nodata = colorizer.get('noDataColor')
    if nodata and (nodata.get('values') or [0, 0, 0, 0])[-1] > 0 and kind != 'CIMRasterUniqueValueColorizer':
        out['nodata'] = {'color': color_of(nodata, where, reading)[0]}
    return out


# --- .lyrx ---------------------------------------------------------------------------------------------------------

def tail_name(text):
    return re.split(r'[\\/]', text.rstrip('\\/'))[-1]


def data_of(connection, geometry, layer_id, reading):
    """data for a layer: a layer file's paths are absolute or climb out of its folder, so only the last names are kept (map-style allows neither)."""
    where = {'layer': layer_id}
    kind = 'raster' if geometry == 'raster' else 'vector'
    if reading.data:
        return {'path': reading.data, 'kind': kind, **({'layer': reading.data_layer} if reading.data_layer else {})}
    connection = connection or {}
    dataset = connection.get('dataset')
    workspace = re.sub(r'^\w+=', '', connection.get('workspaceConnectionString') or '')
    factory = connection.get('workspaceFactory')
    if not dataset:
        reading.note('untranslated', 'no-data', f'the data connection {connection.get("type")} is not a file this style can name: data.path is a placeholder to replace', where, 'data')
        return {'path': f'data/unbound/{layer_id}', 'kind': kind}
    reading.note('approximated', 'data-path', 'map-style allows no absolute path or "..": data.path keeps only the last names of the layer file\'s data connection, to point at where the data is', where, 'data')
    if factory in ('FileGDB', 'SQLite', 'Access') and workspace:
        return {'path': tail_name(workspace), 'layer': dataset, 'kind': kind}
    if factory == 'Shapefile' and not dataset.lower().endswith('.shp'):
        return {'path': f'{tail_name(dataset)}.shp', 'kind': kind}
    if factory in ('Shapefile', 'Raster'):
        return {'path': tail_name(dataset), 'kind': kind}
    reading.note('untranslated', 'no-data', f'the workspace type {factory} is not a file: data.path is a placeholder to replace', where, 'data')
    return {'path': f'data/unbound/{layer_id}', 'layer': dataset, 'kind': kind}


def walk_layers(uris, definitions, parents, out, reading):
    """Layers from the bottom up, as map-style lists them (CIM lists the top first), with the groups around each."""
    for uri in reversed(uris or []):
        definition = definitions.get(uri)
        if definition is None:
            reading.note('untranslated', 'missing-layer', f'the layer file refers to {tail_name(uri)}, which it does not hold')
            continue
        if definition.get('type') == 'CIMGroupLayer':
            walk_layers(definition.get('layers'), definitions, parents + [definition], out, reading)
        else:
            out.append((definition, parents))


def read_lyrx(path, reading):
    try:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'{path.name} is not readable JSON: {error}') from error
    if document.get('type') != 'CIMLayerDocument':
        raise ValueError(f'{path.name} is not a layer file (type {document.get("type")})')
    definitions = {item.get('uRI'): item for item in document.get('layerDefinitions') or []}
    layers = []
    walk_layers(document.get('layers') or [item.get('uRI') for item in document.get('layerDefinitions') or []], definitions, [], layers, reading)
    drawable = [item for item, _ in layers if item.get('type') in ('CIMFeatureLayer', 'CIMRasterLayer')]
    if reading.data and len(drawable) > 1:
        raise ValueError(f'--data names one dataset and {path.name} has {len(drawable)} layers')
    named_groups = set()
    for definition, parents in layers:
        name = definition.get('name') or 'layer'
        found = re.fullmatch(r'map-style layer (.+)', definition.get('description') or '')
        layer_id = reading.unique_id(found[1] if found else name)
        where = {'layer': layer_id}
        kind = definition.get('type')
        layer = Layer(reading, definition, layer_id)
        extra = layer.common()
        groups = [parent.get('name') for parent in parents]
        group = next((g for g in reversed(groups) if g in GROUPS), None)
        if any(parent.get('visibility') is False for parent in parents):
            extra['visible'] = False
        for parent in parents[1:] if len(parents) > 1 else []:
            if parent.get('name') not in GROUPS:
                named_groups.add(parent.get('name'))
        if kind == 'CIMRasterLayer':
            renderer = raster_renderer(definition, where, reading)
            if renderer is None:
                reading.note('untranslated', 'renderer', f'the raster colorizer {(definition.get("colorizer") or {}).get("type")} has no map-style form: layer {name} is left out', where, 'renderer')
                continue
            reading.add_layer(layer_id, 'raster', renderer, data_of(definition.get('dataConnection'), 'raster', layer_id, reading), title=name, group=group, **extra)
            continue
        if kind != 'CIMFeatureLayer':
            reading.note('untranslated', 'layer-type', f'the layer type {kind} has no map-style form: layer {name} is left out', where)
            continue
        symbol = first_symbol(definition.get('renderer'))
        geometry = SYMBOL_GEOMETRY.get((symbol or {}).get('type'))
        if geometry is None:
            reading.note('untranslated', 'renderer', f'layer {name} does not say its geometry type by a symbol: left out', where, 'geometry')
            continue
        table = definition.get('featureTable') or {}
        renderer = layer.renderer(geometry)
        size = layer.visual_variables(geometry, renderer) if renderer else None
        spec = parse_filter(table['definitionExpression'], 'sql') if table.get('definitionExpression') else None
        order = (definition.get('featureSortInfos') or [None])[0]
        sort = {'field': order.get('fieldName'), 'order': 'desc' if order.get('sortDirection') == 'Descending' else 'asc'} if order and order.get('fieldName') else None
        labels = layer.labels(geometry)
        reading.add_layer(layer_id, geometry, renderer, data_of(table.get('dataConnection'), geometry, layer_id, reading), title=name, group=group,
                          labels=labels, filter=spec, size=size, sortBy=sort, **extra)
    if named_groups:
        reading.note('approximated', 'group-layers', f'map-style has a small set of groups: {len(named_groups)} group layer(s) of the file are flattened, keeping the drawing order')


# --- .stylx --------------------------------------------------------------------------------------------------------

def content_json(content):
    """A CONTENT cell: text or bytes, with the NUL Pro leaves after the JSON."""
    if isinstance(content, (bytes, bytearray)):
        content = bytes(content).decode('utf-8')
    return json.loads(content.rstrip('\0'))


def read_stylx(path, reading):
    with path.open('rb') as stream:
        head = stream.read(16)
    if head != b'SQLite format 3\0':
        raise ValueError(f'{path.name} is not a SQLite file, as a .stylx is')
    db = sqlite3.connect(f'file:{quote(str(path.absolute()))}?mode=ro&immutable=1', uri=True)
    try:
        try:
            class_names = dict(db.execute('SELECT * FROM CLASSES').fetchall())
            rows = db.execute('SELECT CLASS, CATEGORY, NAME, KEY, CONTENT FROM ITEMS ORDER BY ID').fetchall()
            binaries = db.execute('SELECT COUNT(*) FROM BINARIES').fetchone()[0]
        except sqlite3.Error as error:
            raise ValueError(f'{path.name} has no style tables ({error})') from error
    finally:
        db.close()
    layers = {'polygon': [], 'line': [], 'point': []}
    symbols = Symbols(reading)
    other = {}
    seen = {geometry: set() for geometry in layers}
    field = reading.field or 'name'
    for cls, category, name, key, content in rows:
        if cls not in SYMBOL_CLASSES:
            other[class_names.get(cls, f'class {cls}')] = other.get(class_names.get(cls, f'class {cls}'), 0) + 1
            continue
        value = key if reading.match == 'key' else name
        try:
            symbol = content_json(content)
        except (ValueError, UnicodeDecodeError) as error:
            reading.note('untranslated', 'unreadable-item', f'{name}: the item cannot be read ({error}): left out')
            continue
        geometry = SYMBOL_GEOMETRY.get(symbol.get('type'))
        if geometry is None:
            reading.note('untranslated', 'unreadable-item', f'{name}: the item is a {symbol.get("type")}, not a symbol of a map layer: left out')
            continue
        layer_id = f'{geometry}-symbols'
        if not value or value in seen[geometry]:
            reading.note('untranslated', 'duplicate-name', f'{value or "(no name)"}: {reading.match} is empty or used by an earlier {geometry} symbol: left out (a class value can match only once)', {'layer': layer_id}, 'renderer.classes')
            continue
        seen[geometry].add(value)
        where = {'layer': layer_id, 'class': value}
        layers[geometry].append({'value': value, 'label': name or value, 'passes': reading.symbol(geometry, where, lambda s=symbol, w=where: symbols.parts(s, geometry, w))})
    for label, count in sorted(other.items()):
        reading.note('untranslated', 'library-item', f'{count} {label} item(s) of the library are not symbols of a map layer: left out')
    reading.extra['library'] = {'items': len(rows), 'symbols': sum(len(v) for v in layers.values()), 'other_items': other, 'binaries': binaries,
                                'by_geometry': {g: len(v) for g, v in layers.items()}}
    if binaries:
        reading.note('untranslated', 'library-item', f'{binaries} binary item(s) of the library (pictures, fonts) are not read')
    for geometry in ('polygon', 'line', 'point'):
        if layers[geometry]:
            layer_id = reading.unique_id(f'{geometry}-symbols')
            data = reading.unbound_data(layer_id, 'a symbol library')
            reading.add_layer(layer_id, geometry, {'type': 'categorized', 'field': field, 'classes': layers[geometry], 'default': None}, {**data, 'kind': 'vector'},
                              title=f'{Path(path).stem} ({geometry})')
