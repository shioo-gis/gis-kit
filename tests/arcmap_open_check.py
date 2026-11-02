# -*- coding: utf-8 -*-
"""Open layer files or a package.py pack --format arcmap package in ArcMap and report what ArcMap sees, as JSON (run with
the 32-bit Python 2.7 that ships with ArcMap, which has arcpy).

    <arcmap python> arcmap_open_check.py WORK_FOLDER OUT.png EPSG WEST,SOUTH,EAST,NORTH FILE [FILE ...] [--sample NAME=LON,LAT ...]

FILE is a .lyr, or a .lpk that ExtractPackage unpacks into WORK_FOLDER (each layer file of its v107 folder is then read).
Lists every layer with its broken state, data source, symbology type and classes, definition query, transparency, labels
and feature count; adds them to a new data frame in EPSG, exports the extent (lon/lat) to OUT.png, and reports the colour at
each sample point.
"""
import glob
import json
import os
import shutil
import sys

import arcpy
import arcpy.mapping as mapping


def text(value):
    return value if isinstance(value, unicode) else str(value).decode('utf-8', 'replace') if value is not None else None


def describe(layer):
    row = {'name': text(layer.name), 'long_name': text(layer.longName), 'group': layer.isGroupLayer, 'visible': layer.visible}
    if layer.isGroupLayer:
        return row
    row['broken'] = layer.isBroken
    row['source'] = text(layer.dataSource) if layer.supports('DATASOURCE') else None
    row['dataset'] = text(layer.datasetName) if layer.supports('DATASETNAME') else None
    row['symbology'] = layer.symbologyType if layer.supports('SYMBOLOGY') or hasattr(layer, 'symbologyType') else None
    if layer.supports('TRANSPARENCY'):
        row['transparency'] = layer.transparency
    if layer.supports('DEFINITIONQUERY'):
        row['query'] = text(layer.definitionQuery)
    if row['symbology'] == 'UNIQUE_VALUES':
        row['value_field'] = text(layer.symbology.valueField)
        row['values'] = [text(v) for v in layer.symbology.classValues]
        row['labels'] = [text(v) for v in layer.symbology.classLabels]
        row['show_other'] = layer.symbology.showOtherValues
    elif row['symbology'] == 'GRADUATED_COLORS':
        row['value_field'] = text(layer.symbology.valueField)
        row['breaks'] = list(layer.symbology.classBreakValues)
        row['labels'] = [text(v) for v in layer.symbology.classBreakLabels]
    if not layer.isBroken:
        source = layer.dataSource
        row['fields'] = [text(f.name) for f in arcpy.ListFields(source)]
        view = 'check_%d' % abs(hash(layer.longName))
        arcpy.MakeFeatureLayer_management(source, view, layer.definitionQuery if layer.supports('DEFINITIONQUERY') else '')
        row['features'] = int(arcpy.GetCount_management(view).getOutput(0))
        arcpy.Delete_management(view)
        row['sr'] = text(arcpy.Describe(source).spatialReference.name)
        extent = layer.getExtent()
        row['extent'] = [extent.XMin, extent.YMin, extent.XMax, extent.YMax]
    if layer.supports('SHOWLABELS'):
        row['show_labels'] = layer.showLabels
        if layer.supports('LABELCLASSES'):
            row['label_classes'] = [{'name': text(c.className), 'expression': text(c.expression), 'query': text(c.SQLQuery), 'show': c.showClassLabels}
                                    for c in layer.labelClasses]
    return row


def template():
    """An empty map document to draw in: the first with a data frame among those that ship with ArcMap."""
    root = os.path.join(arcpy.GetInstallInfo()['InstallDir'], 'MapTemplates')
    for folder in ('Traditional Layouts', 'Standard Page Sizes'):
        for path in sorted(glob.glob(os.path.join(root, folder, '*.mxd'))):
            document = mapping.MapDocument(path)
            frames = mapping.ListDataFrames(document)
            del document
            if frames:
                return path
    raise SystemExit('no map document with a data frame under ' + root)


