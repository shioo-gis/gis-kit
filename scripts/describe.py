# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pyogrio>=0.10",
#     "pyproj>=3.6",
#     "rasterio>=1.3",
# ]
# ///
"""Describe a GIS file from headers only (CRS, extent, layers, fields, bands, NoData); no features or pixels are read."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

RASTER_SUFFIXES = {'.tif', '.tiff', '.vrt', '.img', '.asc', '.jp2', '.nc', '.dem', '.hgt'}
LAYER_LIMIT = 20
FIELD_LIMIT = 50
# GDAL's CSV default for AUTODETECT_SIZE_LIMIT.
CSV_TYPE_SAMPLE_BYTES = 100000


def crs_card(crs) -> dict | None:
    """CRS identity and units via pyproj; None when the source has no CRS."""
    if crs is None:
        return None
    from pyproj import CRS
    parsed = CRS.from_user_input(crs)
    authority = parsed.to_authority(min_confidence=70)
    axis = parsed.axis_info[0] if parsed.axis_info else None
    card = {'id': ':'.join(authority) if authority else None, 'name': parsed.name,
            'geographic': parsed.is_geographic, 'units': axis.unit_name if axis else None}
    horizontal = None if authority else horizontal_card(parsed)
    if horizontal:
        card['horizontal'] = horizontal
    return card


def horizontal_card(parsed) -> dict | None:
    """An inferred EPSG for the horizontal part of a CRS that has no id of its own.

    A compound CRS (projected + height) has no EPSG code as a whole, and pyproj matches the
    horizontal part of an ESRI compound (often a zone-prefixed Gauss-Kruger) only at low
    confidence. A candidate is kept only when its projection parameters and geodetic CRS
    are the same as the source's, so a name-only match is never reported.
    """
    horizontal = parsed.sub_crs_list[0] if parsed.is_compound else parsed
    if horizontal.is_compound or not (horizontal.is_projected or horizontal.is_geographic):
        return None
    import warnings
    from pyproj import CRS
    base = horizontal.geodetic_crs.to_authority(min_confidence=70) if horizontal.geodetic_crs else None
    if not base:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # PROJ strings drop names and axis order, which is all they are compared without
        for match in horizontal.list_authority(min_confidence=0):
            candidate = CRS.from_authority(match.auth_name, match.code)
            if (candidate.to_proj4() == horizontal.to_proj4() and candidate.geodetic_crs
                    and candidate.geodetic_crs.to_authority(min_confidence=70) == base):
                vertical = parsed.sub_crs_list[1].name if parsed.is_compound and len(parsed.sub_crs_list) > 1 else None
                return {'id': f'{match.auth_name}:{match.code}', 'name': candidate.name, 'inferred': True,
                        'basis': ('horizontal part of a compound CRS' if parsed.is_compound else 'low-confidence match')
                                 + f' ({match.confidence}%); projection parameters and geodetic CRS are the same',
                        'vertical': vertical}
    return None


def _extent(bounds) -> list[float] | None:
    if bounds is None:
        return None
    values = [float(v) for v in bounds]
    return values if len(values) == 4 and all(math.isfinite(v) for v in values) else None


def describe_vector(path: Path) -> tuple[dict, list[dict]]:
    import pyogrio
    names = [str(row[0]) for row in pyogrio.list_layers(path)]
    layers, warnings, driver = [], [], None
    for name in names[:LAYER_LIMIT]:
        info = pyogrio.read_info(path, layer=name)  # fast counts/bounds only; -1/None when unknown
        driver = driver or info.get('driver')
        # OGR calls every CSV column a string unless asked to look at the start of the file.
        sampled = driver == 'CSV'
        if sampled:
            info = pyogrio.read_info(path, layer=name, AUTODETECT_TYPE='YES', AUTODETECT_SIZE_LIMIT=str(CSV_TYPE_SAMPLE_BYTES))
        # pandas reports strings as 'object'; the OGR type name is clearer there.
        fields = [{'name': str(n), 'type': str(t) if str(t) != 'object' else str(o).removeprefix('OFT').lower()}
                  for n, t, o in zip(info['fields'], info['dtypes'], info['ogr_types'])]
        count = int(info['features'])
        crs = crs_card(info['crs'])
        geometry = info.get('geometry_type')
        layers.append({'name': name, 'geometry_type': geometry, 'feature_count': count if count >= 0 else None,
                       'crs': crs, 'extent': _extent(info.get('total_bounds')),
                       'fields': fields[:FIELD_LIMIT], 'field_count': len(fields),
                       **({'field_types_sampled_bytes': CSV_TYPE_SAMPLE_BYTES} if sampled else {})})
        if crs is None and geometry:
            warnings.append({'code': 'crs_missing', 'message': f'图层 {name} 没有 CRS'})
    return {'driver': driver or 'unknown', 'layers': layers, 'layer_count': len(names)}, warnings


def _open_raster(path: Path):
    """Open without rasterio's warning on stderr; the card reports a missing georeference itself."""
    import warnings
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', NotGeoreferencedWarning)
        return rasterio.open(path)


