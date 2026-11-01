"""package.py pack --format arcgis-pro: a map-style map as an ArcGIS Pro layer package (made-up data only).

The CIM JSON is checked structurally here; with ArcGIS Pro's Python in GIS_KIT_ARCGIS_PYTHON (Windows), Pro itself
opens the package (tests/arcgis_open_check.py).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
import pyogrio
import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import _arcgis_style as cim  # noqa: E402
import pack_fixture  # noqa: E402
import package  # noqa: E402

ARCGIS_PYTHON = os.environ.get('GIS_KIT_ARCGIS_PYTHON')


def run(fixture, output, *extra, success=True):
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(output), '--format', 'arcgis-pro',
               '--data-root', f'basemap={fixture["basemap"]}', *map(str, extra)]
    result = subprocess.run(command, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GIS_COMPOSER_CONFIG_DIR': str(output.parent / 'no-config')})
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return json.loads((output / 'package.json').read_text()) if success else result.stderr


@pytest.fixture(scope='module')
def pro(tmp_path_factory):
    root = tmp_path_factory.mktemp('pro')
    fixture = pack_fixture.build(root / 'data')
    manifest = run(fixture, root / 'out', '--lyrx')
    document = json.loads((root / 'out' / 'demo-map.lyrx').read_text(encoding='utf-8'))
    return fixture, root / 'out', manifest, document


def definitions(document):
    return {d['name'] if d['type'] != 'CIMFeatureLayer' or d['name'] not in ('labels (placed)', 'label leaders') else d['uRI']: d
            for d in document['layerDefinitions']}


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def test_layer_package_layout(pro):
    fixture, out, manifest, _ = pro
    lpkx = out / 'demo-map.lpkx'
    assert package.archive_kind(lpkx) == '7z'  # what Pro writes; ExtractPackage reads it
    members = {name.rstrip('/') for name, *_ in package.list_members(lpkx, '7z')[0]}
    assert {'p30/demo-map.lyrx', 'esriinfo/item.pkinfo', 'esriinfo/iteminfo.xml', 'commondata/rasters/height.tif',
            'commondata/rasters/cover.tif'} <= members
    assert any(m.startswith('commondata/demo-map.gdb/a0000000') for m in members)
    assert manifest['manifest'] == 'arcgis-pro-package' and manifest['layer_package']['layer_file'] == 'p30/demo-map.lyrx'
    assert manifest['layer_file'] == {'file': 'demo-map.lyrx', 'geodatabase': 'demo-map.gdb', 'rasters': 'rasters'}
    record = json.loads((out / 'record.json').read_text())
    assert {'demo-map.lpkx', 'demo-map.lyrx', 'package.json', 'rasters/height.tif'} <= set(record['files'])
    assert not (out / '_lpkx').exists()
    tables = {row[0] for row in pyogrio.list_layers(out / 'demo-map.gdb')}
    assert tables == set(manifest['geodatabase']['feature_classes']) == {
        'land', 'parcels', 'roads', 'sites', 'map_labels', 'map_label_leaders', 'map_background'}
    assert pyogrio.read_info(out / 'demo-map.gdb', layer='parcels')['crs'] == 'EPSG:32632'  # data keep their own CRS


def test_unpack_reads_the_package_back(pro, tmp_path):
    """The package's own reader (package.py unpack) finds every dataset the layer file names, and nothing unused."""
    out = pro[1]
    result = subprocess.run([sys.executable, str(SCRIPTS / 'package.py'), 'unpack', str(out / 'demo-map.lpkx'), '--output', str(tmp_path / 'u')],
                            capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / 'u' / 'package.json').read_text())
    assert manifest['package']['type'] == 'Layer Package' and manifest['item']['title'] == 'Demo map'
    assert manifest['warnings'] == []
    layer = manifest['layers'][0]
    assert layer['file'] == 'p30/demo-map.lyrx' and layer['data_in_package']
    assert 'commondata/demo-map.gdb:parcels' in layer['data'] and 'commondata/rasters/height.tif' in layer['data']


