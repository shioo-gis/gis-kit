from __future__ import annotations

import math
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import geopandas as gpd
import pytest
from pyproj import CRS, Geod
from shapely.geometry import Point, box


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/buffer.py"
SPEC = importlib.util.spec_from_file_location("buffer_script", SCRIPT)
BUFFER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUFFER)


def _source(path: Path, crs: Optional[str], distances: Optional[list[float]] = None) -> None:
    data = {"id": list(range(len(distances or [0])))}
    if distances is not None:
        data["distance_m"] = distances
    frame = gpd.GeoDataFrame(
        data,
        geometry=[Point(100_000 + i * 1_000, 100_000) for i in range(len(data["id"]))],
        crs=crs,
    )
    frame.to_file(path, driver="GPKG")


def _run(source: Path, output: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), "--output", str(output), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("crs", "expected_coordinate_radius"),
    [("EPSG:26918", 100.0), ("EPSG:2263", 100 / 0.3048006096012192)],
)
def test_distance_is_meters_in_projected_crs(tmp_path: Path, crs: str, expected_coordinate_radius: float) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, crs)

    result = _run(source, output, "--distance", "100")

    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual.crs.to_epsg() == CRS.from_user_input(crs).to_epsg()
    bounds = actual.geometry.iloc[0].bounds
    assert (bounds[2] - bounds[0]) / 2 == pytest.approx(expected_coordinate_radius, rel=1e-7)
    assert actual["id"].tolist() == [0]


def test_field_distances_are_meters_per_feature_in_projected_crs(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, "EPSG:2263", [25.0, 125.0])

    result = _run(source, output, "--field", "distance_m")

    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output).sort_values("id")
    radii = [(row.geometry.bounds[2] - row.geometry.bounds[0]) / 2 for row in actual.itertuples()]
    assert radii == pytest.approx([25 / 0.3048006096012192, 125 / 0.3048006096012192])
    assert actual["distance_m"].tolist() == [25.0, 125.0]


def test_geographic_input_is_buffered_in_meters_and_returned_to_original_crs(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    gpd.GeoDataFrame({"id": [0]}, geometry=[Point(-73.99, 40.75)], crs="EPSG:4326").to_file(source, driver="GPKG")

    result = _run(source, output, "--distance", "100")

    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual.crs.to_epsg() == 4326
    center = actual.geometry.iloc[0].centroid
    west, south, east, north = actual.geometry.iloc[0].bounds
    geod = Geod(ellps="WGS84")
    distances = [
        geod.inv(center.x, center.y, lon, lat)[2]
        for lon, lat in [(west, center.y), (east, center.y), (center.x, south), (center.x, north)]
    ]
    assert min(distances) == pytest.approx(100, abs=1.5)
    assert max(distances) == pytest.approx(100, abs=1.5)


def test_geographic_field_distance_matches_constant_distance(tmp_path: Path) -> None:
    source = tmp_path / "source.gpkg"
    constant_output, field_output = tmp_path / "constant.gpkg", tmp_path / "field.gpkg"
    gpd.GeoDataFrame(
        {"id": [0], "distance_m": [100.0]},
        geometry=[Point(-73.99, 40.75)],
        crs="EPSG:4326",
    ).to_file(source, driver="GPKG")

    constant = _run(source, constant_output, "--distance", "100")
    by_field = _run(source, field_output, "--field", "distance_m")

    assert constant.returncode == 0, constant.stderr
    assert by_field.returncode == 0, by_field.stderr
    constant_geometry = gpd.read_file(constant_output).geometry.iloc[0]
    field_geometry = gpd.read_file(field_output).geometry.iloc[0]
    assert constant_geometry.equals_exact(field_geometry, tolerance=1e-9)


@pytest.mark.parametrize("distance", ["0", "-10"])
def test_zero_and_negative_distances_remain_accepted(tmp_path: Path, distance: str) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, "EPSG:26918")

    result = _run(source, output, "--distance", distance)

    assert result.returncode == 0, result.stderr
    assert len(gpd.read_file(output)) == 1


@pytest.mark.parametrize("mode", ["constant", "field"])
def test_missing_crs_fails_without_writing_output(tmp_path: Path, mode: str) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, None, [100.0] if mode == "field" else None)

    result = _run(source, output, "--field", "distance_m") if mode == "field" else _run(source, output, "--distance", "100")

    assert result.returncode != 0
    assert not output.exists()
    assert "CRS" in result.stderr


