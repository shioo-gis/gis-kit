"""Stage E: hand-calculated semantics, genuine CLI/readback and transactional failures."""
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from shapely.geometry import box, Point, Polygon

from test_analysis_extensions import tif, rcli, raster
from test_daily import frame, write
import _raster_numeric as n


def read(root, name, band=1):
    with rio.open(root/name/'result.tif') as ds:
        return ds.read(band, masked=True).astype(float).filled(np.nan)


def direct(tmp_path, op, inputs, p):
    output = tmp_path/('direct-'+str(len(list(tmp_path.glob('direct-*')))))
    output.mkdir()
    result = raster.process(op, inputs, p, output)
    return output, result


def test_rules_table_endpoints_unmapped_and_overlap(tmp_path):
    data = np.array([0., 1., 2., 3., np.nan])
    p = {'rules': [{'min': 0, 'max': 1, 'output': 10}, {'min': 1, 'max': 2, 'closed': 'both', 'output': 20}], 'unmapped': 'keep'}
    np.testing.assert_equal(n.classify(data, p), [10, 20, 20, 3, np.nan])
    p['rules'][0]['closed'] = 'both'
    with pytest.raises(ValueError, match='Overlapping'):
        n.classify(data, p)
    p['overlap'] = 'first'
    assert n.classify(data, p)[1] == 10
    p['overlap'] = 'last'
    assert n.classify(data, p)[1] == 20
    p['unmapped'] = 'error'
    with pytest.raises(ValueError, match='Unmapped'):
        n.classify(data, p)
    source = tif(tmp_path/'input.tif', [[0, 1, 2, 3, -99]])
    table = tmp_path/'lookup.csv'
    table.write_text('value,output\n0,0\n1,10\n2,20\n', encoding='utf-8')
    record = rcli(tmp_path, 'classes', 'reclassify', [source], {'table': str(table), 'unmapped': 'keep', 'block_size': 1})
    np.testing.assert_equal(read(tmp_path, 'classes'), [[0, 10, 20, 3, np.nan]])
    assert len(record['inputs']) == 2
    assert '_raster_numeric.py' in record['implementation_sha256']
    rcli(tmp_path, 'unmapped', 'reclassify', [source], {'table': str(table), 'unmapped': 'error'}, success=False)


@pytest.mark.parametrize('method', ['minmax', 'zscore'])
def test_normalize_constant_and_empty(method):
    for value in (0., 7.):
        data = np.array([value, value, np.nan])
        with pytest.raises(ValueError, match='Constant'):
            n.transform(data, {'method': method})
        out, _ = n.transform(data, {'method': method, 'constant': 'zero'})
        np.testing.assert_equal(out, [0, 0, np.nan])
        assert np.isnan(n.transform(data, {'method': method, 'constant': 'nodata'})[0]).all()
    with pytest.raises(ValueError):
        n.transform(np.array([np.nan]), {'method': method})


def test_auto_breaks_functions_and_invalid():
    a = np.array([0., 1., 2., 3., 4., np.nan])
    out, info = n.transform(a, {'method': 'auto_class', 'classes': 2, 'mode': 'equal'})
    np.testing.assert_equal(out, [1, 1, 2, 2, 2, np.nan])
    assert info['breaks'] == [0, 2, 4]
    out, info = n.transform(np.array([1., 1., 1.]), {'method': 'auto_class', 'classes': 4, 'mode': 'quantile'})
    assert info['effective_classes'] == 1
    np.testing.assert_equal(out, [1, 1, 1])
    for method in ('sqrt', 'log'):
        with pytest.raises(ValueError, match='domain'):
            n.transform(np.array([-1.]), {'method': method})
        assert np.isnan(n.transform(np.array([-1.]), {'method': method, 'invalid': 'nodata'})[0]).all()
    with pytest.raises(ValueError, match='overflow'):
        n.transform(np.array([1000.]), {'method': 'exp'})
    np.testing.assert_equal(n.transform(a, {'method': 'invalidate', 'min': 1, 'max': 3})[0], [np.nan, 1, 2, 3, np.nan, np.nan])
    np.testing.assert_equal(n.transform(a, {'method': 'clip', 'min': 1, 'max': 3})[0], [1, 1, 2, 3, 3, np.nan])
    np.testing.assert_equal(n.transform(a, {'method': 'power', 'exponent': 2})[0], a*a)


