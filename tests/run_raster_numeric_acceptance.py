#!/usr/bin/env python3
"""Stage-E real local raster + diagnostic regions; double CLI chains, readback and QA."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Existing north-up projected continuous raster, no download')
    parser.add_argument('output')
    parser.add_argument('--source-description', required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    repo = SCRIPTS.parents[1]
    if root == repo or repo in root.parents:
        raise ValueError('Acceptance output must be outside repository')
    root.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    inputs = root/'inputs'
    inputs.mkdir()
    original = Path(args.source).resolve()
    before = digest(original)
    shutil.copy2(original, inputs/'source.tif')
    with rio.open(inputs/'source.tif') as ds:
        bounds, crs, res = ds.bounds, ds.crs, ds.res[0]
        if ds.width*ds.height > 100000 or not ds.crs.is_projected:
            raise ValueError('Acceptance requires a bounded projected source')
        width, height = ds.width, ds.height
    # Freeze geometry, totals, classes and resource/numerical gates before any analysis.
    left, bottom, right, top = bounds
    middle = (left+right)/2 + .37*res
    zero = box(left, top-3*res, left+3*res, top)
    west = box(left+.23*res, bottom+.31*res, middle, top-.17*res).difference(zero)
    east = box(middle, bottom+.31*res, right-.19*res, top-.17*res)
    outside = box(right+10*res, bottom, right+12*res, top)
    regions = gpd.GeoDataFrame({'id': ['west', 'east', 'zero', 'outside'], 'total': [1000., 700., 50., 90.]}, geometry=[west, east, zero, outside], crs=crs)
    regions.to_file(inputs/'regions.gpkg', layer='regions', driver='GPKG')
    gpd.GeoDataFrame({'id': ['zero']}, geometry=[zero], crs=crs).to_file(inputs/'zero.gpkg', driver='GPKG')
    exclusion = box(left+(right-left)*.3, bottom, left+(right-left)*.3+.47*res, top)
    gpd.GeoDataFrame({'id': ['exclude']}, geometry=[exclusion], crs=crs).to_file(inputs/'exclude.gpkg', driver='GPKG')
    gpd.GeoDataFrame({'id': ['all']}, geometry=[box(*bounds)], crs=crs).to_file(inputs/'extent.gpkg', driver='GPKG')
    # A shifted grid and a second non-divisible resolution exercise actual alignment/conservation.
    for name, pixel, x, y in [('reference', res, left+.13*res, top+.11*res), ('coarse', res*1.7, left, top)]:
        w, h = math.ceil((right-x)/pixel), math.ceil((y-bottom)/pixel)
        with rio.open(inputs/f'{name}.tif', 'w', driver='GTiff', width=w, height=h, count=1,
                      dtype='float64', crs=crs, transform=from_origin(x, y, pixel, pixel), nodata=np.nan) as out:
            out.write(np.zeros((h, w)), 1)
    (inputs/'lookup.csv').write_text('value,output\n1,1\n2,2\n3,4\n4,8\n', encoding='utf-8')
    frozen = {'source_description': args.source_description, 'source_sha256': before,
              'source_path': str(original), 'data_scope': 'Real stored elevation; diagnostic regions/totals and weights are synthetic assumptions, not observed population or suitability.',
              'intervals': [0, 2000, 4000, 6000, 10000], 'totals': dict(zip(regions.id, regions.total)),
              'tolerance': {'absolute': 1e-9, 'relative': 1e-12}, 'repeats_block_size': [1, 256],
              'no_performance_claim': True, 'original_evidence_modified': False}
    (inputs/'frozen-policy.json').write_text(json.dumps(frozen, indent=2), encoding='utf-8')
    input_hashes = {p.name: digest(p) for p in inputs.iterdir()}
    runs, signatures = [], []
    for repeat, block in enumerate((1, 256)):
        work = root/f'run{repeat}'
        work.mkdir()
        operations = []
        def cli(name, op, sources, p, success=True):
            p = dict(p, block_size=block)
            spec = work/(name+'.json')
            spec.write_text(json.dumps(p), encoding='utf-8')
            command = [sys.executable, str(SCRIPTS/'raster.py'), op, *map(str, sources), '--params', str(spec), '--output', str(work/name)]
            started = time.perf_counter()
            result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            (work/(name+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
            operations.append({'name': name, 'operation': op, 'seconds': time.perf_counter()-started, 'returncode': result.returncode})
            if not success:
                assert result.returncode != 0 and not (work/name).exists()
                return
            if result.returncode:
                raise RuntimeError(result.stderr)
            return json.loads((work/name/'record.json').read_text(encoding='utf-8'))
        rules = [{'min': lo, 'max': hi, 'output': i+1, 'closed': 'both' if i == 3 else 'left'} for i, (lo, hi) in enumerate(zip(frozen['intervals'], frozen['intervals'][1:]))]
        cli('classes', 'reclassify', [inputs/'source.tif'], {'rules': rules, 'unmapped': 'error'})
        cli('lookup', 'reclassify', [work/'classes/result.tif'], {'table': str(inputs/'lookup.csv'), 'unmapped': 'error'})
        cli('aligned', 'align', [work/'lookup/result.tif'], {'reference': str(inputs/'reference.tif'), 'kind': 'categorical'})
        cli('weights', 'update', [work/'aligned/result.tif'], {'method': 'assign', 'vector': str(inputs/'zero.gpkg'), 'coverage': 'all_touched', 'value': 0})
        params = {'vector': str(inputs/'regions.gpkg'), 'id': 'id', 'total_field': 'total', 'method': 'weighted', 'weight_kind': 'per_area',
                  'quantity': 'total', 'assumption': 'redistribute_over_eligible_support', 'exclude': str(inputs/'exclude.gpkg')}
        allocation = cli('allocation', 'allocate', [work/'aligned/result.tif', work/'weights/result.tif'], params)
        assert {r['zone_id']: r['unallocated'] for r in allocation['zones']} == {'east': 0, 'outside': 90, 'west': 0, 'zero': 50}
        with rio.open(work/'allocation/contributions.tif') as ds:
            assert ds.count == 4
            for i, record in enumerate(allocation['zones'], 1):
                values = ds.read(i, masked=True)
                assert abs(math.fsum(values.compressed())-record['allocated']) <= record['tolerance']
                # Existing zonal CLI: selected source band, source polygon all-touched, totals not fraction-weighted twice.
                zonal = cli(f'back-{i}', 'zonal', [work/'allocation/contributions.tif'], {'vector': str(inputs/'regions.gpkg'), 'id': 'id', 'band': i, 'method': 'all_touched'})
                actual = next(row['sum'] for row in zonal['zones'] if row['zone_id'] == record['zone_id'])
                assert abs((actual or 0)-record['allocated']) <= record['tolerance']
        cli('integer', 'allocate', [work/'aligned/result.tif', work/'weights/result.tif'], dict(params, integer=True))
        conserved = cli('coarse', 'redistribute', [work/'allocation/result.tif'], {'reference': str(inputs/'coarse.tif'), 'quantity': 'total', 'assumption': 'uniform_within_source_pixel'})
        # Shifted reference extends beyond the coarse grid: report genuine outside-support loss, never hide it.
        assert abs(conserved['allocated']+conserved['unallocated']-1700) <= 1.7e-9
        # Overlap and output dtype failures must leave no published bundle.
        overlapping = regions.copy()
        overlapping.loc[overlapping.id == 'east', 'geometry'] = west
        overlap_path = work/'overlap.gpkg'
        overlapping.to_file(overlap_path, driver='GPKG')
        cli('overlap-failure', 'allocate', [work/'aligned/result.tif', work/'weights/result.tif'], dict(params, vector=str(overlap_path)), success=False)
        cli('type-failure', 'bands', [work/'allocation/result.tif'], {'dtype': 'uint8', 'nodata_value': 0}, success=False)
        hashes = {}
        for name in ('classes', 'aligned', 'weights', 'allocation', 'integer', 'coarse'):
            with rio.open(work/name/'result.tif') as ds:
                a = ds.read().astype('<f8')
                a[np.isnan(a)] = np.nan
                hashes[name] = hashlib.sha256(a.tobytes()).hexdigest()
        signatures.append(hashes)
        runs.append({'operations': operations, 'zones': allocation['zones'], 'conservative': {k: conserved[k] for k in ('source_total', 'allocated', 'unallocated', 'conservation_error', 'readback_total')}, 'decoded_sha256': hashes})
    assert signatures[0] == signatures[1]
    assert input_hashes == {p.name: digest(p) for p in inputs.iterdir()}
    assert digest(original) == before
    # Diagnostic presentation only; read the actual published arrays and actual geometry.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 3, figsize=(15, 9))
    displays = [('inputs/source.tif', 'Real stored DEM (m; source datum unverified)', 'terrain'),
                ('run0/classes/result.tif', 'Frozen elevation intervals (derived classes)', 'viridis'),
                ('run0/weights/result.tif', 'Assumed weights; upper-left valid zero', 'magma'),
                ('run0/allocation/result.tif', 'Total / pixel: allocated 1700; unallocated 140', 'viridis'),
                ('run0/integer/result.tif', 'Largest remainder: integer total / pixel', 'viridis'),
                ('run0/coarse/result.tif', 'Conservative 1.7x cell width (known mass only)', 'viridis')]
    for ax, (filename, title, cmap) in zip(axes.ravel(), displays):
        with rio.open(root/filename) as ds:
            a = ds.read(1, masked=True)
            b = ds.bounds
        im = ax.imshow(a, extent=(b.left, b.right, b.bottom, b.top), interpolation='nearest', cmap=cmap)
        for geom, label in zip(regions.geometry[:3], regions.id[:3]):
            gpd.GeoSeries([geom], crs=crs).boundary.plot(ax=ax, color='cyan', linewidth=.7)
            point = geom.representative_point()
            ax.annotate(label, (point.x, point.y), color='black', fontsize=8, bbox=dict(facecolor='white', alpha=.8, edgecolor='none'))
        gpd.GeoSeries([exclusion], crs=crs).boundary.plot(ax=ax, color='red', linewidth=.6)
        ax.set_xlim(b.left, b.right)
        ax.set_ylim(b.bottom, b.top)
        ax.set_title(title, fontsize=10)
        ax.ticklabel_format(style='plain', useOffset=False)
        ax.tick_params(labelsize=7)
        bar = fig.colorbar(im, ax=ax, shrink=.7)
        if '/classes/' in filename:
            bar.set_ticks([1, 2, 3, 4])
        if '/integer/' in filename:
            bar.set_ticks(np.arange(0, int(a.max())+1))
    fig.suptitle('Stage E numerical acceptance | diagnostic regions and weights, no business validation\nCyan: region boundary; red: excluded strip; white: NoData. Outside-grid region has unallocated total 90.', fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, .94))
    fig.savefig(root/'qa.png', dpi=150)
    plt.close(fig)
    summary = {'runs': runs, 'source_unchanged': True, 'inputs_unchanged': True, 'decoded_equal_across_block_sizes': True,
               'data_scope': frozen['data_scope'], 'source_description': args.source_description,
               'elapsed_seconds': time.perf_counter()-start,
               'orchestrator_max_rss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               'children_max_rss': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
               'rss_unit': 'bytes on macOS, KiB on Linux; separate maxima, not process-tree sum',
               'performance_claim': False, 'visual_review': 'pending actual inspection'}
    (root/'acceptance.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(root)


if __name__ == '__main__':
    main()
