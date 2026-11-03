"""scripts/gpkg.py refuses File GDB writes with the real reason and lets GDB reads through."""
from pathlib import Path
import os
import subprocess
import sys

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import Point

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import gpkg  # noqa: E402

GDB_WRITABLE = 'w' in (pyogrio.list_drivers().get('OpenFileGDB') or '')


def _gpkg(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'gpkg.py'), *map(str, args)],
        encoding='utf-8', errors='replace', capture_output=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )


def _sample():
    return gpd.GeoDataFrame({'name': ['甲', '乙']}, geometry=[Point(0, 0), Point(1, 1)], crs='EPSG:4326')


@pytest.fixture
def sources(tmp_path):
    src = tmp_path / 'src.gpkg'
    _sample().to_file(src, layer='pts', engine='pyogrio')
    _sample().to_file(src, layer='more', engine='pyogrio', mode='a')
    gdb = None
    if GDB_WRITABLE:
        gdb = tmp_path / 'src.gdb'
        _sample().to_file(gdb, layer='pts', driver='OpenFileGDB', engine='pyogrio')
        _sample().to_file(gdb, layer='more', driver='OpenFileGDB', engine='pyogrio', mode='a')
    return tmp_path, src, gdb


WRITES = {
    'import': (lambda d, s: ['import', '--source', s, '--target', d / 'new.gdb'], 'import to File GDB', '--target out.gpkg'),
    'copy': (lambda d, s: ['copy', '--source', s, '--target', d / 'new.gdb'], 'copy to File GDB', '--target out.gpkg'),
    'export': (lambda d, s: ['export', '--input', s, '--layer', 'pts', '--output', d / 'new.gdb'], 'export to File GDB', '.gpkg, .shp'),
    'sql': (lambda d, s: ['sql', '--input', s, '--query', 'SELECT * FROM pts', '--output', d / 'new.gdb'], 'SQL output to File GDB', '--output result.gpkg'),
}


@pytest.mark.parametrize('command', WRITES)
def test_write_to_gdb_is_refused_with_the_real_reason(sources, command):
    tmp, src, _ = sources
    argv, phrase, hint = WRITES[command]
    result = _gpkg(*argv(tmp, src))
    assert result.returncode == 1
    assert f'Error: {phrase} is refused: gpkg.py does not write File GDB.' in result.stderr
    assert gpkg.gdal_gdb_support() in result.stderr
    assert hint in result.stderr
    assert 'read-only via GDAL/OGR' not in result.stderr
    assert ' to to ' not in result.stderr
    # export used to fall back to the GPKG driver and leave a GeoPackage named new.gdb.
    assert not (tmp / 'new.gdb').exists()


@pytest.mark.skipif(not GDB_WRITABLE, reason='needs a GDAL that can write a File GDB fixture')
@pytest.mark.parametrize('argv,phrase,hint', [
    (['drop', '--layer', 'pts'], 'layer deletion from File GDB', '--target out.gpkg'),
    (['rename', '--layer', 'pts', '--new-name', 'renamed'], 'layer rename in File GDB', '--target-layer NEW'),
])
def test_in_place_edit_of_gdb_is_refused_and_leaves_it_untouched(sources, argv, phrase, hint):
    _, _, gdb = sources
    result = _gpkg(argv[0], '--input', gdb, *argv[1:])
    assert result.returncode == 1
    assert f'Error: {phrase} is refused:' in result.stderr
    assert hint in result.stderr
    assert [name for name, _ in pyogrio.list_layers(gdb)] == ['pts', 'more']


@pytest.mark.parametrize('caps,expected', [
    ('rw', 'can write File GDB (OpenFileGDB driver), but'),
    ('r', 'can only read File GDB (writing through OpenFileGDB needs GDAL >= 3.6)'),
    (None, 'has no OpenFileGDB driver'),
])
def test_reason_follows_the_running_gdal(monkeypatch, caps, expected):
    drivers = {'GPKG': 'rw'} if caps is None else {'GPKG': 'rw', 'OpenFileGDB': caps}
    monkeypatch.setattr(gpkg.pyogrio, 'list_drivers', lambda: drivers)
    text = gpkg.gdal_gdb_support()
    assert text.startswith(f'GDAL {pyogrio.__gdal_version_string__} here ')
    assert expected in text
    with pytest.raises(SystemExit) as stop:
        gpkg.guard_gdb_write(Path('out.GDB'), 'copy to', 'Use a .gpkg.')
    assert str(stop.value) == f'Error: copy to File GDB is refused: gpkg.py does not write File GDB. {text} Use a .gpkg.'


def test_non_gdb_targets_pass_the_guard():
    for path in (Path('out.gpkg'), Path('gdb_out.gpkg'), Path('out.gdb.gpkg'), Path('roads.shp')):
        gpkg.guard_gdb_write(path, 'copy to', 'unused')


def test_writes_to_gpkg_still_work(sources):
    tmp, src, _ = sources
    out = tmp / 'out.gpkg'
    assert _gpkg('import', '--source', src, '--target', out).returncode == 0
    assert _gpkg('copy', '--source', src, '--source-layer', 'more', '--target', out).returncode == 0
    result = _gpkg('sql', '--input', src, '--query', "SELECT * FROM pts WHERE name = '甲'", '--output', out)
    assert result.returncode == 0, result.stderr
    assert [name for name, _ in pyogrio.list_layers(out)] == ['pts', 'more', 'sql_result']
    assert list(gpd.read_file(out, layer='sql_result').name) == ['甲']


@pytest.mark.skipif(not GDB_WRITABLE, reason='needs a GDAL that can write a File GDB fixture')
def test_reading_from_gdb_still_works(sources):
    tmp, _, gdb = sources
    out = tmp / 'from_gdb.gpkg'
    result = _gpkg('copy', '--source', gdb, '--source-layer', 'more', '--target', out, '--target-layer', 'renamed')
    assert result.returncode == 0, result.stderr
    assert _gpkg('export', '--input', gdb, '--layer', 'pts', '--output', tmp / 'pts.geojson').returncode == 0
    assert list(gpd.read_file(out, layer='renamed').name) == ['甲', '乙']
    assert len(gpd.read_file(tmp / 'pts.geojson')) == 2
    assert 'pts' in _gpkg('layers', gdb).stdout
