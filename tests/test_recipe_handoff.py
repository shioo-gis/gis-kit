"""Recipe and handoff regression: file-backed recipes, dossiers, candidate topology and handoff."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw
import pytest
import shapely
from shapely.geometry import box, mapping, Point

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from _delivery import digest, write_json
from recipe import execute as recipe
from atlas import execute as atlas
from candidates import execute as candidates, A
spec=importlib.util.spec_from_file_location('georef_delivery',SCRIPTS/'georef-delivery.py')
D=importlib.util.module_from_spec(spec);spec.loader.exec_module(D)
G=D.G


def cli(script,*args,ok=True):
    process=subprocess.run([sys.executable,str(SCRIPTS/script),*map(str,args)],env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True)
    if ok: assert process.returncode==0,process.stdout+process.stderr
    else: assert process.returncode!=0
    return process


def recipe_fixture(root):
    source=root/'source.gpkg'
    gpd.GeoDataFrame({'id':['a','b'],'value':[3,7]},geometry=[box(0,0,10,10),box(10,0,20,10)],crs=32631).to_file(source,driver='GPKG')
    data={'schema_version':1,'inputs':{'study':str(source)},'steps':[
        {'id':'grid','operation':'grid','input':'study','params':{'size_m':5,'origin':[0,0]},'validate':{'min_rows':1,'crs':'EPSG:32631'}},
        {'id':'profile','operation':'profile','input':'grid','params':{}}]}
    write_json(root/'recipe.json',data)
    return source,data


def test_recipe_cache_and_parameter_invalidation(tmp_path):
    source,data=recipe_fixture(tmp_path); h=digest(source)
    recipe(tmp_path/'recipe.json',tmp_path/'one',tmp_path/'cache')
    assert recipe(tmp_path/'recipe.json',tmp_path/'two',tmp_path/'cache')['cache_hits']==2
    data['steps'][0]['params']['size_m']=10;write_json(tmp_path/'recipe.json',data)
    assert recipe(tmp_path/'recipe.json',tmp_path/'three',tmp_path/'cache')['cache_hits']==0
    assert digest(source)==h


def test_recipe_corrupt_artifact_and_record(tmp_path):
    _,_=recipe_fixture(tmp_path)
    recipe(tmp_path/'recipe.json',tmp_path/'one',tmp_path/'cache')
    records=json.loads((tmp_path/'one/record.json').read_text(encoding='utf-8'))['steps']
    entry=tmp_path/'cache'/records[0]['signature']; (entry/'result.gpkg').write_bytes(b'broken')
    assert recipe(tmp_path/'recipe.json',tmp_path/'two',tmp_path/'cache')['cache_hits']<=1
    (entry/'record.json').write_text('{}', encoding='utf-8')
    assert recipe(tmp_path/'recipe.json',tmp_path/'three',tmp_path/'cache')['cache_hits']<=1


def test_recipe_failure_resume_matches_fresh(tmp_path):
    _,data=recipe_fixture(tmp_path)
    data['steps'][1]['validate']={'min_rows':99999};write_json(tmp_path/'recipe.json',data)
    with pytest.raises(ValueError):recipe(tmp_path/'recipe.json',tmp_path/'failed',tmp_path/'cache')
    assert not (tmp_path/'failed').exists()
    data['steps'][1]['validate']={'min_rows':1};write_json(tmp_path/'recipe.json',data)
    assert recipe(tmp_path/'recipe.json',tmp_path/'resumed',tmp_path/'cache')['cache_hits']==1
    recipe(tmp_path/'recipe.json',tmp_path/'fresh',tmp_path/'fresh-cache')
    a=gpd.read_file(tmp_path/'resumed/default/grid/result.gpkg');b=gpd.read_file(tmp_path/'fresh/default/grid/result.gpkg')
    assert list(shapely.normalize(a.geometry).to_wkt())==list(shapely.normalize(b.geometry).to_wkt())


def test_recipe_bindings_variants_and_reject_structure(tmp_path):
    source,data=recipe_fixture(tmp_path)
    data['variants']=[{'id':'fine'},{'id':'coarse','params':{'grid':{'size_m':10}}}];write_json(tmp_path/'recipe.json',data)
    cli('recipe.py',tmp_path/'recipe.json','--output',tmp_path/'out','--cache',tmp_path/'cache')
    other=tmp_path/'other.gpkg';gpd.GeoDataFrame({'id':['c']},geometry=[box(0,0,30,10)],crs=32631).to_file(other)
    assert recipe(tmp_path/'recipe.json',tmp_path/'changed',tmp_path/'cache',{'study':str(other)})['cache_hits']==0
    data['steps'][0]['input']='profile';write_json(tmp_path/'recipe.json',data)
    with pytest.raises(ValueError,match='earlier'):recipe(tmp_path/'recipe.json',tmp_path/'invalid',tmp_path/'cache')
    assert not (tmp_path/'invalid').exists()


def test_recipe_held_rule_never_published(tmp_path):
    source,data=recipe_fixture(tmp_path)
    data['steps']=[{'id':'gate','operation':'rules','input':'study','params':{'id':'id','rules':[{'id':'positive','kind':'compare','field':'value','op':'gt','value':100}]}}];write_json(tmp_path/'recipe.json',data)
    with pytest.raises(ValueError,match='held'):recipe(tmp_path/'recipe.json',tmp_path/'bad',tmp_path/'cache')
    assert not (tmp_path/'bad').exists()


def atlas_fixture(root):
    source=root/'objects.gpkg'
    hole=box(0,0,10,10).difference(box(3,3,7,7))
    gpd.GeoDataFrame({'id':['../a','乙','missing'],'name':['<script>x</script>','孔洞对象','empty'],'group':['b','a','a']},geometry=[box(15,0,20,8),hole,None],crs=32631).to_file(source)
    write_json(root/'atlas.json',{'id':'id','group':'group','span_m':50})
    return source


def test_atlas_all_ids_omissions_safe_paths_holes(tmp_path):
    source=atlas_fixture(tmp_path);h=digest(source)
    result=atlas(source,tmp_path/'atlas.json',tmp_path/'atlas')
    assert result['objects']==3 and result['omitted']==1 and digest(source)==h
    r=json.loads((tmp_path/'atlas/record.json').read_text(encoding='utf-8'))
    assert {o['id'] for o in r['objects']}=={'../a','乙','missing'}
    for o in r['objects']:
        d=json.loads((tmp_path/'atlas'/o['path']).read_text(encoding='utf-8'));assert d['id']==o['id']
        if d['status']=='complete':
            assert (tmp_path/'atlas'/d['map']['path']).is_file()
            if d['id']=='乙':assert d['metrics']['area_m2']==84
    html=(tmp_path/'atlas/index.html').read_text(encoding='utf-8');assert '<script>' not in html and '&lt;script&gt;' in html
    with pytest.raises(ValueError):atlas(source,tmp_path/'atlas.json',tmp_path/'atlas')


def test_atlas_extent_failure_atomic(tmp_path):
    source=atlas_fixture(tmp_path);write_json(tmp_path/'atlas.json',{'id':'id','span_m':1})
    with pytest.raises(ValueError,match='span_m'):atlas(source,tmp_path/'atlas.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def network_fixture(root):
    image=root/'synthetic-sheet.png'
    picture=Image.new('RGB',(200,140),'#fff9df');draw=ImageDraw.Draw(picture)
    draw.text((4,2),'SYNTHETIC TOPOLOGY FIXTURE',fill='black')
    state={g:{} for g in A.GROUPS};state.update(revision=1,crossings=[])
    def ring(prefix,coordinates):
        for i,xy in enumerate(coordinates):state['nodes'][f'{prefix}n{i}']={'xy':xy}
        for i,xy in enumerate(coordinates):
            state['edges'][f'{prefix}e{i}']={'start':f'{prefix}n{i}','end':f'{prefix}n{(i+1)%len(coordinates)}','kind':'administrative','status':'visible','geometry':{'type':'polyline','vertices':[]}}
        draw.polygon([tuple(p) for p in coordinates],outline='#333333',fill='#b9d7b4')
    ring('outer',[[10,20],[70,20],[130,20],[130,120],[70,120],[10,120]])
    ring('hole',[[25,40],[45,40],[45,60],[25,60]])
    ring('island',[[90,70],[110,70],[110,90],[90,90]])
    ring('inset',[[160,80],[185,80],[185,110],[160,110]])
    # Same-color shared internal border, split at explicit seam node.
    state['nodes']['seam']={'xy':[70,70]}
    for k,start,end in [('seam-top','outern1','seam'),('seam-bottom','seam','outern4')]:state['edges'][k]={'start':start,'end':end,'kind':'administrative','status':'visible','geometry':{'type':'polyline','vertices':[]}}
    draw.line([(70,20),(70,120)],fill='black',width=1)
    # A native cubic side remains native in the candidate record.
    state['edges']['outere5']['geometry']={'type':'cubic','segments':[{'c1':[8,90],'c2':[8,50]}]}
    picture.save(image);run=root/'annotation';A.init(image,run,'synthetic fixture, no source interpretation')
    write_json(run/'revisions/000001.json',state)
    p={'schema_version':1,'source_sha256':digest(image),'revision_sha256':digest(run/'revisions/000001.json'),'reviewer':'synthetic fixture','evidence':'known programmatic topology; not visual historical review','confirmed_edges':list(state['edges']),'frames':[{'id':'main','pixel_region':mapping(box(0,15,140,135))},{'id':'inset','pixel_region':mapping(box(150,65,195,130))}]}
    write_json(root/'confirmed.json',p)
    return run,state,p


def test_candidate_holes_islands_inset_native_and_seam(tmp_path):
    run,state,p=network_fixture(tmp_path);h=digest(run/'revisions/000001.json')
    result=candidates(run,tmp_path/'confirmed.json',tmp_path/'out')
    assert result['status']=='hold' and result['candidates']==5
    r=json.loads((tmp_path/'out/candidates.json').read_text(encoding='utf-8'))
    assert not r['issues'] and not r['diagnostics']
    assert sum(len(c['holes']) for c in r['candidates'])==2
    assert any(c['frame']=='inset' for c in r['candidates'])
    assert r['native_edges']['outere5']['geometry']['type']=='cubic'
    assert digest(run/'revisions/000001.json')==h


def test_candidate_no_coordinate_snapping(tmp_path):
    run,state,p=network_fixture(tmp_path)
    state['nodes']['duplicate']={'xy':state['nodes']['outern0']['xy']}
    state['edges']['outere0']['start']='duplicate'
    write_json(run/'revisions/000001.json',state);p['revision_sha256']=digest(run/'revisions/000001.json');write_json(tmp_path/'confirmed.json',p)
    candidates(run,tmp_path/'confirmed.json',tmp_path/'out')
    r=json.loads((tmp_path/'out/candidates.json').read_text(encoding='utf-8'));assert r['issues']
    assert any('no noding' in i['reason'] for i in r['issues'])


def test_candidate_stale_and_frame_overlap(tmp_path):
    run,state,p=network_fixture(tmp_path);p['revision_sha256']='stale';write_json(tmp_path/'confirmed.json',p)
    with pytest.raises(ValueError,match='bind'):candidates(run,tmp_path/'confirmed.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    p['revision_sha256']=digest(run/'revisions/000001.json');p['frames'][1]['pixel_region']=p['frames'][0]['pixel_region'];write_json(tmp_path/'confirmed.json',p)
    with pytest.raises(ValueError,match='overlap'):candidates(run,tmp_path/'confirmed.json',tmp_path/'bad')


def test_candidate_dangles_and_crossing_hold(tmp_path):
    run,state,p=network_fixture(tmp_path)
    state['nodes']['dangling']={'xy':[60,30]}
    state['edges']['tail']={'start':'outern0','end':'dangling','kind':'admin','status':'visible','geometry':{'type':'polyline','vertices':[]}}
    write_json(run/'revisions/000001.json',state);p['revision_sha256']=digest(run/'revisions/000001.json');p['confirmed_edges'].append('tail');write_json(tmp_path/'confirmed.json',p)
    candidates(run,tmp_path/'confirmed.json',tmp_path/'out');r=json.loads((tmp_path/'out/candidates.json').read_text(encoding='utf-8'))
    assert any(d['kind']=='dangles' for d in r['diagnostics'])


def georef_fixture(root):
    image=root/'pixels.png';Image.fromarray(np.arange(100*80,dtype=np.uint8).reshape(80,100)).save(image)
    transforms=[]
    for name,bounds in [('main',(0,0,59,79)),('inset',(61,0,99,79))]:
        xmin,ymin,xmax,ymax=bounds
        points=[{'id':f'p{i}','role':'fit' if i<4 else 'check','pixel':[x,y],'world':[500000+2*x,1000-2*y]} for i,(x,y) in enumerate([(xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymax),((xmin+xmax)/2,40)])]
        gcps={'actor':{'model':'test-vision-model','reasoning_effort':None},'source_sha256':digest(image),'source_crs':'EPSG:32631','target_crs':'EPSG:32631','coordinate_reference':'synthetic exact fixture','frame':{'id':name,'pixel_region':mapping(box(*bounds))},'points':points,'acceptance_policy':{'max_check_rmse':.01,'max_check_error':.02,'min_fit_coverage':.99,'rationale':'synthetic exact affine'}}
        write_json(root/(name+'-gcps.json'),gcps);G.fit(root/(name+'-gcps.json'),root/(name+'.json'));transforms.append(str(root/(name+'.json')))
    pixels=root/'pixels.gpkg';gpd.GeoDataFrame({'id':['main-object','inset-object','cross-frame']},geometry=[box(10,10,30,30),box(70,20,85,40),box(50,50,70,60)]).to_file(pixels,layer='faces')
    manifest={'source_sha256':digest(image),'gpkg_sha256':digest(pixels),'sampling_tolerance_pixels':.25,'issue_count':0,'issues':[]};write_json(root/'pixels.manifest.json',manifest)
    p={'schema_version':1,'image':str(image),'pixel_gpkg':str(pixels),'pixel_manifest':str(root/'pixels.manifest.json'),'transforms':transforms};write_json(root/'package.json',p)
    return p


def test_georef_multiframe_real_readback(tmp_path):
    p=georef_fixture(tmp_path);before={f:digest(f) for f in [p['image'],p['pixel_gpkg']]}
    cli('georef-delivery.py','package',tmp_path/'package.json','--output',tmp_path/'delivery')
    r=json.loads((tmp_path/'delivery/record.json').read_text(encoding='utf-8'));assert r['status']=='spatial_candidate' and len(r['frames'])==2
    for f in r['frames']:
        root=tmp_path/'delivery'/f['directory'];m=json.loads((root/'vectors.gpkg.manifest.json').read_text(encoding='utf-8'))
        assert any(o['id']=='cross-frame' for o in m['omitted_objects'])
        assert (root/m['transform']['path']).is_file()
        assert len(gpd.read_file(root/'vectors.gpkg'))==1
    assert before=={f:digest(f) for f in before}


def test_affine_comparison_fixed_check_and_maximum(tmp_path):
    p=georef_fixture(tmp_path);before=p['transforms'][0];after=json.loads(Path(before).read_text(encoding='utf-8'));after['matrix'][0][2]+=3;after['matrix'][1][2]+=4
    write_json(tmp_path/'after.json',after)
    r=D.compare(before,tmp_path/'after.json',tmp_path/'compare');assert r['maximum_frame_displacement']==5
    after['points'][-1]['pixel'][0]+=1;write_json(tmp_path/'changed.json',after)
    with pytest.raises(ValueError,match='fixed'):D.compare(before,tmp_path/'changed.json',tmp_path/'bad')


def test_georef_package_failure_no_partial(tmp_path):
    p=georef_fixture(tmp_path);r=json.loads(Path(p['transforms'][1]).read_text(encoding='utf-8'));r['acceptance']['status']='hold';write_json(p['transforms'][1],r)
    with pytest.raises(ValueError,match='hold'):D.package(tmp_path/'package.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    p['allow_hold']=True;write_json(tmp_path/'package.json',p);D.package(tmp_path/'package.json',tmp_path/'diagnostic')
    assert json.loads((tmp_path/'diagnostic/record.json').read_text(encoding='utf-8'))['status']=='hold'


def test_recipe_dependency_and_implementation_invalidation(tmp_path,monkeypatch):
    import recipe as module
    import shutil
    from types import SimpleNamespace
    source,data=recipe_fixture(tmp_path)
    copied=tmp_path/'scripts';shutil.copytree(SCRIPTS,copied)
    monkeypatch.setattr(module,'SCRIPTS',copied)
    recipe(tmp_path/'recipe.json',tmp_path/'first',tmp_path/'cache')
    (copied/'_metric.py').write_text((copied/'_metric.py').read_text(encoding='utf-8')+'\n# fixture implementation change\n', encoding='utf-8')
    assert recipe(tmp_path/'recipe.json',tmp_path/'code-changed',tmp_path/'cache')['cache_hits']==0
    distributions=list(module.importlib.metadata.distributions())
    monkeypatch.setattr(module.importlib.metadata,'distributions',lambda:distributions+[SimpleNamespace(metadata={'Name':'fixture-dependency'},version='2')])
    assert recipe(tmp_path/'recipe.json',tmp_path/'dependency-changed',tmp_path/'cache')['cache_hits']==0


def test_recipe_preserves_unrelated_step_cache(tmp_path):
    _,data=recipe_fixture(tmp_path)
    data['steps'].append({'id':'independent','operation':'profile','input':'study','params':{}});write_json(tmp_path/'recipe.json',data)
    recipe(tmp_path/'recipe.json',tmp_path/'one',tmp_path/'cache')
    data['steps'][0]['params']['size_m']=10;write_json(tmp_path/'recipe.json',data)
    assert recipe(tmp_path/'recipe.json',tmp_path/'two',tmp_path/'cache')['cache_hits']==1


def test_atlas_feet_units_and_all_invalid(tmp_path):
    source=tmp_path/'feet.gpkg';gpd.GeoDataFrame({'id':['foot']},geometry=[box(1000000,200000,1000010,200010)],crs=2263).to_file(source)
    write_json(tmp_path/'p.json',{'id':'id','span_m':20})
    atlas(source,tmp_path/'p.json',tmp_path/'feet-atlas')
    r=json.loads((tmp_path/'feet-atlas/000001.json').read_text(encoding='utf-8'));assert abs(r['metrics']['area_m2']-9.29034116)<1e-7
    invalid=tmp_path/'invalid.gpkg';gpd.GeoDataFrame({'id':['a','b']},geometry=[None,None],crs=32631).to_file(invalid)
    assert atlas(invalid,tmp_path/'p.json',tmp_path/'invalid-atlas')['omitted']==2


def test_atlas_failure_during_render_not_published(tmp_path,monkeypatch):
    source=atlas_fixture(tmp_path)
    def fail(*args,**kwargs):raise OSError('injected image write failure')
    monkeypatch.setattr(Image,'open',fail)
    with pytest.raises(OSError):atlas(source,tmp_path/'atlas.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_candidate_unconfirmed_and_invalid_edge_diagnostics(tmp_path):
    run,state,p=network_fixture(tmp_path);p['confirmed_edges'].remove('seam-top')
    state['edges']['islande0']['status']='uncertain';write_json(run/'revisions/000001.json',state)
    p['revision_sha256']=digest(run/'revisions/000001.json');write_json(tmp_path/'confirmed.json',p)
    candidates(run,tmp_path/'confirmed.json',tmp_path/'out')
    r=json.loads((tmp_path/'out/candidates.json').read_text(encoding='utf-8'))
    assert {x['edge'] for x in r['omitted_edges']}=={'seam-top','islande0'}
    assert r['status']=='hold'


def test_georef_late_vector_failure_atomic_and_source_intact(tmp_path,monkeypatch):
    p=georef_fixture(tmp_path);h=digest(p['image'])
    def fail(*args,**kwargs):raise ValueError('injected vector export failure')
    monkeypatch.setattr(G,'vectors',fail)
    with pytest.raises(ValueError,match='injected'):D.package(tmp_path/'package.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists() and digest(p['image'])==h


def test_georef_portable_pixel_references(tmp_path):
    p=georef_fixture(tmp_path);D.package(tmp_path/'package.json',tmp_path/'one')
    import shutil
    shutil.move(tmp_path/'one',tmp_path/'moved')
    root=tmp_path/'moved/0001';r=json.loads((root/'vectors.gpkg.manifest.json').read_text(encoding='utf-8'))
    assert digest(root/r['pixel_export']['path'])==r['pixel_export']['sha256']
    assert digest(root/r['pixel_export']['manifest_path'])==r['pixel_export']['manifest_sha256']


def test_atlas_manifest_html_references_resolve(tmp_path):
    from html.parser import HTMLParser
    source=atlas_fixture(tmp_path);atlas(source,tmp_path/'atlas.json',tmp_path/'out')
    class References(HTMLParser):
        def handle_starttag(self,tag,attrs):
            for k,v in attrs:
                if k in ('href','src'):assert (tmp_path/'out'/v).is_file()
    References().feed((tmp_path/'out/index.html').read_text(encoding='utf-8'))


def test_georef_reject_geographic_pixel_input(tmp_path):
    p=georef_fixture(tmp_path)
    f=gpd.read_file(p['pixel_gpkg']);f=f.set_crs(4326);Path(p['pixel_gpkg']).unlink();f.to_file(p['pixel_gpkg'],layer='faces')
    with pytest.raises(ValueError,match='unreferenced'):D.package(tmp_path/'package.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_georef_sidecars_not_silently_unbound(tmp_path):
    p=georef_fixture(tmp_path);wal=Path(p['pixel_gpkg']+'-wal');wal.write_bytes(b'active')
    with pytest.raises(ValueError,match='sidecars'):D.package(tmp_path/'package.json',tmp_path/'bad')
    wal.unlink();Path(p['image']+'.msk').write_bytes(b'external mask')
    with pytest.raises(ValueError,match='sidecars'):D.package(tmp_path/'package.json',tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
