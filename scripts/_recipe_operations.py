"""Fixed CLI adapters and sealed multi-artifact readback for sequential recipes."""
import json
from pathlib import Path
import sys

from _budget import check_vector
from _delivery import digest

# Shared with daily.py: additions cannot silently disappear from recipes.
DAILY_OPERATIONS = ('normalize', 'update', 'attribute_join', 'relations', 'overlay',
    'allocate', 'rules', 'geometry', 'grid', 'select', 'summarize', 'compare',
    'profile', 'distribution', 'grid_summary', 'time_slice', 'cleanup',
    'cleanup_adopt', 'cluster', 'morphology', 'weighted_summary', 'generalize',
    'match', 'match_split', 'match_transfer', 'edge_match')

RUNNERS = {
    'raster': ('raster.py', {'inputs'}, set()),
    'terrain': ('terrain.py', {'input'}, set()),
    'terrain-detail': ('terrain-detail.py', {'input'}, set()),
    'terrain-backend': ('terrain-backend.py', {'input', 'grass'}, {'grass'}),
    'terrain-cost': ('terrain-cost.py', {'input', 'friction', 'grass'}, {'grass'}),
    'network': ('network.py', {'input', 'origins', 'facilities'}, {'access-areas', 'barriers'}),
    'suitability': ('suitability.py', {'input'}, set()),
    'spatial-inference': ('spatial-inference.py', {'input'}, {'backend-python'}),
}
PATH_PARAMS = {'vector', 'reference', 'table', 'exclude', 'protected', 'scope', 'restriction', 'lines'}


def resolve(value, refs):
    if isinstance(value, dict):
        if set(value) == {'input_ref'}:
            if value['input_ref'] not in refs:
                raise ValueError('Unknown or forward input_ref')
            return str(refs[value['input_ref']])
        return {k: resolve(v, refs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, refs) for v in value]
    return value


