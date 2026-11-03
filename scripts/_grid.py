"""Reading and checking one grid, shared by the raster and terrain tools."""
import math

import numpy as np
from pyproj import CRS, Transformer
from rasterio.enums import MaskFlags
from rasterio.transform import Affine
from rasterio.windows import Window

from _delivery import fact

WEB_MERCATOR_BOUND=20037508.342789244
# These read or copy values without combining them, so an undeclared sentinel stays what it was.
_NO_BLENDING=('inspect','copy','clip','sample','histogram','display')
_LINEAR_RESAMPLING=('bilinear','cubic','average')


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


def nodata_unset(ds,band=1):
    """No NoData value and no mask: every value in the band counts as data."""
    return ds.nodata is None and MaskFlags.all_valid in ds.mask_flag_enums[band-1]


def grid_facts(ds,band=1,operation=None,params=None):
    """What one source grid means for an operation, in one place, so each tool does not have to rediscover it."""
    from _metric import _SCALE_TOLERANCE, ground_scale_range
    params=params or {}; name=str(ds.name).replace('\\','/').rsplit('/',1)[-1]; found=[]
    if 1<=band<=ds.count and nodata_unset(ds,band):
        blends=operation not in _NO_BLENDING and (operation not in ('warp','align') or params.get('resampling') in _LINEAR_RESAMPLING)
        message=f'{name} band {band} has no NoData and no mask, so every value counts as data'
        found.append(fact('nodata_unset','warning' if blends else 'info',message+('; a missing-data marker such as -9999 would be blended into the result. If it has one, mark it first: raster transform with method invalidate, or set NoData' if blends else '.'),input=name,band=band))
    crs=CRS.from_user_input(ds.crs) if ds.crs is not None else None
    if crs is not None and crs.to_epsg()==3857 and any(abs(v)>WEB_MERCATOR_BOUND+1e-7 for v in ds.bounds):
        found.append(fact('grid_exceeds_world','warning',f'{name} spans {[round(v,1) for v in ds.bounds]}, beyond the Web Mercator world square +/-{WEB_MERCATOR_BOUND:.1f}.',input=name,bounds=list(ds.bounds)))
    if crs is not None and crs.is_projected:
        scale=ground_scale_range(crs,ds.bounds)
        if scale is not None:
            excess=max(scale[1]-1,1-scale[0])>_SCALE_TOLERANCE
            found.append(fact('ground_scale_range','warning' if excess else 'info',f'{name}: one metre on the ground is {scale[0]:.3f} to {scale[1]:.3f} grid metres in this extent'+('; distances, areas and slopes read from this grid are off by that factor.' if excess else '.'),input=name,min=scale[0],max=scale[1]))
    return found


def pole_outside_target(source_crs,source_bounds,target_crs,target_bounds):
    """A geographic target whose default extent stops short of a pole the source grid covers, or None."""
    target=CRS.from_user_input(target_crs)
    if not target.is_geographic: return None
    x,y=Transformer.from_crs(target.geodetic_crs,source_crs,always_xy=True).transform([0.,0.],[-90.,90.])
    for latitude,a,b,reach in ((-90,x[0],y[0],target_bounds[1]),(90,x[1],y[1],target_bounds[3])):
        if math.isfinite(a) and math.isfinite(b) and source_bounds[0]<=a<=source_bounds[2] and source_bounds[1]<=b<=source_bounds[3] and abs(reach-latitude)>1e-6:
            return fact('polar_cap_outside_extent','warning',f'The source covers the {latitude} degree pole, but the target grid stops at latitude {reach:.3f}; the cap beyond it is missing from the result.',latitude=latitude,reached=reach)
    return None


