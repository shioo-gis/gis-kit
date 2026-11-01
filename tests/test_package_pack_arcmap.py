"""package.py pack --format arcmap: a map-style map as an ArcMap layer package (made-up data only).

The layer files are compared with what ArcObjects itself wrote (tests/arcmap_skeletons/golden.json); with ArcMap's Python in
GIS_KIT_ARCMAP_PYTHON and ArcGIS Pro's in GIS_KIT_ARCGIS_PYTHON (Windows), the real programs open the package
(tests/arcmap_open_check.py, tests/arcmap_open_check_pro.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
from shapely.geometry import LineString, Point, box

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS / 'arcmap_skeletons'))

import _arcmap_style as arcmap  # noqa: E402
import normalize  # noqa: E402
import pack_fixture  # noqa: E402
import package  # noqa: E402

ARCMAP_PYTHON = os.environ.get('GIS_KIT_ARCMAP_PYTHON')
ARCGIS_PYTHON = os.environ.get('GIS_KIT_ARCGIS_PYTHON')
GOLDEN = json.loads((TESTS / 'arcmap_skeletons' / 'golden.json').read_text(encoding='utf-8'))
FILLED = normalize.FILLED
SKELETON_FILES = sorted(arcmap.SKELETONS.glob('*.lyr'))


def run(style, output, *extra, success=True):
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(style), '--output', str(output), '--format', 'arcmap', *map(str, extra)]
    result = subprocess.run(command, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GIS_COMPOSER_CONFIG_DIR': str(output.parent / 'no-config')})
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return json.loads((output / 'package.json').read_text()) if success else result.stderr


# --- container -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('size', [1, 63, 64, 65, 2458, 4095, 4096, 4097, 70000])
def test_container_round_trip(size):
    """The stream comes back byte for byte, in the mini stream and out of it, and each sector count is what a reader expects."""
    stream = bytes(range(256)) * (size // 256 + 1)
    stream = stream[:size]
    data = arcmap.write_layer_file(stream)
    assert data[:8] == arcmap.SIGNATURE and len(data) % 512 == 0
    assert arcmap.read_layer_file(data) == stream


@pytest.mark.parametrize('size', [100, 4096, 70000])
def test_container_is_read_by_olefile(size):
    olefile = pytest.importorskip('olefile')
    stream = os.urandom(size)
    data = arcmap.write_layer_file(stream)
    assert olefile.isOleFile(data)
    with olefile.OleFileIO(data) as ole:
        assert ole.listdir() == [['Layer']] and ole.openstream('Layer').read() == stream
        assert (ole.dll_version, ole.minor_version, ole.sector_size, ole.mini_sector_size) == (3, 0x3E, 512, 64)


def test_container_reader_refuses_what_is_not_a_layer_file():
    with pytest.raises(ValueError, match='not a compound file'):
        arcmap.read_layer_file(b'not a compound file' * 40)
    data = bytearray(arcmap.write_layer_file(b'x' * 100))
    position = data.find('Layer'.encode('utf-16le'))
    data[position] = ord('Y')
    with pytest.raises(ValueError, match='no Layer stream'):
        arcmap.read_layer_file(bytes(data))


# --- strings and ids -----------------------------------------------------------------------------------------------

def test_string_rewrite_any_length_every_mention():
    stream = b'\x01\x02' + arcmap.text_slot('old') + b'\x03' + arcmap.text_slot('old name') + arcmap.text_slot('old')
    for new in ('', 'x', '一个很长的中文图层名称 with spaces', 'n' * 400):
        out = arcmap.replace_slot(stream, 'old', new)
        assert out.count(arcmap.text_slot(new)) == 2 and arcmap.text_slot('old name') in out and arcmap.text_slot('old') not in out
    assert struct.unpack_from('<I', arcmap.text_slot('图层'))[0] == 6  # bytes with the terminator
    assert arcmap.text_slot('图层')[4:-2].decode('utf-16le') == '图层'


def test_renumber_counts_up_and_resolves_second_mentions():
    color, fill = arcmap.RGB_COLOR, arcmap.FILL_SYMBOL
    obj = lambda guid, number: guid + struct.pack('<I', number)  # noqa: E731
    stream = (obj(arcmap.FEATURE_LAYER, 0) + b'..' + obj(fill, 7) + obj(color, 7) + b'.' + obj(fill, 7) + obj(fill, arcmap.BACK_REFERENCE) + obj(arcmap.LABEL_ENGINE, 0))
    out = arcmap.renumber(stream)
    assert arcmap.object_ids(out) == [(arcmap.FEATURE_LAYER, 0), (fill, 1), (color, 2), (fill, 3), (fill, 3), (arcmap.LABEL_ENGINE, 0)]  # a stored GUID stays


def ids_are_consistent(stream):
    """Ids run 1, 2, 3 ...; a repeated id repeats the object of that class; nothing is left unresolved."""
    seen, next_id = {}, 1
    for index, (guid, number) in enumerate(arcmap.object_ids(stream)):
        assert number != arcmap.BACK_REFERENCE
        if number == 0:
            assert index == 0 or guid == arcmap.LABEL_ENGINE
        elif number == next_id:
            seen[number], next_id = guid, next_id + 1
        else:
            assert seen[number] == guid
    return next_id - 1


# --- colour --------------------------------------------------------------------------------------------------------

def lab_values(stream):
    return [struct.unpack_from('<3d', stream, m.start() + arcmap.LAB_OFFSET) for m in re.finditer(re.escape(arcmap.RGB_COLOR), stream)]


def test_colour_is_gamma_18_not_srgb():
    # what ArcObjects wrote for grey 90 in the skeletons: an sRGB reading would draw it 110
    assert arcmap.rgb_to_lab(90, 90, 90) == pytest.approx((46.098253603026016, 0, 0), abs=1e-9)
    assert arcmap.lab_to_rgb(46.098253603026016, 0, 0) == (90, 90, 90)
    assert arcmap.lab_to_rgb(54.044, 0, 0) == (110, 110, 110)  # the grey ArcMap writes by default; sRGB would say 129
    assert arcmap.rgb_to_lab(255, 255, 255) == pytest.approx((100, 0, 0), abs=1e-9)
    assert arcmap.rgb_to_lab(0, 0, 0) == pytest.approx((0, 0, 0), abs=1e-9)


def test_colour_matches_arcobjects_values():
    """Lab as ArcObjects wrote it for rgb(230, 30, 40), read from the file it wrote."""
    fill = lab_values(arcmap.skeleton('polygon'))[1]
    assert arcmap.rgb_to_lab(230, 30, 40) == pytest.approx(fill, abs=1e-9)


def test_colour_round_trips_every_8bit_grid_value():
    for red in range(0, 256, 15):
        for green in range(0, 256, 15):
            for blue in range(0, 256, 15):
                assert arcmap.lab_to_rgb(*arcmap.rgb_to_lab(red, green, blue)) == (red, green, blue)


# --- what ArcObjects itself writes -------------------------------------------------------------------------------

def golden_digest(stream):
    return normalize.digest(arcmap.renumber(stream))


def layer(geometry, renderer, **extra):
    query = extra.pop('query', '')
    return arcmap.feature_layer(geometry, FILLED['name'], FILLED['workspace'], FILLED['dataset'], FILLED['shape'], renderer,
                                query=query, display=FILLED['display'], **extra)


PALETTE = ['#E61E28', '#1E8CC8', '#28AA5A', '#C87814', '#7828B4']
VALUES = ['first', 'second', 'third', 'fourth', 'fifth']
GEOMETRY = {'poly': 'polygon', 'line': 'line', 'poin': 'point'}


def px(points):
    return points / 0.75


def class_symbol(geometry, index):
    color = PALETTE[index]
    spec = {'polygon': {'fill': {'color': color}, 'stroke': {'color': '#5A5A5A', 'width': px(0.75)}}, 'line': {'stroke': {'color': color, 'width': px(1.5)}},
            'point': {'fill': {'color': color}, 'marker': {'shape': 'circle', 'size': px(7)}}}[geometry]
    return arcmap.symbol(spec, geometry, {}, [])


@pytest.mark.parametrize('tag', GEOMETRY)
@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_unique_values_match_arcobjects(tag, count):
    geometry = GEOMETRY[tag]
    rows = [(VALUES[i], f'label {VALUES[i]}', class_symbol(geometry, i)) for i in range(count)]
    block = arcmap.unique_renderer('kind', None, rows, None, 'others', class_symbol(geometry, 4))
    assert golden_digest(layer(geometry, block)) == GOLDEN[f'unique_{tag}_{count}']


@pytest.mark.parametrize('tag', GEOMETRY)
def test_unique_values_with_default_match_arcobjects(tag):
    geometry = GEOMETRY[tag]
    rows = [(VALUES[i], f'label {VALUES[i]}', class_symbol(geometry, i)) for i in range(2)]
    block = arcmap.unique_renderer('kind', None, rows, class_symbol(geometry, 4), 'others', None)
    assert golden_digest(layer(geometry, block)) == GOLDEN[f'unique_{tag}_d2']


@pytest.mark.parametrize('tag', GEOMETRY)
@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_class_breaks_match_arcobjects(tag, count):
    geometry = GEOMETRY[tag]
    rows = [(10.5 * (i + 1), f'class {VALUES[i]}', class_symbol(geometry, i)) for i in range(count)]
    assert golden_digest(layer(geometry, arcmap.breaks_renderer('value', 1.25, rows))) == GOLDEN[f'breaks_{tag}_{count}']


FILL_RED = {'fill': {'color': PALETTE[0]}, 'stroke': {'color': '#5A5A5A', 'width': px(0.75)}}


@pytest.mark.parametrize('case, geometry, spec, extra', [
    ('poly_base', 'polygon', FILL_RED, {}),
    ('poly_w2', 'polygon', {**FILL_RED, 'stroke': {'color': '#5A5A5A', 'width': px(2.25)}}, {}),
    ('poly_hollow', 'polygon', {'stroke': {'color': '#5A5A5A', 'width': px(0.75)}}, {}),
    ('poly_nooutline', 'polygon', {'fill': {'color': PALETTE[0]}}, {}),
    ('poly_query', 'polygon', FILL_RED, {'query': "kind = 'a'"}),
    ('poly_hidden', 'polygon', FILL_RED, {'visible': False}),
    ('poly_trans', 'polygon', FILL_RED, {'transparency': 37}),
    ('line_base', 'line', {'stroke': {'color': PALETTE[0], 'width': px(1.5)}}, {}),
    ('line_w4', 'line', {'stroke': {'color': PALETTE[0], 'width': px(4.5)}}, {}),
    ('line_dash', 'line', {'stroke': {'color': PALETTE[0], 'width': px(1.5), 'dash': [4, 2]}}, {}),
    ('point_base', 'point', {'fill': {'color': PALETTE[0]}, 'marker': {'shape': 'circle', 'size': px(7)}}, {}),
    ('point_s12', 'point', {'fill': {'color': PALETTE[0]}, 'marker': {'shape': 'circle', 'size': px(12.5)}}, {}),
    ('point_square', 'point', {'fill': {'color': PALETTE[0]}, 'marker': {'shape': 'square', 'size': px(7)}}, {}),
    ('point_diamond', 'point', {'fill': {'color': PALETTE[0]}, 'marker': {'shape': 'diamond', 'size': px(7)}}, {}),
    ('point_angle', 'point', {'fill': {'color': PALETTE[0]}, 'marker': {'shape': 'circle', 'size': px(7), 'rotation': -33}}, {}),
    ('point_outline', 'point', {'fill': {'color': PALETTE[0]}, 'stroke': {'color': '#0A141E', 'width': px(1.5)}, 'marker': {'shape': 'circle', 'size': px(7)}}, {}),
])
def test_single_symbols_match_arcobjects(case, geometry, spec, extra):
    assert golden_digest(layer(geometry, arcmap.simple_renderer(arcmap.symbol(spec, geometry, {}, [])), **extra)) == GOLDEN[case]


FONTS = {'Song': [{'family': 'Song', 'names': {'family': '宋体'}}], 'Latin': [{'family': 'Latin', 'names': {'family': 'Arial', 'subfamily': 'Bold Italic'}}]}
TEXT = {'size': px(9), 'color': '#141414', 'font': 'Song'}


@pytest.mark.parametrize('case, geometry, expression, simple, style, shown', [
    ('label_poly', 'polygon', '[kind]', True, TEXT, True),
    ('label_poly2', 'polygon', '[value]', True, {**TEXT, 'size': px(14.5), 'color': '#C81414'}, True),
    ('label_off', 'polygon', '[kind]', True, TEXT, False),
    ('label_line', 'line', '[kind]', True, TEXT, True),
    ('label_point', 'point', '[kind]', True, TEXT, True),
    ('label_expr', 'polygon', '[kind] & "x"', False, TEXT, True),
    ('label_font', 'polygon', '[kind]', True, {**TEXT, 'font': 'Latin'}, True),
])
def test_labels_match_arcobjects(case, geometry, expression, simple, style, shown):
    block = arcmap.label_block(geometry, expression, simple, style, FONTS, {}, [])
    stream = layer(geometry, arcmap.simple_renderer(class_symbol(geometry, 0)), label=block, labels_shown=shown)
    assert golden_digest(stream) == GOLDEN[case]


def children_of(stream):
    """The direct children of a group layer stream, by their byte counts."""
    position = min(p for p in (stream.find(arcmap.FEATURE_LAYER), stream.find(arcmap.GROUP_LAYER, 16)) if p > 0)
    count = struct.unpack_from('<I', stream, position - 12)[0]
    position -= 8
    rows = []
    for _ in range(count):
        size = struct.unpack_from('<Q', stream, position)[0]
        rows.append(stream[position + 8:position + 8 + size])
        position += 8 + size
    return rows


def test_group_layer_holds_children_with_their_byte_counts():
    one = arcmap.feature_layer('polygon', 'one', '.\\a.gdb', 'a', 'SHAPE', arcmap.simple_renderer(class_symbol('polygon', 0)))
    two = arcmap.feature_layer('line', 'two', '.\\a.gdb', 'b', 'SHAPE', arcmap.simple_renderer(class_symbol('line', 1)))
    inner = arcmap.group_layer('inner', [two], visible=False)
    stream = arcmap.group_layer('outer', [one, inner])
    assert stream[:16] == arcmap.GROUP_LAYER
    rows = children_of(stream)
    assert [r[:16] for r in rows] == [arcmap.FEATURE_LAYER, arcmap.GROUP_LAYER]
    assert arcmap.text_slot('one') in rows[0] and len(children_of(rows[1])) == 1
    assert arcmap.text_slot('inner') + b'\0\0' in rows[1]  # the group's visible flag follows its name
    finished = arcmap.renumber(stream)
    ids_are_consistent(finished)
    assert arcmap.object_ids(finished)[0] == (arcmap.GROUP_LAYER, 0)
    assert finished.count(arcmap.FEATURE_LAYER) == 2


def test_skeletons_hold_nothing_of_the_machine_they_came_from():
    """No path, user name or Windows language: only slots and the made-up field names."""
    expected = {'@name', '@dataset', '@workspace', '@browse', '@shape', '@display', '@query', '@field', '@heading', '@value0', '@value1', '@label0', '@label1',
                '@default', '@expression', arcmap.CATEGORY, 'DATABASE', 'Default', ', ', 'OBJECTID', 'Shape', 'Shape_Length', 'Shape_Area', 'kind', 'value'}
    pattern = re.compile(rb'(?=([\x04-\xff]\x00\x00\x00))')
    assert SKELETON_FILES
    for path in SKELETON_FILES:
        data = path.read_bytes()
        assert data[:8] == arcmap.SIGNATURE
        stream = arcmap.read_layer_file(data)
        found = set()
        for match in pattern.finditer(stream):
            size = stream[match.start()]
            text = stream[match.start() + 4:match.start() + 4 + size]
            if size % 2 == 0 and text.endswith(b'\0\0') and len(text) == size:
                try:
                    decoded = text[:-2].decode('utf-16le')
                except UnicodeDecodeError:
                    continue
                if decoded.isprintable() and all(ord(c) < 0x3000 or 0x4e00 <= ord(c) <= 0x9fff for c in decoded) and len(decoded) >= 2:
                    found.add(decoded)
        assert found <= expected, (path.name, found - expected)
        raw = data + stream
        for word in (b'Users', 'Users'.encode('utf-16le'), b'C:\\', 'C:\\'.encode('utf-16le'), '文件地理'.encode('utf-16le'), '默认'.encode('utf-16le')):
            assert word not in raw, (path.name, word)
        assert ids_are_consistent(stream) > 0


# --- symbols and notes ---------------------------------------------------------------------------------------------

def test_what_has_no_skeleton_is_reported():
    notes = []
    pattern = {'kind': 'hatch', 'spacing': 10, 'width': 1, 'color': '#000000'}
    arcmap.symbol({'fill': {'color': '#FFFFFF', 'opacity': 0, 'pattern': pattern}, 'marker': {'shape': 'circle', 'size': 4, 'placement': 'line', 'spacing': 9}}, 'polygon', {'layer': 'a'}, notes)
    arcmap.symbol({'marker': {'shape': 'triangle', 'size': 4, 'rotationField': 'r'}}, 'point', {'layer': 'b'}, notes)
    arcmap.symbol({'marker': {'shape': 'svg', 'size': 4}}, 'point', {'layer': 'b'}, notes)
    arcmap.symbol({'stroke': {'color': '#000000', 'width': 1, 'dash': [1, 1], 'cap': 'round'}, 'fill': {'color': '#000000'}}, 'line', {'layer': 'c'}, notes)
    assert {(n['layer'], n['item']) for n in notes} == {('a', 'symbol.marker'), ('a', 'fill.pattern'), ('b', 'marker.shape'), ('b', 'marker.rotationField'),
                                                        ('c', 'stroke.dash'), ('c', 'stroke.cap'), ('c', 'symbol.fill')}


def test_symbol_parts_at_zero_opacity_are_left_out():
    notes = []
    hollow = arcmap.symbol({'fill': {'color': '#FF0000', 'opacity': 0}, 'stroke': {'color': '#0000FF', 'width': 1}}, 'polygon', {}, notes)
    color = hollow.rfind(arcmap.RGB_COLOR)
    assert struct.unpack_from('<I', hollow, color + arcmap.AFTER_COLOR + 8)[0] == 1  # esriSFSNull
    outline_only_off = arcmap.symbol({'fill': {'color': '#FF0000'}, 'stroke': {'color': '#0000FF', 'width': 1, 'opacity': 0}}, 'polygon', {}, notes)
    line = outline_only_off.find(arcmap.LINE_SYMBOL)
    assert struct.unpack_from('<I', outline_only_off, line + 22 + arcmap.AFTER_COLOR + 8)[0] == 5  # esriSLSNull
    assert notes == []


def test_layer_transparency_takes_a_shared_opacity_and_reports_mixed_ones():
    notes = []
    single = {'geometry': 'polygon', 'opacity': 0.5, 'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#000000', 'opacity': 0.5}}}}
    assert arcmap.layer_transparency(single, {'layer': 'a'}, notes) == 75 and notes == []
    mixed = {'geometry': 'polygon', 'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#000000', 'opacity': 0.5}, 'stroke': {'color': '#000000', 'width': 1}}}}
    assert arcmap.layer_transparency(mixed, {'layer': 'a'}, notes) == 0
    assert notes[0]['item'] == 'symbol.opacity' and notes[0]['kind'] == 'approximated'


def test_transparency_filter_holds_alpha_and_percent():
    body = arcmap.transparency_filter(37)
    assert body[:16] == arcmap.TRANSPARENCY and body[-1] == 37 and body[22] == round(255 * 0.63)


def test_font_name_length_and_charset():
    block = arcmap.label_block('point', '[a]', True, {'size': 16, 'color': '#000000', 'font': 'F'}, {'F': [{'family': 'F', 'names': {'family': '黑体'}}]}, {}, [])
    font = block.find(arcmap.STD_FONT) + 20
    assert struct.unpack_from('<HBHI', block, font + 1) == (0x86, 0, 400, 120000)
    assert block[font + 10] == 4 and block[font + 11:font + 15].decode('gbk') == '黑体'


def test_label_expression_template_is_vbscript():
    assert arcmap.label_expression({'field': '名称'}, {}) == ('[名称]', True)
    assert arcmap.label_expression({'field': 'select'}, {'select': 'select_'}) == ('[select_]', True)
    assert arcmap.label_expression({'template': '{name}（{等级}）'}, {}) == ('[name] & "（" & [等级] & "）"', False)
    assert arcmap.label_expression({'template': 'say "{a}"'}, {}) == ('"say """ & [a] & """"', False)


# --- the package ---------------------------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp('arcmap')
    fixture = pack_fixture.build(root / 'data')
    manifest = run(fixture['style'], root / 'out', '--lyr', '--data-root', f'basemap={fixture["basemap"]}')
    return fixture, root / 'out', manifest


def extract_lpk(lpk, target):
    target.mkdir()
    subprocess.run(['bsdtar', '-xf', str(lpk), '-C', str(target)], check=True)
    return target


def test_layer_package_layout(demo, tmp_path):
    _, out, manifest = demo
    lpk = out / 'demo-map.lpk'
    assert package.archive_kind(lpk) == '7z'
    members = {name.rstrip('/') for name, *_ in package.list_members(lpk, '7z')[0]}
    assert {'v10/0000demo-map.lyr', 'v105/0000demo-map.lyr', 'v107/0000demo-map.lyr', 'esriinfo/item.pkinfo', 'esriinfo/iteminfo.xml',
            'v107/rasters/height.tif', 'v107/rasters/cover.tif'} <= members
    assert any(m.startswith('v107/demo-map.gdb/a0000000') for m in members)
    assert manifest['manifest'] == 'arcmap-package' and manifest['layer_package']['layer_files'][-1] == 'v107/0000demo-map.lyr'
    assert manifest['layer_file'] == {'file': 'demo-map.lyr', 'geodatabase': 'demo-map.gdb'}
    tree = extract_lpk(lpk, tmp_path / 'x')
    info = (tree / 'esriinfo' / 'item.pkinfo').read_text(encoding='utf-8')
    assert '<version>10.7</version>' in info and '<type>Layer Package</type>' in info
    assert '<typekeyword>lpk</typekeyword>' in (tree / 'esriinfo' / 'iteminfo.xml').read_text(encoding='utf-8')
    newest = arcmap.read_layer_file((tree / 'v107' / '0000demo-map.lyr').read_bytes())
    older = arcmap.read_layer_file((tree / 'v10' / '0000demo-map.lyr').read_bytes())
    assert (tree / 'v10' / '0000demo-map.lyr').read_bytes() == (tree / 'v105' / '0000demo-map.lyr').read_bytes()
    assert arcmap.text_slot('.\\demo-map.gdb') in newest and arcmap.text_slot('..\\v107\\demo-map.gdb') in older
    assert (out / 'demo-map.lyr').read_bytes() == (tree / 'v107' / '0000demo-map.lyr').read_bytes()
    assert not (out / '_lpk').exists()
    record = json.loads((out / 'record.json').read_text())
    assert {'demo-map.lpk', 'demo-map.lyr', 'package.json'} <= set(record['files'])


def test_new_content_gets_a_new_package_id(tmp_path):
    root = tmp_path
    fixture = pack_fixture.build(root / 'data')
    run(fixture['style'], root / 'a', '--data-root', f'basemap={fixture["basemap"]}')
    document = json.loads(fixture['style'].read_text(encoding='utf-8'))
    next(layer for layer in document['layers'] if layer['id'] == 'ground')['renderer']['symbol']['fill']['color'] = '#010203'
    fixture['style'].write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    run(fixture['style'], root / 'b', '--data-root', f'basemap={fixture["basemap"]}')
    identities = []
    for name in 'ab':
        tree = extract_lpk(root / name / 'demo-map.lpk', root / f'{name}x')
        identities.append(re.search('<ID>(.*)</ID>', (tree / 'esriinfo' / 'item.pkinfo').read_text(encoding='utf-8')).group(1))
    assert identities[0] != identities[1]


def test_layer_file_tree_and_renderers(demo):
    _, out, manifest = demo
    stream = arcmap.read_layer_file((out / 'demo-map.lyr').read_bytes())
    ids_are_consistent(stream)
    assert stream[:16] == arcmap.GROUP_LAYER and arcmap.text_slot('Demo map') in stream
    for slot in ('@name', '@dataset', '@workspace', '@browse', '@shape', '@display', '@query', '@field', '@heading', '@value0', '@label0', '@default', '@expression'):
        assert arcmap.text_slot(slot) not in stream, slot
    assert stream.count(arcmap.UNIQUE_RENDERER) == 1 and stream.count(arcmap.BREAKS_RENDERER) == 1
    assert arcmap.text_slot('A 类') in stream and arcmap.text_slot('用地分类') in stream
    rows = {row['id']: row for row in manifest['layers']}
    assert rows['parcels']['arcmap_renderer'] == 'unique values' and rows['far']['arcmap_renderer'] == 'class breaks'
    assert rows['roads']['arcmap_renderer'] == 'one layer per rule' and rows['ground']['arcmap_renderer'] == 'simple'
    assert rows['height']['arcmap_renderer'] is None and rows['height']['file'] == 'v107/rasters/height.tif'
    # the tile layer has no data to pack, the rasters no skeleton: both reported
    assert [d['ref'] for d in manifest['not_packed']] == ['@basemap/tiles']
    items = {(n['layer'], n['item']) for n in manifest['unsupported']}
    assert {('height', 'raster'), ('cover', 'raster'), ('sites', 'size'), ('sites', 'sortBy'), ('roads', 'filter'), ('parcels-outline', 'stroke.dash'),
            ('parcels', 'class.visible'), ('labelPlacements', 'position')} <= items
    # rule layers: each rule takes what the rules before it left
    first, second = '等级 IS NOT NULL AND 等级 = \'主干\'', "(等级 IS NOT NULL AND 等级 IN ('主干', '次干')) AND (NOT ((等级 IS NOT NULL AND 等级 = '主干')))"
    assert arcmap.text_slot(first) in stream and arcmap.text_slot(second) in stream


def test_field_table_is_written_for_the_data(demo):
    _, out, _ = demo
    stream = arcmap.read_layer_file((out / 'demo-map.lyr').read_bytes())
    assert arcmap.text_slot('容积率') in stream and arcmap.text_slot('kind') in stream  # kind is a real field of the land table
    assert arcmap.text_slot('value') not in stream and arcmap.text_slot('Shape_Length') not in stream  # the skeleton's are gone


def test_field_table_survives_the_same_fields():
    tail = arcmap.split_layer(arcmap.skeleton('polygon'))[2]
    fields = [('OBJECTID', False, True), ('Shape', False, False), ('Shape_Length', True, True), ('Shape_Area', True, True), ('kind', False, False), ('value', True, False)]
    assert arcmap.renumber(arcmap.field_table(tail, fields)) == arcmap.renumber(tail)


def test_unpack_reads_the_package_back(demo, tmp_path):
    out = demo[1]
    result = subprocess.run([sys.executable, str(SCRIPTS / 'package.py'), 'unpack', str(out / 'demo-map.lpk'), '--output', str(tmp_path / 'u')],
                            capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / 'u' / 'package.json').read_text())
    assert manifest['package']['type'] == 'Layer Package' and manifest['item']['title'] == 'Demo map'
    assert any(row['path'].endswith('demo-map.gdb') or 'demo-map.gdb' in row['path'] for row in manifest['datasets'])


def test_lyr_belongs_to_arcmap(demo, tmp_path):
    fixture = demo[0]
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(tmp_path / 'x'), '--lyr', '--data-root', f'basemap={fixture["basemap"]}']
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 1 and '--lyr belongs to --format arcmap' in result.stderr


def test_capabilities_declares_the_manifest():
    result = subprocess.run([sys.executable, str(SCRIPTS / 'gis.py'), 'capabilities', '--json'], capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    assert 'arcmap-package' in result.stdout


# --- a map to measure on a render ----------------------------------------------------------------------------------

CHECK_BBOX = [10.0, 0.4, 10.1, 0.5]
CHECK_CRS = 'EPSG:32632'
SAMPLES = {
    # class colours, byte for byte; a value on a class boundary goes to the class above it, NULL to the default
    'single': (10.005, 0.495), 'text-a': (10.015, 0.495), 'text-b': (10.025, 0.495), 'text-null': (10.035, 0.495), 'text-unlisted': (10.045, 0.495),
    'number-15': (10.055, 0.495), 'number-4': (10.065, 0.495), 'number-other': (10.075, 0.495),
    'band-05': (10.005, 0.485), 'band-15': (10.015, 0.485), 'band-25': (10.025, 0.485), 'band-30': (10.035, 0.485),
    'half': (10.045, 0.485), 'rule-1': (10.055, 0.485), 'rule-2': (10.065, 0.485), 'rule-else': (10.075, 0.485), 'filtered': (10.085, 0.485),
    'hollow': (10.005, 0.475), 'hidden-class': (10.015, 0.475), 'layer-off': (10.025, 0.475), 'over-under': (10.035, 0.475),
    'line': (10.05, 0.45), 'marker': (10.03, 0.42), 'ground': (10.095, 0.405),
}
WANTED = {'single': '#123456', 'text-a': '#ed7d31', 'text-b': '#4f81bd', 'text-null': '#bbbbbb', 'text-unlisted': '#bbbbbb',
          'number-15': '#aa0000', 'number-4': '#00aa00', 'number-other': '#ffffff',
          'band-05': '#fff5eb', 'band-15': '#fd8d3c', 'band-25': '#a63603', 'band-30': '#a63603',
          'half': '#7f7f7f', 'rule-1': '#c00000', 'rule-2': '#ff9900', 'rule-else': '#888888', 'filtered': '#ffffff',
          'hollow': '#ffffff', 'hidden-class': '#ffffff', 'layer-off': '#ffffff', 'over-under': '#0000ff',
          'line': '#00aa55', 'marker': '#7030a0', 'ground': '#ffffff'}


def cell(key):
    lon, lat = SAMPLES[key]
    return box(lon - 0.004, lat - 0.004, lon + 0.004, lat + 0.004)


def build_check_map(root):
    """Polygons with known colours in a grid, lines and markers of known sizes, on a white ground, in UTM (the skeletons are WGS 84)."""
    project = root / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'gis-project.json').write_text(json.dumps({'schemaVersion': 1, 'id': 'c', 'name': 'C', 'external': {}}), encoding='utf-8')

    def save(name, columns, geometries):
        path = project / 'data' / f'{name}.geojson'
        gpd.GeoDataFrame(columns, geometry=list(geometries), crs='EPSG:4326').to_crs(CHECK_CRS).to_file(path, driver='GeoJSON')
        return f'data/{name}.geojson'

    ground = save('ground', {'kind': ['all']}, [box(*CHECK_BBOX)])
    single = save('single', {'kind': ['x', 'y', 'off', 'top', 'under', 'half']}, [cell('single'), cell('hollow'), cell('layer-off'), cell('over-under'), cell('over-under'), cell('half')])
    text = save('text', {'类别': ['甲', '乙', None, '丙', '丁']}, [cell('text-a'), cell('text-b'), cell('text-null'), cell('text-unlisted'), cell('hidden-class')])
    number = save('number', {'code': [1.5, 4.0, 9.0]}, [cell('number-15'), cell('number-4'), cell('number-other')])
    bands = save('bands', {'容积率': [0.5, 1.5, 2.5, 3.0], 'tag': ['p', 'q', 'r', 's']}, [cell('band-05'), cell('band-15'), cell('band-25'), cell('band-30')])
    rules = save('rules', {'grade': ['main', 'main2', 'other', 'gone']}, [cell('rule-1'), cell('rule-2'), cell('rule-else'), cell('filtered')])
    lines = save('lines', {'kind': ['a']}, [LineString([(10.0, 0.45), (10.1, 0.45)])])
    points = save('points', {'名称': ['甲站', '乙站'], 'kind': ['a', 'b']}, [Point(10.03, 0.42), Point(10.07, 0.42)])

    def fill(color, **extra):
        return {'fill': {'color': color, **extra}}

    layers = [
        {'id': 'ground', 'geometry': 'polygon', 'data': {'path': ground}, 'renderer': {'type': 'single', 'symbol': fill('#FFFFFF')}},
        {'id': 'single', 'geometry': 'polygon', 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'x'}, 'renderer': {'type': 'single', 'symbol': {
            **fill('#123456'), 'stroke': {'color': '#FF0000', 'width': 4}}}},
        {'id': 'hollow', 'geometry': 'polygon', 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'y'},
         'renderer': {'type': 'single', 'symbol': {'stroke': {'color': '#000000', 'width': 2}}}},
        {'id': 'off', 'geometry': 'polygon', 'visible': False, 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'off'}, 'renderer': {'type': 'single', 'symbol': fill('#FF00FF')}},
        {'id': 'half', 'geometry': 'polygon', 'opacity': 0.5, 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'half'}, 'renderer': {'type': 'single', 'symbol': fill('#000000')}},
        {'id': 'under', 'geometry': 'polygon', 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'under'}, 'renderer': {'type': 'single', 'symbol': fill('#FF0000')}},
        {'id': 'top', 'geometry': 'polygon', 'data': {'path': single}, 'filter': {'field': 'kind', 'op': '==', 'value': 'top'}, 'renderer': {'type': 'single', 'symbol': fill('#0000FF')}},
        {'id': 'text', 'title': '文字分类', 'geometry': 'polygon', 'data': {'path': text}, 'renderer': {
            'type': 'categorized', 'field': '类别', 'default': fill('#BBBBBB'), 'classes': [
                {'value': '甲', 'symbol': fill('#ED7D31')}, {'value': '乙', 'label': '乙类', 'symbol': fill('#4F81BD')},
                {'value': '丁', 'visible': False, 'symbol': fill('#FF00FF')}]}},
        {'id': 'number', 'geometry': 'polygon', 'data': {'path': number}, 'renderer': {
            'type': 'categorized', 'field': 'code', 'classes': [{'value': 1.5, 'symbol': fill('#AA0000')}, {'value': 4, 'symbol': fill('#00AA00')}]}},
        {'id': 'bands', 'title': '分级', 'geometry': 'polygon', 'data': {'path': bands}, 'renderer': {
            'type': 'graduated', 'field': '容积率', 'method': 'manual', 'classes': [
                {'range': [0, 1.5], 'symbol': fill('#FFF5EB')}, {'range': [1.5, 2.5], 'symbol': fill('#FD8D3C')}, {'range': [2.5, 3.0], 'symbol': fill('#A63603')}]}},
        {'id': 'rules', 'geometry': 'polygon', 'data': {'path': rules}, 'filter': {'field': 'grade', 'op': '!=', 'value': 'gone'}, 'renderer': {'type': 'rule', 'rules': [
            {'filter': {'field': 'grade', 'op': '==', 'value': 'main'}, 'label': 'first', 'symbol': fill('#C00000')},
            {'filter': {'field': 'grade', 'op': 'in', 'value': ['main', 'main2']}, 'label': 'second', 'symbol': fill('#FF9900')}], 'default': fill('#888888')}},
        {'id': 'lines', 'geometry': 'line', 'data': {'path': lines}, 'renderer': {'type': 'single', 'symbol': {'stroke': {'color': '#00AA55', 'width': 6}}}},
        {'id': 'points', 'geometry': 'point', 'data': {'path': points}, 'filter': {'field': 'kind', 'op': '==', 'value': 'a'},
         'renderer': {'type': 'single', 'symbol': {**fill('#7030A0'), 'marker': {'shape': 'square', 'size': 30}}}},
        {'id': 'circle', 'geometry': 'point', 'data': {'path': points}, 'filter': {'field': 'kind', 'op': '==', 'value': 'b'},
         'renderer': {'type': 'single', 'symbol': {**fill('#C00000'), 'stroke': {'color': '#0000FF', 'width': 4}, 'marker': {'shape': 'circle', 'size': 30}}},
         'labels': {'field': '名称', 'text': {'size': 18, 'color': '#0000AA', 'halo': {'color': '#FFFF00', 'width': 2}}}},
    ]
    document = {'contract': 'map-style', 'schema_version': 1, 'id': 'check-map', 'title': 'Check map', 'units': {'size': 'px'}, 'dataRoot': 'project',
                'extent': {'bbox': CHECK_BBOX}, 'layers': layers}
    style = project / 'check-map.map-style.json'
    style.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return style


def test_check_map_packs_without_surprises(tmp_path):
    style = build_check_map(tmp_path)
    manifest = run(style, tmp_path / 'out', '--lyr')
    assert {row['id']: row['arcmap_renderer'] for row in manifest['layers']}['bands'] == 'class breaks'
    assert {(n['layer'], n['item']) for n in manifest['unsupported']} == {('text', 'class.visible')}
    # the rules layer: a filter joined to every rule's own condition
    stream = arcmap.read_layer_file((tmp_path / 'out' / 'check-map.lyr').read_bytes())
    assert arcmap.text_slot("(grade IS NULL OR grade <> 'gone') AND (grade IS NOT NULL AND grade = 'main')") in stream


def check_arcmap_report(report, png):
    """What ArcMap read and drew of build_check_map: no broken layer, renderer types, class lists, colours byte for byte, sizes in pixels."""
    layers = [layer for row in report['files'] for layer in row['layers']]
    assert not [row for row in report['files'] if row.get('error')]
    plain = {layer['name']: layer for layer in layers if not layer['group']}
    assert not [name for name, layer in plain.items() if layer['broken']]
    assert plain['文字分类']['symbology'] == 'UNIQUE_VALUES' and plain['文字分类']['values'] == ['甲', '乙', '丁'] and plain['文字分类']['show_other']
    assert plain['number']['symbology'] == 'UNIQUE_VALUES' and plain['number']['values'] == ['1.5', '4']
    assert plain['分级']['symbology'] == 'GRADUATED_COLORS' and plain['分级']['breaks'][0] == 0 and plain['分级']['breaks'][-1] == 3
    assert plain['single']['sr'] == 'WGS_1984_UTM_Zone_32N' and plain['half']['transparency'] == 50
    assert plain['off']['visible'] is False and plain['circle']['show_labels'] is True
    assert plain['single']['features'] == 1 and plain['circle']['label_classes'][0]['expression'] == '[名称]'
    assert report['samples'] == WANTED
    # sizes: at 96 dpi one map-style px is one pixel
    import rasterio
    with rasterio.open(png) as source:
        image = source.read()[:3].transpose(1, 2, 0).astype(int)
    xmin, ymin, xmax, ymax = report['frame_extent']
    from pyproj import Transformer
    forward = Transformer.from_crs('EPSG:4326', CHECK_CRS, always_xy=True).transform

    def pixel(lon, lat):
        x, y = forward(lon, lat)
        return int((x - xmin) / (xmax - xmin) * image.shape[1]), int((ymax - y) / (ymax - ymin) * image.shape[0])

    def run_of(flags):
        return int(np.sum(flags))

    def is_colour(window, hex_colour, tolerance=8):
        colour = np.array([int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)])
        return np.abs(window - colour).max(axis=-1) <= tolerance

    x, y = pixel(10.05, 0.45)
    assert run_of(is_colour(image[y - 20:y + 20, x], '#00AA55')) == pytest.approx(6, abs=1)  # a 6 px line
    x, y = pixel(10.03, 0.42)
    assert run_of(is_colour(image[y, x - 40:x + 40], '#7030A0')) == pytest.approx(30, abs=1)  # a 30 px square
    assert run_of(is_colour(image[y - 40:y + 40, x], '#7030A0')) == pytest.approx(30, abs=1)
    x, y = pixel(10.07, 0.42)
    row = is_colour(image[y, x - 40:x + 40], '#C00000')
    assert run_of(row) == pytest.approx(30 - 4, abs=4)  # the circle, its outline over the edge
    assert is_colour(image[y, x - 40:x + 40], '#0000FF').any()
    x, y = pixel(*SAMPLES['single'])
    down = image[y:y + 60, x]
    assert run_of(is_colour(down, '#FF0000')) == pytest.approx(4, abs=1)  # the outline
    assert is_colour(image, '#FFFF00').any() and is_colour(image, '#0000AA').any()  # the label's halo and text


def sample_arguments():
    return [item for name, (lon, lat) in SAMPLES.items() for item in ('--sample', f'{name}={lon},{lat}')]


@pytest.mark.skipif(ARCMAP_PYTHON is None, reason='ArcMap not available (set GIS_KIT_ARCMAP_PYTHON to its Python)')
def test_arcmap_opens_the_package(tmp_path):
    style = build_check_map(tmp_path)
    out = tmp_path / 'out'
    run(style, out, '--lyr')
    png = tmp_path / 'check.png'
    result = subprocess.run([ARCMAP_PYTHON, str(TESTS / 'arcmap_open_check.py'), str(tmp_path / 'work'), str(png), '32632', ','.join(map(str, CHECK_BBOX)),
                             str(out / 'check-map.lyr'), *sample_arguments()], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    check_arcmap_report(json.loads(result.stdout[result.stdout.index('{"'):]), png)
    result = subprocess.run([ARCMAP_PYTHON, str(TESTS / 'arcmap_open_check.py'), str(tmp_path / 'work2'), str(tmp_path / 'lpk.png'), '32632', ','.join(map(str, CHECK_BBOX)),
                             str(out / 'check-map.lpk')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout[result.stdout.index('{"'):])
    assert not [layer for row in report['files'] for layer in row['layers'] if layer.get('broken')] and len(report['files']) == 3


@pytest.mark.skipif(ARCGIS_PYTHON is None, reason='ArcGIS Pro not available (set GIS_KIT_ARCGIS_PYTHON to its Python)')
def test_arcgis_pro_opens_the_package(tmp_path):
    style = build_check_map(tmp_path)
    out = tmp_path / 'out'
    run(style, out, '--lyr')
    result = subprocess.run([ARCGIS_PYTHON, str(TESTS / 'arcmap_open_check_pro.py'), str(tmp_path / 'work'), str(out / 'check-map.lyr'), str(out / 'check-map.lpk')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    check_pro_report(json.loads(result.stdout[result.stdout.index('{"'):]))


def check_pro_report(report):
    assert not [row for row in report['files'] if row.get('error')] and len(report['files']) == 2
    for row in report['files']:
        plain = {layer['name']: layer for layer in row['layers'] if not layer['group']}
        assert not [name for name, layer in plain.items() if layer['broken']]
        assert plain['文字分类']['renderer'] == 'CIMUniqueValueRenderer' and plain['分级']['renderer'] == 'CIMClassBreaksRenderer'
        assert plain['single']['colours'][-1] == [0x12, 0x34, 0x56]
        classes = {c['label']: c['colours'] for c in plain['文字分类']['classes']}
        assert classes['甲'][-1] == [0xED, 0x7D, 0x31] and classes['乙类'][-1] == [0x4F, 0x81, 0xBD]
