"""map-style v1 -> ArcMap: layer files (.lyr) assembled from skeletons, and the files of a layer package (.lpk).

A .lyr is a compound file (OLE2 / CFB v3) with one stream, `Layer`, holding persisted ArcObjects. The format is closed,
so nothing here serialises a class from scratch: templates/arcmap/ holds real layer files written by ArcObjects with
made-up data (how: tests/arcmap_skeletons/), their parameters marked by slot strings, and this module cuts, patches and
joins them. What the stream looks like (read from those files; references/arcmap-package.md has the details):

- An object is its class GUID, a u32 id, a u16 version and its fields; a second mention of the object is the GUID and id
  alone. Ids count up through the whole stream (the root is 0), so adding a class renumbers everything after it; there
  are no lengths around objects, except a u64 byte count before each child of a group layer.
- A string is a u32 byte count (with the terminator) and UTF-16LE. A null object is 16 zero bytes.
- A colour is CIELAB, which ArcMap draws through a gamma 1.8 RGB (Apple RGB, nearly), not sRGB.

Sizes are points (map-style px x 0.75). What cannot be expressed goes to `notes` ({layer, item, reason}), as in the other
writers. No optional dependency is used: olefile only reads compound files, so this module has its own reader and writer.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import shutil
import struct
import uuid

import _arcgis_style as cim
from _arcgis_style import PT_PER_PX, Unusable, sql, value_text
from _map_style import classes_as_rules, layer_symbols, number, resolve_font

SKELETONS = Path(__file__).resolve().parent.parent / 'templates' / 'arcmap'
PACKAGE_VERSION = '10.7'  # item.pkinfo: which v* folder ArcGIS reads
DATA_FOLDER = 'v107'  # holds the data too: ArcGIS Pro unpacks only the newest version folder (and ArcMap the whole package)
LAYER_FOLDERS = ('v10', 'v105', DATA_FOLDER)  # ArcGIS 10.0-10.4, 10.5-10.6, 10.7 and later


def guid_bytes(text):
    return uuid.UUID(text).bytes_le


FEATURE_LAYER = guid_bytes('e663a651-8aad-11d0-bec7-00805f7c4268')
GROUP_LAYER = guid_bytes('edad6647-1810-11d1-86ae-0000f8751720')
SIMPLE_RENDERER = guid_bytes('f3435801-5779-11d0-98bf-00805f7ced21')
UNIQUE_RENDERER = guid_bytes('c3346d29-b2bc-11d1-8817-080009ec732a')
BREAKS_RENDERER = guid_bytes('ae5f7ea2-8b48-11d0-8356-080009b996cc')
LEGEND_GROUP = guid_bytes('167c5ea2-af20-11d1-8817-080009ec732a')
LEGEND_CLASS = guid_bytes('167c5ea3-af20-11d1-8817-080009ec732a')
FILL_SYMBOL = guid_bytes('7914e603-c892-11d0-8bb6-080009ee4e41')
LINE_SYMBOL = guid_bytes('7914e5f9-c892-11d0-8bb6-080009ee4e41')
MARKER_SYMBOL = guid_bytes('7914e5fe-c892-11d0-8bb6-080009ee4e41')
RGB_COLOR = guid_bytes('7ee9c496-d123-11d0-8383-080009b996cc')
FIELD_INFO = guid_bytes('a2baae2d-969b-11d2-ae77-080009ec732a')
LAYER_EXTENSION = guid_bytes('d5bb4b88-e0a1-11d2-9f4d-00c04f6bc78e')
TRANSPARENCY = guid_bytes('ad754a65-13b4-11d3-b89d-00600802e603')
ENVELOPE = guid_bytes('30707212-52d5-11d0-a8f2-00608c85ede5')
ANNOTATION_SET = guid_bytes('1d5849f3-0d33-11d2-a26f-080009b6f22b')
LABEL_ENGINE = guid_bytes('01004145-0d1c-11d2-a26f-080009b6f22b')
TEXT_SYMBOL = guid_bytes('b65a3e74-2993-11d1-9a43-0080c7ec5c96')
STD_FONT = guid_bytes('0be35203-8f91-11ce-9de3-00aa004bb851')
# Every class that is persisted with an id; the renumbering walks these
OBJECT_CLASSES = [FEATURE_LAYER, GROUP_LAYER, SIMPLE_RENDERER, UNIQUE_RENDERER, BREAKS_RENDERER, LEGEND_GROUP, LEGEND_CLASS, FILL_SYMBOL, LINE_SYMBOL,
                  MARKER_SYMBOL, RGB_COLOR, FIELD_INFO, LAYER_EXTENSION, TRANSPARENCY, ENVELOPE, ANNOTATION_SET, LABEL_ENGINE, TEXT_SYMBOL, STD_FONT] + [guid_bytes(g) for g in (
    '75e10086-4226-42ac-afec-cbb9b748f847',  # FeatureClassName
    '5a350011-e371-11d1-aa82-00c04fa33a15',  # WorkspaceName
    '588e5a11-d09b-11d1-aa7c-00c04fa33a15',  # PropertySet
    '7e4f4719-8e54-11d2-aad8-000000000000',  # NumericFormat of a field
    'a6a87a80-1dd1-11b2-bf51-08002022f573',  # GeographicCoordinateSystem
    '78ff7fa1-fb2f-11d1-94a2-080009eebecb',  # the layer's tail object
    'ee535289-41c9-11d1-880a-080009ec732a', '2442958c-d711-11d2-9f41-00c04f6bc6a5', '261a4372-d9d5-11d2-a806-cc9f870bcd5a',
    '261a4377-d9d5-11d2-a806-cc9f870bcd5a')]
OBJECTS = re.compile(b'|'.join(re.escape(g) for g in OBJECT_CLASSES))
BACK_REFERENCE = 0xFFFFFFFE  # marks a second mention while a stream is being assembled: the id of the latest object of that class


# --- compound file -------------------------------------------------------------------------------------------------

SIGNATURE = bytes.fromhex('d0cf11e0a1b11ae1')
SECTOR, MINI_SECTOR, MINI_CUTOFF = 512, 64, 4096
FREE, END_OF_CHAIN, FAT_SECTOR, NO_STREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def read_layer_file(data):
    """The `Layer` stream of a .lyr; ValueError when it is not a compound file with that stream."""
    if data[:8] != SIGNATURE or len(data) < SECTOR:
        raise ValueError('not a compound file')
    sector_size = 1 << struct.unpack_from('<H', data, 30)[0]
    mini_size = 1 << struct.unpack_from('<H', data, 32)[0]
    fat_count, first_dir = struct.unpack_from('<II', data, 44)
    cutoff, first_mini_fat, mini_fat_count, first_difat, difat_count = struct.unpack_from('<IIIII', data, 56)
    if difat_count or first_difat != END_OF_CHAIN:
        raise ValueError('compound file too large for a layer file')

    def sector(index):
        return data[sector_size * (index + 1):sector_size * (index + 2)]

    fat_sectors = struct.unpack_from(f'<{fat_count}I', data, 76)
    fat = []
    for index in fat_sectors:
        fat += struct.unpack(f'<{sector_size // 4}I', sector(index))

    def chain(start):
        out, seen = bytearray(), set()
        while start != END_OF_CHAIN:
            if start in seen or start >= len(fat):
                raise ValueError('broken sector chain')
            seen.add(start)
            out += sector(start)
            start = fat[start]
        return bytes(out)

    directory = chain(first_dir)
    entries = {}
    for offset in range(0, len(directory), 128):
        row = directory[offset:offset + 128]
        length = struct.unpack_from('<H', row, 64)[0]
        if row[66] in (1, 2, 5) and length >= 2:
            entries[row[:length - 2].decode('utf-16le')] = (row[66], *struct.unpack_from('<I', row, 116), *struct.unpack_from('<I', row, 120))
    if 'Layer' not in entries:
        raise ValueError('no Layer stream')
    _, start, size = entries['Layer']
    if size >= cutoff:
        return chain(start)[:size]
    mini_fat = struct.unpack(f'<{len(chain(first_mini_fat)) // 4}I', chain(first_mini_fat)) if mini_fat_count else ()
    mini = chain(entries['Root Entry'][1])
    out, seen = bytearray(), set()
    while start != END_OF_CHAIN:
        if start in seen or start >= len(mini_fat):
            raise ValueError('broken mini stream chain')
        seen.add(start)
        out += mini[start * mini_size:(start + 1) * mini_size]
        start = mini_fat[start]
    return bytes(out[:size])


def directory_entry(name='', kind=0, start=0, size=0, child=NO_STREAM):
    """One 128-byte directory entry; an empty name is an unused entry. All left at zero: class, state, timestamps."""
    encoded = name.encode('utf-16le') + b'\0\0' if name else b''
    entry = encoded.ljust(64, b'\0') + struct.pack('<HBB', len(encoded), kind, 1 if name else 0) + struct.pack('<III', NO_STREAM, NO_STREAM, child)
    return entry + bytes(36) + struct.pack('<IQ', start, size)


def write_layer_file(stream):
    """A compound file v3 with `stream` as its `Layer` stream, laid out as ArcMap writes one: FAT, directory, then the
    small-stream tables (a stream under 4096 bytes lives in the mini stream). Timestamps are left empty."""
    def sectors(payload):
        return -(-len(payload) // SECTOR)

    if len(stream) >= MINI_CUTOFF:
        mini_fat_data, mini_data = b'', b''
        body = stream
    else:
        count = -(-len(stream) // MINI_SECTOR)
        mini_fat_data = b''.join(struct.pack('<I', i + 1 if i + 1 < count else END_OF_CHAIN) for i in range(count))
        mini_fat_data = mini_fat_data.ljust(sectors(mini_fat_data) * SECTOR, b'\xff')
        mini_data = stream.ljust(count * MINI_SECTOR, b'\0')
        body = b''
    n_mini_fat, n_mini, n_body, n_dir = sectors(mini_fat_data), sectors(mini_data), sectors(body), 1
    fat_count = 1
    while True:  # the FAT describes itself too
        total = fat_count + n_dir + n_mini_fat + n_mini + n_body
        if total <= fat_count * (SECTOR // 4):
            break
        fat_count += 1
    if fat_count > 109:
        raise ValueError('a layer file this large needs a DIFAT, which this writer does not write')
    position, fat = 0, []

    def run(count):
        nonlocal position
        first = position if count else END_OF_CHAIN
        for i in range(count):
            fat.append(position + 1 if i + 1 < count else END_OF_CHAIN)
            position += 1
        return first

    for _ in range(fat_count):
        fat.append(FAT_SECTOR)
        position += 1
    dir_start = run(n_dir)
    mini_fat_start = run(n_mini_fat)
    mini_start = run(n_mini)
    body_start = run(n_body)
    fat_data = b''.join(struct.pack('<I', v) for v in fat).ljust(fat_count * SECTOR, b'\xff')
    if body:
        root = directory_entry('Root Entry', 5, END_OF_CHAIN, 0, child=1)
        layer = directory_entry('Layer', 2, body_start, len(stream))
    else:
        root = directory_entry('Root Entry', 5, mini_start, len(mini_data), child=1)
        layer = directory_entry('Layer', 2, 0, len(stream))
    dir_data = root + layer + directory_entry() + directory_entry()
    header = bytearray(SECTOR)
    header[:8] = SIGNATURE
    struct.pack_into('<HHHHH', header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into('<IIII', header, 40, 0, fat_count, dir_start, 0)
    struct.pack_into('<IIIII', header, 56, MINI_CUTOFF, mini_fat_start, n_mini_fat, END_OF_CHAIN, 0)
    struct.pack_into('<109I', header, 76, *range(fat_count), *[FREE] * (109 - fat_count))
    return bytes(header) + fat_data + dir_data + mini_fat_data + mini_data.ljust(n_mini * SECTOR, b'\0') + body.ljust(n_body * SECTOR, b'\0')


# --- stream primitives ---------------------------------------------------------------------------------------------

def text_slot(text):
    """A string as the stream stores it."""
    encoded = text.encode('utf-16le') + b'\0\0'
    return struct.pack('<I', len(encoded)) + encoded


def replace_slot(stream, slot, text):
    """Every mention of a slot string; identity strings repeat in a stream and must all change together."""
    return stream.replace(text_slot(slot), text_slot(text))


def renumber(stream):
    """Give every object its id in stream order: the root keeps 0, a back reference takes the id of the latest object
    of its class, and an id of 0 further on is not an object at all (a class GUID stored as a plain value)."""
    out = bytearray(stream)
    latest, next_id = {}, 1
    for match in OBJECTS.finditer(stream):
        position = match.end()
        current = struct.unpack_from('<I', stream, position)[0]
        if match.start() == 0 or current == 0:
            continue
        if current == BACK_REFERENCE:
            struct.pack_into('<I', out, position, latest[match.group()])
            continue
        struct.pack_into('<I', out, position, next_id)
        latest[match.group()] = next_id
        next_id += 1
    return bytes(out)


def object_ids(stream):
    """(class GUID, id) of every object in the stream, in order."""
    return [(m.group(), struct.unpack_from('<I', stream, m.end())[0]) for m in OBJECTS.finditer(stream)]


# --- colour --------------------------------------------------------------------------------------------------------

# Gamma 1.8 RGB to XYZ as ArcObjects does it (fitted to the Lab it writes for known RGB, exact to 1e-15): close to textbook Apple
# RGB, not the same; its row sums are the D65 white its Lab is relative to
APPLE_TO_XYZ = ((0.44966828014108506, 0.3162608009236535, 0.18454091893526164),
                (0.24461592248526604, 0.6720442513235446, 0.08333982619118942),
                (0.02517578482079592, 0.14115624140928956, 0.9224979737699145))
WHITE = tuple(sum(row) for row in APPLE_TO_XYZ)


def _inverse(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det), ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det), \
           ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det)


XYZ_TO_APPLE = _inverse(APPLE_TO_XYZ)


def rgb_to_lab(red, green, blue):
    """What ArcMap stores for an RGB colour: CIELAB of the colour read as Apple RGB (gamma 1.8)."""
    linear = [(v / 255) ** 1.8 for v in (red, green, blue)]
    xyz = [sum(c * l for c, l in zip(row, linear)) for row in APPLE_TO_XYZ]

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    x, y, z = (f(v / w) for v, w in zip(xyz, WHITE))
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def lab_to_rgb(lightness, a, b):
    """The 8-bit RGB ArcMap draws for a stored Lab colour."""
    fy = (lightness + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def inverse(t):
        return t ** 3 if t ** 3 > 216 / 24389 else (116 * t - 16) * 27 / 24389

    xyz = [inverse(v) * w for v, w in zip((fx, fy, fz), WHITE)]
    linear = [sum(c * v for c, v in zip(row, xyz)) for row in XYZ_TO_APPLE]
    return tuple(min(255, max(0, round(255 * max(0.0, v) ** (1 / 1.8)))) for v in linear)


def hex_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


LAB_OFFSET = 25  # from a colour's class GUID: GUID, id, version, three flag bytes
AFTER_COLOR = 51  # from the same GUID: Lab and two more flag bytes


def set_color(blob, position, value):
    """Overwrite the Lab of the colour object at position, in place."""
    if blob[position:position + 16] != RGB_COLOR:
        raise ValueError('no colour object here')
    struct.pack_into('<3d', blob, position + LAB_OFFSET, *rgb_to_lab(*hex_rgb(value)))


# --- skeletons -----------------------------------------------------------------------------------------------------

_loaded = {}


def skeleton(name):
    """The Layer stream of templates/arcmap/<name>.lyr."""
    if name not in _loaded:
        _loaded[name] = read_layer_file((SKELETONS / f'{name}.lyr').read_bytes())
    return _loaded[name]


def split_layer(stream):
    """A feature layer's stream around its renderer: (head, renderer, tail). The renderer starts at its class GUID and its
    block runs up to the display field, the first thing the three renderer skeletons share again."""
    start = min(p for p in (stream.find(g) for g in (SIMPLE_RENDERER, UNIQUE_RENDERER, BREAKS_RENDERER)) if p >= 0)
    end = stream.find(text_slot('@display'), start)
    if end < 0:
        raise ValueError('no display field slot in this skeleton')
    return stream[:start], stream[start:end], stream[end:]


def symbol_blob(geometry):
    """The symbol of this geometry's single-symbol skeleton, as a bytearray."""
    stream = skeleton(geometry)
    guid = {'polygon': FILL_SYMBOL, 'line': LINE_SYMBOL, 'point': MARKER_SYMBOL}[geometry]
    start = stream.find(guid, stream.find(SIMPLE_RENDERER))
    return bytearray(stream[start:stream.find(LEGEND_GROUP, start)])


