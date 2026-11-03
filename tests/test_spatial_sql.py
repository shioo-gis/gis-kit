"""Small real CLI regressions; scale acceptance is a separate explicit script."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

duckdb = pytest.importorskip('duckdb')
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('spatial_sql', SCRIPTS / 'spatial-sql.py')
sql = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sql)


@pytest.fixture
def con():
    with duckdb.connect() as c:
        try:
            c.execute('LOAD spatial')
        except duckdb.Error:
            pytest.skip('Optional Spatial extension not installed; never install in tests')
        yield c


def source(con, tmp_path, query=None):
    p = tmp_path / "source's.parquet"
    con.execute(f'''COPY ({query or "SELECT 1::BIGINT AS id, 'candidate' AS state, ST_Point(1,2)::GEOMETRY('EPSG:4326') AS geom"})
                    TO {sql.literal(p)} (FORMAT PARQUET)''')
    return p


def cli(tmp_path, p, query=None, *extra, geom=True):
    script = tmp_path / 'query.sql'
    script.write_text(query or f'SELECT * FROM read_parquet({sql.literal(p)}); -- final comment\n', encoding='utf-8')
    command = [sys.executable, str(SCRIPTS / 'gis.py'), '--trace', str(tmp_path/'trace'),
               'spatial-sql', str(script), '--input', str(p), '--output', str(tmp_path/'out'), '--id', 'id']
    if geom:
        command += ['--geometry', 'geom', '--crs', 'EPSG:4326', '--geometry-types', 'POINT']
    result = subprocess.run(command + list(extra), capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    return result


def test_roundtrip_trace_and_immutability(con, tmp_path):
    import pyarrow.parquet as pq
    p = source(con, tmp_path)
    before = sql.fingerprint(p)
    r = cli(tmp_path, p)
    assert r.returncode == 0, r.stderr
    out = tmp_path/'out/result.parquet'
    assert pq.read_table(out, columns=['id', 'state']).to_pydict() == {'id': [1], 'state': ['candidate']}
    assert sql.fingerprint(p) == before
    record = json.loads((tmp_path/'out/record.json').read_text(encoding='utf-8'))
    assert record['validation']['bbox'] == [1, 2, 1, 2]
    assert record['source_unchanged'] and record['status_scope'] == 'execution_only'
    trace = json.loads(next((tmp_path/'trace').glob('*.json')).read_text(encoding='utf-8'))
    assert trace['returncode'] == 0 and not trace['recipe_eligible']
    assert cli(tmp_path, p).returncode != 0  # no overwrite


@pytest.mark.parametrize('query,message', [
    ('SELECT 1; SELECT 2', 'exactly one SELECT'),
    ('CREATE TABLE x AS SELECT 1', 'exactly one SELECT'),
    ('SELECT id, id, geom FROM {source}', 'Duplicate output'),
    ('SELECT * FROM {source} UNION ALL SELECT * FROM {source}', 'unique and non-null'),
    ('SELECT NULL::BIGINT AS id, geom FROM {source}', 'unique and non-null'),
    ('SELECT id, geom::GEOMETRY AS geom FROM {source}', 'CRS missing'),
    ("SELECT id, ST_SetCRS(geom, 'EPSG:3857') AS geom FROM {source}", 'differs from expected'),
    ("SELECT id, ST_SetCRS('LINESTRING(0 0,1 1)'::GEOMETRY, 'EPSG:4326') AS geom FROM {source}", 'Unexpected geometry'),
])
def test_refusals(con, tmp_path, query, message):
    p = source(con, tmp_path)
    r = cli(tmp_path, p, query.format(source=f'read_parquet({sql.literal(p)})'))
    assert r.returncode != 0 and message in r.stderr, r.stderr
    assert not (tmp_path/'out').exists()
    assert not list(tmp_path.glob('.delivery-*'))
    assert not list(tmp_path.glob('*.publish-lock'))


@pytest.mark.parametrize('wkt,kind,typ', [(None, 'null', 'POINT'), ('POINT EMPTY', 'empty', 'POINT'),
    ('POLYGON((0 0,1 1,1 0,0 1,0 0))', 'invalid', 'POLYGON')])
def test_geometry_policy(con, tmp_path, wkt, kind, typ):
    value = 'NULL' if wkt is None else sql.literal(wkt)
    query = f"SELECT 1 AS id, {value}::GEOMETRY('EPSG:4326') AS geom"
    if wkt is None:
        query += " UNION ALL SELECT 2, ST_Point(1,2)::GEOMETRY('EPSG:4326')"
    p = source(con, tmp_path, query)
    r = cli(tmp_path, p, None, '--geometry-types', typ)
    assert r.returncode != 0 and kind in r.stderr, r.stderr
    r = cli(tmp_path, p, None, '--geometry-types', typ, '--allow-' + kind)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)[kind + '_geometry'] == 1


def test_empty_geometry_table(con, tmp_path):
    p = source(con, tmp_path)
    r = cli(tmp_path, p, f'SELECT * FROM read_parquet({sql.literal(p)}) WHERE false')
    # DuckDB 1.5.0 drops GeoParquet metadata on a zero-row geometry output.
    assert r.returncode != 0 and 'schema changed' in r.stderr
    assert not (tmp_path/'out').exists()


def test_native_aggregation_schema_and_no_python_rows(con, tmp_path):
    p = source(con, tmp_path)
    schema = tmp_path/'expected.json'
    schema.write_text(json.dumps({'id': 'VARCHAR', 'n': 'BIGINT'}), encoding='utf-8')
    r = cli(tmp_path, p, f"SELECT state AS id, count(*) AS n FROM read_parquet({sql.literal(p)}) GROUP BY state",
            '--schema', str(schema), geom=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)['schema'] == [['id', 'VARCHAR'], ['n', 'BIGINT']]
    text = (SCRIPTS/'spatial-sql.py').read_text(encoding='utf-8')
    assert 'import geopandas' not in text and '.fetchdf(' not in text and '.to_pandas(' not in text


def test_undeclared_read(con, tmp_path):
    p = source(con, tmp_path)
    other = tmp_path/'other.parquet'
    other.write_bytes(p.read_bytes())
    r = cli(tmp_path, p, f'SELECT * FROM read_parquet({sql.literal(other)})')
    assert r.returncode != 0 and 'disabled by configuration' in r.stderr


def test_schema_and_sidecar_refusal(con, tmp_path):
    p = source(con, tmp_path)
    schema = tmp_path/'expected.json'
    schema.write_text('{"id":"VARCHAR"}', encoding='utf-8')
    assert 'schema differs' in cli(tmp_path, p, None, '--schema', str(schema)).stderr
    Path(str(p)+'-wal').touch()
    assert 'SQLite sidecars' in cli(tmp_path, p).stderr


def test_mixed_crs_native_rejection(con, tmp_path):
    p = source(con, tmp_path)
    r = cli(tmp_path, p, f"SELECT id, geom FROM read_parquet({sql.literal(p)}) WHERE ST_Intersects(geom, ST_Point(1,2)::GEOMETRY('EPSG:3857'))")
    assert r.returncode != 0 and 'CRS' in r.stderr


def test_spatial_join_boundary_aggregation(con, tmp_path):
    p = source(con, tmp_path, "SELECT i AS id, ST_Point(i,0)::GEOMETRY('EPSG:4326') AS geom FROM range(3) t(i)")
    zones = tmp_path/'zones.parquet'
    con.execute(f"COPY (SELECT 10 AS zone, ST_MakeEnvelope(0,-1,1,1)::GEOMETRY('EPSG:4326') AS geom) TO {sql.literal(zones)} (FORMAT PARQUET)")
    query = f'''WITH points AS (SELECT * FROM read_parquet({sql.literal(p)}))
        SELECT z.zone AS id, count(*) AS n FROM points p
        JOIN read_parquet({sql.literal(zones)}) z ON ST_Intersects(p.geom,z.geom) GROUP BY z.zone; -- boundary included'''
    r = cli(tmp_path, p, query, '--input', str(zones), geom=False)
    assert r.returncode == 0, r.stderr
    assert con.execute(f"SELECT * FROM read_parquet({sql.literal(tmp_path/'out/result.parquet')})").fetchall() == [(10, 2)]
    assert 'SPATIAL_JOIN' in (tmp_path/'out/plan.txt').read_text(encoding='utf-8')


def test_source_mutation_blocks_publication(con, tmp_path, monkeypatch):
    from types import SimpleNamespace
    p = source(con, tmp_path)
    query = tmp_path/'query.sql'
    query.write_text(f'SELECT id FROM read_parquet({sql.literal(p)})', encoding='utf-8')
    original = sql.fingerprint
    calls = 0
    def changed(path):
        nonlocal calls
        if Path(path) == p:
            calls += 1
            if calls > 1:
                return 'changed'
        return original(path)
    monkeypatch.setattr(sql, 'fingerprint', changed)
    args = SimpleNamespace(input=[p], sql=query, schema=None, output=tmp_path/'out',
             id='id', geometry=None, crs=None, geometry_types=None, threads=1,
             memory_limit='64MB', max_temp_size='1GB', allow_null=False, allow_empty=False, allow_invalid=False)
    with pytest.raises(ValueError, match='changed during execution'):
        sql.run(args)
    assert not (tmp_path/'out').exists()


def test_empty_attribute_table(con, tmp_path):
    p = source(con, tmp_path)
    r = cli(tmp_path, p, f'SELECT id FROM read_parquet({sql.literal(p)}) WHERE false', geom=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)['rows'] == 0


def test_z_geometry_and_foot_units(con, tmp_path):
    import pyarrow.parquet as pq
    p = source(con, tmp_path, "SELECT 1 AS id, 'POINT Z(1 2 3)'::GEOMETRY('EPSG:2263') AS geom")
    r = cli(tmp_path, p, None, '--crs', 'EPSG:2263')
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info['axis_units'][0]['name'] == 'US survey foot'
    assert info['geoparquet_geometry_types'] == ['Point Z']
    import shapely
    g = shapely.from_wkb(pq.read_table(tmp_path/'out/result.parquet', columns=['geom'])['geom'][0].as_py())
    assert list(g.coords) == [(1, 2, 3)]
