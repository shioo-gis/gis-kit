"""package.py unpack: safe unpacking and inventory of ArcGIS layer / map packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import LineString, box

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
needs_bsdtar = pytest.mark.skipif(shutil.which('bsdtar') is None, reason='bsdtar (libarchive) not installed')


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPTS/'package.py'), *map(str, args)], capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert (result.returncode == 0) == success, result.stdout+result.stderr
    return json.loads((Path(args[args.index('--output') + 1])/'package.json').read_text()) if success else result.stderr


def fake_lyr(folder_name, dataset, label):
    """Bytes shaped like an ArcMap .lyr: an OLE header, then the UTF-16LE strings ArcMap stores for the layer."""
    strings = [dataset, 'Shapefile', f'..\\commondata\\{folder_name}', 'Feature Class', label, 'Default']
    return b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + bytes(56) + b''.join(s.encode('utf-16le') + bytes(6) for s in strings)


def package_tree(root, with_data=True):
    (root/'esriinfo').mkdir(parents=True)
    (root/'esriinfo'/'item.pkinfo').write_text(
        "<?xml version='1.0' encoding='utf-8' ?><pkinfo Culture='zh-CN'><ID>X1</ID><name>示例拆除类</name>"
        "<version>10.5</version><type>Layer Package</type></pkinfo>", encoding='utf-8')
    (root/'esriinfo'/'iteminfo.xml').write_text(
        "<?xml version='1.0' encoding='utf-8' ?><ESRI_ItemInformation><name>示例拆除类</name><title>示例拆除类</title>"
        "<typekeywords><typekeyword>Layer Package</typekeyword></typekeywords>"
        "<extent><xmin>113.8</xmin><ymin>22.5</ymin><xmax>113.9</xmax><ymax>22.6</ymax></extent></ESRI_ItemInformation>",
        encoding='utf-8')
    for version in ('v10', 'v105'):
        (root/version).mkdir()
        (root/version/'0000拆除类.lyr').write_bytes(fake_lyr('示例规划', '拆除类', '[项目名]'))
    if with_data:
        shp = root/'commondata'/'示例规划'
        shp.mkdir(parents=True)
        gpd.GeoDataFrame({'项目名': ['示例工业区']}, geometry=[box(489000, 2490000, 489100, 2490100)], crs='EPSG:4547').to_file(
            shp/'拆除类.shp', encoding='utf-8')
        gpd.GeoDataFrame({'name': ['示例大道']}, geometry=[LineString([(0, 0), (1, 1)])], crs='EPSG:4547').to_file(
            root/'commondata'/'roads.gdb', layer='roads', driver='OpenFileGDB')
        (root/'p20').mkdir()
        (root/'p20'/'roads.lyrx').write_text(json.dumps({'type': 'CIMLayerDocument', 'layerDefinitions': [{
            'type': 'CIMFeatureLayer', 'name': '道路',
            'featureTable': {'dataConnection': {'workspaceConnectionString': 'DATABASE=..\\commondata\\roads.gdb', 'dataset': 'roads'},
                             'definitionExpression': "name <> ''"},
            'renderer': {'type': 'CIMUniqueValueRenderer', 'fields': ['name'],
                         'groups': [{'classes': [{'label': '示例大道', 'values': [{'fieldValues': ['示例大道']}]}]}]},
            'labelClasses': [{'expression': '$feature.name'}]}]}, ensure_ascii=False), encoding='utf-8')
    return root


def seven_zip(tree, archive):
    subprocess.run(['bsdtar', '--format', '7zip', '-cf', str(archive), '-C', str(tree), '.'], check=True)
    return archive


@needs_bsdtar
def test_unpack_lists_data_and_links_layers(tmp_path):
    package = seven_zip(package_tree(tmp_path/'tree'), tmp_path/'示例拆除类.lpk')
    manifest = cli('unpack', package, '--output', tmp_path/'out')
    assert manifest['source']['archive'] == '7z' and manifest['package']['name'] == '示例拆除类'
    assert manifest['item']['extent'] == [113.8, 22.5, 113.9, 22.6] and manifest['layer_versions'] == ['v10', 'v105']
    paths = {d['path']: d for d in manifest['datasets']}
    assert set(paths) == {'commondata/示例规划/拆除类.shp', 'commondata/roads.gdb'}
    assert paths['commondata/示例规划/拆除类.shp']['card']['layers'][0]['crs']['id'] == 'EPSG:4547'
    lyr = next(l for l in manifest['layers'] if l['file'].endswith('.lyr'))
    assert lyr['file'] == 'v105/0000拆除类.lyr'  # the newest version folder only
    assert lyr['data'] == ['commondata/示例规划/拆除类.shp'] and lyr['label_expressions'] == ['[项目名]']
    assert lyr['data_sources'] == ['..\\commondata\\示例规划'] and lyr['symbology'] == 'not read: binary .lyr'
    lyrx = next(l for l in manifest['layers'] if l['file'].endswith('.lyrx'))
    assert lyrx['data'] == ['commondata/roads.gdb:roads']
    roads = lyrx['layers'][0]
    assert roads['renderer'] == 'CIMUniqueValueRenderer' and roads['renderer_fields'] == ['name']
    assert roads['classes'] == [{'label': '示例大道', 'values': ['示例大道']}] and roads['label_expressions'] == ['$feature.name']
    assert roads['definition_query'] == "name <> ''" and manifest['warnings'] == []
    assert gpd.read_file(tmp_path/'out'/'package'/'commondata'/'示例规划'/'拆除类.shp')['项目名'].tolist() == ['示例工业区']


@needs_bsdtar
def test_unpack_reports_layers_without_data(tmp_path):
    package = seven_zip(package_tree(tmp_path/'tree', with_data=False), tmp_path/'ref.lpk')
    manifest = cli('unpack', package, '--output', tmp_path/'out')
    assert manifest['datasets'] == [] and manifest['layers'][0]['data_in_package'] is False
    assert any('layer definitions only' in w for w in manifest['warnings'])
    assert any('1 layers point at data that is not in the package' in w for w in manifest['warnings'])


def test_unpack_a_zip_package_and_refusals(tmp_path):
    tree = package_tree(tmp_path/'tree')
    good = tmp_path/'map.mpkx'
    with zipfile.ZipFile(good, 'w') as archive:
        for item in sorted(tree.rglob('*')):
            if item.is_file():
                archive.write(item, item.relative_to(tree).as_posix())
    manifest = cli('unpack', good, '--output', tmp_path/'zip')
    assert manifest['source']['archive'] == 'zip' and len(manifest['datasets']) == 2
    assert 'above --max-bytes' in cli('unpack', good, '--output', tmp_path/'small', '--max-bytes', 100, success=False)
    evil = tmp_path/'evil.lpk'
    with zipfile.ZipFile(evil, 'w') as archive:
        archive.writestr('esriinfo/item.pkinfo', '<pkinfo/>')
        archive.writestr('../outside.txt', 'x')
    assert 'outside the package folder' in cli('unpack', evil, '--output', tmp_path/'e', success=False)
    linked = tmp_path/'link.lpk'
    with zipfile.ZipFile(linked, 'w') as archive:
        info = zipfile.ZipInfo('commondata/link')
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, '/etc/passwd')
    assert '(link)' in cli('unpack', linked, '--output', tmp_path/'l', success=False)
    text = tmp_path/'plain.lpk'
    text.write_text('not an archive')
    assert 'neither a 7z nor a zip' in cli('unpack', text, '--output', tmp_path/'t', success=False)
    assert 'Source must be' in cli('unpack', tmp_path/'tree'/'esriinfo'/'item.pkinfo', '--output', tmp_path/'s', success=False)
    assert not any((tmp_path/n).exists() for n in ('small', 'e', 'l', 't', 's'))
    assert not (tmp_path/'outside.txt').exists()


def test_unescape_bsdtar_names():
    sys.path.insert(0, str(SCRIPTS))
    from package import unescape
    assert unescape('commondata/\\347\\244\\272\\344\\276\\213/a.shp') == 'commondata/示例/a.shp'
    assert unescape('a\\\\b') == 'a\\b'


def zip_tree(tree, archive):
    with zipfile.ZipFile(archive, 'w') as out:
        for item in sorted(tree.rglob('*')):
            if item.is_file():
                out.write(item, item.relative_to(tree).as_posix())
    return archive


def test_unpack_lists_data_in_version_folders_and_compound_crs(tmp_path):
    """ArcMap packages may keep a geodatabase in each version folder; .lyr names it by a relative path."""
    from pyproj import CRS
    tree = tmp_path/'tree'
    (tree/'esriinfo').mkdir(parents=True)
    (tree/'esriinfo'/'item.pkinfo').write_text('<pkinfo><name>base</name></pkinfo>', encoding='utf-8')
    lyr = (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + bytes(56)
           + b''.join(s.encode('utf-16le') + bytes(6) for s in ['底图', 'File Geodatabase Feature Class', 'plan.gdb', '[name]'])
           + '\x1f'.encode('utf-16le') + 'parcels'.encode('utf-16le') + bytes(6))  # a name glued to a binary character
    (tree/'v10').mkdir()
    gpd.GeoDataFrame({'name': ['a']}, geometry=[box(0, 0, 1, 1)], crs='EPSG:4547').to_file(
        tree/'v10'/'plan.gdb', layer='parcels', driver='OpenFileGDB')
    shutil.copytree(tree/'v10', tree/'v107')
    for version in ('v10', 'v107'):
        (tree/version/'0000底图.lyr').write_bytes(lyr)
    sea = tree/'commondata'/'sea'
    sea.mkdir(parents=True)
    gpd.GeoDataFrame({'k': [1]}, geometry=[box(38489000, 2490000, 38489100, 2490100)], crs='EPSG:4526').to_file(sea/'sea.shp')
    (sea/'sea.prj').write_text(CRS('EPSG:4526+5737').to_wkt('WKT1_ESRI'))  # zone-prefixed Gauss-Kruger + height

    manifest = cli('unpack', zip_tree(tree, tmp_path/'base.lpk'), '--output', tmp_path/'out')
    rows = {d['path']: d for d in manifest['datasets']}
    assert set(rows) == {'commondata/sea/sea.shp', 'v107/plan.gdb'}  # the identical v10 copy is listed once
    assert rows['v107/plan.gdb']['copies'] == ['v10/plan.gdb']
    assert manifest['layers'][0]['data'] == ['v107/plan.gdb:parcels']
    crs = rows['commondata/sea/sea.shp']['card']['layers'][0]['crs']
    assert crs['id'] is None and crs['horizontal']['id'] == 'EPSG:4526' and crs['horizontal']['inferred'] is True
    assert crs['horizontal']['vertical'] == 'Yellow Sea 1985 height'
    assert any('not named by any layer file: commondata/sea/sea.shp' in w for w in manifest['warnings'])
    (tree/'v107'/'0000底图.lyr').write_bytes(lyr + 'sea'.encode('utf-16le') + bytes(2) + '海洋'.encode('utf-16le') + bytes(2))
    shutil.copy(sea/'sea.shp', sea/'海洋.shp')  # a two-character name, between string terminators
    for sidecar in ('shx', 'dbf', 'prj'):
        shutil.copy(sea/f'sea.{sidecar}', sea/f'海洋.{sidecar}')
    manifest = cli('unpack', zip_tree(tree, tmp_path/'named.lpk'), '--output', tmp_path/'named')
    assert manifest['layers'][0]['data'] == ['commondata/sea/sea.shp', 'commondata/sea/海洋.shp', 'v107/plan.gdb:parcels']

    gpd.GeoDataFrame({'name': ['a', 'b']}, geometry=[box(0, 0, 1, 1), box(1, 1, 2, 2)], crs='EPSG:4547').to_file(
        tree/'v10'/'plan.gdb', layer='parcels', driver='OpenFileGDB', mode='w')
    manifest = cli('unpack', zip_tree(tree, tmp_path/'differ.lpk'), '--output', tmp_path/'differ')
    assert {'v10/plan.gdb', 'v107/plan.gdb'} <= {d['path'] for d in manifest['datasets']}
    assert any('2 copies of plan.gdb differ' in w for w in manifest['warnings'])
    assert [d for d in manifest['layers'][0]['data'] if '.gdb' in d] == ['v107/plan.gdb:parcels']  # the layer file's own copy


def test_horizontal_epsg_is_inferred_only_when_parameters_match():
    sys.path.insert(0, str(SCRIPTS))
    from describe import crs_card
    from pyproj import CRS
    assert 'horizontal' not in crs_card('EPSG:4547')
    assert crs_card('EPSG:4547+5737')['horizontal']['id'] == 'EPSG:4547'
    assert crs_card(CRS('EPSG:4526+5737').to_wkt('WKT1_ESRI'))['horizontal']['id'] == 'EPSG:4526'
    assert 'horizontal' not in crs_card('+proj=tmerc +lon_0=114.3 +x_0=500000 +ellps=GRS80 +units=m')  # local grid


@pytest.mark.skipif(os.name == 'nt', reason='fake tools are shell scripts')
@pytest.mark.parametrize(('banner', 'accepted'), [('bsdtar 3.7.7 - libarchive 3.7.7', True), ('tar (GNU tar) 1.34', False)])
def test_tar_counts_as_bsdtar_only_when_it_is_libarchive(tmp_path, monkeypatch, banner, accepted):
    sys.path.insert(0, str(SCRIPTS))
    import package
    fake = tmp_path / 'tar'
    fake.write_text(f'#!/bin/sh\necho "{banner}"\n', encoding='utf-8')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    assert package.bsdtar() == (str(fake) if accepted else None)
