#!/usr/bin/env python3
"""Version-1 sequential daily-operation recipes with validated content caches."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import _recipe_operations as adapters

from _delivery import bundle, digest, write_json, fingerprint as dataset_fingerprint

def fingerprint(path):
    return dataset_fingerprint(path, raster_sidecars=True)

SCRIPTS = Path(__file__).resolve().parent
OPERATIONS = set(adapters.DAILY_OPERATIONS)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('IDs must contain only letters, numbers, underscore or hyphen')
    return value


def validate_step(path, validation):
    from daily import read
    record = json.loads((path / 'record.json').read_text(encoding='utf-8'))
    artifact = path / record['artifact']['name']
    if artifact.parent != path or artifact.name not in ('result.gpkg', 'result.csv'):
        raise ValueError('Invalid artifact reference')
    if record['status'] != 'complete' or fingerprint(artifact) != record['artifact']['sha256']:
        raise ValueError('Step is held or artifact hash changed')
    frame = read(artifact)
    if len(frame) != record['counts']['output']:
        raise ValueError('Artifact row count changed')
    if not set(validation) <= {'min_rows', 'max_rows', 'columns', 'crs'}:
        raise ValueError('Unknown validation rule')
    if len(frame) < validation.get('min_rows', 0) or len(frame) > validation.get('max_rows', float('inf')):
        raise ValueError('Row count validation failed')
    if not set(validation.get('columns', [])) <= set(frame.columns):
        raise ValueError('Required columns missing')
    if 'crs' in validation:
        from pyproj import CRS
        if getattr(frame, 'crs', None) is None or frame.crs != CRS(validation['crs']):
            raise ValueError('Required CRS differs')
    return artifact


def execute(recipe, output, cache, bindings=None):
    data = json.loads(Path(recipe).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not data.get('steps'):
        raise ValueError('Expected nonempty version 1 recipe')
    if not set(data) <= {'schema_version', 'inputs', 'steps', 'variants'}:
        raise ValueError('Unknown recipe field')
    inputs = dict(data['inputs']); inputs.update(bindings or {})
    if set(inputs) != set(data['inputs']):
        raise ValueError('Unknown input role')
    inputs = {identifier(k): Path(v).resolve() if Path(v).is_absolute() else (Path(recipe).resolve().parent/v).resolve() for k,v in inputs.items()}
    initial = {k: fingerprint(p) for k,p in inputs.items()}
    ids = [identifier(s['id']) for s in data['steps']]
    if len(ids) != len(set(ids)) or set(ids) & set(inputs):
        raise ValueError('Step IDs must be unique and distinct from input roles')
    known = set(inputs)
    for step in data['steps']:
        if not set(step) <= {'id','operation','input','right','params','validate','runner','files','artifact','backend'}:
            raise ValueError('Unknown step field')
        if step.get('runner', 'daily') == 'daily':
            if step['operation'] not in OPERATIONS:
                raise ValueError('Unsupported daily operation')
        elif step['runner'] not in adapters.RUNNERS:
            raise ValueError('Unsupported runner')
        else:
            adapters.check_paths(step.get('params', {}))
            adapters.check_files(step)
            if not adapters.referenced(step.get('files', {})) <= known or not adapters.referenced(step.get('params', {})) <= known:
                raise ValueError('Inputs must reference roles or earlier steps')
        if any(step[r] not in known for r in ('input','right') if r in step):
            raise ValueError('Inputs must reference roles or earlier steps')
        known.add(step['id'])
    variants = data.get('variants', [{'id':'default','params':{}}])
    names = [identifier(v['id']) for v in variants]
    if not variants or len(names)!=len(set(names)):
        raise ValueError('Unique nonempty variants required')
    for variant in variants:
        if not set(variant) <= {'id','params'} or not set(variant.get('params',{})) <= set(ids):
            raise ValueError('Unknown variant step or field')
    implementation = {p.name: digest(p) for p in sorted(SCRIPTS.glob('*.py'))}
    from daily import environment as runtime_environment
    environment = {'python':sys.version,'runtime':runtime_environment(),
                   'packages':sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions() if d.metadata['Name'])}
    cache = Path(cache).resolve(); output=Path(output).absolute()
    if output==cache or output in cache.parents or cache in output.parents:
        raise ValueError('Cache and output must be disjoint')
    if any(output==p or output in p.parents or cache==p or cache in p.parents for p in inputs.values()):
        raise ValueError('Input datasets must be outside cache/output')
    cache.mkdir(parents=True, exist_ok=True)
    lock=cache/'.recipe-lock'; lock.mkdir()
    started=time.perf_counter()
    try:
        with bundle(output) as staging:
            executions=[]
            for variant in variants:
                refs=dict(inputs); upstream={k:initial[k] for k in inputs}; source_states={}
                folder=staging/variant['id']; folder.mkdir()
                for step in data['steps']:
                    params={**step.get('params',{}), **variant.get('params',{}).get(step['id'],{})}
                    validation=step.get('validate',{})
                    extended=step.get('runner','daily') != 'daily'
                    if extended:
                        adapters.check_paths(params)
                    roles=adapters.referenced(params) | adapters.referenced(step.get('files',{}))
                    if not roles <= set(refs):
                        raise ValueError('Unknown input reference')
                    resolved=adapters.resolve(params,refs)
                    validate=lambda path: adapters.validate(path,validation,step.get('artifact','result.tif')) if extended else validate_step(path,validation)
                    signature=key({'operation':step['operation'],'runner':step.get('runner','daily'),'backend':step.get('backend',{}),'files':step.get('files',{}),'artifact':step.get('artifact'),'params':params,'validation':validation,
                                   'references':{r:{'content':fingerprint(refs[r]),'upstream':upstream[r]} for r in sorted(roles)},
                                   'inputs':{r:{'upstream':upstream[step[r]],'content':fingerprint(refs[step[r]])} for r in ('input','right') if r in step},
                                   'implementation':implementation,'environment':environment})
                    entry=cache/signature
                    cached=False
                    if entry.exists() and not adapters.reusable(step,params):
                        shutil.rmtree(entry)
                    if entry.exists():
                        try:
                            seal=json.loads((entry/'seal.json').read_text(encoding='utf-8'))
                            if seal['signature'] != signature or seal['record_sha256']!=digest(entry/'record.json'):
                                raise ValueError('Cache record changed')
                            if seal.get('files') != adapters.inventory(entry):
                                raise ValueError('Cache bundle changed')
                            validate(entry); cached=True
                        except (OSError, ValueError, KeyError, TypeError):
                            shutil.rmtree(entry)
                    if not cached:
                        with tempfile.TemporaryDirectory(prefix='.step-',dir=cache) as temporary:
                            work=Path(temporary); write_json(work/'params.json',resolved)
                            command=adapters.command(step,work/'params.json',refs,work,SCRIPTS) if extended else [sys.executable,str(SCRIPTS/'daily.py'),step['operation'],str(refs[step['input']]),'--params',str(work/'params.json'),'--output',str(work/'result')]
                            if not extended and 'right' in step: command += ['--right',str(refs[step['right']])]
                            process=subprocess.run(command,encoding='utf-8',errors='replace',capture_output=True,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                            if process.returncode:
                                raise ValueError(f"Step {step['id']} failed: {process.stderr[-2000:]}")
                            validate(work/'result')
                            write_json(work/'result/seal.json',{'signature':signature,'record_sha256':digest(work/'result/record.json'),'files':adapters.inventory(work/'result')})
                            (work/'result').rename(entry)
                    target=folder/step['id']; shutil.copytree(entry,target)
                    refs[step['id']]=validate(target)
                    upstream[step['id']]=signature
                    step_record=json.loads((target/'record.json').read_text(encoding='utf-8'))
                    lineage=roles | {step[r] for r in ('input','right') if r in step}
                    # Keep the legacy (misnamed) source-status alias conservative:
                    # existing readers must not see candidate promoted to complete.
                    write_json(target/'dossier.json',{'process_status':'complete',
                        'execution_status':step_record.get('status'),
                        'source_status':step_record.get('status'), 'source_status_present':'status' in step_record,
                        'upstream_statuses':{r:source_states.get(r) for r in sorted(lineage)},'derived':extended,
                        'original_evidence_modified':False,'applicability':step_record.get('applicability',step_record.get('assumptions','See operation parameters and original record')),
                        'valid_scope':step_record.get('valid_scope',step_record.get('source_grid',step_record.get('source'))),
                        'modified_pixels':step_record.get('modified_pixels'),'interpolated_pixels':step_record.get('interpolated_pixels'),
                        'lineage_roles':sorted(lineage),'external_backend_cache_disabled':not adapters.reusable(step,params)})
                    source_states[step['id']]=step_record.get('status')
                    executions.append({'variant':variant['id'],'step':step['id'],'cache_hit':cached,'signature':signature,'artifact':str(refs[step['id']].relative_to(staging))})
            if initial != {k:fingerprint(p) for k,p in inputs.items()} or implementation != {p.name:digest(p) for p in sorted(SCRIPTS.glob('*.py'))}:
                raise ValueError('Input or implementation changed during execution')
            write_json(staging/'recipe.json',data)
            write_json(staging/'record.json',{'schema_version':1,'status':'complete','status_scope':'execution_only','inputs':{k:{'path':str(p),'sha256':initial[k]} for k,p in inputs.items()},'environment':environment,'implementation':implementation,'steps':executions,'elapsed_seconds':time.perf_counter()-started})
        return {'output':str(output),'steps':len(executions),'cache_hits':sum(s['cache_hit'] for s in executions)}
    finally:
        lock.rmdir()


def main():
    if len(sys.argv)>1 and sys.argv[1]=='from-trace':
        from _execution_trace import distill_main
        return distill_main(sys.argv[2:])
    p=argparse.ArgumentParser(description=__doc__,epilog='Create a reviewable draft: recipe.py from-trace --help'); p.add_argument('recipe'); p.add_argument('--output',required=True); p.add_argument('--cache',required=True); p.add_argument('--bindings',help='JSON object of replacement input roles')
    a=p.parse_args()
    try:
        print(json.dumps(execute(a.recipe,a.output,a.cache,json.loads(Path(a.bindings).read_text(encoding='utf-8')) if a.bindings else None)))
    except (ValueError,OSError,KeyError,TypeError) as e:p.exit(1,f'ERROR: {e}\n')

if __name__=='__main__':main()