def referenced(value):
    if isinstance(value, dict):
        if set(value) == {'input_ref'}:
            return {value['input_ref']}
        return set().union(*(referenced(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(referenced(v) for v in value))
    return set()


def check_paths(params):
    for key, value in params.items():
        if key == 'profile':
            if not isinstance(value, dict) or not isinstance(value.get('input'), dict) or set(value['input']) != {'input_ref'}:
                raise ValueError('profile.input must use input_ref')
        if key in PATH_PARAMS and (not isinstance(value, dict) or set(value) != {'input_ref'}):
            raise ValueError(f'{key} must use input_ref for cache tracking')


def command(step, params_file, refs, work, scripts):
    runner = step['runner']
    script, required, _ = RUNNERS[runner]
    supplied = step.get('files', {})
    if not required <= set(supplied) or not set(supplied) <= required | RUNNERS[runner][2]:
        raise ValueError(f'{runner} files require {sorted(required)}')
    files = resolve(supplied, refs)
    cmd = [sys.executable, str(scripts/script)]
    if runner in ('raster', 'terrain-backend'):
        cmd += [step['operation']]
    elif step['operation'] != 'run':
        raise ValueError('Adapter operation must be run')
    if runner == 'raster':
        if not isinstance(files['inputs'], list) or not files['inputs']:
            raise ValueError('Nonempty raster inputs list required')
        cmd += files.pop('inputs')
    else:
        cmd += [files.pop('input')]
    for key, value in files.items():
        cmd += ['--'+key, value]
    for key, value in step.get('backend', {}).items():
        cmd += ['--'+key, value]
    return cmd+['--params', str(params_file), '--output', str(work/'result')]


def inventory(path):
    result = {}
    for p in sorted(path.rglob('*')):
        if p.is_symlink():
            raise ValueError('Symlink artifact rejected')
        if p.is_file() and p.name != 'seal.json':
            result[str(p.relative_to(path))] = digest(p)
    return result


def validate(path, validation, artifact):
    import rasterio as rio
    import geopandas as gpd
    import numpy as np
    from pyproj import CRS
    from _grid import inspect
    record = json.loads((path/'record.json').read_text(encoding='utf-8'))
    # Some original raster records have no status. Absence is not acceptance;
    # an explicit null/unknown/hold is not a successful legacy record.
    if 'status' in record and record['status'] not in ('complete', 'candidate'):
        raise ValueError('Held or invalid adapter result')
    if 'allow_candidate' in validation and not isinstance(validation['allow_candidate'], bool):
        raise ValueError('allow_candidate must be a boolean')
    if record.get('status') == 'candidate' and validation.get('allow_candidate') is not True:
        raise ValueError('Candidate requires explicit allow_candidate')
    if not set(validation) <= {'allow_candidate', 'min_valid_pixels', 'crs', 'min_rows', 'max_rows', 'columns'}:
        raise ValueError('Unknown adapter validation')
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValueError('Select one top-level artifact filename')
    target = path/artifact
    if not target.is_file():
        raise ValueError('Selected artifact missing')
    for file in path.rglob('*'):
        if file.suffix == '.tif':
            with rio.open(file) as ds:
                inspect(ds)
                count = 0
                for _, win in ds.block_windows(1):
                    data = ds.read(window=win, masked=True)
                    if np.isinf(data.compressed()).any():
                        raise ValueError('Infinite cached pixels')
                    count += int(np.isfinite(data.compressed()).sum())
                if file == target:
                    if count < validation.get('min_valid_pixels', 0):
                        raise ValueError('Insufficient valid pixels')
                    if 'crs' in validation and ds.crs != CRS(validation['crs']):
                        raise ValueError('Required CRS differs')
        elif file.suffix == '.gpkg':
            for layer in gpd.list_layers(file).name:
                check_vector(file, layer)
                frame = gpd.read_file(file, layer=layer)
                if file == target:
                    if not validation.get('min_rows', 0) <= len(frame) <= validation.get('max_rows', float('inf')) or not set(validation.get('columns', [])) <= set(frame.columns):
                        raise ValueError('Vector validation failed')
                    if 'crs' in validation and frame.crs != CRS(validation['crs']):
                        raise ValueError('Required CRS differs')
        elif file.suffix == '.json':
            json.loads(file.read_text(encoding='utf-8'))
    if target.suffix != '.tif' and 'min_valid_pixels' in validation:
        raise ValueError('Pixel rule requires raster artifact')
    if target.suffix != '.gpkg' and set(validation) & {'min_rows', 'max_rows', 'columns'}:
        raise ValueError('Row rules require vector artifact')
    if target.suffix not in ('.gpkg', '.tif') and 'crs' in validation:
        raise ValueError('CRS rule requires spatial artifact')
    return target


def check_files(step):
    runner = step['runner']
    supplied = step.get('files', {})
    _, required, optional = RUNNERS[runner]
    if not required <= set(supplied) or not set(supplied) <= required | optional:
        raise ValueError('Invalid adapter files')
    backend=step.get('backend', {})
    if backend and (runner != 'spatial-inference' or set(backend)-{'backend-site-packages','backend-proj-data'} or any(not isinstance(v,str) or not Path(v).is_absolute() or not Path(v).is_dir() for v in backend.values())):
        raise ValueError('Invalid explicit inference backend directories')
    for key, value in supplied.items():
        values = value if key == 'inputs' else [value]
        if not isinstance(values, list) or not values or any(not isinstance(v, dict) or set(v) != {'input_ref'} for v in values):
            raise ValueError('Every file must use input_ref')


def reusable(step, params):
    # External runtimes may change modules without changing their launcher. Never
    # reuse these steps; the output is still sealed and descendants hash its content.
    return not step.get('backend') and step.get('runner') not in ('terrain-backend', 'terrain-cost') and not (step.get('runner') == 'spatial-inference' and params.get('operation') == 'infer')
