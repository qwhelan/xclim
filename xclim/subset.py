import logging
import warnings
from functools import wraps
from pathlib import Path
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import fiona
import geojson
import numpy as np
import rasterio.crs
import rioxarray
import xarray
from pyproj import Geod

__all__ = ["subset_bbox", "subset_gridpoint", "subset_shape", "subset_time"]
logging.basicConfig(level=logging.INFO)


def _read_geometries(
    shape: Union[str, Path], crs: Optional[Union[str, int, dict]] = None
) -> Tuple[List[geojson.geometry.Geometry], rasterio.crs.CRS]:
    """
    A decorator to perform a check to verify a geometry is valid. Returns the function with geom set to
      the shapely Shape object.
    """
    try:
        if shape is None:
            raise ValueError
    except (KeyError, ValueError):
        logging.exception("No shape provided.")
        raise

    geom = list()
    geometry_types = list()
    try:
        with fiona.open(shape) as fio:
            logging.info("Vector read OK.")
            if crs:
                shape_crs = rasterio.crs.CRS.from_user_input(crs)
            else:
                shape_crs = rasterio.crs.CRS(fio.crs or 4326)
            for i, feat in enumerate(fio):
                g = geojson.GeoJSON(feat)
                geom.append(g["geometry"])
                geometry_types.append(g["geometry"]["type"])
    except fiona.errors.DriverError:
        logging.exception("Unable to read shape.")
        raise

    if len(geom):
        logging.info("Shapes found are {}.".format(", ".join(set(geometry_types))))
        return geom, shape_crs
    else:
        raise RuntimeError("No geometries found.")


def check_date_signature(func):
    @wraps(func)
    def func_checker(*args, **kwargs):
        """
        A decorator to reformat the deprecated `start_yr` and `end_yr` calls to subset functions and return
         `start_date` and `end_date` to kwargs. Deprecation warnings are raised for deprecated usage.
        """

        _DEPRECATION_MESSAGE = (
            '"start_yr" and "end_yr" (type: int) are being deprecated. Temporal subsets will soon exclusively'
            ' support "start_date" and "end_date" (type: str) using formats of "%Y", "%Y-%m" or "%Y-%m-%d".'
        )

        if "start_yr" in kwargs:
            warnings.warn(_DEPRECATION_MESSAGE, FutureWarning, stacklevel=3)
            if kwargs["start_yr"] is not None:
                kwargs["start_date"] = str(kwargs.pop("start_yr"))
            elif kwargs["start_yr"] is None:
                kwargs["start_date"] = None
        elif "start_date" not in kwargs:
            kwargs["start_date"] = None

        if "end_yr" in kwargs:
            if kwargs["end_yr"] is not None:
                warnings.warn(_DEPRECATION_MESSAGE, FutureWarning, stacklevel=3)
                kwargs["end_date"] = str(kwargs.pop("end_yr"))
            elif kwargs["end_yr"] is None:
                kwargs["end_date"] = None
        elif "end_date" not in kwargs:
            kwargs["end_date"] = None

        return func(*args, **kwargs)

    return func_checker


def check_start_end_dates(func):
    @wraps(func)
    def func_checker(*args, **kwargs):
        """
        A decorator to verify that start and end dates are valid in a time subsetting function.
        """
        da = args[0]
        if "start_date" not in kwargs:
            # use string for first year only - .sel() will include all time steps
            kwargs["start_date"] = da.time.min().dt.strftime("%Y").values
        if "end_date" not in kwargs:
            # use string for last year only - .sel() will include all time steps
            kwargs["end_date"] = da.time.max().dt.strftime("%Y").values

        try:
            da.time.sel(time=kwargs["start_date"])
        except KeyError:
            warnings.warn(
                '"start_date" not found within input date time range. Defaulting to minimum time step in '
                "xarray object.",
                Warning,
                stacklevel=2,
            )
            kwargs["start_date"] = da.time.min().dt.strftime("%Y").values
        try:
            da.time.sel(time=kwargs["end_date"])
        except KeyError:
            warnings.warn(
                '"end_date" not found within input date time range. Defaulting to maximum time step in '
                "xarray object.",
                Warning,
                stacklevel=2,
            )
            kwargs["end_date"] = da.time.max().dt.strftime("%Y").values

        if (
            da.time.sel(time=kwargs["start_date"]).min()
            > da.time.sel(time=kwargs["end_date"]).max()
        ):
            raise ValueError("Start date is after end date.")

        return func(*args, **kwargs)

    return func_checker


