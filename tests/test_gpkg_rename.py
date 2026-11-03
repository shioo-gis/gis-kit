"""In-place GeoPackage layer rename via scripts/gpkg.py rename."""
from pathlib import Path
import hashlib
import os
import sqlite3
import subprocess
import sys

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from shapely.geometry import Point

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import gpkg  # noqa: E402


def _rename(path, old, new):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'gpkg.py'), 'rename', '--input', str(path), '--layer', old, '--new-name', new],
        encoding='utf-8', errors='replace', capture_output=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )


def _master(path):
    with sqlite3.connect(path) as conn:
        return conn.execute('SELECT type, name, tbl_name, sql FROM sqlite_master').fetchall()


def _sample(n=5):
    return gpd.GeoDataFrame(
        {'code': list(range(n)), 'label': [f'p{i}' for i in range(n)], 'value': [i * 0.5 for i in range(n)]},
        geometry=[Point(i, i) for i in range(n)], crs='EPSG:4326',
    )


def _make(path, layer, extra=True):
    _sample().to_file(path, layer=layer, engine='pyogrio')
    if extra:
        _sample(2).to_file(path, layer='other', engine='pyogrio', mode='a')
    # Leave a gap in the FIDs so a rewrite (which renumbers) would be detected.
    with sqlite3.connect(path) as conn:
        conn.execute(f'DELETE FROM {gpkg.quote_ident(layer)} WHERE fid = 2')


@pytest.mark.parametrize('old,new', [('old', 'new'), ('roads 2024', 'Roads "v2" o\'neil')])
def test_rename_in_place_keeps_rows_fids_types_and_index(tmp_path, old, new):
    path = tmp_path / 't.gpkg'
    _make(path, old)
    before = pyogrio.read_dataframe(path, layer=old, fid_as_index=True)
    info_before = pyogrio.read_info(path, layer=old)
    other_before = _master(path)
    other_before = [r for r in other_before if r[2] == 'other' or r[1].startswith('rtree_other')]

    result = _rename(path, old, new)
    assert result.returncode == 0, result.stderr

    assert [name for name, _ in pyogrio.list_layers(path)] == [new, 'other']
    after = pyogrio.read_dataframe(path, layer=new, fid_as_index=True)
    pd.testing.assert_frame_equal(before, after)
    assert list(after.index) == [1, 3, 4, 5]
    info_after = pyogrio.read_info(path, layer=new)
    for key in ('fields', 'dtypes'):
        assert list(info_after[key]) == list(info_before[key])
    for key in ('crs', 'geometry_type', 'geometry_name', 'fid_column', 'features'):
        assert info_after[key] == info_before[key]
    assert info_after['capabilities']['fast_spatial_filter']

    hit = pyogrio.read_dataframe(path, layer=new, bbox=(2.5, 2.5, 3.5, 3.5), fid_as_index=True)
    assert list(hit.index) == [4]

    master = _master(path)
    assert not [r for r in master if old.lower() in r[1].lower() or r[2] == old]
    rtree = f'rtree_{new}_geom'
    names = {r[1] for r in master}
    assert {rtree, f'{rtree}_rowid', f'{rtree}_node', f'{rtree}_parent'} <= names
    assert {f'{rtree}_insert', f'{rtree}_delete', f'trigger_insert_feature_count_{new}',
            f'trigger_delete_feature_count_{new}'} <= names
    assert all(r[2] == new for r in master if r[0] == 'trigger' and new in r[1])
    # Other layers' objects are untouched.
    assert [r for r in master if r[2] == 'other' or r[1].startswith('rtree_other')] == other_before

    with sqlite3.connect(path) as conn:
        for table in ('gpkg_contents', 'gpkg_geometry_columns', 'gpkg_extensions', 'gpkg_ogr_contents'):
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE table_name = ?', (old,)).fetchone()[0] == 0
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE table_name = ?', (new,)).fetchone()[0] == 1
        assert conn.execute('SELECT identifier FROM gpkg_contents WHERE table_name = ?', (new,)).fetchone()[0] == new
        assert conn.execute(f'SELECT count(*) FROM {gpkg.quote_ident(rtree)}').fetchone()[0] == 4
        assert conn.execute('SELECT seq FROM sqlite_sequence WHERE name = ?', (new,)).fetchone()[0] == 5
        # Recreated feature-count trigger targets the new name.
        conn.execute(f'DELETE FROM {gpkg.quote_ident(new)} WHERE fid = 1')
        assert conn.execute('SELECT feature_count FROM gpkg_ogr_contents WHERE table_name = ?', (new,)).fetchone()[0] == 3
        assert conn.execute(f'SELECT count(*) FROM {gpkg.quote_ident(rtree)}').fetchone()[0] == 3

    # GDAL can keep writing through the renamed layer and its index triggers.
    _sample(1).set_geometry([Point(10, 10)]).to_file(path, layer=new, engine='pyogrio', mode='a')
    hit = pyogrio.read_dataframe(path, layer=new, bbox=(9, 9, 11, 11), fid_as_index=True)
    assert list(hit.index) == [6]
    assert pyogrio.read_info(path, layer=new)['features'] == 4


