#!/usr/bin/env python3
"""Real local elevation -> grid means / class scheme / display values -> Composer project runs.

gis-kit publishes its analysis, an uncoloured display grid (display-raster v1) and one
class scheme (class-scheme v1) atomically into the Composer project's inputs/<name>/.
Composer colours vector and raster from that scheme by theme and renders with --project
into runs/<run-id>/. Diagnostic grid is constructed here, not an administrative/observed
boundary. This is a runnable acceptance example, not a workflow engine or delivery protocol.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import geopandas as gpd
import numpy as np
from PIL import Image
import rasterio as rio
from rasterio.warp import transform_bounds, transform
from shapely import contains_xy
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from _delivery import bundle, digest, write_json

INPUT = 'mont-blanc grid'
BREAKS = [1000, 2000, 3000, 4000]
LABELS = ['< 1,000 m', '1,000–<2,000 m', '2,000–<3,000 m', '3,000–<4,000 m', '≥ 4,000 m']
UNKNOWN = '未知 / NoData（无有效像元）'
THEMES = ('research', 'atlas', 'presentation')


def run(command, cwd, log, success=True):
    result = subprocess.run(list(map(str, command)), cwd=cwd, capture_output=True, text=True)
    write_json(log, {'command': list(map(str, command)), 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    if success and result.returncode:
        raise RuntimeError(f'Command failed; see {log}: {result.stderr[-1200:]}')
    if not success and result.returncode == 0:
        raise AssertionError(f'Expected failure: {command}')
    return result


def scene(kind, theme, bounds, semantics):
    # Scenes live in the project's scenes/; analysis files are referenced in inputs/<name>/.
    # No colours here: the class scheme says what each class means, the theme colours it.
    at = lambda name: f'../inputs/{INPUT}/{name}'
    s = {'id':f'{kind}-{theme}', 'theme':theme, 'renderer':'maplibre',
         'title': '勃朗峰周边 · ' + ('网格平均高程' if kind == 'vector' else '高程分级'),
         'subtitle':'本地 GEBCO 2023 标记样区 | candidate · 来源与垂直基准未独立核验 · 注记为网格均值',
         'canvas':{'width':1400,'height':1000,'pixelRatio':1},
         'map':{'bounds':bounds,'fitPadding':{'top':150,'bottom':100,'left':60,'right':330},'renderWorldCopies':False},
         'typography':{'fontFamily':'AnalysisSans','mapFontFamily':'AnalysisSans','fonts':[{'family':'AnalysisSans','path':at('font.otf'),'license':'SIL OFL 1.1; font-license.txt'}]},
         'sources':[{'id':'grid','type':'geojson','path':at('grid means.geojson')},{'id':'names','type':'geojson','path':at('grid labels.geojson')}],
         'layers':[], 'analysis':semantics,
         'overlay':{'frame':False,'scaleBar':False,'title':{'size':34},'subtitle':{'size':17},'legend':{'x':1080,'y':190,'width':275,'fontSize':17}},
         'qa':{'requiredLabels':[{'layerId':'names','ids':[f'{i:02}' for i in range(1,13)]}]}}
    if kind == 'raster':
        s['sources'].insert(0, {'id':'elevation','type':'display-raster','path':at('display/raster.json')})
        s['layers'] += [{'id':'unknown','source':'grid','type':'fill','paint':{'fill-color':'{palette.noData}','fill-opacity':1},'legend':False},
                        {'id':'elevation','source':'elevation','type':'raster','legend':{'title':'像元高程 / m','nodata':'{palette.noData}'}}]
    else:
        s['layers'].append({'id':'means','source':'grid','type':'fill','scheme':{'path':at('scheme/scheme.json'),'field':'mean'},'legend':{'title':'网格均值 / m','histogram':True}})
    s['layers'] += [{'id':'grid-lines','source':'grid','type':'line','paint':{'line-color':'#ffffff','line-width':1.4},'legend':False},
                    {'id':'names','source':'names','type':'symbol','text':{'field':'name','size':16,'color':'#172c3f','haloColor':'#ffffff','haloWidth':2},'placement':{'optional':False},'legend':False}]
    return s


def composer_json(result):
    # Composer prints one JSON document per command on stdout.
    return json.loads(result.stdout)


def rgb(color):
    return [int(color[j:j+2], 16) for j in (1, 3, 5)]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', help='Real elevation sample in metres, self-contained GeoTIFF')
    p.add_argument('output', help='New output directory, outside source repositories; the Composer project is created inside it')
    p.add_argument('--composer', required=True)
    p.add_argument('--source-description', required=True)
    args=p.parse_args()
    source=Path(args.source).resolve(); output=Path(args.output).resolve(); composer=Path(args.composer).resolve()
    output.mkdir(parents=True,exist_ok=False)
    logs=output/'logs'; logs.mkdir()
    before=digest(source)
    entry=composer/'src/agent-entry.js'
    project=output/'project'
    counter=0
    def command(cmd, cwd=output, success=True):
        nonlocal counter
        counter+=1
        return run(cmd,cwd,logs/f'{counter:02}.json',success)
    def composer_cmd(*args, root=project, success=True):
        return command(['node',entry,'--project',root,*args],success=success)
    def gis(script,*args):
        return command([sys.executable,SCRIPTS/script,*args])
    def raster(operation,input,params,dest):
        settings=output/f'params-{dest.name}.json'; write_json(settings,params)
        gis('raster.py',operation,input,'--params',settings,'--output',dest)
        return json.loads((dest/'record.json').read_text(encoding='utf-8'))
    command(['node',entry,'project','init',project,'--name','gis-kit composer acceptance'])
    analysis=project/'inputs'/INPUT
    scheme_params={'breaks':BREAKS,'labels':LABELS,'unit':'m','status':'candidate','nodata_label':UNKNOWN}
    # Publication of the analysis inputs is all-or-nothing, using the existing directory bundle helper.
    with bundle(analysis) as stage:
        shutil.copy2(source,stage/'source.tif')
        for src,dest in [('SourceHanSansCN-Normal.otf','font.otf'),('SourceHanSansCN-Normal.license.txt','font-license.txt')]:
            shutil.copy2(composer/'assets/fonts'/src,stage/dest)
        warped=raster('warp',stage/'source.tif',{'band':1,'crs':'EPSG:3857','kind':'continuous','resampling':'nearest','resolution':250},stage/'warp')
        with rio.open(stage/'warp/result.tif') as ds:
            left,bottom,right,top=ds.bounds
        dx=(right-left)/3; dy=(top-bottom)/3
        grid=gpd.GeoDataFrame({'id':[f'{i:02}' for i in range(1,13)]},geometry=[box(left+x*dx,bottom+y*dy,left+(x+1)*dx,bottom+(y+1)*dy) for y in range(3) for x in range(4)],crs=3857)
        grid.to_file(stage/'zones.gpkg',layer='diagnostic_grid')
        # Decoy layer catches accidentally relying on first/default layer.
        grid.iloc[:1].to_file(stage/'zones.gpkg',layer='decoy')
        zonal=raster('zonal',stage/'source.tif',{'vector':str(stage/'zones.gpkg'),'layer':'diagnostic_grid','id':'id','band':1,'method':'center'},stage/'zonal')
        # Independent polygon contains tests over actual source pixel centres.
        with rio.open(stage/'source.tif') as ds:
            data=ds.read(1,masked=True); rows,cols=np.indices(data.shape)
            xs=ds.transform.c+(cols+.5)*ds.transform.a; ys=ds.transform.f+(rows+.5)*ds.transform.e
            for geom,row in zip(grid.to_crs(ds.crs).geometry,zonal['zones']):
                valid=contains_xy(geom,xs,ys) & ~np.ma.getmaskarray(data) & np.isfinite(data.data)
                assert row['valid_pixels']==int(valid.sum())
                expected=float(data.data[valid].mean()) if valid.any() else None
                assert row['mean'] is None if expected is None else np.isclose(row['mean'],expected,rtol=1e-12)
        grid['mean']=[r['mean'] for r in zonal['zones']]
        grid['valid_pixels']=[r['valid_pixels'] for r in zonal['zones']]
        grid['status']='candidate'; grid['unit']='m'
        grid.to_file(stage/'analysis.gpkg',layer='means')
        gis('inspect-data.py',stage/'analysis.gpkg','--layer','means','summary','--json')
        gis('stats.py','describe',stage/'analysis.gpkg','--layer','means','--json')
        gis('convert.py',stage/'analysis.gpkg','--layer','means','--crs','EPSG:4326','--output',stage/'grid means.geojson')
        reread=gpd.read_file(stage/'grid means.geojson')
        assert np.allclose(grid['mean'],reread['mean'],equal_nan=True)
        names=gpd.GeoDataFrame({'id':grid.id,'name':[f'网格 {i}\n{v:.0f} m' if np.isfinite(v) else f'网格 {i}\n未知' for i,v in zip(grid.id,grid['mean'])]},geometry=grid.geometry.centroid,crs=3857).to_crs(4326)
        names.to_file(stage/'grid labels.geojson')
        # One scheme for both: the vector field and the uncoloured display grid share breaks and labels.
        settings=output/'params-scheme.json'; write_json(settings,scheme_params)
        gis('scheme.py',stage/'analysis.gpkg','--layer','means','--field','mean','--params',settings,'--output',stage/'scheme')
        display=raster('display',stage/'warp/result.tif',{'band':1,'mode':'values',**scheme_params},stage/'display')
        vector_scheme=json.loads((stage/'scheme/scheme.json').read_text(encoding='utf-8')); raster_scheme=json.loads((stage/'display/scheme.json').read_text(encoding='utf-8'))
        assert vector_scheme['breaks']==raster_scheme['breaks']==BREAKS and [c['label'] for c in vector_scheme['classes']]==[c['label'] for c in raster_scheme['classes']]==LABELS
        assert 'colors' not in vector_scheme and 'colors' not in raster_scheme and display['encoding']=='class-index'
        bounds=list(transform_bounds('EPSG:3857','EPSG:4326',left,bottom,right+dx,top))
        semantics={'status':'candidate','source':{'file':'source.tif','sha256':before,'description':args.source_description},
                   'vector':{'file':'analysis.gpkg','layer':'means','field':'mean','unit':'m','denominator':'valid source pixel count, equal-weight centre inclusion','zones':'constructed diagnostic grid; not administrative units'},
                   'raster':{'file':'warp/result.tif','band':1,'unit':'m','resampling':'nearest, explicit EPSG:3857 display grid'},
                   'class_scheme':{'vector':'scheme/scheme.json','raster':'display/scheme.json','breaks':BREAKS,'interval_rule':raster_scheme['interval_rule']},
                   'unknown':'null mean / no valid pixels; transparent raster over the theme noData grid; never zero',
                   'upstream_identity_verified':False,'vertical_datum':'unknown'}
        controls=[]
        with rio.open(stage/'warp/result.tif') as ds:
            values=ds.read(1,masked=True)
            # Interior, away from grid lines and centre labels; the expected colour depends on the theme.
            for i,(rf,cf) in enumerate(((.13,.13),(.23,.77),(.44,.21),(.61,.87),(.83,.41))):
                row,col=int(ds.height*rf),int(ds.width*cf)
                x,y=ds.xy(row,col)
                lon,lat=transform(ds.crs,'EPSG:4326',[x],[y])
                controls.append({'id':str(i),'coordinates':[lon[0],lat[0]],'class':int(np.searchsorted(BREAKS,values[row,col],side='right'))})
        write_json(stage/'record.json',{'delivery':'complete','consumer':'gis-composer project inputs','analysis_status':'candidate','analysis':semantics,
                  'files':{str(f.relative_to(stage)):digest(f) for f in stage.rglob('*') if f.is_file()}})
    published=json.loads((analysis/'record.json').read_text(encoding='utf-8'))['files']
    runs={}; palettes={}
    for theme in THEMES:
        for kind in ('vector','raster'):
            name=f'{kind}-{theme}'
            spec=scene(kind,theme,bounds,semantics)
            if kind=='raster': spec['qa']['controlPoints']=[{'id':c['id'],'coordinates':c['coordinates']} for c in controls]
            write_json(project/'scenes'/f'{name}.scene.json',spec)
            assert composer_json(composer_cmd('lint',f'scenes/{name}.scene.json','--expect-no-frame'))['ok']
            result=composer_json(composer_cmd('qa',f'scenes/{name}.scene.json','--name',name))
            assert result['summary']['ok']
            run_dir=Path(result['run']['dir'])
            # Provenance: Composer hashed every file it read from inputs/<name>/ with the SHA-256 gis-kit published.
            prefix=f'inputs/{INPUT}/'
            used={i['ref'][len(prefix):]:i['sha256'] for i in json.loads(Path(result['run']['record']).read_text(encoding='utf-8'))['inputs'] if i['ref'].startswith(prefix)}
            assert used and all(published[n]==sha for n,sha in used.items()), used
            assert ('display/values.png' in used and 'display/scheme.json' in used) if kind=='raster' else 'scheme/scheme.json' in used
            metrics=json.loads(Path(result['metrics']).read_text(encoding='utf-8'))
            assert metrics['render']['resourceUrls']
            assert all(url.startswith(('http://127.0.0.1:','data:','blob:')) for url in metrics['render']['resourceUrls'])
            resolved=json.loads((run_dir/'scene.resolved.json').read_text(encoding='utf-8'))
            ramp=resolved['palette']['dataRamp']; assert len(ramp)==len(LABELS)  # a ramp of the class count is used as is
            legend=resolved['overlay']['legend']['items']
            scheme_item=next(item for item in legend if item.get('model')=='classified')
            # Legends list classes high to low; colours come from the theme ramp.
            assert [c['label'] for c in scheme_item['classes']][:5]==LABELS[::-1] and [c['color'] for c in scheme_item['classes']][:5]==ramp[::-1]
            assert scheme_item['classes'][5]['label']==UNKNOWN and scheme_item['classes'][5]['color']==resolved['palette']['noData']
            if kind=='vector':
                assert [c.get('count') for c in scheme_item['classes']][:5]==vector_scheme['counts'][::-1]
            else:
                pixels=np.asarray(Image.open(result['image']).convert('RGB'))
                for observed,expected in zip(metrics['render']['maps']['main']['controlPoints'],controls):
                    x,y=observed['pixel']; want=rgb(ramp[expected['class']])
                    assert np.array_equal(pixels[round(y),round(x)],want), (theme,observed,want,pixels[round(y),round(x)])
            palettes[theme]=ramp
            runs[name]={'run':result['run']['id'],'png_sha256':digest(result['image']),'inputs_from_gis_kit':sorted(used)}
    assert len({tuple(r) for r in palettes.values()})==len(THEMES), 'each theme must colour the same scheme differently'
    moved=output/'moved project with spaces'; project.rename(moved)
    protected={str(f.relative_to(moved)):digest(f) for f in (moved/'inputs').rglob('*') if f.suffix in ('.tif','.gpkg','.geojson','.png','.json')}
    comparisons={}
    for name in ('vector-research','raster-research'):
        # Fresh subprocess and fresh Chrome from another cwd; rerun re-checks input hashes, then compares bytes.
        result=composer_json(command(['node',entry,'--project',moved,'rerun',runs[name]['run']],cwd=moved/'inputs'))
        assert result['rerun']['identical'] and not result['rerun']['mismatched'] and not result['rerun']['missing'], result
        comparisons[name]={'rerun':result['run']['id'],'byte_identical':True,'png_sha256':runs[name]['png_sha256']}
    styled=json.loads((moved/'scenes/vector-research.scene.json').read_text(encoding='utf-8')); styled['layers'][1]['paint']['line-color']='#263238'
    write_json(moved/'scenes/style-only.scene.json',styled)
    assert digest(composer_json(composer_cmd('qa','scenes/style-only.scene.json','--name','style-only',root=moved))['image'])!=runs['vector-research']['png_sha256']
    failures={}
    expected_codes={'missing-resource':'SOURCE_FILE_MISSING','outside-project':'PATH_OUTSIDE_PROJECT','qa-failed':'IMPORTANT_LABEL_MISSING','output-path-flag':'OUTPUT_FLAG_REMOVED'}
    for case in expected_codes:
        bad=copy.deepcopy(styled); extra=[]
        if case=='missing-resource': bad['sources'][0]['path']=f'../inputs/{INPUT}/missing.geojson'
        if case=='outside-project': bad['sources'][0]['path']='../../outside.geojson'
        if case=='qa-failed': bad['qa']['requiredLabels'][0]['ids'].append('not-present')
        if case=='output-path-flag': extra=['--out','map.png']
        write_json(moved/'scenes/failure.scene.json',bad)
        existing={r.name for r in (moved/'runs').iterdir()}
        result=composer_cmd('qa','scenes/failure.scene.json','--name',case,*extra,root=moved,success=False)
        assert expected_codes[case] in result.stdout+result.stderr, (case,result.stdout[-600:],result.stderr[-600:])
        # A refused command leaves no run, or a run whose record is not ok.
        statuses=[json.loads((r/'run.json').read_text(encoding='utf-8'))['status'] for r in (moved/'runs').iterdir() if r.is_dir() and r.name not in existing]
        assert all(status!='ok' for status in statuses), (case,statuses)
        failures[case]=statuses or ['refused before a run was created']
    (moved/'scenes/failure.scene.json').unlink()
    assert protected=={str(f.relative_to(moved)):digest(f) for f in (moved/'inputs').rglob('*') if f.suffix in ('.tif','.gpkg','.geojson','.png','.json')}
    assert digest(source)==before
    write_json(output/'acceptance.json',{'source_sha256':before,'project':str(moved),'analysis_inputs':f'inputs/{INPUT}','themes':list(THEMES),'theme_ramps':palettes,
               'runs':runs,'repeat':comparisons,'failure_cases':failures,'analysis_unchanged':True,'interior_raster_color_controls':len(controls)*len(THEMES),
               'observed_page_resources':'loopback/data/blob only; no basemap requests',
               'visual_review':'pending human/agent image inspection','limits':['constructed grid','upstream identity and vertical datum unverified','exact glyph bounds unavailable']})
    print(output/'acceptance.json')


if __name__=='__main__': main()
