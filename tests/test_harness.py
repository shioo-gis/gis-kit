"""Thin discovery, real invocation -> recipe replay, and semantic boundary checks."""
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import _execution_trace as trace
import _recipe_operations as adapters
from _delivery import digest, write_json
import recipe
import gis

spec = importlib.util.spec_from_file_location('semantic_check', SCRIPTS / 'semantic-check.py')
semantic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(semantic)


def cli(*args, cwd=None, site=True):
    return subprocess.run([sys.executable, *([] if site else ['-S']), str(SCRIPTS / 'gis.py'),
                           *map(str, args)], cwd=cwd, text=True, capture_output=True,
                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})


def dataset(path):
    gpd.GeoDataFrame({'id': ['a', 'b'], 'value': [0., None], 'review': ['candidate', 'hold']},
                     geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)], crs=32631).to_file(path)
    return path


@pytest.fixture
def recorded(tmp_path):
    source = dataset(tmp_path / 'source.gpkg')
    params = tmp_path / 'params.json'
    write_json(params, {'size_m': 5, 'origin': [0, 0]})
    result = cli('--trace', tmp_path / 'trace', 'daily', 'grid', source,
                 '--params', params, '--output', tmp_path / 'grid')
    assert result.returncode == 0, result.stderr
    path = next((tmp_path / 'trace').glob('*.json'))
    return source, params, path, json.loads(path.read_text())


def test_discovery_no_site_packages_and_help_passthrough():
    result = cli('list', '--json', '--search', 'network', site=False)
    assert result.returncode == 0, result.stderr
    commands = json.loads(result.stdout)['commands']
    network = next(c for c in commands if c['name'] == 'network')
    assert 'references/network-access.md' in network['references']
    assert all(not c['name'].startswith('_') for c in gis.catalog())
    native = subprocess.run([sys.executable, str(SCRIPTS / 'network.py'), '--help'], text=True, capture_output=True)
    forwarded = cli('network', '--help')
    assert forwarded.returncode == native.returncode == 0
    assert forwarded.stdout == native.stdout and forwarded.stderr == native.stderr
    assert '--access-areas' in forwarded.stdout