def test_cim_document_is_well_formed(pro, tmp_path):
    """Every layer has a unique URI, every group names layers that exist, every colour is RGB 0-255 with alpha 0-100,
    and every data connection is relative to the layer file and points at something in the package."""
    _, out, _, document = pro
    assert document['type'] == 'CIMLayerDocument' and document['version'] == cim.CIM_VERSION == '3.0.0'
    uris = [d['uRI'] for d in document['layerDefinitions']]
    assert len(uris) == len(set(uris)) and all(u.startswith('CIMPATH=') for u in uris)
    referenced = list(document['layers']) + [u for d in document['layerDefinitions'] for u in d.get('layers', [])]
    assert sorted(referenced) == sorted(uris)  # every layer sits exactly once in the tree
    for node in walk(document):
        assert 'type' in node or set(node) <= {'xmin', 'ymin', 'xmax', 'ymax', 'curveRings', 'rings', 'paths', 'a'}, node
        if node.get('type') == 'CIMRGBColor':
            *rgb, alpha = node['values']
            assert len(rgb) == 3 and all(0 <= v <= 255 for v in rgb) and 0 <= alpha <= 100
    gdb_tables = {row[0] for row in pyogrio.list_layers(out / 'demo-map.gdb')}
    for node in walk(document):
        if node.get('type') == 'CIMStandardDataConnection':
            workspace = node['workspaceConnectionString']
            if node['workspaceFactory'] == 'FileGDB':
                assert workspace == 'DATABASE=.\\demo-map.gdb' and node['dataset'] in gdb_tables
            else:
                assert workspace == 'DATABASE=.\\rasters' and (out / 'rasters' / node['dataset']).is_file()
    # the layer file inside the package is the same document with the paths seen from p30/
    tool = package.list_members(out / 'demo-map.lpkx', '7z')[1]
    package.extract(out / 'demo-map.lpkx', '7z', tool, tmp_path / 'x')
    packaged = json.loads((tmp_path / 'x' / 'p30' / 'demo-map.lyrx').read_text(encoding='utf-8'))
    assert {n['workspaceConnectionString'] for n in walk(packaged) if n.get('type') == 'CIMStandardDataConnection'} == {
        'DATABASE=..\\commondata\\demo-map.gdb', 'DATABASE=..\\commondata\\rasters'}
    assert package.relink(packaged, {'DATABASE=..\\commondata\\demo-map.gdb': 'DATABASE=.\\demo-map.gdb',
                                     'DATABASE=..\\commondata\\rasters': 'DATABASE=.\\rasters'}) == document


def test_groups_panels_and_order(pro):
    _, _, manifest, document = pro
    found = definitions(document)
    by_uri = {d['uRI']: d for d in document['layerDefinitions']}
    top = by_uri[document['layers'][0]]
    assert top['type'] == 'CIMGroupLayer' and top['name'] == 'Demo map'
    assert [by_uri[u]['name'] for u in top['layers']] == ['main', 'inset', 'background']  # top first; the background at the bottom
    main, inset = by_uri[top['layers'][0]], by_uri[top['layers'][1]]
    assert main['visibility'] and not inset['visibility']  # the inset panel draws the same place again: hidden
    assert [by_uri[u]['name'] for u in main['layers']] == ['labels', 'points', 'lines', 'fills', 'ground']
    fills = by_uri[main['layers'][3]]
    assert [by_uri[u]['name'] for u in fills['layers']] == ['容积率', '用地分类']  # map-style lists bottom first, CIM top first
    assert not found['容积率']['visibility'] and found['容积率']['transparency'] == pytest.approx(30)
    background = found['background']
    assert background['renderer']['symbol']['symbol']['symbolLayers'][0]['color']['values'] == [180, 214, 226, 100]
    assert manifest['background'] == {'color': '#B4D6E2', 'layer': 'map_background'}
    assert manifest['panels'] == [{'id': 'main', 'shown': True}, {'id': 'inset', 'shown': False}]


