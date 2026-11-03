"""Real GPKG/CLI chain; optionally retain deliverables with GIS_EVIDENCE_DIR."""
from pathlib import Path
import json
import os
import subprocess
import sys

import geopandas as gpd
import pytest
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def test_vector_cli_chain(tmp_path):
    work = Path(os.environ.get('GIS_EVIDENCE_DIR', tmp_path))
    work.mkdir(parents=True, exist_ok=True)
    commands = []

    def run(script, *args):
        command = [sys.executable, str(SCRIPTS / script), *map(str, args)]
        result = subprocess.run(command, encoding='utf-8', errors='replace', capture_output=True,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        commands.append({'script': script, 'arguments': list(map(str, args)),
                         'returncode': result.returncode, 'stdout': result.stdout,
                         'stderr': result.stderr})
        (work / 'commands.json').write_text(json.dumps(commands, indent=2), encoding='utf-8')
        assert result.returncode == 0, result.stderr
        return result

    def write(name, data, geoms):
        path = work / (name + '.gpkg')
        gpd.GeoDataFrame(data, geometry=geoms, crs=32650).to_file(path, driver='GPKG')
        return path

    x, y = 500000, 3000000
    parcels = write('parcels', {'pid': ['p1', 'p2']},
                    [box(x,y,x+100,y+100), box(x+100,y,x+200,y+100)])
    buildings = write('buildings', {'bid': ['cross', 'inside'], 'floors': [2,3]},
                      [box(x+80,y+20,x+120,y+60), box(x+10,y+10,x+30,y+30)])
    boundary = write('boundary', {'region': ['study']}, [box(x,y,x+200,y+100)])
    clipped, joined, indicators, dissolved = [work / (n+'.gpkg') for n in
                                             ('clipped','joined','indicators','dissolved')]
    run('clip.py', buildings, '--clip-layer', boundary, '--output', clipped)
    run('join.py', '--left', clipped, '--right', boundary, '--fields', 'region',
        '--agg', 'first', '--output', joined)
    linked = gpd.read_file(joined)
    assert linked.bid.tolist() == ['cross', 'inside']
    assert linked.region.tolist() == ['study', 'study']
    assert linked.crs.to_epsg() == 32650
    assert linked.geometry.area.sum() == pytest.approx(2000)
    run('indicators.py', 'summary', '--buildings', joined, '--parcels', parcels,
        '--floors-field', 'floors', '--parcel-id', 'pid', '--output', indicators)
    result = gpd.read_file(indicators).set_index('pid')
    assert result.crs.to_epsg() == 32650
    assert result.FAR.to_dict() == pytest.approx({'p1': .28, 'p2': .16})
    assert result.density.to_dict() == pytest.approx({'p1': .12, 'p2': .08})
    assert result.parcel_area_m2.sum() == pytest.approx(20000)
    run('stats.py', 'spatial', joined)
    run('dissolve.py', indicators, '--agg', 'parcel_area_m2:sum', '--output', dissolved)
    merged = gpd.read_file(dissolved)
    assert len(merged) == 1
    assert merged.geometry.is_valid.all()
    assert merged.geometry.area.sum() == pytest.approx(20000)
    assert merged.parcel_area_m2.sum() == pytest.approx(20000)
    assert merged.crs.to_epsg() == 32650
    run('topo.py', dissolved, '--expected-coverage', boundary,
        '--fail-on-error', '--output', work/'topology.txt')
    assert 'Issues: 0 (0 errors)' in (work/'topology.txt').read_text(encoding='utf-8')
    run('topo.py', dissolved, '--expected-coverage', boundary, '--format', 'json',
        '--fail-on-error', '--output', work/'topology.json')
    assert json.loads((work/'topology.json').read_text(encoding='utf-8'))['summary']['error_count'] == 0


@pytest.mark.parametrize('script', ['test_large_data_paths.py', 'test_benchmark.py'])
def test_legacy_main_regression(script):
    """These historical checks expose main(), so pytest alone would skip them."""
    result = subprocess.run([sys.executable, str(Path(__file__).with_name(script))],
                            capture_output=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('chunk_size', [1, 3])
def test_chunked_merge_then_global_aggregation(tmp_path, chunk_size):
    """Chunked transport preserves exact global aggregation; block dissolve is distinct."""
    import pandas as pd
    from pandas.testing import assert_frame_equal
    values = [None, 2., 7., 4., None, 11., None]
    functions = ['first', 'sum', 'mean', 'count', 'min', 'max']
    frame = gpd.GeoDataFrame(
        {'zone': ['A','B','A',None,'B','A',None],
         **{name: values for name in functions}},
        geometry=[box(i*2, 0, i*2+1, 1) for i in range(7)], crs=32650)
    source = tmp_path/'all.gpkg'
    parts = [tmp_path/'part1.gpkg', tmp_path/'part2.gpkg']
    frame.to_file(source)
    frame.iloc[:3].to_file(parts[0])
    frame.iloc[3:].to_file(parts[1])

    def call(script, *arguments):
        result = subprocess.run([sys.executable, str(SCRIPTS/script), *map(str, arguments)],
                                capture_output=True, encoding='utf-8', errors='replace',
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        assert result.returncode == 0, result.stderr

    merged = tmp_path/'merged.gpkg'
    call('merge.py', '--inputs', *parts, '--chunk-size', chunk_size, '--output', merged)
    agg = ','.join(name+':'+name for name in functions)
    outputs = [tmp_path/'reference.gpkg', tmp_path/'actual.gpkg']
    for input_path, output_path in zip([source, merged], outputs):
        call('dissolve.py', input_path, '--by', 'zone', '--agg', agg, '--output', output_path)
    expected, actual = [gpd.read_file(path).sort_values('zone', na_position='last').reset_index(drop=True)
                        for path in outputs]
    assert_frame_equal(pd.DataFrame(expected.drop(columns='geometry')),
                       pd.DataFrame(actual.drop(columns='geometry')))
    assert actual.crs == expected.crs
    assert all(a.equals(b) for a,b in zip(actual.geometry, expected.geometry))
    assert actual.geometry.area.sum() == pytest.approx(7)
    assert actual.loc[actual.zone == 'A', 'sum'].iloc[0] == 18
    assert actual.loc[actual.zone == 'A', 'mean'].iloc[0] == 9
    assert actual.loc[actual.zone == 'A', 'count'].iloc[0] == 2
    assert actual.loc[actual.zone == 'A', 'first'].isna().all()
