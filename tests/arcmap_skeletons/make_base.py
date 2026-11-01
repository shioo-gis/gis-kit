# -*- coding: utf-8 -*-
# Made-up data in a file geodatabase and one base .lyr per geometry, written by ArcMap itself (ArcMap's Python 2.7):
#     python make_base.py ROOT
import os, sys
import arcpy
ROOT = sys.argv[1]
GDB = os.path.join(ROOT, 'skeleton.gdb')
arcpy.env.overwriteOutput = True
if not arcpy.Exists(GDB):
    arcpy.CreateFileGDB_management(ROOT, 'skeleton.gdb')
sr = arcpy.SpatialReference(4326)
def table(name, kind):
    path = os.path.join(GDB, name)
    arcpy.CreateFeatureclass_management(GDB, name, kind, spatial_reference=sr)
    arcpy.AddField_management(path, 'kind', 'TEXT', field_length=50)
    arcpy.AddField_management(path, 'value', 'DOUBLE')
    return path
polys = table('polygons', 'POLYGON')
with arcpy.da.InsertCursor(polys, ['SHAPE@', 'kind', 'value']) as cur:
    for i, (k, v) in enumerate([('a', 10.0), ('b', 40.0), ('c', 80.0)]):
        x = 10.0 + i * 0.03
        cur.insertRow([arcpy.Polygon(arcpy.Array([arcpy.Point(x, 0.4), arcpy.Point(x, 0.42), arcpy.Point(x + 0.02, 0.42), arcpy.Point(x + 0.02, 0.4)]), sr), k, v])
lines = table('lines', 'POLYLINE')
with arcpy.da.InsertCursor(lines, ['SHAPE@', 'kind', 'value']) as cur:
    for i, (k, v) in enumerate([('a', 10.0), ('b', 40.0)]):
        y = 0.4 + i * 0.03
        cur.insertRow([arcpy.Polyline(arcpy.Array([arcpy.Point(10.0, y), arcpy.Point(10.1, y)]), sr), k, v])
points = table('points', 'POINT')
with arcpy.da.InsertCursor(points, ['SHAPE@', 'kind', 'value']) as cur:
    for i, (k, v) in enumerate([('a', 10.0), ('b', 40.0), ('c', 80.0)]):
        cur.insertRow([arcpy.Point(10.0 + i * 0.03, 0.45), k, v])
for name in ('polygons', 'lines', 'points'):
    out = os.path.join(ROOT, 'base_%s.lyr' % name)
    if os.path.exists(out):
        os.remove(out)
    arcpy.MakeFeatureLayer_management(os.path.join(GDB, name), 'tmp_' + name)
    arcpy.SaveToLayerFile_management('tmp_' + name, out, 'ABSOLUTE')
    print out, os.path.getsize(out)
