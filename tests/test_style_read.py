"""package.py read: QGIS .qml and ArcGIS Pro .lyrx, .stylx into map-style (made-up styles only), and map-style -> pack -> read back."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import _arcgis_style as cim_writer  # noqa: E402
import _map_style  # noqa: E402
import _qgis_style as qgis_writer  # noqa: E402
import _style_read  # noqa: E402
from _style_read import parse_filter  # noqa: E402
import pack_fixture  # noqa: E402


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / 'package.py'), *map(str, args)], capture_output=True, encoding='utf-8', errors='replace',
                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})


def read(source, output, **options):
    """The map-style, the report and the folder from reading a style file."""
    args = argparse.Namespace(source=str(source), output=str(output), data=options.get('data'), data_layer=options.get('data_layer'),
                              field=options.get('field'), match=options.get('match', 'name'), font_dir=options.get('font_dir'))
    summary = _style_read.run(args)
    document = _map_style.load(output / summary['map_style'])
    check_schema(document)
    return document, json.loads((output / 'style-read.json').read_text(encoding='utf-8')), output


def check_schema(document):
    """Against the contract when it is at hand: gis-plugin's contracts folder in GIS_PLUGIN_CONTRACTS, and jsonschema installed."""
    contracts = os.environ.get('GIS_PLUGIN_CONTRACTS')
    if not contracts:
        return
    try:
        import jsonschema
    except ImportError:
        return
    schema = json.loads((Path(contracts) / 'map-style.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator(schema).validate(document)


def qml(*children, labels_enabled=1, geometry=None):
    tail = f'<layerGeometryType>{geometry}</layerGeometryType>' if geometry is not None else ''
    return f"<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'><qgis version=\"3.34.0\" labelsEnabled=\"{labels_enabled}\">{''.join(children)}{tail}</qgis>"


def options(**values):
    return '<Option type="Map">' + ''.join(f'<Option name="{k}" type="QString" value="{v}"/>' for k, v in values.items()) + '</Option>'


def layer(cls, enabled=1, inner='', **values):
    return f'<layer class="{cls}" enabled="{enabled}" locked="0" pass="0">{options(**values)}{inner}</layer>'


def symbol(kind, name, *layers, alpha=1):
    return f'<symbol type="{kind}" name="{name}" alpha="{alpha}">' + ''.join(layers) + '</symbol>'


def single(kind, *layers, alpha=1):
    return f'<renderer-v2 type="singleSymbol"><symbols>{symbol(kind, "0", *layers, alpha=alpha)}</symbols></renderer-v2>'


FILL = dict(color='237,125,49,255', style='solid', outline_style='no')


def write(path, text):
    path.write_text(text, encoding='utf-8')
    return path


def sym(document, index=0):
    return document['layers'][index]['renderer']['symbol']


# --- QML, by level -------------------------------------------------------------------------------------------------

def test_qml_categorized_and_graduated_colours_come_through_as_they_are(tmp_path):
    categories = ('<categories><category value="A" type="string" label="A 类" symbol="0" render="true"/>'
                  '<category value="7" type="long" label="seven" symbol="1" render="false"/>'
                  '<category value="" type="string" label="other" symbol="2" render="true"/></categories>')
    symbols = '<symbols>' + ''.join(symbol('fill', str(i), layer('SimpleFill', **{**FILL, 'color': c})) for i, c in enumerate(('255,0,0,255', '0,255,0,128', '0,0,255,255'))) + '</symbols>'
    document, report, _ = read(write(tmp_path / 'landuse.qml', qml(f'<renderer-v2 type="categorizedSymbol" attr="kind">{categories}{symbols}</renderer-v2>', geometry=2)), tmp_path / 'c')
    spec = document['layers'][0]['renderer']
    assert spec['type'] == 'categorized' and spec['field'] == 'kind'
    assert [c['value'] for c in spec['classes']] == ['A', 7] and spec['classes'][0]['label'] == 'A 类'
    assert spec['classes'][0]['symbol']['fill'] == {'color': '#FF0000'} and spec['classes'][1]['symbol']['fill'] == {'color': '#00FF00', 'opacity': 0.502}
    assert spec['classes'][1]['visible'] is False and spec['default']['fill']['color'] == '#0000FF'
    assert report['symbols']['total'] == 3 and report['symbols']['exact'] == 3
    ranges = ('<ranges><range lower="0" upper="10" label="low" symbol="0" render="true"/><range lower="10" upper="20" label="high" symbol="1" render="true"/></ranges>'
              '<classificationMethod id="Quantile"/>')
    symbols = '<symbols>' + ''.join(symbol('line', str(i), layer('SimpleLine', line_color=c, line_width='1', line_width_unit='Pixel')) for i, c in enumerate(('1,2,3,255', '4,5,6,255'))) + '</symbols>'
    document, report, _ = read(write(tmp_path / 'band.qml', qml(f'<renderer-v2 type="graduatedSymbol" attr="v">{ranges}{symbols}</renderer-v2>', geometry=1)), tmp_path / 'g')
    spec = document['layers'][0]['renderer']
    assert spec['type'] == 'graduated' and spec['method'] == 'quantile' and [c['range'] for c in spec['classes']] == [[0, 10], [10, 20]]
    assert spec['classes'][1]['symbol']['stroke'] == {'color': '#040506', 'width': 1}
    assert 'range-boundary' in report['losses']  # 10 falls in the lower class in QGIS and in the upper one in map-style


def test_qml_simple_symbols_in_their_units(tmp_path):
    fill = layer('SimpleFill', color='10,20,30,255', style='solid', outline_style='solid', outline_color='40,50,60,255', outline_width='1', outline_width_unit='MM', joinstyle='round')
    document, report, _ = read(write(tmp_path / 'poly.qml', qml(single('fill', fill), geometry=2)), tmp_path / 'p')
    assert sym(document) == {'fill': {'color': '#0A141E'}, 'stroke': {'color': '#28323C', 'width': 3.7795, 'join': 'round'}}
    assert report['symbols']['exact'] == 1 and document['layers'][0]['data']['path'].startswith(_style_read.UNBOUND)
    assert any(n['property'] == 'data' for n in document['notes'])
    point = layer('SimpleMarker', name='square', color='255,0,0,255', outline_style='solid', outline_color='0,0,0,255', outline_width='2', outline_width_unit='Pixel', size='12', size_unit='Point', angle='30')
    document, _, _ = read(write(tmp_path / 'pt.qml', qml(single('marker', point), geometry=0)), tmp_path / 'q')
    assert sym(document) == {'marker': {'shape': 'square', 'size': 16, 'rotation': 30}, 'fill': {'color': '#FF0000'}, 'stroke': {'color': '#000000', 'width': 2}}
    dashed = layer('SimpleLine', line_color='0,0,0,255', line_width='2', line_width_unit='Pixel', use_custom_dash='1', customdash='4;2', customdash_unit='Pixel', capstyle='round', line_style='solid')
    document, _, _ = read(write(tmp_path / 'ln.qml', qml(single('line', dashed), geometry=1)), tmp_path / 'r')
    assert sym(document)['stroke'] == {'color': '#000000', 'width': 2, 'dash': [4, 2], 'cap': 'round'}


def test_qml_old_prop_rows_and_symbol_opacity(tmp_path):
    text = ('<renderer-v2 type="singleSymbol"><symbols><symbol type="fill" name="0" alpha="0.5"><layer class="SimpleFill" locked="0" pass="0">'
            '<prop k="color" v="100,100,100,255"/><prop k="style" v="solid"/><prop k="outline_style" v="no"/></layer></symbol></symbols></renderer-v2>')
    document, _, _ = read(write(tmp_path / 'old.qml', qml(text, geometry=2)), tmp_path / 'o')
    assert sym(document) == {'fill': {'color': '#646464', 'opacity': 0.5}}


def test_qml_svg_marker_inside_the_style_and_beside_it(tmp_path):
    image = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20"><rect width="10" height="20" fill="param(fill) #000"/></svg>'
    inner = base64.b64encode(image.encode()).decode()
    marker = layer('SvgMarker', name='base64:' + inner, size='5', size_unit='Pixel', color='1,2,3,255', angle='90')
    document, report, folder = read(write(tmp_path / 'icon.qml', qml(single('marker', marker), geometry=0)), tmp_path / 'i')
    mark = sym(document)['marker']
    assert mark['shape'] == 'svg' and mark['size'] == 10 and mark['rotation'] == 90  # QGIS's size is the width of a 1:2 image
    written = (folder / mark['svg']['path']).read_text(encoding='utf-8')
    assert 'fill="#010203"' in written and 'param(' not in written  # the symbol's colour goes in, as QGIS does
    assert report['images'][0]['sha256'] == mark['svg']['sha256']
    (tmp_path / 'beside.svg').write_text(image.replace('param(fill) #000', '#FF0000'), encoding='utf-8')
    marker = layer('SvgMarker', name='beside.svg', size='5', size_unit='Pixel', color='1,2,3,255')
    document, _, folder = read(write(tmp_path / 'file.qml', qml(single('marker', marker), geometry=0)), tmp_path / 'f')
    assert 'fill="#FF0000"' in (folder / sym(document)['marker']['svg']['path']).read_text(encoding='utf-8')
    marker = layer('SvgMarker', name='/etc/passwd', size='5', size_unit='Pixel', color='1,2,3,255')  # nothing outside the style's folder is read
    document, report, _ = read(write(tmp_path / 'far.qml', qml(single('marker', marker), geometry=0)), tmp_path / 'g')
    assert sym(document)['marker']['shape'] == 'circle' and report['losses']['svg-not-found'] == 1 and report['images'] == []


def test_qml_hatch_and_cross_hatch(tmp_path):
    ground = layer('SimpleFill', **{**FILL, 'color': '255,255,255,255'})

    def hatch(angle):
        line = symbol('line', '@0@1', layer('SimpleLine', line_color='0,0,255,255', line_width='1', line_width_unit='Pixel'))
        return layer('LinePatternFill', inner=line, angle=angle, distance='6', distance_unit='Pixel')
    document, report, _ = read(write(tmp_path / 'h.qml', qml(single('fill', ground, hatch('45')), geometry=2)), tmp_path / 'h')
    assert sym(document)['fill'] == {'color': '#FFFFFF', 'pattern': {'kind': 'hatch', 'angle': 45, 'spacing': 6, 'width': 1, 'color': '#0000FF'}} and report['symbols']['exact'] == 1
    document, _, _ = read(write(tmp_path / 'x.qml', qml(single('fill', hatch('45'), hatch('135')), geometry=2)), tmp_path / 'x')
    assert sym(document)['fill']['opacity'] == 0 and sym(document)['fill']['pattern']['kind'] == 'crossHatch' and sym(document)['fill']['pattern']['angle'] == 45


def test_qml_markers_along_a_line(tmp_path):
    stroke = layer('SimpleLine', line_color='192,0,0,255', line_width='2', line_width_unit='Pixel')
    marker = symbol('marker', '@0@1', layer('SimpleMarker', name='circle', color='0,0,255,255', outline_style='no', size='8', size_unit='Pixel', angle='0'))
    along = layer('MarkerLine', inner=marker, interval='40', interval_unit='Pixel', offset_along_line='20', offset_along_line_unit='Pixel', placements='Interval', rotate='1')
    document, report, _ = read(write(tmp_path / 'm.qml', qml(single('line', stroke, along), geometry=1)), tmp_path / 'm')
    result = sym(document)
    assert result['stroke']['color'] == '#C00000' and result['fill'] == {'color': '#0000FF'}
    assert result['marker'] == {'shape': 'circle', 'size': 8, 'rotation': 270, 'placement': 'line', 'spacing': 40, 'alongLine': True} and report['symbols']['exact'] == 1
    # a marker whose outline differs from the line cannot share the symbol's stroke: its own colours go into an image
    outlined = symbol('marker', '@0@1', layer('SimpleMarker', name='square', color='0,0,255,255', outline_style='solid', outline_color='0,255,0,255', outline_width='1', outline_width_unit='Pixel', size='8', size_unit='Pixel'))
    along = layer('MarkerLine', inner=outlined, interval='40', interval_unit='Pixel', offset_along_line='20', offset_along_line_unit='Pixel', placements='Interval', rotate='1')
    document, report, folder = read(write(tmp_path / 'n.qml', qml(single('line', stroke, along), geometry=1)), tmp_path / 'n')
    assert sym(document)['marker']['shape'] == 'svg' and sym(document)['marker']['spacing'] == 40 and 'fill' not in sym(document)
    assert '#00FF00' in (folder / sym(document)['marker']['svg']['path']).read_text(encoding='utf-8') and report['symbols']['svg'] == 1
    other = layer('MarkerLine', inner=marker, placements='LastVertex', interval='40', interval_unit='Pixel')
    document, report, _ = read(write(tmp_path / 'o.qml', qml(single('line', stroke, other), geometry=1)), tmp_path / 'o')
    assert 'marker' not in sym(document) and report['losses']['line-marker-position'] == 1 and report['symbols']['lost'] == 1


def test_qml_complex_symbols_split_or_are_reported(tmp_path):
    casing = layer('SimpleLine', line_color='0,0,0,255', line_width='5', line_width_unit='Pixel')
    core = layer('SimpleLine', line_color='255,255,255,255', line_width='3', line_width_unit='Pixel')
    document, report, _ = read(write(tmp_path / 'road.qml', qml(single('line', casing, core), geometry=1)), tmp_path / 'r')
    assert [(l['id'], l['renderer']['symbol']['stroke']['width']) for l in document['layers']] == [('road', 5), ('road-2', 3)]
    assert document['layers'][1]['legend'] == {'show': False} and report['symbols']['split'] == 1
    one = layer('SimpleMarker', name='circle', color='255,0,0,255', outline_style='no', size='10', size_unit='Pixel')
    two = layer('SimpleMarker', name='cross', color='0,0,0,0', outline_style='solid', outline_color='0,0,0,255', outline_width='1', outline_width_unit='Pixel', size='6', size_unit='Pixel')
    document, report, folder = read(write(tmp_path / 'stack.qml', qml(single('marker', one, two), geometry=0)), tmp_path / 's')
    mark = sym(document)['marker']
    assert mark['shape'] == 'svg' and mark['size'] == 10 and report['symbols']['svg'] == 1
    assert 'fill="#FF0000"' in (folder / mark['svg']['path']).read_text(encoding='utf-8') and len(document['layers']) == 1
    other = layer('SimpleMarker', name='star', color='255,0,0,255', outline_style='no', size='10', size_unit='Pixel')
    document, report, _ = read(write(tmp_path / 'star.qml', qml(single('marker', other), geometry=0)), tmp_path / 't')
    assert sym(document)['marker']['shape'] == 'svg' and report['symbols']['svg'] == 1
    unknown = layer('SimpleMarker', name='some_new_shape', color='255,0,0,255', outline_style='no', size='10', size_unit='Pixel')
    _, report, _ = read(write(tmp_path / 'new.qml', qml(single('marker', unknown), geometry=0)), tmp_path / 'u')
    assert report['losses']['marker-shape'] == 1 and report['symbols']['approximated'] == 1
    generator = layer('GeometryGenerator', SymbolType='Fill', geometryModifier='buffer($geometry, 1)')
    document, report, _ = read(write(tmp_path / 'gen.qml', qml(single('fill', generator, layer('SimpleFill', **FILL)), geometry=2)), tmp_path / 'v')
    assert sym(document)['fill']['color'] == '#ED7D31' and report['losses']['symbol-layer'] == 1 and report['symbols']['lost'] == 1
    assert any('GeometryGenerator' in n['reason'] and n['kind'] == 'untranslated' for n in document['notes'])
    gradient = layer('GradientFill', color='1,2,3,255', color2='9,9,9,255')
    centre = layer('CentroidFill', inner=symbol('marker', '@0@1', one))
    document, report, _ = read(write(tmp_path / 'grad.qml', qml(single('fill', gradient, centre), geometry=2)), tmp_path / 'w')
    assert sym(document)['fill']['color'] == '#010203' and report['losses']['gradient'] == 1 and report['losses']['polygon-center-marker'] == 1
    font = layer('FontMarker', font='No Such Font At All', chr='A', size='10', size_unit='Pixel', color='0,0,0,255')
    document, report, folder = read(write(tmp_path / 'font.qml', qml(single('marker', font), geometry=0)), tmp_path / 'x')
    assert report['fonts']['missing'] == ['No Such Font At All'] and report['losses']['text-needs-font'] == 1
    assert '<text' in (folder / sym(document)['marker']['svg']['path']).read_text(encoding='utf-8')
    disabled = layer('SimpleFill', enabled=0, **FILL)
    document, report, _ = read(write(tmp_path / 'off.qml', qml(single('fill', disabled, layer('SimpleFill', **{**FILL, 'color': '1,1,1,255'})), geometry=2)), tmp_path / 'y')
    assert sym(document) == {'fill': {'color': '#010101'}} and report['symbols']['exact'] == 1  # what does not draw is no loss


def test_qml_rules_labels_and_layer_settings(tmp_path):
    rules = ('<rules key="r"><rule key="a" filter="&quot;k&quot; = \'x\'" symbol="0" label="X"/><rule key="b" filter="&quot;n&quot; &gt; 3" symbol="1" label="big" active="0" scalemaxdenom="5000"/>'
             '<rule key="c" filter="ELSE" symbol="2" label="rest"/></rules>')
    symbols = '<symbols>' + ''.join(symbol('fill', str(i), layer('SimpleFill', **{**FILL, 'color': f'{i},0,0,255'})) for i in range(3)) + '</symbols>'
    labels = ('<labeling type="simple"><settings><text-style fontFamily="Demo" namedStyle="Bold" fontSize="10" fontSizeUnit="Point" textColor="1,2,3,255" textOpacity="1" fieldName="name" isExpression="0">'
              '<text-buffer bufferDraw="1" bufferSize="1" bufferSizeUnits="Pixel" bufferColor="255,255,255,255" bufferOpacity="1"/>'
              '<background shapeDraw="1" shapeType="0" shapeSizeType="0" shapeSizeX="4" shapeSizeY="2" shapeSizeUnit="Pixel" shapeFillColor="255,255,0,255" shapeRadiiX="1"/>'
              '</text-style><placement placement="1" centroidInside="1"/></settings></labeling>')
    document, report, _ = read(write(tmp_path / 'rule.qml', qml(f'<renderer-v2 type="RuleRenderer" enableorderby="1">{rules}{symbols}<orderby><orderByClause asc="0">"n"</orderByClause></orderby></renderer-v2>',
                                                                 labels, '<layerOpacity>0.5</layerOpacity><blendMode>6</blendMode>', geometry=2)), tmp_path / 'r')
    item = document['layers'][0]
    spec = item['renderer']
    assert spec['type'] == 'rule' and spec['rules'][0]['filter'] == {'field': 'k', 'op': '==', 'value': 'x'} and spec['rules'][1]['filter'] == {'field': 'n', 'op': '>', 'value': 3}
    assert spec['rules'][1]['visible'] is False and spec['default']['fill']['color'] == '#020000'
    assert item['opacity'] == 0.5 and item['sortBy'] == {'field': 'n', 'order': 'desc'}
    assert item['labels'] == {'field': 'name', 'text': {'size': 13.3333, 'color': '#010203', 'font': 'Demo', 'weight': 700, 'halo': {'color': '#FFFFFF', 'width': 1},
                                                        'box': {'fill': {'color': '#FFFF00'}, 'padding': [2, 4]}}, 'placement': 'polygon-center'}
    for code in ('rule-overlap', 'scale-range', 'blend-mode', 'label-box'):
        assert report['losses'][code] >= 1
    rules = ('<rules key="r"><rule key="a" filter="&quot;k&quot; = 1" symbol="0" label="one"/><rule key="b" filter="&quot;k&quot; = 2" symbol="1" label="two"/></rules>')
    document, report, _ = read(write(tmp_path / 'same.qml', qml(f'<renderer-v2 type="RuleRenderer">{rules}{symbols}</renderer-v2>', geometry=2)), tmp_path / 's')
    assert document['layers'][0]['renderer']['type'] == 'categorized' and 'rule-overlap' not in report['losses']


def test_qml_label_expressions(tmp_path):
    def label(field, expression):
        return ('<labeling type="simple"><settings><text-style fontSize="8" textColor="0,0,0,255" fieldName="' + field.replace('"', '&quot;') + f'" isExpression="{expression}"/></settings></labeling>')
    empty = single('fill', layer('SimpleFill', **FILL))
    for name, field, expression, expected, code in (('a', 'plain', 0, {'field': 'plain'}, None), ('b', '"quoted"', 1, {'field': 'quoted'}, None),
                                                    ('c', "concat('No. ', \"n\", ' (', \"k\", ')')", 1, {'template': 'No. {n} ({k})'}, None),
                                                    ('d', "'x' || \"n\"", 1, {'template': 'x{n}'}, None), ('e', 'upper("n")', 1, {'field': 'n'}, 'label-expression')):
        document, report, _ = read(write(tmp_path / f'{name}.qml', qml(empty, label(field, expression), geometry=2)), tmp_path / name)
        labels = document['layers'][0]['labels']
        assert {k: v for k, v in labels.items() if k in ('field', 'template')} == expected
        assert (code in report['losses']) if code else 'label-expression' not in report['losses']
    document, report, _ = read(write(tmp_path / 'f.qml', qml(empty, label('1 + 1', 1), geometry=2)), tmp_path / 'f')
    assert 'labels' not in document['layers'][0] and report['losses']['label-expression'] == 1


def test_qml_rasters(tmp_path):
    def raster(body, kind, **attrs):
        extra = ' '.join(f'{k}="{v}"' for k, v in attrs.items())
        return f'<pipe><rasterrenderer type="{kind}" band="1" opacity="0.5" {extra}>{body}</rasterrenderer><brightnesscontrast brightness="10" contrast="0"/></pipe>'
    ramp = ('<rastershader><colorrampshader colorRampType="INTERPOLATED" minimumValue="0" maximumValue="10"><item value="0" color="#000000" alpha="255" label="low"/>'
            '<item value="10" color="#ffffff" alpha="128" label="high"/></colorrampshader></rastershader>')
    document, report, _ = read(write(tmp_path / 'ramp.qml', qml(raster(ramp, 'singlebandpseudocolor', nodataColor='255,0,0,255'))), tmp_path / 'r')
    item = document['layers'][0]
    assert item['geometry'] == 'raster' and item['opacity'] == 0.5 and item['data']['kind'] == 'raster'
    assert item['renderer'] == {'type': 'pseudocolor', 'interpolation': 'linear', 'stops': [{'value': 0, 'color': '#000000', 'label': 'low'}, {'value': 10, 'color': '#FFFFFF', 'label': 'high'}], 'nodata': {'color': '#FF0000'}}
    assert report['losses']['raster-alpha'] == 1 and report['losses']['raster-adjust'] == 1
    discrete = ('<rastershader><colorrampshader colorRampType="DISCRETE" minimumValue="0"><item value="9.999999999999998" color="#111111" alpha="255"/><item value="inf" color="#222222" alpha="255"/></colorrampshader></rastershader>')
    document, _, _ = read(write(tmp_path / 'disc.qml', qml(raster(discrete, 'singlebandpseudocolor', classificationMin='0'))), tmp_path / 'd')
    assert [(s['value'], s['color']) for s in document['layers'][0]['renderer']['stops']] == [(0, '#111111'), (10, '#222222')]
    palette = '<colorPalette><paletteEntry value="1" color="#a6cee3" alpha="255" label="one"/><paletteEntry value="2" color="#b2df8a" alpha="255"/></colorPalette>'
    document, _, _ = read(write(tmp_path / 'pal.qml', qml(raster(palette, 'paletted'))), tmp_path / 'p')
    assert document['layers'][0]['renderer'] == {'type': 'palette', 'classes': [{'value': 1, 'color': '#A6CEE3', 'label': 'one'}, {'value': 2, 'color': '#B2DF8A'}]}
    document, report, _ = read(write(tmp_path / 'rgb.qml', qml(raster('', 'multibandcolor', redBand=3, greenBand=2, blueBand=1))), tmp_path / 'b')
    assert document['layers'][0]['renderer'] == {'type': 'rgb', 'bands': [3, 2, 1]} and report['losses']['raster-stretch'] == 1
    with pytest.raises(ValueError, match='hillshade'):
        read(write(tmp_path / 'hill.qml', qml(raster('', 'hillshade'))), tmp_path / 'h')


def test_qml_refuses_what_is_not_a_style(tmp_path):
    with pytest.raises(ValueError, match='not a QGIS style'):
        read(write(tmp_path / 'a.qml', '<nothing/>'), tmp_path / 'a')
    with pytest.raises(ValueError, match='entities'):
        read(write(tmp_path / 'b.qml', '<!DOCTYPE q [<!ENTITY x "y">]><qgis/>'), tmp_path / 'b')
    with pytest.raises(ValueError, match='no renderer'):
        read(write(tmp_path / 'c.qml', '<qgis/>'), tmp_path / 'c')
    with pytest.raises(ValueError, match='not found'):
        read(tmp_path / 'missing.qml', tmp_path / 'd')
    with pytest.raises(ValueError, match='reads'):
        read(write(tmp_path / 'e.sld', '<x/>'), tmp_path / 'e')


# --- CIM -----------------------------------------------------------------------------------------------------------

def rgb(r, g, b, a=100):
    return {'type': 'CIMRGBColor', 'values': [r, g, b, a]}


def solid(color):
    return {'type': 'CIMSolidFill', 'enable': True, 'color': color}


def cim_stroke(color, width=1, **more):
    return {'type': 'CIMSolidStroke', 'enable': True, 'capStyle': 'Butt', 'joinStyle': 'Miter', 'width': width, 'color': color, **more}


def polygon(*layers):
    return {'type': 'CIMPolygonSymbol', 'symbolLayers': list(layers)}


FRAME = {'xmin': -5, 'ymin': -5, 'xmax': 5, 'ymax': 5}
CIRCLE = {'curveRings': [[[0, 5], {'a': [[0, 5], [0, 0], 0, 1]}]]}


def vector(geometry, symbol_body, size=9, **more):
    return {'type': 'CIMVectorMarker', 'enable': True, 'anchorPointUnits': 'Relative', 'size': size, 'frame': FRAME,
            'markerGraphics': [{'type': 'CIMMarkerGraphic', 'geometry': geometry, 'symbol': symbol_body}], 'scaleSymbolsProportionally': True, 'respectFrame': True, **more}


def stylx(path, items):
    """A made-up style file as Pro keeps one: SQLite, CIM JSON in ITEMS.CONTENT with a NUL after it."""
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE CLASSES (ID INTEGER PRIMARY KEY, NAME TEXT)')
    db.executemany('INSERT INTO CLASSES VALUES (?, ?)', [(1, 'Color'), (3, 'Point Symbol'), (4, 'Line Symbol'), (5, 'Polygon Symbol')])
    db.execute('CREATE TABLE ITEMS (ID INTEGER PRIMARY KEY, CLASS INTEGER, CATEGORY TEXT, NAME TEXT, TAGS TEXT, CONTENT TEXT, KEY TEXT)')
    db.execute('CREATE TABLE BINARIES (ID INTEGER PRIMARY KEY)')
    for index, (cls, name, key, content) in enumerate(items, 1):
        db.execute('INSERT INTO ITEMS VALUES (?, ?, ?, ?, ?, ?, ?)', (index, cls, 'demo', name, 'tag', json.dumps(content) + '\0', key))
    db.commit()
    db.close()
    return path


def test_stylx_reads_a_library_as_categorized_layers(tmp_path):
    dashes = {'type': 'CIMGeometricEffectDashes', 'dashTemplate': [3, 3], 'lineDashEnding': 'NoConstraint'}
    hatch = {'type': 'CIMHatchFill', 'enable': True, 'rotation': 45, 'separation': 4.5, 'lineSymbol': {'type': 'CIMLineSymbol', 'symbolLayers': [cim_stroke(rgb(0, 0, 255), 0.75)]}}
    items = [
        (5, '0101', '0101 paddy', polygon(solid(rgb(255, 0, 0)))),
        (5, '0102', '0102 dry', polygon(cim_stroke(rgb(0, 0, 0), 1.5, effects=[dashes]), solid(rgb(0, 255, 0, 50)))),
        (5, 'hatched', 'hatched key', polygon(cim_stroke(rgb(0, 0, 0)), hatch)),
        (5, 'centre', 'centre key', polygon(vector(CIRCLE, polygon(solid(rgb(0, 0, 0))), markerPlacement={'type': 'CIMMarkerPlacementPolygonCenter'}), solid(rgb(9, 9, 9)))),
        (5, '0101', 'dup', polygon(solid(rgb(1, 1, 1)))),
        (4, 'road', 'road key', {'type': 'CIMLineSymbol', 'symbolLayers': [cim_stroke(rgb(255, 255, 255), 2.25), cim_stroke(rgb(0, 0, 0), 3.75)]}),
        (4, 'path', 'path key', {'type': 'CIMLineSymbol', 'symbolLayers': [cim_stroke(rgb(1, 1, 1))]}),
        (3, 'dot', 'dot key', {'type': 'CIMPointSymbol', 'symbolLayers': [vector(CIRCLE, polygon(cim_stroke(rgb(0, 0, 0), 2), solid(rgb(255, 0, 0))))]}),
        (1, 'just a colour', 'c', {'type': 'CIMRGBColor', 'values': [1, 2, 3, 100]}),
    ]
    document, report, _ = read(stylx(tmp_path / 'lib.stylx', items), tmp_path / 'out', field='code')
    layers = {l['id']: l for l in document['layers']}
    assert sorted(layers) == ['line-symbols', 'line-symbols-2', 'point-symbols', 'polygon-symbols']
    classes = {c['value']: c for c in layers['polygon-symbols']['renderer']['classes']}
    assert layers['polygon-symbols']['renderer']['field'] == 'code' and sorted(classes) == ['0101', '0102', 'centre', 'hatched']
    assert classes['0101']['symbol'] == {'fill': {'color': '#FF0000'}}  # the first of two with this name
    assert classes['0102']['symbol'] == {'fill': {'color': '#00FF00', 'opacity': 0.5}, 'stroke': {'color': '#000000', 'width': 2, 'dash': [4, 4]}}
    assert classes['hatched']['symbol']['fill'] == {'color': '#0000FF', 'opacity': 0, 'pattern': {'kind': 'hatch', 'angle': 45, 'spacing': 6, 'width': 1, 'color': '#0000FF'}}
    assert classes['centre']['symbol'] == {'fill': {'color': '#090909'}}
    road = layers['line-symbols']['renderer']['classes']
    assert road[0]['symbol']['stroke']['width'] == 5 and len(layers['line-symbols-2']['renderer']['classes']) == 1  # top first in CIM, bottom first here
    assert layers['line-symbols-2']['renderer']['classes'][0]['symbol']['stroke']['width'] == 3
    dot = layers['point-symbols']['renderer']['classes'][0]['symbol']
    assert dot == {'marker': {'shape': 'circle', 'size': 12}, 'fill': {'color': '#FF0000'}, 'stroke': {'color': '#000000', 'width': 2.4}}  # 2 pt at 9 pt / 10 frame units
    assert report['symbols']['total'] == 7 and report['symbols']['lost'] == 1 and report['symbols']['split'] == 1
    assert report['losses']['polygon-center-marker'] == 1 and report['losses']['duplicate-name'] == 1 and report['losses']['library-item'] == 1
    assert report['library'] == {'items': 9, 'symbols': 7, 'other_items': {'Color': 1}, 'binaries': 0, 'by_geometry': {'polygon': 4, 'line': 2, 'point': 1}}
    document, _, _ = read(stylx(tmp_path / 'lib2.stylx', items[:2]), tmp_path / 'by-key', match='key')
    assert [c['value'] for c in document['layers'][0]['renderer']['classes']] == ['0101 paddy', '0102 dry']
    assert document['layers'][0]['renderer']['classes'][0]['label'] == '0101'


def test_stylx_multi_layer_marker_becomes_one_image(tmp_path):
    text = {'type': 'CIMTextSymbol', 'fontFamilyName': 'No Such Font At All', 'fontStyleName': 'Regular', 'height': 4, 'horizontalAlignment': 'Center', 'symbol': polygon(solid(rgb(1, 2, 3)))}
    label = {'type': 'CIMVectorMarker', 'enable': True, 'size': 9, 'frame': FRAME, 'anchorPointUnits': 'Relative', 'scaleSymbolsProportionally': True,
             'markerGraphics': [{'type': 'CIMMarkerGraphic', 'geometry': {'x': 0, 'y': 0}, 'symbol': text, 'textString': '医<'}]}
    bezier = {'curveRings': [[[-5, 0], {'b': [[5, 0], [-5, 4], [5, 4]]}, {'c': [[0, -5], [3.5355, -3.5355]]}, [-5, 0]]]}
    ring = vector(bezier, polygon(solid(rgb(255, 0, 0)), cim_stroke(rgb(0, 0, 0))), rotation=90, offsetX=1.5,
                  clippingPath={'type': 'CIMClippingPath', 'clippingType': 'Intersect', 'path': {'rings': [[[-5, -5], [5, -5], [5, 5], [-5, 5], [-5, -5]]]}})
    point = {'type': 'CIMPointSymbol', 'symbolLayers': [label, ring]}
    picture = {'type': 'CIMPictureMarker', 'enable': True, 'size': 7.5, 'scaleX': 1, 'url': 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + b'IHDR' + (20).to_bytes(4, 'big') + (10).to_bytes(4, 'big')).decode()}
    outside = {'type': 'CIMPictureMarker', 'enable': True, 'size': 7.5, 'url': 'https://example.invalid/a.png'}
    items = [(3, 'stack', 'k1', point), (3, 'pic', 'k2', {'type': 'CIMPointSymbol', 'symbolLayers': [picture]}), (3, 'far', 'k3', {'type': 'CIMPointSymbol', 'symbolLayers': [outside]})]
    document, report, folder = read(stylx(tmp_path / 'marks.stylx', items), tmp_path / 'out')
    classes = {c['value']: c['symbol'] for c in document['layers'][0]['renderer']['classes']}
    image = (folder / classes['stack']['marker']['svg']['path']).read_text(encoding='utf-8')
    _map_style.svg_size(image, 'test')
    assert '<path' in image and '#FF0000' in image and 'font-family="No Such Font At All"' in image and '医&lt;' in image and report['fonts']['missing'] == ['No Such Font At All']
    assert report['losses']['text-needs-font'] == 1 and 'marker-clip' not in report['losses']
    assert '<image' in (folder / classes['pic']['marker']['svg']['path']).read_text(encoding='utf-8')
    assert report['losses']['picture-marker'] == 1 and report['losses']['nothing-drawn'] >= 1
    for name in ('path', 'sha256'):
        assert name in classes['pic']['marker']['svg']


def test_graphics_inside_a_marker_draw_in_their_listed_order_and_symbol_layers_the_other_way(tmp_path):
    square = {'rings': [[[-5, -5], [-5, 5], [5, 5], [5, -5], [-5, -5]]]}
    small = {'rings': [[[-2, -2], [-2, 2], [2, 2], [2, -2], [-2, -2]]]}
    below = {'type': 'CIMMarkerGraphic', 'geometry': square, 'symbol': polygon(solid(rgb(0, 0, 0)))}
    above = {'type': 'CIMMarkerGraphic', 'geometry': small, 'symbol': polygon(cim_stroke(rgb(0, 255, 0)), solid(rgb(255, 0, 0)))}
    marker = vector(square, polygon(solid(rgb(0, 0, 0))), markerGraphics=[below, above])
    document, _, folder = read(stylx(tmp_path / 'order.stylx', [(3, 'a', 'k', {'type': 'CIMPointSymbol', 'symbolLayers': [marker]})]), tmp_path / 'out')
    image = (folder / document['layers'][0]['renderer']['classes'][0]['symbol']['marker']['svg']['path']).read_text(encoding='utf-8')
    assert image.index('#000000') < image.index('#FF0000') < image.index('#00FF00')


def test_stylx_short_colours_and_a_symbol_that_cannot_be_read(tmp_path):
    short = {'type': 'CIMPolygonSymbol', 'symbolLayers': [{'type': 'CIMSolidFill', 'enable': True, 'color': [10, 20, 30, 128]}]}  # as Pro's own style files write it
    broken = {'type': 'CIMPolygonSymbol', 'symbolLayers': [{'type': 'CIMSolidFill', 'enable': True, 'color': 'not a colour'}]}
    document, report, _ = read(stylx(tmp_path / 's.stylx', [(5, 'a', 'k', short), (5, 'b', 'j', broken)]), tmp_path / 'out')
    classes = document['layers'][0]['renderer']['classes']
    assert classes[0]['symbol'] == {'fill': {'color': '#0A141E', 'opacity': 0.502}} and classes[1]['symbol']['fill']['opacity'] == 0
    assert report['losses']['unreadable-symbol'] == 1 and report['symbols']['lost'] == 1


def test_stylx_text_becomes_outlines_when_the_font_is_there(tmp_path):
    from matplotlib import font_manager
    family = font_manager.FontProperties(fname=font_manager.findfont('DejaVu Sans')).get_name()
    text = {'type': 'CIMTextSymbol', 'fontFamilyName': family, 'fontStyleName': 'Regular', 'height': 8, 'symbol': polygon(solid(rgb(1, 2, 3)))}
    label = {'type': 'CIMVectorMarker', 'enable': True, 'size': 9, 'frame': FRAME, 'anchorPointUnits': 'Relative', 'markerGraphics': [{'type': 'CIMMarkerGraphic', 'geometry': {'x': 0, 'y': 0}, 'symbol': text, 'textString': 'A'}]}
    document, report, folder = read(stylx(tmp_path / 'glyph.stylx', [(3, 'a', 'k', {'type': 'CIMPointSymbol', 'symbolLayers': [label]})]), tmp_path / 'out')
    image = (folder / document['layers'][0]['renderer']['classes'][0]['symbol']['marker']['svg']['path']).read_text(encoding='utf-8')
    assert '<text' not in image and '<path' in image and report['fonts'] == {'outlined': [family], 'missing': []} and report['symbols']['svg'] == 1
    label['markerGraphics'][0].update(textString='A\r\nB', symbol={**text, 'fontFamilyName': 'No Such Font At All'})
    document, _, folder = read(stylx(tmp_path / 'two.stylx', [(3, 'a', 'k', {'type': 'CIMPointSymbol', 'symbolLayers': [label]})]), tmp_path / 'two')
    image = (folder / document['layers'][0]['renderer']['classes'][0]['symbol']['marker']['svg']['path']).read_text(encoding='utf-8')
    assert image.count('<text') == 2 and '>A</text>' in image and '>B</text>' in image  # two lines, not one with a space


def test_stylx_refuses_other_files(tmp_path):
    with pytest.raises(ValueError, match='not a SQLite'):
        read(write(tmp_path / 'a.stylx', 'not sqlite'), tmp_path / 'a')
    db = sqlite3.connect(tmp_path / 'b.stylx')
    db.execute('CREATE TABLE x (a)')
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='no style tables'):
        read(tmp_path / 'b.stylx', tmp_path / 'b')


