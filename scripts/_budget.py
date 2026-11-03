"""Memory budget: estimate a whole read before it happens and fail with a machine-readable error.

The budget comes from GIS_KIT_MEMORY_BUDGET (``gis.py --memory-budget`` sets it for the tool). Unset
means a share of the memory available now; ``off`` turns the check off. Estimates only stop reads that
clearly exceed the budget; they are not an RSS limit. Contract: references/memory-budget.md.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

ENV = 'GIS_KIT_MEMORY_BUDGET'
EXIT_CODE = 3
ERROR_PREFIX = 'GIS_KIT_ERROR '
OFF = ('off', 'none', 'unlimited')
SAMPLE_FEATURES = 1000
# The default leaves room for the interpreter and the imports, which the estimates do not count.
DEFAULT_SHARE = 0.8
# Peak RSS while pyogrio decodes a layer was 2.3-3.1x the resident frame (GPKG, 20k-200k features).
VECTOR_PEAK_FACTOR = 3
_UNITS = {'': 1, 'b': 1, 'kb': 10**3, 'mb': 10**6, 'gb': 10**9, 'tb': 10**12,
          'kib': 2**10, 'mib': 2**20, 'gib': 2**30, 'tib': 2**40}


class BudgetExceeded(SystemExit):
    """Exit code 3 through ``finally`` cleanup; ``except Exception`` handlers do not swallow it."""

    def __init__(self, record: dict):
        super().__init__(EXIT_CODE)
        self.record = record


def parse_size(text: str) -> int:
    """Bytes from '2GiB', '512 MB' or '1048576'; binary units are powers of 1024."""
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*', str(text))
    unit = match.group(2).lower() if match else None
    if unit not in _UNITS:
        raise ValueError(f'Invalid memory size: {text!r}; use e.g. 2GiB, 512MB or bytes')
    value = math.floor(float(match.group(1))*_UNITS[unit])
    if value < 1:
        raise ValueError(f'Memory size must be positive: {text!r}')
    return value


def available_memory() -> int | None:
    """Bytes this machine can hand out now without swapping, or None when it cannot say."""
    try:
        if sys.platform == 'darwin':
            text = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5, check=True).stdout
            page = int(re.search(r'page size of (\d+) bytes', text).group(1))
            pages = {name.strip(): int(value.strip(' .')) for name, value in re.findall(r'^Pages ([^:]+):\s+(\d+)\.$', text, re.M)}
            return page*sum(pages[name] for name in ('free', 'inactive', 'speculative', 'purgeable'))
        if sys.platform == 'win32':
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                            ('page_total', ctypes.c_ulonglong), ('page_available', ctypes.c_ulonglong), ('virtual_total', ctypes.c_ulonglong),
                            ('virtual_available', ctypes.c_ulonglong), ('extended', ctypes.c_ulonglong)]
            status = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
            return int(status.available) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        with open('/proc/meminfo', encoding='utf-8') as stream:
            return 1024*int(re.search(r'^MemAvailable:\s+(\d+) kB', stream.read(), re.M).group(1))
    except (OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def default_budget() -> int | None:
    available = available_memory()
    return int(available*DEFAULT_SHARE) if available else None


def budget() -> int | None:
    value = os.environ.get(ENV, '').strip()
    if value.lower() in OFF:
        return None
    return parse_size(value) if value else default_budget()


def require(estimate: int | None, *, basis: str, input, layer: str | None = None) -> None:
    """Fail before reading when the estimate exceeds the budget; no budget or no estimate passes."""
    limit = budget()
    if limit is None or estimate is None or estimate <= limit:
        return
    record = {'contract': 'error', 'schema_version': 1, 'code': 'memory_budget_exceeded',
              'tool': Path(sys.argv[0]).stem or None, 'input': str(Path(input).absolute()), 'layer': layer,
              'estimated_bytes': int(estimate), 'budget_bytes': limit, 'basis': basis,
              'budget_source': 'explicit' if os.environ.get(ENV, '').strip() else 'available_memory'}
    origin = f'{ENV}' if record['budget_source'] == 'explicit' else f'default, {DEFAULT_SHARE:.0%} of available memory; {ENV}=off removes it'
    sys.stderr.write(f'ERROR: estimated memory {estimate/2**30:.2f} GiB exceeds budget {limit/2**30:.2f} GiB '
                     f'({origin}) for {record["input"]}\n')
    sys.stderr.write(ERROR_PREFIX + json.dumps(record, ensure_ascii=False) + '\n')
    sys.stderr.flush()
    raise BudgetExceeded(record)


def raster_bytes(width: int, height: int, *, arrays: float = 1, itemsize: int = 8) -> int:
    """Grid-sized arrays resident at once; float64 working copies by default."""
    return math.ceil(width*height*arrays*itemsize)


def check_raster(ds, *, arrays: float = 1, itemsize: int = 8, width: int | None = None, height: int | None = None) -> None:
    """Check ``arrays`` grid-sized working arrays of ``ds`` (or of an explicit target grid)."""
    if budget() is None:
        return
    require(raster_bytes(width or ds.width, height or ds.height, arrays=arrays, itemsize=itemsize),
            basis='decoded_pixels', input=ds.name)


def check_image(path, width: int, height: int, *, bytes_per_pixel: float) -> None:
    """Check an image library's full decode, measured in bytes per pixel of the steps the tool performs."""
    if budget() is None:
        return
    require(math.ceil(width*height*bytes_per_pixel), basis='decoded_pixels', input=path)


def vector_bytes(path, layer: str | None = None, bbox=None) -> int | None:
    """Sample the first features, extrapolate to the layer or bbox, then allow for the decoding peak."""
    import geopandas as gpd
    import pyogrio
    import shapely
    if bbox is None:
        count = int(pyogrio.read_info(path, layer=layer).get('features', -1))
    else:
        count = len(pyogrio.read_bounds(path, layer=layer, bbox=tuple(bbox))[0])
    if count < 0:
        return None
    if count == 0:
        return 0
    sample = pyogrio.read_dataframe(path, layer=layer, max_features=SAMPLE_FEATURES)
    if not len(sample):
        return None
    attributes = sample
    size = 0
    if isinstance(sample, gpd.GeoDataFrame):
        attributes = sample.drop(columns=sample.geometry.name)
        # Coordinates as float64 pairs plus a per-object GEOS/pointer overhead.
        size += int(shapely.get_num_coordinates(sample.geometry.to_numpy()).sum())*16 + len(sample)*128
    size += int(attributes.memory_usage(deep=True, index=False).sum())
    return math.ceil(size/len(sample)*count*VECTOR_PEAK_FACTOR)


def check_vector(path, layer: str | None = None, bbox=None) -> None:
    if budget() is None:
        return
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            estimate = vector_bytes(path, layer, bbox)
    except Exception:  # The tool's own read reports an unreadable source; a failed estimate must not decide it.
        estimate = None
    require(estimate, basis='sampled_features', input=path, layer=layer)
