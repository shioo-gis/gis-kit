"""Tools report what they did and do not corrupt data quietly: split, gpkg copy/import/sql/drop, field cast bool, fix --fix-encoding."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import Point

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'


def cli(tool, *args, success=True, cwd=None):
    result = subprocess.run([sys.executable, str(SCRIPTS/tool), *map(str, args)], capture_output=True, encoding='utf-8', errors='replace',
                            cwd=cwd, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout+result.stderr
    assert 'Traceback' not in result.stderr, result.stderr
    return result


def points(count=4, crs='EPSG:4326', **fields):
    return gpd.GeoDataFrame({'id': list(range(count)), **fields}, geometry=[Point(i, i) for i in range(count)], crs=crs)


def objects(path, like):
    with sqlite3.connect(path) as db:
        return [name for name, in db.execute('SELECT name FROM sqlite_master WHERE name LIKE ?', (like,))]


# split

@pytest.mark.parametrize('extension,fmt', [('gpkg', 'gpkg'), ('geojson', 'geojson'), ('shp', 'shp')])
def test_split_keeps_null_rows_and_reports_the_files_it_wrote(tmp_path, extension, fmt):
    source = tmp_path/'src.gpkg'
    points(k=['a', None, 'a', 'b']).to_file(source)
    out = tmp_path/'out'
    run = cli('split.py', source, '--by', 'k', '--output-dir', out, '--format', fmt)
    written = {path.stem: gpd.read_file(path) for path in out.glob(f'*.{extension}')}
    assert {name: sorted(frame.id) for name, frame in written.items()} == {'src_a': [0, 2], 'src_b': [3], 'src_NULL': [1]}
    assert written['src_NULL'].k.isna().all()
    assert '3 files created' in run.stdout and '(4 of 4 features)' in run.stdout and 'null value' in run.stdout


@pytest.mark.parametrize('values,share', [(['a/b', 'a_b', 'c'], "'a/b' and 'a_b' -> src_a_b.gpkg"),
                                          (['Road', 'ROAD', 'c'], "'ROAD' and 'Road' -> src_ROAD.gpkg"),  # One file on a case-insensitive volume.
                                          (['NULL', None, 'c'], "'NULL' and null -> src_NULL.gpkg")])
def test_split_refuses_values_that_would_share_a_file_and_writes_nothing(tmp_path, values, share):
    source = tmp_path/'src.gpkg'
    points(3, k=values).to_file(source)
    out = tmp_path/'out'
    run = cli('split.py', source, '--by', 'k', '--output-dir', out, success=False)
    assert 'nothing was written' in run.stderr and share in run.stderr
    assert not list(out.iterdir())


def test_split_replaces_existing_outputs_only_when_asked(tmp_path):
    source = tmp_path/'src.gpkg'
    points(k=['a', 'a', 'b', 'b']).to_file(source)
    out = tmp_path/'out'
    cli('split.py', source, '--by', 'k', '--output-dir', out)
    points(k=['a', 'b', 'b', 'c']).to_file(source)
    run = cli('split.py', source, '--by', 'k', '--output-dir', out, success=False)
    assert '--overwrite' in run.stderr and 'nothing was written' in run.stderr
    assert sorted(path.name for path in out.iterdir()) == ['src_a.gpkg', 'src_b.gpkg'] and len(gpd.read_file(out/'src_a.gpkg')) == 2
    cli('split.py', source, '--by', 'k', '--output-dir', out, '--overwrite')
    assert len(gpd.read_file(out/'src_a.gpkg')) == 1 and len(gpd.read_file(out/'src_c.gpkg')) == 1


def test_split_never_replaces_its_input(tmp_path):
    source = tmp_path/'x_a.gpkg'
    points(k=['a', 'a', 'b', 'b']).to_file(source)
    before = source.read_bytes()
    run = cli('split.py', source, '--by', 'k', '--output-dir', tmp_path, '--prefix', 'x', '--overwrite', success=False)
    assert 'replace the input' in run.stderr and source.read_bytes() == before and not (tmp_path/'x_b.gpkg').exists()


# gpkg copy, import and sql output

@pytest.mark.parametrize('command', ['copy', 'import'])
def test_writing_a_layer_twice_is_refused_and_leaves_the_rows_alone(tmp_path, command):
    source, target = tmp_path/'src.gpkg', tmp_path/'target.gpkg'
    points().to_file(source, layer='pts')
    cli('gpkg.py', command, '--source', source, '--target', target)
    run = cli('gpkg.py', command, '--source', source, '--target', target, success=False)
    assert "layer 'pts' already exists" in run.stderr and 'nothing was written' in run.stderr and '--target-layer' in run.stderr
    assert len(gpd.read_file(target, layer='pts')) == 4
    cli('gpkg.py', command, '--source', source, '--target', target, '--target-layer', 'pts2')
    assert sorted(name for name, _ in pyogrio.list_layers(target)) == ['pts', 'pts2']
    assert cli('gpkg.py', command, '--source', source, '--target', target, '--target-layer', 'PTS', success=False).stderr.count("'pts'") == 1


def test_sql_output_to_an_existing_layer_is_refused(tmp_path):
    source, target = tmp_path/'src.gpkg', tmp_path/'target.gpkg'
    points().to_file(source, layer='pts')
    query = ('sql', '--input', source, '--query', 'SELECT * FROM pts', '--output', target)
    cli('gpkg.py', *query)
    assert '--output-layer' in cli('gpkg.py', *query, success=False).stderr
    assert len(gpd.read_file(target, layer='sql_result')) == 4
    cli('gpkg.py', *query, '--output-layer', 'again')
    assert len(gpd.read_file(target, layer='again')) == 4


# gpkg drop

@pytest.mark.parametrize('layer', ['two', 'two words', 'o"dd'])
def test_drop_leaves_nothing_behind_and_the_name_can_be_used_again(tmp_path, layer):
    path, source = tmp_path/'data.gpkg', tmp_path/'src.gpkg'
    points().to_file(source, layer='fresh')
    points().to_file(path, layer='one')
    points(2).to_file(path, layer=layer, mode='a')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE layer_styles (id INTEGER PRIMARY KEY, f_table_name TEXT, styleName TEXT)')
        db.executemany('INSERT INTO layer_styles (f_table_name, styleName) VALUES (?, ?)', [(layer, 'gone'), ('one', 'kept')])
    assert objects(path, f'rtree_{layer}_%')
    cli('gpkg.py', 'drop', '--input', path, '--layer', layer)
    assert objects(path, f'%{layer}%') == []
    with sqlite3.connect(path) as db:
        for table in ('gpkg_contents', 'gpkg_geometry_columns', 'gpkg_extensions', 'gpkg_ogr_contents'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE table_name = ?', (layer,)).fetchone() == (0,), table
        assert db.execute('SELECT styleName FROM layer_styles').fetchall() == [('kept',)]
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert objects(path, 'rtree_one_%')
    kept = gpd.read_file(path, layer='one', bbox=(0.5, 0.5, 2.5, 2.5))
    assert sorted(kept.id) == [1, 2]  # The other layer's spatial index still answers.
    cli('gpkg.py', 'copy', '--source', source, '--source-layer', 'fresh', '--target', path, '--target-layer', layer)
    assert len(gpd.read_file(path, layer=layer, bbox=(0.5, 0.5, 2.5, 2.5))) == 2


def test_drop_handles_a_table_without_geometry(tmp_path):
    path = tmp_path/'data.gpkg'
    points().to_file(path, layer='one')
    pyogrio.write_dataframe(points().drop(columns='geometry'), path, layer='plain', append=True)
    cli('gpkg.py', 'drop', '--input', path, '--layer', 'plain')
    assert [name for name, _ in pyogrio.list_layers(path)] == ['one'] and objects(path, '%plain%') == []


# field cast bool

def cast(tmp_path, values, success=True):
    source, out = tmp_path/'src.gpkg', tmp_path/'out.gpkg'
    points(len(values), f=values).to_file(source)
    run = cli('field.py', 'cast', source, '--field', 'f', '--type', 'bool', '--output', out, success=success)
    return run, out


def stored(path):
    with sqlite3.connect(path) as db:
        return [value for value, in db.execute('SELECT f FROM out ORDER BY fid')]


@pytest.mark.parametrize('values,expected', [(['false', 'true', 'FALSE', ' True '], [0, 1, 0, 1]),
                                             (['no', 'yes', 'n', 'y', 'f', 't', '0', '1'], [0, 1, 0, 1, 0, 1, 0, 1]),
                                             ([0, 1, 1], [0, 1, 1]),
                                             ([0.0, 1.0], [0, 1]),
                                             ([True, False], [1, 0])])
def test_cast_bool_follows_what_the_value_says(tmp_path, values, expected):
    _, out = cast(tmp_path, values)
    assert stored(out) == expected
    assert pyogrio.read_info(out)['dtypes'][pyogrio.read_info(out)['fields'].tolist().index('f')] == 'bool'


def test_cast_bool_keeps_nulls_null(tmp_path):
    _, out = cast(tmp_path, ['false', None, 'true'])
    assert stored(out) == [0, None, 1]


@pytest.mark.parametrize('values,named', [(['true', 'maybe'], "'maybe'"), ([0, 1, 2], "'2'"), (['true', ''], "''")])
def test_cast_bool_refuses_anything_else_and_writes_nothing(tmp_path, values, named):
    run, out = cast(tmp_path, values, success=False)
    assert 'not booleans' in run.stderr and named in run.stderr and not out.exists()


# fix --fix-encoding

def garbled(text, encoding='gbk'):
    return text.encode(encoding).decode('latin-1')


def fix(tmp_path, *extra, **fields):
    source, out = tmp_path/'src.gpkg', tmp_path/'out.gpkg'
    points(len(next(iter(fields.values()))), **fields).to_file(source)
    run = cli('fix.py', source, '--fix-encoding', '--output', out, *extra)
    return run.stdout, gpd.read_file(out)


def test_fix_encoding_repairs_a_field_that_proves_garbled(tmp_path):
    report, back = fix(tmp_path, name=[garbled('道路名称'), garbled('中文'), 'abc', None])
    assert back.name.tolist()[:3] == ['道路名称', '中文', 'abc'] and back.name.isna().tolist()[3]
    assert 'Fixed encoding values: 2' in report and "Encoding fixed in 'name': 2 values" in report


@pytest.mark.parametrize('values', [['ÄÖ', 'éé'], ['Müller', 'Jäger', 'plain'], ['Größe']])
def test_fix_encoding_leaves_legitimate_latin_text_alone_and_says_so(tmp_path, values):
    report, back = fix(tmp_path, name=values)
    assert back.name.tolist() == values
    assert 'Fixed encoding values: 0' in report
    assert "Encoding left unchanged in 'name'" in report


def test_fix_encoding_explains_what_it_would_have_done(tmp_path):
    report, _ = fix(tmp_path, name=['ÄÖ'])
    assert "'ÄÖ' -> '闹'" in report and 'nothing shows they are garbled' in report


def test_fix_encoding_does_not_touch_a_field_with_values_that_cannot_be_garbled(tmp_path):
    values = [garbled('道路名称'), '河流']
    report, back = fix(tmp_path, name=values)
    assert back.name.tolist() == values and 'Fixed encoding values: 0' in report and "such as '河流'" in report


def test_fix_encoding_decides_field_by_field(tmp_path):
    report, back = fix(tmp_path, bad=[garbled('道路名称'), garbled('闹')], good=['ÄÖ', 'éé'], real=['道路', '河流'])
    assert back.bad.tolist() == ['道路名称', '闹']  # The second value proves nothing alone; the field does.
    assert back.good.tolist() == ['ÄÖ', 'éé'] and back.real.tolist() == ['道路', '河流']
    assert 'Fixed encoding values: 2' in report and "left unchanged in 'good'" in report and "'real'" not in report


def test_fix_encoding_honours_the_source_encoding(tmp_path):
    _, back = fix(tmp_path, '--source-encoding', 'utf-8', name=[garbled('é', 'utf-8'), 'abc'])
    assert back.name.tolist() == ['é', 'abc']


def test_fix_encoding_leaves_text_that_is_not_the_source_encoding(tmp_path):
    report, back = fix(tmp_path, '--source-encoding', 'utf-8', name=['café'])
    assert back.name.tolist() == ['café'] and 'Fixed encoding values: 0' in report