def check_lons(func):
    @wraps(func)
    def func_checker(*args, **kwargs):
        """
        A decorator to reformat user-specified "lon" or "lon_bnds" values based on the lon dimensions of a supplied
         xarray DataSet or DataArray. Examines an xarray object longitude dimensions and depending on extent
         (either -180 to +180 or 0 to +360), will reformat user-specified lon values to be synonymous with
         xarray object boundaries.
         Returns a numpy array of reformatted `lon` or `lon_bnds` in kwargs with min() and max() values.
        """
        if "lon_bnds" in kwargs:
            lon = "lon_bnds"
        elif "lon" in kwargs:
            lon = "lon"
        else:
            return func(*args, **kwargs)

        if isinstance(args[0], (xarray.DataArray, xarray.Dataset)):
            if kwargs[lon] is None:
                kwargs[lon] = np.asarray(args[0].lon.min(), args[0].lon.max())
            else:
                kwargs[lon] = np.asarray(kwargs[lon])
            if np.all(args[0].lon >= 0) and np.any(kwargs[lon] < 0):
                if isinstance(kwargs[lon], float):
                    kwargs[lon] += 360
                else:
                    kwargs[lon][kwargs[lon] < 0] += 360
            if np.all(args[0].lon <= 0) and np.any(kwargs[lon] > 0):
                if isinstance(kwargs[lon], float):
                    kwargs[lon] -= 360
                else:
                    kwargs[lon][kwargs[lon] < 0] -= 360

        return func(*args, **kwargs)

    return func_checker