def lyrx(path, *definitions, top=None):
    write(path, json.dumps({'type': 'CIMLayerDocument', 'version': '3.4.0', 'layers': top or [d['uRI'] for d in definitions], 'layerDefinitions': list(definitions)}))
    return path


def feature(name, renderer, **more):
    table = {'dataConnection': {'type': 'CIMStandardDataConnection', 'workspaceConnectionString': 'DATABASE=C:\\somewhere\\demo.gdb', 'workspaceFactory': 'FileGDB', 'dataset': name}}
    table.update(more.pop('table', {}))
    return {'type': 'CIMFeatureLayer', 'name': name, 'uRI': f'CIMPATH=map/{name}.json', 'featureTable': table, 'renderer': renderer, **more}


def reference(body):
    return {'type': 'CIMSymbolReference', 'symbol': body}


def test_lyrx_renderers_layer_order_and_data(tmp_path):
    one = reference(polygon(solid(rgb(255, 0, 0))))
    two = reference(polygon(solid(rgb(0, 255, 0))))
    unique = {'type': 'CIMUniqueValueRenderer', 'fields': ['kind'], 'useDefaultSymbol': True, 'isDefaultSymbolVisible': True, 'defaultSymbol': two, 'defaultLabel': 'other',
              'groups': [{'classes': [{'label': 'A', 'symbol': one, 'values': [{'fieldValues': ['a']}, {'fieldValues': ['b']}], 'visible': False},
                                      {'label': 'none', 'symbol': one, 'values': [{'fieldValues': ['<Null>']}]}]}]}
    breaks = {'type': 'CIMClassBreaksRenderer', 'field': 'v', 'minimumBreak': 0, 'classificationMethod': 'Quantile', 'breaks': [
        {'label': 'low', 'upperBound': 5, 'symbol': reference({'type': 'CIMLineSymbol', 'symbolLayers': [cim_stroke(rgb(1, 1, 1))]})},
        {'label': 'high', 'upperBound': 9, 'symbol': reference({'type': 'CIMLineSymbol', 'symbolLayers': [cim_stroke(rgb(2, 2, 2))]})}]}
    two_fields = {'type': 'CIMUniqueValueRenderer', 'fields': ['a', 'b'], 'groups': [{'classes': [{'label': 'x', 'symbol': one, 'values': [{'fieldValues': ['1', '2']}]},
                                                                                                    {'label': 'y', 'symbol': two, 'values': [{'fieldValues': ['1', '<Null>']}]}]}]}
    expression = {'type': 'CIMUniqueValueRenderer', 'fields': [], 'valueExpressionInfo': {'expression': 'Upper($feature.k)'}, 'groups': [{'classes': [{'label': 'x', 'symbol': one, 'values': [{'fieldValues': ['X']}]}]}]}
    definitions = [feature('top', unique, transparency=25, visibility=False, table={'definitionExpression': "kind <> 'z' AND n > 1"}, minScale=1000),
                   feature('middle', breaks, featureSortInfos=[{'fieldName': 'v', 'sortDirection': 'Descending'}]),
                   feature('pair', two_fields), feature('computed', expression),
                   {'type': 'CIMRasterLayer', 'name': 'grid', 'uRI': 'CIMPATH=map/grid.json', 'dataConnection': {'workspaceFactory': 'Raster', 'workspaceConnectionString': 'DATABASE=..\\r', 'dataset': 'g.tif'},
                    'colorizer': {'type': 'CIMRasterStretchColorizer', 'stretchStats': {'min': 0, 'max': 10}, 'stretchType': 'MinimumMaximum', 'bandIndex': 1,
                                  'colorRamp': {'type': 'CIMMultipartColorRamp', 'weights': [1, 3], 'colorRamps': [{'type': 'CIMLinearContinuousColorRamp', 'fromColor': rgb(0, 0, 0), 'toColor': rgb(255, 0, 0)},
                                                                                                                    {'type': 'CIMLinearContinuousColorRamp', 'fromColor': rgb(255, 0, 0), 'toColor': rgb(255, 255, 255)}]}}},
                   {'type': 'CIMGroupLayer', 'name': 'ground', 'uRI': 'CIMPATH=map/ground.json', 'layers': ['CIMPATH=map/low.json'], 'visibility': False},
                   {**feature('low', {'type': 'CIMSimpleRenderer', 'symbol': one}), 'uRI': 'CIMPATH=map/low.json'},
                   {'type': 'CIMTiledServiceLayer', 'name': 'web', 'uRI': 'CIMPATH=map/web.json'}]
    top = ['CIMPATH=map/top.json', 'CIMPATH=map/middle.json', 'CIMPATH=map/pair.json', 'CIMPATH=map/computed.json', 'CIMPATH=map/grid.json', 'CIMPATH=map/ground.json', 'CIMPATH=map/web.json', 'CIMPATH=map/gone.json']
    document, report, _ = read(lyrx(tmp_path / 'map.lyrx', *definitions, top=top), tmp_path / 'out')
    layers = {l['id']: l for l in document['layers']}
    assert [l['id'] for l in document['layers']] == ['low', 'grid', 'computed', 'pair', 'middle', 'top']  # bottom first
    item = layers['top']
    assert item['opacity'] == 0.75 and item['visible'] is False and item['data'] == {'path': 'demo.gdb', 'layer': 'top', 'kind': 'vector'}
    assert item['filter'] == {'all': [{'all': [{'field': 'kind', 'op': 'notNull'}, {'field': 'kind', 'op': '!=', 'value': 'z'}]}, {'field': 'n', 'op': '>', 'value': 1}]}
    rules = item['renderer']
    assert rules['type'] == 'rule' and rules['rules'][0]['filter'] == {'field': 'kind', 'op': '==', 'value': 'a'} and rules['rules'][2]['filter'] == {'field': 'kind', 'op': 'isNull'}
    assert rules['rules'][0]['visible'] is False and rules['default']['fill']['color'] == '#00FF00'
    assert 'somewhere' not in json.dumps(document)  # no local path goes into the document
    grades = layers['middle']['renderer']
    assert grades['method'] == 'quantile' and [c['range'] for c in grades['classes']] == [[0, 5], [5, 9]]
    assert layers['middle']['sortBy'] == {'field': 'v', 'order': 'desc'} and layers['middle']['geometry'] == 'line'
    assert layers['pair']['renderer']['rules'][0]['filter'] == {'all': [{'field': 'a', 'op': '==', 'value': '1'}, {'field': 'b', 'op': '==', 'value': '2'}]}
    assert layers['pair']['renderer']['rules'][1]['filter']['all'][1] == {'field': 'b', 'op': 'isNull'}
    assert layers['computed']['renderer']['rules'][0]['filter'] == {'expression': '(Upper($feature.k)) == "X"', 'dialect': 'arcade'}
    assert layers['low']['group'] == 'ground' and layers['low']['visible'] is False
    grid = layers['grid']
    assert grid['renderer']['band'] == 2 and [s['value'] for s in grid['renderer']['stops']] == [0, 2.5, 10] and grid['data'] == {'path': 'g.tif', 'kind': 'raster'}
    for code in ('scale-range', 'layer-type', 'missing-layer', 'data-path'):
        assert report['losses'][code] >= 1


