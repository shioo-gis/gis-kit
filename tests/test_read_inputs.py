"""What each tool says about its input: tables, images with world files, null fields, southern boxes, -I, discovery."""
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
import pyogrio
import pytest
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.transform import from_origin
from shapely.geometry import Point, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
NO_GEOMETRY = 'has no geometry'


def run(tool, *args, flags=(), cwd=None):
    return subprocess.run([sys.executable, *flags, str(SCRIPTS/f'{tool}.py'), *map(str, args)],
                          capture_output=True, encoding='utf-8', errors='replace', cwd=cwd)


def refuses_table(result):
    assert result.returncode != 0 and 'Traceback' not in result.stderr, result.stderr
    assert NO_GEOMETRY in result.stderr and 'daily normalize' in result.stderr, result.stderr


@pytest.fixture
def table(tmp_path):
    path = tmp_path/'counts.csv'
    path.write_text('site,count,lon,lat\na,1,10.5,20.5\nb,2,11.5,21.5\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('tool,args', [
    ('inspect-data', ['{in}', 'summary']), ('inspect-data', ['{in}', 'summary', '--json']), ('inspect-data', ['{in}', 'bbox']),
    ('convert', ['{in}', '--output', '{out}.gpkg']), ('convert', ['{in}', '--output', '{out}.gpkg', '--crs', 'EPSG:3857']),
    ('clip', ['{in}', '-o', '{out}.gpkg', '--bbox', '0,0,1,1']), ('clip', ['{in}', '-o', '{out}.gpkg', '--where', 'count > 0']),
    ('buffer', ['{in}', '--distance', '10', '--output', '{out}.gpkg']),
    ('dissolve', ['{in}', '--output', '{out}.gpkg']), ('dissolve', ['{in}', '--output', '{out}.gpkg', '--no-global-merge']),
    ('export-table', ['{in}', '--output', '{out}.csv']),
    ('topo', ['{in}']),
])
def test_tables_get_a_message_not_a_traceback(tool, args, table, tmp_path):
    out = tmp_path/'out'
    result = run(tool, *(arg.format(**{'in': table, 'out': out}) for arg in args))
    refuses_table(result)
    assert not list(tmp_path.glob('out*'))


def test_every_table_source_is_refused_and_every_layer_with_geometry_is_not(tmp_path):
    data = tmp_path/'mixed.gpkg'
    gpd.GeoDataFrame({'a': [1]}, geometry=[Point(0, 0)], crs=4326).to_file(data, layer='points')
    pyogrio.write_dataframe(pd.DataFrame({'a': [1, 2]}), data, layer='plain', append=True)
    dbf = tmp_path/'plain.dbf'
    pyogrio.write_dataframe(pd.DataFrame({'a': [1, 2]}), dbf, driver='ESRI Shapefile')
    in_file = run('convert', data, '--layer', 'plain', '--output', tmp_path/'o.gpkg')
    refuses_table(in_file)
    assert 'Layers with geometry: points' in in_file.stderr
    refuses_table(run('convert', dbf, '--output', tmp_path/'o.gpkg'))
    converted = run('convert', data, '--layer', 'points', '--output', tmp_path/'p.gpkg')
    assert converted.returncode == 0, converted.stderr
    # A CSV with a WKT column and GeoJSON whose features have no geometry yet are still vector layers to GDAL.
    wkt = tmp_path/'wkt.csv'
    wkt.write_text('id,WKT\n1,"POINT (1 2)"\n', encoding='utf-8')
    empty = tmp_path/'null.geojson'
    empty.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': None, 'properties': {'a': 1}}]}), encoding='utf-8')
    for source in (wkt, empty):
        assert run('inspect-data', source, 'summary').returncode == 0, source


def test_clip_refuses_a_table_as_its_mask(world, table, tmp_path):
    result = run('clip', world, '-o', tmp_path/'o.gpkg', '--clip-layer', table)
    refuses_table(result)


def test_batch_convert_names_the_table_and_goes_on(tmp_path):
    source = tmp_path/'in'
    source.mkdir()
    gpd.GeoDataFrame({'a': [1]}, geometry=[Point(0, 0)], crs=4326).to_file(source/'a.gpkg', layer='points')
    pyogrio.write_dataframe(pd.DataFrame({'a': [1]}), source/'b.gpkg', layer='plain')
    result = run('convert', source, '--output', tmp_path/'out'/'x.geojson')
    assert result.returncode == 1 and NO_GEOMETRY in result.stderr and 'Traceback' not in result.stderr
    assert (tmp_path/'out'/'a.geojson').exists() and not (tmp_path/'out'/'b.geojson').exists()


def test_attribute_commands_still_read_tables(table):
    schema = run('inspect-data', table, 'schema')
    assert schema.returncode == 0 and 'none (table)' in schema.stdout and 'count' in schema.stdout, schema.stderr
    head = run('inspect-data', table, 'head', '-n', '1')
    assert head.returncode == 0 and '(first 1 rows)' in head.stdout, head.stderr
    values = run('inspect-data', table, 'values', 'site')
    assert values.returncode == 0 and 'Total: 2' in values.stdout, values.stderr


