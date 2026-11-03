"""Directed costs verified independently of GRASS path search."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import geopandas as gpd
import numpy as np
import pytest
import rasterio as rio
from rasterio.transform import from_origin

S = Path(__file__).resolve().parents[1]/'scripts'; sys.path.insert(0, str(S))
spec = importlib.util.spec_from_file_location('walk', S/'terrain-cost.py'); W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
T = from_origin(500000, 3100000, 10, 10)
P = dict(vertical_unit='metre', vertical_datum='unknown', source_description='synthetic test', start=[500005,3099975], end=[500045,3099975], walk_coeff=[.72,6,1.9998,-1.9998], slope_factor=-.2125, friction_lambda=1, corridor_extra_seconds=0, cost_assumptions='synthetic explicit friction, null barriers')


def raster(path, a):
    with rio.open(path, 'w', driver='GTiff', height=a.shape[0], width=a.shape[1], count=1, dtype='float64', crs=32645, transform=T, nodata=np.nan) as ds: ds.write(a.astype(float),1)
    return path


def read(path):
    with rio.open(path) as ds: return ds.read(1)


@pytest.fixture
def grass():
    value=os.environ.get('GIS_TEST_GRASS')
    if not value: pytest.skip('Explicit existing GRASS required')
    return value


def test_directional_hand_calculation():
    assert W.step_cost(0,1,10,0,0,P) == pytest.approx(13.2)
    assert W.step_cost(1,0,10,0,0,P) == pytest.approx(5.2002)
    assert W.step_cost(3,0,10,0,0,P) == pytest.approx(13.1994)
    assert W.step_cost(0,1,10,1,3,P) == pytest.approx(33.2)


@pytest.mark.parametrize('delta', [1,3])
def test_real_grass_up_down_and_transpose(tmp_path, grass, delta):
    z=np.tile(np.arange(5.)*delta,(5,1)); f=np.full((5,5),np.nan); f[2,:]=0
    dem=raster(tmp_path/'dem.tif',z); friction=raster(tmp_path/'friction.tif',f)
    W.execute(dem,friction,P,tmp_path/'up',grass)
    p=dict(P,start=P['end'],end=P['start'])
    W.execute(dem,friction,p,tmp_path/'down',grass)
    up=json.loads((tmp_path/'up/record.json').read_text(encoding='utf-8')); down=json.loads((tmp_path/'down/record.json').read_text(encoding='utf-8'))
    assert up['cost_seconds']==pytest.approx(4*(7.2+6*delta))
    expected=4*(7.2-(1.9998 if delta==1 else -1.9998)*delta)
    assert down['cost_seconds']==pytest.approx(expected)
    assert up['cost_seconds']!=down['cost_seconds']
    assert np.all(read(tmp_path/'up/corridor.tif')[2,:]==1)
    assert np.all(read(tmp_path/'up/excess_seconds.tif')[2,:]<1e-7)
    route=gpd.read_file(tmp_path/'up/routes.gpkg'); assert route.length.iloc[0]==40
    assert not list(tmp_path.glob('.walk-work-*'))


def test_barrier_unreachable_and_same_cell(tmp_path,grass):
    z=np.zeros((5,5));f=np.zeros_like(z);f[:,2]=np.nan
    dem=raster(tmp_path/'dem.tif',z);friction=raster(tmp_path/'friction.tif',f)
    W.execute(dem,friction,P,tmp_path/'blocked',grass)
    record=json.loads((tmp_path/'blocked/record.json').read_text(encoding='utf-8'))
    assert record['reachable'] is False and record['cost_seconds'] is None
    assert gpd.read_file(tmp_path/'blocked/routes.gpkg').empty
    assert np.isnan(read(tmp_path/'blocked/corridor.tif')).all()
    W.execute(dem,friction,dict(P,end=P['start']),tmp_path/'same',grass)
    assert json.loads((tmp_path/'same/record.json').read_text(encoding='utf-8'))['cost_seconds']==0
    assert gpd.read_file(tmp_path/'same/routes.gpkg').empty


def test_corridor_against_independent_graph(tmp_path,grass):
    # Asymmetric random terrain, variable friction, gap barrier: exhaustive Bellman-Ford oracle.
    import networkx as nx
    rng=np.random.default_rng(19);z=rng.uniform(0,6,(5,5));f=rng.uniform(0,2,(5,5));f[1:4,2]=np.nan
    dem=raster(tmp_path/'dem.tif',z);friction=raster(tmp_path/'friction.tif',f);p=dict(P,corridor_extra_seconds=8)
    W.execute(dem,friction,p,tmp_path/'out',grass)
    g=nx.DiGraph()
    for r,c in np.ndindex(z.shape):
        if not np.isfinite(f[r,c]):continue
        g.add_node((r,c))
        for dr,dc in [(a,b) for a in [-1,0,1] for b in [-1,0,1] if a or b]:
            rr,cc=r+dr,c+dc
            if not(0<=rr<5 and 0<=cc<5) or not np.isfinite(f[rr,cc]):continue
            dh=z[rr,cc]-z[r,c];distance=np.hypot(dr,dc)*10
            coeff=6 if dh>=0 else (1.9998 if dh/distance>=-.2125 else -1.9998)
            weight=.72*distance+coeff*dh+(f[r,c]+f[rr,cc])*.5*distance
            g.add_edge((r,c),(rr,cc),weight=weight)
    forward=nx.single_source_bellman_ford_path_length(g,(2,0));reverse=nx.single_source_bellman_ford_path_length(g.reverse(),(2,4))
    a=read(tmp_path/'out/from_start.tif');b=read(tmp_path/'out/to_end.tif');corridor=read(tmp_path/'out/corridor.tif')
    for q in g:
        assert a[q]==pytest.approx(forward[q],abs=1e-7)
        assert b[q]==pytest.approx(reverse[q],abs=1e-7)
        assert corridor[q]==(forward[q]+reverse[q]-forward[(2,4)]<=8+1e-7)


@pytest.mark.parametrize('change',[{'walk_coeff':[.1,6,2,-2]}, {'walk_coeff':[.72,-1,2,-2]}, {'slope_factor':0}, {'start':[500006,3099975]}, {'friction_lambda':-1}, {'corridor_extra_seconds':True}, {'unknown':1}])
def test_bad_parameters(tmp_path,change):
    path=raster(tmp_path/'dem.tif',np.zeros((5,5)))
    with rio.open(path) as ds:
        with pytest.raises(ValueError):W.validate(dict(P,**change),ds)


def test_input_failure_and_no_publish(tmp_path,grass):
    z=np.zeros((5,5));f=np.zeros_like(z);f[2,0]=np.nan
    dem=raster(tmp_path/'dem.tif',z);friction=raster(tmp_path/'friction.tif',f)
    with pytest.raises(ValueError,match='barrier'):W.execute(dem,friction,P,tmp_path/'out',grass)
    assert not (tmp_path/'out').exists()
    f[:]=0;friction=raster(tmp_path/'friction.tif',f);z[0,0]=np.nan;dem=raster(tmp_path/'dem.tif',z)
    with pytest.raises(ValueError,match='Complete DEM'):W.execute(dem,friction,P,tmp_path/'out',grass)
    assert not (tmp_path/'out').exists()


def test_failed_backend_cleanup(tmp_path,monkeypatch):
    from types import SimpleNamespace
    dem=raster(tmp_path/'dem.tif',np.zeros((5,5)));friction=raster(tmp_path/'friction.tif',np.zeros((5,5)))
    monkeypatch.setattr(W.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr=''))
    with pytest.raises(ValueError,match='no result published'):W.execute(dem,friction,P,tmp_path/'out',sys.executable)
    assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.walk-work-*')) and not list(tmp_path.glob('.delivery-*')) and not list(tmp_path.glob('.*publish-lock'))


def test_friction_grid_and_negative_rejection(tmp_path):
    dem=raster(tmp_path/'dem.tif',np.zeros((5,5)));friction=raster(tmp_path/'friction.tif',-np.ones((5,5)))
    with pytest.raises(ValueError,match='nonnegative'):W.execute(dem,friction,P,tmp_path/'out',sys.executable)
    raster(friction,np.ones((5,5)))
    with rio.open(friction,'r+') as ds:ds.transform=from_origin(500001,3100000,10,10)
    with pytest.raises(ValueError,match='exactly match'):W.execute(dem,friction,P,tmp_path/'out',sys.executable)
    assert not (tmp_path/'out').exists()
