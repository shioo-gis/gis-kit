#!/usr/bin/env python3
"""Run three real CLI/file cases twice and render a source-backed QA contact sheet.

Usage: PYTHONDONTWRITEBYTECODE=1 python3 tests/run_daily_acceptance.py /absolute/new/directory
Synthetic, hand-calculated fixtures only; this is not production-data acceptance.
"""
from pathlib import Path
import hashlib
import json
import os
import sys
import time


def main():
    root=Path(sys.argv[1]).resolve()
    root.mkdir(parents=True,exist_ok=False)
    os.environ['MPLCONFIGDIR']=str(root/'mplconfig')
    os.environ['XDG_CACHE_HOME']=str(root/'cache')
    import test_daily as cases
    start=time.perf_counter()
    cases.test_three_end_to_end_cases(root)
    elapsed=time.perf_counter()-start
    import geopandas as gpd
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image
    from shapely import from_wkt
    # Exercise the existing product map CLI as well as the diagnostic contact sheet.
    for name,label in [('multi_source','code'),('candidates','candidate_id')]:
        cases.cli('plot.py','--inputs',root/f'run0/{name}/result.gpkg','--output',root/f'{name}.png',
                  '--label-field',label,'--title',name,'--dpi',120,'--figsize','8,5')
    fingerprints=[]
    for repeat in (1,2):
        fig,axes=plt.subplots(1,3,figsize=(15,5))
        for ax,name,label,title in [(axes[0],'multi_source','code','1. CSV + XLSX / stable IDs'),(axes[1],'candidates','candidate_id','2. Restriction subtraction')]:
            source=gpd.read_file(root/f'run0/{name}/result.gpkg')
            source.plot(ax=ax,color=['#c3deea','#f0d9b5','#c9dec2'],edgecolor='#334455')
            for _,row in source.iterrows():
                point=row.geometry.representative_point();ax.text(point.x,point.y,row[label],ha='center')
            if name=='candidates':
                gpd.read_file(root/'restriction.gpkg').plot(ax=ax,color='#ee9999',edgecolor='#aa3333',hatch='//')
            ax.set_title(title);ax.set_aspect('equal');ax.set_xlabel('Analysis X (m)');ax.set_ylabel('Analysis Y (m)')
        ax=axes[2]
        table=pd.read_csv(root/'run0/neighbors/result.csv',dtype={'source_id':str,'target_id':str})
        for _,row in table.sort_values('rank',ascending=False).iterrows():
            geom=from_wkt(row.connector_wkt);x,y=geom.xy
            ax.plot(x,y,color='#df873c' if row['rank']==1 else '#829aac',lw=3 if row['rank']==1 else 1,alpha=.8)
        sources=gpd.read_file(root/'run0/points/result.gpkg');targets=gpd.read_file(root/'facilities.gpkg')
        ax.scatter(sources.geometry.x,sources.geometry.y,color='#224466',s=60,marker='o')
        ax.scatter(targets.geometry.x,targets.geometry.y,color='#cc5533',s=60,marker='^')
        for _,row in sources.iterrows():ax.text(row.geometry.x,-.35,row['code'],ha='center',va='top')
        for _,row in targets.iterrows():ax.text(row.geometry.x,.35,row['id'],ha='center')
        ax.set_xlim(-1,11);ax.set_ylim(-2,2);ax.set_aspect('equal');ax.set_xlabel('Analysis X (m)')
        ax.set_title('3. Nearest K=2 / exact distances')
        ax.text(.5,-.7,'001 → f1: 3 m; f2: 7 m\n002 → f2: 3 m; f1: 7 m',transform=ax.transAxes,ha='center')
        fig.suptitle('Daily vector — synthetic, hand-calculated file-chain QA',fontsize=14)
        fig.tight_layout();path=root/f'qa-{repeat}.png';fig.savefig(path,dpi=140);plt.close(fig)
        im=Image.open(path).convert('RGBA')
        fingerprints.append({'size':im.size,'decoded_rgba_sha256':hashlib.sha256(im.tobytes()).hexdigest()})
    assert fingerprints[0]==fingerprints[1]
    report={'three_cases_twice_seconds':elapsed,'fixtures':'synthetic CSV/XLSX/GPKG; no production data supplied',
            'reproducibility':'normalized geometry + attributes; PNG decoded pixels; no claim of GPKG byte equality',
            'visual':fingerprints[0],'cases':['multi-source normalization/update','parcel selection/allocation/summary/rules','nearest relationships'],
            'visual_review':'requires explicit human/agent image inspection, not inferred from successful rendering'}
    (root/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