def test_lyrx_labels_and_visual_variables(tmp_path):
    marker = reference({'type': 'CIMPointSymbol', 'symbolLayers': [vector(CIRCLE, polygon(solid(rgb(255, 0, 0))), rotation=30)]})
    renderer = {'type': 'CIMSimpleRenderer', 'symbol': marker, 'visualVariables': [
        {'type': 'CIMRotationVisualVariable', 'rotationTypeZ': 'Arithmetic', 'visualVariableInfoZ': {'valueExpressionInfo': {'expression': '$feature.bearing'}}},
        {'type': 'CIMTransparencyVisualVariable'}]}
    symbol_text = {'type': 'CIMTextSymbol', 'fontFamilyName': 'Demo', 'fontStyleName': 'Bold Italic', 'height': 9, 'symbol': polygon(solid(rgb(1, 2, 3, 50))), 'haloSize': 1.5, 'haloSymbol': polygon(solid(rgb(255, 255, 255))),
                   'letterSpacing': 10, 'callout': {'type': 'CIMBackgroundCallout', 'backgroundSymbol': polygon(cim_stroke(rgb(0, 0, 0)), solid(rgb(255, 255, 0))), 'margin': {'left': 3, 'right': 3, 'top': 1.5, 'bottom': 1.5}}}
    item = feature('p', renderer, labelVisibility=False, labelClasses=[
        {'name': 'a', 'expression': '"No. " + DefaultValue($feature["n"], "") + $feature.m', 'expressionEngine': 'Arcade', 'textSymbol': reference(symbol_text), 'visibility': True, 'whereClause': 'n > 1',
         'maplexLabelPlacementProperties': {'pointPlacementMethod': 'CenteredOnPoint'}},
        {'name': 'b', 'expression': '$feature.z', 'expressionEngine': 'Arcade', 'textSymbol': reference(symbol_text), 'visibility': True}])
    document, report, _ = read(lyrx(tmp_path / 'p.lyrx', item), tmp_path / 'out')
    result = document['layers'][0]
    assert result['renderer']['symbol']['marker'] == {'shape': 'circle', 'size': 12, 'rotation': 330, 'rotationField': 'bearing'}
    labels = result['labels']
    assert labels['template'] == 'No. {n}{m}' and labels['enabled'] is False and labels['placement'] == 'point'
    assert labels['text'] == {'size': 12, 'color': '#010203', 'opacity': 0.5, 'font': 'Demo', 'weight': 700, 'halo': {'color': '#FFFFFF', 'width': 2}, 'letterSpacing': 0.1,
                              'box': {'stroke': {'color': '#000000', 'width': 1.3333}, 'fill': {'color': '#FFFF00'}, 'padding': [2, 4]}}
    for code in ('visual-variable', 'label-rules', 'font-style', 'letter-spacing'):
        assert report['losses'][code] >= 1
    python = feature('q', {'type': 'CIMSimpleRenderer', 'symbol': marker}, labelClasses=[{'expression': '[NAME]', 'expressionEngine': 'Python', 'textSymbol': reference(symbol_text)}])
    document, _, _ = read(lyrx(tmp_path / 'q.lyrx', python), tmp_path / 'out2')
    assert document['layers'][0]['labels']['field'] == 'NAME'


