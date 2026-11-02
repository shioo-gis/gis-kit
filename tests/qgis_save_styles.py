"""Have QGIS itself save layer styles, so `package.py read` is checked on what QGIS writes (run with the Python that ships with QGIS).

    <qgis python> qgis_save_styles.py FOLDER

Every style is made-up and set through the QGIS API on an empty memory layer (a raster one on a tiny GeoTIFF), then
saved with saveNamedStyle as FOLDER/<name>.qml. tests/test_style_read.py reads them back.
"""
import os
import sys

import numpy as np
from osgeo import gdal, osr
from qgis.core import (Qgis, QgsApplication, QgsCategorizedSymbolRenderer, QgsColorRampShader, QgsFillSymbol, QgsGraduatedSymbolRenderer, QgsLineSymbol,
                       QgsLinePatternFillSymbolLayer, QgsMarkerLineSymbolLayer, QgsMarkerSymbol, QgsPalettedRasterRenderer, QgsPalLayerSettings, QgsRasterLayer, QgsRasterShader,
                       QgsRendererCategory, QgsRendererRange, QgsRuleBasedRenderer, QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer, QgsSingleBandPseudoColorRenderer,
                       QgsSingleSymbolRenderer, QgsSvgMarkerSymbolLayer, QgsTextBufferSettings, QgsTextFormat, QgsUnitTypes, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QFont

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20"><rect width="10" height="10" fill="param(fill) #FF0000"/>'
       '<rect y="10" width="10" height="10" fill="#0000FF"/></svg>')


def layer(geometry):
    return QgsVectorLayer(f'{geometry}?crs=EPSG:4326&field=kind:string&field=v:double&field=name:string', 'demo', 'memory')


def fill(color, outline='no'):
    return QgsFillSymbol.createSimple({'color': color, 'style': 'solid', 'outline_style': outline, 'outline_color': '0,0,0,255', 'outline_width': '0.5', 'outline_width_unit': 'MM'})


def stroke(color, width='0.8', **more):
    return QgsLineSymbol.createSimple({'line_color': color, 'line_width': width, 'line_width_unit': 'MM', **more})


def marker(name, color, size='4'):
    return QgsMarkerSymbol.createSimple({'name': name, 'color': color, 'size': size, 'size_unit': 'MM', 'outline_style': 'no'})


def single(geometry, symbol):
    item = layer(geometry)
    item.setRenderer(QgsSingleSymbolRenderer(symbol))
    return item