def line_fields(blob, position, stroke, where, notes):
    """Colour, width and style of the line symbol at position (a line layer's symbol, or a fill's outline)."""
    color = position + 22
    if blob[position:position + 16] != LINE_SYMBOL or blob[color:color + 16] != RGB_COLOR:
        raise ValueError('no line symbol here')
    set_color(blob, color, stroke['color'])
    struct.pack_into('<d', blob, color + AFTER_COLOR, float(stroke['width']) * PT_PER_PX)
    style = 0
    if stroke.get('dash'):
        style = 1  # esriSLSDash: a simple line has fixed dash patterns only
        notes.append({**where, 'item': 'stroke.dash', 'kind': 'approximated', 'reason': f'dash {stroke["dash"]} is drawn as ArcMap\'s fixed dashed line'})
    if stroke.get('cap', 'butt') != 'butt' or stroke.get('join', 'miter') != 'miter':
        notes.append({**where, 'item': 'stroke.cap', 'kind': 'approximated', 'reason': 'a simple line symbol has round caps and joins whatever is asked'})
    struct.pack_into('<I', blob, color + AFTER_COLOR + 8, style)


def fill_symbol(fill, stroke, where, notes):
    """A SimpleFillSymbol: no fill is its null style, no stroke an outline of null style."""
    blob = symbol_blob('polygon')
    line = blob.find(LINE_SYMBOL)
    if stroke:
        line_fields(blob, line, stroke, where, notes)
    else:
        struct.pack_into('<I', blob, line + 22 + AFTER_COLOR + 8, 5)  # esriSLSNull
    color = blob.rfind(RGB_COLOR)
    if fill:
        set_color(blob, color, fill['color'])
    else:
        struct.pack_into('<I', blob, color + AFTER_COLOR + 8, 1)  # esriSFSNull
    return bytes(blob)


