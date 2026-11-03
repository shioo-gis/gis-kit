"""Opt-in real TLC coordinate/zone benchmark, with streaming independent GEOS/PROJ recount.

Provide downloaded NYC Open Data gi8d-wdg5 CSV (:id, pickup_longitude,
pickup_latitude, passenger_count) and a GDAL-readable taxi zones dataset.
No network access or data download is performed. All artifacts go to a NEW --output.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from _delivery import fingerprint, write_json


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def worker(mode, work, limit):
    """One fresh process per measurement; ru_maxrss is lifetime peak including imports."""
    start = time.monotonic()
    if mode == 'sql':
        import importlib.util
        import pandas as pd
        def forbid_frame(*args, **kwargs):
            raise AssertionError('SQL path must not construct a pandas DataFrame')
        pd.DataFrame.__init__ = forbid_frame
        spec = importlib.util.spec_from_file_location('spatial_sql', SCRIPTS/'spatial-sql.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rc = module.main([str(work/'query.sql'), '--input', str(work.parent/'points.parquet'),
                          '--input', str(work.parent/'zones.parquet'), '--output', str(work/'delivery'),
                          '--id', 'id', '--geometry', 'geom', '--crs', 'OGC:CRS84',
                          '--geometry-types', 'MULTIPOLYGON', 'POLYGON', '--memory-limit', '64MB', '--threads', '2'])
        assert rc == 0
        assert 'geopandas' not in sys.modules
    else:
        import geopandas as gpd
        points = gpd.read_parquet(work.parent/'points.parquet', filters=[('row_index', '<=', limit)])
        zones = gpd.read_parquet(work.parent/'zones.parquet')
        joined = gpd.sjoin(points, zones, predicate='intersects')
        counts = joined.groupby('zone_id').agg(n=('id', 'size'), passengers=('passenger_count', 'sum'))
        write_json(work/'baseline-counts.json', {str(k): [int(v.n), int(v.passengers)] for k, v in counts.iterrows()})
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write_json(work/'metrics.json', {'seconds': time.monotonic()-start,
                'peak_rss_mib': peak / (1024**2 if sys.platform == 'darwin' else 1024),
                'mode': mode, 'limit': limit})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv', type=Path, required=True)
    p.add_argument('--zones', type=Path, required=True, help='Single-file GPKG; read-only GDAL source')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--sizes', type=int, nargs='+', default=[100000, 500000, 1500000])
    p.add_argument('--repeats', type=int, default=3)
    args = p.parse_args()
    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    before = {str(x.resolve()): fingerprint(x) for x in [args.csv, args.zones]}
    import duckdb
    import pyogrio
    import pyarrow.csv as csv
    import pyarrow.parquet as pq
    import numpy as np
    import shapely

    c = duckdb.connect(config={'threads': 2, 'memory_limit': '256MB', 'temp_directory': str(work/'spill')})
    c.execute('LOAD spatial')
    # Normalize only the small reference layer with one explicit PROJ implementation.
    # Different bundled PROJ databases can select different datum operations; do not
    # assume DuckDB ST_Transform and PyProj use an identical NAD83/WGS84 pipeline.
    zones = pyogrio.read_dataframe(args.zones)
    assert zones.crs.to_epsg() == 2263 and zones.geometry.is_valid.all()
    zones = zones.to_crs('OGC:CRS84')
    canonical = zones[['OBJECTID', 'geometry']].rename(columns={'OBJECTID':'zone_id'}).rename_geometry('geom')
    canonical.to_parquet(work/'zones.parquet', index=False)
    c.execute(f'''COPY (
        SELECT row_number() OVER () AS row_index, ":id" AS id, passenger_count,
            ST_Point(pickup_longitude, pickup_latitude)::GEOMETRY('OGC:CRS84') AS geom
        FROM read_csv({lit(args.csv.resolve())}, header=true,
             columns={{':id':'VARCHAR', 'pickup_longitude':'DOUBLE', 'pickup_latitude':'DOUBLE', 'passenger_count':'BIGINT'}})
        WHERE pickup_longitude BETWEEN -75 AND -72 AND pickup_latitude BETWEEN 40 AND 42
    ) TO {lit(work/'points.parquet')} (FORMAT PARQUET, COMPRESSION ZSTD)''')
    total = c.execute(f"SELECT count(*) FROM read_parquet({lit(work/'points.parquet')})").fetchone()[0]
    unique = c.execute(f"SELECT count(DISTINCT id), count(id) FROM read_parquet({lit(work/'points.parquet')})").fetchone()
    assert unique == (total, total), 'Source IDs must be non-null and unique'
    sizes = sorted(set(min(n, total) for n in args.sizes))
    input_hash = {str(x): fingerprint(x) for x in [work/'points.parquet', work/'zones.parquet']}
    # Independent full recount: raw CSV -> Shapely STRtree against explicitly normalized zones, bounded batches.
    zone_ids = zones.OBJECTID.to_numpy()
    tree = shapely.STRtree(zones.geometry.to_numpy())
    counts = {n: {} for n in sizes}
    raw_rows = accepted = 0
    independent_start = time.monotonic()
    for batch in csv.open_csv(args.csv, read_options=csv.ReadOptions(block_size=4*1024*1024)):
        raw_rows += batch.num_rows
        lon = batch.column('pickup_longitude').to_numpy(zero_copy_only=False)
        lat = batch.column('pickup_latitude').to_numpy(zero_copy_only=False)
        passengers = batch.column('passenger_count').to_numpy(zero_copy_only=False)
        valid = (lon >= -75) & (lon <= -72) & (lat >= 40) & (lat <= 42)
        x, y = lon[valid], lat[valid]
        values = np.nan_to_num(passengers[valid], nan=0).astype('int64')
        pairs = tree.query(shapely.points(x, y), predicate='intersects')
        global_rows = pairs[0] + accepted + 1
        for size in sizes:
            take = global_rows <= size
            ids = zone_ids[pairs[1][take]]
            ps = values[pairs[0][take]]
            for zone in np.unique(ids):
                previous = counts[size].setdefault(str(int(zone)), [0, 0])
                previous[0] += int(np.sum(ids == zone))
                previous[1] += int(ps[ids == zone].sum())
        accepted += len(x)
    assert accepted == total
    independent_seconds = time.monotonic() - independent_start
    write_json(work/'independent-counts.json', counts)
    records = []
    for size in sizes:
        for mode in ['sql', 'geopandas']:
            for repeat in range(args.repeats):
                trial = work/f'{mode}-{size}-{repeat}'
                trial.mkdir()
                query = f'''SELECT z.zone_id AS id, count(*)::BIGINT AS n,
                    sum(p.passenger_count)::BIGINT AS passengers, first(z.geom) AS geom
                    FROM read_parquet({lit(work/'points.parquet')}) p
                    JOIN read_parquet({lit(work/'zones.parquet')}) z ON ST_Intersects(p.geom, z.geom)
                    WHERE p.row_index <= {size} GROUP BY z.zone_id'''
                (trial/'query.sql').write_text(query, encoding='utf-8')
                result = subprocess.run([sys.executable, __file__, '_worker', mode, str(trial), str(size)],
                             capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
                (trial/'stdout.txt').write_text(result.stdout, encoding='utf-8')
                (trial/'stderr.txt').write_text(result.stderr, encoding='utf-8')
                assert result.returncode == 0, result.stderr
                metrics = json.loads((trial/'metrics.json').read_text(encoding='utf-8'))
                if mode == 'sql':
                    assert 'SPATIAL_JOIN' in (trial/'delivery/plan.txt').read_text(encoding='utf-8')
                    data = pq.read_table(trial/'delivery/result.parquet').to_pydict()
                    actual = {str(k): [n, v] for k, n, v in zip(data['id'], data['n'], data['passengers'])}
                    report = json.loads((trial/'delivery/record.json').read_text(encoding='utf-8'))
                    metrics['query_seconds'] = report['query_seconds']
                    # Independent geometry and CRS readback; exact original zone WKB.
                    output_geometry = shapely.from_wkb(data['geom'])
                    expected = {int(k): g for k, g in zip(zone_ids, zones.geometry)}
                    assert all(shapely.equals_exact(g, expected[k], 0) for k, g in zip(data['id'], output_geometry))
                    assert report['validation']['axis_units'][0]['name'] == 'degree'
                else:
                    actual = json.loads((trial/'baseline-counts.json').read_text(encoding='utf-8'))
                assert actual == counts[size], f'Independent aggregation mismatch: {mode}/{size}: ' + repr({k:(v,counts[size].get(k)) for k,v in actual.items() if v != counts[size].get(k)})
                records.append(metrics)
    assert input_hash == {name: fingerprint(name) for name in input_hash}
    assert before == {name: fingerprint(name) for name in before}
    c.close()
    write_json(work/'acceptance.json', {'source_fingerprints': before, 'processed_fingerprints': input_hash,
               'raw_rows': raw_rows, 'accepted_rows': accepted, 'excluded_coordinates': raw_rows-accepted,
               'predicate': 'ST_Intersects; overlapping zones may produce multiple matches',
               'independent_recount_seconds': independent_seconds, 'measurements': records,
               'source_unchanged': True, 'independent_exact_counts': True,
               'platform': sys.platform, 'duckdb': duckdb.__version__})
    print(json.dumps({'raw_rows':raw_rows, 'accepted_rows': accepted, 'measurements':records}, indent=2))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_worker':
        worker(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
    else:
        main()
