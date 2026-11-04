"""Shared correctness exits: direct library calls may only go down, and records name what really ran."""
from collections import Counter
from pathlib import Path
import re
import sys

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))

from _delivery import implementation_hashes, implementation_modules

# Calls that still bypass the shared exits, per script. Lower a number when a call moves behind them;
# a new direct call fails here until it goes through the shared code instead.
ALLOWED = {
    '.to_crs(': {'_core.py': 2},
    'gpd.read_file(': {
        '_core.py': 1, '_raster_numeric.py': 1, '_recipe_operations.py': 1,
        'atlas.py': 1, 'cad.py': 8, 'cartographic-map.py': 1, 'edit.py': 1,
        'georef-delivery.py': 2, 'gpkg.py': 2, 'hydro-review.py': 1, 'indicators.py': 1,
        'join.py': 1, 'nonlinear-georef.py': 1, 'plot-chart.py': 1,
        'plot.py': 1, 'raster-georef.py': 1, 'raster.py': 2, 'scheme.py': 1, 'spatial-inference.py': 1,
        'stats.py': 1, 'suitability.py': 2, 'terrain-detail.py': 1,
        'vector-georef.py': 1,
    },
    '.to_file(': {
        'atlas.py': 1, 'cartographic-map.py': 1, 'geocode.py': 1, 'gpkg.py': 4, 'indicators.py': 1,
        'nonlinear-georef.py': 1, 'raster-annotate.py': 1, 'raster-georef.py': 1, 'split.py': 1,
        'topo.py': 1,
    },
}


def found():
    counts = {pattern: Counter() for pattern in ALLOWED}
    for path in sorted(SCRIPTS.glob('*.py')):
        text = path.read_text(encoding='utf-8')
        for pattern in ALLOWED:
            hits = len(re.findall(re.escape(pattern), text))
            if hits:
                counts[pattern][path.name] = hits
    return counts


def test_direct_calls_only_go_down():
    for pattern, counts in found().items():
        allowed = ALLOWED[pattern]
        grown = {name: n for name, n in counts.items() if n > allowed.get(name, 0)}
        assert not grown, f'New direct {pattern} calls: {grown}; use the shared exit in _core.py'
        stale = {name: allowed[name] for name in allowed if counts.get(name, 0) < allowed[name]}
        assert not stale, f'{pattern} calls went down in {sorted(stale)}; lower ALLOWED to lock that in'


def test_implementation_modules_follow_imports_and_with_name(tmp_path):
    (tmp_path/'tool.py').write_text("import _first as f\nfrom pathlib import Path\nWORKER = Path(__file__).with_name('_worker.py')\n", encoding='utf-8')
    (tmp_path/'_first.py').write_text("def later():\n    from _second import x\n", encoding='utf-8')
    (tmp_path/'_second.py').write_text('x = 1\n', encoding='utf-8')
    (tmp_path/'_worker.py').write_text('import json\n', encoding='utf-8')
    (tmp_path/'_unused.py').write_text('', encoding='utf-8')
    assert sorted(implementation_modules(tmp_path/'tool.py')) == ['_first.py', '_second.py', '_worker.py', 'tool.py']
    before = implementation_hashes(tmp_path/'tool.py')
    (tmp_path/'_second.py').write_text('x = 2\n', encoding='utf-8')
    (tmp_path/'_new.py').write_text('', encoding='utf-8')
    (tmp_path/'_first.py').write_text('import _new\nimport _second\n', encoding='utf-8')
    after = implementation_hashes(tmp_path/'tool.py')
    assert after['_second.py'] != before['_second.py'] and '_new.py' in after


def test_tools_record_their_shared_modules():
    for tool, expected in {'daily.py': {'_core.py', '_daily.py', '_metric.py', '_safe_io.py', '_analysis.py', '_cartography.py', '_conflation.py', '_recipe_operations.py', '_delivery.py'},
                           'raster.py': {'_core.py', '_grid.py', '_raster_display.py', '_scheme.py', '_raster_numeric.py', '_raster_cleanup.py', '_daily.py', '_delivery.py', '_budget.py'},
                           'terrain.py': {'_grid.py', '_delivery.py', '_budget.py'},
                           'terrain-detail.py': {'_grid.py', '_daily.py', '_metric.py', '_safe_io.py', '_delivery.py'},
                           'terrain-backend.py': {'_grid.py', '_grass_worker.py', 'terrain.py', '_delivery.py'},
                           'terrain-cost.py': {'_grid.py', '_walk_worker.py', 'terrain-backend.py', 'terrain.py', '_delivery.py', '_safe_io.py'},
                           'spatial-inference.py': {'_grid.py', '_pysal_worker.py', '_core.py'}}.items():
        modules = implementation_modules(SCRIPTS/tool)
        assert tool in modules and expected <= set(modules), (tool, expected-set(modules))