MARKER_STYLES = {'circle': 0, 'square': 1, 'cross': 2, 'x': 3, 'diamond': 4}
MARKER_ANGLE, MARKER_OUTLINE_FLAG, MARKER_OUTLINE_SIZE, MARKER_OUTLINE_COLOR = 71, 95, 96, 104  # from the marker's colour GUID


def marker_symbol(spec, where, notes):
    """A SimpleMarkerSymbol. Its size is the diameter; only five shapes exist, the others draw as circles (reported)."""
    blob = symbol_blob('point')
    marker = spec.get('marker') or {'shape': 'circle', 'size': 6}
    if not spec.get('marker'):
        notes.append({**where, 'item': 'symbol.marker', 'reason': 'point symbol without a marker: drawn as a 6 px circle'})
    fill, stroke = spec.get('fill'), spec.get('stroke')
    fill = fill if fill and fill.get('opacity', 1) > 0 else None
    stroke = stroke if stroke and stroke.get('opacity', 1) > 0 else None
    color = blob.find(RGB_COLOR)
    if marker['shape'] not in MARKER_STYLES:
        notes.append({**where, 'item': 'marker.shape', 'kind': 'approximated', 'reason': f'shape {marker["shape"]} has no simple marker in ArcMap: drawn as a circle'})
    main = (fill or stroke or {'color': '#000000'})['color']
    set_color(blob, color, main)
    struct.pack_into('<d', blob, color + AFTER_COLOR, float(marker['size']) * PT_PER_PX)
    struct.pack_into('<I', blob, color + AFTER_COLOR + 8, MARKER_STYLES.get(marker['shape'], 0))
    # map-style turns clockwise, ArcMap counter-clockwise
    struct.pack_into('<d', blob, color + MARKER_ANGLE, -float(marker.get('rotation', 0)) % 360)
    if marker.get('rotationField'):
        notes.append({**where, 'item': 'marker.rotationField', 'reason': 'rotation by field has no skeleton yet: the fixed rotation alone is drawn'})
    if fill and stroke:
        blob[color + MARKER_OUTLINE_FLAG] = 1
        struct.pack_into('<d', blob, color + MARKER_OUTLINE_SIZE, float(stroke['width']) * PT_PER_PX)
        set_color(blob, color + MARKER_OUTLINE_COLOR, stroke['color'])
    return bytes(blob)


