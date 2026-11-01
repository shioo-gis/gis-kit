"""Open a package.py pack --format arcgis-pro package in ArcGIS Pro and report what Pro sees, as JSON (run with
the Python that ships with ArcGIS Pro, which has arcpy).

    <pro python> arcgis_open_check.py FOLDER NAME OUT.png EPSG WEST,SOUTH,EAST,NORTH [LON,LAT ...]

FOLDER holds NAME.lpkx (and, packed with --lyrx, NAME.lyrx beside its geodatabase). Lists every layer of the
direct .lyrx and of the layer file extracted from the .lpkx (ExtractPackage) with its broken state, data source,
renderer, classes and colours, definition query, label classes and feature count; then adds the extracted layer
file, which is what a colleague opens, to a new map in EPSG, exports the extent (lon/lat) to OUT.png and samples the
colour at each LON,LAT.
"""
import json
import os
import shutil
import sys
import tempfile

import arcpy


def colour(value):
    return [round(v, 2) for v in value.values] if value is not None else None


def symbol_layers(reference):
    """The symbol layer classes as Pro reads them, and the properties connected to fields."""
    rows = [type(item).__name__ for item in getattr(reference.symbol, 'symbolLayers', [])]
    return rows + [f'{item.propertyName} by field' for item in reference.primitiveOverrides or []]


def describe(layer):
    row = {'name': layer.longName, 'group': layer.isGroupLayer, 'visible': layer.visible}
    if layer.isGroupLayer:
        return row
    row['broken'] = layer.isBroken
    row['source'] = layer.dataSource if layer.supports('DATASOURCE') else None
    definition = layer.getDefinition('V3')
    row['type'] = type(definition).__name__
    renderer = getattr(definition, 'renderer', None)
    if renderer is not None:
        row['renderer'] = type(renderer).__name__
        if row['renderer'] == 'CIMUniqueValueRenderer':
            row['fields'] = list(renderer.fields or [])
            row['expression'] = bool(renderer.valueExpressionInfo)
            row['classes'] = [{'label': c.label, 'values': [v.fieldValues for v in c.values], 'visible': c.visible,
                               'colours': [colour(l.color) for l in getattr(c.symbol.symbol, 'symbolLayers', []) if hasattr(l, 'color')],
                               'symbol_layers': symbol_layers(c.symbol)}
                              for g in renderer.groups or [] for c in g.classes]
        elif row['renderer'] == 'CIMClassBreaksRenderer':
            row['breaks'] = [{'label': b.label, 'upper': b.upperBound,
                              'colours': [colour(l.color) for l in b.symbol.symbol.symbolLayers if hasattr(l, 'color')]} for b in renderer.breaks]
        if getattr(renderer, 'visualVariables', None):
            row['visual_variables'] = [f'{type(v).__name__}:{getattr(v, "variableType", "")}' for v in renderer.visualVariables]
    if getattr(definition, 'colorizer', None) is not None:
        row['colorizer'] = type(definition.colorizer).__name__
    table = getattr(definition, 'featureTable', None)
    if table is not None:
        row['definition'] = table.definitionExpression or None
        row['features'] = int(arcpy.management.GetCount(layer)[0])
    if getattr(definition, 'labelClasses', None) and layer.supports('SHOWLABELS'):
        row['labels'] = layer.showLabels
        row['label_classes'] = [{'name': c.name, 'where': c.whereClause or None, 'font': c.textSymbol.symbol.fontFamilyName,
                                 'style': c.textSymbol.symbol.fontStyleName} for c in definition.labelClasses]
    return row


def read(path):
    return [describe(layer) for layer in arcpy.mp.LayerFile(path).listLayers()]


def blank_project(folder):
    """A copy of the empty project that ships with Pro (arcpy cannot create one from nothing)."""
    install = arcpy.GetInstallInfo()['InstallDir']
    for root, _, files in os.walk(os.path.join(install, 'Resources')):
        if 'Blank.aprx' in files:
            target = os.path.join(folder, 'check.aprx')
            shutil.copy(os.path.join(root, 'Blank.aprx'), target)
            return arcpy.mp.ArcGISProject(target)
    raise SystemExit('no Blank.aprx found under the ArcGIS Pro install folder')


def main():
    folder, name, png, epsg, bbox = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), [float(v) for v in sys.argv[5].split(',')]
    points = [tuple(float(v) for v in item.split(',')) for item in sys.argv[6:]]
    arcpy.env.overwriteOutput = True
    work = tempfile.mkdtemp(prefix='gis-kit-pro-')
    report = {'version': arcpy.GetInstallInfo()['Version']}
    direct = os.path.join(folder, name + '.lyrx')
    if os.path.exists(direct):
        report['lyrx'] = read(direct)
    extracted = os.path.join(work, 'extracted')
    arcpy.management.ExtractPackage(os.path.join(folder, name + '.lpkx'), extracted)
    files = sorted(os.path.relpath(os.path.join(root, f), extracted).replace('\\', '/')
                   for root, _, names in os.walk(extracted) if not root.endswith('.gdb') for f in names)
    report['extracted'] = files
    inner = [f for f in files if f.endswith('.lyrx')]
    report['lpkx'] = read(os.path.join(extracted, inner[0])) if inner else None
    project = blank_project(work)
    target = project.createMap('check')
    sr = arcpy.SpatialReference(epsg)
    target.spatialReference = sr
    target.addLayer(arcpy.mp.LayerFile(os.path.join(extracted, inner[0])))
    layout = project.createLayout(20, 20, 'CENTIMETER')
    frame = layout.createMapFrame(arcpy.Extent(0, 0, 20, 20), target)
    corners = [arcpy.Point(bbox[0], bbox[1]), arcpy.Point(bbox[2], bbox[1]), arcpy.Point(bbox[2], bbox[3]), arcpy.Point(bbox[0], bbox[3])]
    frame.camera.setExtent(arcpy.Polygon(arcpy.Array(corners), arcpy.SpatialReference(4326)).projectAs(sr).extent)
    extent = frame.camera.getExtent()
    layout.exportToPNG(png, resolution=96)
    report['samples'] = []
    if points:
        from PIL import Image
        image = Image.open(png).convert('RGB')
        width, height = image.size
        for lon, lat in points:
            point = arcpy.PointGeometry(arcpy.Point(lon, lat), arcpy.SpatialReference(4326)).projectAs(sr).firstPoint
            x = int((point.X - extent.XMin) / (extent.XMax - extent.XMin) * width)
            y = int((extent.YMax - point.Y) / (extent.YMax - extent.YMin) * height)
            report['samples'].append('#%02x%02x%02x' % image.getpixel((x, y)))
    del project
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