def test_renderers(pro):
    _, _, manifest, document = pro
    found = definitions(document)
    parcels = found['用地分类']['renderer']
    assert parcels['type'] == 'CIMUniqueValueRenderer' and parcels['fields'] == ['类别']
    classes = parcels['groups'][0]['classes']
    assert [(c['label'], c['values'][0]['fieldValues'], c['visible']) for c in classes] == [
        ('A 类', ['A 类'], True), ('B 类', ['B 类'], True), ('C 类', ['C 类'], False), ('other (no value)', ['<Null>'], True)]
    fill = classes[2]['symbol']['symbol']['symbolLayers'][0]
    assert fill['type'] == 'CIMSolidFill' and fill['color']['values'] == [155, 187, 89, 80]  # opacity 0.8
    assert parcels['useDefaultSymbol'] and parcels['isDefaultSymbolVisible']  # unmatched values; NULL has its own class
    assert parcels['defaultSymbol']['symbol']['symbolLayers'][0]['color']['values'] == [187, 187, 187, 100]
    far = found['容积率']
    assert far['featureTable']['definitionExpression'] == '类别 IS NOT NULL'
    breaks = far['renderer']['breaks']
    assert far['renderer']['type'] == 'CIMClassBreaksRenderer' and far['renderer']['minimumBreak'] == 0
    assert [b['label'] for b in breaks] == ['< 1.5', '1.5–2.5', '≥ 2.5'] and breaks[-1]['upperBound'] == 3.0
    assert np.float32(breaks[0]['upperBound']) < np.float32(1.5) and np.float32(breaks[1]['upperBound']) < np.float32(2.5)
    roads = found['roads']['renderer']
    assert roads['type'] == 'CIMUniqueValueRenderer' and roads['fields'] == [] and not roads['useDefaultSymbol']
    assert [c['label'] for c in roads['groups'][0]['classes']] == ['主干', '次干', '其他方言']  # the QGIS-dialect rule is skipped
    expression = roads['valueExpressionInfo']['expression']
    assert 'if ((!IsEmpty($feature["等级"]) && $feature["等级"] == "主干")) { return "1" }' in expression
    assert 'if ((Count($feature["name"]) > 3)) { return "3" }' in expression and expression.endswith('return null')
    stroke = roads['groups'][0]['classes'][0]['symbol']['symbol']['symbolLayers'][0]
    assert (stroke['width'], stroke['capStyle'], stroke['joinStyle']) == (2.25, 'Round', 'Round')  # 3 px
    outline = found['parcels-outline']['renderer']['symbol']['symbol']['symbolLayers']
    assert [l['type'] for l in outline] == ['CIMSolidStroke'] and outline[0]['capStyle'] == 'Butt'
    assert outline[0]['effects'] == [{'type': 'CIMGeometricEffectDashes', 'dashTemplate': [3.0, 1.5], 'lineDashEnding': 'NoConstraint'}]
    sites = found['sites']
    size = sites['renderer']['visualVariables'][0]
    assert (size['variableType'], size['minSize'], size['maxSize'], size['minValue'], size['maxValue']) == ('Graduated', 4.5, 15.0, 0, 4)
    assert size['valueExpressionInfo']['expression'] == 'Sqrt($feature["规模"])'
    assert sites['featureSortInfos'] == [{'type': 'CIMFeatureSortInfo', 'fieldName': '规模', 'sortDirection': 'Descending'}]
    marker = sites['renderer']['symbol']['symbol']['symbolLayers'][0]
    assert marker['type'] == 'CIMVectorMarker' and marker['size'] == 4.5 and 'curveRings' in marker['markerGraphics'][0]['geometry']
    assert [l['type'] for l in marker['markerGraphics'][0]['symbol']['symbolLayers']] == ['CIMSolidStroke', 'CIMSolidFill']
    marks = found['site-marks']['renderer']['symbol']['symbol']['symbolLayers'][0]
    assert marks['rotation'] == -10 and 'paths' in marks['markerGraphics'][0]['geometry']  # an x of strokes, clockwise 10° -> -10
    assert found['names']['renderer']['symbol']['symbol']['symbolLayers'] == []  # renderer null: labels only
    rows = {row['id']: row for row in manifest['layers']}
    assert rows['far']['arcgis_renderer'] == 'CIMClassBreaksRenderer' and rows['far']['filter'] == '类别 IS NOT NULL'
    assert rows['height']['arcgis_renderer'] == 'CIMRasterStretchColorizer' and rows['cover']['arcgis_renderer'] == 'CIMRasterUniqueValueColorizer'


def test_rasters(pro):
    found = definitions(pro[3])
    height = found['height']
    assert height['type'] == 'CIMRasterLayer' and height['transparency'] == pytest.approx(40)
    stretch = height['colorizer']
    assert stretch['statsType'] == 'GlobalStats' and (stretch['stretchStats']['min'], stretch['stretchStats']['max']) == (0, 138)
    assert stretch['colorRamp']['weights'] == pytest.approx([70 / 138, 68 / 138])
    assert [r['fromColor']['values'][:3] for r in stretch['colorRamp']['colorRamps']] == [[44, 123, 182], [255, 255, 191]]
    cover = found['cover']['colorizer']
    assert [(c['values'], c['color']['values'][:3]) for c in cover['groups'][0]['classes']] == [
        (['1'], [166, 206, 227]), (['2'], [178, 223, 138]), (['3'], [251, 154, 153])]


def test_auto_labels_on_points_stand_off_the_point():
    def maplex(**spec):
        labels = {'field': 'name', 'text': {'size': 20, 'color': '#000000'}, **spec}
        return cim.layer_labels(labels, 'point', {'name': 'name'}, {}, {}, [])['maplexLabelPlacementProperties']
    around = maplex(placement='auto')
    assert (around['pointPlacementMethod'], around['primaryOffset'], around['primaryOffsetUnit']) == ('AroundPoint', 12, 'Point')
    assert maplex()['primaryOffset'] == 12
    assert (maplex(placement='point')['pointPlacementMethod'], maplex(placement='point')['primaryOffset']) == ('CenteredOnPoint', 1)