def symbol(spec, geometry, where, notes):
    """The skeleton symbol of this geometry with a map-style Symbol's values in it."""
    fill, stroke, marker = spec.get('fill'), spec.get('stroke'), spec.get('marker')
    if geometry == 'point':
        return marker_symbol(spec, where, notes)
    if marker:
        notes.append({**where, 'item': 'symbol.marker', 'reason': f'a marker on a {geometry} layer has no skeleton yet: not drawn'})
    if fill and fill.get('pattern') and geometry == 'polygon':
        notes.append({**where, 'item': 'fill.pattern', 'reason': 'hatching has no skeleton yet: only the ground colour is drawn'})
    # a part at opacity 0 is not there; other opacities are the layer's business (layer_transparency)
    fill = fill if fill and fill.get('opacity', 1) > 0 else None
    stroke = stroke if stroke and stroke.get('opacity', 1) > 0 else None
    if geometry == 'polygon':
        return fill_symbol(fill, stroke, where, notes)
    if geometry == 'line':
        if fill:
            notes.append({**where, 'item': 'symbol.fill', 'reason': 'fill on a line layer is not drawn'})
        if not stroke:
            notes.append({**where, 'item': 'symbol', 'reason': 'nothing in this symbol draws on a line layer'})
        blob = symbol_blob('line')
        line_fields(blob, 0, stroke or {'color': '#000000', 'width': 0}, where, notes)
        if not stroke:
            struct.pack_into('<I', blob, 22 + AFTER_COLOR + 8, 5)
        return bytes(blob)
    raise ValueError(f'no skeleton for geometry {geometry}')


def empty_symbol(geometry):
    """A symbol that draws nothing: a layer with only labels, or a class that is switched off."""
    if geometry == 'point':
        return marker_symbol({'marker': {'shape': 'circle', 'size': 0}}, {}, [])
    if geometry == 'line':
        blob = symbol_blob('line')
        struct.pack_into('<I', blob, 22 + AFTER_COLOR + 8, 5)
        return bytes(blob)
    return fill_symbol(None, None, {}, [])


# --- renderers -----------------------------------------------------------------------------------------------------

def simple_renderer(symbol_bytes):
    """The single-symbol renderer: the legend class mentions the same symbol object again."""
    block = split_layer(skeleton('polygon'))[1]
    start = block.find(FILL_SYMBOL)
    group = block.find(LEGEND_GROUP)
    mention = block.find(FILL_SYMBOL, group)
    return block[:start] + symbol_bytes + block[group:mention] + symbol_bytes[:16] + struct.pack('<I', BACK_REFERENCE) + block[mention + 20:]


def class_template(block, label_slot, next_start):
    """One legend class of a renderer skeleton, around its symbol: (before the symbol, after the label)."""
    head_end = block.find(LEGEND_CLASS, next_start[0]) + 22
    tail_start = block.find(text_slot(label_slot), head_end) + len(text_slot(label_slot))
    return block[next_start[0]:head_end], block[tail_start:next_start[1]]


def unique_renderer(field, heading, classes, default, default_label, spare):
    """A unique-value renderer. classes: (value, label, symbol bytes); default: symbol bytes or None; spare: a symbol for the
    slot that has to hold one even when there is no default. In the stream: the default symbol, the number of legend groups,
    one group per class (the default's first, with an empty value), and a table of each value's place in that list."""
    block = split_layer(skeleton('unique'))[1]
    default_start = block.find(FILL_SYMBOL)
    first = block.find(LEGEND_GROUP)
    second = block.find(LEGEND_GROUP, first + 16)
    head, tail = class_template(block, '@label0', (first, second))
    end_of_classes = block.find(text_slot('@label1'), second) + len(text_slot('@label1')) + len(tail)
    table = end_of_classes + 16 + 4  # a null object, then the number of values
    table_end = block.find(text_slot('@value1'), table) + len(text_slot('@value1')) + 12
    after = bytearray(block[table_end:])
    label = after.find(text_slot('@default'))
    after[label - 2:label] = b'\xff\xff' if default is not None else b'\0\0'
    # the default's legend class mentions the default symbol again
    rows = ([('', default_label, default[:16] + struct.pack('<I', BACK_REFERENCE))] if default is not None else []) + list(classes)
    out = bytearray(block[:default_start].replace(text_slot('@field'), text_slot(field)))
    out += (default if default is not None else spare) + struct.pack('<I', len(rows))
    for value, text, symbol_bytes in rows:
        heading_text = value if heading is None else heading
        out += head.replace(text_slot('@value0'), text_slot(heading_text)) + symbol_bytes + text_slot(text) + tail
    out += block[end_of_classes:end_of_classes + 16] + struct.pack('<I', len(classes))
    for index, (value, _, _) in enumerate(classes):
        out += text_slot(value) + struct.pack('<III', index + (1 if default is not None else 0), 0, 0)
    return bytes(out + replace_slot(bytes(after), '@default', default_label))


def breaks_renderer(field, minimum, classes):
    """A class-breaks renderer. classes: (upper bound, label, symbol bytes). Its bounds are a list of doubles, and all its
    classes sit in one legend group."""
    block = split_layer(skeleton('breaks'))[1]
    field_end = block.find(text_slot('@field')) + len(text_slot('@field')) + len(text_slot(''))
    bounds = field_end + 4 + 8 * struct.unpack_from('<I', block, field_end)[0]
    group = block.find(LEGEND_GROUP)
    first = block.find(LEGEND_CLASS, group)
    second = block.find(LEGEND_CLASS, first + 16)
    head, tail = class_template(block, '@label0', (first, second))
    rest = block.find(text_slot('@label1'), second) + len(text_slot('@label1')) + len(tail)
    old_count = (bounds - field_end - 4) // 8
    settings = bytearray(block[bounds + 8 * old_count:group])  # after the bounds, as many doubles at zero
    struct.pack_into('<d', settings, len(settings) - 8, float(minimum))
    out = bytearray(block[:field_end].replace(text_slot('@field'), text_slot(field))) + struct.pack('<I', len(classes))
    out += b''.join(struct.pack('<d', float(upper)) for upper, _, _ in classes) + bytes(8 * len(classes)) + settings
    out += block[group:first - 4].replace(text_slot('@heading'), text_slot(field)) + struct.pack('<I', len(classes))
    for _, text, symbol_bytes in classes:
        out += head + symbol_bytes + text_slot(text) + tail
    return bytes(out + block[rest:])


# --- labels --------------------------------------------------------------------------------------------------------

LABEL_SIZE_BEFORE_FONT = 36  # a text symbol's size is this many bytes before its font object


def field_expression(name):
    return f'[{name}]'


