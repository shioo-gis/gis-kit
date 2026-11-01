"""Open the layer files of a package.py pack --format arcmap package in ArcGIS Pro and report what Pro sees, as JSON (run
with the Python that ships with ArcGIS Pro, which has arcpy). Pro reads ArcMap layer files; this checks they survive that.

    <pro python> arcmap_open_check_pro.py WORK_FOLDER FILE [FILE ...]

FILE is a .lyr, or a .lpk that ExtractPackage unpacks into WORK_FOLDER.
"""
import glob
import json
import os
import sys

import arcpy


def colours(symbol):
    return [[round(v) for v in layer.color.values[:3]] for layer in getattr(symbol, 'symbolLayers', []) if getattr(layer, 'color', None) is not None]


def describe(layer):
    row = {'name': layer.name, 'group': layer.isGroupLayer, 'visible': layer.visible}
    if layer.isGroupLayer:
        return row
    row['broken'] = layer.isBroken
    row['source'] = layer.dataSource if layer.supports('DATASOURCE') else None
    definition = layer.getDefinition('V3')
    renderer = getattr(definition, 'renderer', None)
    row['transparency'] = getattr(definition, 'transparency', None)
    table = getattr(definition, 'featureTable', None)
    row['query'] = (table.definitionExpression or None) if table is not None else None
    if renderer is None:
        return row
    row['renderer'] = type(renderer).__name__
    if row['renderer'] == 'CIMSimpleRenderer':
        row['colours'] = colours(renderer.symbol.symbol)
    elif row['renderer'] == 'CIMUniqueValueRenderer':
        row['fields'] = list(renderer.fields or [])
        row['classes'] = [{'label': c.label, 'values': [v.fieldValues for v in c.values], 'colours': colours(c.symbol.symbol)} for g in renderer.groups or [] for c in g.classes]
        row['default'] = renderer.useDefaultSymbol
    elif row['renderer'] == 'CIMClassBreaksRenderer':
        row['field'] = renderer.field
        row['breaks'] = [{'label': b.label, 'upper': b.upperBound, 'colours': colours(b.symbol.symbol)} for b in renderer.breaks]
    if getattr(definition, 'labelClasses', None):
        row['labels'] = layer.showLabels
        row['label_classes'] = [c.expression for c in definition.labelClasses]
    return row


def main():
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    arcpy.env.overwriteOutput = True
    report = {'version': arcpy.GetInstallInfo()['Version'], 'files': []}
    for item in sys.argv[2:]:
        paths = [item]
        if item.lower().endswith('.lpk'):
            target = os.path.join(work, os.path.splitext(os.path.basename(item))[0] + '_lpk')
            arcpy.management.ExtractPackage(item, target)
            paths = sorted(glob.glob(os.path.join(target, '**', '*.lyr'), recursive=True))
        for path in paths:
            row = {'file': path}
            try:
                row['layers'] = [describe(layer) for layer in arcpy.mp.LayerFile(path).listLayers()]
            except Exception as error:
                row['error'] = repr(error)
            report['files'].append(row)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