def test_combine_mask_logic_and_arithmetic():
    a, b = np.array([[0., 2., np.nan]]), np.array([[4., np.nan, 6.]])
    np.testing.assert_equal(n.combine([a, b], {'method': 'sum'}), [[4, np.nan, np.nan]])
    np.testing.assert_equal(n.combine([a, b], {'method': 'mean', 'nodata': 'ignore'}), [[2, 2, 6]])
    np.testing.assert_equal(n.combine([a, b], {'method': 'weighted_mean', 'weights': [1, 3], 'nodata': 'ignore'}), [[3, 2, 6]])
    np.testing.assert_equal(n.combine([a, b], {'method': 'std', 'nodata': 'ignore'}), [[2, 0, 0]])
    for method in ('min', 'max', 'median'):
        assert np.isfinite(n.combine([a, b], {'method': method, 'nodata': 'ignore'})).all()
    for method in ('gt', 'ge', 'eq', 'and', 'or', 'add', 'subtract', 'multiply'):
        assert np.isnan(n.combine([a, b], {'method': method})[0, 1:]).all()
    with pytest.raises(ValueError, match='zero denominator'):
        n.combine([b, a], {'method': 'divide'})
    np.testing.assert_equal(n.combine([b, a], {'method': 'divide', 'invalid': 'nodata'}), [[np.nan]*3])
    condition = np.array([[1., 0., np.nan]])
    np.testing.assert_equal(n.combine([condition, a, np.array([[np.nan, 5., 6.]])], {'method': 'where'}), [[0, 5, np.nan]])
    with pytest.raises(ValueError):
        n.combine([a, b], {'method': '__import__("os")'})
    with pytest.raises(ValueError):
        n.combine([a, b], {'method': 'weighted_mean', 'weights': [0, 0]})
    with pytest.raises(ValueError):
        n.combine([a, b], {'method': 'mean', 'min_valid': 0})


def test_cli_multiband_calibration_types_and_split(tmp_path):
    path = tmp_path/'multi.tif'
    with rio.open(path, 'w', driver='GTiff', height=1, width=3, count=2, dtype='int16', nodata=-99, crs='EPSG:26918', transform=from_origin(0, 1, 1, 1)) as ds:
        ds.write(np.array([[[0, 2, -99]], [[3, 4, 5]]], dtype='int16'))
        ds.scales = (2., 1.)
        ds.offsets = (1., 0.)
    rcli(tmp_path, 'raw-reject', 'bands', [path], {'operands': [{'input': 0, 'band': 1}]}, success=False)
    record = rcli(tmp_path, 'calibrated', 'bands', [path], {'calibrate': True, 'operands': [{'input': 0, 'band': 2}, {'input': 0, 'band': 1}], 'dtype': 'int16', 'nodata_value': -99, 'format': 'COG'})
    np.testing.assert_equal(read(tmp_path, 'calibrated', 2), [[1, 5, np.nan]])
    assert record['calibration'][1] == {'scale': 2., 'offset': 1.}
    source = tmp_path/'calibrated/result.tif'
    rcli(tmp_path, 'arithmetic', 'combine', [source], {'operands': [{'input': 0, 'band': 1}, {'input': 0, 'band': 2}], 'method': 'add'})
    np.testing.assert_equal(read(tmp_path, 'arithmetic'), [[4, 9, np.nan]])
    rcli(tmp_path, 'split', 'bands', [source], {'split': True, 'operands': [{'input': 0, 'band': 2}, {'input': 0, 'band': 1}]})
    assert (tmp_path/'split/band-2.tif').exists()
    for name, params in [('collision', {'dtype': 'int16', 'nodata_value': 3}), ('overflow', {'dtype': 'uint8', 'nodata_value': 255, 'calibrate': True, 'operands': [{'input': 0, 'scale': 1000}]}), ('bad-band', {'operands': [{'input': 0, 'band': 3}]})]:
        rcli(tmp_path, name, 'bands', [source], params, success=False)
    decimals = tif(tmp_path/'decimal.tif', [[1.5, 2.5, -99]])
    rcli(tmp_path, 'round-fail', 'bands', [decimals], {'dtype': 'int16', 'nodata_value': -99}, success=False)
    rcli(tmp_path, 'round', 'bands', [decimals], {'dtype': 'int16', 'nodata_value': -99, 'rounding': 'nearest_even'})
    np.testing.assert_equal(read(tmp_path, 'round'), [[2, 2, np.nan]])


