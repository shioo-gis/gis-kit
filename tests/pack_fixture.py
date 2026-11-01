"""A made-up map for package.py pack: a project with a gis-project.json, an external data root and a map-style.

Everything is invented: open ocean near (10.05 E, 0.45 N), UTM zone 32N for the analysis data. The map has a main
panel and an inset panel (as a hero map does), a background colour, a tile layer and producer notes.
"""
from __future__ import annotations

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