def label_expression(spec, names):
    """A label's text as an ArcMap expression, and whether it is a plain field (VBScript otherwise)."""
    if spec.get('template'):
        parts = []
        for index, piece in enumerate(re.split(r'\{([^{}]+)\}', spec['template'])):
            if piece:
                parts.append(field_expression(names.get(piece, piece)) if index % 2 else '"' + piece.replace('"', '""') + '"')
        return ' & '.join(parts) or '""', False
    return field_expression(names.get(spec['field'], spec['field'])), True


def font_object(blob, position, family, named, points, chinese):
    """The font of a text symbol: a StdFont holds its size in ten-thousandths of a point and its name in the ANSI code page. A font
    with the Western character set draws Chinese text as empty boxes, so Chinese text asks for the GB2312 one (Windows then
    draws it with a Chinese font when the named font has none)."""
    weight = 700 if named and 'bold' in named.lower() else 400
    flags = 2 if named and 'italic' in named.lower() else 0
    try:
        name, charset = family.encode('ascii'), 0x86 if chinese else 0
    except UnicodeEncodeError:
        name, charset = family.encode('gbk', 'replace'), 0x86
    body = position + 20
    old_length = blob[body + 10]
    blob[body:body + 11 + old_length] = bytes([1]) + struct.pack('<HBHI', charset, flags, weight, round(points * 10000)) + bytes([len(name)]) + name


def label_block(geometry, expression, simple, style, fonts, where, notes, chinese=False):
    """The annotation properties of a layer with one label class. The class is ArcMap's standard label engine with its
    default placement. chinese: the label text holds Chinese characters."""
    tail = split_layer(skeleton(f'label_{geometry}'))[2]
    block = bytearray(tail[tail.find(ANNOTATION_SET):tail.find(LAYER_EXTENSION) - 2])
    text = block.find(TEXT_SYMBOL)
    set_color(block, block.find(RGB_COLOR, text), style['color'])
    font = block.find(STD_FONT)
    points = float(style['size']) * PT_PER_PX
    struct.pack_into('<d', block, font - LABEL_SIZE_BEFORE_FONT, points)
    family, named = resolve_font(style, fonts)
    font_object(block, font, family or ('SimSun' if chinese else 'Arial'), named, points, chinese)
    halo = style.get('halo')
    if halo:
        mask = font - LABEL_SIZE_BEFORE_FONT + 8
        # the mask style, its size, and a null symbol become a halo style, the width and a fill symbol
        block[mask:mask + 28] = struct.pack('<Id', 1, float(halo['width']) * PT_PER_PX) + fill_symbol({'color': halo['color']}, None, where, notes)
        if halo.get('blur'):
            notes.append({**where, 'item': 'text.halo.blur', 'kind': 'approximated', 'reason': 'an ArcMap halo has no blur: drawn as a sharp halo'})
        if halo.get('opacity', 1) < 1:
            notes.append({**where, 'item': 'text.halo.opacity', 'kind': 'approximated', 'reason': 'a halo has no transparency: drawn opaque'})
    for key, item in (('letterSpacing', 'letter spacing'), ('box', 'a background box')):
        if style.get(key):
            notes.append({**where, 'item': f'text.{key}', 'reason': f'{item} has no skeleton yet: not drawn'})
    if style.get('opacity', 1) < 1:
        notes.append({**where, 'item': 'text.opacity', 'kind': 'approximated', 'reason': 'label text has no transparency of its own: drawn opaque'})
    slot = text_slot('@expression')
    at = block.find(slot)
    return bytes(block[:at]) + text_slot(expression) + (b'\xff\xff' if simple else b'\0\0') + bytes(block[at + len(slot) + 2:])


# --- layers --------------------------------------------------------------------------------------------------------

def field_table(tail, fields):
    """The layer's table of fields, rewritten for the data: a field is its name, a FieldInfo with the alias, a number format
    (null except for real numbers), and a read-only flag. fields: (name, real number?, read only?)."""
    start = tail.find(text_slot('@query')) + len(text_slot('@query')) + len(text_slot('')) + 4
    first = start + 4

    def mentions(name):  # a field's name and alias, in the skeleton's table
        one = tail.find(text_slot(name), first)
        return one, tail.find(text_slot(name), one + 4)

    (kind, kind_alias), (value, value_alias) = mentions('kind'), mentions('value')
    size = len(text_slot('kind')), len(text_slot('value'))
    end_of_table = tail.find(bytes.fromhex('ffffffffffff0000'), value_alias) + 8
    plain_body, plain_rest = tail[kind + size[0]:kind_alias], tail[kind_alias + size[0]:value - 2]
    real_body, real_rest = tail[value + size[1]:value_alias], tail[value_alias + size[1]:end_of_table]
    out = bytearray()
    for index, (name, real, _) in enumerate(fields):
        if index:
            out += b'\xff\xff' if fields[index - 1][2] else b'\0\0'
        out += text_slot(name) + (real_body if real else plain_body) + text_slot(name) + (real_rest if real else plain_rest)
    return tail[:start] + struct.pack('<I', len(fields)) + bytes(out) + tail[end_of_table:]


CATEGORY = 'File Geodatabase Feature Class'
EXTENSION_FILTER = 16 + 4 + 2 + 10  # from the layer extension object's GUID: id, version, its fields, then the display filter slot
LABELS_SHOWN = 16  # from the end of the display field: whether labels are shown
HOLDER = struct.pack('<I', 1)  # any id but 0: renumber() gives it its real one


def transparency_filter(percent):
    """The layer's display filter that makes it transparent: alpha 0-255, then, past the object, the percentage."""
    alpha = round(255 * (1 - percent / 100))
    return TRANSPARENCY + HOLDER + struct.pack('<HBBH', 1, alpha, 0, 1) + bytes.fromhex('feffff0000000000') + bytes([int(percent)])


def feature_layer(geometry, name, workspace, dataset, shape_field, renderer_block, query='', display='', visible=True, transparency=0,
                  label=None, labels_shown=True, fields=None):
    """The stream of one feature layer, with ids still to be numbered. workspace: the geodatabase, usually relative to the
    layer file; fields: see field_table (left out, the layer keeps the skeleton's, which ArcMap also accepts)."""
    head, _, tail = split_layer(skeleton(geometry))
    if fields:
        tail = field_table(tail, fields)
    if transparency:
        slot = tail.find(LAYER_EXTENSION) + EXTENSION_FILTER
        if tail[slot:slot + 17] != bytes(17):
            raise ValueError('no display filter slot in this skeleton')
        tail = tail[:slot] + transparency_filter(transparency) + tail[slot + 17:]
    if label:
        extension = tail.find(LAYER_EXTENSION)
        end = extension - 2
        tail = tail[:end - 16] + label + tail[end:]
        if labels_shown:
            flag = tail.find(text_slot('@display')) + len(text_slot('@display')) + LABELS_SHOWN
            tail = tail[:flag] + b'\xff\xff' + tail[flag + 2:]
    head = bytearray(head)
    category = head.find(text_slot(CATEGORY)) + len(text_slot(CATEGORY))
    head[category:category + 2] = b'\xff\xff' if visible else b'\0\0'
    stream = bytes(head) + renderer_block + tail
    for slot_name, text in (('@query', query), ('@display', display), ('@name', name), ('@dataset', dataset), ('@workspace', workspace),
                            ('@browse', workspace_name(workspace)), ('@shape', shape_field)):
        stream = replace_slot(stream, slot_name, text)
    return stream