def test_lyrx_colour_models_rasters_and_refusals(tmp_path):
    def fill_with(color):
        return feature('f', {'type': 'CIMSimpleRenderer', 'symbol': reference(polygon(solid(color)))})
    for index, (color, expected) in enumerate((({'type': 'CIMGrayColor', 'values': [128, 100]}, '#808080'), ({'type': 'CIMHSVColor', 'values': [0, 100, 100, 100]}, '#FF0000'),
                                               ({'type': 'CIMHSLColor', 'values': [240, 100, 50, 100]}, '#0000FF'), ({'type': 'CIMCMYKColor', 'values': [0, 100, 100, 0, 100]}, '#FF0000'),
                                               ({'type': 'CIMLABColor', 'values': [100, 0, 0, 100]}, '#FFFFFF'))):
        document, _, _ = read(lyrx(tmp_path / f'{index}.lyrx', fill_with(color)), tmp_path / f'o{index}')
        assert sym(document)['fill']['color'] == expected
    classify = {'type': 'CIMRasterLayer', 'name': 'c', 'uRI': 'CIMPATH=m/c.json', 'colorizer': {'type': 'CIMRasterClassifyColorizer', 'classBreaks': [{'upperBound': cim_writer.below(10), 'color': rgb(1, 1, 1), 'label': 'a'}, {'upperBound': 20, 'color': rgb(2, 2, 2)}]}}
    unique = {'type': 'CIMRasterLayer', 'name': 'u', 'uRI': 'CIMPATH=m/u.json', 'colorizer': {'type': 'CIMRasterUniqueValueColorizer', 'groups': [{'classes': [{'values': ['1'], 'color': rgb(3, 3, 3), 'label': 'one'}, {'values': ['x'], 'color': rgb(4, 4, 4)}]}]}}
    rgbs = {'type': 'CIMRasterLayer', 'name': 'r', 'uRI': 'CIMPATH=m/r.json', 'colorizer': {'type': 'CIMRasterRGBColorizer', 'redBandIndex': 2, 'greenBandIndex': 1, 'blueBandIndex': 0}}
    other = {'type': 'CIMRasterLayer', 'name': 'o', 'uRI': 'CIMPATH=m/o.json', 'colorizer': {'type': 'CIMRasterDiscreteColorColorizer'}}
    document, report, _ = read(lyrx(tmp_path / 'r.lyrx', classify, unique, rgbs, other), tmp_path / 'r')
    layers = {l['id']: l for l in document['layers']}
    assert sorted(layers) == ['c', 'r', 'u'] and report['losses']['renderer'] == 1
    assert [s['value'] for s in layers['c']['renderer']['stops']] == [cim_writer.below(10), 10]
    assert layers['u']['renderer']['classes'] == [{'value': 1, 'color': '#030303', 'label': 'one'}] and layers['r']['renderer']['bands'] == [3, 2, 1]
    with pytest.raises(ValueError, match='not a layer file'):
        read(write(tmp_path / 'x.lyrx', '{"type": "CIMMapDocument"}'), tmp_path / 'x')
    with pytest.raises(ValueError, match='nothing in it'):
        read(write(tmp_path / 'y.lyrx', '{"type": "CIMLayerDocument", "layers": [], "layerDefinitions": []}'), tmp_path / 'y')
    with pytest.raises(ValueError, match='one dataset'):
        read(lyrx(tmp_path / 'z.lyrx', fill_with(rgb(1, 1, 1)), {**fill_with(rgb(1, 1, 1)), 'uRI': 'CIMPATH=m/2.json'}), tmp_path / 'z', data='a.gpkg')


