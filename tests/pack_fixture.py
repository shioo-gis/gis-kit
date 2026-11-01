"""A made-up map for package.py pack: a project with a gis-project.json, an external data root and a map-style.

Everything is invented: open ocean near (10.05 E, 0.45 N), UTM zone 32N for the analysis data. The map has a main
panel and an inset panel (as a hero map does), a background colour, a tile layer and producer notes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point, box

BBOX = [10.00, 0.40, 10.10, 0.50]
INSET = [10.03, 0.43, 10.06, 0.46]


def utm_box(lon0, lat0, lon1, lat1):
    """A lon/lat box as a polygon in EPSG:32632."""
    return gpd.GeoSeries([box(lon0, lat0, lon1, lat1)], crs='EPSG:4326').to_crs('EPSG:32632').iloc[0]


def build(root: Path, *, with_raster=True):
    project, basemap = root / 'project', root / 'shared-basemap'
    (project / 'inputs' / 'analysis').mkdir(parents=True)
    (project / 'maps').mkdir()
    basemap.mkdir()
    (project / 'gis-project.json').write_text(json.dumps({'schemaVersion': 1, 'id': 'demo', 'name': 'Demo',
                                                          'external': {'basemap': {}}}), encoding='utf-8')
    # ground: one big land polygon and a small island outside the extent (lon/lat, external root)
    gpd.GeoDataFrame({'kind': ['land', 'island']}, geometry=[box(9.9, 0.3, 10.2, 0.6), box(11.0, 1.0, 11.1, 1.1)],
                     crs='EPSG:4326').to_file(basemap / 'land.geojson', driver='GeoJSON')
    # parcels: a 4 x 4 grid of cells 0.03 degrees wide starting at 9.99 / 0.39, so the outer ring crosses the extent edge
    cells, rows = [], []
    for i in range(4):
        for j in range(4):
            lon, lat = 9.99 + 0.03 * i, 0.39 + 0.03 * j
            cells.append(utm_box(lon, lat, lon + 0.03, lat + 0.03))
            rows.append({'类别': [None, 'A 类', 'B 类', 'C 类'][(i + j) % 4], '容积率': [0.5, 1.5, 2.5, 3.0][(i * 4 + j) % 4],
                         '编号': f'P{i}{j}', '面积': round(cells[-1].area, 1)})
    gpd.GeoDataFrame(rows, geometry=cells, crs='EPSG:32632').to_file(project / 'inputs' / 'analysis' / 'parcels.gpkg', layer='parcels')
    # roads: three lines, one far outside the extent
    roads = gpd.GeoDataFrame({'等级': ['主干', '次干', '支路'], 'name': ['Road One', 'Road Two', 'Road Far']},
                             geometry=[LineString([(9.95, 0.45), (10.15, 0.45)]), LineString([(10.05, 0.35), (10.05, 0.55)]),
                                       LineString([(12.0, 2.0), (12.1, 2.1)])], crs='EPSG:4326').to_crs('EPSG:32632')
    roads.to_file(project / 'inputs' / 'analysis' / 'roads.geojson', driver='GeoJSON')
    # points with a size field
    gpd.GeoDataFrame({'规模': [1, 4, 16], 'name': ['Site 1', 'Site 2', 'Site 3']},
                     geometry=[Point(10.02, 0.42), Point(10.05, 0.45), Point(10.3, 0.7)], crs='EPSG:4326').to_crs('EPSG:32632').to_file(
        project / 'inputs' / 'analysis' / 'sites.geojson', driver='GeoJSON')
    layers = [
        {'id': 'ground', 'group': 'ground', 'geometry': 'polygon', 'data': {'path': '@basemap/land.geojson'},
         'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#E1E1E1', 'opacity': 1}}}, 'legend': {'show': False}},
        {'id': 'parcels', 'title': '用地分类', 'group': 'fills', 'geometry': 'polygon', 'data': {'path': 'inputs/analysis/parcels.gpkg', 'layer': 'parcels'},
         'renderer': {'type': 'categorized', 'field': '类别', 'classes': [
             {'value': 'A 类', 'label': 'A 类', 'symbol': {'fill': {'color': '#ED7D31', 'opacity': 1}}},
             {'value': 'B 类', 'label': 'B 类', 'symbol': {'fill': {'color': '#4F81BD', 'opacity': 1}}},
             {'value': 'C 类', 'label': 'C 类', 'symbol': {'fill': {'color': '#9BBB59', 'opacity': 0.8}}, 'visible': False}],
             'default': {'fill': {'color': '#BBBBBB', 'opacity': 1}}},
         'legend': {'show': True, 'title': '用地分类'}},
        {'id': 'far', 'title': '容积率', 'group': 'fills', 'geometry': 'polygon', 'visible': False, 'opacity': 0.7,
         'data': {'path': 'inputs/analysis/parcels.gpkg', 'layer': 'parcels'},
         'filter': {'field': '类别', 'op': 'notNull'},
         'renderer': {'type': 'graduated', 'field': '容积率', 'method': 'manual', 'classes': [
             {'range': [0, 1.5], 'label': '< 1.5', 'symbol': {'fill': {'color': '#FFF5EB'}}},
             {'range': [1.5, 2.5], 'label': '1.5–2.5', 'symbol': {'fill': {'color': '#FD8D3C'}}},
             {'range': [2.5, 3.0], 'label': '≥ 2.5', 'symbol': {'fill': {'color': '#A63603'}}}]}},
        {'id': 'parcels-outline', 'group': 'lines', 'geometry': 'polygon', 'data': {'path': 'inputs/analysis/parcels.gpkg', 'layer': 'parcels'},
         'renderer': {'type': 'single', 'symbol': {'stroke': {'color': '#FFFFFF', 'width': 0.8, 'opacity': 0.9, 'dash': [4, 2]}}},
         'labels': {'field': '编号', 'text': {'font': 'Demo Sans', 'size': 10, 'color': '#333333', 'halo': {'color': '#FFFFFF', 'width': 1.5}},
                    'placement': 'polygon-center', 'enabled': False}},
        {'id': 'roads', 'group': 'lines', 'geometry': 'line', 'data': {'path': 'inputs/analysis/roads.geojson'},
         'renderer': {'type': 'rule', 'rules': [
             {'filter': {'field': '等级', 'op': '==', 'value': '主干'}, 'label': '主干', 'symbol': {'stroke': {'color': '#C00000', 'width': 3, 'cap': 'round', 'join': 'round'}}},
             {'filter': {'field': '等级', 'op': 'in', 'value': ['主干', '次干']}, 'label': '次干', 'symbol': {'stroke': {'color': '#FF9900', 'width': 2}}},
             {'filter': {'expression': 'length($geometry) > 100', 'dialect': 'qgis'}, 'label': '长路', 'symbol': {'stroke': {'color': '#888888', 'width': 1}}},
             {'filter': {'expression': 'Count($feature["name"]) > 3', 'dialect': 'arcade'}, 'label': '其他方言', 'symbol': {'stroke': {'color': '#000000', 'width': 1}}}],
             'default': None},
         'labels': {'template': '{name}（{等级}）', 'text': {'size': 9, 'color': '#222222'}, 'placement': 'line'}},
        {'id': 'sites', 'group': 'points', 'geometry': 'point', 'data': {'path': 'inputs/analysis/sites.geojson'},
         'renderer': {'type': 'single', 'symbol': {'marker': {'shape': 'circle', 'size': 6}, 'fill': {'color': '#7030A0', 'opacity': 0.85},
                                                   'stroke': {'color': '#FFFFFF', 'width': 1}}},
         'size': {'field': '规模', 'scale': 'sqrt', 'domain': [0, 16], 'range': [3, 10], 'clamp': True},
         'sortBy': {'field': '规模', 'order': 'desc'}},
        {'id': 'names', 'group': 'labels', 'geometry': 'point', 'data': {'path': 'inputs/analysis/sites.geojson'}, 'renderer': None,
         'labels': {'field': 'name', 'text': {'font': 'Demo Sans', 'size': 13.4, 'color': '#333333', 'halo': {'color': '#FFFFFF', 'width': 2}},
                    'placement': 'point', 'enabled': False}},
    ]
    if with_raster:
        import rasterio
        from rasterio.transform import from_origin
        height = np.add.outer(np.arange(60), np.arange(80)).astype('float32')
        height[0, 0] = -9999
        with rasterio.open(project / 'inputs' / 'analysis' / 'height.tif', 'w', driver='GTiff', width=80, height=60, count=1, dtype='float32',
                           crs='EPSG:4326', transform=from_origin(9.96, 0.54, 0.0025, 0.0025), nodata=-9999) as target:
            target.write(height, 1)
        classes = (np.indices((30, 40)).sum(axis=0) % 3 + 1).astype('uint8')
        classes[:5, :5] = 255
        with rasterio.open(project / 'inputs' / 'analysis' / 'cover.tif', 'w', driver='GTiff', width=40, height=30, count=1, dtype='uint8',
                           crs='EPSG:4326', transform=from_origin(10.0, 0.5, 0.005, 0.005), nodata=255) as target:
            target.write(classes, 1)
        layers.insert(1, {'id': 'cover', 'group': 'ground', 'geometry': 'raster', 'data': {'path': 'inputs/analysis/cover.tif'}, 'visible': False,
                          'renderer': {'type': 'palette', 'band': 1, 'classes': [{'value': 1, 'color': '#A6CEE3', 'label': 'one'},
                                                                                 {'value': 2, 'color': '#B2DF8A', 'label': 'two'},
                                                                                 {'value': 3, 'color': '#FB9A99', 'label': 'three'}]}})
        layers.insert(1, {'id': 'height', 'group': 'ground', 'geometry': 'raster', 'data': {'path': 'inputs/analysis/height.tif'}, 'opacity': 0.6,
                          'renderer': {'type': 'pseudocolor', 'band': 1, 'interpolation': 'linear', 'stretch': {'method': 'minmax'},
                                       'stops': [{'value': 0, 'color': '#2C7BB6'}, {'value': 70, 'color': '#FFFFBF'}, {'value': 138, 'color': '#D7191C'}]}})
    for layer in layers:
        layer['panel'] = 'main'
    layers.insert(0, {'id': 'tiles', 'panel': 'main', 'group': 'ground', 'geometry': 'raster',
                      'data': {'path': '@basemap/tiles', 'kind': 'rasterTiles'}, 'renderer': {'type': 'rgb', 'bands': [1, 2, 3]}})
    layers.append({'id': 'site-marks', 'panel': 'main', 'group': 'points', 'geometry': 'point', 'data': {'path': 'inputs/analysis/sites.geojson'},
                   'renderer': {'type': 'single', 'symbol': {'marker': {'shape': 'x', 'size': 8, 'rotation': 10}, 'stroke': {'color': '#000000', 'width': 1}}}})
    layers.append({'id': 'inset.parcels', 'panel': 'inset', 'group': 'fills', 'geometry': 'polygon',
                   'data': {'path': 'inputs/analysis/parcels.gpkg', 'layer': 'parcels'},
                   'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#00AA00'}}}})
    document = {'contract': 'map-style', 'schema_version': 1, 'id': 'demo-map', 'title': 'Demo map',
                'producer': {'engine': 'test', 'version': '0'}, 'units': {'size': 'px'}, 'dataRoot': 'project',
                'background': {'color': '#B4D6E2'},
                'extent': {'bbox': BBOX, 'panels': [{'id': 'main', 'bbox': BBOX}, {'id': 'inset', 'bbox': INSET}],
                           'print': {'widthCm': 10, 'heightCm': 10, 'dpi': 300}},
                # a theme alias whose installed font is Arial Bold (present wherever QGIS checks run on macOS)
                'fonts': [{'family': 'Demo Sans', 'file': 'assets/fonts/demo.ttf',
                           'names': {'family': 'Arial', 'subfamily': 'Bold', 'fullName': 'Arial Bold', 'postScriptName': 'Arial-BoldMT',
                                     'localized': [{'language': 'zh-CN', 'family': 'Arial', 'fullName': 'Arial Bold'}]}}],
                'notes': [{'kind': 'approximated', 'layer': 'roads', 'property': 'line-blur', 'reason': 'a made-up note'}],
                'layers': layers,
                'labelPlacements': [
                    {'layer': 'names', 'featureId': 1, 'text': 'Site 1', 'position': [10.021, 0.421], 'anchor': 'left',
                     'style': {'font': 'Demo Sans', 'size': 13.4, 'color': '#333333', 'opacity': 0.9, 'letterSpacing': 0.1,
                               'halo': {'color': '#FFFFFF', 'width': 2, 'opacity': 0.78, 'blur': 0.6},
                               'box': {'fill': {'color': '#FFFFCC'}, 'stroke': {'color': '#333333', 'width': 0.5}, 'padding': 2}}},
                    {'layer': 'names', 'text': 'Site 2\nsecond line', 'position': [10.07, 0.47], 'anchor': 'bottom-right', 'rotation': 15,
                     'leader': [[10.05, 0.45], [10.07, 0.47]], 'leaderStroke': {'color': '#0000FF', 'width': 1.5, 'dash': [3, 2]},
                     'style': {'size': 12, 'color': '#C00000'}},
                    {'layer': 'inset.parcels', 'text': 'Inset', 'position': [10.045, 0.445], 'leader': [[10.04, 0.44], [10.045, 0.445]],
                     'style': {'font': 'Plain Alias', 'size': 10, 'color': '#000000'}}]}
    style = project / 'maps' / 'demo-map.map-style.json'
    style.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'project': project, 'basemap': basemap, 'style': style, 'document': document}


# An image taller than wide (1:2), red over blue, so its size, proportions and direction can be read from a render
TALL_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20">'
            '<rect x="0" y="0" width="10" height="10" fill="#FF0000"/><rect x="0" y="10" width="10" height="10" fill="#0000FF"/></svg>')
WIDE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
            '<rect x="0" y="0" width="20" height="20" fill="#00CC00"/><rect x="20" y="0" width="20" height="20" fill="#FF00FF"/></svg>')
FONT_NAMES = {400: 'Regular', 700: 'Bold'}
MARKS = {'tall': (10.06, 0.48), 'wide': (10.08, 0.48), 'east': (10.06, 0.46), 'none': (10.08, 0.46), 'text-east': (10.06, 0.44), 'text-bad': (10.08, 0.44),
         'tall-east': (10.06, 0.42), 'sized': (10.09, 0.47)}
LABELS = {'number': (10.07, 0.405), 'pair': (10.09, 0.405), 'four': (10.07, 0.415)}


def build_round_two(root: Path):
    """A map that uses map-style's second-round fields, on a white ground, with sizes large enough to read off a render.

    The map is 0.1 degrees across, so at 800 pixels 1 px is 0.000125 degrees (and 1 map-style px where the render has 96 dpi).
    West half: hatched cells and lines with markers. East half: points with SVG markers and rotation, and labels.
    """
    project = root / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'icons').mkdir()
    (project / 'gis-project.json').write_text(json.dumps({'schemaVersion': 1, 'id': 'r2', 'name': 'R2', 'external': {}}), encoding='utf-8')
    for name, text in (('tall.svg', TALL_SVG), ('wide.svg', WIDE_SVG)):
        (project / 'icons' / name).write_text(text, encoding='utf-8')
    digest = {name: hashlib.sha256((project / 'icons' / name).read_bytes()).hexdigest() for name in ('tall.svg', 'wide.svg')}
    gpd.GeoDataFrame({'kind': ['ground']}, geometry=[box(*BBOX)], crs='EPSG:4326').to_file(project / 'data' / 'ground.geojson', driver='GeoJSON')
    gpd.GeoDataFrame({'kind': ['hatch', 'vertical', 'cross', 'outlined']},
                     geometry=[box(10.005, 0.46, 10.025, 0.48), box(10.03, 0.46, 10.05, 0.48), box(10.005, 0.435, 10.025, 0.455), box(10.03, 0.435, 10.05, 0.455)],
                     crs='EPSG:4326').to_file(project / 'data' / 'cells.geojson', driver='GeoJSON')
    gpd.GeoDataFrame({'kind': ['with', 'free', 'back', 'turned'], 'bearing': [None, None, None, 45.0]},
                     geometry=[LineString([(10.005, 0.425), (10.05, 0.425)]), LineString([(10.005, 0.415), (10.05, 0.415)]),
                               LineString([(10.05, 0.405), (10.005, 0.405)]), LineString([(10.045, 0.4 + 0.0005), (10.045, 0.43)])],
                     crs='EPSG:4326').to_file(project / 'data' / 'routes.geojson', driver='GeoJSON')
    gpd.GeoDataFrame({'kind': list(MARKS), 'bearing': [0.0, 0.0, 90.0, None, 0.0, 0.0, 90.0, 0.0],
                      'bearing_text': ['0', '0', '0', '0', '90', 'not a number', '0', '0'], 'scale': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]},
                     geometry=[Point(*position) for position in MARKS.values()],
                     crs='EPSG:4326').to_file(project / 'data' / 'marks.geojson', driver='GeoJSON')
    arrow = {'fill': {'color': '#C00000'}}
    style_box = {'fill': {'color': '#FFFF00'}, 'stroke': {'color': '#000000', 'width': 1}}
    text = {'font': 'Demo Text', 'size': 14, 'color': '#000000'}

    def marker_rule(kind, symbol):
        return {'filter': {'field': 'kind', 'op': '==', 'value': kind}, 'label': kind, 'symbol': symbol}

    layers = [
        {'id': 'ground', 'group': 'ground', 'geometry': 'polygon', 'data': {'path': 'data/ground.geojson'},
         'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#FFFFFF'}}}},
        {'id': 'cells', 'group': 'fills', 'geometry': 'polygon', 'data': {'path': 'data/cells.geojson'},
         'renderer': {'type': 'categorized', 'field': 'kind', 'classes': [
             {'value': 'hatch', 'symbol': {'fill': {'color': '#CCCCFF', 'pattern': {'kind': 'hatch', 'angle': 45, 'spacing': 20, 'width': 4, 'color': '#00AA00'}}}},
             {'value': 'vertical', 'symbol': {'fill': {'color': '#FFFFFF', 'opacity': 0, 'pattern': {'kind': 'hatch', 'angle': 90, 'spacing': 40, 'width': 10, 'color': '#AA0000'}}}},
             {'value': 'cross', 'symbol': {'fill': {'color': '#FFEEBB', 'pattern': {'kind': 'crossHatch', 'angle': 0, 'spacing': 40, 'width': 10, 'color': '#0000AA', 'opacity': 0.5}}}},
             {'value': 'outlined', 'symbol': {'fill': {'color': '#EEEEEE'}, 'stroke': {'color': '#000000', 'width': 2},
                                              'marker': {'shape': 'circle', 'size': 12, 'placement': 'line', 'spacing': 80}}}]}},
        {'id': 'routes', 'group': 'lines', 'geometry': 'line', 'data': {'path': 'data/routes.geojson'},
         'renderer': {'type': 'rule', 'rules': [
             marker_rule('with', {'stroke': {'color': '#808080', 'width': 2, 'dash': [6, 3]}, **arrow,
                                  'marker': {'shape': 'triangle', 'size': 20, 'placement': 'line', 'spacing': 80}}),
             marker_rule('free', {'stroke': {'color': '#808080', 'width': 2}, **arrow,
                                  'marker': {'shape': 'triangle', 'size': 20, 'rotation': 180, 'placement': 'line', 'spacing': 80, 'alongLine': False}}),
             marker_rule('back', {'stroke': {'color': '#808080', 'width': 2},
                                  'marker': {'shape': 'chevron', 'size': 20, 'placement': 'line', 'spacing': 160}}),
             marker_rule('turned', {'marker': {'shape': 'svg', 'size': 24, 'placement': 'line', 'spacing': 100, 'rotation': 45, 'rotationField': 'bearing',
                                               'svg': {'path': 'icons/tall.svg', 'sha256': digest['tall.svg']}}})]}},
        {'id': 'marks', 'group': 'points', 'geometry': 'point', 'data': {'path': 'data/marks.geojson'},
         'renderer': {'type': 'rule', 'rules': [
             marker_rule('tall', {'marker': {'shape': 'svg', 'size': 40, 'svg': {'path': 'icons/tall.svg', 'sha256': digest['tall.svg']}}}),
             marker_rule('wide', {'marker': {'shape': 'svg', 'size': 40, 'svg': {'path': 'icons/wide.svg', 'sha256': digest['wide.svg']}}}),
             marker_rule('east', {**arrow, 'marker': {'shape': 'triangle', 'size': 30, 'rotationField': 'bearing'}}),
             marker_rule('none', {**arrow, 'marker': {'shape': 'triangle', 'size': 30, 'rotationField': 'bearing', 'rotation': 180}}),
             marker_rule('text-east', {**arrow, 'marker': {'shape': 'triangle', 'size': 30, 'rotationField': 'bearing_text'}}),
             marker_rule('text-bad', {**arrow, 'marker': {'shape': 'triangle', 'size': 30, 'rotationField': 'bearing_text'}}),
             marker_rule('tall-east', {'marker': {'shape': 'svg', 'size': 40, 'rotationField': 'bearing', 'svg': {'path': 'icons/tall.svg', 'sha256': digest['tall.svg']}}})]}},
        {'id': 'sized', 'group': 'points', 'geometry': 'point', 'data': {'path': 'data/marks.geojson'}, 'filter': {'field': 'kind', 'op': '==', 'value': 'sized'},
         'renderer': {'type': 'single', 'symbol': {'marker': {'shape': 'svg', 'size': 40, 'svg': {'path': 'icons/tall.svg', 'sha256': digest['tall.svg']}}}},
         'size': {'field': 'scale', 'scale': 'linear', 'domain': [0, 1], 'range': [5, 30]}},
        {'id': 'inset.marks', 'panel': 'inset', 'group': 'points', 'geometry': 'point', 'data': {'path': 'data/marks.geojson'},
         'renderer': {'type': 'single', 'symbol': {'fill': {'color': '#00AA00'}, 'marker': {'shape': 'circle', 'size': 10}}}},
    ]
    for layer in layers:
        layer.setdefault('panel', 'main')
    fonts = [{'family': 'Demo Text', 'weight': weight, 'file': f'assets/fonts/demo-{weight}.ttf',
              'names': {'family': 'Arial', 'subfamily': FONT_NAMES[weight], 'fullName': f'Arial {FONT_NAMES[weight]}'}} for weight in (400, 700)]
    fonts += [{'family': 'One Weight', 'weight': '500', 'names': {'family': 'Arial', 'subfamily': 'Regular'}},
              {'family': 'No Style', 'weight': 700, 'names': {'family': 'Arial'}}]
    placements = [
        {'text': 'number', 'position': list(LABELS['number']), 'style': {**text, 'box': {**style_box, 'padding': 6}}},
        {'text': 'pair', 'position': list(LABELS['pair']), 'style': {**text, 'box': {**style_box, 'padding': [2, 12]}}},
        {'text': 'four', 'position': list(LABELS['four']), 'style': {**text, 'box': {**style_box, 'padding': [2, 12, 8, 4]}}},
        {'text': 'bold', 'position': [10.09, 0.415], 'style': {**text, 'weight': 700}},
        {'text': 'closest', 'position': [10.07, 0.425], 'style': {**text, 'weight': '500'}},
        {'text': 'heavy', 'position': [10.09, 0.425], 'style': {**text, 'weight': 900}},
        {'text': 'alone', 'position': [10.07, 0.435], 'style': {**text, 'font': 'One Weight'}},
        {'text': 'derived', 'position': [10.09, 0.435], 'style': {**text, 'font': 'No Style', 'weight': 700}},
        {'layer': 'marks', 'panel': 'inset', 'text': 'own panel', 'position': [10.05, 0.45], 'style': text},
        {'layer': 'marks', 'text': 'layer panel', 'position': [10.09, 0.495], 'style': text},
        {'text': 'whole map', 'position': [10.02, 0.495], 'style': text}]
    document = {'contract': 'map-style', 'schema_version': 1, 'id': 'round-two', 'title': 'Round two', 'units': {'size': 'px'}, 'dataRoot': 'project',
                'extent': {'bbox': BBOX, 'panels': [{'id': 'main', 'bbox': BBOX, 'frame': {'x': 0, 'y': 0, 'width': 10, 'height': 10}},
                                                    {'id': 'inset', 'bbox': INSET, 'frame': {'x': 7, 'y': 7, 'width': 3, 'height': 3}}],
                           'print': {'widthCm': 10, 'heightCm': 10, 'dpi': 300}},
                'fonts': fonts, 'layers': layers, 'labelPlacements': placements}
    style = project / 'round-two.map-style.json'
    style.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'project': project, 'style': style, 'document': document, 'digest': digest}


def check_round_two_render(png, four_sides):
    """Measure a render of build_round_two (the map fills the image, one map-style px is one pixel): line spacing and width,
    the first marker half a spacing from the start, image sizes and directions, box padding. four_sides: the platform
    draws padding [2, 12, 8, 4] side by side; otherwise each pair is averaged."""
    import rasterio
    with rasterio.open(png) as source:
        image = source.read()[:3].transpose(1, 2, 0).astype(int)
    size = image.shape[0]

    def x_of(lon):
        return int(round((lon - BBOX[0]) / (BBOX[2] - BBOX[0]) * size))

    def y_of(lat):
        return int(round((BBOX[3] - lat) / (BBOX[3] - BBOX[1]) * size))

    def mask(hex_colour, tolerance=30):
        colour = np.array([int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)])
        return np.abs(image - colour).max(axis=2) <= tolerance

    def runs(flags):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], flags.astype(int), [0]))))
        return [(int(a), int(b) - int(a)) for a, b in zip(edges[::2], edges[1::2])]

    def box_of(colour, lon, lat, reach=30):
        """(left, top, right, bottom) of a colour around a point, or None."""
        x, y = x_of(lon), y_of(lat)
        window = mask(colour)[max(0, y - reach):y + reach, max(0, x - reach):x + reach]
        rows, columns = np.flatnonzero(window.any(axis=1)), np.flatnonzero(window.any(axis=0))
        if not len(rows):
            return None
        return (columns[0] + max(0, x - reach), rows[0] + max(0, y - reach), columns[-1] + max(0, x - reach) + 1, rows[-1] + max(0, y - reach) + 1)

    def pointing(colour, lon, lat):
        """The way a filled triangle points: its centre of mass lies towards its base."""
        left, top, right, bottom = box_of(colour, lon, lat)
        rows, columns = np.nonzero(mask(colour)[top:bottom, left:right])
        dx, dy = columns.mean() + 0.5 - (right - left) / 2, rows.mean() + 0.5 - (bottom - top) / 2
        return ('west' if dx > 0 else 'east') if abs(dx) > abs(dy) else ('north' if dy > 0 else 'south')

    def extent_of(*colours, lon, lat, reach=30):
        found = [box_of(c, lon, lat, reach) for c in colours]
        found = [b for b in found if b]
        left, top, right, bottom = min(b[0] for b in found), min(b[1] for b in found), max(b[2] for b in found), max(b[3] for b in found)
        return right - left, bottom - top

    # hatch at 45 degrees, 20 px apart and 4 wide: 28.3 px and 5.7 px along a row, each row up shifted one px east
    row = y_of(0.47)
    cell = slice(x_of(10.005), x_of(10.025))
    lines = runs(mask('#00AA00')[row, cell])
    assert np.median(np.diff([a for a, _ in lines])) == pytest_approx(28.3, 1.5) and np.median([b for _, b in lines]) == pytest_approx(5.7, 2)
    above = runs(mask('#00AA00')[row - 10, cell])
    shift = (above[1][0] - lines[1][0]) % 28.28
    assert shift == pytest_approx(10, 2.5), 'the hatch leans the wrong way'  # `/`, not `\\`
    assert mask('#CCCCFF')[row, cell].any()  # the fill shows between the lines
    # hatch at 90 degrees, 40 px apart and 10 wide, on a fill of opacity 0 (the white ground shows)
    cell = slice(x_of(10.03), x_of(10.05))
    lines = runs(mask('#AA0000')[row, cell])
    inner = lines[1:-1] if len(lines) > 2 else lines
    assert np.median(np.diff([a for a, _ in lines])) == pytest_approx(40, 1.5) and np.median([b for _, b in inner]) == pytest_approx(10, 1.5)
    assert mask('#FFFFFF')[row, cell].any()
    # cross hatch: a quarter of each direction is line, so 9/16 of the cell is bare; lines at opacity 0.5 over the fill, darker at crossings
    area = image[y_of(0.455) + 2:y_of(0.435) - 2, x_of(10.005) + 2:x_of(10.025) - 2]
    bare = (np.abs(area - np.array([0xFF, 0xEE, 0xBB])).max(axis=2) <= 10).mean()
    assert bare == pytest_approx(9 / 16, 0.05)
    single = np.abs(area - np.array([0xFF, 0xEE, 0xBB]) // 2 - np.array([0, 0, 0xAA // 2])).max(axis=2) <= 12
    assert single.mean() == pytest_approx(6 / 16, 0.06)
    # markers along lines: the first half a spacing (40 px) from the start, then every 80 px; 20 px triangles
    west = x_of(10.005)
    centres = []
    for lat in (0.425, 0.415):
        columns = np.flatnonzero(mask('#C00000')[y_of(lat) - 12:y_of(lat) + 12, west:x_of(10.05)].any(axis=0))
        ends = np.flatnonzero(np.diff(columns) > 1)
        starts, stops = columns[np.r_[0, ends + 1]], columns[np.r_[ends, len(columns) - 1]] + 1
        centres.append((starts + stops) / 2)
        assert centres[-1][0] == pytest_approx(40, 2) and np.median(np.diff(centres[-1])) == pytest_approx(80, 1.5)
    # the markers turn with the line, or keep to the page with alongLine false (rotation 180 here)
    assert pointing('#C00000', 10.005 + centres[0][0] / size * 0.1, 0.425) == 'east'
    assert pointing('#C00000', 10.005 + centres[1][0] / size * 0.1, 0.415) == 'south'
    # a line drawn east to west starts at its east end
    chevrons = runs(mask('#808080', 60)[y_of(0.405) - 5, west:x_of(10.05) + 1])
    assert len(chevrons) == 2 and abs(chevrons[1][0] - chevrons[0][0] - 160) <= 3
    # an image along a line, turned 45 + field 45 = 90 past the line's direction (north): red, its top, points east
    x, y = x_of(10.045), y_of(0.4005) - 50
    red, blue = box_of('#FF0000', 10.045, 0.4005 + 50 / size * 0.1, 25), box_of('#0000FF', 10.045, 0.4005 + 50 / size * 0.1, 25)
    assert blue[0] < red[0] and abs((blue[0] + red[2]) / 2 - x) <= 2 and abs((red[1] + red[3]) / 2 - y) <= 2
    assert extent_of('#FF0000', '#0000FF', lon=10.045, lat=0.4005 + 50 / size * 0.1, reach=25) == (pytest_approx(24, 2), pytest_approx(12, 2))
    # SVG markers keep their proportions with the larger side at the size
    assert extent_of('#FF0000', '#0000FF', lon=MARKS['tall'][0], lat=MARKS['tall'][1]) == (pytest_approx(20, 2), pytest_approx(40, 2))
    assert extent_of('#00CC00', '#FF00FF', lon=MARKS['wide'][0], lat=MARKS['wide'][1]) == (pytest_approx(40, 2), pytest_approx(20, 2))
    tall = box_of('#FF0000', *MARKS['tall']), box_of('#0000FF', *MARKS['tall'])
    assert tall[0][3] <= tall[1][1] + 1  # own colours, red on top
    wide = box_of('#00CC00', *MARKS['wide']), box_of('#FF00FF', *MARKS['wide'])
    assert wide[0][2] <= wide[1][0] + 1
    # size by field sets the larger side of an image: radius 30 px at value 1
    assert extent_of('#FF0000', '#0000FF', lon=MARKS['sized'][0], lat=MARKS['sized'][1], reach=45) == (pytest_approx(30, 3), pytest_approx(60, 3))
    # rotation by field, clockwise, added to rotation; NULL and non-numeric add nothing
    assert pointing('#C00000', *MARKS['east']) == 'east'
    assert pointing('#C00000', *MARKS['none']) == 'south'
    assert pointing('#C00000', *MARKS['text-east']) == 'east'
    assert pointing('#C00000', *MARKS['text-bad']) == 'north'
    east = box_of('#FF0000', *MARKS['tall-east']), box_of('#0000FF', *MARKS['tall-east'])
    assert east[1][2] <= east[0][0] + 1  # the image too: its top, red, now points east

    # box padding, against the label with 6 px on every side (the words differ, so a pixel or two of play)
    def gaps(lon, lat):
        left, top, right, bottom = box_of('#FFFF00', lon, lat, 60)
        inside = mask('#000000', 110)[top + 2:bottom - 2, left + 2:right - 2]
        rows, columns = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
        return columns[0] + 2, rows[0] + 2, right - left - columns[-1] - 3, bottom - top - rows[-1] - 3  # left, top, right, bottom
    base = gaps(*LABELS['number'])
    pair = gaps(*LABELS['pair'])
    assert (pair[0] - base[0], pair[1] - base[1], pair[2] - base[2], pair[3] - base[3]) == (pytest_approx(6, 3), pytest_approx(-4, 3), pytest_approx(6, 3), pytest_approx(-4, 3))
    four = gaps(*LABELS['four'])
    wanted = (4, 2, 12, 8) if four_sides else (8, 5, 8, 5)  # left, top, right, bottom
    assert tuple(a - b for a, b in zip(four, base)) == tuple(pytest_approx(w - 6, 3) for w in wanted)


class pytest_approx:  # noqa: N801 - reads like pytest.approx; this helper module does not import pytest
    def __init__(self, value, tolerance):
        self.value, self.tolerance = value, tolerance

    def __eq__(self, other):
        return abs(other - self.value) <= self.tolerance

    def __repr__(self):
        return f'{self.value} +- {self.tolerance}'
