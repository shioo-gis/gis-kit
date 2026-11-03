"""Coverage backend regressions; unittest permits validation without installing pytest."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import Polygon, MultiPolygon, Point, box

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from _analysis import cleanup, cleanup_adopt, validate_coverage

AVAILABLE = callable(getattr(shapely, 'coverage_simplify', None)) and shapely.geos_version >= (3, 12, 0)


def fixture():
    seam = [(5, 0)] + [(5 + (.18 if i % 2 else -.18), float(i)) for i in range(1, 10)] + [(5, 10)]
    left = Polygon([(0, 0)] + seam + [(0, 10)], [list(box(1, 2, 2, 3).exterior.coords)])
    right = Polygon([seam[0], (10, 0), (10, 10)] + list(reversed(seam[1:])))
    islands = MultiPolygon([box(12, 1, 13, 2), box(12, 4, 13, 5)])
    return gpd.GeoDataFrame({'id': ['west', 'east', 'islands'], 'value': [10, 20, 30]},
                            geometry=[left, right, islands], crs=26918)


def params(**extra):
    return {'id': 'id', 'method': 'coverage_simplify', 'tolerance_m': 1, **extra}


def cli(root, name, operation, source, parameters, right=None, success=True):
    spec = root / (name + '.json'); spec.write_text(json.dumps(parameters), encoding='utf-8')
    dest = root / name
    command = [sys.executable, str(SCRIPTS / 'daily.py'), operation, str(source), '--params', str(spec), '--output', str(dest)]
    if right is not None: command += ['--right', str(right)]
    proc = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    if (proc.returncode == 0) != success: raise AssertionError(proc.stdout + proc.stderr)
    if not success:
        if dest.exists(): raise AssertionError('Failed operation published a bundle')
        return proc.stderr
    return dest, json.loads((dest / 'record.json').read_text(encoding='utf-8'))


@unittest.skipUnless(AVAILABLE, 'Requires Shapely 2.1 / GEOS 3.12; execute with the coverage runtime')
class CoverageCleanupTests(unittest.TestCase):
    def test_shared_edges_holes_islands_and_impact(self):
        before = fixture(); after, report = cleanup(before, None, params())
        self.assertTrue(shapely.coverage_is_valid(after.geometry.to_numpy()))
        self.assertTrue(before.geometry.union_all().equals(after.geometry.union_all()))
        self.assertLess(report['coverage']['vertices_after'], report['coverage']['vertices_before'])
        self.assertEqual(after.id.tolist(), before.id.tolist())
        self.assertEqual(after.value.tolist(), before.value.tolist())
        self.assertEqual(len(after.geometry.iloc[0].interiors), 1)
        self.assertEqual(len(after.geometry.iloc[2].geoms), 2)
        self.assertEqual(after.geometry.iloc[0].intersection(after.geometry.iloc[1]).area, 0)
        self.assertGreater(after.geometry.iloc[0].boundary.intersection(after.geometry.iloc[1].boundary).length, 0)
        self.assertEqual(report['status'], 'candidate'); self.assertFalse(report['adopted'])
        self.assertIn('not a displacement limit', report['coverage']['tolerance_semantics'])

    def test_row_and_vertex_order(self):
        before = fixture(); first, _ = cleanup(before, None, params())
        reordered = before.iloc[::-1].copy(); reordered.geometry = shapely.reverse(reordered.geometry.to_numpy())
        second, _ = cleanup(reordered, None, params())
        lookup = second.set_index('id')
        for _, row in first.iterrows(): self.assertTrue(row.geometry.equals(lookup.loc[row.id].geometry))

    def test_overlap_rejected(self):
        data = gpd.GeoDataFrame({'id': ['a', 'b']}, geometry=[box(0, 0, 2, 2), box(1, 0, 3, 2)], crs=26918)
        with self.assertRaisesRegex(ValueError, 'Invalid coverage'): cleanup(data, None, params())

    def test_unmatched_shared_edges_rejected(self):
        data = gpd.GeoDataFrame({'id': ['a', 'b']}, geometry=[box(0, 0, 1, 2), Polygon([(1, 0), (2, 0), (2, 2), (1, 2), (1, 1)])], crs=26918)
        with self.assertRaisesRegex(ValueError, 'Invalid coverage'): cleanup(data, None, params())

    def test_gap_policy(self):
        data = gpd.GeoDataFrame({'id': ['a', 'b']}, geometry=[box(0, 0, 1, 2), box(1.05, 0, 2, 2)], crs=26918)
        cleanup(data, None, params())  # holes/gaps allowed unless the caller supplies a gap threshold
        with self.assertRaisesRegex(ValueError, 'Invalid coverage'): cleanup(data, None, params(gap_width_m=.1))

    def test_invalid_parameters_and_types(self):
        for options in ({'tolerance_m': 0}, {'gap_width_m': -1}, {'gap_width_m': float('nan')}, {'simplify_boundary': 'false'}):
            with self.subTest(options=options), self.assertRaises(ValueError): cleanup(fixture(), None, params(**options))
        point = gpd.GeoDataFrame({'id': ['p']}, geometry=[Point(0, 0)], crs=26918)
        with self.assertRaisesRegex(ValueError, 'Polygon'): cleanup(point, None, params())
        measured = gpd.GeoDataFrame({'id': ['m']}, geometry=[shapely.from_wkt('POLYGON M ((0 0 1, 1 0 1, 1 1 1, 0 0 1))')], crs=26918)
        with self.assertRaisesRegex(ValueError, '2D'): cleanup(measured, None, params())

    def test_us_survey_feet(self):
        metres = fixture(); feet = metres.copy().set_crs(2263, allow_override=True)
        factor = feet.crs.axis_info[0].unit_conversion_factor
        feet.geometry = shapely.transform(feet.geometry.to_numpy(), lambda xy: xy / factor)
        _, a = cleanup(metres, None, params()); _, b = cleanup(feet, None, params())
        self.assertEqual(a['coverage']['vertices_after'], b['coverage']['vertices_after'])
        for x, y in zip(a['impacts'], b['impacts']):
            self.assertAlmostEqual(x['displacement_m'], y['displacement_m'], places=10)
            self.assertAlmostEqual(x['area_delta_m2'], y['area_delta_m2'], places=10)

    def test_explicit_outer_boundary_simplification(self):
        source = gpd.GeoDataFrame({'id': ['a']}, geometry=[Polygon([(0, 0), (5, -.1), (10, 0), (10, 10), (0, 10)])], crs=26918)
        fixed, _ = cleanup(source, None, params())
        changed, report = cleanup(source, None, params(simplify_boundary=True))
        self.assertTrue(source.geometry.iloc[0].equals(fixed.geometry.iloc[0]))
        self.assertGreater(report['coverage']['footprint_change_m2'], 0)
        self.assertFalse(source.geometry.iloc[0].equals(changed.geometry.iloc[0]))

    def test_adoption_rechecks_coverage_and_preserves_original_attributes(self):
        source = fixture(); candidate, _ = cleanup(source, None, params()); candidate['value'] = 999
        approval = {'id': 'id', 'approved_by': 'synthetic test', 'source_sha256': 'source', 'candidate_sha256': 'candidate',
                    'max_displacement_m': 2, 'max_area_change_m2': 20, 'rules': [], 'require_coverage': True}
        result, report = cleanup_adopt(source, candidate, approval, ['source', 'candidate'])
        self.assertTrue(report['coverage']['output_valid']); self.assertEqual(result.value.tolist(), [10, 20, 30])
        broken = candidate.copy(); broken.loc[broken.index[0], 'geometry'] = broken.geometry.iloc[0].buffer(.05)
        with self.assertRaises(ValueError): cleanup_adopt(source, broken, approval, ['source', 'candidate'])
        with self.assertRaisesRegex(ValueError, 'stale'): cleanup_adopt(source, candidate, approval, ['changed', 'candidate'])

    def test_real_cli_failure_and_gpkg_readback(self):
        with tempfile.TemporaryDirectory(prefix='coverage-cli-') as raw:
            root = Path(raw); source = root / 'source.gpkg'; fixture().to_file(source, driver='GPKG', engine='pyogrio')
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            dest, record = cli(root, 'candidate', 'cleanup', source, params())
            data = gpd.read_file(dest / 'result.gpkg')
            self.assertTrue(shapely.coverage_is_valid(data.geometry.to_numpy()))
            self.assertEqual(record['status'], 'hold'); self.assertTrue(record['details']['coverage']['output_valid'])
            cli(root, 'rejected', 'cleanup', source, params(gap_width_m=-1), success=False)
            self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertFalse(list(root.glob('.daily-*')))


if __name__ == '__main__': unittest.main()
