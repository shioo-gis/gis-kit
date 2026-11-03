"""Administrative chain of a site from a small synthetic GADM-style GeoPackage."""
import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'admin-chain.py'


@pytest.fixture
def levels(tmp_path):
    """Two countries; A has two regions; region A.1 has two districts (one with an unknown local name)."""
    path = tmp_path/'levels.gpkg'
    gpd.GeoDataFrame({'GID_0': ['A', 'B'], 'COUNTRY': ['Alpha', 'Beta']},
                     geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)], crs='EPSG:4326').to_file(path, layer='ADM_0')
    gpd.GeoDataFrame({'GID_0': ['A', 'A', 'B'], 'GID_1': ['A.1_1', 'A.2_1', 'B.1_1'], 'NAME_1': ['North', 'South', 'East'],
                      'NL_NAME_1': ['北部|北部', 'NA', 'NA']},
                     geometry=[box(0, 5, 10, 10), box(0, 0, 10, 5), box(10, 0, 20, 10)], crs='EPSG:4326').to_file(path, layer='ADM_1')
    gpd.GeoDataFrame({'GID_0': ['A', 'A', 'A'], 'GID_1': ['A.1_1', 'A.1_1', 'A.2_1'], 'GID_2': ['A.1.1_1', 'A.1.2_1', 'A.2.1_1'],
                      'NAME_2': ['West', 'Harbour', 'Plain'], 'NL_NAME_2': ['西区', 'NA', 'NA']},
                     geometry=[box(0, 5, 5, 10), box(5, 5, 10, 10), box(0, 0, 10, 5)], crs='EPSG:4326').to_file(path, layer='ADM_2')
    return path


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def read(directory, name):
    return json.loads((directory/name).read_text(encoding='utf-8'))


def test_point_chain_children_and_manifest(levels, tmp_path):
    out = tmp_path/'chain'
    result = run('--source', levels, '--point', '7,8', '--levels', '0,1,2', '--name-field', 'NL_NAME', '--site-label', 'Site', '--output', out)
    assert result.returncode == 0, result.stderr
    manifest = read(out, 'chain.json')
    assert manifest['contract'] == 'admin-chain' and manifest['schema_version'] == 1
    assert [(row['key'], row['gid'], row['name']) for row in manifest['levels']] == [('adm0', 'A', 'Alpha'), ('adm1', 'A.1_1', '北部'), ('adm2', 'A.1.2_1', 'Harbour')]
    assert manifest['levels'][1]['bounds'] == [0, 5, 10, 10]
    assert [row['children'] for row in manifest['levels']] == [{'key': 'adm1', 'count': 2}, {'key': 'adm2', 'count': 2}, None]
    assert manifest['site'] == {'kind': 'point', 'label': 'Site', 'point': [7, 8], 'bounds': [7, 8, 7, 8]}
    chain = read(out, 'chain.geojson')['features']
    assert [feature['properties']['key'] for feature in chain] == ['adm0', 'adm1', 'adm2', 'site']
    context = read(out, 'context.geojson')['features']
    assert [(f['properties']['parent_gid'], f['properties']['gid']) for f in context] == [('A', 'A.1_1'), ('A', 'A.2_1'), ('A.1_1', 'A.1.1_1'), ('A.1_1', 'A.1.2_1')]
    record = read(out, 'record.json')
    assert record['status'] == 'complete' and record['files']['chain']['sha256'] == manifest['files']['chain']['sha256']
    assert json.loads(result.stdout)['levels'] == ['adm0 Alpha', 'adm1 北部', 'adm2 Harbour']


def test_labels_area_site_and_repeatable_bytes(levels, tmp_path):
    site = tmp_path/'site.geojson'
    gpd.GeoDataFrame(geometry=[box(1, 6, 2, 7)], crs='EPSG:4326').to_crs('EPSG:3857').to_file(site)
    first, second = tmp_path/'one', tmp_path/'two'
    for out in (first, second):
        result = run('--source', levels, '--site', site, '--levels', '0,2', '--labels', 'Alpha land,West ward', '--output', out)
        assert result.returncode == 0, result.stderr
    manifest = read(first, 'chain.json')
    assert [row['label'] for row in manifest['levels']] == ['Alpha land', 'West ward']
    assert manifest['site']['kind'] == 'area' and manifest['levels'][0]['children'] == {'key': 'adm2', 'count': 3}
    for name in ('chain.geojson', 'context.geojson'):
        assert (first/name).read_bytes() == (second/name).read_bytes()


def test_failures_leave_no_output(levels, tmp_path):
    out = tmp_path/'chain'
    outside = run('--source', levels, '--point', '30,30', '--output', out)
    assert outside.returncode == 1 and '0 units of ADM_0 contain the site' in outside.stderr
    missing = run('--source', levels, '--point', '7,8', '--levels', '0,3', '--output', out)
    assert missing.returncode == 1 and 'Layer ADM_3 not in source' in missing.stderr
    labels = run('--source', levels, '--point', '7,8', '--labels', 'only one', '--output', out)
    assert labels.returncode == 1 and 'needs 3 names' in labels.stderr
    assert not out.exists()
    out.mkdir()
    exists = run('--source', levels, '--point', '7,8', '--output', out)
    assert exists.returncode == 1 and 'new directory' in exists.stderr