def workspace_name(path):
    return re.split(r'[\\/]', path)[-1].rsplit('.', 1)[0]


def group_layer(name, children, visible=True):
    """A group layer around child layer streams: the number of children, then each with its byte count before it (the only
    lengths in the format; ids run on through all of them)."""
    stream = skeleton('group')
    first = stream.find(FEATURE_LAYER)
    head = bytearray(stream[:first - 8 - 4])  # up to the number of children; each child has a u64 byte count
    last = stream.rfind(FEATURE_LAYER)
    # past the last child: from what the skeleton's own byte count says
    size = struct.unpack_from('<Q', stream, last - 8)[0]
    tail = stream[last + size:]
    label = head.find(text_slot('@name')) + len(text_slot('@name'))
    head[label:label + 2] = b'\xff\xff' if visible else b'\0\0'
    out = bytearray(replace_slot(bytes(head), '@name', name)) + struct.pack('<I', len(children))
    for child in children:
        body = child[:16] + HOLDER + child[20:]
        out += struct.pack('<Q', len(body)) + body
    return bytes(out + tail)


def finish(stream):
    """A complete layer file from an assembled stream."""
    return write_layer_file(renumber(stream))


# --- one map-style layer -------------------------------------------------------------------------------------------

def layer_transparency(layer, where, notes):
    """Percent for the whole layer. A symbol has no transparency of its own, so when every part of every symbol has the same
    opacity the layer takes it (exact); otherwise those opacities are dropped and reported."""
    opacities = {float(part.get('opacity', 1)) for spec in layer_symbols(layer) for part in (spec.get('fill'), spec.get('stroke'))
                 if part and part.get('opacity', 1) > 0}
    opacity = float(layer.get('opacity', 1))
    if len(opacities) == 1:
        opacity *= opacities.pop()
    elif opacities and opacities != {1.0}:
        notes.append({**where, 'item': 'symbol.opacity', 'kind': 'approximated',
                      'reason': f'parts of the symbols have different opacities ({", ".join(number(v) for v in sorted(opacities))}): drawn opaque, as a symbol has no transparency of its own'})
    return round(100 * (1 - opacity))


def and_query(*queries):
    parts = [q for q in queries if q]
    return ' AND '.join(f'({q})' for q in parts) if len(parts) > 1 else (parts[0] if parts else '')


def below(value):
    """The next double under value: a class holds (previous bound, bound] and map-style's [low, high)."""
    return math.nextafter(float(value), -math.inf)


def has_chinese(*texts):
    return any(0x2E80 <= ord(c) <= 0x9FFF or 0xF900 <= ord(c) <= 0xFAFF or 0xFF00 <= ord(c) <= 0xFF60 for text in texts for c in str(text))


def layer_streams(layer, title, table, shape, fields, workspace, names, fonts, notes, read_column):
    """The stream(s) of a map-style vector layer: one feature layer, or a group of them where the renderer is a list of rules."""
    where = {'layer': layer['id']}
    geometry, spec = layer['geometry'], layer['renderer']
    query = cim.definition_query(layer.get('filter'), where, notes, names) or ''
    visible = layer.get('visible') is not False
    transparency = layer_transparency(layer, where, notes)
    labels = layer.get('labels')
    label, display = None, next((f[0] for f in fields[2:] if not f[1]), '')
    if labels:
        expression, simple = label_expression(labels, names)
        used = [names.get(f, f) for f in re.findall(r'\{([^{}]+)\}', labels['template'])] if labels.get('template') else [names.get(labels['field'], labels['field'])]
        chinese = has_chinese(labels.get('template', '')) or any(has_chinese(*read_column(table, f)) for f in used)
        label = label_block(geometry, expression, simple, labels['text'], fonts, where, notes, chinese)
        if labels.get('field'):
            display = names.get(labels['field'], labels['field'])
        if labels.get('placement', 'auto') != 'auto':
            notes.append({**where, 'item': 'labels.placement', 'kind': 'approximated',
                          'reason': f'placement {labels["placement"]} has no skeleton yet: ArcMap\'s default placement for this geometry is used'})
    if layer.get('size'):
        notes.append({**where, 'item': 'size', 'reason': 'size by field has no skeleton yet: every marker has its symbol\'s size'})
    if layer.get('sortBy'):
        notes.append({**where, 'item': 'sortBy', 'reason': 'draw order by field has no skeleton yet'})

    def one(name, renderer_block, condition=query, shown=visible):
        return feature_layer(geometry, name, workspace, table, shape, renderer_block, query=condition, display=display, visible=shown, transparency=transparency,
                             label=label, labels_shown=bool(labels) and labels.get('enabled', True) is not False, fields=fields)

    if spec is None:
        return one(title, simple_renderer(empty_symbol(geometry))), 'simple (labels only)'
    kind = spec['type']
    field = names.get(spec.get('field'), spec.get('field'))
    if kind == 'single':
        return one(title, simple_renderer(symbol(spec['symbol'], geometry, where, notes))), 'simple'
    if kind == 'categorized':
        rows = []
        for item in spec['classes']:
            text = value_text(item['value'])
            shown = item.get('visible', True) is not False
            if not shown:
                notes.append({**where, 'item': 'class.visible', 'kind': 'approximated', 'class': text, 'reason': 'a class that is off is written with a symbol that draws nothing: it stays in the legend'})
            rows.append((text, item.get('label', text), symbol(item['symbol'], geometry, {**where, 'class': text}, notes) if shown else empty_symbol(geometry)))
        default = spec.get('default')
        block = unique_renderer(field, None, rows, symbol(default, geometry, {**where, 'class': 'default'}, notes) if default is not None else None, 'other', empty_symbol(geometry))
        return one(title, block), 'unique values'
    if kind == 'graduated' and cim.contiguous(spec):
        classes = spec['classes']
        rows = [(float(item['range'][1]) if index == len(classes) - 1 else below(item['range'][1]),
                 item.get('label', f'{number(item["range"][0])} - {number(item["range"][1])}'),
                 symbol(item['symbol'], geometry, {**where, 'class': f'{number(item["range"][0])}-{number(item["range"][1])}'}, notes))
                for index, item in enumerate(classes)]
        return one(title, breaks_renderer(field, float(classes[0]['range'][0]), rows)), 'class breaks'
    # first-match rules, or classes a class-breaks renderer cannot hold: one single-symbol layer per rule, each with a definition
    # query that leaves out what the rules before it took
    if kind == 'graduated':
        notes.append({**where, 'item': 'renderer', 'kind': 'approximated', 'reason': 'graduated classes with gaps, hidden classes or a default are written as one layer per class in a group'})
    rules = spec['rules'] if kind == 'rule' else classes_as_rules(spec)
    children, taken = [], []
    default = None
    for index, rule in enumerate(rules):
        if rule.get('default'):
            default = rule.get('symbol')
            continue
        name = rule.get('label', str(index))
        try:
            condition = sql(rule['filter'], names)[1:-1] if rule.get('filter') is not None else ''
        except Unusable as error:
            notes.append({**where, 'item': 'filter', 'rule': name, 'reason': f'{error}: the rule is skipped (it matches nothing)'})
            continue
        before = ' OR '.join(f'({t})' for t in taken)
        condition_all = and_query(query, condition, f'NOT ({before})' if before else '')
        children.append(one(name, simple_renderer(symbol(rule['symbol'], geometry, {**where, 'rule': name}, notes)), condition_all,
                            rule.get('visible', True) is not False))
        if condition:
            taken.append(condition)
        else:
            break
    if kind == 'rule':
        default = spec.get('default', default)
    if default is not None:
        before = ' OR '.join(f'({t})' for t in taken)
        children.append(one('other', simple_renderer(symbol(default, geometry, {**where, 'rule': 'default'}, notes)), and_query(query, f'NOT ({before})' if before else '')))
    return group_layer(title, list(reversed(children)), visible), 'one layer per rule'