def test_labels_and_placements(pro):
    _, out, manifest, document = pro
    found = definitions(document)
    roads = found['roads']
    assert roads['labelVisibility'] and roads['labelClasses'][0]['expression'] == 'DefaultValue($feature["name"], "") + "（" + DefaultValue($feature["等级"], "") + "）"'
    assert roads['labelClasses'][0]['maplexLabelPlacementProperties']['linePlacementMethod'] == 'CenteredCurvedOnLine'
    boxed = cim.layer_labels({'field': 'ref', 'placement': 'line', 'text': {'size': 12, 'color': '#FFFFFF', 'box': {'fill': {'color': '#2C7FB8'}}}},
                             'line', {}, {}, {}, [])
    assert boxed['maplexLabelPlacementProperties']['linePlacementMethod'] == 'CenteredStraightOnLine'  # Pro boxes straight labels only
    names = found['names']
    assert not names['labelVisibility']  # enabled: false, kept but off
    text = names['labelClasses'][0]['textSymbol']['symbol']
    assert (text['fontFamilyName'], text['fontStyleName'], text['height']) == ('Arial', 'Bold', pytest.approx(13.4 * 0.75))
    assert names['labelClasses'][0]['maplexLabelPlacementProperties']['pointPlacementMethod'] == 'CenteredOnPoint'
    labels = pyogrio.read_dataframe(out / 'demo-map.gdb', layer='map_labels')
    assert labels.crs.to_epsg() == 32632  # the map's CRS, so Pro opens the map in it
    assert list(labels['text']) == ['Site 1', 'Site 2\nsecond line', 'Inset'] and list(labels['angle']) == [0, -15, 0]
    assert pyogrio.read_info(out / 'demo-map.gdb', layer='map_labels')['ogr_types'][list(labels.columns).index('style_id')] == 'OFTInteger'
    main = next(d for d in document['layerDefinitions'] if d['name'] == 'labels (placed)' and "'main'" in d['featureTable']['definitionExpression'])
    assert main['featureTable']['definitionExpression'] == "panel = 'main'" and main['renderer']['symbol']['symbol']['symbolLayers'] == []
    classes = {c['name']: c for c in main['labelClasses']}
    assert set(classes) == {'style 1 left', 'style 2 bottom-right rotated'}
    plain, turned = classes['style 1 left'], classes['style 2 bottom-right rotated']
    assert plain['whereClause'] == "style_id = 1 AND anchor = 'left' AND angle = 0"
    assert plain['maplexLabelPlacementProperties']['pointPlacementMethod'] == 'EastOfPoint'
    assert plain['maplexLabelPlacementProperties']['primaryOffset'] == 0 and plain['maplexLabelPlacementProperties']['neverRemoveLabel']
    symbol = plain['textSymbol']['symbol']
    assert symbol['symbol']['symbolLayers'][0]['color']['values'] == [51, 51, 51, 90]
    assert symbol['haloSize'] == 1.5 and symbol['haloSymbol']['symbolLayers'][0]['color']['values'] == [255, 255, 255, 78]
    assert symbol['callout']['type'] == 'CIMBackgroundCallout' and symbol['callout']['margin']['left'] == 1.5
    assert turned['maplexLabelPlacementProperties']['rotationProperties'] == {
        'type': 'CIMMaplexRotationProperties', 'enable': True, 'rotationField': 'angle', 'rotationType': 'Arithmetic', 'alignmentType': 'Straight'}
    leaders = next(d for d in document['layerDefinitions'] if d['name'] == 'label leaders' and "'main'" in d['featureTable']['definitionExpression'])
    leader = leaders['renderer']['groups'][0]['classes'][0]
    assert leaders['renderer']['fields'] == ['stroke_id'] and leader['values'][0]['fieldValues'] == ['1']
    assert leader['symbol']['symbol']['symbolLayers'][0]['effects'][0]['dashTemplate'] == [2.25, 1.5]
    reasons = {(n['layer'], n['item']) for n in manifest['unsupported']}
    assert {('labelPlacements', 'text.halo.blur'), ('labelPlacements', 'text.letterSpacing'), ('labelPlacements', 'anchor'),
            ('roads', 'filter'), ('height', 'renderer.stretch'), ('labelPlacements', 'font')} <= reasons
    assert any("dialect 'qgis' is not Arcade" in n['reason'] and 'skipped' in n['reason'] for n in manifest['unsupported'])
    assert manifest['label_placements']['labels'] == 3 and manifest['not_packed'][0]['ref'] == '@basemap/tiles'


