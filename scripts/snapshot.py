#!/usr/bin/env python3
"""Local snapshot of a whitelisted XYZ tile service for offline, repeatable map rendering.

Downloads every tile covering `bounds` from `minzoom` to `maxzoom` into <output>/tiles/z/x/y.ext and
writes snapshot.json (contract tile-snapshot v1): provider, URL template, license, attribution,
retrieval time and the SHA-256 of every tile. Renderers read the snapshot, never the service.
Providers come from references/snapshot-providers.json; services whose terms forbid caching are
refused with the reason. No styling, colouring or reprojection happens here.
"""
import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from _delivery import bundle, digest, write_json

REGISTRY = Path(__file__).resolve().parents[1] / 'references' / 'snapshot-providers.json'
REGISTRY_ENV = 'GIS_KIT_SNAPSHOT_PROVIDERS'  # alternative registry file, e.g. a local test server
VERSION = '0.1.0'
MERCATOR_LAT = 85.0511287798066


def providers():
    return json.loads(Path(os.environ.get(REGISTRY_ENV, REGISTRY)).read_text(encoding='utf-8'))['providers']


def tile_range(bounds, zoom):
    """Inclusive x/y tile ranges covering lon/lat bounds at one zoom (XYZ, y down)."""
    west, south, east, north = bounds
    n = 2 ** zoom
    def x(lon): return min(n - 1, max(0, int((lon + 180) / 360 * n)))
    def y(lat):
        lat = max(-MERCATOR_LAT, min(MERCATOR_LAT, lat))
        r = math.radians(lat)
        return min(n - 1, max(0, int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)))
    return range(x(west), x(east) + 1), range(y(north), y(south) + 1)


def tiles(bounds, minzoom, maxzoom):
    for z in range(minzoom, maxzoom + 1):
        xs, ys = tile_range(bounds, z)
        for tx in xs:
            for ty in ys:
                yield z, tx, ty


def fetch(url, attempts=3, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': f'gis-kit/{VERSION} snapshot (offline map rendering)'})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (400, 401, 403, 404, 410) or attempt == attempts - 1:
                raise ValueError(f'HTTP {error.code} for {url}') from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts - 1:
                raise ValueError(f'Fetch failed for {url}: {error}') from error
        time.sleep(1.5 * (attempt + 1))


def validate(params):
    allowed = {'schema_version', 'provider', 'bounds', 'minzoom', 'maxzoom', 'max_tiles', 'delay_seconds'}
    if not isinstance(params, dict) or set(params) - allowed or params.get('schema_version', 1) != 1:
        raise ValueError('Unknown parameter or schema')
    registry = providers()
    name = params.get('provider')
    if name not in registry:
        raise ValueError(f'provider must be one of {", ".join(sorted(registry))}')
    provider = registry[name]
    if provider.get('cache') != 'allowed':
        raise ValueError(f'{provider["name"]} cannot be snapshotted: {provider.get("reason", "caching not permitted")}')
    bounds = params.get('bounds')
    if not (isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, (int, float)) for v in bounds)
            and -180 <= bounds[0] < bounds[2] <= 180 and -90 <= bounds[1] < bounds[3] <= 90):
        raise ValueError('bounds must be [west, south, east, north] in degrees')
    minzoom, maxzoom = params.get('minzoom', 0), params.get('maxzoom')
    if not (isinstance(minzoom, int) and isinstance(maxzoom, int) and 0 <= minzoom <= maxzoom <= provider.get('maxzoom', 18)):
        raise ValueError(f'minzoom/maxzoom must be integers with 0 <= minzoom <= maxzoom <= {provider.get("maxzoom", 18)}')
    limit = params.get('max_tiles', 2000)
    count = sum(1 for _ in tiles(bounds, minzoom, maxzoom))
    if not isinstance(limit, int) or count > limit:
        raise ValueError(f'{count} tiles exceed max_tiles {limit}; shrink bounds or zoom range')
    return name, provider, bounds, minzoom, maxzoom, count


def execute(params, output):
    name, provider, bounds, minzoom, maxzoom, count = validate(params)
    delay = float(params.get('delay_seconds', 0.05))
    started = datetime.now(timezone.utc)
    with bundle(output) as stage:
        listing = []
        for z, tx, ty in tiles(bounds, minzoom, maxzoom):
            data = fetch(provider['url'].format(z=z, x=tx, y=ty))
            relative = Path('tiles') / str(z) / str(tx) / f'{ty}.{provider["format"]}'
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if digest(target) != hashlib.sha256(data).hexdigest():
                raise ValueError(f'Tile readback differs: {relative}')
            listing.append({'z': z, 'x': tx, 'y': ty, 'path': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
            if delay:
                time.sleep(delay)
        listing.sort(key=lambda t: (t['z'], t['x'], t['y']))
        tiles_sha256 = hashlib.sha256('\n'.join(f'{t["path"]} {t["sha256"]}' for t in listing).encode()).hexdigest()
        manifest = {'contract': 'tile-snapshot', 'schema_version': 1,
                    'provider': {'id': name, **{k: provider[k] for k in ('name', 'url', 'license', 'attribution', 'terms_url', 'cache')}},
                    'kind': provider['kind'], **({'encoding': provider['encoding']} if 'encoding' in provider else {}),
                    'format': provider['format'], 'tile_size': provider['tile_size'], 'scheme': 'xyz',
                    'template': f'tiles/{{z}}/{{x}}/{{y}}.{provider["format"]}',
                    'bounds': bounds, 'minzoom': minzoom, 'maxzoom': maxzoom,
                    'retrieved_at': started.isoformat(timespec='seconds'),
                    'tile_count': len(listing), 'tiles_sha256': tiles_sha256, 'tiles': listing, 'record': 'record.json'}
        write_json(stage / 'snapshot.json', manifest)
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'snapshot', 'parameters': params,
                                            'provider': manifest['provider'], 'tile_count': len(listing), 'expected_tiles': count,
                                            'bytes': sum(t['bytes'] for t in listing), 'tiles_sha256': tiles_sha256,
                                            'retrieved_at': manifest['retrieved_at'],
                                            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                            'assumptions': ['Tiles are stored as served; no recompression or reprojection',
                                                            'License and attribution are the registry entry at retrieval time; review the provider terms for reuse']})
    return Path(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--params', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true', help='Validate and report the tile count without downloading')
    args = parser.parse_args()
    try:
        params = json.loads(Path(args.params).read_text(encoding='utf-8'))
        if args.dry_run:
            name, provider, bounds, minzoom, maxzoom, count = validate(params)
            print(json.dumps({'provider': name, 'tiles': count, 'zoom': [minzoom, maxzoom], 'license': provider['license']}, ensure_ascii=False))
            return
        execute(params, args.output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