# --- the package ---------------------------------------------------------------------------------------------------

def table_fields(gdb, table):
    """The field table for a feature class as GDAL wrote it: (name, real number?, read only?)."""
    import pyogrio
    info = pyogrio.read_info(gdb, layer=table)
    rows = [(info.get('geometry_name') or 'SHAPE', False, False), (info.get('fid_column') or 'OBJECTID', False, True)]
    return rows + [(str(name), str(dtype).startswith('float'), False) for name, dtype in zip(info['fields'], info['dtypes'])], rows[0][0]


def placement_streams(placed, gdb, panels, workspace, fonts, notes):
    """labelPlacements in the tables placement_tables wrote: a point layer per text style that draws only its label, and a line
    layer per leader stroke. ArcMap's label engine chooses the side, so the text and style hold but the exact position does not."""
    rows = []
    names = placed['renames']
    for table, items, key, geometry in (('map_label_leaders', placed['leaders'], 'stroke_id', 'line'), ('map_labels', placed['labels'], 'style_id', 'point')):
        if not items:
            continue
        fields, shape = table_fields(gdb, table)
        for panel in [p for p in panels if any(r['panel'] == p for r in items)] + ([None] if any(r['panel'] is None for r in items) else []):
            for number_ in sorted({r[key] for r in items if r['panel'] == panel}):
                conditions = [f'{cim.sql_field(names.get(key, key))} = {number_}']
                if panels:
                    panel_field = cim.sql_field(names.get('panel', 'panel'))
                    conditions.append(f'{panel_field} IS NULL' if panel is None else f'{panel_field} = {cim.sql_literal(panel)}')
                query = and_query(*conditions)
                row_id = f'{table}.{panel or "none"}.{number_}'
                where = {'layer': 'labelPlacements', key: number_}
                if table == 'map_labels':
                    texts = [r['text'] for r in items if r['panel'] == panel and r[key] == number_]
                    label = label_block('point', f'[{names.get("text", "text")}]', True, placed['styles'][number_ - 1], fonts, where, notes, has_chinese(*texts))
                    stream = feature_layer('point', f'labels {number_}', workspace, table, shape, simple_renderer(empty_symbol('point')), query=query,
                                           display=names.get('text', 'text'), label=label, fields=fields)
                else:
                    block = simple_renderer(symbol({'stroke': placed['strokes'][number_ - 1]}, 'line', where, notes))
                    stream = feature_layer('line', f'leaders {number_}', workspace, table, shape, block, query=query, display='', fields=fields)
                rows.append({'id': row_id, 'layer': {'id': row_id, 'panel': panel, 'group': 'labels'}, 'stream': stream, 'table': table, 'query': query,
                             'kind': 'labels only' if table == 'map_labels' else 'simple', 'features': sum(r['panel'] == panel and r[key] == number_ for r in items)})
    if placed['labels']:
        notes.append({'layer': 'labelPlacements', 'item': 'position', 'kind': 'approximated',
                      'reason': 'text and style hold, but ArcMap\'s label engine picks the side of the point and may drop a label: anchor and rotation are not applied'})
    return rows