def test_sql_and_arcade_filters():
    names = {}
    assert cim.sql({'field': 'a', 'op': '!=', 'value': "it's"}, names) == "(a IS NULL OR a <> 'it''s')"
    assert cim.sql({'field': 'a b', 'op': 'notIn', 'value': [1, 2.5]}, names) == '("a b" IS NULL OR "a b" NOT IN (1, 2.5))'
    assert cim.sql({'not': {'field': 'b', 'op': '==', 'value': True}}, names) == '(NOT (b IS NOT NULL AND b = 1))'  # booleans are 0/1
    assert cim.sql({'any': [{'field': 'c', 'op': 'isNull'}, {'field': 'c', 'op': 'in', 'value': []}]}, names) == '((c IS NULL) OR (1 = 0))'
    assert cim.sql({'field': 'select', 'op': '<', 'value': 3}, {'select': 'select_'}) == '(select_ IS NOT NULL AND select_ < 3)'
    assert cim.sql({'expression': 'x > 1', 'dialect': 'SQL'}, names) == '(x > 1)'
    notes = []
    assert cim.definition_query({'expression': 'x', 'dialect': 'qgis'}, {'layer': 'l'}, notes, names) is None
    assert notes and 'treated as no filter' in notes[0]['reason']
    assert cim.arcade({'field': '名称', 'op': 'in', 'value': ['甲', 'b"c']}, names) == '(!IsEmpty($feature["名称"]) && Includes(["甲", "b\\"c"], $feature["名称"]))'
    assert cim.arcade({'not': {'field': 'n', 'op': '>=', 'value': 2}}, names) == '(!(!IsEmpty($feature["n"]) && $feature["n"] >= 2))'
    with pytest.raises(cim.Unusable):
        cim.arcade({'expression': 'x', 'dialect': 'sql'}, names)


def test_graduated_with_a_default_or_gap_becomes_rules():
    notes = []
    layer = {'id': 'g', 'geometry': 'polygon', 'renderer': {'type': 'graduated', 'field': 'v', 'default': {'fill': {'color': '#444444'}}, 'classes': [
        {'range': [0, 1], 'symbol': {'fill': {'color': '#000000'}}}, {'range': [2, 3], 'symbol': {'fill': {'color': '#FFFFFF'}}}]}}
    render, kind = cim.renderer(layer, {}, notes)
    assert kind == 'CIMUniqueValueRenderer' and notes[0]['item'] == 'renderer'
    assert [c['values'][0]['fieldValues'] for c in render['groups'][0]['classes']] == [['1'], ['2'], ['default']]
    expression = render['valueExpressionInfo']['expression']
    assert '$feature["v"] >= 0) && (!IsEmpty($feature["v"]) && $feature["v"] < 1' in expression and expression.endswith('return "default"')


def test_marker_shapes():
    notes = []

    def marker(spec):
        return cim.point_symbol(spec, {'layer': 'm'}, notes)['symbolLayers'][0]
    arrow = marker({'marker': {'shape': 'arrow', 'size': 8, 'rotation': 90}, 'fill': {'color': '#FF0000'}})
    assert arrow['rotation'] == -90 and arrow['size'] == 6 and 'rings' in arrow['markerGraphics'][0]['geometry']
    assert 'scaleSymbolsProportionally' not in arrow  # outline widths stay in points, not scaled with the marker
    chevron = marker({'marker': {'shape': 'chevron', 'size': 8}, 'fill': {'color': '#FF0000'}})
    graphic = chevron['markerGraphics'][0]
    assert 'paths' in graphic['geometry'] and graphic['symbol']['type'] == 'CIMLineSymbol'  # line-only shapes take the fill colour
    line = graphic['symbol']['symbolLayers'][0]
    assert line['width'] == pytest.approx(8 * 0.34 * 0.75) and (line['capStyle'], line['joinStyle']) == ('Round', 'Round')
    assert arrow['markerGraphics'][0]['geometry']['rings'][0] == cim.RINGS['triangle']  # as wide and as long as its size, as gis-composer draws it
    ring = marker({'marker': {'shape': 'ring', 'size': 8}, 'fill': {'color': '#00FF00'}})
    assert [l['type'] for l in ring['markerGraphics'][0]['symbol']['symbolLayers']] == ['CIMSolidStroke']
    cross = marker({'marker': {'shape': 'cross', 'size': 8}, 'fill': {'color': '#000000'}})
    assert len(cross['markerGraphics'][0]['geometry']['rings'][0]) == 13  # filled cross
    assert marker({'fill': {'color': '#FF0000'}})['size'] == 4.5 and notes[-1]['item'] == 'symbol.marker'