# --- filters -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('dialect, text, expected', [
    ('qgis', '"a" = 1', {'field': 'a', 'op': '==', 'value': 1}),
    ('qgis', 'a = \'x\'\'y\'', {'field': 'a', 'op': '==', 'value': "x'y"}),
    ('qgis', '"a" <> 1', {'all': [{'field': 'a', 'op': 'notNull'}, {'field': 'a', 'op': '!=', 'value': 1}]}),
    ('qgis', '("a" IS NULL OR "a" <> 1)', {'field': 'a', 'op': '!=', 'value': 1}),
    ('qgis', '("a" IS NOT NULL AND "a" >= 1.5)', {'field': 'a', 'op': '>=', 'value': 1.5}),
    ('qgis', '"a" IN (\'x\', \'y\')', {'field': 'a', 'op': 'in', 'value': ['x', 'y']}),
    ('qgis', '"a" IS NULL OR "a" NOT IN (1, 2)', {'field': 'a', 'op': 'notIn', 'value': [1, 2]}),
    ('qgis', '"a" = TRUE AND ("b" < 2 OR "c" IS NOT NULL)', {'all': [{'field': 'a', 'op': '==', 'value': True}, {'any': [{'field': 'b', 'op': '<', 'value': 2}, {'field': 'c', 'op': 'notNull'}]}]}),
    ('qgis', 'NOT coalesce("a" = 1, FALSE)', {'not': {'field': 'a', 'op': '==', 'value': 1}}),
    ('qgis', 'NOT "a" = 1', {'expression': 'NOT "a" = 1', 'dialect': 'qgis'}),  # unknown for NULL in QGIS, true in map-style
    ('qgis', 'length($geometry) > 1', {'expression': 'length($geometry) > 1', 'dialect': 'qgis'}),
    ('sql', "name LIKE 'a%'", {'expression': "name LIKE 'a%'", 'dialect': 'sql'}),
    ('sql', '(NOT (a IS NOT NULL AND a > 1))', {'not': {'field': 'a', 'op': '>', 'value': 1}}),
    ('sql', '"odd name" = 1', {'field': 'odd name', 'op': '==', 'value': 1}),
    ('arcade', '(!IsEmpty($feature["a"]) && $feature["a"] > 1)', {'field': 'a', 'op': '>', 'value': 1}),
    ('arcade', '(IsEmpty($feature["a"]) || $feature["a"] != "x")', {'field': 'a', 'op': '!=', 'value': 'x'}),
    ('arcade', '(!IsEmpty($feature["a"]) && Includes(["x", "y"], $feature["a"]))', {'field': 'a', 'op': 'in', 'value': ['x', 'y']}),
    ('arcade', '(IsEmpty($feature["a"]) || !Includes([1, 2], $feature["a"]))', {'field': 'a', 'op': 'notIn', 'value': [1, 2]}),
    ('arcade', 'IsEmpty($feature.a)', {'field': 'a', 'op': 'isNull'}),
    ('arcade', 'Count($feature["a"]) > 3', {'expression': 'Count($feature["a"]) > 3', 'dialect': 'arcade'}),
])
def test_filter_text_reads_as_the_structured_filter_only_when_it_plainly_is(dialect, text, expected):
    assert parse_filter(text, dialect) == expected