def test_describe_infers_table_types_and_says_it_sampled(table):
    card = json.loads(run('describe', table, '--json').stdout)
    layer = card['layers'][0]
    assert card['kind'] == 'vector' and layer['geometry_type'] is None and layer['field_types_sampled_bytes'] == 100000
    types = {field['name']: field['type'] for field in layer['fields']}
    assert types['site'] == 'string' and types['count'].startswith('int') and types['lon'] == 'float64'


def test_describe_leaves_other_vector_formats_unsampled(tmp_path):
    source = tmp_path/'v.gpkg'
    gpd.GeoDataFrame({'a': [1]}, geometry=[Point(0, 0)], crs=4326).to_file(source)
    assert 'field_types_sampled_bytes' not in json.loads(run('describe', source, '--json').stdout)['layers'][0]


def image(path, world=None):
    Image.fromarray(np.zeros((8, 10, 3), 'uint8')).save(path)
    if world:
        path.with_suffix(world[0]).write_text('\n'.join(map(str, world[1])) + '\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('name,sidecar', [('tile.jpg', '.jgw'), ('tile.png', '.pgw'), ('tile.jpeg', '.wld')])
def test_describe_reads_image_with_world_file(name, sidecar, tmp_path):
    # A world file names the centre of the first pixel; the affine names its outer corner.
    source = image(tmp_path/name, (sidecar, (15, 0, 0, -15, 7.5, 899992.5)))
    result = run('describe', source, '--json')
    assert result.returncode == 0 and result.stderr == '', result.stderr
    card = json.loads(result.stdout)
    grid = card['raster']
    assert card['kind'] == 'raster' and (grid['width'], grid['height'], grid['band_count']) == (10, 8, 3)
    assert grid['transform'] == [15.0, 0.0, 0.0, 0.0, -15.0, 900000.0] and grid['resolution'] == [15.0, 15.0]
    assert grid['extent'] == [0.0, 899880.0, 150.0, 900000.0] and grid['world_file'] == source.with_suffix(sidecar).name
    assert grid['crs'] is None
    missing = next(w for w in card['warnings'] if w['code'] == 'crs_missing')
    assert '未知' in missing['message'] and grid['world_file'] in missing['message']
    assert 'georeference_missing' not in [w['code'] for w in card['warnings']]


def test_describe_keeps_a_rotated_world_file_rotated(tmp_path):
    source = image(tmp_path/'r.jpg', ('.jgw', (10, 2, 3, -10, 105, 995)))
    grid = json.loads(run('describe', source, '--json').stdout)['raster']
    assert grid['transform'][:2] == [10.0, 3.0] and grid['transform'][3:5] == [2.0, -10.0] and grid['resolution'] is None


def test_describe_image_without_world_file_claims_no_place(tmp_path):
    result = run('describe', image(tmp_path/'scan.jpg'), '--json')
    assert result.returncode == 0 and result.stderr == '', result.stderr
    card = json.loads(result.stdout)
    grid = card['raster']
    assert card['kind'] == 'raster' and grid['width'] == 10
    assert grid['transform'] is None and grid['extent'] is None and grid['resolution'] is None and grid['world_file'] is None
    assert {'crs_missing', 'georeference_missing'} <= {w['code'] for w in card['warnings']}


def test_describe_gcp_raster_is_not_reported_as_unplaced(tmp_path):
    source = tmp_path/'scan.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=4, height=4, count=1, dtype='uint8') as dst:
        dst.write(np.zeros((1, 4, 4), 'uint8'))
        dst.gcps = ([GroundControlPoint(0, 0, 10, 20), GroundControlPoint(4, 4, 11, 19), GroundControlPoint(0, 4, 10, 19)], 'EPSG:4326')
    card = json.loads(run('describe', source, '--json').stdout)
    assert card['raster']['transform'] is None and 'georeference_missing' not in [w['code'] for w in card['warnings']]


def test_describe_tiff_is_still_placed_and_names_no_world_file(tmp_path):
    source = tmp_path/'r.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=4, height=3, count=1, dtype='uint8', nodata=0,
                       crs='EPSG:32650', transform=from_origin(500000, 3000000, 30, 30)) as dst:
        dst.write(np.ones((1, 3, 4), 'uint8'))
    card = json.loads(run('describe', source, '--json').stdout)
    assert card['raster']['transform'] == [30.0, 0.0, 500000.0, 0.0, -30.0, 3000000.0] and card['raster']['world_file'] is None
    assert card['warnings'] == []


def test_describe_reads_a_raster_only_geopackage_as_raster(tmp_path):
    source = tmp_path/'tiles.gpkg'
    with rasterio.open(source, 'w', driver='GPKG', width=256, height=256, count=3, dtype='uint8',
                       crs='EPSG:3857', transform=from_origin(0, 256, 1, 1)) as dst:
        dst.write(np.zeros((3, 256, 256), 'uint8'))
    card = json.loads(run('describe', source, '--json').stdout)
    assert card['kind'] == 'raster' and card['raster']['crs']['id'] == 'EPSG:3857'