def test_geographic_extent_spanning_multiple_utm_zones_fails(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    gpd.GeoDataFrame({"id": [0, 1]}, geometry=[Point(-74, 40), Point(-66, 40)], crs="EPSG:4326").to_file(source, driver="GPKG")

    result = _run(source, output, "--distance", "100")

    assert result.returncode != 0
    assert not output.exists()


def test_geographic_buffer_that_exceeds_local_utm_extent_fails(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    gpd.GeoDataFrame({"id": [0]}, geometry=[Point(-73.99, 40.75)], crs="EPSG:4326").to_file(source, driver="GPKG")

    result = _run(source, output, "--distance", "1000000000")

    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize(
    ("name_a", "factor_a", "name_b", "factor_b", "message"),
    [
        ("unknown", 1.0, "unknown", 1.0, "Cannot determine"),
        ("", 1.0, "", 1.0, "Cannot determine"),
        ("metre", 1.0, "metre", 0.3048, "inconsistent"),
    ],
)
def test_unreliable_projected_axis_units_fail(
    name_a: str, factor_a: float, name_b: str, factor_b: float, message: str
) -> None:
    axes = [
        SimpleNamespace(unit_name=name_a, unit_conversion_factor=factor_a),
        SimpleNamespace(unit_name=name_b, unit_conversion_factor=factor_b),
    ]

    with pytest.raises(ValueError, match=message):
        BUFFER._horizontal_unit_to_meters(SimpleNamespace(axis_info=axes))


@pytest.mark.parametrize("distance", ["nan", "inf", "-inf"])
def test_non_finite_constant_distance_fails(tmp_path: Path, distance: str) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, "EPSG:26918")

    result = _run(source, output, "--distance={}".format(distance))

    assert result.returncode != 0
    assert not output.exists()


def test_non_finite_field_distance_fails(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    _source(source, "EPSG:2263", [math.inf])

    result = _run(source, output, "--field", "distance_m")

    assert result.returncode != 0
    assert not output.exists()


def test_distances_make_concentric_rings_per_feature(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "rings.gpkg"
    _source(source, "EPSG:26918", [0, 0])

    result = _run(source, output, "--distances", "800,400,1200")

    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual["id"].tolist() == [0, 0, 0, 1, 1, 1]
    assert actual["buffer_m"].tolist() == [400.0, 800.0, 1200.0] * 2
    assert actual["ring"].tolist() == [1, 2, 3] * 2
    radii = [(geometry.bounds[2] - geometry.bounds[0]) / 2 for geometry in actual.geometry]
    assert radii == pytest.approx([400, 800, 1200] * 2, rel=1e-7)


def test_distances_dissolve_ring_by_ring(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "rings.gpkg"
    _source(source, "EPSG:26918", [0, 0])

    result = _run(source, output, "--distances", "400,800", "--dissolve")

    assert result.returncode == 0, result.stderr
    actual = gpd.read_file(output)
    assert actual["ring"].tolist() == [1, 2]
    assert actual["buffer_m"].tolist() == [400.0, 800.0]
    assert list(actual.columns) == ["ring", "buffer_m", "geometry"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--distances", "400", "--distance", "100"), "only one of"),
        (("--distances", "400,0"), "positive finite"),
        (("--distances", "400,400"), "must not repeat"),
        (("--distances", "a,b"), "comma-separated numbers"),
    ],
)
def test_distances_reject_bad_input(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "rings.gpkg"
    _source(source, "EPSG:26918")

    result = _run(source, output, *args)

    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


def test_geojson_output_is_named_after_the_file_and_repeatable(tmp_path: Path) -> None:
    source = tmp_path / "source.gpkg"
    _source(source, "EPSG:26918")
    outputs = [tmp_path / "a" / "rings.geojson", tmp_path / "b" / "rings.geojson"]
    for output in outputs:
        result = _run(source, output, "--distances", "400,800")
        assert result.returncode == 0, result.stderr
    first, second = (output.read_bytes() for output in outputs)
    assert first == second
    assert b'"name": "rings"' in first


def test_buffer_crossing_a_utm_zone_names_the_way_out(tmp_path: Path) -> None:
    source, output = tmp_path / "source.gpkg", tmp_path / "buffer.gpkg"
    gpd.GeoDataFrame({"id": [0]}, geometry=[box(120.00, 30.00, 120.05, 30.05)], crs="EPSG:4326").to_file(source, driver="GPKG")
    refused = _run(source, output, "--distance", "500")
    assert refused.returncode != 0
    assert "crosses UTM zones" in refused.stderr and "--analysis-crs EPSG:32651" in refused.stderr
    assert not output.exists()
    accepted = _run(source, output, "--distance", "500", "--analysis-crs", "EPSG:32651")
    assert accepted.returncode == 0, accepted.stderr