@pytest.mark.parametrize('spec', [
    {'field': 'a', 'op': '==', 'value': 'x'}, {'field': 'a', 'op': '!=', 'value': 1}, {'field': 'a', 'op': '<', 'value': 2.5}, {'field': 'a', 'op': 'in', 'value': ['x', 'y']},
    {'field': 'a', 'op': 'notIn', 'value': [1, 2]}, {'field': 'a', 'op': 'isNull'}, {'field': 'a', 'op': 'notNull'},
    {'all': [{'field': 'a', 'op': '>=', 'value': 1}, {'any': [{'field': 'b', 'op': '==', 'value': 'x'}, {'not': {'field': 'c', 'op': '==', 'value': 1}}]}]},
])
def test_each_writer_filter_reads_back(spec):
    assert parse_filter(qgis_writer.structured(spec), 'qgis') == spec
    assert parse_filter(cim_writer.sql(spec, {}), 'sql') == spec
    assert parse_filter(cim_writer.arcade(spec, {}), 'arcade') == spec


# --- map-style -> pack -> read back --------------------------------------------------------------------------------

def main_symbol(item):
    """The fields of a symbol that a round trip must keep."""
    fill, stroke, marker = item.get('fill') or {}, item.get('stroke') or {}, item.get('marker') or {}
    pattern = fill.get('pattern') or {}
    return {'fill': (fill.get('color') if fill.get('opacity', 1) else None, round(fill.get('opacity', 1), 2)) if fill else None,
            'pattern': (pattern['kind'], float(pattern.get('angle', 45)) % 180, pattern['spacing'], pattern['width'], pattern['color']) if pattern else None,
            'stroke': (stroke['color'], stroke['width'], round(stroke.get('opacity', 1), 2), stroke.get('dash'), stroke.get('cap', 'butt'), stroke.get('join', 'miter')) if stroke else None,
            'marker': ({'arrow': 'triangle'}.get(marker['shape'], marker['shape']), marker['size'], float(marker.get('rotation', 0)) % 360, marker.get('placement', 'point'), marker.get('spacing'),
                       marker.get('rotationField'), (marker.get('svg') or {}).get('sha256')) if marker else None}