def test_renamed_fields_follow_into_the_layer_file(tmp_path):
    """A field the file geodatabase must rename (an SQL keyword) is renamed in the renderer, query and labels too."""
    import geopandas as gpd
    from shapely.geometry import box
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    gpd.GeoDataFrame({'select': ['a', 'b'], 'big': [2**40, 3]}, geometry=[box(10.01, 0.41, 10.02, 0.42), box(10.03, 0.41, 10.04, 0.42)],
                     crs='EPSG:4326').to_file(fixture['project'] / 'inputs' / 'kw.geojson', driver='GeoJSON')
    document = json.loads(fixture['style'].read_text())
    document['layers'] = [{'id': 'kw', 'geometry': 'polygon', 'data': {'path': 'inputs/kw.geojson'}, 'filter': {'field': 'select', 'op': '!=', 'value': 'z'},
                           'renderer': {'type': 'categorized', 'field': 'select', 'classes': [{'value': 'a', 'symbol': {'fill': {'color': '#123456'}}}]},
                           'labels': {'field': 'select', 'text': {'size': 10, 'color': '#000000'}}}]
    document.pop('labelPlacements')
    document.pop('background')
    fixture['style'].write_text(json.dumps(document))
    manifest = run(fixture, tmp_path / 'out', '--clip')
    assert manifest['datasets'][0]['renamed_fields'] == {'select': 'select_'} and manifest['layer_file'] is None
    assert any('renamed' in w for w in manifest['warnings']) and any('Integer64' in w and 'Pro before 3.2' in w for w in manifest['warnings'])
    members = {name for name, *_ in package.list_members(tmp_path / 'out' / 'demo-map.lpkx', '7z')[0]}
    assert not (tmp_path / 'out' / 'demo-map.gdb').exists() and 'p30/demo-map.lyrx' in members  # without --lyrx only the package
    assert manifest['layers'][0]['filter'] == "select_ IS NULL OR select_ <> 'z'"


@pytest.fixture(scope='module')
def round_two(tmp_path_factory):
    root = tmp_path_factory.mktemp('round-two')
    fixture = pack_fixture.build_round_two(root / 'data')
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(root / 'out'), '--format', 'arcgis-pro', '--lyrx']
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads((root / 'out' / 'round-two.lyrx').read_text(encoding='utf-8'))
    return root / 'out', json.loads((root / 'out' / 'package.json').read_text()), {d['uRI']: d for d in document['layerDefinitions']}, document


def named(found, name, query=None):
    return next(d for d in found.values() if d['name'] == name and (query is None or query in d['featureTable'].get('definitionExpression', '')))


def classes_of(layer):
    return {c['label']: c['symbol'] for g in layer['renderer']['groups'] for c in g['classes']}


def test_round_two_markers_along_lines(round_two):
    _, _, found, _ = round_two
    routes = classes_of(named(found, 'routes'))
    with_line = routes['with']['symbol']['symbolLayers']
    assert [l['type'] for l in with_line] == ['CIMVectorMarker', 'CIMSolidStroke']  # the marker on top
    assert with_line[0]['markerPlacement'] == {'type': 'CIMMarkerPlacementAlongLineSameSize', 'angleToLine': True, 'endings': 'NoConstraint',
                                               'offsetAlongLine': 30, 'placementTemplate': [60]}
    assert with_line[0]['rotation'] == -90 and with_line[0]['size'] == 15  # map-style's 0 points along the line, CIM's stands across it
    assert 'effects' in with_line[1] and 'effects' not in with_line[0]['markerGraphics'][0]['symbol']['symbolLayers'][0]  # the dash stays on the line
    free = routes['free']['symbol']['symbolLayers'][0]
    assert free['markerPlacement']['angleToLine'] is False and free['rotation'] == -180
    outlined = classes_of(named(found, 'cells'))['outlined']['symbol']['symbolLayers']
    assert [l['type'] for l in outlined] == ['CIMPictureMarker', 'CIMSolidStroke', 'CIMSolidFill']
    assert outlined[0]['markerPlacement']['placementTemplate'] == [45]


def test_round_two_svg_markers(round_two):
    _, manifest, found, _ = round_two
    marks = classes_of(named(found, 'marks'))
    tall, wide = marks['tall']['symbol']['symbolLayers'][0], marks['wide']['symbol']['symbolLayers'][0]
    assert tall['type'] == wide['type'] == 'CIMPictureMarker'
    assert tall['size'] == 30 and wide['size'] == 15  # CIM's size is the height and map-style's the longer side
    assert base64.b64decode(tall['url'].removeprefix('data:image/svg+xml;base64,')).decode() == pack_fixture.TALL_SVG
    assert {row['ref'] for row in manifest['icons']} == {'icons/tall.svg', 'icons/wide.svg'}
    notes = []
    image = {'shape': 'svg', 'size': 10, '_svg': {'base64': 'AA==', 'width': 2, 'height': 1}}
    layer = {'id': 's', 'geometry': 'point', 'size': {'field': 'v', 'scale': 'linear', 'domain': [0, 1], 'range': [1, 2]},
             'renderer': {'type': 'single', 'symbol': {'marker': image}}}
    cim.renderer(layer, {}, notes)
    assert notes[0]['item'] == 'size' and notes[0]['kind'] == 'approximated'  # a graduated size is the height, not the longer side
    notes = []
    image['_svg'] = {'base64': 'AA==', 'width': 1, 'height': 2}
    cim.renderer(layer, {}, notes)
    assert notes == []