@check_date_signature
def subset_shape(
    da: Union[xarray.DataArray, xarray.Dataset],
    shape: Union[str, Path],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    da_crs: Optional[str] = None,
    geometry: Optional[List[geojson.GeoJSON]] = None,
    shape_crs: Optional[str] = None,
) -> Union[xarray.DataArray, xarray.Dataset]:
    """Subset a DataArray or Dataset spatially (and temporally) using a vector shape and date selection.

    Return a subsetted data array for grid points falling within the area of a polygon and/or MultiPolygon shape,
      or grid points along the path of a LineString and/or MultiLineString.

    Parameters
    ----------
    da : Union[xarray.DataArray, xarray.Dataset]
      Input data.
    shape : Union[str, Path, geometry.GeometryCollection]
      Path to a single-layer vector file, or a shapely GeometryCollection object.
    start_date : Optional[str]
      Start date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to first day of input data-array.
    end_date : Optional[str]
      End date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to last day of input data-array.
    geometry: Optional[List[geojson.GeoJSON]]
      A list of all GeoJSON shapes to be used in clipping the xarray.DataArray or xarray.Dataset
    da_crs : Optional[Union[int, dict, str]]
      CRS of the xarray.DataArray or xarray.Dataset provided. Default: dict(epsg=4326).
    shape_crs : Optional[Union[int, dict, str]]
      CRS of the geometries provided. If passing GeometryCollections as shapes, CRS must be explicitly stated.
    start_yr : int
      Deprecated
        First year of the subset. Defaults to first year of input data-array.
    end_yr : int
      Deprecated
        Last year of the subset. Defaults to last year of input data-array.

    Returns
    -------
    Union[xarray.DataArray, xarray.Dataset]
      Subsetted xarray.DataArray or xarray.Dataset

    Warnings
    --------
    This functions relies on the rioxarray library and requires xarray Datasets and DataArrays that have been read with
     the `rioxarray` library imported. Attempting to use this function with pure xarray objects will raise exceptions.

    Examples
    --------
    >>> from xclim import subset
    >>> import rioxarray
    >>> import xarray as xr
    >>> ds = xarray.open_dataset('pr.day.nc')
    Subset lat lon and years
    >>> prSub = subset.subset_shape(ds.pr, shape="/path/to/polygon.shp", start_yr='1990', end_yr='1999')
    Subset data array lat, lon and single year
    >>> prSub = subset.subset_shape(ds.pr, shape="/path/to/polygon.shp", start_yr='1990', end_yr='1990')
    Subset data array single year keep entire lon, lat grid
    >>> prSub = subset.subset_bbox(ds.pr, start_yr='1990', end_yr='1990') # one year only entire grid
    Subset multiple variables in a single dataset
    >>> ds = xarray.open_mfdataset(['pr.day.nc','tas.day.nc'])
    >>> dsSub = subset.subset_bbox(ds, shape="/path/to/polygon.shp", start_yr='1990', end_yr='1999')
     # Subset with year-month precision - Example subset 1990-03-01 to 1999-08-31 inclusively
    >>> prSub = subset.subset_time(ds.pr, shape="/path/to/polygon.shp", start_date='1990-03', end_date='1999-08')
    # Subset with specific start_dates and end_dates
    >>> prSub = \
            subset.subset_time(ds.pr, shape="/path/to/polygon.shp", start_date='1990-03-13', end_date='1990-08-17')
    """

    if geometry and shape_crs:
        shape_crs = rasterio.crs.CRS.from_user_input(shape_crs)
    else:
        geometry, shape_crs = _read_geometries(shape, crs=shape_crs)

    try:
        # NetCDF data doesn't typically have defined CRS. Ensure this is the case and append one if needed.
        if da.rio.crs is None:
            if da_crs is None:

                if "rlon" in da.dims or "rlat" in da.dims:
                    raise NotImplementedError("Rotated poles are not supported.")

                else:
                    crs = rasterio.crs.CRS.from_epsg(4326)
                    if np.any(da.lon < -180) or np.any(da.lon > 360):
                        raise rasterio.crs.CRSError(
                            "NetCDF doesn't seem to be in EPSG:4326. Set CRS manually."
                        )

                    # Convert longitudes from 0,+360 to -180,+180
                    if np.any(da.lon > 180):
                        lon_attrs = da.lon.attrs.copy()
                        fix_lon = da.lon.values
                        fix_lon[fix_lon > 180] = fix_lon[fix_lon > 180] - 360

                        # Correct lon_bnds in xarray.Datasets
                        if isinstance(da, xarray.Dataset):
                            if "lon_bnds" in da.data_vars:
                                fix_lon_bnds = da.lon_bnds.values
                                fix_lon_bnds[fix_lon_bnds > 180] = (
                                    fix_lon_bnds[fix_lon_bnds > 180] - 360
                                )
                        da = da.assign_coords(lon=fix_lon)
                        da = da.sortby("lon")
                        da.lon.attrs = lon_attrs
            else:
                crs = rasterio.crs.CRS.from_user_input(da_crs)
        else:
            crs = da.rio.crs

    except Exception as e:
        logging.exception(e)
        raise

    if shape_crs != crs:
        raise rasterio.crs.CRSError("Shape and Raster CRS are not the same.")

    if isinstance(da, xarray.Dataset):
        # Create a new empty xarray.Dataset and populate with corrected/clipped variables
        ds_out = xarray.Dataset(data_vars=None, attrs=da.attrs)
        for v in da.data_vars:
            if "lon" in da[v].dims and "lat" in da[v].dims:
                dss = da[v]
                # Identify spatial dimensions
                dss.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=True)
                dss.rio.write_crs(crs, inplace=True)
                ds_out[v] = dss.rio.clip(
                    geometry, crs=dss.rio.crs, all_touched=True, drop=True, invert=False
                )

        for v in da.data_vars:
            if not ("lon" in da[v].dims and "lat" in da[v].dims):
                if "lat" in da[v].dims:
                    ds_out[v] = da[v].sel(lat=ds_out.lat)
                elif "lon" in da[v].dims:
                    ds_out[v] = da[v].sel(lon=ds_out.lon)
                else:
                    ds_out[v] = da[v]
    else:
        da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=True)
        da.rio.write_crs(crs, inplace=True)
        ds_out = da.rio.clip(
            geometry, crs=crs, all_touched=True, drop=True, invert=False
        )

    if start_date or end_date:
        ds_out = subset_time(ds_out, start_date=start_date, end_date=end_date)

    return ds_out