def main():
    folder = sys.argv[1]
    os.makedirs(folder, exist_ok=True)
    QgsApplication.setPrefixPath(os.environ.get('QGIS_PREFIX_PATH', ''), True)
    app = QgsApplication([], False)
    app.initQgis()
    styles = {}

    styles['single-fill'] = single('Polygon', fill('237,125,49,255', 'solid'))
    styles['single-line'] = single('LineString', stroke('192,0,0,255', '1', use_custom_dash='1', customdash='3;2', customdash_unit='MM', capstyle='round'))
    styles['single-point'] = single('Point', QgsMarkerSymbol.createSimple({'name': 'square', 'color': '255,0,0,255', 'size': '4', 'size_unit': 'MM', 'angle': '30',
                                                                           'outline_style': 'solid', 'outline_color': '0,0,0,255', 'outline_width': '0.4', 'outline_width_unit': 'MM'}))

    categories = [QgsRendererCategory('A', fill('255,0,0,255'), 'class A'), QgsRendererCategory('B', fill('0,255,0,128'), 'class B', False), QgsRendererCategory(None, fill('0,0,255,255'), 'other')]
    styles['categorized'] = layer('Polygon')
    styles['categorized'].setRenderer(QgsCategorizedSymbolRenderer('kind', categories))

    ranges = [QgsRendererRange(0, 10, fill('255,255,178,255'), 'low'), QgsRendererRange(10, 20, fill('189,0,38,255'), 'high')]
    styles['graduated'] = layer('Polygon')
    styles['graduated'].setRenderer(QgsGraduatedSymbolRenderer('v', ranges))

    root = QgsRuleBasedRenderer.Rule(None)
    first = QgsRuleBasedRenderer.Rule(stroke('255,0,0,255'))
    first.setFilterExpression('"kind" = \'a\'')
    first.setLabel('rule a')
    second = QgsRuleBasedRenderer.Rule(stroke('0,0,255,255'))
    second.setFilterExpression('"v" > 3 AND "name" IS NOT NULL')
    second.setLabel('rule b')
    third = QgsRuleBasedRenderer.Rule(stroke('0,255,0,255'))
    third.setFilterExpression('length($geometry) > 1')
    third.setLabel('rule c')
    for rule in (first, second, third):
        root.appendChild(rule)
    styles['rules'] = layer('LineString')
    styles['rules'].setRenderer(QgsRuleBasedRenderer(root))

    hatch = QgsLinePatternFillSymbolLayer()
    hatch.setLineAngle(45)
    hatch.setDistance(3)
    hatch.setDistanceUnit(QgsUnitTypes.RenderMillimeters)
    hatch.setSubSymbol(stroke('0,0,255,255', '0.3'))
    symbol = fill('255,255,204,255')
    symbol.appendSymbolLayer(hatch)
    styles['hatch'] = single('Polygon', symbol)

    along = QgsMarkerLineSymbolLayer(True, 20)
    along.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    along.setSubSymbol(marker('triangle', '255,0,0,255', '3'))
    symbol = stroke('128,128,128,255', '0.5')
    symbol.appendSymbolLayer(along)
    styles['marker-line'] = single('LineString', symbol)

    svg_path = os.path.join(folder, 'demo-icon.svg')
    with open(svg_path, 'w', encoding='utf-8') as target:
        target.write(SVG)
    image = QgsSvgMarkerSymbolLayer(svg_path, 5, 0)
    image.setFillColor(QColor(0, 128, 0))
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, image)
    styles['svg-marker'] = single('Point', symbol)

    symbol = stroke('0,0,0,255', '2')
    symbol.appendSymbolLayer(QgsSimpleLineSymbolLayer(QColor(255, 255, 255), 1))
    styles['two-lines'] = single('LineString', symbol)

    symbol = marker('circle', '255,0,0,255', '6')
    symbol.appendSymbolLayer(QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Star, 3, 0, Qgis.ScaleMethod.ScaleDiameter, QColor(255, 255, 255)))
    styles['stacked-markers'] = single('Point', symbol)

    labelled = single('Point', marker('circle', '0,0,0,255'))
    text = QgsTextFormat()
    text.setFont(QFont('Arial'))
    text.setNamedStyle('Bold')
    text.setSize(10)
    text.setSizeUnit(QgsUnitTypes.RenderPoints)
    text.setColor(QColor(20, 30, 40))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize(1)
    buffer.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    buffer.setColor(QColor(255, 255, 255))
    text.setBuffer(buffer)
    settings = QgsPalLayerSettings()
    settings.fieldName = 'name'
    settings.setFormat(text)
    labelled.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    labelled.setLabelsEnabled(True)
    labelled.setOpacity(0.5)
    styles['labels'] = labelled

    tif = os.path.join(folder, 'demo-grid.tif')
    driver = gdal.GetDriverByName('GTiff').Create(tif, 2, 2, 1, gdal.GDT_Float32)
    driver.SetGeoTransform((0, 1, 0, 2, 0, -1))
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    driver.SetProjection(reference.ExportToWkt())
    driver.GetRasterBand(1).WriteArray(np.array([[0.0, 1.0], [2.0, 3.0]]))
    driver = None
    ramp = QgsRasterLayer(tif, 'grid')
    shader = QgsColorRampShader(0, 3)
    shader.setColorRampType(QgsColorRampShader.Interpolated)
    shader.setColorRampItemList([QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0), 'low'), QgsColorRampShader.ColorRampItem(3, QColor(255, 255, 255), 'high')])
    holder = QgsRasterShader()
    holder.setRasterShaderFunction(shader)
    ramp.setRenderer(QgsSingleBandPseudoColorRenderer(ramp.dataProvider(), 1, holder))
    styles['raster-ramp'] = ramp
    palette = QgsRasterLayer(tif, 'grid')
    palette.setRenderer(QgsPalettedRasterRenderer(palette.dataProvider(), 1, [QgsPalettedRasterRenderer.Class(1, QColor(166, 206, 227), 'one'), QgsPalettedRasterRenderer.Class(2, QColor(178, 223, 138), 'two')]))
    styles['raster-palette'] = palette

    for name, item in styles.items():
        message, saved = item.saveNamedStyle(os.path.join(folder, name + '.qml'))
        if not saved:
            raise SystemExit(f'{name}: {message}')
    print(Qgis.QGIS_VERSION, len(styles), flush=True)
    os._exit(0)  # the styles are saved; tearing QGIS down outside its own application can crash on the way out


if __name__ == '__main__':
    main()