def test_rename_updates_optional_metadata_tables(tmp_path):
    path = tmp_path / 't.gpkg'
    _make(path, 'old', extra=False)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE gpkg_data_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, name TEXT)')
        conn.execute("INSERT INTO gpkg_data_columns VALUES ('old', 'code', 'Code')")
        conn.execute('CREATE TABLE gpkg_metadata_reference (reference_scope TEXT, table_name TEXT, md_file_id INTEGER)')
        conn.execute("INSERT INTO gpkg_metadata_reference VALUES ('table', 'old', 1)")

    assert _rename(path, 'old', 'new').returncode == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT table_name FROM gpkg_data_columns').fetchall() == [('new',)]
        assert conn.execute('SELECT table_name FROM gpkg_metadata_reference').fetchall() == [('new',)]


def test_rename_attribute_table_without_geometry(tmp_path):
    path = tmp_path / 't.gpkg'
    _sample().to_file(path, layer='points', engine='pyogrio')
    pyogrio.write_dataframe(pd.DataFrame({'k': [1, 2], 'v': ['a', 'b']}), path, layer='attrs', driver='GPKG', append=True)
    before = pyogrio.read_dataframe(path, layer='attrs', fid_as_index=True, read_geometry=False)

    result = _rename(path, 'attrs', 'lookup')
    assert result.returncode == 0, result.stderr
    after = pyogrio.read_dataframe(path, layer='lookup', fid_as_index=True, read_geometry=False)
    pd.testing.assert_frame_equal(before, after)
    assert not [r for r in _master(path) if 'attrs' in r[1]]


@pytest.mark.parametrize('target', ['other', 'OTHER', 'rtree_old_geom_extra'])
def test_rename_refuses_existing_names_and_leaves_file_untouched(tmp_path, target):
    path = tmp_path / 't.gpkg'
    _make(path, 'old')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE rtree_old_geom_extra (x)')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _rename(path, 'old', target)
    assert result.returncode != 0
    assert 'exists' in result.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_rename_rolls_back_when_rtree_target_is_taken(tmp_path):
    path = tmp_path / 't.gpkg'
    _make(path, 'old')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE rtree_new_geom_node (x)')
    before = _master(path)
    result = _rename(path, 'old', 'new')
    assert result.returncode != 0
    assert 'rtree_new_geom_node' in result.stderr
    assert _master(path) == before


def test_rename_missing_layer_and_gdb_guard(tmp_path):
    path = tmp_path / 't.gpkg'
    _make(path, 'old')
    result = _rename(path, 'nope', 'new')
    assert result.returncode != 0 and "layer 'nope' not found" in result.stderr
    gdb = tmp_path / 'x.gdb'
    gdb.mkdir()
    result = _rename(gdb, 'old', 'new')
    assert result.returncode != 0 and 'File GDB' in result.stderr


def test_rewrite_trigger_sql_only_touches_tokens():
    sql = ('CREATE TRIGGER "t_old" AFTER DELETE ON old WHEN old."geom" NOT NULL BEGIN '
           "DELETE FROM [rtree_old_g] WHERE id = OLD.fid AND 'old x' <> 'old'; /* old */ END")
    out = gpkg._rewrite_trigger_sql(sql, {'old': 'n"w', 't_old': 't_new', 'rtree_old_g': 'rtree_n"w_g'}, {'old': 'n"w'})
    assert out == ('CREATE TRIGGER "t_new" AFTER DELETE ON "n""w" WHEN old."geom" NOT NULL BEGIN '
                   "DELETE FROM \"rtree_n\"\"w_g\" WHERE id = OLD.fid AND 'old x' <> 'n\"w'; /* old */ END")
