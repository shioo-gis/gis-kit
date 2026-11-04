"""Reprojection's silent failures: flipped features, collapsed sparse edges, non-finite coordinates."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform
import shapely
from shapely.geometry import Point, Polygon, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))

from _core import reproject

POLAR = 'EPSG:3031'
# In the polar CRS x grows towards 90E and y towards 0E, so the 180 meridian is the ray x=0, y<0.
AROUND_POLE = box(-500_000, -500_000, 500_000, 500_000)
ACROSS_SEAM = box(-300_000, -2_300_000, 300_000, -1_700_000)
BESIDE_SEAM = box(1_000_000, 1_000_000, 1_500_000, 1_500_000)


def layer(path, geometries, crs=POLAR, **fields):
    frame = gpd.GeoDataFrame({'id': range(len(geometries)), **fields}, geometry=list(geometries), crs=crs)
    frame.to_file(path, driver='GPKG')
    return path


def tool(name, *arguments, check=None):
    done = subprocess.run([sys.executable, str(SCRIPTS/f'{name}.py'), *map(str, arguments)], capture_output=True, text=True, encoding='utf-8')
    if check is not None:
        assert done.returncode == check, done.stderr
    return done


def frame(geometries, crs=POLAR):
    return gpd.GeoDataFrame({'id': range(len(geometries))}, geometry=list(geometries), crs=crs)


def test_pole_and_seam_are_refused_in_source_coordinates():
    with pytest.raises(ValueError, match=r'row position\(s\) 0 touch a pole.*row position\(s\) 1 cross the 180 degree'):
        reproject(frame([AROUND_POLE, ACROSS_SEAM, BESIDE_SEAM]), 'EPSG:4326', purpose='publish')
    result = reproject(frame([BESIDE_SEAM]), 'EPSG:4326', purpose='publish')
    assert result.crs.to_epsg() == 4326 and result.geometry.is_valid.all()
    assert result.total_bounds[2]-result.total_bounds[0] < 90


def test_measuring_tools_refuse_instead_of_taking_bounds_from_a_flip():
    import _metric
    import _network_access
    import topo
    with pytest.raises(ValueError, match='cross the 180 degree'):
        _metric._local_utm_crs(frame([ACROSS_SEAM]))
    with pytest.raises(ValueError, match='touch a pole'):
        _network_access.projected(frame([AROUND_POLE]), 'EPSG:4326', ['Polygon'])
    with pytest.raises(ValueError, match='cross the 180 degree'):
        topo._coverage_union(frame([ACROSS_SEAM]), target_crs='EPSG:4326', name='Expected coverage')


def test_a_site_across_the_seam_is_refused_and_the_error_names_the_point_option(tmp_path):
    spec = importlib.util.spec_from_file_location('admin_chain', SCRIPTS/'admin-chain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    site = layer(tmp_path/'site.gpkg', [ACROSS_SEAM])
    with pytest.raises(ValueError, match='--point'):
        module.load_site(argparse.Namespace(point=None, site=site, site_layer=None))


def test_valid_looking_flip_is_what_the_refusal_prevents():
    # The plain transform of a seam-crossing polygon stays valid while spanning most of the globe.
    plain = frame([ACROSS_SEAM]).to_crs('EPSG:4326')
    assert plain.geometry.is_valid.all() and plain.total_bounds[2]-plain.total_bounds[0] > 300
    with pytest.raises(ValueError, match='without an error'):
        reproject(frame([ACROSS_SEAM]), 'EPSG:4326', purpose='publish')


def test_points_and_touching_the_seam_pass():
    points = frame([Point(0, 0), Point(0, -2_000_000)])
    assert len(reproject(points, 'EPSG:4326', purpose='publish')) == 2
    # An edge lying on the wrap meridian does not cross it; the Mercator world edge is the same case.
    edge = frame([box(170, 0, 180, 10)], 'EPSG:4326')
    assert reproject(edge, 'EPSG:3857', purpose='publish').total_bounds[2] == pytest.approx(20037508.34, abs=1)
    world_edge = frame([box(19_000_000, 0, 20037508.342789244, 1_000_000)], 'EPSG:3857')
    assert reproject(world_edge, 'EPSG:4326', purpose='publish').total_bounds[2] == pytest.approx(180)


def test_seam_follows_the_target_central_meridian():
    crossing = frame([box(-80, 20, -70, 30)], 'EPSG:4326')
    with pytest.raises(ValueError, match='cross the -75 degree'):
        reproject(crossing, '+proj=aea +lat_1=25 +lat_2=47 +lon_0=105', purpose='publish')
    assert len(reproject(frame([box(100, 20, 110, 30)], 'EPSG:4326'), '+proj=aea +lat_1=25 +lat_2=47 +lon_0=105', purpose='publish')) == 1
    # A zone near the wrap meridian holds features across it.
    across = gpd.GeoDataFrame(geometry=[box(650_000, 8_000_000, 900_000, 8_100_000)], crs='EPSG:32760')
    with pytest.raises(ValueError, match='cross the 180 degree'):
        reproject(across, 'EPSG:4326', purpose='publish')


def test_targets_without_a_seam_accept_the_same_features():
    assert len(reproject(frame([AROUND_POLE, ACROSS_SEAM]), 'EPSG:3976', purpose='publish')) == 2
    # Same-datum geographic moves have no singular pole either.
    cap = frame([box(-180, -90, 180, -75)], 'EPSG:4326')
    assert len(reproject(cap, 'EPSG:4490', purpose='publish')) == 1


def test_sparse_geographic_edges_are_densified_towards_a_bending_target():
    cap = frame([box(-180, -90, 180, -75)], 'EPSG:4326')
    facts = []
    dense = reproject(cap, POLAR, purpose='align', facts=facts)
    assert dense.geometry.is_valid.all() and [f['code'] for f in facts][0] == 'edges_densified'
    radius = abs(cap.iloc[[0]].set_geometry([Point(0, -75)]).to_crs(POLAR).geometry.iloc[0].y)
    assert dense.geometry.iloc[0].area == pytest.approx(np.pi*radius**2, rel=0.01)
    assert cap.to_crs(POLAR).geometry.iloc[0].area < dense.geometry.iloc[0].area/10
    short = frame([box(10, 10, 10.5, 10.5)], 'EPSG:4326')
    facts = []
    assert shapely.get_num_coordinates(reproject(short, 'EPSG:32633', purpose='publish', facts=facts).geometry.iloc[0]) == 5 and not facts
    assert shapely.get_num_coordinates(reproject(frame([box(10, 10, 15, 11)], 'EPSG:4326'), 'EPSG:3857', purpose='publish').geometry.iloc[0]) == 5


def test_non_finite_results_are_refused():
    with pytest.raises(ValueError, match='touch a pole'):
        reproject(frame([box(0, -90, 1, -89)], 'EPSG:4326'), 'EPSG:3857', purpose='publish')
    for point, target in ((Point(0, 90), POLAR), (Point(-79, 0), '+proj=ortho +lon_0=100')):
        with pytest.raises(ValueError, match='no usable coordinates'):
            reproject(frame([point], 'EPSG:4326'), target, purpose='publish')


def test_series_and_missing_crs_and_same_crs():
    series = gpd.GeoSeries([BESIDE_SEAM, None], crs=POLAR)
    assert reproject(series, 'EPSG:4326', purpose='return').crs.to_epsg() == 4326
    with pytest.raises(ValueError, match='without a CRS'):
        reproject(gpd.GeoSeries([BESIDE_SEAM]), 'EPSG:4326', purpose='return')
    same = frame([AROUND_POLE])
    assert reproject(same, POLAR, purpose='publish').geometry.iloc[0].equals(AROUND_POLE)


def test_invalid_input_passes_through_without_blame():
    bowtie = Polygon([(1_000_000, 1_000_000), (1_500_000, 1_500_000), (1_500_000, 1_000_000), (1_000_000, 1_500_000)])
    result = reproject(frame([bowtie]), 'EPSG:4326', purpose='publish')
    assert not result.geometry.iloc[0].is_valid


def test_convert_refuses_and_publishes_nothing(tmp_path):
    source = layer(tmp_path/'s.gpkg', [AROUND_POLE, BESIDE_SEAM])
    output = tmp_path/'o.geojson'
    done = tool('convert', source, '--output', output, '--crs', 'EPSG:4326', check=1)
    assert 'touch a pole' in done.stderr and not output.exists()
    good = layer(tmp_path/'g.gpkg', [BESIDE_SEAM])
    tool('convert', good, '--output', output, '--crs', 'EPSG:4326', check=0)
    assert gpd.read_file(output).geometry.is_valid.all()


def test_merge_refuses_a_polar_input_going_geographic(tmp_path):
    polar = layer(tmp_path/'p.gpkg', [AROUND_POLE])
    other = layer(tmp_path/'o.gpkg', [box(0, 0, 1, 1)], 'EPSG:4326')
    done = tool('merge', '--inputs', other, polar, '--output', tmp_path/'m.gpkg')
    assert done.returncode != 0 and 'touch a pole' in done.stderr and not (tmp_path/'m.gpkg').exists()


def test_clip_refuses_a_mask_it_would_flip_and_the_named_way_out_works(tmp_path):
    points = layer(tmp_path/'p.gpkg', [Point(0, -89), Point(120, -85), Point(-60, -86), Point(0, 10)], 'EPSG:4326')
    mask = layer(tmp_path/'m.gpkg', [box(-1_500_000, -1_500_000, 1_500_000, 1_500_000)])
    done = tool('clip', points, '--clip-layer', mask, '--output', tmp_path/'o.gpkg')
    assert done.returncode != 0 and 'touch a pole' in done.stderr and 'convert' in done.stderr and not (tmp_path/'o.gpkg').exists()
    # Convert the points to the polar CRS, as the error says, and the three inside are found.
    moved = tmp_path/'moved.gpkg'
    tool('convert', layer(tmp_path/'inside.gpkg', [Point(0, -89), Point(120, -85), Point(-60, -86)], 'EPSG:4326'), '--output', moved, '--crs', POLAR, check=0)
    tool('clip', moved, '--clip-layer', mask, '--output', tmp_path/'o.gpkg', check=0)
    assert len(gpd.read_file(tmp_path/'o.gpkg')) == 3


def test_buffer_cannot_return_a_flipped_result(tmp_path):
    source = layer(tmp_path/'s.gpkg', [Point(0, -89.8)], 'EPSG:4326')
    done = tool('buffer', source, '--distance', 100_000, '--analysis-crs', POLAR, '--output', tmp_path/'b.gpkg')
    assert done.returncode != 0 and 'touch a pole' in done.stderr and not (tmp_path/'b.gpkg').exists()
    far = layer(tmp_path/'f.gpkg', [Point(45, -70)], 'EPSG:4326')
    tool('buffer', far, '--distance', 1000, '--analysis-crs', POLAR, '--output', tmp_path/'b.gpkg', check=0)
    assert gpd.read_file(tmp_path/'b.gpkg').geometry.is_valid.all()


def polar_raster(path, size=120, cell=50_000):
    half = size*cell/2
    with rio.open(path, 'w', driver='GTiff', width=size, height=size, count=1, dtype='float32', crs=POLAR, nodata=-1,
                  transform=from_origin(-half, half, cell, cell)) as out:
        out.write(np.ones((size, size), 'float32'), 1)
    return path


def centre_latitudes(path):
    with rio.open(path) as ds:
        columns, rows = np.meshgrid(np.arange(ds.width), np.arange(ds.height))
        x, y = rio.transform.xy(ds.transform, rows.ravel(), columns.ravel())
        return np.array(warp_transform(ds.crs, 'EPSG:4326', x, y)[1]), (ds.height, ds.width)


def raster_params(tmp_path, name, **values):
    path = tmp_path/f'{name}.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    return path


def test_sparse_cap_masks_the_polar_raster_correctly(tmp_path):
    raster = polar_raster(tmp_path/'r.tif')
    cap = layer(tmp_path/'cap.gpkg', [box(-180, -90, 180, -75)], 'EPSG:4326')
    latitudes, shape = centre_latitudes(raster)
    truth = int((latitudes < -75).sum())
    tool('raster', 'zonal', raster, '--params', raster_params(tmp_path, 'z', vector=str(cap), id='id'), '--output', tmp_path/'z', check=0)
    zones = json.loads((tmp_path/'z'/'record.json').read_text(encoding='utf-8'))['zones']
    assert truth > 1000 and abs(zones[0]['valid_pixels']-truth) <= truth*0.01
    tool('raster', 'clip', raster, '--params', raster_params(tmp_path, 'c', vector=str(cap)), '--output', tmp_path/'c', check=0)
    with rio.open(tmp_path/'c'/'result.tif') as ds:
        assert abs(int(ds.read(1, masked=True).count())-truth) <= truth*0.01
