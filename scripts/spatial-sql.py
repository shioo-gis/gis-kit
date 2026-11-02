#!/usr/bin/env python3
"""Execute one native DuckDB SELECT into validated (Geo)Parquet, without Python row materialization."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import time

from _delivery import bundle, fingerprint, write_json


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def schema(connection, query):
    return [(r[0], r[1]) for r in connection.execute('DESCRIBE ' + query).fetchall()]


def validate(connection, path, expected, args):
    """Only metadata and bounded aggregate rows cross the Python boundary."""
    from pyproj import CRS
    import pyarrow.parquet as pq

    source = f'read_parquet({literal(path)})'
    actual = schema(connection, 'SELECT * FROM ' + source)
    if actual != expected:
        raise ValueError(f'Parquet schema changed: {expected!r} -> {actual!r}')
    columns = dict(actual)
    if args.id not in columns:
        raise ValueError('Output ID column is missing')
    if columns[args.id] not in {'VARCHAR', 'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT',
                                'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'HUGEINT', 'UHUGEINT'}:
        raise ValueError('Stable ID must be a string or integer column')
    key = identifier(args.id)
    rows, nonnull, unique = connection.execute(
        f'SELECT count(*), count({key}), count(DISTINCT {key}) FROM {source}').fetchone()
    if rows != nonnull or rows != unique:
        raise ValueError('Output ID must be unique and non-null')
    result = {'rows': rows, 'schema': actual, 'id': args.id}
    geometries = [name for name, typ in actual if typ.startswith('GEOMETRY')]
    if geometries != ([args.geometry] if args.geometry else []):
        raise ValueError('Declare exactly one output geometry column, or return an attribute-only table')
    if not args.geometry:
        if args.crs or args.geometry_types:
            raise ValueError('CRS/geometry types require --geometry')
        return result
    if not args.crs or not args.geometry_types:
        raise ValueError('Geometry output requires --crs and --geometry-types')
    metadata = pq.read_schema(path).metadata or {}
    geo = json.loads(metadata.get(b'geo', b'{}'))
    if geo.get('primary_column') != args.geometry:
        raise ValueError('GeoParquet primary geometry column changed')
    info = geo['columns'][args.geometry]
    # GeoParquet absent CRS means CRS84; explicit null means unknown.
    declared = info.get('crs', 'OGC:CRS84')
    if declared is None or not CRS.from_user_input(declared).equals(CRS.from_user_input(args.crs)):
        raise ValueError('Output CRS missing or differs from expected CRS; no implicit assignment/reprojection')
    if info.get('encoding') != 'WKB':
        raise ValueError('Expected WKB GeoParquet encoding')
    geom = identifier(args.geometry)
    nulls, empty, invalid, xmin, ymin, xmax, ymax = connection.execute(f'''
        SELECT count(*) FILTER (WHERE {geom} IS NULL),
               count(*) FILTER (WHERE ST_IsEmpty({geom})),
               count(*) FILTER (WHERE NOT ST_IsValid({geom})),
               min(ST_XMin({geom})), min(ST_YMin({geom})),
               max(ST_XMax({geom})), max(ST_YMax({geom})) FROM {source}''').fetchone()
    types = [r[0] for r in connection.execute(
        f'SELECT DISTINCT ST_GeometryType({geom})::VARCHAR FROM {source} WHERE {geom} IS NOT NULL').fetchall()]
    if set(types) - {t.upper() for t in args.geometry_types}:
        raise ValueError(f'Unexpected geometry types: {types}')
    for name, count in [('null', nulls), ('empty', empty), ('invalid', invalid)]:
        if count and not getattr(args, 'allow_' + name):
            raise ValueError(f'Output has {count} {name} geometries; explicitly allow or fix in SQL')
    bbox = [xmin, ymin, xmax, ymax]
    if any(v is not None and not math.isfinite(v) for v in bbox):
        raise ValueError('Non-finite geometry extent')
    metadata_bbox = info.get('bbox')
    if metadata_bbox is not None and all(v is not None for v in bbox):
        xy_bbox = metadata_bbox if len(metadata_bbox) == 4 else [metadata_bbox[i] for i in (0, 1, 3, 4)]
        if xy_bbox != bbox:
            raise ValueError('GeoParquet bbox differs from actual geometry extent')
    crs = CRS.from_user_input(declared)
    result.update(geometry=args.geometry, crs=crs.to_json_dict(), geometry_types=sorted(types),
                  null_geometry=nulls, empty_geometry=empty, invalid_geometry=invalid,
                  geoparquet_geometry_types=info.get('geometry_types'),
                  bbox=bbox,
                  axis_units=[{'name': a.unit_name, 'to_si': a.unit_conversion_factor} for a in crs.axis_info],
                  measurement_semantics='SQL author responsibility; coordinate units are not automatically metres')
    return result


def run(args):
    import duckdb

    inputs = [p.resolve(strict=True) for p in args.input]
    if any(p.suffix.lower() not in {'.parquet', '.geoparquet'} for p in inputs):
        raise ValueError('This bounded adapter accepts local Parquet/GeoParquet sources only; use native GDAL/ST_Read for other formats')
    sql_path = args.sql.resolve(strict=True)
    tracked = list(dict.fromkeys([sql_path, *inputs, *([args.schema.resolve(strict=True)] if args.schema else [])]))
    before = {str(p): fingerprint(p) for p in tracked}
    implementation = {p.name: fingerprint(p) for p in sorted(Path(__file__).parent.glob('*.py'))}
    query = sql_path.read_text(encoding='utf-8')
    start = time.monotonic()
    with bundle(args.output) as stage:
        spill = stage / 'spill'
        spill.mkdir()
        with duckdb.connect(config={'memory_limit': args.memory_limit, 'threads': args.threads,
                                    'temp_directory': str(spill), 'max_temp_directory_size': args.max_temp_size,
                                    'autoinstall_known_extensions': False,
                                    'autoload_known_extensions': False}) as con:
            con.execute('LOAD spatial')  # Never install or silently fall back.
            version = con.execute('SELECT version()').fetchone()[0]
            extension = con.execute("SELECT extension_version FROM duckdb_extensions() WHERE extension_name='spatial'").fetchone()[0]
            statements = con.extract_statements(query)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError('SQL file must contain exactly one SELECT (WITH is supported)')
            # Native access controls reduce accidental undeclared reads, not an untrusted-SQL sandbox.
            con.execute('SET allowed_paths = ?', [[str(p) for p in inputs] + [str(stage / 'result.parquet')]])
            con.execute('SET enable_external_access = false')
            expected = schema(con, query)
            if any(typ == 'GEOMETRY' for _, typ in expected):
                raise ValueError('Output CRS missing; SQL must retain or explicitly establish a known CRS')
            names = [name.casefold() for name, _ in expected]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate output column names; assign explicit aliases')
            if args.schema:
                required = json.loads(args.schema.read_text(encoding='utf-8'))
                if dict(expected) != required:
                    raise ValueError(f'Query schema differs from --schema: {expected!r}')
            plan = '\n'.join(row[1] for row in con.execute('EXPLAIN ' + query).fetchall())
            (stage / 'plan.txt').write_text(plan + '\n',encoding='utf-8')
            con.execute('CREATE TEMP VIEW _gis_result AS ' + query)
            query_start = time.monotonic()
            con.execute(f'COPY (SELECT * FROM _gis_result) TO {literal(stage / "result.parquet")} (FORMAT PARQUET, COMPRESSION ZSTD)')
            query_seconds = time.monotonic() - query_start
            result = validate(con, stage / 'result.parquet', expected, args)
        shutil.rmtree(spill)
        after = {str(p): fingerprint(p) for p in tracked}
        if before != after or implementation != {p.name: fingerprint(p) for p in sorted(Path(__file__).parent.glob('*.py'))}:
            raise ValueError('Input, SQL or implementation changed during execution; output not published')
        (stage / 'query.sql').write_text(query + '\n',encoding='utf-8')
        write_json(stage / 'record.json', {
            'status': 'complete', 'status_scope': 'execution_only',
            'engine': {'duckdb': version, 'spatial': extension},
            'inputs': before, 'source_unchanged': True,
            'implementation': implementation,
            'parameters': {'memory_limit': args.memory_limit, 'threads': args.threads,
                           'max_temp_size': args.max_temp_size,
                           'geometry': args.geometry, 'crs': args.crs, 'geometry_types': args.geometry_types,
                           'allow_null': args.allow_null, 'allow_empty': args.allow_empty,
                           'allow_invalid': args.allow_invalid},
            'query_seconds': query_seconds, 'elapsed_seconds': time.monotonic() - start,
            'validation': result, 'output_sha256': fingerprint(stage / 'result.parquet')})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sql', type=Path, help='Trusted native SELECT file; use absolute local paths in SQL')
    parser.add_argument('--input', type=Path, action='append', required=True, help='Local Parquet/GeoParquet source, repeatable; fingerprinted before/after')
    parser.add_argument('--output', type=Path, required=True, help='New delivery directory')
    parser.add_argument('--id', required=True, help='Unique non-null integer/string result ID; SQL defines its meaning')
    parser.add_argument('--geometry', help='Single result geometry column; omit for attribute-only aggregation')
    parser.add_argument('--crs', help='Expected CRS, never assigned or transformed by this adapter')
    parser.add_argument('--geometry-types', nargs='+', help='Allowed OGC types, e.g. POINT MULTIPOINT')
    parser.add_argument('--schema', type=Path, help='Optional exact name -> DuckDB type JSON assertion')
    for kind in ('null', 'empty', 'invalid'):
        parser.add_argument('--allow-' + kind, action='store_true')
    parser.add_argument('--memory-limit', default='512MB', help='DuckDB buffer budget, NOT a process RSS cap')
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--max-temp-size', default='10GB')
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        parser.exit(1, f'ERROR: {error}\n')


if __name__ == '__main__':
    sys.exit(main())