def samples(png, extent, reference, points, width, height):
    """The colour under each lon/lat, read back from the export through arcpy (ArcMap's Python has no imaging library)."""
    image = arcpy.RasterToNumPyArray(png)
    out = {}
    wgs84 = arcpy.SpatialReference(4326)
    for name, lon, lat in points:
        point = arcpy.PointGeometry(arcpy.Point(lon, lat), wgs84).projectAs(reference).firstPoint
        x = int((point.X - extent.XMin) / (extent.XMax - extent.XMin) * width)
        y = int((extent.YMax - point.Y) / (extent.YMax - extent.YMin) * height)
        out[name] = '#%02x%02x%02x' % tuple(int(image[band, y, x]) for band in range(3))
    return out


def main():
    arguments = sys.argv[1:]
    points = []
    while '--sample' in arguments:
        at = arguments.index('--sample')
        name, _, position = arguments[at + 1].partition('=')
        lon, lat = position.split(',')
        points.append((name, float(lon), float(lat)))
        del arguments[at:at + 2]
    work, png, epsg, bbox = arguments[0], arguments[1], int(arguments[2]), [float(v) for v in arguments[3].split(',')]
    arguments = arguments[4:]
    arcpy.env.overwriteOutput = True
    if not os.path.isdir(work):
        os.makedirs(work)
    report = {'version': arcpy.GetInstallInfo()['Version'], 'files': []}
    files = []
    for item in arguments:
        if item.lower().endswith('.lpk'):
            target = os.path.join(work, os.path.splitext(os.path.basename(item))[0] + '_lpk')
            if os.path.isdir(target):
                shutil.rmtree(target)
            arcpy.ExtractPackage_management(item, target)
            found = []
            for root, folders, names in os.walk(target):
                folders[:] = [f for f in folders if not f.endswith('.gdb')]
                found += [os.path.relpath(os.path.join(root, n), target).replace('\\', '/') for n in names]
            report['extracted'] = sorted(text(f) for f in found)
            for folder in sorted(glob.glob(os.path.join(target, 'v*'))):
                files += sorted(glob.glob(os.path.join(folder, '*.lyr')))
        else:
            files.append(item)
    copy = os.path.join(work, 'check.mxd')
    shutil.copyfile(template(), copy)
    document = mapping.MapDocument(copy)
    frame = mapping.ListDataFrames(document)[0]
    for old in mapping.ListLayers(document, '', frame):
        mapping.RemoveLayer(frame, old)
    frame.spatialReference = arcpy.SpatialReference(epsg)
    for path in files:
        row = {'file': text(path), 'layers': []}
        try:
            opened = mapping.Layer(path)
            for layer in mapping.ListLayers(opened):
                row['layers'].append(describe(layer))
            mapping.AddLayer(frame, opened, 'TOP')
        except Exception as error:
            row['error'] = text(repr(error))
        report['files'].append(row)
    corners = arcpy.Array([arcpy.Point(bbox[0], bbox[1]), arcpy.Point(bbox[2], bbox[3])])
    box = arcpy.Polyline(corners, arcpy.SpatialReference(4326)).projectAs(frame.spatialReference).extent
    frame.extent = box
    document.activeView = frame.name
    width, height = 800, 800
    mapping.ExportToPNG(document, png, frame, width, height, 96, world_file=False, transparent_color=None)
    # a data frame keeps its own proportions and the export widens the shorter side again to fill the image
    extent = frame.extent
    x, y = (extent.XMin + extent.XMax) / 2, (extent.YMin + extent.YMax) / 2
    half = max(extent.width, extent.height) / 2
    shown = arcpy.Extent(x - half, y - half, x + half, y + half)
    report['frame_extent'] = [shown.XMin, shown.YMin, shown.XMax, shown.YMax]
    report['samples'] = samples(png, shown, frame.spatialReference, points, width, height) if points else {}
    del document
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