def test_update_fallback_mask_outside_and_alignment(tmp_path):
    a = tif(tmp_path/'a.tif', [[0, -99], [2, -99]])
    b = tif(tmp_path/'b.tif', [[10, 11], [-99, -99]])
    c = tif(tmp_path/'c.tif', [[20, 21], [22, 23]])
    mask = write(tmp_path, 'mask', frame([box(0, 0, 1, 2)], id=['m']))
    rcli(tmp_path, 'fill', 'update', [a, b, c], {'method': 'fill'})
    np.testing.assert_equal(read(tmp_path, 'fill'), [[0, 11], [2, 23]])
    base = {'vector': str(mask), 'coverage': 'center'}
    rcli(tmp_path, 'assign', 'update', [a], dict(base, method='assign', value=9))
    np.testing.assert_equal(read(tmp_path, 'assign'), [[9, np.nan], [9, np.nan]])
    rcli(tmp_path, 'replace', 'update', [a, b], dict(base, method='replace', replacement_nodata='keep'))
    np.testing.assert_equal(read(tmp_path, 'replace'), [[10, np.nan], [2, np.nan]])
    shifted = tif(tmp_path/'shifted.tif', transform=from_origin(.5, 2, 1, 1))
    rcli(tmp_path, 'grid-fail', 'combine', [a, shifted], {'method': 'sum'}, success=False)


def test_rasterization_priority_partial_holes_points(tmp_path):
    source = tif(tmp_path/'grid.tif', [[0, 0], [0, 0]])
    v = write(tmp_path, 'zones', frame([box(0, 0, 1.5, 2), box(1, 0, 2, 2)], id=['a', 'b'], v=[2., 10.]))
    p = {'vector': str(v), 'id': 'id', 'field': 'v', 'coverage': 'fractional', 'overlap': 'first'}
    output, _ = direct(tmp_path, 'rasterize', [source], p)
    with rio.open(output/'result.tif') as ds:
        np.testing.assert_equal(ds.read(1), [[2, 6], [2, 6]])
    p['overlap'] = 'last'
    output, _ = direct(tmp_path, 'rasterize', [source], p)
    with rio.open(output/'result.tif') as ds:
        np.testing.assert_equal(ds.read(1), [[2, 10], [2, 10]])
    p['overlap'] = 'error'
    with pytest.raises(ValueError, match='Overlapping'):
        direct(tmp_path, 'rasterize', [source], p)
    points = write(tmp_path, 'points', frame([Point(.5, 1.5)], id=['p'], v=[4]))
    rcli(tmp_path, 'point-burn', 'rasterize', [source], dict(p, vector=str(points), coverage='center'))
    np.testing.assert_equal(read(tmp_path, 'point-burn'), [[4, np.nan], [np.nan, np.nan]])
    hole = Polygon(box(0, 0, 2, 2).exterior.coords, [box(.5, .5, 1.5, 1.5).exterior.coords])
    with rio.open(source) as ds:
        np.testing.assert_allclose(n.coverage(hole, ds, 'fractional', [100]), np.full((2, 2), .75))


def allocation_params(vector, **kwargs):
    return dict(vector=str(vector), id='id', total_field='total', method='area', quantity='total', assumption='redistribute_over_eligible_support', **kwargs)


def test_allocation_fraction_zero_missing_overlap_and_integer(tmp_path):
    domain = tif(tmp_path/'grid.tif', [[0, 0], [0, -99]])
    zones = write(tmp_path, 'regions', frame([box(0, 0, 1.5, 2)], id=['a'], total=[50.]))
    p = allocation_params(zones)
    record = rcli(tmp_path, 'area', 'allocate', [domain], p)
    np.testing.assert_allclose(read(tmp_path, 'area'), [[20, 10], [20, np.nan]])
    assert record['zones'][0]['readback_total'] == 50
    p['method'] = 'uniform'
    output, _ = direct(tmp_path, 'allocate', [domain], p)
    with rio.open(output/'result.tif') as ds:
        np.testing.assert_allclose(ds.read(1, masked=True).compressed(), [50/3]*3)
    weight = tif(tmp_path/'weights.tif', [[1, 0], [3, 1]])
    p.update(method='weighted', weight_kind='per_area')
    rcli(tmp_path, 'weighted', 'allocate', [domain, weight], p)
    np.testing.assert_equal(read(tmp_path, 'weighted'), [[12.5, 0], [37.5, np.nan]])
    zero = tif(tmp_path/'zero.tif', [[0, 0], [0, 0]])
    record = rcli(tmp_path, 'zero', 'allocate', [domain, zero], p)
    assert record['zones'][0]['unallocated'] == 50
    zones2 = write(tmp_path, 'regions2', frame([box(0, 0, 2, 2), box(1, 0, 2, 2)], id=['a', 'b'], total=[5, 4]))
    rcli(tmp_path, 'overlap-fail', 'allocate', [domain], allocation_params(zones2), success=False)
    record = rcli(tmp_path, 'independent', 'allocate', [domain], allocation_params(zones2, overlap='independent', integer=True))
    assert [x['readback_total'] for x in record['zones']] == [5, 4]
    with rio.open(tmp_path/'independent/contributions.tif') as ds:
        np.testing.assert_equal(ds.read(1, masked=True).filled(-99), [[2, 2], [1, -99]])
    exclusion = write(tmp_path, 'exclude', frame([box(0, 0, .5, 2)], id=['e']))
    p = allocation_params(zones, exclude=str(exclusion))
    rcli(tmp_path, 'excluded', 'allocate', [domain], p)
    np.testing.assert_allclose(read(tmp_path, 'excluded'), [[50/3, 50/3], [50/3, np.nan]])


