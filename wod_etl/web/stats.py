"""Descriptive statistics of a file's variables, for the preview's Statistics tab.

A written file is summarized per ragged dimension. An input NetCDF file is summarized as
stored; any other input by the columns `read_any` splits it into, before any staging.

Numbers get count, quartiles, mean, standard deviation and a histogram; flags and text their
most common values; times their first and last.
"""
import re
import warnings
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from wod_etl.web.preview import (_cast_count, _counts, _flag_names, _level_dimension,
                                 _times, _values)
from wod_etl.writers.generate_netcdf_ragged import CAST_DIM

BINS = 24                            # histogram bins, between a variable's min and max
TOP = 8                              # values listed for a flag or text variable
SAMPLE = 200                         # text values tried as dates before parsing them all
LIMIT = 5_000_000                    # values of one stored NetCDF variable read in full

# Text that starts like a date: 2023-06-16, 2023/6/16 12:14, 16.06.2023.
_DATE = re.compile(r"^\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")


def output_stats(path: str) -> Dict[str, Any]:
    """Each variable of a written file, grouped by dimension: levels (or Taxa-Records) first,
    then casts. Count variables are structure, not data, and are left out."""
    import netCDF4

    with netCDF4.Dataset(str(path)) as ds:
        level = _level_dimension(ds, _counts(ds))
        flags = _flag_names(ds)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for name, v in ds.variables.items():
            if v.ndim != 1 or hasattr(v, "sample_dimension"):
                continue
            dim = v.dimensions[0]
            if name == "time" and dim == CAST_DIM:
                summary = _cast_times(_times(ds, _cast_count(ds)))
            else:
                summary = summarize(_series(v), flag=name in flags or hasattr(v, "flag_meanings"),
                                    meanings=_meanings(v))
            groups.setdefault(dim, []).append({"name": name, "units": getattr(v, "units", None),
                                               "long_name": getattr(v, "long_name", None),
                                               **summary})
        order = sorted(groups, key=lambda d: (d != level, d != CAST_DIM))
        return {"level_dimension": level,
                "groups": [{"dimension": d, "size": len(ds.dimensions[d]),
                            "variables": groups[d]} for d in order]}


def input_stats(path: str) -> Dict[str, Any]:
    """An input file's variables. A NetCDF file's are described as stored; a table's columns
    across all its casts, as `read_any` splits them."""
    from wod_etl.discovery import read_any, sniff_format
    from wod_etl.mapping.map_metadata import is_flag_column

    fmt = sniff_format(str(path))
    if fmt.get("reader") == "netcdf":
        return {"format": fmt.get("format_family"), "stored": True, "error": None,
                "groups": _stored_netcdf(path)}
    casts, info = read_any(str(path), sniff=fmt)
    out = {"format": fmt.get("format_family"), "stored": False, "casts": len(casts),
           "warnings": info.get("warnings") or []}
    if not casts:
        return {**out, "error": info.get("error") or "no table read", "groups": []}
    frames = [c["df"] for c in casts]
    variables = [{"name": name, "units": None, "long_name": None,
                  **summarize(s, flag=is_flag_column(name))}
                 for name, s in _columns(frames).items()]
    return {**out, "error": None,
            "groups": [{"dimension": None, "size": sum(len(df) for df in frames),
                        "variables": variables}]}