def test_round_two_rotation_field(round_two):
    _, _, found, _ = round_two
    marks = classes_of(named(found, 'marks'))
    east, none = marks['east'], marks['none']
    assert 'rotation' not in east['symbol']['symbolLayers'][0] and east['symbol']['symbolLayers'][0]['primitiveName']
    override = east['primitiveOverrides'][0]
    assert override['primitiveName'] == east['symbol']['symbolLayers'][0]['primitiveName'] and override['propertyName'] == 'Rotation'
    assert override['valueExpressionInfo']['expression'] == 'var v = Number($feature["bearing"]);\nreturn -(0 + IIf(IsNan(v), 0, v))'
    assert none['primitiveOverrides'][0]['valueExpressionInfo']['expression'].endswith('return -(180 + IIf(IsNan(v), 0, v))')
    assert none['symbol']['symbolLayers'][0]['rotation'] == -180  # what Pro draws where the connection gives nothing
    turned = classes_of(named(found, 'routes'))['turned']
    assert turned['primitiveOverrides'][0]['valueExpressionInfo']['expression'].endswith('return -(135 + IIf(IsNan(v), 0, v))')  # 45, and 90 from along the line
    renamed = cim.symbol({'marker': {'shape': 'circle', 'size': 4, 'rotationField': 'select'}}, 'point', {}, [], {'select': 'select_'})
    assert '$feature["select_"]' in renamed['primitiveOverrides'][0]['valueExpressionInfo']['expression']


def test_round_two_patterns(round_two):
    _, _, found, _ = round_two
    cells = classes_of(named(found, 'cells'))
    hatch = cells['hatch']['symbol']['symbolLayers']
    assert [l['type'] for l in hatch] == ['CIMHatchFill', 'CIMSolidFill']  # the lines over the fill
    assert hatch[0]['rotation'] == 45 and hatch[0]['separation'] == 15 and hatch[0]['lineSymbol']['symbolLayers'][0]['width'] == 3
    assert hatch[0]['lineSymbol']['symbolLayers'][0]['color']['values'] == [0, 170, 0, 100]
    assert cells['vertical']['symbol']['symbolLayers'][1]['color']['values'][3] == 0  # fill opacity 0: lines only
    cross = cells['cross']['symbol']['symbolLayers']
    assert [l['type'] for l in cross] == ['CIMHatchFill', 'CIMHatchFill', 'CIMSolidFill'] and {l['rotation'] for l in cross[:2]} == {0, 90}
    assert cross[0]['lineSymbol']['symbolLayers'][0]['color']['values'][3] == 50
    notes = []
    assert cim.symbol({'stroke': {'color': '#000000', 'width': 1}, 'fill': {'color': '#FF0000', 'pattern': {'kind': 'hatch', 'spacing': 4, 'width': 1, 'color': '#000000'}}},
                      'line', {'layer': 'l'}, notes)['symbol']['symbolLayers'][0]['type'] == 'CIMSolidStroke'
    assert {n['item'] for n in notes} == {'symbol.fill', 'symbol.fill.pattern'}
    notes = []
    text = cim.text_symbol({'size': 10, 'color': '#000000', 'box': {'fill': {'color': '#FFFFFF', 'pattern': {'kind': 'hatch', 'spacing': 4, 'width': 1, 'color': '#000000'}}}}, {}, {}, notes)
    assert notes[0]['item'] == 'text.box.fill.pattern'
    assert [l['type'] for l in text['symbol']['callout']['backgroundSymbol']['symbolLayers']] == ['CIMSolidFill']


def test_round_two_text(round_two):
    _, manifest, found, _ = round_two
    placed = next(d for d in found.values() if d['name'] == 'labels (placed)' and d['featureTable']['definitionExpression'] == 'panel IS NULL')
    classes = [c['textSymbol']['symbol'] for c in placed['labelClasses']]
    assert [(t['fontFamilyName'], t['fontStyleName']) for t in classes] == [
        ('Arial', 'Regular'), ('Arial', 'Regular'), ('Arial', 'Regular'), ('Arial', 'Bold'), ('Arial', 'Regular'), ('Arial', 'Bold'),
        ('Arial', 'Regular'), ('Arial', 'Bold'), ('Arial', 'Regular')]
    margins = [(t['callout']['margin']['top'], t['callout']['margin']['right'], t['callout']['margin']['bottom'], t['callout']['margin']['left']) for t in classes[:3]]
    assert margins == [(4.5, 4.5, 4.5, 4.5), (1.5, 9, 1.5, 9), (1.5, 9, 6, 3)]  # four sides, all kept
    assert not any(n['item'] == 'text.box.padding' for n in manifest['unsupported'])
    assert sum(n['item'] == 'font.weight' for n in manifest['unsupported']) == 2