@check_lons
@check_date_signature
def subset_bbox(
    da: Union[xarray.DataArray, xarray.Dataset],
    lon_bnds: Union[np.array, Tuple[Optional[float], Optional[float]]] = None,
    lat_bnds: Union[np.array, Tuple[Optional[float], Optional[float]]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Union[xarray.DataArray, xarray.Dataset]:
    """Subset a datarray or dataset spatially (and temporally) using a lat lon bounding box and date selection.

    Return a subsetted data array for grid points falling within a spatial bounding box
    defined by longitude and latitudinal bounds and for dates falling within provided bounds.

    TODO: returns the what?
    In the case of a lat-lon rectilinear grid, this simply returns the

    Parameters
    ----------
    da : Union[xarray.DataArray, xarray.Dataset]
      Input data.
    lon_bnds : Union[np.array, Tuple[Optional[float], Optional[float]]]
      List of minimum and maximum longitudinal bounds. Optional. Defaults to all longitudes in original data-array.
    lat_bnds : Union[np.array, Tuple[Optional[float], Optional[float]]]
      List of minimum and maximum latitudinal bounds. Optional. Defaults to all latitudes in original data-array.
    start_date : Optional[str]
      Start date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to first day of input data-array.
    end_date : Optional[str]
      End date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to last day of input data-array.
    start_yr : int
      Deprecated
        First year of the subset. Defaults to first year of input data-array.
    end_yr : int
      Deprecated
        Last year of the subset. Defaults to last year of input data-array.

    Returns
    -------
    Union[xarray.DataArray, xarray.Dataset]
      Subsetted xarray.DataArray or xarray.Dataset

    Examples
    --------
    >>> from xclim import subset
    >>> ds = xarray.open_dataset('pr.day.nc')
    Subset lat lon and years
    >>> prSub = subset.subset_bbox(ds.pr, lon_bnds=[-75, -70], lat_bnds=[40, 45], start_yr='1990', end_yr='1999')
    Subset data array lat, lon and single year
    >>> prSub = subset.subset_bbox(ds.pr, lon_bnds=[-75, -70], lat_bnds=[40, 45], start_yr='1990', end_yr='1990')
    Subset dataarray single year keep entire lon, lat grid
    >>> prSub = subset.subset_bbox(ds.pr, start_yr='1990', end_yr='1990') # one year only entire grid
    Subset multiple variables in a single dataset
    >>> ds = xarray.open_mfdataset(['pr.day.nc','tas.day.nc'])
    >>> dsSub = subset.subset_bbox(ds, lon_bnds=[-75, -70], lat_bnds=[40, 45], start_yr='1990', end_yr='1999')
     # Subset with year-month precision - Example subset 1990-03-01 to 1999-08-31 inclusively
    >>> prSub = \
        subset.subset_time(ds.pr, lon_bnds=[-75, -70], lat_bnds=[40, 45],start_date='1990-03', end_date='1999-08')
    # Subset with specific start_dates and end_dates
    >>> prSub = subset.subset_time(ds.pr, lon_bnds=[-75, -70], lat_bnds=[40, 45],\
                                    start_date='1990-03-13', end_date='1990-08-17')
    """
    # start_date, end_date = _check_times(
    #     start_date=start_date, end_date=end_date, start_yr=start_yr, end_yr=end_yr
    # )

    # Rectilinear case (lat and lon are the 1D dimensions)
    if ("lat" in da.dims) or ("lon" in da.dims):

        if "lat" in da.dims and lat_bnds is not None:
            lat_bnds = _check_desc_coords(coord=da.lat, bounds=lat_bnds, dim="lat")
            da = da.sel(lat=slice(*lat_bnds))

        if "lon" in da.dims and lon_bnds is not None:
            lon_bnds = _check_desc_coords(coord=da.lon, bounds=lon_bnds, dim="lon")
            da = da.sel(lon=slice(*lon_bnds))

    # Curvilinear case (lat and lon are coordinates, not dimensions)
    elif (("lat" in da.coords) and ("lon" in da.coords)) or (
        ("lat" in da.data_vars) and ("lon" in da.data_vars)
    ):

        # Define a bounding box along the dimensions
        # This is an optimization, a simple `where` would work but take longer for large hi-res grids.
        if lat_bnds is not None:
            lat_b = assign_bounds(lat_bnds, da.lat)
            lat_cond = in_bounds(lat_b, da.lat)
        else:
            lat_b = None
            lat_cond = True

        if lon_bnds is not None:
            lon_b = assign_bounds(lon_bnds, da.lon)
            lon_cond = in_bounds(lon_b, da.lon)
        else:
            lon_b = None
            lon_cond = True

        # Crop original array using slice, which is faster than `where`.
        ind = np.where(lon_cond & lat_cond)
        args = {}
        for i, d in enumerate(da.lat.dims):
            coords = da[d][ind[i]]
            args[d] = slice(coords.min(), coords.max())
        da = da.sel(**args)

        # Recompute condition on cropped coordinates
        if lat_bnds is not None:
            lat_cond = in_bounds(lat_b, da.lat)

        if lon_bnds is not None:
            lon_cond = in_bounds(lon_b, da.lon)

        # Mask coordinates outside the bounding box
        if isinstance(da, xarray.Dataset):
            # If da is a xr.DataSet Mask only variables that have the
            # same 2d coordinates as da.lat (or da.lon)
            for var in da.data_vars:
                if set(da.lat.dims).issubset(da[var].dims):
                    da[var] = da[var].where(lon_cond & lat_cond, drop=True)
        else:

            da = da.where(lon_cond & lat_cond, drop=True)

    else:
        raise (
            Exception(
                'subset_bbox() requires input data with "lon" and "lat" dimensions, coordinates or variables'
            )
        )

    if start_date or end_date:
        da = subset_time(da, start_date=start_date, end_date=end_date)

    return da


def assign_bounds(
    bounds: Tuple[Optional[float], Optional[float]], coord: xarray.Coordinate
) -> tuple:
    """Replace unset boundaries by the minimum and maximum coordinates.

    Parameters
    ----------
    bounds : Tuple[Optional[float], Optional[float]]
      Boundaries.
    coord : xarray.Coordinate
      Grid coordinates.

    Returns
    -------
    tuple
      Lower and upper grid boundaries.

    """
    if bounds[0] > bounds[1]:
        bounds = np.flip(bounds)
    bn, bx = bounds
    bn = bn if bn is not None else coord.min()
    bx = bx if bx is not None else coord.max()
    return bn, bx


def in_bounds(bounds: Tuple[float, float], coord: xarray.Coordinate) -> bool:
    """Check which coordinates are within the boundaries."""
    bn, bx = bounds
    return (coord >= bn) & (coord <= bx)


def _check_desc_coords(coord, bounds, dim):
    """If dataset coordinates are descending reverse bounds"""
    if np.all(coord.diff(dim=dim) < 0):
        bounds = np.flip(bounds)
    return bounds


@check_lons
@check_date_signature
def subset_gridpoint(
    da: Union[xarray.DataArray, xarray.Dataset],
    lon: Optional[float] = None,
    lat: Optional[float] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Union[xarray.DataArray, xarray.Dataset]:
    """Extract a nearest gridpoint from datarray based on lat lon coordinate.

    Return a subsetted data array (or Dataset) for the grid point falling nearest the input longitude and latitude
    coordinates. Optionally subset the data array for years falling within provided date bounds.
    Time series can optionally be subsetted by dates.

    Parameters
    ----------
    da : Union[xarray.DataArray, xarray.Dataset]
      Input data.
    lon : Optional[float]
      Longitude coordinate.
    lat : Optional[float]
      Latitude coordinate.
    start_date : Optional[str]
      Start date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to first day of input data-array.
    end_date : Optional[str]
      End date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to last day of input data-array.
    start_yr : int
      Deprecated
        First year of the subset. Defaults to first year of input data-array.
    end_yr : int
      Deprecated
        Last year of the subset. Defaults to last year of input data-array.

    Returns
    -------
    Union[xarray.DataArray, xarray.Dataset]
      Subsetted xarray.DataArray or xarray.Dataset

    Examples
    --------
    >>> from xclim import subset
    >>> ds = xarray.open_dataset('pr.day.nc')
    Subset lat lon point and multiple years
    >>> prSub = subset.subset_gridpoint(ds.pr, lon=-75,lat=45,start_date='1990',end_date='1999')
    Subset lat, lon point and single year
    >>> prSub = subset.subset_gridpoint(ds.pr, lon=-75,lat=45,start_date='1990',end_date='1999')
     Subset multiple variables in a single dataset
    >>> ds = xarray.open_mfdataset(['pr.day.nc','tas.day.nc'])
    >>> dsSub = subset.subset_gridpoint(ds, lon=-75,lat=45,start_date='1990',end_date='1999')
    # Subset with year-month precision - Example subset 1990-03-01 to 1999-08-31 inclusively
    >>> prSub = subset.subset_time(ds.pr,lon=-75, lat=45, start_date='1990-03',end_date='1999-08')
    # Subset with specific start_dates and end_dates
    >>> prSub = subset.subset_time(ds.pr,lon=-75,lat=45, start_date='1990-03-13',end_date='1990-08-17')
    """

    # check if trying to subset lon and lat
    if lat is not None and lon is not None:
        # make sure input data has 'lon' and 'lat'(dims, coordinates, or data_vars)
        if hasattr(da, "lon") and hasattr(da, "lat"):
            dims = list(da.dims)

            # if 'lon' and 'lat' are present as data dimensions use the .sel method.
            if "lat" in dims and "lon" in dims:
                da = da.sel(lat=lat, lon=lon, method="nearest")
            else:
                g = Geod(ellps="WGS84")  # WGS84 ellipsoid - decent globaly
                lon1 = da.lon.values
                lat1 = da.lat.values
                shp_orig = lon1.shape
                lon1 = np.reshape(lon1, lon1.size)
                lat1 = np.reshape(lat1, lat1.size)
                # calculate geodesic distance between grid points and point of interest
                az12, az21, dist = g.inv(
                    lon1,
                    lat1,
                    np.broadcast_to(lon, lon1.shape),
                    np.broadcast_to(lat, lat1.shape),
                )
                dist = dist.reshape(shp_orig)
                iy, ix = np.unravel_index(np.argmin(dist, axis=None), dist.shape)
                xydims = [x for x in da.lon.dims]
                args = dict()
                args[xydims[0]] = iy
                args[xydims[1]] = ix
                da = da.isel(**args)
        else:
            raise (
                Exception(
                    '{} requires input data with "lon" and "lat" coordinates or data variables.'.format(
                        subset_gridpoint.__name__
                    )
                )
            )

    if start_date or end_date:
        da = subset_time(da, start_date=start_date, end_date=end_date)

    return da


@check_start_end_dates
def subset_time(
    da: Union[xarray.DataArray, xarray.Dataset],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Union[xarray.DataArray, xarray.Dataset]:
    """Subset input data based on start and end years.

    Return a subsetted data array (or dataset) for dates falling within the provided bounds.

    Parameters
    ----------
    da : Union[xarray.DataArray, xarray.Dataset]
      Input data.
    start_date : Optional[str]
      Start date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to first day of input data-array.
    end_date : Optional[str]
      End date of the subset.
      Date string format -- can be year ("%Y"), year-month ("%Y-%m") or year-month-day("%Y-%m-%d").
      Defaults to last day of input data-array.

    Returns
    -------
    Union[xarray.DataArray, xarray.Dataset]
      Subsetted xarray.DataArray or xarray.Dataset

    Examples
    --------
    >>> from xclim import subset
    >>> ds = xarray.open_dataset('pr.day.nc')
    # Subset complete years
    >>> prSub = subset.subset_time(ds.pr,start_date='1990',end_date='1999')
    # Subset single complete year
    >>> prSub = subset.subset_time(ds.pr,start_date='1990',end_date='1990')
    # Subset multiple variables in a single dataset
    >>> ds = xarray.open_mfdataset(['pr.day.nc','tas.day.nc'])
    >>> dsSub = subset.subset_time(ds,start_date='1990',end_date='1999')
    # Subset with year-month precision - Example subset 1990-03-01 to 1999-08-31 inclusively
    >>> prSub = subset.subset_time(ds.pr,start_date='1990-03',end_date='1999-08')
    # Subset with specific start_dates and end_dates
    >>> prSub = subset.subset_time(ds.pr,start_date='1990-03-13',end_date='1990-08-17')

    Notes
    -----
    TODO add notes about different calendar types. Avoid "%Y-%m-31". If you want complete month use only "%Y-%m".
    """

    return da.sel(time=slice(start_date, end_date))