def _stored_netcdf(path: str) -> List[Dict[str, Any]]:
    """Each variable of any NetCDF file, grouped by its dimensions, largest group first.

    Undeclared fill (EPIC's 1e35) counts as missing. A variable over `LIMIT` values is read at
    an even step along its first dimension."""
    import netCDF4

    from wod_etl.discovery.read_any import _decode_time, _is_clock, honour_fill
    from wod_etl.staging.moorings import UNDECLARED_FILL

    with netCDF4.Dataset(str(path)) as ds:
        flags = _flag_names(ds)
        groups: Dict[Any, Dict[str, Any]] = {}
        for name, v in ds.variables.items():
            if not v.ndim:
                continue
            step = -(-v.size // LIMIT)
            data = np.ma.asarray(honour_fill(v)[::step] if step > 1 else honour_fill(v)[:])
            if data.dtype.kind == "S":
                data = netCDF4.chartostring(np.ma.filled(data, b"")) if data.ndim > 1 else data
                s = pd.Series([x.decode("utf-8", "replace") if isinstance(x, bytes) else x
                               for x in np.ma.getdata(data).ravel()], dtype=object)
            elif v.dtype is str or data.dtype.kind in "OU":
                s = pd.Series(_values(data.ravel()), dtype=object)
            else:
                x = np.ma.filled(data.astype(float), np.nan).ravel()
                x[np.abs(x) >= UNDECLARED_FILL] = np.nan
                s = pd.Series(x)
            if _is_clock(v):
                summary = _clock(s, lambda t: _decode_time(t, v.units, getattr(v, "calendar", None)))
            else:
                summary = summarize(s, flag=name in flags or hasattr(v, "flag_meanings"),
                                    meanings=_meanings(v))
            key = "one value each" if v.size == 1 else ", ".join(v.dimensions)
            group = groups.setdefault(key, {"dimension": key, "variables": [],
                                            "size": int(v.size) if v.size > 1 else None})
            group["variables"].append({"name": name, "units": getattr(v, "units", None),
                                       "long_name": getattr(v, "long_name", None),
                                       "every": step if step > 1 else None, **summary})
        return sorted(groups.values(), key=lambda g: (-(g["size"] or 0),
                                                      -g["dimension"].count(",")))


def _clock(s: pd.Series, decode) -> Dict[str, Any]:
    """A CF time variable's first and last time, to the second, as its units count them."""
    x = s.to_numpy(dtype=float)
    ok = x[np.isfinite(x)]
    first, last = (decode(float(t)) for t in (ok.min(), ok.max())) if ok.size else (None, None)
    if first is None or last is None:
        return _numbers(x, len(x))
    first, last = _to_second(first), _to_second(last)
    seconds = bool(first.second or last.second)
    return {"kind": "time", "n": int(ok.size), "missing": len(x) - int(ok.size),
            "first": _stamp(first, seconds), "last": _stamp(last, seconds)}


def _to_second(when):
    """Rounded to the nearest second: an hourly clock in float days lands a hair early."""
    from datetime import timedelta

    micro = getattr(when, "microsecond", 0)
    return when + timedelta(microseconds=1_000_000 - micro if micro >= 500_000 else -micro)


def _stamp(when, seconds: bool) -> str:
    """From the fields, so any calendar works."""
    return (f"{when.year:04d}-{when.month:02d}-{when.day:02d} {when.hour:02d}:{when.minute:02d}"
            + (f":{when.second:02d}" if seconds else ""))


def summarize(s: pd.Series, flag: bool = False,
              meanings: Optional[Dict[float, str]] = None) -> Dict[str, Any]:
    """One variable's summary. `n` values were read, `missing` were empty; a numeric column
    holding some text (`ND`, `<0.01`) also lists that text, which counts as neither."""
    if flag:
        return _counted(s, "flag", meanings or {})
    if pd.api.types.is_bool_dtype(s):
        return _counted(s.astype(object), "text", {})
    if pd.api.types.is_numeric_dtype(s):
        return _numbers(s.to_numpy(dtype=float, na_value=np.nan), len(s))
    if pd.api.types.is_datetime64_any_dtype(s):
        return _dates(s, len(s))
    text = s.dropna().astype(str).str.strip()
    text = text[text != ""]
    numbers = pd.to_numeric(text, errors="coerce")
    if numbers.notna().sum() > len(text) / 2:
        out = _numbers(numbers.to_numpy(dtype=float, na_value=np.nan), len(s))
        other = text[numbers.isna()]
        out["missing"] = len(s) - len(text)
        if len(other):
            out["text"] = {"count": len(other), "values": _top(other.value_counts(), {})}
        return out
    dates = _parsed_dates(text)
    if dates is not None:
        return _dates(dates, len(s))
    return _counted(text, "text", {}, size=len(s))


def _numbers(x: np.ndarray, size: int) -> Dict[str, Any]:
    ok = x[np.isfinite(x)]
    out: Dict[str, Any] = {"kind": "number", "n": int(ok.size), "missing": size - int(ok.size)}
    if not ok.size:
        return {**out, "kind": "empty"}
    q = np.percentile(ok, [0, 25, 50, 75, 100])
    counts = (np.histogram(ok, bins=BINS, range=(q[0], q[4]))[0] if q[0] < q[4]
              else np.array([ok.size]))
    return {**out, "min": float(q[0]), "p25": float(q[1]), "median": float(q[2]),
            "p75": float(q[3]), "max": float(q[4]), "mean": float(ok.mean()),
            "std": float(ok.std(ddof=1)) if ok.size > 1 else None,
            "histogram": counts.tolist()}


def _counted(s: pd.Series, kind: str, meanings: Dict[float, str],
             size: Optional[int] = None) -> Dict[str, Any]:
    """The most common values; a flag's in scale order, each with its meaning."""
    present = s.dropna()
    counts = present.value_counts()
    top = counts.head(TOP)
    if kind == "flag":
        try:
            top = top.sort_index()
        except TypeError:                               # numbers and text mixed
            pass
    out = {"kind": kind, "n": len(present), "missing": (size or len(s)) - len(present),
           "distinct": len(counts), "values": _top(top, meanings)}
    return out if len(present) else {**out, "kind": "empty"}


def _top(counts: pd.Series, meanings: Dict[float, str]) -> List[Dict[str, Any]]:
    out = []
    for value, count in counts.head(TOP).items():
        value = value.item() if isinstance(value, np.generic) else value
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        try:
            meaning = meanings.get(float(value))
        except (TypeError, ValueError):
            meaning = None
        out.append({"value": value if isinstance(value, (int, float)) else str(value),
                    "count": int(count), "meaning": meaning})
    return out


def _dates(t: pd.Series, size: int) -> Dict[str, Any]:
    ok = t.dropna()
    out: Dict[str, Any] = {"kind": "time", "n": len(ok), "missing": size - len(ok)}
    if not len(ok):
        return {**out, "kind": "empty"}
    zone = ""
    if ok.dt.tz is not None:
        ok, zone = ok.dt.tz_convert("UTC"), " UTC"
    fmt = ("%Y-%m-%d" if (ok == ok.dt.normalize()).all() else
           "%Y-%m-%d %H:%M" if (ok.dt.second == 0).all() else "%Y-%m-%d %H:%M:%S")
    return {**out, "first": ok.min().strftime(fmt) + zone, "last": ok.max().strftime(fmt) + zone}


def _parsed_dates(text: pd.Series) -> Optional[pd.Series]:
    """Text as times, when nearly all of it reads as dates; None otherwise."""
    sample = text.head(SAMPLE)
    if not len(sample) or sample.str.match(_DATE).mean() < 0.9:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dates = pd.to_datetime(text, errors="coerce")
    except (ValueError, TypeError, OverflowError):      # e.g. mixed time zones
        return None
    return dates if dates.notna().mean() >= 0.9 else None


def _cast_times(times: List[Optional[str]]) -> Dict[str, Any]:
    """Cast times as the cast list shows them: UTC, or the date alone without a time of day.
    These strings sort in time order."""
    ok = [t for t in times if t]
    out = {"kind": "time", "n": len(ok), "missing": len(times) - len(ok)}
    return {**out, "first": min(ok), "last": max(ok)} if ok else {**out, "kind": "empty"}


def _series(v) -> pd.Series:
    """A NetCDF variable as floats with NaN for fill, or as text with None."""
    data = v[:]
    if v.dtype is str or np.ma.getdata(data).dtype.kind in "OSU":
        return pd.Series(_values(data), dtype=object)
    return pd.Series(np.ma.filled(np.ma.asarray(data, dtype=float), np.nan))


def _meanings(v) -> Dict[float, str]:
    """A flag variable's CF `flag_values` to `flag_meanings`."""
    values, meanings = getattr(v, "flag_values", None), getattr(v, "flag_meanings", None)
    if values is None or not meanings:
        return {}
    return dict(zip(np.atleast_1d(values).astype(float).tolist(), str(meanings).split()))


def _columns(frames: List[pd.DataFrame]) -> Dict[str, pd.Series]:
    """Each column across every cast, in order of first appearance. A name repeated within
    one table is numbered, so its columns stay apart."""
    parts: Dict[str, List[pd.Series]] = {}
    for df in frames:
        seen: Dict[str, int] = {}
        for j, name in enumerate(map(str, df.columns)):
            seen[name] = seen.get(name, 0) + 1
            key = name if seen[name] == 1 else f"{name} ({seen[name]})"
            parts.setdefault(key, []).append(df.iloc[:, j].reset_index(drop=True))
    return {k: v[0] if len(v) == 1 else pd.concat(v, ignore_index=True)
            for k, v in parts.items()}
