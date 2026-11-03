#!/usr/bin/env python3
"""Discover GIS capabilities without importing backends; forward to their native CLI."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import _budget

SCRIPTS = Path(__file__).resolve().parent
VERSION = '0.1.0'
# Importability probes only (find_spec, no import); see references/runtime-environment.md.
FEATURES = {
    'vector': ('geopandas', 'shapely', 'pyogrio', 'pyproj', 'pandas', 'numpy'),
    'raster': ('rasterio', 'numpy'),
    'plot': ('matplotlib', 'PIL', 'yaml'),
    'clustering': ('sklearn', 'scipy'),
    'image-enhance': ('cv2',),
    'network': ('networkx',),
    'cad': ('ezdxf',),
    'cad-ole': ('olefile',),
    'spatial-sql': ('duckdb', 'pyarrow', 'pyproj'),
}


def catalog(scripts: Path = SCRIPTS) -> list[dict]:
    """The installed scripts and reference texts are the catalog; no registry."""
    references = {p.name: p.read_text(encoding='utf-8')
                  for p in sorted((scripts.parent / 'references').glob('*.md'))}
    result = []
    for path in sorted(scripts.glob('*.py')):
        if path.name.startswith('_') or path.name == 'gis.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=path.name)
        summary = (ast.get_docstring(tree) or '').strip().split('\n')[0]
        result.append({'name': path.stem, 'summary': summary,
                       'references': ['references/' + name for name, text in references.items()
                                      if path.name in text]})
    return result


# Run records written by the CAD, package and vector-transform tools, by their manifest / report name.
MANIFESTS = ('cad-inventory', 'cad-convert', 'cad-extract', 'cad-polygonize', 'cad-label', 'cad-xiangyuan', 'cad-overlay', 'cad-fill-compare', 'crs-assignment', 'vector-transform', 'vector-georef-apply', 'esri-package', 'qgis-package', 'arcgis-pro-package', 'arcmap-package', 'style-read')


def capabilities() -> dict:
    """Capabilities document (schema_version 1) for external diagnostics such as gis-plugin; probes without importing backends."""
    features = []
    for name, modules in FEATURES.items():
        missing = [m for m in modules if importlib.util.find_spec(m) is None]
        row = {'name': name, 'available': not missing}
        if missing:
            row['reason'] = 'not importable: ' + ', '.join(missing)
        features.append(row)
    return {'contract': 'capabilities', 'schema_version': 1, 'engine': 'gis-kit', 'engine_version': VERSION,
            'commands': ['capabilities', 'describe', 'list'],
            'contracts': {'describe': {'write': [1]}, 'class-scheme': {'write': [1]}, 'display-raster': {'write': [1]}, 'tile-snapshot': {'write': [1]}, 'admin-chain': {'write': [1]}, 'display-vector': {'write': [1]},
                          **{name: {'write': [1]} for name in MANIFESTS},
                          'memory-budget': {'env': _budget.ENV, 'exit_code': _budget.EXIT_CODE, 'error': [1]}},
            'features': features}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'gis.py list [--json] [--search TEXT]; gis.py capabilities --json; gis.py TOOL --help; '
        'gis.py [--trace DIRECTORY] [--memory-budget SIZE] TOOL ... . Existing script CLIs remain supported.'))
    parser.add_argument('--trace', type=Path, help='Opt-in local execution records; no stdout/stderr or environment capture')
    parser.add_argument('--memory-budget', metavar='SIZE', help=f'Read budget such as 2GiB, or off, passed to the tool as {_budget.ENV}; defaults to '
                        f'{_budget.DEFAULT_SHARE:.0%} of available memory; estimated overruns exit {_budget.EXIT_CODE} before reading')
    parser.add_argument('tool', nargs='?', help='list, capabilities, or a public script name without .py')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    # Tools print JSON and Chinese messages: UTF-8 everywhere, not the Windows code page.
    env = {'PYTHONIOENCODING': 'utf-8', **os.environ}
    if args.memory_budget is not None:
        try:
            env[_budget.ENV] = 'off' if args.memory_budget.strip().lower() in _budget.OFF else str(_budget.parse_size(args.memory_budget))
        except ValueError as error:
            parser.error(str(error))
    if args.tool is None:
        parser.print_help()
        return 0
    if args.tool == 'list':
        listing = argparse.ArgumentParser(prog='gis.py list', description='Read script docstrings and existing reference links; no backend imports')
        listing.add_argument('--json', action='store_true')
        listing.add_argument('--search', default='')
        options = listing.parse_args(args.arguments)
        rows = [row for row in catalog() if options.search.casefold() in json.dumps(row, ensure_ascii=False).casefold()]
        if options.json:
            print(json.dumps({'commands': rows}, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['name']:22} {row['summary']}")
        return 0
    if args.tool == 'capabilities':
        probe = argparse.ArgumentParser(prog='gis.py capabilities', description='Version, contract and importability probes for external diagnostics; no backend imports')
        probe.add_argument('--json', action='store_true', required=True)
        probe.parse_args(args.arguments)
        print(json.dumps(capabilities(), ensure_ascii=False, indent=2))
        return 0
    # Resolve only an existing public script, never a path or a private worker.
    scripts = {p.stem: p for p in SCRIPTS.glob('*.py')
               if not p.name.startswith('_') and p.name != 'gis.py'}
    if args.tool not in scripts:
        parser.error('Unknown tool; use gis.py list')
    command = [sys.executable, str(scripts[args.tool]), *args.arguments]
    try:
        if args.trace is not None:
            from _execution_trace import run_traced
            return run_traced(args.trace, args.tool, args.arguments, command, env)
        result = subprocess.run(command, env=env)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        parser.exit(1, f'ERROR: {error}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    sys.exit(main())
