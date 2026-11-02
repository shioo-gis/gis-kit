"""Stage F: explicit unknowns, halo equality, instances, areas and sealed recipes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box

from test_analysis_extensions import tif, rcli, raster
from test_daily import frame

def write(frame, path):
    frame.to_file(path, driver="GPKG")
    return path
from test_raster_numeric import direct
from _delivery import digest, write_json
import _raster_cleanup as c
import _recipe_operations as adapters
import recipe


def decoded(path, name='result.tif'):
    with rio.open(path/name) as ds:
        return ds.read(1, masked=True).astype(float).filled(np.nan)


def test_holes_size_distance_scope_protection(tmp_path):
    a = np.ones((7, 9)); a[2, 2] = np.nan; a[3:5, 5:7] = np.nan; a[0, 4] = np.nan
    source = tif(tmp_path/'source.tif', a)
    p = dict(kind='categorical', method='nearest', max_hole_pixels=1, max_distance_m=1)
    root, record = direct(tmp_path, 'fill_holes', [source], p)
    result = decoded(root)
    assert result[2, 2] == 1 and np.isnan(result[3:5, 5:7]).all() and np.isnan(result[0, 4])
    assert record['modified_pixels'] == record['interpolated_pixels'] == 1
    assert decoded(root, 'modified.tif').sum() == 1
    for key in ('protected', 'scope'):
        mask = np.zeros(a.shape) if key == 'protected' else np.ones(a.shape)
        mask[2, 2] = 1 if key == 'protected' else 0
        f = tif(tmp_path/(key+'.tif'), mask)
        root, _ = direct(tmp_path, 'fill_holes', [source], {**p, key: str(f)})
        assert np.isnan(decoded(root)[2, 2])
    root, _ = direct(tmp_path, 'fill_holes', [source], {**p, 'max_distance_m': .5})
    assert np.isnan(decoded(root)[2, 2])


def test_hole_idw_no_cascade_and_kind(tmp_path):
    a = np.array([[0, 2, 0], [4, np.nan, 6], [0, 8, 0.]])
    source = tif(tmp_path/'source.tif', a)
    p = dict(kind='continuous', method='idw', max_hole_pixels=1, max_distance_m=1)
    root, _ = direct(tmp_path, 'fill_holes', [source], p)
    assert decoded(root)[1, 1] == 5
    with pytest.raises(ValueError, match='Categorical'):
        direct(tmp_path, 'fill_holes', [source], {**p, 'kind': 'categorical'})
    with pytest.raises(ValueError, match='max_donor_pairs'):
        direct(tmp_path, 'fill_holes', [source], {**p, 'max_donor_pairs': 1})


@pytest.mark.parametrize('stat', ['mean', 'sum', 'min', 'max', 'median', 'std', 'mode'])
@pytest.mark.parametrize('boundary', ['partial', 'pad', 'truncate'])
def test_focal_halo_full_and_independent(stat, boundary):
    a = np.arange(63, dtype=float).reshape(7, 9)%5; a[3, 3] = np.nan
    grid = SimpleNamespace(width=9, height=7)
    p = dict(kind='categorical' if stat == 'mode' else 'continuous', statistic=stat,
             size=3, boundary=boundary, min_coverage=.5)
    one = c.focal(a, grid, {**p, 'block_size': 1})
    full = c.focal(a, grid, {**p, 'block_size': 256})
    np.testing.assert_allclose(one, full, rtol=0, atol=1e-12, equal_nan=True)
    expected = np.full(a.shape, np.nan)
    for r, col in np.ndindex(a.shape):
        sub = a[max(0, r-1):r+2, max(0, col-1):col+2]
        denominator = sub.size if boundary == 'partial' else 9
        if np.isfinite(a[r,col]) and np.isfinite(sub).sum() >= .5*denominator and (boundary != 'truncate' or sub.size == 9):
            expected[r,col] = c.reduce_values(sub, stat)
    np.testing.assert_allclose(one, expected, rtol=0, atol=1e-12, equal_nan=True)


def test_focal_radius_missing_tie_and_guards(tmp_path):
    source = tif(tmp_path/'source.tif', [[0, 1, 0], [1, np.nan, 1], [0, 1, 0]], transform=from_origin(0, 6, 2, 2))
    p = dict(kind='categorical', statistic='mode', radius_m=2, min_coverage=0, derive_missing=True)
    root, _ = direct(tmp_path, 'focal', [source], p)
    assert decoded(root)[1, 1] == 1
    assert c.reduce_values(np.array([0,1]), 'mode') == 0
    with pytest.raises(ValueError):
        direct(tmp_path, 'focal', [source], {**p, 'radius_m': -1})
    with pytest.raises(ValueError):
        direct(tmp_path, 'focal', [source], {**p, 'min_coverage': 2})


@pytest.mark.parametrize('boundary,shape,last', [('partial', (2,2), 8), ('pad', (2,2), np.nan), ('truncate', (1,1), 2)])
def test_aggregate_partial_pad_truncate(tmp_path, boundary, shape, last):
    source = tif(tmp_path/'source.tif', np.arange(9).reshape(3,3))
    root, _ = direct(tmp_path, 'aggregate', [source], dict(factor=2, boundary=boundary, statistic='mean', kind='continuous'))
    data = decoded(root)
    assert data.shape == shape
    np.testing.assert_equal(data[-1,-1], last)
    with rio.open(root/'result.tif') as ds:
        assert ds.transform == from_origin(0,2,2,2)
    coverage = decoded(root, 'coverage.tif')
    assert coverage[-1,-1] == (.25 if boundary == 'pad' else 1)


def test_instances_connectivity_sieve_seed_and_protection(tmp_path):
    a = np.array([[1,0,0,0],[0,1,0,0],[0,0,0,1]], dtype=float)
    labels4, rows4 = c.regions(a, {'connectivity':4})
    labels8, rows8 = c.regions(a, {'connectivity':8})
    assert labels4[0,0] != labels4[1,1] and labels8[0,0] == labels8[1,1]
    assert labels8[2,3] != labels8[0,0]
    source=tif(tmp_path/'source.tif', a)
    protect=np.zeros(a.shape); protect[2,3]=1
    f=tif(tmp_path/'protected.tif',protect)
    root, record=direct(tmp_path,'sieve',[source],dict(min_area_m2=2, protected=str(f)))
    out=decoded(root); assert np.isnan(out[0,0]) and out[2,3] == 1
    root, _=direct(tmp_path,'sieve',[source],dict(min_area_m2=2,method='nearest',max_distance_m=1,protected=str(f)))
    assert decoded(root)[0,0] == 0 and decoded(root)[2,3] == 1
    root, _=direct(tmp_path,'seed_region',[source],dict(seeds=[[0,0]],connectivity=8))
    assert np.isfinite(decoded(root)).sum()==2
    with pytest.raises(ValueError):direct(tmp_path,'seed_region',[source],dict(seeds=[[-1,0]]))
    root, record=direct(tmp_path,'regions',[source],{})
    assert sum(x['area_m2'] for x in record['instances'])==12


def test_morphology_unknown_hole_and_protected_object(tmp_path):
    a=np.zeros((7,7)); a[2:5,2:5]=1; a[3,3]=np.nan
    source=tif(tmp_path/'source.tif',a); protected=np.zeros(a.shape); protected[3,1]=1
    f=tif(tmp_path/'protect.tif',protected)
    for method in ('dilate','erode','open','close'):
        root,_=direct(tmp_path,'morphology',[source],dict(category=1,background=0,method=method,size=3,protected=str(f)))
        result=decoded(root); assert np.isnan(result[3,3]) and result[3,1]==0
    with pytest.raises(ValueError):direct(tmp_path,'morphology',[source],dict(category=1,background=1,method='close'))


def test_class_area_fractional_hole_zero_unknown_and_feet(tmp_path):
    source=tif(tmp_path/'source.tif',[[0,1],[1,np.nan]],crs='EPSG:2263')
    zone=frame([box(.5,0,2,2)], id=['z']).set_crs(2263,allow_override=True)
    f=write(zone,tmp_path/'zones.gpkg')
    root, record=direct(tmp_path,'class_area',[source],dict(vector=str(f),id='id',method='fractional',block_size=1))
    row=record['zones'][0]; factor=(1200/3937)**2
    assert row['valid_area_m2']==pytest.approx(2*factor)
    assert row['raster_support_area_m2']==pytest.approx(3*factor)
    assert row['coverage']==pytest.approx(2/3)
    assert row['classes'][0]['category']==0 and row['classes'][0]['area_m2']==pytest.approx(.5*factor)
    root2, record2=direct(tmp_path,'class_area',[source],dict(vector=str(f),id='id',method='fractional',block_size=256))
    assert record['zones']==record2['zones']


def test_transition_combination_unknown_and_grid_rejection(tmp_path):
    a=tif(tmp_path/'a.tif',[[0,1,np.nan],[2,np.nan,0]])
    b=tif(tmp_path/'b.tif',[[1,1,2],[np.nan,np.nan,0]])
    root, record=direct(tmp_path,'transition',[a,b],{})
    assert record['joint_valid_area_m2']==3
    assert record['before_only_area_m2']==record['after_only_area_m2']==record['both_unknown_area_m2']==1
    assert sum(x['area_m2'] for x in record['codebook'])==3
    np.testing.assert_equal(decoded(root,'change.tif'), [[1,0,np.nan],[np.nan,np.nan,0]])
    root2, rec=direct(tmp_path,'class_combine',[a,b,a],{})
    assert len(rec['codebook'])==3
    bad=tif(tmp_path/'bad.tif',[[0,0,0],[0,0,0]],transform=from_origin(.5,2,1,1))
    with pytest.raises(ValueError,match='Grids'):direct(tmp_path,'transition',[a,bad],{})
    with pytest.raises(ValueError,match='two dates'):direct(tmp_path,'transition',[a],{})


def test_zonal_fill_weighting_overlap_unknown(tmp_path):
    a=tif(tmp_path/'a.tif',[[0,2],[4,np.nan]])
    z=write(frame([box(.5,0,2,2)],id=['z']),tmp_path/'zones.gpkg')
    root,record=direct(tmp_path,'zonal_fill',[a],dict(vector=str(z),id='id',method='fractional'))
    assert record['zones'][0]['value']==2
    np.testing.assert_equal(decoded(root),[[2,2],[2,np.nan]])
    z2=write(frame([box(0,0,1,2),box(.5,0,2,2)],id=['a','b']),tmp_path/'overlap.gpkg')
    with pytest.raises(ValueError,match='Overlapping'):direct(tmp_path,'zonal_fill',[a],dict(vector=str(z2),id='id',method='fractional'))


def test_cli_failure_and_source_immutable(tmp_path,monkeypatch):
    source=tif(tmp_path/'source.tif',[[1,1,1],[1,np.nan,1],[1,1,1]])
    h=digest(source); p=dict(kind='continuous',method='nearest',max_hole_pixels=1,max_distance_m=1)
    record=rcli(tmp_path,'good','fill_holes',[source],p)
    assert record['interpolated_pixels']==1 and digest(source)==h
    rcli(tmp_path,'bad','fill_holes',[source],{**p,'max_distance_m':-1},success=False)
    import argparse
    params=tmp_path/'params.json'; write_json(params,p)
    original=c.n.write
    def broken(*args,**kwargs):
        result=original(*args,**kwargs)
        raise ValueError('injected readback failure')
    monkeypatch.setattr(c.n,'write',broken)
    with pytest.raises(ValueError,match='injected'):
        raster.execute(argparse.Namespace(input=[str(source)],params=str(params),output=str(tmp_path/'failed'),operation='fill_holes'))
    assert not (tmp_path/'failed').exists() and digest(source)==h


def ref(name):return {'input_ref':name}


def fixture_recipe(tmp_path):
    source=tif(tmp_path/'source.tif',np.arange(25,dtype=float).reshape(5,5))
    zones=write(frame([box(0,0,5,2)],id=['z']),tmp_path/'zones.gpkg')
    data=dict(schema_version=1, inputs=dict(source=str(source),zones=str(zones)), steps=[
        dict(id='clean',runner='raster',operation='focal',files=dict(inputs=[ref('source')]),params=dict(kind='continuous',statistic='mean',size=3,min_coverage=.5)),
        dict(id='classes',runner='raster',operation='reclassify',files=dict(inputs=[ref('clean')]),params=dict(rules=[dict(min=0,max=100,output=1)])),
        dict(id='area',runner='raster',operation='class_area',files=dict(inputs=[ref('classes')]),params=dict(vector=ref('zones'),id='id',method='fractional'),artifact='table.json')])
    write_json(tmp_path/'recipe.json',data)
    return source,zones,data


def test_recipe_raster_cache_auxiliary_corruption_source_params(tmp_path):
    source,zones,data=fixture_recipe(tmp_path)
    path=tmp_path/'recipe.json'; cache=tmp_path/'cache'
    assert recipe.execute(path,tmp_path/'one',cache)['cache_hits']==0
    assert recipe.execute(path,tmp_path/'two',cache)['cache_hits']==3
    entries=json.loads((tmp_path/'one/record.json').read_text())['steps']
    (cache/entries[0]['signature']/'modified.tif').write_bytes(b'corrupt')
    assert recipe.execute(path,tmp_path/'three',cache)['cache_hits']==2
    data['steps'][0]['params']['size']=1; write_json(path,data)
    assert recipe.execute(path,tmp_path/'four',cache)['cache_hits']==0
    other=tif(tmp_path/'other.tif',np.arange(25,dtype=float).reshape(5,5)+2)
    assert recipe.execute(path,tmp_path/'five',cache,{'source':str(other)})['cache_hits']==0
    with rio.open(source,'r+') as ds:ds.write(np.ones((5,5)),1)
    assert recipe.execute(path,tmp_path/'six',cache)['cache_hits']==0


def test_recipe_terrain_candidate_failure_resume_and_untracked(tmp_path):
    source,zones,data=fixture_recipe(tmp_path)
    data['steps'] += [dict(id='terrain',runner='terrain',operation='run',files=dict(input=ref('source')),
        params=dict(vertical_unit='metre',vertical_datum='unknown',source_description='synthetic plane'),artifact='slope.tif',validate=dict(min_valid_pixels=99))]
    path=tmp_path/'recipe.json';write_json(path,data)
    with pytest.raises(ValueError,match='valid pixels'):recipe.execute(path,tmp_path/'failed',tmp_path/'cache')
    assert not (tmp_path/'failed').exists()
    data['steps'][-1]['validate']['min_valid_pixels']=1;write_json(path,data)
    assert recipe.execute(path,tmp_path/'resumed',tmp_path/'cache')['cache_hits']==3
    # Candidate gates survive the adapter; no implicit acceptance.
    record=tmp_path/'resumed/default/terrain/record.json'
    r=json.loads(record.read_text());r['status']='candidate';write_json(record,r)
    with pytest.raises(ValueError,match='Candidate'):adapters.validate(record.parent,{},'slope.tif')
    adapters.validate(record.parent,{'allow_candidate':True},'slope.tif')
    r['status']='hold';write_json(record,r)
    with pytest.raises(ValueError,match='Held'):adapters.validate(record.parent,{'allow_candidate':True},'slope.tif')
    data['steps'][2]['params']['vector']=str(zones);write_json(path,data)
    with pytest.raises(ValueError,match='input_ref'):recipe.execute(path,tmp_path/'untracked',tmp_path/'cache')


def test_recipe_implementation_invalidation(tmp_path,monkeypatch):
    _,_,data=fixture_recipe(tmp_path); path=tmp_path/'recipe.json'
    recipe.execute(path,tmp_path/'one',tmp_path/'cache')
    original=recipe.digest
    monkeypatch.setattr(recipe,'digest',lambda p: 'changed' if Path(p).name=='_raster_cleanup.py' else original(p))
    assert recipe.execute(path,tmp_path/'two',tmp_path/'cache')['cache_hits']==0


def test_recipe_reject_untracked_file_and_sidecar_cache(tmp_path):
    source,_,data=fixture_recipe(tmp_path); path=tmp_path/'recipe.json'
    recipe.execute(path,tmp_path/'one',tmp_path/'cache')
    Path(str(source)+'.msk').write_bytes(b'external')
    with pytest.raises(ValueError,match='sidecars'):recipe.execute(path,tmp_path/'bad',tmp_path/'cache')
    Path(str(source)+'.msk').unlink()
    data['steps'][0]['files']['inputs']=[str(source)];write_json(path,data)
    with pytest.raises(ValueError,match='input_ref'):recipe.execute(path,tmp_path/'untracked',tmp_path/'cache')


def test_recipe_zone_binding_and_variants(tmp_path):
    _,_,data=fixture_recipe(tmp_path);path=tmp_path/'recipe.json'
    data['variants']=[{'id':'small'},{'id':'large','params':{'clean':{'size':5}}}];write_json(path,data)
    recipe.execute(path,tmp_path/'one',tmp_path/'cache')
    zones=write(frame([box(0,0,3,2)],id=['other']),tmp_path/'other.gpkg')
    result=recipe.execute(path,tmp_path/'two',tmp_path/'cache',{'zones':str(zones)})
    assert result['cache_hits']==4
    for variant in ('small','large'):
        row=json.loads((tmp_path/'two'/variant/'area/table.json').read_text())[0]
        assert row['zone_id']=='other'


def test_recipe_terrain_detail_candidate_real_cli(tmp_path):
    source,_,data=fixture_recipe(tmp_path)
    data['steps']=[dict(id='contours',runner='terrain-detail',operation='run',files=dict(input=ref('source')),
                       params=dict(vertical_unit='metre',vertical_datum='unknown',source_description='synthetic plane',levels_m=[5,10,15]),
                       artifact='contours.gpkg',validate=dict(allow_candidate=True,min_rows=1))]
    path=tmp_path/'recipe.json';write_json(path,data)
    recipe.execute(path,tmp_path/'one',tmp_path/'cache')
    assert recipe.execute(path,tmp_path/'two',tmp_path/'cache')['cache_hits']==1
    assert json.loads((tmp_path/'two/default/contours/dossier.json').read_text())['execution_status']=='candidate'


def test_every_operation_lists_its_params_in_help():
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'raster.py'
    for operation in ['inspect', 'display', 'clip', 'warp', 'align', 'mosaic', 'copy', 'calculate', 'reclassify', 'sample', 'histogram', 'zonal',
                      'transform', 'combine', 'update', 'rasterize', 'bands', 'allocate', 'redistribute',
                      'fill_holes', 'focal', 'aggregate', 'regions', 'sieve', 'seed_region', 'morphology', 'class_area', 'transition', 'class_combine', 'zonal_fill']:
        done = subprocess.run([sys.executable, '-B', str(script), operation, '--help'], capture_output=True, encoding='utf-8',
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=120)
        assert done.returncode == 0, done.stderr
        assert f'Parameters of {operation} (' in done.stdout and 'references/' in done.stdout, operation
    focal = subprocess.run([sys.executable, '-B', str(script), 'focal', '--help'], capture_output=True, encoding='utf-8',
                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=120)
    assert 'statistic:mean' in focal.stdout