def test_capabilities_no_site_packages_and_version():
    result = cli('capabilities', '--json', site=False)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc['contract'] == 'capabilities' and doc['schema_version'] == 1 and doc['engine'] == 'gis-kit'
    changelog = (SCRIPTS.parent / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert re.search(r'^## v(\d+\.\d+\.\d+)', changelog, re.M).group(1) == gis.VERSION == doc['engine_version']
    assert {f['name'] for f in doc['features']} == set(gis.FEATURES)
    assert all(not f['available'] and f['reason'].startswith('not importable') for f in doc['features'])
    assert cli('capabilities').returncode == 2
    manifests = ('cad-inventory', 'cad-convert', 'cad-extract', 'cad-polygonize', 'cad-label', 'cad-xiangyuan', 'cad-overlay', 'cad-fill-compare',
                 'crs-assignment', 'vector-transform', 'vector-georef-apply', 'esri-package', 'qgis-package', 'arcgis-pro-package', 'style-read')
    assert doc['contracts'] == {'describe': {'write': [1]}, 'class-scheme': {'write': [1]}, 'display-raster': {'write': [1]}, 'tile-snapshot': {'write': [1]}, 'admin-chain': {'write': [1]}, 'display-vector': {'write': [1]},
                                **{name: {'write': [1]} for name in manifests},
                                'memory-budget': {'env': 'GIS_KIT_MEMORY_BUDGET', 'exit_code': 3, 'error': [1]}}
    assert 'describe' in doc['commands']


def test_describe_reads_headers_only(tmp_path):
    vector = dataset(tmp_path / 'v.gpkg')
    gpd.GeoDataFrame({'id': [1]}, geometry=[box(0, 0, 1, 1)]).to_file(tmp_path / 'nocrs.shp')
    raster = tmp_path / 'r.tif'
    with rasterio.open(raster, 'w', driver='GTiff', width=4, height=3, count=1, dtype='float32', nodata=float('nan'),
                       crs='EPSG:32650', transform=from_origin(500000, 3000000, 30, 30)) as dst:
        dst.write(np.zeros((1, 3, 4), 'float32'))
    card = json.loads(cli('describe', vector, '--json').stdout)
    layer = card['layers'][0]
    assert card['contract'] == 'describe' and card['schema_version'] == 1 and card['kind'] == 'vector'
    assert layer['feature_count'] == 2 and layer['crs']['id'] == 'EPSG:32631' and layer['crs']['units'] == 'metre'
    assert {'name': 'review', 'type': 'string'} in layer['fields'] and card['warnings'] == []
    nocrs = json.loads(cli('describe', tmp_path / 'nocrs.shp', '--json').stdout)
    assert nocrs['layers'][0]['crs'] is None and [w['code'] for w in nocrs['warnings']] == ['crs_missing']
    grid = json.loads(cli('describe', raster, '--json').stdout)
    assert grid['raster']['nodata'] == ['nan'] and grid['raster']['resolution'] == [30.0, 30.0] and grid['warnings'] == []
    assert grid['raster']['decoded_bytes'] == 4*3*4
    assert cli('describe', tmp_path / 'missing.gpkg', '--json').returncode == 2
    try:
        import jsonschema
    except ImportError:
        return
    schema = json.loads((Path(gis.__file__).resolve().parents[1] / 'references' / 'describe.schema.json').read_text(encoding='utf-8'))
    for doc in (card, nocrs, grid):
        jsonschema.validate(doc, schema)


@pytest.mark.parametrize('tool', ['../daily', '_grass_worker', '/tmp/daily.py', 'gis'])
def test_discovery_rejects_paths_and_private_workers(tool):
    assert cli(tool).returncode == 2


def test_trace_to_recipe_real_replay_binding_and_cache(tmp_path, recorded):
    source, params, first_path, first = recorded
    write_json(params, {})
    second = cli('--trace', tmp_path / 'trace', 'daily', 'profile', tmp_path / 'grid/result.gpkg',
                 '--params', params, '--output', tmp_path / 'profile')
    assert second.returncode == 0, second.stderr
    assert first['recipe_eligible'] is True and first['capture']['params']['size_m'] == 5
    assert first['returncode'] == 0 and 'stdout' not in first and 'environment' not in first
    draft = tmp_path / 'recipe.json'
    made = cli('recipe', 'from-trace', tmp_path / 'trace', '--output', draft)
    assert made.returncode == 0, made.stderr
    data = json.loads(draft.read_text())
    assert len(data['inputs']) == 1 and data['steps'][1]['input'] == 'step_001'
    assert data['steps'][0]['params']['size_m'] == 5  # not the subsequently edited params file
    before = digest(source)
    for name in ('one', 'two'):
        run = cli('recipe', draft, '--cache', tmp_path / 'cache', '--output', tmp_path / name, cwd=tmp_path)
        assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['cache_hits'] == 2
    expected = gpd.read_file(tmp_path / 'grid/result.gpkg')
    actual = gpd.read_file(tmp_path / 'one/default/step_001/result.gpkg')
    assert expected.geometry.equals(actual.geometry) and expected.crs == actual.crs
    dossier = json.loads((tmp_path / 'one/default/step_002/dossier.json').read_text())
    assert dossier['lineage_roles'] == ['step_001'] and dossier['upstream_statuses'] == {'step_001': 'complete'}
    assert json.loads((tmp_path / 'one/record.json').read_text())['status_scope'] == 'execution_only'
    other = dataset(tmp_path / 'other.gpkg')
    role = next(iter(data['inputs']))
    bound = recipe.execute(draft, tmp_path / 'bound', tmp_path / 'cache2', {role: str(other)})
    assert bound['steps'] == 2 and digest(source) == before
    # Draft creation cannot overwrite an existing recipe or either source.
    assert cli('recipe', 'from-trace', tmp_path / 'trace', '--output', draft).returncode != 0


def test_trace_failed_and_incomplete_require_explicit_selection(tmp_path, recorded):
    _, _, _, event = recorded
    failed = cli('--trace', tmp_path / 'trace', 'daily', 'no-such-operation')
    assert failed.returncode == 2
    with pytest.raises(ValueError, match='not recipe-eligible'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad.json')
    made = trace.distill(tmp_path / 'trace', tmp_path / 'selected.json', [event['id']])
    assert len(made['omitted']) == 1
    incomplete = dict(event, id='incomplete', execution_status='started', returncode=None, recipe_eligible=False)
    write_json(tmp_path / 'trace/incomplete.json', incomplete)
    with pytest.raises(ValueError, match='not recipe-eligible'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad2.json', ['incomplete'])
    assert not (tmp_path / 'bad.json').exists()


@pytest.mark.parametrize('target', ['input', 'output'])
def test_trace_rejects_changed_evidence(tmp_path, recorded, target):
    source, _, _, _ = recorded
    if target == 'input':
        source.write_bytes(b'changed')
    else:
        write_json(tmp_path / 'grid/added.json', {})
    with pytest.raises(ValueError, match='changed'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad.json')
    assert not (tmp_path / 'bad.json').exists()


def test_trace_overlapping_and_duplicate_identities_rejected(tmp_path, recorded):
    _, _, path, event = recorded
    duplicate = dict(event, id='second')
    write_json(path.with_name('second.json'), duplicate)
    with pytest.raises(ValueError, match='Overlapping'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad.json')
    write_json(path.with_name('second.json'), event)
    with pytest.raises(ValueError, match='Duplicate'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad2.json')


def test_trace_redacts_and_never_captures_output_or_environment(tmp_path, monkeypatch):
    secret = 'fixture-sensitive-value'
    monkeypatch.setattr(trace.subprocess, 'run', lambda command, env=None: SimpleNamespace(returncode=0))
    trace.run_traced(tmp_path / 'trace', 'geocode', ['--api-key', secret, '--url=https://example.invalid/?token=' + secret], ['unused'])
    text = next((tmp_path / 'trace').glob('*.json')).read_text()
    event = json.loads(text)
    assert secret not in text and trace.REDACTED in text
    assert 'capture' not in event and not event['recipe_eligible']
    assert not {'stdout', 'stderr', 'environment'} & event.keys()
    assert trace.redact({'nested': {'Authorization': secret}})['nested']['Authorization'] == trace.REDACTED


def test_trace_interruption_and_launch_failure(tmp_path, monkeypatch):
    def interrupt(command, env=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(trace.subprocess, 'run', interrupt)
    assert trace.run_traced(tmp_path / 'interrupt', 'inspect-data', [], ['unused']) == 130
    event = json.loads(next((tmp_path / 'interrupt').glob('*.json')).read_text())
    assert event['execution_status'] == 'interrupted' and not event['recipe_eligible']
    def fail(command, env=None):
        raise FileNotFoundError('missing test executable')
    monkeypatch.setattr(trace.subprocess, 'run', fail)
    with pytest.raises(FileNotFoundError):
        trace.run_traced(tmp_path / 'failure', 'inspect-data', [], ['unused'])
    assert json.loads(next((tmp_path / 'failure').glob('*.json')).read_text())['execution_status'] == 'launch_failed'


def test_parameters_drift_and_sensitive_snapshot_not_promotable(tmp_path, monkeypatch):
    source = dataset(tmp_path / 'source.gpkg')
    params = tmp_path / 'params.json'
    write_json(params, {'size_m': 5})
    cap = trace.prepare('daily', ['grid', str(source), '--params', str(params), '--output', str(tmp_path / 'new')])
    write_json(params, {'size_m': 10})
    with pytest.raises(ValueError, match='Parameters changed'):
        trace.finish_capture(cap)
    write_json(params, {'size_m': 5, 'password': 'fixture-sensitive-value'})
    with pytest.raises(ValueError, match='Credential'):
        trace.prepare('daily', ['grid', str(source), '--params', str(params), '--output', str(tmp_path / 'new')])


def test_raster_trace_recipe_preserves_absent_status(tmp_path):
    source = tmp_path / 'dem.tif'
    with rasterio.open(source, 'w', driver='GTiff', count=1, width=2, height=2, dtype='float64',
                       crs=3857, transform=from_origin(0, 20, 10, 10), nodata=np.nan) as ds:
        ds.write(np.array([[0, 2], [np.nan, 4.]]), 1)
    params = tmp_path / 'params.json'
    write_json(params, {})
    run = cli('--trace', tmp_path / 'trace', 'raster', 'copy', source, '--params', params, '--output', tmp_path / 'copy')
    assert run.returncode == 0, run.stderr
    trace.distill(tmp_path / 'trace', tmp_path / 'recipe.json')
    recipe.execute(tmp_path / 'recipe.json', tmp_path / 'replay', tmp_path / 'cache')
    dossier = json.loads((tmp_path / 'replay/default/step_001/dossier.json').read_text())
    assert dossier['source_status'] is None and dossier['source_status_present'] is False
    assert dossier['execution_status'] is None and dossier['process_status'] == 'complete'
    with rasterio.open(tmp_path / 'replay/default/step_001/result.tif') as ds:
        assert ds.read(1, masked=True)[0, 0] == 0 and ds.read(1, masked=True).mask[1, 0]


def test_adapter_network_files_and_daily_choices_are_shared(tmp_path):
    files = {k: {'input_ref': k} for k in ('input', 'origins', 'facilities', 'access-areas', 'barriers')}
    step = {'runner': 'network', 'operation': 'run', 'files': files}
    adapters.check_files(step)
    command = adapters.command(step, tmp_path / 'p.json', {k: tmp_path / k for k in files}, tmp_path, SCRIPTS)
    assert '--access-areas' in command and '--barriers' in command
    assert recipe.OPERATIONS == set(adapters.DAILY_OPERATIONS)
    assert {'morphology', 'match', 'generalize'} <= recipe.OPERATIONS


@pytest.mark.parametrize('state', [None, 'unknown', 'hold'])
def test_adapter_rejects_explicit_invalid_status(tmp_path, state):
    write_json(tmp_path / 'record.json', {'status': state})
    with pytest.raises(ValueError, match='Held'):
        adapters.validate(tmp_path, {}, 'result.tif')


@pytest.mark.parametrize('before,after', [('candidate', 'accepted'), ('hold', 'complete'),
                                       ('unknown', 0), (None, 0), ('', False), ('spatial_candidate', 'pass')])
def test_semantic_protected_value_changes(before, after):
    result = semantic.check_tables([{'id': 'a', 'v': before}], [{'id': 'a', 'v': after}], 'id', ['v'])
    assert not result['passed'] and result['protected_values_checked'] == 1


def test_semantic_identity_not_order_and_null_equivalence():
    before = [{'id': 'a', 'v': 'candidate'}, {'id': 'b', 'v': None}, {'id': 'c', 'v': 0}]
    after = [{'id': 'c', 'v': 0}, {'id': 'b', 'v': 'unknown'}, {'id': 'a', 'v': 'candidate'}]
    result = semantic.check_tables(before, after, 'id', ['v'])
    assert result['passed'] and result['protected_values_checked'] == 2
    assert semantic.check_tables([{'id': 'a', 'v': 0}], [{'id': 'a', 'v': 0}], 'id', ['v'])['status'] == 'no_protected_values'


@pytest.mark.parametrize('after', [[], [{'id': 'a'}]])
def test_semantic_deleted_row_or_field(after):
    result = semantic.check_tables([{'id': 'a', 'v': 'hold'}], after, 'id', ['v'])
    assert result['issues'][0]['issue'] == 'missing_downstream'


@pytest.mark.parametrize('rows', [[{'id': 'a'}, {'id': 'a'}], [{'id': None}], [{}]])
def test_semantic_invalid_identity(rows):
    with pytest.raises(ValueError, match='identity'):
        semantic.check_tables(rows, [], 'id', ['v'])


def test_semantic_missing_upstream_and_scalar_pointers():
    with pytest.raises(ValueError, match='Upstream'):
        semantic.check_tables([{'id': 'a'}], [], 'id', ['status'])
    assert semantic.pointer({'a/b': {'~key': ['hold']}}, '/a~1b/~0key/0') == 'hold'
    with pytest.raises(ValueError, match='escape'):
        semantic.pointer({}, '/bad~2')
    with pytest.raises(ValueError, match='scalar'):
        semantic.report([({'status': 'hold'}, {}, '/record')])


def test_semantic_cli_json_csv_and_gpkg(tmp_path):
    before, after = tmp_path / 'before.json', tmp_path / 'after.json'
    write_json(before, {'status': 'hold'})
    write_json(after, {'status': 'complete'})
    result = cli('semantic-check', before, after, '--pointers', '/status', site=False)
    assert result.returncode == 1 and json.loads(result.stdout)['issues']
    assert cli('semantic-check', before, after, '--pointers', '/missing', site=False).returncode == 2
    source = dataset(tmp_path / 'source.gpkg')
    data = gpd.read_file(source)
    data['review'] = 'accepted'
    data.to_file(tmp_path / 'changed.gpkg')
    result = cli('semantic-check', source, tmp_path / 'changed.gpkg', '--id', 'id', '--fields', 'review', 'value')
    assert result.returncode == 1 and json.loads(result.stdout)['protected_values_checked'] == 3
    (tmp_path / 'before.csv').write_text('id,v\na,\nb,candidate\n')
    (tmp_path / 'after.csv').write_text('id,v\nb,candidate\na,unknown\n')
    result = cli('semantic-check', tmp_path / 'before.csv', tmp_path / 'after.csv', '--id', 'id', '--fields', 'v', site=False)
    assert result.returncode == 0 and json.loads(result.stdout)['protected_values_checked'] == 2


def test_semantic_multilayer_requires_selection(tmp_path):
    path = dataset(tmp_path / 'layers.gpkg')
    gpd.read_file(path).to_file(path, layer='second')
    with pytest.raises(ValueError, match='Multiple'):
        semantic.table(path, ['id', 'value'])
    assert len(semantic.table(path, ['id', 'value'], 'second')) == 2


def test_p4_network_trace_candidate_review_and_recipe(tmp_path):
    from test_network_access import fixture
    edges, origins, facilities, parameters, areas, barriers = fixture()
    root = tmp_path / 'workspace with spaces'
    root.mkdir()
    paths = {}
    for name, data in [('input', edges), ('origins', origins), ('facilities', facilities),
                       ('access-areas', areas), ('barriers', barriers)]:
        paths[name] = root / (name + '.gpkg')
        data.to_file(paths[name])
    write_json(root / 'params.json', parameters)
    run = cli('--trace', root / 'trace', 'network', paths['input'].name,
              '--params', 'params.json', '--output', 'network',
              *[item for name in paths if name != 'input' for item in ('--' + name, paths[name].name)], cwd=root)
    assert run.returncode == 0, run.stderr
    event = json.loads(next((root / 'trace').glob('*.json')).read_text())
    assert event['recipe_eligible'] is True and event['capture']['source_status'] == 'candidate'
    with pytest.raises(ValueError, match='explicit --artifact'):
        trace.distill(root / 'trace', root / 'ambiguous.json')
    trace.distill(root / 'trace', root / 'recipe.json', artifacts={event['id']: 'routes.gpkg'})
    draft = json.loads((root / 'recipe.json').read_text())
    step = draft['steps'][0]
    assert step['validate'] == {'allow_candidate': False}
    assert {'access-areas', 'barriers'} <= step['files'].keys()
    with pytest.raises(ValueError, match='Candidate'):
        recipe.execute(root / 'recipe.json', root / 'held', root / 'cache')
    assert not (root / 'held').exists()
    # This is explicit test review permission, never granted by distillation.
    step['validate']['allow_candidate'] = True
    write_json(root / 'reviewed.json', draft)
    for name in ('replay', 'cached'):
        result = recipe.execute(root / 'reviewed.json', root / name, root / 'cache')
    assert result['cache_hits'] == 1
    first = json.loads((root / 'network/record.json').read_text())
    replay = json.loads((root / 'replay/default/step_001/record.json').read_text())
    assert replay['status'] == 'candidate' and replay['details'] == first['details']
    assert replay['details']['od'][0]['cost'] == pytest.approx(47)
    dossier = json.loads((root / 'replay/default/step_001/dossier.json').read_text())
    assert dossier['source_status'] == dossier['execution_status'] == 'candidate'
    assert dossier['process_status'] == 'complete'


def test_professional_capture_keeps_backend_executable_as_input(tmp_path):
    source = dataset(tmp_path / 'source.gpkg')
    backend = tmp_path / 'backend-python'
    backend.write_text('synthetic launcher identity, never executed')
    write_json(tmp_path / 'p.json', {'operation': 'infer'})
    site = tmp_path / 'site'
    site.mkdir()
    cap = trace.prepare('spatial-inference', [str(source), '--params', str(tmp_path / 'p.json'),
                        '--output', str(tmp_path / 'out'), '--backend-python', str(backend),
                        '--backend-site-packages', str(site)])
    assert cap['files']['backend-python'] == str(backend)
    assert str(backend) in cap['inputs'] and 'backend-python' not in cap['backend']
    assert cap['backend']['backend-site-packages'] == str(site)


def test_parameter_file_references_use_recorded_paths(tmp_path):
    source = dataset(tmp_path / 'source.gpkg')
    write_json(tmp_path / 'p.json', {'vector': str(source), 'profile': {'input': str(source)}})
    cap = trace.prepare('raster', ['zonal', str(source), '--params', str(tmp_path / 'p.json'),
                                 '--output', str(tmp_path / 'out')])
    converted = trace.map_parameter_paths(cap['params'], lambda path: {'input_ref': 'source'})
    assert converted == {'vector': {'input_ref': 'source'}, 'profile': {'input': {'input_ref': 'source'}}}
    assert cap['inputs'] == {str(source): digest(source)}


def test_daily_trace_cannot_select_record_as_result(tmp_path, recorded):
    event = recorded[-1]
    with pytest.raises(ValueError, match='native result'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad.json', artifacts={event['id']: 'record.json'})


def test_multiple_consumed_artifacts_not_silently_externalized(tmp_path, recorded):
    # Deliberately build local trace evidence to test the single-artifact boundary.
    _, _, path, first = recorded
    second_path = tmp_path / 'grid/other.gpkg'
    second_path.write_bytes((tmp_path / 'grid/result.gpkg').read_bytes())
    first['capture']['artifacts'] = adapters.inventory(tmp_path / 'grid')
    write_json(path, first)
    later = json.loads(json.dumps(first))
    later.update(id='later', started_ns=first['finished_ns'] + 1, finished_ns=first['finished_ns'] + 2)
    later['capture']['inputs'] = {str(p): digest(p) for p in [tmp_path / 'grid/result.gpkg', second_path]}
    write_json(path.with_name('later.json'), later)
    with pytest.raises(ValueError, match='multiple artifacts'):
        trace.distill(tmp_path / 'trace', tmp_path / 'bad.json')


@pytest.mark.parametrize('actor', [None, {}, {'model': ''}, {'model': 'test'},
                                  {'model': 'test', 'reasoning_effort': 1}])
def test_actor_attribution_requires_explicit_values(actor):
    from _model_actor import validate_actor
    with pytest.raises(ValueError):
        validate_actor(actor)


def test_actor_selection_is_upstream_and_legacy_declarations_still_valid():
    from _model_actor import POLICY, validate_actor
    validate_actor({'model': 'caller-selected-test-model', 'reasoning_effort': None})
    validate_actor({'model': 'gpt-6-astra', 'reasoning_effort': 'low'})
    assert POLICY['selection'] == 'caller' and POLICY['fallback'] == 'forbidden'


def test_trace_preparation_and_distillation_without_gis_imports(tmp_path, recorded):
    source, params, _, _ = recorded
    # Invoke the original script directly: gis.py launches a child interpreter,
    # so passing -S to the wrapper alone would not prove this boundary.
    made = subprocess.run([sys.executable, '-S', str(SCRIPTS / 'recipe.py'),
        'from-trace', str(tmp_path / 'trace'), '--output', str(tmp_path / 'draft.json')],
        text=True, capture_output=True)
    assert made.returncode == 0, made.stderr
    args = ['grid', str(source), '--params', str(params), '--output', str(tmp_path / 'unused')]
    code = (f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); '
            f'import _execution_trace as t; t.prepare("daily", {args!r}); '
            'assert not {"geopandas", "numpy", "rasterio", "pyogrio", "shapely"} & set(sys.modules)')
    prepared = subprocess.run([sys.executable, '-S', '-c', code], text=True, capture_output=True)
    assert prepared.returncode == 0, prepared.stderr


@pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity', '1e999'])
def test_trace_nonfinite_snapshot_still_runs_native_command(tmp_path, monkeypatch, literal):
    source = tmp_path / 'source.csv'
    source.write_text('id,value\na,1\n')
    params = tmp_path / 'params.json'
    params.write_text('{"extra": ' + literal + '}')
    calls = []
    monkeypatch.setattr(trace.subprocess, 'run',
                        lambda command, env=None: calls.append(command) or SimpleNamespace(returncode=0))
    args = ['grid', str(source), '--params', str(params), '--output', str(tmp_path / 'out')]
    assert trace.run_traced(tmp_path / 'trace', 'daily', args, ['native-command']) == 0
    event = json.loads(next((tmp_path / 'trace').glob('*.json')).read_text())
    assert calls == [['native-command']]
    assert 'capture' not in event and event['recipe_eligible'] is False


def test_trace_nested_consumed_artifact_is_not_externalized(tmp_path, recorded):
    _, params, path, first = recorded
    nested = tmp_path / 'grid/nested/result.gpkg'
    nested.parent.mkdir()
    nested.write_bytes((tmp_path / 'grid/result.gpkg').read_bytes())
    # Extend the local fixture bundle to represent an adapter with nested outputs.
    first['capture']['artifacts'] = adapters.inventory(tmp_path / 'grid')
    write_json(path, first)
    write_json(params, {})
    run = cli('--trace', tmp_path / 'trace', 'daily', 'profile', nested,
              '--params', params, '--output', tmp_path / 'profile')
    assert run.returncode == 0, run.stderr
    with pytest.raises(ValueError, match='top-level'):
        trace.distill(tmp_path / 'trace', tmp_path / 'invalid.json')
    assert not (tmp_path / 'invalid.json').exists()


def test_trace_relative_backend_directories_keep_original_cwd(tmp_path, monkeypatch):
    (tmp_path / 'source.csv').write_text('id,value\na,1\n')
    write_json(tmp_path / 'params.json', {'operation': 'infer'})
    (tmp_path / 'site').mkdir()
    monkeypatch.chdir(tmp_path)
    cap = trace.prepare('spatial-inference', ['source.csv', '--params', 'params.json',
        '--output', 'out', '--backend-site-packages', 'site'])
    assert cap['backend']['backend-site-packages'] == str(tmp_path / 'site')
    adapters.check_files({'runner': 'spatial-inference',
        'files': {'input': {'input_ref': 'source'}}, 'backend': cap['backend']})


@pytest.mark.parametrize('suffix', ['.csv', '.gpkg'])
@pytest.mark.parametrize('valid', [False, True])
def test_semantic_empty_table_still_checks_upstream_columns(tmp_path, valid, suffix):
    before, after = tmp_path / ('before' + suffix), tmp_path / ('after' + suffix)
    column = 'status' if valid else 'value'
    if suffix == '.csv':
        before.write_text('id,' + column + '\n')
        after.write_text('id,status\n')
    else:
        gpd.GeoDataFrame({'id': [], column: []}, geometry=[], crs=32631).to_file(before)
        gpd.GeoDataFrame({'id': [], 'status': []}, geometry=[], crs=32631).to_file(after)
    result = cli('semantic-check', before, after, '--id', 'id', '--fields', 'status')
    assert result.returncode == (0 if valid else 2), result.stderr
    if valid:
        assert json.loads(result.stdout)['status'] == 'no_protected_values'


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal', '.msk', '.aux.xml', '.ovr'])
def test_shared_fingerprint_preserves_sidecar_boundaries(tmp_path, suffix):
    import daily
    source = tmp_path / 'source.tif'
    source.write_bytes(b'fixture')
    Path(str(source) + suffix).write_bytes(b'sidecar')
    with pytest.raises(ValueError, match='sidecar'):
        recipe.fingerprint(source)
    if suffix.startswith('-'):
        with pytest.raises(ValueError, match='sidecar'):
            daily.fingerprint(source)
    else:
        assert daily.fingerprint(source) == digest(source)


def test_tools_print_utf8_whatever_the_locale(tmp_path):
    # An ASCII file name: under LC_ALL=C on Linux a Chinese one cannot reach GDAL at all, which is a different matter.
    vector = tmp_path / 'data.geojson'
    vector.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"地类":"林地"},'
                      '"geometry":{"type":"Point","coordinates":[120,30]}}]}', encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONIOENCODING'}
    run = subprocess.run([sys.executable, '-X', 'utf8=0', str(SCRIPTS / 'gis.py'), 'describe', str(vector), '--json'],
                         capture_output=True, env={**env, 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'})
    assert run.returncode == 0, run.stderr
    assert '地类' in run.stdout.decode('utf-8')