def test_describe_names_both_attempts_when_nothing_opens_it(tmp_path):
    source = tmp_path/'notes.jpg'
    source.write_text('not an image', encoding='utf-8')
    result = run('describe', source, '--json')
    assert result.returncode == 1 and 'as vector' in result.stderr and 'as raster' in result.stderr and 'Traceback' not in result.stderr


def schema_rows(stdout):
    rows = {}
    for line in stdout.splitlines()[4:]:
        parts = line.split()
        if len(parts) >= 3:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def test_schema_nullable_sees_a_null_past_the_first_rows(tmp_path):
    source = tmp_path/'v.gpkg'
    frame = gpd.GeoDataFrame({'late': [1, 2, 3, 4, 5, None, 7], 'name': list('abcdefg'), 'full': range(7)},
                             geometry=[Point(i, i) for i in range(7)], crs=4326)
    frame['late'] = frame['late'].astype('Int64')
    frame.to_file(source, engine='pyogrio')
    result = run('inspect-data', source, 'schema')
    assert result.returncode == 0, result.stderr
    rows = schema_rows(result.stdout)
    assert rows['late'] == ('int64', 'True'), result.stdout
    assert rows['full'] == ('int64', 'False') and rows['name'] == ('object', 'False')


def test_schema_nullable_sees_a_null_in_a_later_chunk(tmp_path):
    source = tmp_path/'big.gpkg'
    frame = pd.DataFrame({'id': range(100_010), 'sparse': pd.array([1]*100_005 + [None]*5, dtype='Int64')})
    pyogrio.write_dataframe(frame, source, layer='big')
    rows = schema_rows(run('inspect-data', source, 'schema').stdout)
    assert rows['sparse'] == ('int64', 'True') and rows['id'] == ('int64', 'False')


def test_schema_samples_skip_nulls_at_the_start(tmp_path):
    source = tmp_path/'v.gpkg'
    gpd.GeoDataFrame({'late': [None]*6 + ['found']}, geometry=[Point(i, i) for i in range(7)], crs=4326).to_file(source)
    assert 'found' in run('inspect-data', source, 'schema').stdout


@pytest.fixture
def world(tmp_path):
    path = tmp_path/'world.gpkg'
    gpd.GeoDataFrame({'name': ['south', 'north']}, geometry=[box(-170, -80, -160, -70), box(10, 10, 20, 20)], crs=4326).to_file(path)
    return path


@pytest.mark.parametrize('form', [['--bbox', '-180,-90,0,0'], ['--bbox=-180,-90,0,0'], ['--bbox', '-180.5,-90,0,0']])
def test_clip_takes_a_box_that_starts_with_a_minus(form, world, tmp_path):
    out = tmp_path/'out.gpkg'
    result = run('clip', world, '-o', out, *form)
    assert result.returncode == 0, result.stderr
    assert list(gpd.read_file(out)['name']) == ['south']


def test_clip_negative_box_through_gis_py_and_other_arguments_stay_options(world, tmp_path):
    out = tmp_path/'out.gpkg'
    result = subprocess.run([sys.executable, str(SCRIPTS/'gis.py'), 'clip', str(world), '--bbox', '-180,-90,0,0', '-o', str(out)],
                            capture_output=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stderr
    # --bbox without a value is still argparse's error, and a malformed box is still refused.
    assert run('clip', world, '-o', tmp_path/'x.gpkg', '--bbox', '-o').returncode == 2
    malformed = run('clip', world, '-o', tmp_path/'y.gpkg', '--bbox', '-1,2')
    assert malformed.returncode != 0 and 'minx,miny,maxx,maxy' in malformed.stderr


def test_gis_py_runs_under_isolated_mode(tmp_path, table):
    listed = run('gis', 'list', '--json', flags=['-I', '-B'], cwd=tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert {row['name'] for row in json.loads(listed.stdout)['commands']} >= {'daily', 'describe'}
    assert run('gis', 'capabilities', '--json', flags=['-I', '-B'], cwd=tmp_path).returncode == 0
    forwarded = run('gis', 'describe', table, '--json', flags=['-I', '-B'], cwd=tmp_path)
    assert forwarded.returncode == 0, forwarded.stderr
    assert json.loads(forwarded.stdout)['kind'] == 'vector'


@pytest.mark.parametrize('term', ['csv', 'xy', 'normalize', 'point layer'])
def test_catalog_search_reaches_daily_normalize(term):
    rows = json.loads(run('gis', 'list', '--json', '--search', term).stdout)['commands']
    assert 'normalize' in next(row for row in rows if row['name'] == 'daily')['summary']


def test_the_way_out_named_in_the_error_works(table, tmp_path):
    params = tmp_path/'params.json'
    params.write_text(json.dumps({'schema_version': 1, 'id': 'site', 'x': 'lon', 'y': 'lat', 'crs': 'EPSG:4326'}), encoding='utf-8')
    result = run('daily', 'normalize', table, '--params', params, '--output', tmp_path/'bundle')
    assert result.returncode == 0, result.stderr
    points = gpd.read_file(tmp_path/'bundle'/'result.gpkg')
    assert list(points.geometry.geom_type) == ['Point', 'Point'] and points.crs.to_epsg() == 4326