def guard_pixels(count,limit,subject,*,param='max_pixels',bytes_per_pixel=8,way_out='Work on smaller bounds or a coarser resolution'):
    """The memory guard every grid tool shares: what was asked for, what the limit is, and what to do about it."""
    if count<=limit: return
    raise ValueError(f'{subject} has {count:,} pixels, over the memory guard {param}={limit:,} (about {count*bytes_per_pixel/2**30:.2f} GiB of working arrays). '
                     f'{way_out}, or raise {param} if that much memory is free.')


def check_band(ds,band):
    if not 1<=band<=ds.count: raise ValueError('Band outside dataset')
    if ds.scales[band-1]!=1 or ds.offsets[band-1]!=0: raise ValueError('Calibrated band scale/offset unsupported; normalize explicitly before analysis')


def band_data(ds, band, window=None):
    check_band(ds,band)
    raw=ds.read(band,window=window,masked=True)
    # Only 8-byte integers can exceed float64's exact range.
    if raw.dtype.kind in 'iu' and raw.dtype.itemsize==8 and np.any(np.abs(raw.compressed().astype('longdouble'))>2**53): raise ValueError('Integer values exceed exact float64 range')
    return np.ma.masked_invalid(raw.astype('float64'),copy=False)


def warp_output_type(ds,band,category,method):
    """(dtype, nodata) of a warp result: the source type when it can hold the result, else the smallest float that can.

    Floats mark missing pixels with NaN. Integers keep their type only for sampled (not interpolated) values and
    only with a declared NoData, since pixels outside the source need a value that cannot be data.
    """
    dtype=ds.dtypes[band-1]; kind=np.dtype(dtype)
    if kind.kind=='f': return dtype,float('nan')
    if ds.nodata is not None and np.isfinite(ds.nodata) and (category=='categorical' or method in ('nearest','mode')): return dtype,ds.nodata
    return ('float32' if kind.itemsize<=2 else 'float64'),float('nan')


def warp_can_stream(ds,band,window,size=1024):
    """Whether GDAL can read the band itself: only a declared NoData marks missing pixels and no stray non-finite value would spread."""
    dtype=np.dtype(ds.dtypes[band-1])
    if dtype.kind in 'iu' and dtype.itemsize==8: return False  # needs the exact-range check on its values
    if any(flag.name not in ('nodata','all_valid','nan') for flag in ds.mask_flag_enums[band-1]): return False
    if dtype.kind!='f': return True
    left,top,width,height=(window.col_off,window.row_off,window.width,window.height) if window is not None else (0,0,ds.width,ds.height)
    declared=ds.nodata is None or np.isnan(ds.nodata)
    for part in windows(width,height,size):
        values=ds.read(band,window=Window(left+part.col_off,top+part.row_off,part.width,part.height))
        if np.isinf(values).any() or (not declared and np.isnan(values).any()): return False
    return True


def clip_grid_to_world(crs,transform,width,height):
    """Trim an EPSG:3857 grid to the world square in whole pixels; other grids and rotated ones pass unchanged.

    Returns (transform, width, height, trimmed bounds or None). Web Mercator's y grows without limit towards the poles.
    """
    if CRS.from_user_input(crs).to_epsg()!=3857 or transform.b or transform.d or transform.a<=0 or transform.e>=0: return transform,width,height,None
    x,y,a,e=transform.c,transform.f,transform.a,-transform.e; slack=1e-9
    first_col=max(0,math.ceil((-WEB_MERCATOR_BOUND-x)/a-slack)); last_col=min(width,math.floor((WEB_MERCATOR_BOUND-x)/a+slack))
    first_row=max(0,math.ceil((y-WEB_MERCATOR_BOUND)/e-slack)); last_row=min(height,math.floor((y+WEB_MERCATOR_BOUND)/e+slack))
    if (first_col,first_row,last_col,last_row)==(0,0,width,height): return transform,width,height,None
    if last_col<=first_col or last_row<=first_row: raise ValueError('Target grid lies outside the Web Mercator world square')
    before=[x,y-e*height,x+a*width,y]
    return transform*Affine.translation(first_col,first_row),last_col-first_col,last_row-first_row,before
