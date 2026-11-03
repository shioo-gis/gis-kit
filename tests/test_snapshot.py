"""Tile snapshots from a whitelisted provider, served here by a local HTTP server."""
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import snapshot as S


@pytest.fixture
def server(tmp_path):
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if 'missing' in self.path:
                self.send_error(404)
                return
            body = f'tile {self.path}'.encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    entry = dict(kind='raster', format='png', tile_size=256, maxzoom=8, license='test only', attribution='Local test tiles', terms_url='http://example.invalid')
    registry = tmp_path/'providers.json'
    registry.write_text(json.dumps({'schema_version': 1, 'providers': {
        'local': {**entry, 'name': 'Local', 'url': base + '/{z}/{x}/{y}.png', 'cache': 'allowed'},
        'gaps': {**entry, 'name': 'Gaps', 'url': base + '/missing/{z}/{x}/{y}.png', 'cache': 'allowed'},
        'closed': {'name': 'Closed', 'url': base + '/{z}/{x}/{y}.png', 'kind': 'raster', 'cache': 'forbidden', 'reason': 'terms forbid caching'}}}), encoding='utf-8')
    yield registry, hits
    httpd.shutdown()


def run(tmp_path, registry, params, *extra):
    (tmp_path/'p.json').write_text(json.dumps(params), encoding='utf-8')
    return subprocess.run([sys.executable, str(SCRIPTS/'snapshot.py'), '--params', str(tmp_path/'p.json'), '--output', str(tmp_path/'snap'), *extra],
                          capture_output=True, text=True, env={**os.environ, S.REGISTRY_ENV: str(registry)})


def test_tile_ranges_cover_bounds():
    assert S.tile_range([-180, -85, 180, 85], 1) == (range(0, 2), range(0, 2))
    # z7 tiles are 2.8125° wide: 6–10.5°E spans x 66 (5.625–8.4375°) and 67 (8.4375–11.25°).
    xs, ys = S.tile_range([6.0, 45.8, 10.5, 47.8], 7)
    assert list(xs) == [66, 67] and list(ys) == [44, 45]


def test_snapshot_writes_tiles_and_manifest(tmp_path, server):
    registry, hits = server
    result = run(tmp_path, registry, {'provider': 'local', 'bounds': [6.0, 45.8, 10.5, 47.8], 'minzoom': 5, 'maxzoom': 7, 'delay_seconds': 0})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path/'snap'/'snapshot.json').read_text(encoding='utf-8'))
    assert manifest['contract'] == 'tile-snapshot' and manifest['schema_version'] == 1
    assert manifest['tile_count'] == len(hits) == 1 + 1 + 4
    assert manifest['provider']['attribution'] == 'Local test tiles' and manifest['template'] == 'tiles/{z}/{x}/{y}.png'
    for tile in manifest['tiles']:
        data = (tmp_path/'snap'/tile['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == tile['sha256'] and data == f'tile /{tile["z"]}/{tile["x"]}/{tile["y"]}.png'.encode()
    lines = '\n'.join(f'{t["path"]} {t["sha256"]}' for t in manifest['tiles'])
    assert manifest['tiles_sha256'] == hashlib.sha256(lines.encode()).hexdigest()
    assert json.loads((tmp_path/'snap'/'record.json').read_text(encoding='utf-8'))['expected_tiles'] == 6


def test_forbidden_provider_oversized_request_and_missing_tiles_fail_without_output(tmp_path, server):
    registry, hits = server
    closed = run(tmp_path, registry, {'provider': 'closed', 'bounds': [0, 0, 1, 1], 'maxzoom': 1})
    assert closed.returncode == 1 and 'terms forbid caching' in closed.stderr and not hits
    big = run(tmp_path, registry, {'provider': 'local', 'bounds': [-180, -80, 180, 80], 'maxzoom': 6, 'max_tiles': 100})
    assert big.returncode == 1 and 'exceed max_tiles' in big.stderr and not hits
    dry = run(tmp_path, registry, {'provider': 'local', 'bounds': [6.0, 45.8, 10.5, 47.8], 'minzoom': 5, 'maxzoom': 7}, '--dry-run')
    assert dry.returncode == 0 and json.loads(dry.stdout)['tiles'] == 6 and not hits
    gaps = run(tmp_path, registry, {'provider': 'gaps', 'bounds': [0, 0, 1, 1], 'maxzoom': 1, 'delay_seconds': 0})
    assert gaps.returncode == 1 and 'HTTP 404' in gaps.stderr
    assert not (tmp_path/'snap').exists(), 'a failed snapshot publishes nothing'


def test_registry_lists_terms_for_every_provider():
    for name, entry in S.providers().items():
        assert entry['cache'] in ('allowed', 'forbidden') and entry.get('terms_url'), name
        if entry['cache'] == 'allowed':
            assert entry['attribution'] and entry['license'] and '{z}' in entry['url'], name
        else:
            assert entry['reason'], name
