"""join.py --on: attribute joins of indicator tables to parcels, with parcel-number normalisation and full reports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPTS/'join.py'), *map(str, args)], capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout+result.stderr
    return result


def parcels(path):
    frame = gpd.GeoDataFrame({'parcel_id': ['01-01', '01-02', '01-03', '01-10', None], 'landuse': ['R2', 'C1', 'R3', 'G1', 'S1']},
                             geometry=[box(i*10, 0, i*10+10, 10) for i in range(5)])
    with pytest.warns(UserWarning):  # drawing coordinates: no CRS, as cad.py writes them
        frame.to_file(path)
    return path


def indicators(path):
    table = pd.DataFrame({'地块编号': ['1-1', '０１－０２', '01—03', '01-03', '09-09'],
                          '建筑量': ['12000', '8000', '3000', '2000', '500'], '容积率': ['2.5', '3.0', '1.5', '1.0', '0.8'],
                          '备注': ['', '商住', '', '', '']})
    table.to_excel(path, sheet_name='指标表', index=False)
    return path


def test_normalised_parcel_numbers_join_an_excel_table(tmp_path):
    left, right = parcels(tmp_path/'parcels.gpkg'), indicators(tmp_path/'指标.xlsx')
    cli('--left', left, '--right', right, '--right-sheet', '指标表', '--on', 'parcel_id=地块编号', '--normalize', 'id',
        '--fields', '建筑量,容积率', '--output', tmp_path/'first.gpkg', '--report-json', tmp_path/'first.json')
    out = gpd.read_file(tmp_path/'first.gpkg').set_index('parcel_id')
    assert out.loc['01-01', '建筑量'] == 12000 and out.loc['01-02', '容积率'] == 3.0
    assert out.loc['01-03', '建筑量'] == 3000 and out.loc['01-03', 'match_count'] == 2  # first of two rows
    assert pd.isna(out.loc['01-10', '建筑量']) and out.crs is None
    report = json.loads((tmp_path/'first.json').read_text(encoding='utf-8'))
    assert report['predicate'] == 'attribute' and report['key'] == {'left': 'parcel_id', 'right': '地块编号', 'normalize': 'id'}
    assert report['left_unmatched_keys'] == ['01-10'] and report['right_unmatched_keys'] == ['09-09']
    assert report['left_without_key'] == 1 and report['right_duplicate_keys'] == {'1-3': ['01—03', '01-03']}
    assert report['right_collisions'] == {'1-3': ['01-03', '01—03']}
    assert ['０１－０２', '1-2'] in report['right_normalised'] and ['01-01', '1-1'] in report['left_normalised']
    cli('--left', left, '--right', right, '--on', 'parcel_id=地块编号', '--normalize', 'id', '--fields', '建筑量',
        '--agg', 'sum', '--output', tmp_path/'sum.gpkg')
    assert gpd.read_file(tmp_path/'sum.gpkg').set_index('parcel_id').loc['01-03', '建筑量'] == 5000


def test_keys_as_given_and_a_gbk_csv_with_a_title_row(tmp_path):
    left = tmp_path/'parcels.gpkg'
    with pytest.warns(UserWarning):
        gpd.GeoDataFrame({'编号': ['007', '7']}, geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)]).to_file(left)
    right = tmp_path/'指标.csv'
    right.write_bytes('示例片区地块指标表\n编号,容积率\n007,2.5\n'.encode('gbk'))
    cli('--left', left, '--right', right, '--right-header-row', 2, '--right-encoding', 'gbk', '--on', '编号',
        '--output', tmp_path/'out.gpkg', '--report-json', tmp_path/'out.json')
    out = gpd.read_file(tmp_path/'out.gpkg').set_index('编号')
    assert out.loc['007', '容积率'] == 2.5 and pd.isna(out.loc['7', '容积率'])  # leading zeros kept without --normalize id
    assert json.loads((tmp_path/'out.json').read_text(encoding='utf-8'))['left_unmatched_keys'] == ['7']


def test_vector_on_the_right_and_refusals(tmp_path):
    left, right = parcels(tmp_path/'parcels.gpkg'), indicators(tmp_path/'指标.xlsx')
    other = tmp_path/'other.gpkg'
    gpd.GeoDataFrame({'地块编号': ['01-01'], 'x': [1]}, geometry=[box(500, 500, 501, 501)], crs='EPSG:4547').to_file(other)
    cli('--left', left, '--right', other, '--on', 'parcel_id=地块编号', '--output', tmp_path/'v.gpkg')
    assert gpd.read_file(tmp_path/'v.gpkg').set_index('parcel_id').loc['01-01', 'x'] == 1  # geometry and CRS play no part
    for extra, message in ((('--on', 'parcel_id=地块编号', '--how', 'right'), 'keep the left geometry'),
                           (('--on', 'parcel_id=地块编号', '--predicate', 'nearest'), 'do not apply'),
                           ((), 'has no geometry'),
                           (('--on', 'parcel_id=编号'), 'right key field not found')):
        assert message in cli('--left', left, '--right', right, *extra, '--output', tmp_path/'bad.gpkg', success=False).stderr
    assert not (tmp_path/'bad.gpkg').exists()


@pytest.mark.parametrize('raw,key', [('01-03', '1-3'), ('１－３', '1-3'), ('01—03', '1-3'), (' dy 01 ', 'DY1'), (103.0, '103'),
                                     ('03-03-02', '3-3-2'), (None, None), ('', None)])
def test_normalize_id(raw, key):
    from join import normalize_id
    assert normalize_id(raw) == key
