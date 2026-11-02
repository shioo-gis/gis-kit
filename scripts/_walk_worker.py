"""Internal fixed GRASS r.walk worker; reverse cost uses transposed edge elevations."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import time


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')); work = Path(job['work']); p = job['params']
    started = time.perf_counter(); calls = []
    def run(module, *args):
        result = subprocess.run([module, *map(str, args), '--quiet'], capture_output=True, text=True)
        calls.append(dict(module=module, returncode=result.returncode))
        if result.returncode: raise RuntimeError(f'GRASS module failed: {module}')
    for name in ('dem', 'friction'):
        run('r.in.gdal', f'input={work/name}.tif', f'output={name}')
    run('g.region', 'raster=dem')
    run('r.mapcalc', 'expression=reverse_dem = -dem')
    for name, dem, point in [('from_start', 'dem', p['start']), ('to_end', 'reverse_dem', p['end'])]:
        args = ['outdir=back_direction'] if name == 'from_start' else []
        run('r.walk', f'elevation={dem}', 'friction=friction', f'output={name}', *args,
            f'start_coordinates={point[0]},{point[1]}', 'walk_coeff='+','.join(map(str, p['walk_coeff'])),
            f"slope_factor={p['slope_factor']}", f"lambda={p['friction_lambda']}", 'memory=256')
    for name in ('from_start', 'to_end', 'back_direction'):
        run('r.out.gdal', f'input={name}', f'output={work/name}.tif', 'format=GTiff', 'type=Float64', 'nodata=-999999999', 'createopt=COMPRESS=DEFLATE')
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)
    (work/'worker.json').write_text(json.dumps(dict(calls=calls, wall_seconds=time.perf_counter()-started, completed_child_max_rss_bytes=rss, scope='maximum completed module child, not concurrent process tree')), encoding='utf-8')


if __name__ == '__main__': main()
