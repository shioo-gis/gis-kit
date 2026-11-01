"""package.py pack: a map-style map and its data into one GeoPackage styled for QGIS (made-up data only)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET

import geopandas as gpd
import pyogrio
import pytest
import rasterio

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import _map_style  # noqa: E402
import _qgis_style as qgis  # noqa: E402
import pack_fixture  # noqa: E402
import package  # noqa: E402

QGIS_PYTHON = os.environ.get('GIS_KIT_QGIS_PYTHON') or next(
    (p for p in ('/Applications/QGIS.app/Contents/MacOS/bin/python3',) if Path(p).is_file()), None)


def run(fixture, output, *extra, success=True, env=None):
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(output),
               '--data-root', f'basemap={fixture["basemap"]}', *map(str, extra)]
    result = subprocess.run(command, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GIS_COMPOSER_CONFIG_DIR': str(output.parent / 'no-config'),
                                 **(env or {})})
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return json.loads((output / 'package.json').read_text()) if success else result.stderr


@pytest.fixture(scope='module')
def packs(tmp_path_factory):
    root = tmp_path_factory.mktemp('pack')
    fixture = pack_fixture.build(root / 'data')
    out = {scope: root / scope for scope in ('extent', 'clip', 'full')}
    manifests = {'extent': run(fixture, out['extent']), 'clip': run(fixture, out['clip'], '--clip'),
                 'full': run(fixture, out['full'], '--full')}
    return fixture, out, manifests


def styles(gpkg):
    with sqlite3.connect(gpkg) as db:
        rows = db.execute('SELECT f_table_name, styleName, useAsDefault, styleQML FROM layer_styles ORDER BY id').fetchall()
    db.close()
    return {name: {'table': table, 'default': bool(default), 'qml': ET.fromstring(text.split('\n', 1)[1])} for table, name, default, text in rows}


def test_default_scope_keeps_whole_features_that_touch_the_extent(packs):
    fixture, out, manifests = packs
    manifest = manifests['extent']
    gpkg = out['extent'] / 'demo-map.gpkg'
    assert manifest['manifest'] == 'qgis-package' and manifest['scope'] == 'extent'
    rows = {d['ref']: d for d in manifest['datasets']}
    assert rows['@basemap/land.geojson']['features'] == 1 and rows['@basemap/land.geojson']['source_features'] == 2
    assert rows['@basemap/land.geojson']['root'] == '@basemap'
    assert rows['inputs/analysis/roads.geojson']['features'] == 2 and rows['inputs/analysis/sites.geojson']['features'] == 2
    parcels = gpd.read_file(gpkg, layer='parcels')
    source = gpd.read_file(fixture['project'] / 'inputs' / 'analysis' / 'parcels.gpkg', layer='parcels')
    assert len(parcels) == 16 and 'clipped' not in parcels.columns
    assert parcels.crs.to_epsg() == 32632  # data keep their own CRS
    assert sorted(parcels.geometry.area.round(1)) == sorted(source.geometry.area.round(1))  # whole features, not cut
    assert gpd.read_file(gpkg, layer='land').crs.to_epsg() == 4326
    with rasterio.open(gpkg, table='height') as height:  # the extent's window of the float grid, stored in the GeoPackage
        assert (height.width, height.height) == (41, 40) and height.dtypes[0] == 'float32'
    assert (out['extent'] / 'rasters' / 'cover.tif').is_file()  # a Byte raster with nodata is not lossless as GeoPackage tiles
    assert {d['ref']: d['storage'] for d in manifest['datasets'] if d['kind'] == 'raster'} == {
        'inputs/analysis/height.tif': 'geopackage', 'inputs/analysis/cover.tif': 'geotiff'}
    with sqlite3.connect(gpkg) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 10200
    db.close()
    record = json.loads((out['extent'] / 'record.json').read_text())
    assert record['tool'] == 'package pack' and set(record['files']) == {'demo-map.gpkg', 'package.json', 'rasters/cover.tif'}


def test_clip_cuts_at_the_extent_and_marks_cut_features(packs):
    fixture, out, manifests = packs
    gpkg = out['clip'] / 'demo-map.gpkg'
    parcels = gpd.read_file(gpkg, layer='parcels')
    assert parcels['clipped'].sum() == 12 == next(d for d in manifests['clip']['datasets'] if d['table'] == 'parcels')['clipped_features']
    extent = package.extent_in(pack_fixture.BBOX, parcels.crs)
    assert parcels.geometry.buffer(-0.01).covered_by(extent).all()
    assert (parcels.loc[parcels['clipped'], 'geometry'].area < parcels.loc[parcels['clipped'], '面积'] - 1).all()
    assert set(parcels.geometry.geom_type) <= {'Polygon', 'MultiPolygon'}
    roads = gpd.read_file(gpkg, layer='roads')
    assert len(roads) == 2 and roads['clipped'].all()


def test_full_keeps_whole_datasets(packs):
    fixture, out, manifests = packs
    rows = {d['table']: d for d in manifests['full']['datasets'] if d['table']}
    assert rows['land']['features'] == 2 and rows['roads']['features'] == 3 and rows['sites']['features'] == 3
    assert rows['height']['size'] == [80, 60] and 'window' not in rows['height']
    assert {d['scope'] for d in manifests['full']['datasets']} == {'full'}


def test_one_table_per_dataset_and_one_style_per_layer(packs):
    out, manifest = packs[1], packs[2]['extent']
    gpkg = out['extent'] / 'demo-map.gpkg'
    tables = {row[0] for row in pyogrio.list_layers(gpkg)}
    assert {'land', 'parcels', 'roads', 'sites', 'map_labels', 'map_label_leaders'} <= tables
    parcels = next(d for d in manifest['datasets'] if d['table'] == 'parcels')
    assert parcels['layers'] == ['parcels', 'far', 'parcels-outline', 'inset.parcels']
    found = styles(gpkg)
    assert {name: (s['table'], s['default']) for name, s in found.items() if s['table'] == 'parcels'} == {
        'parcels': ('parcels', True), 'far': ('parcels', False), 'parcels-outline': ('parcels', False), 'inset.parcels': ('parcels', False)}
    defaults = [s['table'] for s in found.values() if s['default']]
    assert len(defaults) == len(set(defaults))  # exactly one default style per table


def test_renderers_in_qml(packs):
    found = styles(packs[1]['extent'] / 'demo-map.gpkg')
    categorized = found['parcels']['qml'].find('renderer-v2')
    assert categorized.get('type') == 'categorizedSymbol' and categorized.get('attr') == '类别'
    categories = [(c.get('value'), c.get('render')) for c in categorized.iter('category')]
    assert categories == [('A 类', 'true'), ('B 类', 'true'), ('C 类', 'false'), ('', 'true')]  # '' = QGIS "all other values"
    fill = categorized.find("symbols/symbol[@name='0']/layer[@class='SimpleFill']")
    assert {o.get('name'): o.get('value') for o in fill.iter('Option')}['color'] == '237,125,49,255'
    c_fill = categorized.find("symbols/symbol[@name='2']/layer[@class='SimpleFill']")
    assert {o.get('name'): o.get('value') for o in c_fill.iter('Option')}['color'] == '155,187,89,204'  # opacity 0.8
    far = found['far']['qml']
    assert far.find('layerOpacity').text == '0.7'
    parent = far.find('renderer-v2/rules/rule')  # the layer filter wraps the graduated classes
    assert parent.get('filter') == '("类别" IS NOT NULL)'
    children = parent.findall('rule')
    assert children[0].get('filter') == 'coalesce((("容积率" IS NOT NULL AND "容积率" >= 0) AND ("容积率" IS NOT NULL AND "容积率" < 1.5)), FALSE)'
    assert children[-1].get('filter').startswith('coalesce((("容积率" IS NOT NULL AND "容积率" >= 2.5) AND ("容积率" IS NOT NULL AND "容积率" <= 3))')
    outline = found['parcels-outline']['qml']
    line = {o.get('name'): o.get('value') for o in outline.find(".//layer[@class='SimpleLine']").iter('Option')}
    assert line['line_width'] == qgis.number(0.8 * 25.4 / 96) and line['use_custom_dash'] == '1'
    assert line['customdash'] == ';'.join(qgis.number(v * 25.4 / 96) for v in (4, 2)) and line['capstyle'] == 'flat'
    assert outline.get('labelsEnabled') == '0' and outline.find('labeling').get('type') == 'simple'  # enabled: false
    roads = found['roads']['qml']
    rules = roads.findall('renderer-v2/rules/rule')
    assert [r.get('label') for r in rules] == ['主干', '次干', '长路']  # the rule in an unknown dialect is skipped, not a catch-all
    assert rules[1].get('filter') == ('coalesce(("等级" IN (\'主干\', \'次干\')), FALSE) AND NOT coalesce(("等级" IS NOT NULL AND "等级" = \'主干\'), FALSE)')
    assert rules[2].get('filter').startswith('coalesce(length($geometry) > 100, FALSE) AND NOT')  # dialect qgis used as written
    assert roads.get('labelsEnabled') == '1'
    assert roads.find('labeling/settings/text-style').get('fieldName') == 'concat("name", \'（\', "等级", \'）\')'
    sites = found['sites']['qml']
    marker = sites.find(".//layer[@class='SimpleMarker']")
    size = next(o for o in marker.iter('Option') if o.get('name') == 'expression').get('value')
    assert size == '2 * 0.26458333333333334 * (3 + 7 * max(0, min(1, (sqrt("规模") - 0) / 4)))'
    assert sites.find('renderer-v2').get('enableorderby') == '1' and sites.find('renderer-v2/orderby/orderByClause').get('asc') == '0'
    assert found['names']['qml'].find('renderer-v2').get('type') == 'nullSymbol'
    names = found['names']['qml'].find('labeling/settings/text-style')  # the theme alias resolved through fonts[].names
    assert (names.get('fontFamily'), names.get('namedStyle')) == ('Arial', 'Bold')
    marks = {o.get('name'): o.get('value') for o in found['site-marks']['qml'].find(".//layer[@class='SimpleMarker']/Option")}
    assert (marks['name'], marks['angle'], marks['color'], marks['outline_color']) == ('cross2', '10', '0,0,0,0', '0,0,0,255')
    assert found['ground']['qml'].find('renderer-v2').get('type') == 'singleSymbol'


def test_label_placements_become_a_point_layer_with_leaders(packs):
    out, manifest = packs[1], packs[2]['extent']
    gpkg = out['extent'] / 'demo-map.gpkg'
    labels = gpd.read_file(gpkg, layer='map_labels')
    assert labels.crs.to_epsg() == 4326 and list(labels['text']) == ['Site 1', 'Site 2\nsecond line', 'Inset']
    assert list(labels['quadrant']) == [5, 0, 4]  # anchor left -> text to the right; bottom-right -> above left; center
    assert list(labels['panel']) == ['main', 'main', 'inset'] and list(labels['style_id']) == [1, 2, 3]
    assert labels['size_mm'][0] == pytest.approx(13.4 * 25.4 / 96) and labels['halo_color'][0] == '#FFFFFF'
    assert (labels['font'][0], labels['font_style'][0], labels['font_name'][0]) == ('Arial', 'Bold', 'Demo Sans')
    assert labels['halo_color'].isna()[1] and labels['rotation'][1] == 15 and labels['font'][2] == 'Plain Alias'
    assert labels.geometry[0].coords[0] == (10.021, 0.421)
    leaders = gpd.read_file(gpkg, layer='map_label_leaders')
    assert list(leaders['label_id']) == [2, 3] and list(leaders['stroke_id']) == [1, 2]
    assert list(leaders.geometry[0].coords) == [(10.05, 0.45), (10.07, 0.47)]
    style = styles(gpkg)['map_labels']['qml']
    assert style.get('labelsEnabled') == '1' and style.find('renderer-v2').get('type') == 'nullSymbol'
    rules = style.findall('labeling/rules/rule')  # one rule per distinct text style, static settings
    assert [r.get('filter') for r in rules] == ['"style_id" = 1', '"style_id" = 2', '"style_id" = 3']
    first = rules[0].find('settings/text-style')
    size = 13.4 * 25.4 / 96
    assert (first.get('fontFamily'), first.get('namedStyle'), first.get('textOpacity')) == ('Arial', 'Bold', '0.9')
    assert float(first.get('fontLetterSpacing')) == pytest.approx(0.1 * size)
    buffer = first.find('text-buffer')
    assert buffer.get('bufferOpacity') == '0.78'
    blur = {o.get('name'): o.get('value') for o in buffer.find('effect').iter('Option')}
    assert float(blur['blur_level']) == pytest.approx(0.6 * 25.4 / 96) and blur['blur_unit'] == 'MM'
    box = first.find('background')
    assert (box.get('shapeDraw'), box.get('shapeFillColor'), box.get('shapeBorderColor')) == ('1', '255,255,204,255', '51,51,51,255')
    assert float(box.get('shapeSizeX')) == pytest.approx(2 * 25.4 / 96) and float(box.get('shapeBorderWidth')) == pytest.approx(0.5 * 25.4 / 96)
    defined = {o.get('name') for o in rules[0].find('settings/dd_properties/Option/Option[@name="properties"]')}
    assert defined == {'OffsetQuad', 'LabelRotation'}
    leader_style = styles(gpkg)['map_label_leaders']['qml']
    lines = [{o.get('name'): o.get('value') for o in layer.iter('Option')} for layer in leader_style.iter('layer')]
    assert (lines[0]['line_color'], lines[0]['use_custom_dash']) == ('0,0,255,255', '1')  # leaderStroke
    assert (lines[1]['line_color'], lines[1]['line_width']) == ('0,0,0,255', qgis.number(25.4 / 96))  # text colour, 1 px
    assert manifest['label_placements'] == {'labels': 3, 'leaders': 2, 'table': 'map_labels', 'leader_table': 'map_label_leaders',
                                            'text_styles': 3, 'leader_strokes': 2}


def test_embedded_project(packs):
    out, manifest = packs[1], packs[2]['extent']
    gpkg = out['extent'] / 'demo-map.gpkg'
    project = ET.fromstring(qgis.read_project(gpkg, 'demo-map').split('\n', 1)[1])
    assert project.find('projectCrs/spatialrefsys/authid').text == 'EPSG:32632' == manifest['project']['crs']
    assert project.find('properties/SpatialRefSys/ProjectionsEnabled').text == '1'
    order = [item.text.rsplit('_', 5)[0] for item in project.findall('layer-tree-group/custom-order/item')]
    assert order == ['map_labels_inset', 'map_labels_main', 'map_label_leaders_inset', 'map_label_leaders_main', 'inset_parcels', 'site_marks',
                     'names', 'sites', 'roads', 'parcels_outline', 'far', 'parcels', 'cover', 'height', 'ground']
    panels = project.findall('layer-tree-group/layer-tree-group')  # a group per panel first, then the map-style groups
    assert [(g.get('name'), g.get('checked')) for g in panels] == [('main', 'Qt::Checked'), ('inset', 'Qt::Unchecked')]
    assert [g.get('name') for g in panels[0].findall('layer-tree-group')] == ['labels', 'points', 'lines', 'fills', 'ground']
    assert [g.get('name') for g in panels[1].findall('layer-tree-group')] == ['labels', 'fills']
    unchecked = {l.get('name') for l in project.iter('layer-tree-layer') if l.get('checked') == 'Qt::Unchecked'}
    assert unchecked == {'容积率', 'cover'}
    themes = {t.get('name'): t for t in project.findall('visibility-presets/visibility-preset')}
    assert set(themes) == {'main', 'inset'}
    assert [n.get('id') for n in themes['inset'].iter('checked-group-node')] == ['inset', 'inset/fills', 'inset/labels']
    assert [b.get('name') for b in project.iter('Bookmark')] == ['main', 'inset']
    assert [project.find(f'properties/Gui/CanvasColor{c}Part').text for c in ('Red', 'Green', 'Blue')] == ['180', '214', '226']
    sources = {l.find('id').text.rsplit('_', 5)[0]: l.find('datasource').text for l in project.iter('maplayer')}
    assert sources['map_labels_inset'] == './demo-map.gpkg|layername=map_labels|subset="panel" = \'inset\''
    sources = {l.find('layername').text: l.find('datasource').text for l in project.iter('maplayer')}
    assert sources['用地分类'] == './demo-map.gpkg|layername=parcels' and sources['height'] == 'GPKG:./demo-map.gpkg:height'
    assert sources['cover'] == './rasters/cover.tif'
    raster = next(l for l in project.iter('maplayer') if l.find('layername').text == 'height')
    assert raster.find('pipe/rasterrenderer').get('type') == 'singlebandpseudocolor'
    assert [i.get('value') for i in raster.iter('item')] == ['0', '70', '138']
    extent = [float(project.find(f'mapcanvas/extent/{k}').text) for k in ('xmin', 'ymin', 'xmax', 'ymax')]
    assert extent == pytest.approx(manifest['project']['extent'])
    with sqlite3.connect(gpkg) as db:
        assert db.execute("SELECT data_type FROM gpkg_contents WHERE table_name = 'layer_styles'").fetchone() == ('attributes',)
    db.close()


def test_manifest_reports_unsupported_fonts_and_external_data(packs):
    manifest = packs[2]['extent']
    reasons = {(n['layer'], n['item']) for n in manifest['unsupported']}
    assert ('roads', 'filter') in reasons and ('height', 'renderer.stretch') in reasons and ('labelPlacements', 'font') in reasons
    assert any('arcade' in n['reason'] and 'skipped' in n['reason'] for n in manifest['unsupported'])
    fonts = {row['name']: row for row in manifest['fonts']}
    assert (fonts['Demo Sans']['family'], fonts['Demo Sans']['style']) == ('Arial', 'Bold') and 'labelPlacements' in fonts['Demo Sans']['used_by']
    assert fonts['Plain Alias']['unresolved']
    assert any('fonts Arial Bold, Plain Alias' in w for w in manifest['warnings'])
    assert manifest['not_packed'] == [{'ref': '@basemap/tiles', 'layers': ['tiles'], 'reason': 'rasterTiles: tiles are not packed'}]
    assert manifest['producer_notes'][0]['reason'] == 'a made-up note' and any('producer reports 1' in w for w in manifest['warnings'])
    assert manifest['background'] == '#B4D6E2'
    assert manifest['panels'] == [{'id': 'main', 'shown': True, 'bookmark': True}, {'id': 'inset', 'shown': False, 'bookmark': True}]
    assert any('external data roots' in w for w in manifest['warnings'])
    assert any('rasters/cover.tif' in w for w in manifest['warnings'])
    layers = {l['id']: l for l in manifest['layers']}
    assert layers['far']['qgis_renderer'] == 'RuleRenderer' and layers['far']['filter'] == '("类别" IS NOT NULL)'
    assert layers['parcels-outline']['labels'] == 'off' and layers['roads']['labels'] == 'on'


def test_filters_and_class_edges():
    notes = []
    assert qgis.expression({'field': 'a', 'op': '!=', 'value': "it's"}, {}, notes) == '("a" IS NULL OR "a" <> \'it\'\'s\')'
    assert qgis.expression({'field': 'a', 'op': 'notIn', 'value': [1, 2.5]}, {}, notes) == '("a" IS NULL OR "a" NOT IN (1, 2.5))'
    assert qgis.expression({'not': {'field': 'b', 'op': '==', 'value': True}}, {}, notes) == \
        '(NOT coalesce(("b" IS NOT NULL AND "b" = TRUE), FALSE))'
    assert qgis.expression({'any': [{'field': 'c', 'op': 'isNull'}, {'expression': '"c" > 1', 'dialect': 'QGIS'}]}, {}, notes) == \
        '(("c" IS NULL) OR ("c" > 1))'
    assert notes == []
    assert qgis.expression({'all': [{'field': 'c', 'op': 'isNull'}, {'expression': 'x', 'dialect': 'arcade'}]}, {'layer': 'l'}, notes) is None
    assert notes and notes[0]['layer'] == 'l'
    layer = {'id': 'g', 'geometry': 'polygon', 'renderer': {'type': 'graduated', 'field': 'v', 'classes': [
        {'range': [0, 1], 'symbol': {'fill': {'color': '#000000'}}}, {'range': [1, 2], 'symbol': {'fill': {'color': '#FFFFFF'}}}]}}
    text, kind = qgis.qml(layer, [], {})
    ranges = ET.fromstring(text.split('\n', 1)[1]).findall('renderer-v2/ranges/range')
    assert kind == 'graduatedSymbol'
    assert float(ranges[0].get('upper')) < 1.0 == float(ranges[1].get('lower'))  # a value of exactly 1 falls in the upper class
    assert float(ranges[1].get('upper')) == 2.0  # the last class includes its high
    with pytest.raises(ValueError):
        _map_style.check_filter({'field': 'a', 'op': 'in', 'value': 1}, 'here')


def test_marker_shapes():
    def marker(spec):
        layer = {'id': 'm', 'geometry': 'point', 'renderer': {'type': 'single', 'symbol': spec}}
        text, _ = qgis.qml(layer, notes, {})
        found = ET.fromstring(text.split('\n', 1)[1]).find(".//layer[@class='SimpleMarker']")
        values = {o.get('name'): o.get('value') for o in found.find('Option')}
        return values['name'], values['angle'], values['color']
    notes = []
    assert marker({'marker': {'shape': 'arrow', 'size': 8}, 'fill': {'color': '#FF0000'}}) == ('triangle', '0', '255,0,0,255')
    assert marker({'marker': {'shape': 'chevron', 'size': 8, 'rotation': 90}, 'fill': {'color': '#FF0000'}}) == ('arrowhead', '0', '0,0,0,0')
    assert marker({'marker': {'shape': 'cross', 'size': 8}, 'fill': {'color': '#FF0000'}}) == ('cross_fill', '0', '255,0,0,255')
    assert marker({'marker': {'shape': 'x', 'size': 8}, 'fill': {'color': '#FF0000'}}) == ('cross_fill', '45', '255,0,0,255')
    assert marker({'marker': {'shape': 'cross', 'size': 8}, 'stroke': {'color': '#000000', 'width': 1}}) == ('cross', '0', '0,0,0,0')
    text, _ = qgis.qml({'id': 'c', 'geometry': 'point', 'renderer': {'type': 'single', 'symbol': {'marker': {'shape': 'chevron', 'size': 8}, 'fill': {'color': '#FF0000'}}}}, notes, {})
    chevron = {o.get('name'): o.get('value') for o in ET.fromstring(text.split('\n', 1)[1]).find(".//layer[@class='SimpleMarker']").find('Option')}
    assert float(chevron['outline_width']) == pytest.approx(_map_style.mm(8 * 0.34)) and (chevron['cap_style'], chevron['joinstyle']) == ('round', 'round')
    assert chevron['outline_color'] == '255,0,0,255' and float(chevron['offset'].split(',')[0]) == pytest.approx(_map_style.mm(2))  # its tip is on the point: moved to centre it
    assert marker({'fill': {'color': '#FF0000'}})[0] == 'circle' and notes[-1]['item'] == 'symbol.marker'


@pytest.fixture(scope='module')
def round_two(tmp_path_factory):
    root = tmp_path_factory.mktemp('round-two')
    fixture = pack_fixture.build_round_two(root / 'data')
    return fixture, root / 'out', run_plain(fixture, root / 'out')


def run_plain(fixture, output, *extra, success=True):
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(output), *map(str, extra)]
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return json.loads((output / 'package.json').read_text()) if success else result.stderr


def layer_options(node):
    return {o.get('name'): o.get('value') for o in node.find('Option')}


def rules_of(style):
    return {r.get('label'): r.get('symbol') for r in style['qml'].iter('rule') if r.get('symbol') is not None}


def symbol_named(style, name):
    return style['qml'].find(f"renderer-v2/symbols/symbol[@name='{name}']")


def test_font_matching_follows_css():
    table = {'Sans': [{'family': 'Sans', 'weight': w, 'names': {'family': 'Real', 'subfamily': str(w)}} for w in (300, '400', 700)]}
    def picked(weight):
        return _map_style.resolve_font({'font': 'Sans', 'weight': weight}, table)[1]
    assert picked(None) == '400' and picked('400') == '400' and picked(700) == '700'  # missing counts as 400; numeric strings are numbers
    assert picked(500) == '400' and picked(450) == '400'  # 400 to 500: up to 500, then down
    assert picked(600) == '700' and picked(900) == '700'  # above 500: up, then down
    assert picked(200) == '300' and picked(350) == '300'  # below 400: down, then up
    assert _map_style.closest_weight(400, [300, 500, 700]) == 500 and _map_style.closest_weight(500, [300, 700]) == 300
    assert _map_style.closest_weight(100, [300, 700]) == 300 and _map_style.closest_weight(800, [300, 700]) == 700
    # an entry that names no installed font is passed over; a font without any entry takes a style name from its weight
    mixed = {'Sans': [{'family': 'Sans', 'weight': 700}, {'family': 'Sans', 'weight': 400, 'names': {'family': 'Real'}}]}
    assert _map_style.resolve_font({'font': 'Sans', 'weight': 700}, mixed) == ('Real', '')
    assert _map_style.resolve_font({'font': 'Other', 'weight': 700}, {}) == ('Other', 'Bold')


def test_font_weights_and_substitutions_reach_the_manifest(round_two):
    _, _, manifest = round_two
    rows = {(row['name'], row.get('weight')): row for row in manifest['fonts']}
    assert rows['Demo Text', None]['style'] == 'Regular' and rows['Demo Text', 700]['style'] == 'Bold'
    assert rows['Demo Text', 500]['substituted_weight'] == 400 and rows['Demo Text', 900]['substituted_weight'] == 700
    assert 'substituted_weight' not in rows['One Weight', None]  # a single entry and no weight asked: an alias unique on its own
    assert rows['No Style', 700]['style'] == 'Bold'
    notes = [n for n in manifest['unsupported'] if n['item'] == 'font.weight']
    assert len(notes) == 2 and all(n['kind'] == 'approximated' for n in notes) and any('no weight 900' in n['reason'] for n in notes)


def test_auto_labels_on_points_stand_off_the_point():
    def placement(geometry, **spec):
        node = qgis.labeling({'field': 'name', 'text': {'size': 20, 'color': '#000000'}, **spec}, geometry, {}, [])
        return node.find('settings/placement').attrib
    around = placement('point', placement='auto')
    assert (around['placement'], float(around['dist']), around['distUnits']) == ('0', 16, 'Pixel')
    assert placement('point')['dist'] == around['dist']
    on_point = placement('point', placement='point')
    assert (on_point['placement'], float(on_point['dist'])) == ('1', 0)
    assert float(placement('line', placement='auto')['dist']) == 0


def test_box_padding_forms():
    assert _map_style.padding_sides(None) == (0, 0, 0, 0) and _map_style.padding_sides(3) == (3, 3, 3, 3)
    assert _map_style.padding_sides([2, 4]) == (2, 4, 2, 4) and _map_style.padding_sides([1, 2, 3, 4]) == (1, 2, 3, 4)
    with pytest.raises(ValueError):
        _map_style.padding_sides([1, 2, 3])


def test_round_two_box_padding_in_qml(round_two):
    _, out, manifest = round_two
    found = styles(out / 'round-two.gpkg')['map_labels']['qml']
    sizes = [(b.get('shapeSizeX'), b.get('shapeSizeY')) for b in found.iter('background') if b.get('shapeDraw') == '1']
    assert [(float(x) / _map_style.MM_PER_PX, float(y) / _map_style.MM_PER_PX) for x, y in sizes] == [
        (pytest.approx(6), pytest.approx(6)), (pytest.approx(12), pytest.approx(2)), (pytest.approx(8), pytest.approx(5))]
    notes = [n for n in manifest['unsupported'] if n['item'] == 'text.box.padding']
    assert len(notes) == 1 and notes[0]['kind'] == 'approximated' and '[2, 12, 8, 4]' in notes[0]['reason']


def test_round_two_markers_along_lines_in_qml(round_two):
    _, out, _ = round_two
    style = styles(out / 'round-two.gpkg')['routes']
    rules = rules_of(style)
    node = symbol_named(style, rules['with'])
    line, along = node.findall('layer')
    assert (line.get('class'), along.get('class')) == ('SimpleLine', 'MarkerLine')  # the marker on top
    values = layer_options(along)
    assert float(values['interval']) == pytest.approx(_map_style.mm(80)) and float(values['offset_along_line']) == pytest.approx(_map_style.mm(40))
    assert values['interval_unit'] == values['offset_along_line_unit'] == 'MM' and values['rotate'] == '1' and values['placements'] == 'Interval'
    inner = along.find('symbol')
    assert inner.get('type') == 'marker'
    marker = layer_options(inner.find('layer'))
    assert marker['angle'] == '90' and marker['color'] == '192,0,0,255'  # map-style's 0 points along the line, QGIS's stands across it
    assert layer_options(line)['use_custom_dash'] == '1' and marker['outline_style'] == 'solid'  # the dash stays on the line
    free = symbol_named(style, rules['free']).findall('layer')[1]
    assert layer_options(free)['rotate'] == '0' and layer_options(free.find('symbol/layer'))['angle'] == '180'
    outline = symbol_named(styles(out / 'round-two.gpkg')['cells'], '3')
    assert [n.get('class') for n in outline.findall('layer')] == ['SimpleFill', 'SimpleLine', 'MarkerLine']
    assert outline.get('force_rhr') == '1' and symbol_named(styles(out / 'round-two.gpkg')['cells'], '0').get('force_rhr') == '0'


def test_markers_that_do_not_apply_are_reported():
    notes = []
    line = {'id': 'l', 'geometry': 'line', 'renderer': {'type': 'single', 'symbol': {'stroke': {'color': '#000000', 'width': 1}, 'fill': {'color': '#FF0000'},
                                                                                     'marker': {'shape': 'circle', 'size': 4}}}}
    qgis.qml(line, notes, {})
    assert {'symbol.fill', 'symbol.marker'} == {n['item'] for n in notes}
    notes = []
    point = {'id': 'p', 'geometry': 'point', 'renderer': {'type': 'single', 'symbol': {
        'fill': {'color': '#FF0000', 'pattern': {'kind': 'hatch', 'spacing': 4, 'width': 1, 'color': '#000000'}},
        'marker': {'shape': 'circle', 'size': 4, 'placement': 'line', 'spacing': 10}}}}
    text, _ = qgis.qml(point, notes, {})
    assert {'symbol.marker.placement', 'symbol.fill.pattern'} == {n['item'] for n in notes} and 'MarkerLine' not in text and 'LinePatternFill' not in text
    notes = []
    settings = ET.Element('s')
    qgis.text_style(settings, {'size': 10, 'color': '#000000', 'box': {'fill': {'color': '#FFFFFF', 'pattern': {'kind': 'hatch', 'spacing': 4, 'width': 1, 'color': '#000000'}}}},
                    'f', False, {}, {'layer': 'l'}, notes)
    assert notes[0]['item'] == 'text.box.fill.pattern'


def test_round_two_svg_markers_in_qml(round_two):
    fixture, out, manifest = round_two
    style = styles(out / 'round-two.gpkg')['marks']
    rules = rules_of(style)
    tall = layer_options(symbol_named(style, rules['tall']).find('layer'))
    wide = layer_options(symbol_named(style, rules['wide']).find('layer'))
    # QGIS's size is the width and map-style's the longer side
    assert float(tall['size']) == pytest.approx(_map_style.mm(20)) and float(wide['size']) == pytest.approx(_map_style.mm(40))
    assert base64.b64decode(tall['name'].removeprefix('base64:')).decode() == pack_fixture.TALL_SVG  # inside the style, so inside the GeoPackage
    icons = {row['ref']: row for row in manifest['icons']}
    assert icons['icons/tall.svg']['sha256'] == fixture['digest']['tall.svg'] and sorted(icons['icons/tall.svg']['layers']) == ['marks', 'routes', 'sized']
    assert (icons['icons/wide.svg']['width'], icons['icons/wide.svg']['height']) == (40, 20)
    sized = styles(out / 'round-two.gpkg')['sized']['qml'].find(".//layer[@class='SvgMarker']")
    expression = sized.find("data_defined_properties/Option/Option[@name='properties']/Option[@name='size']/Option[@name='expression']").get('value')
    assert expression.endswith(') * 0.5')  # the larger side, not the width, follows the field


def test_round_two_rotation_field_in_qml(round_two):
    _, out, _ = round_two
    style = styles(out / 'round-two.gpkg')['marks']
    rules = rules_of(style)

    def angle(rule):
        node = symbol_named(style, rules[rule]).find('layer/data_defined_properties/Option/Option[@name="properties"]/Option[@name="angle"]/Option[@name="expression"]')
        return node.get('value')
    assert angle('east') == 'coalesce(try(to_real("bearing")), 0) + 0'
    assert angle('none') == 'coalesce(try(to_real("bearing")), 0) + 180'
    routes = styles(out / 'round-two.gpkg')['routes']
    inner = symbol_named(routes, rules_of(routes)['turned']).find('layer/symbol/layer/data_defined_properties/Option/Option[@name="properties"]/Option[@name="angle"]/Option[@name="expression"]')
    assert inner.get('value') == 'coalesce(try(to_real("bearing")), 0) + 135'  # rotation 45, and 90 from along the line


def test_round_two_patterns_in_qml(round_two):
    _, out, _ = round_two
    style = styles(out / 'round-two.gpkg')['cells']
    hatch = symbol_named(style, '0').findall('layer')
    assert [n.get('class') for n in hatch] == ['SimpleFill', 'LinePatternFill']  # the lines over the fill
    values = layer_options(hatch[1])
    assert values['angle'] == '45' and float(values['distance']) == pytest.approx(_map_style.mm(20)) and values['distance_unit'] == 'MM'
    line = layer_options(hatch[1].find('symbol/layer'))
    assert line['line_color'] == '0,170,0,255' and float(line['line_width']) == pytest.approx(_map_style.mm(4))
    assert layer_options(symbol_named(style, '1').find('layer'))['color'].endswith(',0')  # fill opacity 0: lines only
    cross = symbol_named(style, '2').findall('layer')
    assert [n.get('class') for n in cross] == ['SimpleFill', 'LinePatternFill', 'LinePatternFill']
    assert [layer_options(n)['angle'] for n in cross[1:]] == ['0', '90']
    assert layer_options(cross[1].find('symbol/layer'))['line_color'] == '0,0,170,128'


def test_round_two_panels(round_two):
    _, out, manifest = round_two
    labels = gpd.read_file(out / 'round-two.gpkg', layer='map_labels').set_index('text')
    assert labels.loc['own panel', 'panel'] == 'inset'  # its own panel first, though its layer is in main
    assert labels.loc['layer panel', 'panel'] == 'main' and labels.loc['whole map'].isna()['panel']
    panels = {row['id']: row for row in manifest['panels']}
    assert panels['inset']['frame'] == {'x': 7, 'y': 7, 'width': 3, 'height': 3} and not panels['inset']['shown']
    assert any(n['item'] == 'extent.panels.frame' for n in manifest['unsupported'])
    project = ET.fromstring(qgis.read_project(out / 'round-two.gpkg', 'round-two').split('\n', 1)[1])
    assert {g.get('name') for g in project.find('layer-tree-group').findall('layer-tree-group')} >= {'main', 'inset'}


def test_a_panel_only_labels_name_still_gets_its_layer(tmp_path):
    fixture = pack_fixture.build_round_two(tmp_path / 'data')
    document = fixture['document']
    document['extent'].pop('panels')
    for layer in document['layers']:
        layer.pop('panel', None)
    document['layers'] = document['layers'][:1]
    document['labelPlacements'] = [{'panel': 'side', 'text': 'alone', 'position': [10.05, 0.45], 'style': {'size': 10, 'color': '#000000'}}]
    fixture['style'].write_text(json.dumps(document))
    manifest = run_plain(fixture, tmp_path / 'out')
    assert [row['id'] for row in manifest['panels']] == ['side']
    assert [row['features'] for row in manifest['layers'] if row['table'] == 'map_labels'] == [1]


def test_round_two_errors(tmp_path):
    fixture = pack_fixture.build_round_two(tmp_path / 'data')
    good = fixture['style'].read_text()

    def refused(change, name):
        document = json.loads(good)
        change(document)
        fixture['style'].write_text(json.dumps(document))
        return run_plain(fixture, tmp_path / name, success=False)

    def tall(document):
        return document['layers'][3]['renderer']['rules'][0]['symbol']['marker']
    assert 'does not match marker.svg.sha256' in refused(lambda d: tall(d)['svg'].update(sha256='0' * 64), 'a')
    assert 'not found' in refused(lambda d: tall(d)['svg'].update(path='icons/missing.svg'), 'b')
    assert 'must be relative' in refused(lambda d: tall(d)['svg'].update(path='../outside.svg'), 'c')
    assert 'needs svg.path and svg.sha256' in refused(lambda d: tall(d).pop('svg'), 'd')
    assert 'needs a spacing above 0' in refused(lambda d: d['layers'][2]['renderer']['rules'][0]['symbol']['marker'].pop('spacing'), 'e')
    assert 'is not one of extent.panels' in refused(lambda d: d['labelPlacements'][0].update(panel='nowhere'), 'f')
    (fixture['project'] / 'icons' / 'broken.svg').write_text('<svg><rect></svg>', encoding='utf-8')
    (fixture['project'] / 'icons' / 'empty.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding='utf-8')

    def using(name):
        digest = hashlib.sha256((fixture['project'] / 'icons' / name).read_bytes()).hexdigest()
        return lambda d: tall(d)['svg'].update(path=f'icons/{name}', sha256=digest)
    assert 'not a readable SVG' in refused(using('broken.svg'), 'g')
    assert 'neither a viewBox nor a width and height' in refused(using('empty.svg'), 'h')
    assert not any((tmp_path / name).exists() for name in 'abcdefgh')  # nothing published


def check_python_env():
    resources = Path(QGIS_PYTHON).parents[2] / 'Resources'
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONDONTWRITEBYTECODE': '1'}
    if (resources / 'proj').is_dir():  # the macOS bundle does not set its own PROJ and GDAL data paths
        env.update(PROJ_DATA=str(resources / 'proj'), PROJ_LIB=str(resources / 'proj'), GDAL_DATA=str(resources / 'gdal'))
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(key, None)
    return env


@pytest.mark.skipif(QGIS_PYTHON is None, reason='QGIS not available')
def test_qgis_draws_the_round_two_fields(round_two, tmp_path):
    """QGIS reads the styles as written, and the render measures up: line spacing, marker positions, image sizes, directions, padding."""
    _, out, _ = round_two
    result = subprocess.run([QGIS_PYTHON, str(TESTS / 'qgis_open_check.py'), str(out / 'round-two.gpkg'), 'round-two', str(tmp_path / 'map.png')],
                            capture_output=True, text=True, env=check_python_env(), timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report['read'] and all(row['valid'] for row in report['layers'].values())
    layers = {row['name']: row for row in report['layers'].values()}
    assert [c['symbol_layers'] for c in layers['cells']['classes']] == [
        ['SimpleFill', 'LinePatternFill/SimpleLine'], ['SimpleFill', 'LinePatternFill/SimpleLine'],
        ['SimpleFill', 'LinePatternFill/SimpleLine', 'LinePatternFill/SimpleLine'], ['SimpleFill', 'SimpleLine', 'MarkerLine/SvgMarker']]
    routes = {r['label']: r['symbol_layers'] for r in layers['routes']['rules']}
    assert routes['with'] == ['SimpleLine', 'MarkerLine/SimpleMarker'] and routes['turned'] == ['MarkerLine/SvgMarker']
    placed = next(row for row in layers.values() if row.get('label_settings') and len(row['label_settings']) == 9)
    assert [(row['family'], row['style']) for row in placed['label_settings']][3] == ('Arial', 'Bold')
    pack_fixture.check_round_two_render(tmp_path / 'map.png', four_sides=False)


def test_errors_are_clear(tmp_path):
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    output = tmp_path / 'out'
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(output)]
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GIS_COMPOSER_CONFIG_DIR': str(tmp_path / 'no-config')}
    missing = subprocess.run(command, capture_output=True, text=True, env=env)
    assert missing.returncode == 1 and 'Data root @basemap has no path on this device' in missing.stderr and 'roots.json' in missing.stderr
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'roots.json').write_text(json.dumps({'roots': {'basemap': str(fixture['basemap'])}}))
    assert subprocess.run(command, capture_output=True, text=True, env={**env, 'GIS_COMPOSER_CONFIG_DIR': str(config)}).returncode == 0
    undeclared = subprocess.run(command[:-2] + ['--output', str(tmp_path / 'o2'), '--data-root', f'other={tmp_path}'],
                                capture_output=True, text=True, env=env)
    assert 'not declared in gis-project.json' in undeclared.stderr
    document = json.loads(fixture['style'].read_text())
    document['layers'][1]['data']['sha256'] = '0' * 64
    document.pop('extent')
    changed = tmp_path / 'changed.json'
    changed.write_text(json.dumps(document))
    stale = run({**fixture, 'style': changed}, tmp_path / 'o3', '--full', '--project', fixture['project'], success=False)
    assert 'has changed since the map-style was written' in stale
    document['layers'][1]['data'].pop('sha256')
    changed.write_text(json.dumps(document))
    no_extent = run({**fixture, 'style': changed}, tmp_path / 'o4', '--project', fixture['project'], success=False)
    assert 'no extent.bbox' in no_extent and '--full' in no_extent
    no_project = run({**fixture, 'style': changed}, tmp_path / 'o5', '--full', success=False)
    assert 'no gis-project.json was found' in no_project
    assert run({**fixture, 'style': changed}, tmp_path / 'o6', '--full', '--project', fixture['project'])['scope'] == 'full'
    document['layers'][1]['data']['path'] = '../outside.gpkg'
    changed.write_text(json.dumps(document))
    assert 'must be relative, without ".."' in run({**fixture, 'style': changed}, tmp_path / 'o7', '--full', success=False)


def test_unreadable_data_is_listed_not_packed(tmp_path):
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    tiles = fixture['basemap'] / 'folder'  # tiles without data.kind: GDAL cannot read the folder
    (tiles / '12').mkdir(parents=True)
    (tiles / '12' / '0.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    document = json.loads(fixture['style'].read_text())
    document['dataRoot'] = 'file'
    document['layers'].insert(0, {'id': 'tile-folder', 'geometry': 'polygon', 'data': {'path': '@basemap/folder'},
                                  'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#000000'}}}})
    style = fixture['project'] / 'tiles.map-style.json'  # dataRoot "file": paths relative to this file, here the project root
    style.write_text(json.dumps(document))
    manifest = run({**fixture, 'style': style}, tmp_path / 'out')
    assert {row['ref']: row['layers'] for row in manifest['not_packed']} == {'@basemap/tiles': ['tiles'], '@basemap/folder': ['tile-folder']}
    assert not {'tiles', 'tile-folder'} & {l['id'] for l in manifest['layers']}


@pytest.mark.skipif(QGIS_PYTHON is None, reason='QGIS not installed (set GIS_KIT_QGIS_PYTHON to its Python)')
def test_qgis_opens_the_package(packs, tmp_path):
    """QGIS itself reads the embedded project and the default styles, and renders the class colours."""
    gpkg = packs[1]['extent'] / 'demo-map.gpkg'
    resources = Path(QGIS_PYTHON).parents[2] / 'Resources'
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONDONTWRITEBYTECODE': '1'}
    if (resources / 'proj').is_dir():  # the macOS bundle does not set its own PROJ and GDAL data paths
        env.update(PROJ_DATA=str(resources / 'proj'), PROJ_LIB=str(resources / 'proj'), GDAL_DATA=str(resources / 'gdal'))
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(key, None)
    points = ['10.005,0.405', '10.035,0.405', '10.065,0.405', '10.035,0.435', '10.0925,0.4075']
    result = subprocess.run([QGIS_PYTHON, str(TESTS / 'qgis_open_check.py'), str(gpkg), 'demo-map', str(tmp_path / 'map.png'), *points],
                            capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report['read'] and report['crs'] == 'EPSG:32632' and report['title'] == 'Demo map'
    by_id = {lid.rsplit('_', 5)[0]: row for lid, row in report['layers'].items()}
    layers = {row['name']: row for lid, row in report['layers'].items() if 'map_label' not in lid}
    assert all(row['valid'] for row in report['layers'].values()) and len(by_id) == 15
    assert layers['用地分类']['renderer'] == 'categorizedSymbol'
    assert [(c['value'], c['color'], c['render']) for c in layers['用地分类']['classes']] == [
        ('A 类', '#ed7d31', True), ('B 类', '#4f81bd', True), ('C 类', '#9bbb59', False), ('', '#bbbbbb', True)]
    assert layers['容积率']['renderer'] == 'RuleRenderer' and layers['容积率']['opacity'] == pytest.approx(0.7)
    assert [r['color'] for r in layers['容积率']['rules'][0]['children']] == ['#fff5eb', '#fd8d3c', '#a63603']
    assert layers['roads']['labels'] and not layers['parcels-outline']['labels'] and by_id['map_labels_main']['labels']
    assert layers['height']['renderer'] == 'singlebandpseudocolor' and layers['cover']['renderer'] == 'paletted'
    assert layers['sites']['size_expression'].startswith('2 * 0.26458333333333334')
    order = [lid.rsplit('_', 5)[0] for lid in report['drawing_order_top_first']]
    assert order == ['map_labels_inset', 'map_labels_main', 'map_label_leaders_inset', 'map_label_leaders_main', 'inset_parcels', 'site_marks',
                     'names', 'sites', 'roads', 'parcels_outline', 'far', 'parcels', 'cover', 'height', 'ground']
    assert [t['group'] for t in report['tree']] == ['main', 'inset']
    assert not report['checked'][next(l for l in report['checked'] if l.startswith('inset_parcels'))]  # the inset panel is hidden
    assert report['background'] == '#b4d6e2' and report['bookmarks'] == ['main', 'inset']
    assert sorted(lid.rsplit('_', 5)[0] for lid in report['themes']['inset']) == ['inset_parcels', 'map_label_leaders_inset', 'map_labels_inset']
    assert by_id['map_labels_inset']['subset'] == '"panel" = \'inset\'' and by_id['map_labels_inset']['features'] == 1
    first = by_id['map_labels_main']['label_settings'][0]
    assert (first['family'], first['style'], first['installed'], first['background']) == ('Arial', 'Bold', True, True)
    assert (layers['names']['label_settings'][0]['family'], layers['names']['label_settings'][0]['style']) == ('Arial', 'Bold')
    alone = report['tables_alone']
    assert alone['parcels']['renderer'] == 'categorizedSymbol' and alone['parcels']['styles'] == ['far', 'inset.parcels', 'parcels', 'parcels-outline']
    assert alone['map_labels']['renderer'] == 'nullSymbol' and alone['map_labels']['labels']
    colors = [s['color'] for s in report['samples']]
    assert colors[:4] == ['#bbbbbb', '#ed7d31', '#4f81bd', '#4f81bd']  # default class, A, B, B: as the map-style says
    assert colors[4] != '#9bbb59'  # the hidden C class is not drawn
    assert (tmp_path / 'map.png').stat().st_size > 1000


def test_clip_keeps_field_types_and_split_parts(tmp_path):
    """Integer and boolean fields with NULLs keep their types; a polygon cut in two becomes a MultiPolygon table."""
    import pandas as pd
    from shapely.geometry import Polygon
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    # a U shape whose two arms reach into the extent from the south, joined below it
    u = Polygon([(10.01, 0.30), (10.09, 0.30), (10.09, 0.45), (10.07, 0.45), (10.07, 0.35), (10.03, 0.35), (10.03, 0.45), (10.01, 0.45)])
    frame = gpd.GeoDataFrame({'count': pd.array([7, None], dtype='Int64'), 'flag': pd.array([True, None], dtype='boolean')},
                             geometry=[u, Polygon([(10.04, 0.42), (10.06, 0.42), (10.06, 0.44), (10.04, 0.44)])], crs='EPSG:4326')
    pyogrio.write_dataframe(frame, fixture['project'] / 'inputs' / 'shapes.gpkg', layer='shapes')
    document = json.loads(fixture['style'].read_text())
    document['layers'] = [{'id': 'shapes', 'geometry': 'polygon', 'data': {'path': 'inputs/shapes.gpkg'},
                           'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#123456'}}}}]
    document.pop('labelPlacements')
    fixture['style'].write_text(json.dumps(document))
    manifest = run(fixture, tmp_path / 'out', '--clip')
    gpkg = tmp_path / 'out' / 'demo-map.gpkg'
    info = pyogrio.read_info(gpkg, layer='shapes')
    assert info['geometry_type'] == 'MultiPolygon'
    assert dict(zip(info['fields'], info['ogr_types'])) == {'count': 'OFTInteger64', 'flag': 'OFTInteger', 'clipped': 'OFTInteger'}
    assert dict(zip(info['fields'], info['ogr_subtypes']))['flag'] == 'OFSTBoolean'
    shapes = gpd.read_file(gpkg, layer='shapes')
    assert list(shapes['clipped']) == [True, False] and len(shapes.geometry[0].geoms) == 2
    assert manifest['datasets'][0]['clipped_features'] == 1 and not any('pyarrow' in w for w in manifest['warnings'])