def rows(spec, dialect=None):
    """A `rule` or `categorized` renderer as (filter, label, symbol) rows: rules that are exactly classes read back as classes.
    dialect: the rule filters a platform can read; a writer skips the rest, and says so."""
    if spec['type'] == 'categorized':
        return [({'field': spec['field'], 'op': '==', 'value': c['value']}, c.get('label', str(c['value'])), main_symbol(c['symbol'])) for c in spec['classes']]
    return [(r['filter'], r.get('label'), main_symbol(r['symbol'])) for r in spec['rules'] if r['filter'].get('dialect') in (None, dialect)]


def main_renderer(spec, dialect=None):
    if spec is None:
        return None
    kind = spec['type']
    default = main_symbol(spec['default']) if spec.get('default') else None
    if kind == 'single':
        return main_symbol(spec['symbol'])
    if kind in ('categorized', 'rule'):
        return (rows(spec, dialect), default)
    if kind == 'graduated':
        return (spec['field'], [(c['range'], c.get('label', f'{_map_style.number(c["range"][0])} - {_map_style.number(c["range"][1])}'), main_symbol(c['symbol'])) for c in spec['classes']], default)
    return spec


def main_labels(labels):
    if not labels:
        return None
    text = labels['text']
    return {'field': labels.get('field'), 'template': labels.get('template'), 'size': text['size'], 'color': text['color'], 'halo': text.get('halo'), 'placement': labels.get('placement', 'auto'),
            'enabled': labels.get('enabled', True)}


def assert_same_layer(original, back, dialect=None):
    where = original['id']
    assert back['geometry'] == original['geometry'], where
    assert main_renderer(back['renderer'], dialect) == main_renderer(original['renderer'], dialect), where
    assert back.get('opacity', 1) == pytest.approx(original.get('opacity', 1)), where
    expected = dict(original.get('size') or {})
    assert (back.get('size') or {}).get('clamp', True) == expected.pop('clamp', True), where
    assert {k: v for k, v in (back.get('size') or {}).items() if k != 'clamp'} == expected, where
    assert back.get('sortBy') == original.get('sortBy'), where
    expected, got = main_labels(original.get('labels')), main_labels(back.get('labels'))
    if expected:  # `enabled` follows whether the map placed its labels itself, and is checked by the writers' own tests
        expected.pop('enabled'), got.pop('enabled')
    assert got == expected, where


@pytest.fixture(scope='module')
def round_trip(tmp_path_factory):
    root = tmp_path_factory.mktemp('round')
    if shutil.which('bsdtar') is None and shutil.which('7z') is None:
        pytest.skip('no 7z writer')
    out = {}
    for fixture, name in ((pack_fixture.build(root / 'demo'), 'demo'), (pack_fixture.build_round_two(root / 'two'), 'two')):
        qgis_out, pro_out = root / f'{name}-qgis', root / f'{name}-pro'
        env = ['--data-root', f'basemap={fixture["basemap"]}'] if fixture.get('basemap') else []
        for target, extra in ((qgis_out, []), (pro_out, ['--format', 'arcgis-pro', '--lyrx'])):
            result = run_cli('pack', fixture['style'], '--output', target, *env, *extra)
            assert result.returncode == 0, result.stderr
        out[name] = {'fixture': fixture, 'qgis': qgis_out, 'pro': pro_out, 'root': root}
    return out


def qmls_of(gpkg, folder):
    """The default style of each layer in a packed GeoPackage, as .qml files."""
    db = sqlite3.connect(gpkg)
    rows = db.execute('SELECT styleName, styleQML FROM layer_styles').fetchall()
    db.close()
    folder.mkdir()
    return {name: write(folder / (name.replace('/', '_') + '.qml'), text) for name, text in rows}


def read_all_qml(trip, label):
    gpkg = next(trip['qgis'].glob('*.gpkg'))
    files = qmls_of(gpkg, trip['root'] / f'{label}-qml')
    return {name: read(path, trip['root'] / f'{label}-read-{index}')[0] for index, (name, path) in enumerate(sorted(files.items()))}


@pytest.mark.parametrize('name', ['demo', 'two'])
def test_round_trip_through_qgis(round_trip, name):
    trip = round_trip[name]
    back = read_all_qml(trip, f'qgis-{name}')
    checked = 0
    for original in trip['fixture']['document']['layers']:
        if original['id'] in back and original['geometry'] != 'raster':
            layer_back = back[original['id']]['layers'][0]
            assert_same_layer(original, layer_back, 'qgis')
            assert layer_back.get('filter') == original.get('filter'), original['id']
            checked += 1
    assert checked >= 5


@pytest.mark.parametrize('name', ['demo', 'two'])
def test_round_trip_through_arcgis_pro(round_trip, name):
    trip = round_trip[name]
    document, report, _ = read(next(trip['pro'].glob('*.lyrx')), trip['root'] / f'pro-{name}-read')
    back = {l['id']: l for l in document['layers']}
    checked = 0
    for original in trip['fixture']['document']['layers']:
        if original['id'] not in back or original['data'].get('kind') == 'rasterTiles':
            continue
        if original['geometry'] == 'raster':
            assert_raster_same(original, back[original['id']])
        else:
            assert_same_layer(original, back[original['id']], 'arcade')
            assert back[original['id']].get('filter') == original.get('filter'), original['id']
        checked += 1
    assert checked >= 5 and report['symbols']['total'] > 0


def assert_raster_same(original, back):
    spec, got = original['renderer'], back['renderer']
    assert got['type'] == spec['type'] and back.get('opacity', 1) == pytest.approx(original.get('opacity', 1)), original['id']
    if spec['type'] == 'pseudocolor':
        assert [(s['value'], s['color']) for s in got['stops']] == [(s['value'], s['color']) for s in spec['stops']], original['id']
        assert got.get('interpolation', 'linear') == spec.get('interpolation', 'linear')
    elif spec['type'] == 'palette':
        assert [(c['value'], c['color'], c.get('label')) for c in got['classes']] == [(c['value'], c['color'], c.get('label')) for c in spec['classes']]


