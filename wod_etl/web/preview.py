"""Read a WOD file this package wrote, for the browser preview.

Nothing here depends on the writer's variable names, except time for display. A variable on
`casts` is a per-cast field. A count variable's `sample_dimension` says which values on that
dimension belong to each cast (CF contiguous ragged arrays). So fields the writer adds later
show up without changes here.
"""
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from wod_etl.writers.generate_netcdf_ragged import CAST_DIM


def exit_with(parent: int) -> None:
    """Worker initializer: end the worker once the server that started it is gone. A server
    stopped by a signal or killed runs no exit handlers, so it cannot stop the worker itself."""
    def watch():
        while os.getppid() == parent:
            time.sleep(1)
        os._exit(0)

    threading.Thread(target=watch, name="exit with the server", daemon=True).start()


def describe(path: str) -> Dict[str, Any]:
    """The file's attributes, dimensions and variables, and an index of its casts."""
    import netCDF4

    with netCDF4.Dataset(str(path)) as ds:
        counts = _counts(ds)
        level_dim = _level_dimension(ds, counts)
        n = _cast_count(ds)
        ids = _column(ds, "cast_id") or [str(i) for i in range(n)]
        lats, lons = _column(ds, "lat"), _column(ds, "lon")
        datasets = _column(ds, "dataset")
        sizes = counts[level_dim][2].tolist() if level_dim else None
        times = _times(ds, n)
        casts = [{"index": i, "id": ids[i] or str(i), "time": times[i],
                  "lat": lats[i] if lats else None, "lon": lons[i] if lons else None,
                  "dataset": datasets[i] if datasets else None,
                  "size": sizes[i] if sizes else None} for i in range(n)]
        return {"attributes": {k: _attr(ds.getncattr(k)) for k in ds.ncattrs()},
                "dimensions": {name: len(d) for name, d in ds.dimensions.items()},
                "variables": [{"name": name, "dimensions": list(v.dimensions),
                               "type": "string" if v.dtype is str else v.dtype.name,
                               "attributes": {k: _attr(v.getncattr(k)) for k in v.ncattrs()}}
                              for name, v in ds.variables.items()],
                "level_dimension": level_dim, "casts": casts}


def cast(path: str, index: int) -> Dict[str, Any]:
    """One cast: its per-cast fields, and its values on each ragged dimension.

    A dimension holding a single variable (the cast's variable codes) is given as a field;
    the others are tables, with columns holding no value for this cast listed in `empty`.
    """
    import netCDF4

    with netCDF4.Dataset(str(path)) as ds:
        if not 0 <= index < _cast_count(ds):
            raise IndexError(f"no cast {index}")
        flags = _flag_names(ds)
        fields = [_field(name, v, _values(v[index:index + 1])[0])
                  for name, v in ds.variables.items() if v.dimensions == (CAST_DIM,)]
        tables = []
        for dim, (_, starts, sizes) in _counts(ds).items():
            lo, hi = int(starts[index]), int(starts[index] + sizes[index])
            on_dim = [(name, v) for name, v in ds.variables.items() if v.dimensions == (dim,)]
            if len(on_dim) == 1:
                name, v = on_dim[0]
                fields.append(_field(name, v, _values(v[lo:hi])))
                continue
            columns, empty = [], []
            for name, v in on_dim:
                values = _values(v[lo:hi])
                if all(x is None for x in values):
                    empty.append(name)
                    continue
                columns.append({**_field(name, v, None), "values": values,
                                "numeric": v.dtype is not str and v.dtype.kind in "iuf",
                                "flag": name in flags,
                                "down": getattr(v, "axis", "") == "Z"
                                        or getattr(v, "positive", "") == "down"})
            tables.append({"dimension": dim, "rows": hi - lo, "columns": columns,
                           "empty": empty})
        return {"index": index, "fields": fields, "tables": tables}


def _cast_count(ds) -> int:
    return len(ds.dimensions[CAST_DIM]) if CAST_DIM in ds.dimensions else 0


def _counts(ds) -> Dict[str, Tuple[str, np.ndarray, np.ndarray]]:
    """`{sample dimension: (count variable, first index per cast, count per cast)}`."""
    out = {}
    for name, v in ds.variables.items():
        dim = getattr(v, "sample_dimension", None)
        if v.dimensions == (CAST_DIM,) and dim in ds.dimensions:
            sizes = np.ma.filled(v[:], 0).astype(np.int64)
            out[dim] = (name, np.concatenate([[0], np.cumsum(sizes)[:-1]]), sizes)
    return out


def _level_dimension(ds, counts) -> Optional[str]:
    """The ragged dimension holding the most variables: levels, or Taxa-Records."""
    held = {dim: sum(v.dimensions == (dim,) for v in ds.variables.values()) for dim in counts}
    return max(held, key=held.get) if held else None


def _flag_names(ds) -> set:
    """Variables some other variable names as its flags (CF `ancillary_variables`)."""
    return {name for v in ds.variables.values()
            for name in str(getattr(v, "ancillary_variables", "")).split()}


def _column(ds, name: str) -> Optional[List[Any]]:
    v = ds.variables.get(name)
    return _values(v[:]) if v is not None and v.dimensions == (CAST_DIM,) else None


def _times(ds, n: int) -> List[Optional[str]]:
    """Each cast's time, UTC; the date alone when `GMT_time` says no time of day was stated."""
    import netCDF4

    v = ds.variables.get("time")
    if v is None or v.dimensions != (CAST_DIM,) or not hasattr(v, "units"):
        return [None] * n
    days = np.ma.filled(np.ma.asarray(v[:], dtype=float), np.nan)
    gmt = ds.variables.get("GMT_time")
    no_time = (np.ma.getmaskarray(gmt[:]) if gmt is not None and gmt.dimensions == (CAST_DIM,)
               else np.zeros(n, bool))
    out: List[Optional[str]] = [None] * n
    ok = np.flatnonzero(np.isfinite(days))
    try:
        dates = netCDF4.num2date(days[ok], v.units, getattr(v, "calendar", "standard"),
                                 only_use_cftime_datetimes=False,
                                 only_use_python_datetimes=True)
    except (ValueError, TypeError):
        return out
    for i, d in zip(ok, np.atleast_1d(dates)):
        out[i] = (d.strftime("%Y-%m-%d") if no_time[i] else
                  d.strftime("%Y-%m-%d %H:%M:%S" if d.second else "%Y-%m-%d %H:%M") + " UTC")
    return out


def _field(name: str, v, value) -> Dict[str, Any]:
    return {"name": name, "value": value, "long_name": getattr(v, "long_name", None),
            "units": getattr(v, "units", None)}


def _values(data) -> List[Any]:
    """Values as JSON: fill, NaN and empty text are None; a float32 keeps its shortest text."""
    mask = np.ma.getmaskarray(data)
    raw = np.ma.getdata(data)
    if raw.dtype.kind == "f":
        out = [None if m else float(s) for s, m in zip(raw.astype(str), mask)]
        return [x if x is None or math.isfinite(x) else None for x in out]
    if raw.dtype.kind in "iu":
        return [None if m else int(x) for x, m in zip(raw.tolist(), mask)]
    return [None if m or x is None or str(x) == "" else str(x)
            for x, m in zip(raw.tolist(), mask)]


def _attr(value) -> Any:
    """An attribute as JSON. NaN, a common fill value, is shown as text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray):
        return [_attr(x) for x in value]
    if isinstance(value, np.floating):
        value = float(str(value))
    elif isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value
