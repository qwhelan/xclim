import logging
import warnings
from functools import wraps
from pathlib import Path
from typing import Optional
from typing import Tuple
from typing import Union

import geojson
import fiona
import rioxarray
import rasterio.crs
import numpy as np
import shapely.geometry
import xarray
from pyproj import Geod

__all__ = ["subset_bbox", "subset_gridpoint", "subset_time"]
logging.basicConfig(level=logging.INFO)


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


def check_geometry(func):
    @wraps(func)
    def func_checker(*args, **kwargs):
        """
        A decorator to perform a check to verify a geometry is either a Polygon or a MultiPolygon.
          Returns the function with geom set to the shapely Shape object.
        """
        try:
            shape = kwargs["shape"]
            if shape is None:
                raise ValueError
        except (KeyError, ValueError):
            logging.exception("No shape provided.")
            raise

        if "use_all_features" in kwargs:
            use_all_features = bool(kwargs["use_all_features"])
        else:
            use_all_features = False

        if not isinstance(shape, shapely.geometry.GeometryCollection):
            geom = list()
            geometry_types = list()
            try:
                fio = fiona.open(shape)
                logging.info("Geometry OK.")
                if "crs" not in kwargs:
                    kwargs["crs"] = rasterio.crs.CRS(fio.crs).to_epsg()
                if use_all_features:
                    for i, feat in enumerate(fio):
                        g = geojson.loads(
                            feat["geometry"]
                        )  # shapely.geometry.shape(feat["geometry"])
                        geom.append(g)
                        geometry_types.append(g.geom_type)
                else:
                    # g = shapely.geometry.shape(next(iter(fio))["geometry"])
                    g = geojson.loads(next(iter(fio))["geometry"])
                    geom.append(g)
                    geometry_types.append(g.geom_type)
                fio.close()
            except fiona.errors.DriverError:
                logging.exception("Unable to load vector as shapely.geometry.shape().")
        else:
            geom = shape
            geometry_types = shape.geom_type

        if geom[0].is_valid:
            print(geom)
            kwargs["geometry"] = geom[0]  # geojson.GeometryCollection(geom)
            logging.info("Shapes found are {}.".format(", ".join(set(geometry_types))))
            return func(*args, **kwargs)
        raise RuntimeError("No appropriate geometries found.")

    return func_checker


@check_geometry
@check_date_signature
def subset_shape(
    da: Union[xarray.DataArray, xarray.Dataset],
    shape: Union[str, Path],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    geometry: Optional[shapely.geometry.GeometryCollection] = None,
    crs: Optional[str] = None,
    use_all_features: bool = False,
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
    geometry: Optional[geometry.GeometryCollection]
    crs : Optional[str]
      CRS of the geometries provided. If passing GeometryCollections as shapes, CRS must be explicitly stated.
    use_all_features : bool
      Use either the first found feature geometry or the union of all found features. Default: False.
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
    vectors = geometry or shape
    assert da.rio.clip

    crs = rasterio.crs.CRS().from_epsg(crs)
    print(crs, da.rio.crs)
    da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=True)
    da.rio.write_crs(crs, inplace=True)
    print(crs, da.rio.crs)

    clipped = da.rio.clip(
        geometries=vectors, crs=da.rio.crs, all_touched=False, drop=True, invert=False
    )
    if start_date or end_date:
        clipped = subset_time(clipped, start_date=start_date, end_date=end_date)

    return clipped


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


if __name__ == "__main__":
    import rioxarray
    import xarray
    import xclim.subset
    import geojson
    import shapely.geometry
    a = "/home/tjs/Downloads/map.geojson"
    nc = (
        "/home/tjs/Downloads/nc_data/pr_Amon_CanESM2_historical_r1i1p1_185001-200512.nc"
    )
    with open(a) as gj:
        g = geojson.loads(gj.read())
    ds = xarray.open_dataset(nc)
    ss = xclim.subset.subset_shape(ds, shape=g)
