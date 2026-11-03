"""Internal GRASS session worker. Fixed operations only; no arbitrary command input."""
import json
from pathlib import Path
import subprocess
import sys
import time
from _peak_rss import peak_rss_bytes


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    work = Path(job['work']); p = job['params']; started = time.perf_counter()
    calls = []
    def run(module, *args):
        result = subprocess.run([module, *map(str, args), '--quiet'], capture_output=True, text=True)
        calls.append({'module': module, 'returncode': result.returncode})
        if result.returncode:
            raise RuntimeError(f'GRASS module failed: {module}; exit {result.returncode}')
    run('r.in.gdal', f"input={job['input']}", 'output=dem')
    run('g.region', 'raster=dem')
    names = []
    if job['operation'] == 'hydrology':
        flags = ['-s'] if p['flow_method'] == 'D8' else []
        run('r.watershed', *flags, 'elevation=dem', 'accumulation=accumulation', 'drainage=drainage',
            'basin=basins', 'stream=streams', f"threshold={int(p['threshold_cells'])}", 'convergence=5')
        names = ['accumulation', 'drainage', 'basins', 'streams']
        if 'outlet' in p:
            run('r.water.outlet', 'input=drainage', 'output=catchment', f"coordinates={p['outlet'][0]},{p['outlet'][1]}")
            names.append('catchment')
    elif job['operation'] == 'viewshed':
        for i, observer in enumerate(p['observers']):
            name = f'view_{i}'; names.append(name)
            run('r.viewshed', '-b', 'input=dem', f'output={name}',
                f"coordinates={observer['x']},{observer['y']}", f"observer_elevation={observer['height_m']}",
                f"target_elevation={p['target_height_m']}", f"max_distance={p['max_distance_m']}",
                f'directory={work}', 'memory=256')
    else:
        raise ValueError('Unsupported worker operation')
    for name in names:
        run('r.out.gdal', f'input={name}', f'output={work/name}.tif', 'format=GTiff',
            'type=Float64', 'nodata=-999999999', 'createopt=COMPRESS=DEFLATE')
    (work/'worker.json').write_text(json.dumps({'calls': calls, 'wall_seconds': time.perf_counter()-started,
        'child_max_rss_bytes': peak_rss_bytes(children=True),
        'rss_scope': 'maximum of completed module children; not a concurrent process-tree peak'}), encoding='utf-8')


if __name__ == '__main__':
    main()
