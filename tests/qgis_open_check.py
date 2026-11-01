"""Open a package.py pack GeoPackage in QGIS and report what QGIS sees, as JSON (run with QGIS's own Python).

    <qgis python> qgis_open_check.py PACK.gpkg PROJECT_NAME OUT.png [LON,LAT ...]

Reads the project stored in the GeoPackage, lists its layers in panel and drawing order with their validity,
renderer, classes and labelling, adds every table on its own to see the default style QGIS applies, renders the
project at its saved extent to OUT.png and samples the colour at each LON,LAT.
"""
import json
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFontUtils,  # noqa: E402
                       QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY, QgsProject, QgsRasterLayer, QgsReferencedRectangle,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402


def symbol_layers(symbol):
    """The symbol layer classes QGIS built, with those of a sub-symbol (the marker of a marker line) after a slash."""
    rows = []
    for item in symbol.symbolLayers():
        inner = item.subSymbol()
        rows.append(item.layerType() + ('/' + ','.join(symbol_layers(inner)) if inner is not None else ''))
    return rows


def renderer_summary(layer):
    renderer = layer.renderer()
    row = {'renderer': renderer.type()}
    if renderer.type() == 'categorizedSymbol':
        row['field'] = renderer.classAttribute()
        row['classes'] = [{'value': c.value(), 'color': c.symbol().color().name(), 'alpha': c.symbol().color().alpha(), 'render': c.renderState(),
                           'symbol_layers': symbol_layers(c.symbol())}
                          for c in renderer.categories()]
    elif renderer.type() == 'graduatedSymbol':
        row['field'] = renderer.classAttribute()
        row['classes'] = [{'lower': r.lowerValue(), 'upper': r.upperValue(), 'color': r.symbol().color().name()} for r in renderer.ranges()]
    elif renderer.type() == 'RuleRenderer':
        def walk(rule):
            return [{'filter': r.filterExpression(), 'label': r.label(), 'active': r.active(),
                     'color': r.symbol().color().name() if r.symbol() else None,
                     'symbol_layers': symbol_layers(r.symbol()) if r.symbol() else [], 'children': walk(r)} for r in rule.children()]
        row['rules'] = walk(renderer.rootRule())
    elif renderer.type() == 'singleSymbol':
        symbol = renderer.symbol()
        row['color'] = symbol.color().name()
        row['symbol_layers'] = symbol_layers(symbol)
        marker = symbol.symbolLayers()[0] if symbol.symbolLayers() else None
        if marker is not None and marker.layerType() == 'SimpleMarker':
            row['size_expression'] = marker.dataDefinedProperties().property(marker.PropertySize).expressionString()
    return row


def font_summary(settings):
    """The font a label setting asks for, its style, and whether this system has that family."""
    text = settings.format()
    family = text.font().family()
    return {'family': family, 'style': text.namedStyle(), 'installed': QgsFontUtils.fontFamilyOnSystem(family),
            'buffer': text.buffer().enabled(), 'background': text.background().enabled()}


def labeling_summary(labeling):
    if labeling.type() == 'simple':
        return [font_summary(labeling.settings())]
    return [{'filter': rule.filterExpression(), **font_summary(rule.settings())} for rule in labeling.rootRule().children() if rule.settings()]


def layer_summary(layer):
    row = {'name': layer.name(), 'valid': layer.isValid(), 'crs': layer.crs().authid(), 'source': layer.source()}
    if isinstance(layer, QgsVectorLayer):
        row.update(features=layer.featureCount(), labels=layer.labelsEnabled(), opacity=layer.opacity(),
                   labeling=layer.labeling().type() if layer.labeling() else None, **renderer_summary(layer))
        if layer.labeling():
            row['label_settings'] = labeling_summary(layer.labeling())
        row['subset'] = layer.subsetString()
    elif isinstance(layer, QgsRasterLayer):
        row.update(renderer=layer.renderer().type(), opacity=layer.renderer().opacity(), width=layer.width(), height=layer.height())
    return row


def main():
    gpkg, name, png = sys.argv[1:4]
    points = [tuple(map(float, p.split(','))) for p in sys.argv[4:]]
    app = QgsApplication([], False)  # noqa: F841 - must stay alive while QGIS is used
    app.initQgis()
    project = QgsProject.instance()
    ok = project.read(f'geopackage:{gpkg}?projectName={name}')
    root = project.layerTreeRoot()
    report = {'read': ok, 'crs': project.crs().authid(), 'title': project.title(),
              'tree': [{'group': g.name(), 'layers': [l.layerId() for l in g.findLayers()]} for g in root.findGroups()],
              'panel_order': [l.layerId() for l in root.findLayers()],
              'drawing_order_top_first': [l.id() for l in root.layerOrder()],
              'checked': {l.layerId(): l.isVisible() for l in root.findLayers()},
              'layers': {lid: layer_summary(layer) for lid, layer in project.mapLayers().items()},
              'background': project.backgroundColor().name(),
              'themes': {name: sorted(project.mapThemeCollection().mapThemeVisibleLayerIds(name))
                         for name in project.mapThemeCollection().mapThemes()},
              'bookmarks': [b.name() for b in project.bookmarkManager().bookmarks()]}
    tables = {}
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer):
            table = layer.source().split('layername=')[1].split('|')[0]
            if table not in tables:
                alone = QgsVectorLayer(f'{gpkg}|layername={table}', table, 'ogr')
                related, _, names, _, _ = alone.listStylesInDatabase()
                tables[table] = {'valid': alone.isValid(), **renderer_summary(alone), 'labels': alone.labelsEnabled(),
                                 'styles': sorted(names[:related])}
    report['tables_alone'] = tables
    view = project.viewSettings().defaultViewExtent()
    settings = QgsMapSettings()
    settings.setLayers([l for l in root.layerOrder() if root.findLayer(l.id()).isVisible()])
    settings.setDestinationCrs(project.crs())
    extent = QgsReferencedRectangle(view, view.crs()) if not view.isEmpty() else project.mapLayers()[report['panel_order'][-1]].extent()
    settings.setExtent(extent)
    settings.setOutputSize(QSize(800, 800))
    settings.setOutputDpi(96)  # so one map-style px is one pixel
    settings.setBackgroundColor(project.backgroundColor())
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    image.save(png)
    report['extent'] = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
    to_map = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'), project.crs(), project)
    samples = []
    for lon, lat in points:
        point = to_map.transform(QgsPointXY(lon, lat))
        pixel = settings.mapToPixel().transform(point)
        samples.append({'lonlat': [lon, lat], 'color': QColor(image.pixel(int(pixel.x()), int(pixel.y()))).name()})
    report['samples'] = samples
    print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
    os._exit(0)  # QGIS may crash while tearing down a headless application; the report is complete by now


if __name__ == '__main__':
    main()