def test_conservative_nondivisible_and_density(tmp_path):
    source = tif(tmp_path/'source.tif', [[2, 4], [6, -99]], from_origin(0, 2, 1, 1))
    reference = tif(tmp_path/'target.tif', np.zeros((3, 3)), from_origin(0, 2, .75, .75))
    p = {'reference': str(reference), 'quantity': 'total', 'assumption': 'uniform_within_source_pixel'}
    record = rcli(tmp_path, 'resampled', 'redistribute', [source], p)
    assert record['source_total'] == 12
    assert record['allocated'] == 12
    assert np.nansum(read(tmp_path, 'resampled')) == 12
    with rio.open(tmp_path/'resampled/coverage.tif') as support:
        fraction = support.read(1, masked=True)
        assert fraction[1, 1] == pytest.approx(5/9)
        assert fraction[0, 0] == 1 and fraction.mask[2, 2]
        assert fraction.sum()*.75**2 == pytest.approx(3, abs=1e-12)
    small = tif(tmp_path/'small.tif', [[0]], from_origin(.5, 1.5, 1, 1))
    output, record = direct(tmp_path, 'redistribute', [source], dict(p, reference=str(small)))
    assert record['allocated'] == 3 and record['unallocated'] == 9
    output, record = direct(tmp_path, 'redistribute', [source], dict(p, quantity='density'))
    with rio.open(output/'result.tif') as ds:
        assert np.nansum(ds.read(1))*.75**2 == pytest.approx(12, rel=1e-12, abs=1e-9)
    with pytest.raises(ValueError):
        direct(tmp_path, 'redistribute', [source], dict(p, quantity='ratio'))


def test_area_units_invalid_geographic_and_guards(tmp_path):
    path = tif(tmp_path/'feet.tif', [[1]], from_origin(0, 10, 10, 10), crs='EPSG:2263')
    with rio.open(path) as ds:
        assert n.area_factor(ds)*100 == pytest.approx(9.290341161327486)
    geo = tif(tmp_path/'geo.tif', [[1]], crs='EPSG:4326')
    zones = write(tmp_path, 'zones', gpd.GeoDataFrame({'id': ['a'], 'total': [1]}, geometry=[box(0, 0, 1, 1)], crs='EPSG:4326'))
    rcli(tmp_path, 'geo-fail', 'allocate', [geo], allocation_params(zones), success=False)
    rcli(tmp_path, 'memory-fail', 'combine', [path, path], {'method': 'sum', 'max_elements': 1}, success=False)
    bad = tif(tmp_path/'bad.tif', [[1e300]])
    rcli(tmp_path, 'dtype-fail', 'bands', [bad], {'dtype': 'float32'}, success=False)
    rcli(tmp_path, 'overflow-fail', 'transform', [bad], {'method': 'exp'}, success=False)
    assert not list(tmp_path.glob('.raster-*'))


def test_mixed_pixel_contributions_and_input_order(tmp_path):
    domain = tif(tmp_path/'one.tif', [[0]], from_origin(0, 1, 1, 1))
    zones = frame([box(0, 0, .25, 1), box(.25, 0, 1, 1)], id=['left', 'right'], total=[3, 7])
    a = write(tmp_path, 'a', zones)
    b = write(tmp_path, 'b', zones.iloc[::-1])
    for name, vector in [('first', a), ('second', b)]:
        rcli(tmp_path, name, 'allocate', [domain], allocation_params(vector))
    np.testing.assert_equal(read(tmp_path, 'first'), [[10]])
    assert (tmp_path/'first/contributions.tif').read_bytes() == (tmp_path/'second/contributions.tif').read_bytes()
    with rio.open(tmp_path/'first/contributions.tif') as ds:
        np.testing.assert_equal(ds.read()[:, 0, 0], [3, 7])
        assert ds.descriptions == ('left', 'right')
    # The aggregate's fractional zone sums would be 2.5 and 7.5, not source totals.
    assert 10*.25 != 3