def write_package(ctx, stage, args, shared):
    """A layer package (.lpk) for ArcMap: the data in a file geodatabase, one layer file with a group layer for the map in each
    version folder, and the package information; with --lyr also the layer file beside an unpacked copy of the data."""
    import geopandas as gpd
    import pyogrio
    document, notes, fonts, bbox, scope = ctx['document'], ctx['notes'], ctx['fonts'], ctx['bbox'], ctx['scope']
    name = re.sub(r'[^\w.-]+', '_', args.name or document['id']).strip('._') or 'map'
    build = stage / '_lpk'
    common = build / DATA_FOLDER
    common.mkdir(parents=True)
    gdb = common / f'{name}.gdb'
    packed, not_packed, warnings = shared.store(ctx, common, gdb, 'OpenFileGDB', tiles=False)
    crs = shared.project_crs(document['layers'], packed)
    placed = shared.placement_tables(document, gdb, fonts, driver='OpenFileGDB', crs=crs)
    background = document.get('background')
    title = document.get('title') or document['id']
    if background:
        area = shared.background_polygon(document, bbox, packed, crs)
        if area is not None:
            pyogrio.write_dataframe(gpd.GeoDataFrame({'color': [background['color']]}, geometry=[area], crs=crs or 'EPSG:4326'),
                                    gdb, layer='map_background', driver='OpenFileGDB')
    panels = shared.panel_order(document)
    tables = {r[0] for r in pyogrio.list_layers(gdb)}
    layer_notes = []

    def read_column(table, column):
        """The distinct values of a text field, to know the labels' script before writing their font."""
        frame = pyogrio.read_dataframe(gdb, layer=table, columns=[column], read_geometry=False)
        return list(frame[column].dropna().unique()) if column in frame else []

    def assemble(workspace):
        """The map as one group layer, with the data at workspace (the path differs in the package and beside the data, and a
        path's length is in the group layer's byte counts, so the stream is put together for each)."""
        rows, manifest_rows = [], []
        layer_notes.clear()
        if 'map_background' in tables:
            fields, shape = table_fields(gdb, 'map_background')
            spec = {'fill': {'color': background['color']}}
            stream = feature_layer('polygon', 'background', workspace, 'map_background', shape, simple_renderer(symbol(spec, 'polygon', {}, layer_notes)),
                                   display='color', fields=fields, transparency=round(100 * (1 - float(background.get('opacity', 1)))))
            rows.append({'id': 'map_background', 'layer': {'id': 'map_background'}, 'stream': stream})
            manifest_rows.append({'id': 'map_background', 'title': 'background', 'panel': None, 'group': None, 'table': 'map_background', 'map_style_renderer': None,
                                  'arcmap_renderer': 'simple', 'labels': None, 'filter': None})
        for layer in document['layers']:
            entry = packed.get(layer.get('_key'))
            if entry is None:
                continue
            title_ = layer.get('title') or (layer.get('legend') or {}).get('title') or layer['id']
            row = {'id': layer['id'], 'title': title_, 'panel': layer.get('panel'), 'group': layer.get('group'),
                   'table': None if entry['kind'] == 'raster' else entry['table'], 'map_style_renderer': (layer['renderer'] or {}).get('type')}
            if entry['kind'] == 'raster':
                layer_notes.append({'layer': layer['id'], 'item': 'raster', 'reason': f'a raster layer has no skeleton yet: the GeoTIFF is in the package ({DATA_FOLDER}/{entry["file"]}) but no layer draws it'})
                row.update(arcmap_renderer=None, file=f'{DATA_FOLDER}/{entry["file"]}')
                manifest_rows.append(row)
                continue
            fields, shape = table_fields(gdb, entry['table'])
            stream, kind = layer_streams(layer, title_, entry['table'], shape, fields, workspace, entry['renames'], fonts, layer_notes, read_column)
            labels = layer.get('labels') or {}
            row.update(arcmap_renderer=kind, labels=None if not labels else 'on' if labels.get('enabled', True) else 'off',
                       filter=cim.definition_query(layer.get('filter'), {}, [], entry['renames']))
            rows.append({'id': layer['id'], 'layer': layer, 'stream': stream})
            manifest_rows.append(row)
        for row in placement_streams(placed, gdb, panels, workspace, fonts, layer_notes):
            rows.append(row)
            manifest_rows.append({'id': row['id'], 'title': row['id'], 'panel': row['layer']['panel'], 'group': 'labels', 'table': row['table'],
                                  'map_style_renderer': None, 'arcmap_renderer': row['kind'], 'labels': 'on' if row['table'] == 'map_labels' else None,
                                  'filter': row['query'], 'features': row['features']})
        by_id = {row['id']: row for row in rows}

        def build_tree(nodes):
            out = []
            for node in nodes:
                if node[0] == 'group':
                    _, group, checked, children = node
                    out.append(group_layer(group, build_tree(children), checked))
                else:
                    out.append(by_id[node[1]]['stream'])
            return out

        return group_layer(title, build_tree(shared.layer_tree(rows, panels))), manifest_rows

    stream, layer_rows = assemble(f'.\\{gdb.name}')
    notes += layer_notes
    layer_file = finish(stream)
    older = finish(assemble(f'..\\{DATA_FOLDER}\\{gdb.name}')[0])
    file_name = f'0000{name}.lyr'
    for folder in LAYER_FOLDERS:
        (build / folder).mkdir(exist_ok=True)
        (build / folder / file_name).write_bytes(layer_file if folder == DATA_FOLDER else older)
    # ArcGIS unpacks a package once per ID and reuses that copy, so new content needs a new ID (as for .lpkx)
    item_id = cim.guid(document['id'], name, shared.folder_digest(build))
    (build / 'esriinfo').mkdir()
    (build / 'esriinfo' / 'item.pkinfo').write_text(cim.pkinfo(item_id, name, PACKAGE_VERSION), encoding='utf-8')
    (build / 'esriinfo' / 'iteminfo.xml').write_text(
        cim.iteminfo(item_id, name, title, f'map-style {document["id"]}, packed by gis-kit', ['map-style', 'gis-kit'], bbox, crs.name if crs is not None else None,
                     keywords=('Layer Package', 'Data', 'ArcMap', 'lpk')), encoding='utf-8')
    lpk = stage / f'{name}.lpk'
    tool = shared.seven_zip(build, lpk, ['esriinfo', *LAYER_FOLDERS])
    if args.lyr:  # the same layer file beside the data, opened without unpacking
        shutil.move(str(gdb), str(stage / gdb.name))
        if (common / 'rasters').exists():
            shutil.move(str(common / 'rasters'), str(stage / 'rasters'))
        (stage / f'{name}.lyr').write_bytes(layer_file)
    shutil.rmtree(build)
    for entry in packed.values():
        if entry.get('file'):
            entry['file'] = f'{DATA_FOLDER}/{entry["file"]}'
    size = lpk.stat().st_size
    more, font_rows = shared.shared_warnings(ctx, packed, size)
    warnings += more
    if any(e.get('renames') for e in packed.values()) or placed['renames']:
        warnings.append('Some fields were renamed in the file geodatabase (see datasets[].renamed_fields); the layer file uses the new names')
    manifest = {
        **shared.manifest_head(ctx, 'arcmap-package'),
        'layer_package': {'file': lpk.name, 'bytes': size, 'archive': '7z', 'tool': tool, 'layer_files': [f'{folder}/{file_name}' for folder in LAYER_FOLDERS],
                          'geodatabase': f'{DATA_FOLDER}/{gdb.name}', 'rasters': sorted(e['file'] for e in packed.values() if e.get('file'))},
        'layer_file': {'file': f'{name}.lyr', 'geodatabase': gdb.name} if args.lyr else None,
        'geodatabase': {'name': gdb.name, 'feature_classes': sorted(tables)},
        'map': {'name': name, 'title': title, 'crs': shared.crs_label(crs),
                'open': f'ArcMap: Add Data > {lpk.name}, or double-click it; it unpacks and adds the group layer' + (f'. Or Add Data > {name}.lyr, kept beside {gdb.name}' if args.lyr else '')},
        'datasets': [shared.dataset_row(entry, scope) for entry in packed.values()],
        'layers': layer_rows,
        'panels': [{'id': panel, 'shown': index == 0, **shared.panel_frame(document, panel)} for index, panel in enumerate(panels)],
        'background': {'color': background['color'], 'layer': 'map_background'} if background else None,
        'label_placements': {'labels': len(placed['labels']), 'leaders': len(placed['leaders']), 'text_styles': len(placed['styles']), 'leader_strokes': len(placed['strokes'])},
        'fonts': font_rows,
        'icons': ctx['icons'],
        'producer_notes': document.get('notes') or [],
        'not_packed': not_packed,
        'unsupported': shared.unique_notes(notes),
        'bytes': size,
        'warnings': warnings,
    }
    shared.write_record(ctx, stage, args, packed, manifest, ['Sizes: 1 px = 0.75 pt', 'Colours: CIELAB of the RGB read as Apple RGB (gamma 1.8), which ArcMap draws back byte for byte',
                                                              'Data keep their own CRS; label placements, leaders and the background are stored in the CRS most map layers use',
                                                              'Layer sources are relative to the layer file: the geodatabase sits beside it in v107/ (and beside the .lyr), v10/ and v105/ point there'])
    return manifest