def _world_file(src) -> str | None:
    """The sidecar GDAL read the affine from (.jgw, .pgw, .tfw, .wld, ...), if any."""
    for name in src.files[1:]:  # the first one is the dataset itself
        if re.fullmatch(r'\.(?:wld|(?!raw)[a-z0-9]{2,4}w)', Path(name).suffix.lower()):
            return Path(name).name
    return None


def describe_raster(path: Path) -> tuple[dict, list[dict]]:
    import numpy as np
    from _grid import nodata_unset
    warnings = []
    with _open_raster(path) as src:
        crs = crs_card(src.crs.to_wkt()) if src.crs else None
        # GDAL's identity transform means "no transform": its pixel coordinates are not a place.
        placed = not src.transform.is_identity
        world_file = _world_file(src) if placed else None
        controls = bool(src.gcps[0]) or src.rpcs is not None
        # JSON has no NaN: a NaN NoData is reported as the string "nan", unset as null.
        nodata = [None if v is None else 'nan' if math.isnan(v) else float(v) for v in src.nodatavals]
        raster = {'width': src.width, 'height': src.height, 'band_count': src.count,
                  'dtypes': list(src.dtypes), 'nodata': nodata, 'crs': crs,
                  # Size of all bands decoded in their own dtypes; a fact, not a memory estimate.
                  'decoded_bytes': src.width*src.height*sum(np.dtype(d).itemsize for d in src.dtypes),
                  'resolution': [float(src.res[0]), float(src.res[1])] if placed and src.transform.is_rectilinear else None,
                  'extent': _extent(src.bounds) if placed else None,
                  'transform': [float(v) for v in tuple(src.transform)[:6]] if placed else None,
                  'world_file': world_file}
        driver = src.driver
        undeclared = all(nodata_unset(src, band) for band in src.indexes)
    if crs is None:
        warnings.append({'code': 'crs_missing', 'message': f'CRS 未知：世界文件 {world_file} 只含仿射，不含 CRS' if world_file else '栅格没有 CRS'})
    if not placed and not controls:
        warnings.append({'code': 'georeference_missing', 'message': '栅格没有仿射、GCP 或 RPC，像元不对应地理位置'})
    if undeclared:
        warnings.append({'code': 'nodata_unset', 'message': '所有波段都未设置 NoData，也没有掩膜'})
    return {'driver': driver, 'raster': raster}, warnings


def read_card(path: Path) -> tuple[str, dict, list[dict]]:
    """The suffix only picks which reader goes first: an image with a world file is a raster under any suffix."""
    readers = {'vector': describe_vector, 'raster': describe_raster}
    first, second = ('raster', 'vector') if path.suffix.lower() in RASTER_SUFFIXES else ('vector', 'raster')
    try:
        return first, *readers[first](path)
    except Exception as error:
        try:
            return second, *readers[second](path)
        except Exception as other:
            raise ValueError(f'not readable as {first} ({error}) or as {second} ({other})') from None


def describe(path: Path) -> dict:
    stat = path.stat()
    kind, body, warnings = read_card(path)
    return {'contract': 'describe', 'schema_version': 1, 'path': str(path), 'kind': kind,
            'driver': body.pop('driver'), 'revision': {'size': stat.st_size if path.is_file() else None,
                                                       'mtime': stat.st_mtime},
            **body, 'warnings': warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', help='GIS file or dataset directory (e.g. .gdb)')
    parser.add_argument('--json', action='store_true', required=True, help='Print the describe card as JSON')
    args = parser.parse_args(argv)
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        parser.exit(2, f'ERROR: path not found: {path}\n')
    try:
        card = describe(path)
    except Exception as error:  # driver errors come back as a plain message, not a traceback
        parser.exit(1, f'ERROR: cannot describe {path}: {error}\n')
    print(json.dumps(card, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    sys.exit(main())
