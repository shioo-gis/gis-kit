"""Reading and checking one grid, shared by the raster and terrain tools."""
import math

import numpy as np
from pyproj import CRS, Transformer
from rasterio.windows import Window


def positive_integer(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value or int(value)<1: raise ValueError('Expected positive integer')
    return int(value)


def windows(width,height,size):
    if size<1: raise ValueError('block_size must be positive')
    for y in range(0,height,size):
        for x in range(0,width,size): yield Window(x,y,min(size,width-x),min(size,height-y))


def band_matches(ds,band,expected,equal_nan=True,size=1024):
    """Compare one decoded band with an in-memory array window by window, without a second full copy."""
    if ds.shape!=expected.shape: return False
    return all(np.array_equal(ds.read(band,window=w),expected[w.toslices()],equal_nan=equal_nan) for w in windows(ds.width,ds.height,size))


def inspect(ds):
    if ds.crs is None: raise ValueError('Raster requires known CRS')
    if not np.isfinite(list(ds.transform)).all() or ds.transform.determinant==0: raise ValueError('Invalid raster transform')
    return {'crs':str(ds.crs),'transform':list(ds.transform)[:6],'width':ds.width,'height':ds.height,'count':ds.count,
            'dtypes':list(ds.dtypes),'nodata':str(ds.nodata) if ds.nodata is not None and not np.isfinite(ds.nodata) else ds.nodata,
            'scales':ds.scales,'offsets':ds.offsets,'units':ds.units,'descriptions':ds.descriptions,'mask_flags':[[x.name for x in v] for v in ds.mask_flag_enums],
            'bounds':list(ds.bounds),'block_shapes':ds.block_shapes}


def right_handed(crs, bounds):
    """True when +x lies clockwise of +y on the ground, as aspect and light directions assume.

    rasterio's x and y are the data axes whatever order the CRS declares, so the CRS axis names say nothing here.
    """
    crs=CRS.from_user_input(crs); to_lonlat=Transformer.from_crs(crs,crs.geodetic_crs,always_xy=True)
    x,y=(bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2; step=max(abs(bounds[2]-bounds[0]),abs(bounds[3]-bounds[1]))/100 or 1.
    centre=to_lonlat.transform(x,y); geod=crs.get_geod()
    towards_x=geod.inv(*centre,*to_lonlat.transform(x+step,y))[0]; towards_y=geod.inv(*centre,*to_lonlat.transform(x,y+step))[0]
    return 0<(towards_x-towards_y)%360<180


def planar_metres_per_unit(crs, bounds):
    """Metres per coordinate unit of a projected grid, or an error naming what stops planar derivatives."""
    from _metric import _horizontal_meters_per_unit
    crs=CRS.from_user_input(crs)
    if not crs.is_projected or len(crs.axis_info)!=2: raise ValueError('Two-dimensional projected CRS required; warp explicitly first')
    factor=_horizontal_meters_per_unit(crs)
    if not right_handed(crs,bounds): raise ValueError(f'{crs.name}: x and y form a left-handed system, so aspect and hillshade would be mirrored; warp explicitly to a CRS with x clockwise of y first')
    return factor


def band_data(ds, band, window=None):
    if not 1<=band<=ds.count: raise ValueError('Band outside dataset')
    if ds.scales[band-1]!=1 or ds.offsets[band-1]!=0: raise ValueError('Calibrated band scale/offset unsupported; normalize explicitly before analysis')
    raw=ds.read(band,window=window,masked=True)
    if raw.dtype.kind in 'iu' and np.any(np.abs(raw.compressed().astype('longdouble'))>2**53): raise ValueError('Integer values exceed exact float64 range')
    a=raw.astype('float64')
    return np.ma.masked_invalid(a)