def test_round_two_panels(round_two):
    out, manifest, _, _ = round_two
    labels = pyogrio.read_dataframe(out / 'round-two.gdb', layer='map_labels').set_index('text')
    assert labels.loc['own panel', 'panel'] == 'inset' and labels.loc['layer panel', 'panel'] == 'main' and labels.loc['whole map'].isna()['panel']
    panels = {row['id']: row for row in manifest['panels']}
    assert panels['main']['frame'] == {'x': 0, 'y': 0, 'width': 10, 'height': 10} and any(n['item'] == 'extent.panels.frame' for n in manifest['unsupported'])
    placed = [row for row in manifest['layers'] if row['table'] == 'map_labels']
    assert {(row['panel'], row['features']) for row in placed} == {('main', 1), ('inset', 1), (None, 9)}


def test_new_content_gets_a_new_package_id(tmp_path):
    """Pro keeps one unpacked copy per package ID, so a package packed again with other content must not reuse the ID."""
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)

    def package_id(name):
        run(fixture, tmp_path / name)
        lpkx = tmp_path / name / 'demo-map.lpkx'
        package.extract(lpkx, '7z', package.list_members(lpkx, '7z')[1], tmp_path / f'{name}-x')
        return re.search('<ID>(.+)</ID>', (tmp_path / f'{name}-x' / 'esriinfo' / 'item.pkinfo').read_text()).group(1)
    first = package_id('a')
    document = json.loads(fixture['style'].read_text())
    document['layers'][1]['renderer']['symbol'] = {'fill': {'color': '#123456'}}
    fixture['style'].write_text(json.dumps(document))
    assert package_id('b') != first


def test_errors(tmp_path):
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    command = [sys.executable, str(SCRIPTS / 'package.py'), 'pack', str(fixture['style']), '--output', str(tmp_path / 'o'), '--lyrx',
               '--data-root', f'basemap={fixture["basemap"]}']
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 1 and '--lyrx belongs to --format arcgis-pro' in result.stderr


@pytest.mark.skipif(ARCGIS_PYTHON is None, reason='ArcGIS Pro not available (set GIS_KIT_ARCGIS_PYTHON to its Python)')
def test_arcgis_pro_opens_the_package(pro, tmp_path):
    """ArcGIS Pro reads both layer files, finds every data source and draws the class colours."""
    out = pro[1]
    points = ['10.005,0.405', '10.035,0.405', '10.065,0.405', '10.035,0.435']
    result = subprocess.run([ARCGIS_PYTHON, str(TESTS / 'arcgis_open_check.py'), str(out), 'demo-map', str(tmp_path / 'map.png'), '32632',
                             ','.join(map(str, pack_fixture.BBOX)), *points], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    for key in ('lyrx', 'lpkx'):
        layers = [row for row in report[key] if not row['group']]
        assert len(layers) == 16 and not any(row['broken'] for row in layers)
        rows = {row['name'].split('\\')[-1]: row for row in layers}
        assert rows['用地分类']['renderer'] == 'CIMUniqueValueRenderer' and rows['容积率']['features'] == 12
        assert rows['height']['colorizer'] == 'CIMRasterStretchColorizer'
    assert report['samples'] == ['#bbbbbb', '#ed7d31', '#4f81bd', '#4f81bd']


@pytest.mark.skipif(ARCGIS_PYTHON is None, reason='ArcGIS Pro not available (set GIS_KIT_ARCGIS_PYTHON to its Python)')
def test_arcgis_pro_draws_the_round_two_fields(round_two, tmp_path):
    """ArcGIS Pro reads the symbols as written, and the render measures up: line spacing, marker positions, image sizes, directions, padding."""
    out = round_two[0]
    result = subprocess.run([ARCGIS_PYTHON, str(TESTS / 'arcgis_open_check.py'), str(out), 'round-two', str(tmp_path / 'map.png'), '4326',
                             ','.join(map(str, pack_fixture.BBOX))], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    for key in ('lyrx', 'lpkx'):
        layers = [row for row in report[key] if not row['group']]
        assert not any(row['broken'] for row in layers)
        rows = {row['name'].split('\\')[-1]: row for row in layers}
        cells = {c['label']: c['symbol_layers'] for c in rows['cells']['classes']}
        assert cells['cross'] == ['CIMHatchFill', 'CIMHatchFill', 'CIMSolidFill'] and cells['outlined'][0] == 'CIMPictureMarker'
        marks = {c['label']: c['symbol_layers'] for c in rows['marks']['classes']}
        assert marks['tall'] == ['CIMPictureMarker'] and marks['east'] == ['CIMVectorMarker', 'Rotation by field']
        routes = {c['label']: c['symbol_layers'] for c in rows['routes']['classes']}
        assert routes['turned'] == ['CIMPictureMarker', 'Rotation by field']
    pack_fixture.check_round_two_render(tmp_path / 'map.png', four_sides=True)
