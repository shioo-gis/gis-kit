"""Have ArcGIS Pro itself save layer files, so `package.py read` is checked on what Pro writes (run with the Python that ships with Pro).

    <pro python> arcgis_save_styles.py FOLDER [PACKED.lyrx ...]

Makes up a small file geodatabase, sets symbology on its layers through arcpy.mp and saves each as FOLDER/<name>.lyrx.
Each PACKED.lyrx (from package.py pack --format arcgis-pro --lyrx, beside its data) is opened and saved again as
FOLDER/resaved-<name>.lyrx. Prints a JSON report; tests/test_style_read.py reads the files back.
"""
import json
import os
import shutil
import sys
import tempfile

import arcpy


def blank_project(folder):
    """A copy of the empty project that ships with Pro (arcpy cannot create one from nothing)."""
    install = arcpy.GetInstallInfo()['InstallDir']
    for root, _, files in os.walk(os.path.join(install, 'Resources')):
        if 'Blank.aprx' in files:
            target = os.path.join(folder, 'save.aprx')
            shutil.copy(os.path.join(root, 'Blank.aprx'), target)
            return arcpy.mp.ArcGISProject(target)
    raise SystemExit('no Blank.aprx found under the ArcGIS Pro install folder')


def feature_class(gdb, name, geometry, rows):
    arcpy.management.CreateFeatureclass(gdb, name, geometry, spatial_reference=arcpy.SpatialReference(4326))
    table = os.path.join(gdb, name)
    arcpy.management.AddField(table, 'kind', 'TEXT')
    arcpy.management.AddField(table, 'v', 'DOUBLE')
    with arcpy.da.InsertCursor(table, ['SHAPE@', 'kind', 'v']) as cursor:
        for row in rows:
            cursor.insertRow(row)
    return table


def rgb(*values):
    return {'RGB': list(values) + ([100] if len(values) == 3 else [])}


def main():
    folder = sys.argv[1]
    os.makedirs(folder, exist_ok=True)
    arcpy.env.overwriteOutput = True
    work = tempfile.mkdtemp(prefix='gis-kit-save-')
    gdb = arcpy.management.CreateFileGDB(work, 'demo.gdb')[0]
    sr = arcpy.SpatialReference(4326)
    squares = [arcpy.Polygon(arcpy.Array([arcpy.Point(x, 0), arcpy.Point(x + 1, 0), arcpy.Point(x + 1, 1), arcpy.Point(x, 1)]), sr) for x in range(3)]
    polygons = feature_class(gdb, 'cells', 'POLYGON', [(squares[0], 'a', 1.0), (squares[1], 'b', 5.0), (squares[2], None, 9.0)])
    lines = feature_class(gdb, 'routes', 'POLYLINE', [(arcpy.Polyline(arcpy.Array([arcpy.Point(0, 0), arcpy.Point(3, 1)]), sr), 'a', 1.0)])
    points = feature_class(gdb, 'sites', 'POINT', [(arcpy.PointGeometry(arcpy.Point(1, 1), sr), 'a', 1.0)])
    project = blank_project(work)
    target = project.createMap('save')
    report = {'version': arcpy.GetInstallInfo()['Version'], 'saved': []}

    def add(path):
        return target.addDataFromPath(path)

    def save(layer, name):
        layer.saveACopy(os.path.join(folder, name + '.lyrx'))
        report['saved'].append(name)

    simple = add(polygons)
    simple.symbology.renderer.symbol.color = rgb(237, 125, 49)
    simple.symbology.renderer.symbol.outlineColor = rgb(0, 0, 0)
    simple.symbology.renderer.symbol.outlineWidth = 1.5
    simple.symbology = simple.symbology
    save(simple, 'simple-fill')

    unique = add(polygons)
    symbology = unique.symbology
    symbology.updateRenderer('UniqueValueRenderer')
    symbology.renderer.fields = ['kind']
    for group in symbology.renderer.listValueGroups() if hasattr(symbology.renderer, 'listValueGroups') else symbology.renderer.groups:
        for item in group.items:
            item.symbol.color = rgb(255, 0, 0) if item.values[0][0] == 'a' else rgb(0, 0, 255)
    unique.symbology = symbology
    unique.transparency = 25
    save(unique, 'unique-values')

    graded = add(polygons)
    symbology = graded.symbology
    symbology.updateRenderer('GraduatedColorsRenderer')
    symbology.renderer.classificationField = 'v'
    symbology.renderer.breakCount = 3
    graded.symbology = symbology
    save(graded, 'graduated')

    line = add(lines)
    line.symbology.renderer.symbol.color = rgb(192, 0, 0)
    line.symbology.renderer.symbol.width = 2
    line.symbology = line.symbology
    save(line, 'simple-line')

    point = add(points)
    point.symbology.renderer.symbol.color = rgb(0, 112, 192)
    point.symbology.renderer.symbol.size = 12
    point.symbology = point.symbology
    point.showLabels = True
    labels = point.listLabelClasses()[0]
    labels.expression = '$feature.kind'
    save(point, 'labelled-point')

    # symbols from Pro's own gallery, several symbol layers each, through the same symbol API
    gallery = {'gallery-hatch': (polygons, 'POLYGON', ('Hatch', 'hatch')), 'gallery-road': (lines, 'LINE', ('Highway', 'Road', 'Freeway')), 'gallery-point': (points, 'POINT', ('Pushpin', 'Hospital', 'Airport'))}
    for name, (data, style_class, words) in gallery.items():
        layer = add(data)
        items = project.listStyleItems('ArcGIS 2D', style_class)
        item = next((i for word in words for i in items if word in i.name), items[0] if items else None)
        if item is not None:
            symbology = layer.symbology
            symbology.renderer.symbol.applySymbolFromGallery(item.name)
            layer.symbology = symbology
        save(layer, name)

    resaved = []
    for path in sys.argv[2:]:
        name = 'resaved-' + os.path.splitext(os.path.basename(path))[0]
        arcpy.mp.LayerFile(path).saveACopy(os.path.join(folder, name + '.lyrx'))
        resaved.append(name)
    report['resaved'] = resaved
    del project
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