def test_read_output_packs_again(tmp_path):
    """What was read can be packed for a platform: with --data every layer draws from one dataset."""
    fixture = pack_fixture.build(tmp_path / 'data', with_raster=False)
    fill = layer('SimpleFill', color='237,125,49,255', style='solid', outline_style='solid', outline_color='0,0,0,255', outline_width='1', outline_width_unit='Pixel')
    source = write(tmp_path / 'style.qml', qml(single('fill', fill), geometry=2))
    data = fixture['project'] / 'inputs' / 'analysis' / 'parcels.gpkg'
    folder = tmp_path / 'read'
    folder.mkdir()
    shutil.copy(data, folder / 'parcels.gpkg')
    work = tmp_path / 'read-out'
    summary = run_cli('read', source, '--output', work, '--data', 'parcels.gpkg', '--data-layer', 'parcels')
    assert summary.returncode == 0, summary.stderr
    result = json.loads(summary.stdout)
    assert result['symbols']['exact'] == 1 and result['layers'] == 1 and sorted(p.name for p in work.iterdir()) == ['record.json', 'style-read.json', 'style.map-style.json']
    shutil.copy(data, work / 'parcels.gpkg')
    packed = run_cli('pack', work / 'style.map-style.json', '--output', tmp_path / 'again', '--full')
    assert packed.returncode == 0, packed.stderr
    assert (tmp_path / 'again' / 'style.gpkg').exists()
    placeholder = run_cli('read', source, '--output', tmp_path / 'no-data')
    assert placeholder.returncode == 0
    refused = run_cli('pack', tmp_path / 'no-data' / 'style.map-style.json', '--output', tmp_path / 'never', '--full')
    assert refused.returncode != 0 and 'unbound' in refused.stderr


def test_command_errors_and_the_output_folder(tmp_path):
    source = write(tmp_path / 's.qml', qml(single('fill', layer('SimpleFill', **FILL)), geometry=2))
    assert 'must be new' in run_cli('read', source, '--output', tmp_path).stderr.lower() or run_cli('read', source, '--output', tmp_path).returncode != 0
    assert run_cli('read', source, '--output', tmp_path / 'a', '--field', 'x').returncode != 0
    assert run_cli('read', source, '--output', tmp_path / 'b', '--data', '/abs/path.gpkg').returncode != 0
    record = run_cli('read', source, '--output', tmp_path / 'c')
    assert record.returncode == 0
    saved = json.loads((tmp_path / 'c' / 'record.json').read_text(encoding='utf-8'))
    assert saved['tool'] == 'package read' and 's.map-style.json' in saved['files'] and saved['status'] == 'complete'


# --- files the platforms saved themselves ---------------------------------------------------------------------------

QGIS_PYTHON = os.environ.get('GIS_KIT_QGIS_PYTHON') or next((p for p in ('/Applications/QGIS.app/Contents/MacOS/bin/python3',) if Path(p).is_file()), None)
ARCGIS_SAVED = os.environ.get('GIS_KIT_ARCGIS_SAVED')  # a folder written by tests/arcgis_save_styles.py on a machine with ArcGIS Pro


@pytest.fixture(scope='module')
def qgis_saved(tmp_path_factory):
    if not QGIS_PYTHON:
        pytest.skip('QGIS not available')
    root = tmp_path_factory.mktemp('qgis-saved')
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    resources = Path(QGIS_PYTHON).parents[2] / 'Resources'
    env.update(QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
    if resources.is_dir():
        env.update(PROJ_DATA=str(resources / 'proj'), PROJ_LIB=str(resources / 'proj'), GDAL_DATA=str(resources / 'gdal'))
    result = subprocess.run([QGIS_PYTHON, str(TESTS / 'qgis_save_styles.py'), str(root)], capture_output=True, text=True, env=env, timeout=300)
    if result.returncode != 0:
        pytest.skip('QGIS could not save the styles: ' + result.stderr[-300:])
    return root


def test_reads_what_qgis_itself_saves(qgis_saved, tmp_path):
    def doc(name):
        return read(qgis_saved / f'{name}.qml', tmp_path / name)

    document, report, _ = doc('single-fill')
    assert sym(document)['fill'] == {'color': '#ED7D31'} and sym(document)['stroke']['color'] == '#000000' and sym(document)['stroke']['width'] == pytest.approx(1.8898, abs=1e-3)
    assert report['symbols']['exact'] == 1
    stroke = sym(doc('single-line')[0])['stroke']
    assert stroke['dash'] == pytest.approx([11.3386, 7.5591], abs=1e-3) and stroke['cap'] == 'round'
    point = sym(doc('single-point')[0])
    assert point['marker']['shape'] == 'square' and point['marker']['rotation'] == 30 and point['fill']['color'] == '#FF0000'
    classes = doc('categorized')[0]['layers'][0]['renderer']
    assert [(c['value'], c['label']) for c in classes['classes']] == [('A', 'class A'), ('B', 'class B')] and classes['classes'][1]['visible'] is False
    assert classes['default']['fill']['color'] == '#0000FF'
    grades = doc('graduated')[0]['layers'][0]['renderer']
    assert grades['type'] == 'graduated' and [c['range'] for c in grades['classes']] == [[0, 10], [10, 20]]
    rules = doc('rules')[0]['layers'][0]['renderer']['rules']
    assert rules[0]['filter'] == {'field': 'kind', 'op': '==', 'value': 'a'} and rules[2]['filter'] == {'expression': 'length($geometry) > 1', 'dialect': 'qgis'}
    pattern = sym(doc('hatch')[0])['fill']['pattern']
    assert pattern['angle'] == 45 and pattern['spacing'] == pytest.approx(11.3386, abs=1e-3) and pattern['color'] == '#0000FF'
    along = sym(doc('marker-line')[0])
    assert along['marker']['placement'] == 'line' and along['marker']['shape'] == 'triangle' and along['fill']['color'] == '#FF0000'
    document, report, folder = doc('svg-marker')
    image = (folder / sym(document)['marker']['svg']['path']).read_text(encoding='utf-8')
    assert '#008000' in image.lower()  # the symbol's colour went into the parametric SVG
    assert sym(document)['marker']['size'] == pytest.approx(5 * 2 * 96 / 25.4, abs=1e-2)  # 5 mm wide, an image twice as high
    assert [l['id'] for l in doc('two-lines')[0]['layers']] == ['two-lines', 'two-lines-2']
    assert sym(doc('stacked-markers')[0])['marker']['shape'] == 'svg'
    item = doc('labels')[0]['layers'][0]
    assert item['opacity'] == 0.5 and item['labels']['field'] == 'name' and item['labels']['text']['font'] == 'Arial' and item['labels']['text']['weight'] == 700
    assert item['labels']['text']['halo']['color'] == '#FFFFFF'
    assert doc('raster-ramp')[0]['layers'][0]['renderer']['stops'] == [{'value': 0, 'color': '#000000', 'label': 'low'}, {'value': 3, 'color': '#FFFFFF', 'label': 'high'}]
    assert [c['value'] for c in doc('raster-palette')[0]['layers'][0]['renderer']['classes']] == [1, 2]


@pytest.mark.skipif(not ARCGIS_SAVED, reason='GIS_KIT_ARCGIS_SAVED: a folder from tests/arcgis_save_styles.py')
def test_reads_what_arcgis_pro_itself_saves(tmp_path):
    saved, seen = Path(ARCGIS_SAVED), {}

    def doc(name):
        return seen.setdefault(name, read(saved / f'{name}.lyrx', tmp_path / name)[0]['layers']) if name not in seen else seen[name]

    assert doc('simple-fill')[0]['renderer']['type'] == 'single' and doc('simple-line')[0]['geometry'] == 'line'
    assert doc('graduated')[0]['renderer']['type'] == 'graduated' and doc('graduated')[0]['renderer']['field'] == 'v'
    rules = doc('unique-values')[0]['renderer']['rules']
    assert {r['label'] for r in rules} >= {'a', 'b'} and next(r for r in rules if r['label'] == 'a')['symbol']['fill']['color'] == '#FF0000'
    assert doc('unique-values')[0]['opacity'] == 0.75
    assert doc('labelled-point')[0]['labels']['field'] == 'kind' and doc('labelled-point')[0]['labels'].get('enabled', True) is True
    assert doc('simple-fill')[0]['labels']['enabled'] is False  # Pro keeps a default label class on every layer, switched off
    for name in ('gallery-hatch', 'gallery-road', 'gallery-point'):
        assert doc(name)


@pytest.mark.skipif(not ARCGIS_SAVED, reason='GIS_KIT_ARCGIS_SAVED: a folder from tests/arcgis_save_styles.py')
@pytest.mark.parametrize('name, saved_name', [('demo', 'resaved-demo-map'), ('two', 'resaved-round-two')])
def test_round_trip_through_arcgis_pro_saving_it_again(round_trip, name, saved_name, tmp_path):
    """map-style -> pack -> opened and saved again by Pro -> read: what Pro itself keeps of it."""
    document, _, _ = read(Path(ARCGIS_SAVED) / f'{saved_name}.lyrx', tmp_path / 'read')
    back = {l['id']: l for l in document['layers']}
    checked = 0
    for original in round_trip[name]['fixture']['document']['layers']:
        if original['id'] in back and original['data'].get('kind') != 'rasterTiles':
            if original['geometry'] == 'raster':
                assert_raster_same(original, back[original['id']])
            else:
                assert_same_layer(original, back[original['id']], 'arcade')
            checked += 1
    assert checked >= 5
