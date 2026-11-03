#!/usr/bin/env python3
"""Two-run native-coverage CLI acceptance; optional separate plotting interpreter."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import geopandas as gpd
import shapely
from test_coverage_cleanup import fixture, params, cli


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(frame):
    frame = frame.sort_values('id')
    value = [(row.id, int(row.value), shapely.normalize(row.geometry).wkb_hex) for _, row in frame.iterrows()]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def render(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image
    from shapely.ops import orient
    source = gpd.read_file(root / 'run0/source.gpkg')
    result = gpd.read_file(root / 'run0/adopt/result.gpkg')
    # Matplotlib's winding fill rule needs oriented rings; do not modify source files.
    source.geometry = source.geometry.map(orient)
    result.geometry = result.geometry.map(orient)
    for number in (1, 2):
        fig, axes = plt.subplots(1, 3, figsize=(14, 5))
        for ax, data, title in zip(axes[:2], (source, result), ('Before: matched zigzag edge', 'After: shared-edge simplification')):
            data.plot(ax=ax, color=['#a6cee3', '#b2df8a', '#fdbf6f'], edgecolor='#263746', linewidth=1)
            for _, row in data.iterrows():
                pt = row.geometry.representative_point(); ax.text(pt.x, pt.y, row.id, ha='center', fontsize=10)
            ax.set_title(title); ax.set_aspect('equal'); ax.set_xlim(-.5, 13.5); ax.set_ylim(-.5, 10.5)
        old = source.geometry.iloc[0].boundary.intersection(source.geometry.iloc[1].boundary)
        new = result.geometry.iloc[0].boundary.intersection(result.geometry.iloc[1].boundary)
        gpd.GeoSeries([old]).plot(ax=axes[2], color='#d95f02', linewidth=2, label='before')
        gpd.GeoSeries([new]).plot(ax=axes[2], color='#1b9e77', linewidth=1.5, label='after')
        axes[2].set_aspect('auto')
        axes[2].set_title('Shared edge detail (x exaggerated)'); axes[2].set_xlim(4.5, 5.5); axes[2].set_ylim(-.2, 10.2)
        axes[2].legend(loc='upper left'); axes[2].set_xlabel('metres'); fig.tight_layout()
        fig.savefig(root / f'coverage-qa-{number}.png', dpi=150); plt.close(fig)
    hashes=[]
    for number in (1,2):
        with Image.open(root/f'coverage-qa-{number}.png') as image:
            hashes.append(hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest())
    if hashes[0]!=hashes[1]: raise AssertionError('Repeated preview pixels differ')
    (root/'render.json').write_text(json.dumps({'rgba_sha256':hashes,'visual_review':'requires inspection'},indent=2), encoding='utf-8')


def execute(root, render_python):
    repo = Path(__file__).resolve().parents[1]
    if root == repo or repo in root.parents: raise ValueError('Test artifacts must be outside repository')
    root.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter(); runs=[]
    for i in range(2):
        work=root/f'run{i}';work.mkdir()
        source=work/'source.gpkg'; fixture().to_file(source, driver='GPKG', engine='pyogrio')
        source_hash=digest(source)
        bundle, record=cli(work,'candidate','cleanup',source,params())
        candidate=bundle/'result.gpkg'; after=gpd.read_file(candidate)
        if record['status']!='hold': raise AssertionError('Candidate auto-approved')
        if not shapely.coverage_is_valid(after.geometry.to_numpy()): raise AssertionError('Output coverage invalid')
        if not fixture().geometry.union_all().equals(after.geometry.union_all()): raise AssertionError('Footprint changed')
        cli(work,'difference','compare',source,{'id':'id'},candidate)
        approval={'id':'id','approved_by':'synthetic acceptance fixture', 'source_sha256':source_hash,'candidate_sha256':digest(candidate),
                  'max_displacement_m':.5,'max_area_change_m2':2,'rules':[{'id':'positive','kind':'compare','field':'value','op':'gt','value':0}],
                  'require_coverage':True}
        adopted, adopted_record=cli(work,'adopt','cleanup_adopt',source,approval,candidate)
        cli(work,'rules','rules',adopted/'result.gpkg',{'id':'id','rules':approval['rules']})
        stale=dict(approval,candidate_sha256='stale')
        cli(work,'stale','cleanup_adopt',source,stale,candidate,success=False)
        reread=gpd.read_file(adopted/'result.gpkg')
        if digest(source)!=source_hash: raise AssertionError('Source modified')
        if not adopted_record['details']['coverage']['output_valid']: raise AssertionError('Adoption did not revalidate coverage')
        runs.append({'canonical_sha256':canonical(reread),'source_unchanged':True,'coverage':record['details']['coverage'],
                     'max_displacement_m':max(x['displacement_m'] for x in record['details']['impacts'])})
    if runs[0]['canonical_sha256']!=runs[1]['canonical_sha256']: raise AssertionError('Repeated outputs differ')
    subprocess.run([render_python,str(Path(__file__).resolve()),str(root),'--render-only'],check=True,
                   env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','MPLCONFIGDIR':str(root/'mpl-cache')})
    (root/'acceptance.json').write_text(json.dumps({'runs':runs,'elapsed_seconds':time.perf_counter()-started,
           'coverage_python':sys.executable,'render_python':render_python,'performance_claim':False,
           'scope':'synthetic planar coverage, actual GPKG and CLI chain; no production-map/historical acceptance'},indent=2), encoding='utf-8')
    print(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    parser.add_argument('--render-python',default=sys.executable);parser.add_argument('--render-only',action='store_true')
    args=parser.parse_args();root=args.output.resolve()
    if args.render_only: render(root)
    else: execute(root,args.render_python)
