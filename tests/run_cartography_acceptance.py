#!/usr/bin/env python3
"""Repeatable Stage G CLI/file acceptance. All outputs must be outside the repository."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from shapely.geometry import Point,LineString,Polygon,box

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from _delivery import digest,write_json


def execute(args):
    output=Path(args.output).resolve()
    if output.is_relative_to(SCRIPTS.parent.parent) or output.exists():raise ValueError('New external output directory required')
    output.mkdir(parents=True);inputs=output/'inputs';inputs.mkdir()
    sources={k:Path(getattr(args,k)).resolve() for k in ('left_rivers','right_rivers','network','settlements','polygons','image')}
    for k,v in sources.items():shutil.copyfile(v,inputs/(k+v.suffix))
    frozen={k:{'path':str(v),'sha256':digest(v)} for k,v in sources.items()};write_json(output/'source-manifest.json',{'sources':frozen,'identity_note':args.source_description,'synthetic_scope':'small coverage/building/seam/ratio fixtures test known numerical gates; do not represent observed changes'})
    def vector(name,data):
        path=inputs/(name+'.gpkg');data.to_file(path,driver='GPKG',engine='pyogrio');return path
    paths={k:inputs/(k+v.suffix) for k,v in sources.items()}
    roads=gpd.read_file(paths['network']);roads['priority']=roads['speed_kmh'];vector('roads',roads)
    settlements=gpd.read_file(paths['settlements']).to_crs(32632);settlements['id']=settlements.source_id.astype(str);vector('cities',settlements)
    polygons=gpd.read_file(paths['polygons']);polygons['id']=[str(i) for i in range(len(polygons))];vector('polygons_ready',polygons)
    fixture=gpd.GeoDataFrame({'id':['a','b','hole'],'group':['x']*3,'ratio':[.2,.8,.5],'weight':[1.,3.,0.],'category':['one','two','one']},geometry=[Polygon([(0,0),(100,0),(101,50),(100,100),(0,100)]),Polygon([(100,0),(200,0),(200,100),(100,100),(101,50)]),Polygon([(220,0),(320,0),(320,100),(220,100)],holes=[[(240,20),(260,20),(260,40),(240,40)]])],crs=32632)
    vector('coverage_fixture',fixture)
    vector('building_fixture',gpd.GeoDataFrame({'id':['near_rectangle','protected_hole']},geometry=[Polygon([(0,0),(40,0),(40,20),(39,21),(0,20)]),Polygon([(100,0),(140,0),(140,40),(100,40)],holes=[[(110,10),(120,10),(120,20),(110,20)]])],crs=32632))
    seam=gpd.GeoDataFrame({'id':['a','b']},geometry=[LineString([(0,0),(50,0)]),LineString([(50,0),(100,0)])],crs=32632);vector('seam_fixture',seam)
    vector('seam_reference',gpd.GeoDataFrame({'id':['target']},geometry=[LineString([(50,1),(50,20)])],crs=32632))
    started=time.monotonic();runs=[]
    for n in range(2):
        root=output/f'run{n}';root.mkdir();artifacts={}
        def call(name,op,source,p,right=None,script='daily.py'):
            spec=root/(name+'.json');write_json(spec,p);dest=root/name
            cmd=[sys.executable,str(SCRIPTS/script)]+([op] if script=='daily.py' else [])+[str(source),'--params',str(spec),'--output',str(dest)]
            if right:cmd+=['--right',str(right)]
            t=time.monotonic();result=subprocess.run(cmd,capture_output=True,encoding='utf-8', errors='replace',env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            (root/(name+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
            if result.returncode:raise RuntimeError(f'{name}: {result.stderr}')
            record=json.loads((dest/'record.json').read_text(encoding='utf-8'));artifact=dest/record.get('artifact',{}).get('name','objects.gpkg')
            if artifact.exists():
                data=gpd.read_file(artifact) if artifact.suffix=='.gpkg' else pd.read_csv(artifact)
                if artifact.suffix=='.gpkg':assert data.crs and data.is_valid.all()
                artifacts[name]=artifact
            record['cli_wall_seconds']=time.monotonic()-t
            return artifact,record
        matchp={'id':'id','radius_m':1500,'max_angle_deg':45,'min_overlap_m':100,'max_pairs':10000}
        corr,record=call('cross_source','match',paths['left_rivers'],matchp,paths['right_rivers'])
        table=pd.read_csv(corr);assert len(table)>0 and table.ambiguous.any()
        parts,splitrec=call('segments','match_split',paths['left_rivers'],matchp,paths['right_rivers'])
        split=gpd.read_file(parts);original=gpd.read_file(paths['left_rivers']);assert np.isclose(split.length.sum(),original.length.sum(),rtol=1e-12)
        for oid,g in zip(original.id,original.geometry):assert split[split.source_id==oid].geometry.union_all().hausdorff_distance(g)<1e-7
        morph,_=call('morphology','morphology',paths['polygons'],{'id':'id','method':'oriented_envelope'})
        aggregate,_=call('settlement_groups','generalize',inputs/'cities.gpkg',{'id':'id','method':'aggregate_points','distance_m':30000,'protected_ids':[str(settlements.id.iloc[0])]})
        lakes,_=call('polygon_groups','generalize',inputs/'polygons_ready.gpkg',{'id':'id','method':'aggregate_polygons','distance_m':2000})
        thin,threc=call('roads','generalize',inputs/'roads.gpkg',{'id':'id','method':'thin_network','from':'u','to':'v','priority':'priority','keep_priority_at_least':50,'protected_ids':[str(roads.id.iloc[0])]})
        assert threc['details']['components_before']==threc['details']['components_after']
        for kind,value in [('ratio','ratio'),('category','category')]:
            summary,_=call(kind,'weighted_summary',inputs/'coverage_fixture.gpkg',{'group':'group','value':value,'weight':'weight','quantity_type':kind,'weight_meaning':'diagnostic denominator'})
            if kind=='ratio':assert np.isclose(pd.read_csv(summary).weighted_ratio.iloc[0],.65,rtol=1e-12,atol=1e-12)
        clean,clrec=call('shared_edges','cleanup',inputs/'coverage_fixture.gpkg',{'id':'id','method':'coverage_simplify','tolerance_m':2,'simplify_boundary':False})
        assert clrec['details']['coverage']['footprint_preserved']
        building,brec=call('buildings','generalize',inputs/'building_fixture.gpkg',{'id':'id','method':'regularize_buildings','max_displacement_m':2,'max_area_change_m2':40})
        assert any(r['displacement_m']>0 for r in brec['details']['impacts'])
        assert all(r['holes_before']==r['holes_after'] for r in brec['details']['impacts'])
        call('building_map',None,building,{'id':'id','scale':1000,'map_width_mm':180,'map_height_mm':100,'dpi':160,'font_size':7,'title':'Bounded envelope candidate | 1:1000'},script='cartographic-map.py')
        call('lake_atlas',None,inputs/'polygons_ready.gpkg',{'id':'id','max_objects':10},script='atlas.py')
        seamout,_=call('seam','edge_match',inputs/'seam_fixture.gpkg',{'id':'id','max_displacement_m':1,'endpoint_pairs':[{'source_id':'a','target_id':'target','source_end':-1,'target_end':0}]},inputs/'seam_reference.gpkg')
        call('seam_adopt','cleanup_adopt',inputs/'seam_fixture.gpkg',{'id':'id','approved_by':'automated numerical fixture gate, not human geographic approval','source_sha256':digest(inputs/'seam_fixture.gpkg'),'candidate_sha256':digest(seamout),'max_displacement_m':1,'max_area_change_m2':0,'rules':[]},seamout)
        colors,_=call('colors','generalize',clean,{'id':'id','method':'color'})
        _,maprec=call('coverage_map',None,colors,{'id':'id','label':'id','color_field':'color_index','scale':2000,'map_width_mm':180,'map_height_mm':100,'dpi':160,'title':'Coverage candidate | 1:2000'},script='cartographic-map.py')
        assert not maprec['label_audit']['overlaps']
        _,maprec=call('road_map',None,thin,{'id':'id','label':'id','scale':18000,'map_width_mm':180,'map_height_mm':140,'dpi':160,'font_size':6,'max_labels':60,'label_collision_policy':'omit','title':'OSM endpoint-preserving selection | 1:18000'},script='cartographic-map.py')
        assert not maprec['label_audit']['overlaps'] and maprec['status']=='complete'
        size=Image.open(paths['image']).size
        call('oriented_sample',None,paths['image'],{'center_xy':[size[0]/2,size[1]/2],'size_px':[256,256],'angle_deg':20,'source_pixels_per_output_pixel':1},script='oriented-crop.py')
        runs.append(artifacts)
    for name,path in runs[0].items():
        other=runs[1][name]
        if path.suffix=='.gpkg':
            from geopandas.testing import assert_geodataframe_equal
            assert_geodataframe_equal(gpd.read_file(path),gpd.read_file(other))
        else:pd.testing.assert_frame_equal(pd.read_csv(path),pd.read_csv(other))
    for name in ('coverage_map/map.png','road_map/map.png','building_map/map.png','oriented_sample/crop.png'):
        assert np.array_equal(np.asarray(Image.open(output/'run0'/name)),np.asarray(Image.open(output/'run1'/name)))
    for key,item in frozen.items():assert digest(item['path'])==item['sha256']
    # Source-backed overlay; both unmodified source products remain visually distinguishable.
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(8,8));gpd.read_file(paths['right_rivers']).plot(ax=ax,color='#bac8d1',linewidth=.7);original.plot(ax=ax,color='#d95f02',linewidth=1.8);split[split.match_status=='ambiguous'].plot(ax=ax,color='#9c27b0',linewidth=3)
    ax.set_title('Natural Earth / GloRiC correspondence candidates\nOrange: NE; grey: GloRiC; purple: ambiguous source intervals');ax.set_axis_off();fig.tight_layout();fig.savefig(output/'cross-source-qa.png',dpi=160);plt.close(fig)
    acceptance={'status':'numerical_and_repeatability_pass','cross_source_candidates':len(table),'ambiguous_pairs':int(table.ambiguous.sum()),'source_segments':len(split),'network_input':len(roads),'network_output':len(gpd.read_file(runs[0]['roads'])),'wall_seconds':time.monotonic()-started,'self_maxrss':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'children_maxrss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,'rss_unit':'bytes on macOS; separate process maxima, not sum or process tree peak','visual_review':'pending','performance_claim':'none'}
    write_json(output/'acceptance.json',acceptance);print(json.dumps(acceptance))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('left-rivers','right-rivers','network','settlements','polygons','image','source-description'):p.add_argument('--'+k,required=True)
    p.add_argument('output');execute(p.parse_args())