def test_fractional_rasterization_disjoint_pixel_and_reversed_priority(tmp_path):
    source = tif(tmp_path/'grid.tif', [[0]], from_origin(0, 1, 1, 1))
    vector = write(tmp_path, 'v', frame([box(0, 0, .25, 1), box(.25, 0, 1, 1)], id=['a', 'b'], value=[4, 8]))
    p = dict(vector=str(vector), field='value', id='id', overlap='error', coverage='fractional')
    rcli(tmp_path, 'fractions', 'rasterize', [source], p)
    np.testing.assert_equal(read(tmp_path, 'fractions'), [[7]])
    rcli(tmp_path, 'touch-fail', 'rasterize', [source], dict(p, coverage='all_touched'), success=False)


def test_normalization_cli_and_float_roundtrip(tmp_path):
    source = tif(tmp_path/'values.tif', [[0, 2], [4, -99]])
    rcli(tmp_path, 'normalize', 'transform', [source], {'method': 'minmax', 'dtype': 'float32'})
    np.testing.assert_equal(read(tmp_path, 'normalize'), [[0, .5], [1, np.nan]])
    rcli(tmp_path, 'autoclass', 'transform', [source], {'method': 'auto_class', 'mode': 'quantile', 'classes': 2})
    np.testing.assert_equal(read(tmp_path, 'autoclass'), [[1, 2], [2, np.nan]])
    bad = tif(tmp_path/'empty.tif', [[-99]])
    rcli(tmp_path, 'empty-fail', 'transform', [bad], {'method': 'minmax'}, success=False)


@pytest.mark.parametrize('writer,method', [('write', 'minmax'), ('process_windows', 'sqrt')])
def test_atomic_late_failure_and_source_change(tmp_path, monkeypatch, writer, method):
    from argparse import Namespace
    source = tif(tmp_path/'source.tif', [[0, 1]])
    spec = tmp_path/'params.json'
    spec.write_text(json.dumps({'method': method}), encoding='utf-8')
    args = Namespace(input=[str(source)], params=str(spec), output=str(tmp_path/'output'), operation='transform')
    original = getattr(raster.numeric, writer)
    def fail(*a, **kw):
        original(*a, **kw)
        raise OSError('Injected disk failure after complete file write')
    monkeypatch.setattr(raster.numeric, writer, fail)
    with pytest.raises(OSError, match='Injected'):
        raster.execute(args)
    assert not (tmp_path/'output').exists() and not list(tmp_path.glob('.raster-*'))
    def mutate(*a, **kw):
        result = original(*a, **kw)
        with rio.open(source, 'r+') as ds:
            ds.write(np.array([[0., 2.]]), 1)
        return result
    monkeypatch.setattr(raster.numeric, writer, mutate)
    with pytest.raises(ValueError, match='Inputs changed'):
        raster.execute(args)
    assert not (tmp_path/'output').exists() and not list(tmp_path.glob('.raster-*'))


@pytest.mark.parametrize('total,weights', [(-1, [[1, 1]]), (1, [[-1, 1]]), (.5, [[1, 1]])])
def test_allocation_fail_closed_totals_weights_and_integer(tmp_path, total, weights):
    source = tif(tmp_path/'domain.tif', [[0, 0]], from_origin(0, 1, 1, 1))
    weight = tif(tmp_path/'weights.tif', weights, from_origin(0, 1, 1, 1))
    zones = write(tmp_path, 'z', frame([box(0, 0, 2, 1)], id=['z'], total=[total]))
    p = allocation_params(zones, integer=True)
    p.update(method='weighted', weight_kind='per_area')
    rcli(tmp_path, 'failure', 'allocate', [source, weight], p, success=False)


def test_allocation_zero_source_and_no_support(tmp_path):
    source = tif(tmp_path/'domain.tif', [[-99]], from_origin(0, 1, 1, 1))
    zones = write(tmp_path, 'zones', frame([box(0, 0, 1, 1), box(2, 0, 3, 1)], id=['a', 'b'], total=[0, 10]))
    record = rcli(tmp_path, 'allocation', 'allocate', [source], allocation_params(zones))
    assert [r['unallocated'] for r in record['zones']] == [0, 10]
    assert np.isnan(read(tmp_path, 'allocation')).all()
