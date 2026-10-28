"""Build WOD MRB profiles from a mooring's per-instrument files: one profile a day, each
instrument's daily mean at its depth (manual p.17; p.14, note 6).

Instruments without pressure or a known depth are left out rather than placed at a guess.
"""
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from wod_etl.staging.mooring_layout import file_serial, serial_key
from wod_etl.staging.parse import parse_coordinate, pressure_to_depth

# CF standard names of what a moored instrument measures in the water, as Table 3a codes.
# What a name matcher would read as temperature, pressure or oxygen and is not a measurement
# of the water: the instrument's own temperature, the air, a derived quantity, a saturation.
_NOT_OF_THE_WATER_STANDARD = {
    "air_temperature", "air_pressure", "air_pressure_at_mean_sea_level",
    "surface_air_pressure", "dew_point_temperature", "sea_water_potential_temperature",
    "sea_water_conservative_temperature", "fractional_saturation_of_oxygen_in_sea_water"}
_NOT_OF_THE_WATER = re.compile(
    r"\b(?:internal|instrument|housing|logger|board|battery|electronics|pressure sensor|"
    r"sensor temp\w*|dew ?point|air|atmospher\w*|barometric|sea level pressure|potential|"
    r"conservative|saturation)\b|%", re.I)
_FLAG_NAME = re.compile(r"(?:^| )(?:qc|flags?|quality|qual)(?: |$)", re.I)
_BAD_FLAG = re.compile(r"bad|fail|reject|missing|invalid|erroneous|wrong", re.I)
_PRESSURE_DBAR = {"dbar": 1.0, "decibar": 1.0, "decibars": 1.0, "db": 1.0, "kpa": 0.1,
                  "pa": 1e-4, "bar": 10.0, "mbar": 0.01, "millibar": 0.01, "hpa": 0.01,
                  "psi": 0.6894757}
_TIME_UNITS = re.compile(r"^\s*(seconds?|minutes?|hours?|days?)\s+since\s+(.+?)\s*$", re.I)
_SECONDS = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}
_STANDARD_CALENDARS = ("", "standard", "gregorian", "proleptic_gregorian")
_DEPTH_ATTRS = ("instrument_depth", "nominal_instrument_depth", "nominal_depth",
                "sensor_depth", "inst_depth", "depth")
_INSTRUMENT_ATTRS = ("instrument", "model", "inst_type", "instrument_type", "make_model")
_SERIAL_ATTRS = ("serial_number", "serialnumber", "serialnumbertemperature", "serial")
CHUNK = 1_000_000
MIN_DAY_FRACTION = 0.8           # of a day's hours a variable must cover for a daily mean
DAY = 86400.0


def _attr(ds, names) -> Optional[str]:
    lowered = {a.lower(): a for a in ds.ncattrs()}
    for n in names:
        a = lowered.get(n)
        if a is not None:
            v = str(ds.getncattr(a)).strip()
            if v and v not in ("[]", "nan"):
                return v
    return None


def _number(text) -> Optional[float]:
    try:
        v = float(str(text).split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return v if math.isfinite(v) else None


def _words(*texts) -> str:
    return " ".join(str(t or "") for t in texts).replace("_", " ")


def _time(ds):
    """(time variable name, seconds per unit, epoch seconds of its reference), or None.
    Non-standard calendars are refused."""
    import pandas as pd

    for name, v in ds.variables.items():
        m = _TIME_UNITS.match(str(getattr(v, "units", "")))
        if m and v.ndim == 1:
            calendar = str(getattr(v, "calendar", "") or "").strip().lower()
            if calendar not in _STANDARD_CALENDARS:
                raise ValueError(f"its time is on the '{calendar}' calendar, not the standard "
                                 f"one: stage it with a script recipe")
            unit = m.group(1).lower().rstrip("s")
            ref = pd.Timestamp(m.group(2).replace("UTC", "").strip())
            ref = ref.tz_convert(None) if ref.tzinfo else ref
            return name, _SECONDS[unit], ref.value / 1e9
    return None


def _coordinate(ds, axis: str) -> Optional[float]:
    """The instrument's latitude or longitude: a variable (scalar, or one value along a
    length-1 dimension) or a global attribute, west and south negative."""
    # A one-letter side needs an underscore, so 'degrees' (a heading) is not read as south.
    sides = (r"_?(?:north|south)|_[ns]" if axis == "latitude" else r"_?(?:east|west)|_[ew]")
    for name, v in ds.variables.items():
        units = str(getattr(v, "units", "")).lower().replace(" ", "")
        std = str(getattr(v, "standard_name", "")).lower()
        if not (std == axis or re.fullmatch(rf"degrees?(?:{sides})", units)) or v.size != 1:
            continue
        value = float(np.ma.filled(np.ma.asarray(v[...], dtype=float), np.nan).ravel()[0])
        # An undeclared fill (-9999, 1e35) is no position.
        if math.isfinite(value) and abs(value) <= (90 if axis == "latitude" else 360):
            return -abs(value) if re.search(r"(?:west|south|_[ws])$", units) else value
    text = _attr(ds, (axis, axis[:3], f"geospatial_{axis[:3]}_min"))
    return parse_coordinate(text) if text else None


def _flags(ds, name: str, v):
    """(flag variable, the flag values that mean bad, or None without stated meanings) for a
    measurement, through `ancillary_variables` or a `<name>_qc` beside it."""
    candidates = str(getattr(v, "ancillary_variables", "") or "").split()
    candidates += [n for n in ds.variables
                   if n.lower() in (f"{name}_qc".lower(), f"{name}_flag".lower(),
                                    f"qc_{name}".lower(), f"{name}_quality".lower())]
    for n in candidates:
        fv = ds.variables.get(n)
        if fv is None:
            continue
        values = getattr(fv, "flag_values", None)
        meanings = str(getattr(fv, "flag_meanings", "") or "").split()
        if values is None or not meanings:
            return n, None
        values = np.atleast_1d(values).tolist()
        return n, [float(x) for x, m in zip(values, meanings) if _BAD_FLAG.search(m)]
    return None, None


def read_instrument(path) -> Dict[str, Any]:
    """What one instrument's file holds, without reading its record.

    `{"file", "name", "model", "instrument", "serial" (as `serial_key` compares it),
    "latitude", "longitude", "samples", "interval" (typical seconds between samples),
    "record" ([first, last] epoch seconds), "variables": [{"name", "code", "column", "units",
    "factor", "offset", "flag", "bad_flags"}], "pressure" (variable name or None), "depth",
    "depth_source", "not_stored": [labels], "notes": [...], "error"}`. Pressure is converted
    to decibars by `factor`, less `offset` (the atmosphere in an absolute pressure)."""
    import netCDF4

    from wod_etl.mapping.query_wod_spec import (STANDARD_NAME_LABELS, declared_label,
                                                match_variable, prefer_declared)
    from wod_etl.reference import wod_variables

    p = Path(path)
    out: Dict[str, Any] = {"file": str(p), "name": p.name, "variables": [], "pressure": None,
                           "depth": None, "depth_source": None, "not_stored": [],
                           "notes": [], "error": None, "model": None, "instrument": None,
                           "serial": None}
    stored = {code for code, e in wod_variables().items() if "MRB" in (e.get("datasets") or [])}
    try:
        with netCDF4.Dataset(str(p)) as ds:
            t = _time(ds)
            if t is None:
                out["error"] = "no time variable ('<unit> since <date>')"
                return out
            tvar = ds.variables[t[0]]
            tdim = tvar.dimensions[0]
            n = len(ds.dimensions[tdim])
            out["samples"] = n
            head = np.ma.filled(np.ma.asarray(tvar[:min(n, 2000)], dtype="float64"), np.nan)
            tail = np.ma.filled(np.ma.asarray(tvar[max(0, n - 2000):], dtype="float64"),
                                np.nan)
            steps = np.diff(head[np.isfinite(head)]) * t[1]
            out["interval"] = float(np.median(steps[steps > 0])) if (steps > 0).any() else None
            ends = np.concatenate([head, tail])
            ends = ends[np.isfinite(ends)] * t[1] + t[2]
            out["record"] = [float(ends.min()), float(ends.max())] if ends.size else None
            out["latitude"] = _coordinate(ds, "latitude")
            out["longitude"] = _coordinate(ds, "longitude")
            model = _attr(ds, _INSTRUMENT_ATTRS)
            if not model:                     # Sea-Bird puts it in the header it copies in
                m = re.search(r"\bSBE\s?\d+[A-Za-z+]*",
                              _attr(ds, ("fileheader", "header")) or "")
                model = m.group(0).replace(" ", "") if m else None
            serial = _attr(ds, _SERIAL_ATTRS)
            out["serial"] = serial_key(serial) or file_serial(p.name)
            out["model"] = model
            out["instrument"] = " ".join(x for x in (model, f"SN {serial}" if serial else None)
                                         if x) or None
            absolute_type = str(_attr(ds, ("pressuretype",)) or "").lower() == "absolute"
            atmosphere = _number(_attr(ds, ("pressureatmospheric",))) or 10.1325
            candidates = []
            for order, (name, v) in enumerate(ds.variables.items()):
                if name == t[0] or tdim not in v.dimensions or v.ndim == 0 or \
                        getattr(v.dtype, "kind", "U") in "SU":
                    continue
                if any(len(ds.dimensions[d]) != 1 for d in v.dimensions if d != tdim):
                    out["error"] = (f"'{name}' holds several depths in one file: one "
                                    f"instrument per file is what a mooring recipe reads")
                    return out
                if hasattr(v, "flag_values") or hasattr(v, "flag_meanings") or \
                        _FLAG_NAME.search(_words(name)):
                    continue                  # an originator's flags, not a measurement
                long_name = str(getattr(v, "long_name", "") or name)
                units = str(getattr(v, "units", "") or "").strip()
                # EPIC writes the unit into the name too: 'TEMPERATURE (C)', units 'C'.
                label = (f"{long_name} ({units})" if units and f"({units})" not in long_name
                         else long_name)
                std = str(getattr(v, "standard_name", "")).lower()
                if std in _NOT_OF_THE_WATER_STANDARD or \
                        (std not in STANDARD_NAME_LABELS and _NOT_OF_THE_WATER.search(
                            _words(name, long_name))):
                    out["not_stored"].append(f"{label} (not a measurement of the water)")
                    continue
                # A declared standard_name or EPIC code outranks the name.
                by_name = declared_label(v) is None
                if not by_name:
                    label = prefer_declared(label, v, units)
                code = match_variable(label) or (match_variable(name) if by_name else None)
                if code in (30, 31, 32) or std in ("latitude", "longitude") or \
                        code not in stored:
                    out["not_stored"].append(label)
                    continue
                entry = {"name": name, "code": code, "column": label, "units": units,
                         "factor": 1.0, "offset": 0.0}
                gauge = False
                if code == 25:
                    factor = _PRESSURE_DBAR.get(units.lower().replace(" ", ""))
                    if factor is None:
                        out["not_stored"].append(f"{label} (pressure in '{units or 'no unit'}'"
                                                 f": not a unit read as decibars)")
                        continue
                    gauge = std == "sea_water_pressure_due_to_sea_water" or bool(re.search(
                        r"sea pressure|gauge|due to sea water", _words(name, long_name),
                        re.I))
                    absolute = not gauge and (absolute_type or "absolute" in long_name.lower())
                    entry.update(factor=factor, offset=atmosphere if absolute else 0.0,
                                 column="Pressure (dbar)", units="dbar")
                entry["flag"], entry["bad_flags"] = _flags(ds, name, v)
                if entry["flag"] and entry["bad_flags"] is None:
                    out["notes"].append(f"{name}'s flags ({entry['flag']}) state no meanings, "
                                        f"so none was applied")
                # A standard name beats a name read; sea (gauge) pressure beats absolute.
                rank = (1 if by_name else 0, 0 if gauge or code != 25 else 1, order)
                candidates.append((code, rank, entry))
            for code in dict.fromkeys(c for c, _, _ in candidates):
                ranked = sorted(((r, e) for c, r, e in candidates if c == code),
                                key=lambda x: x[0])
                out["variables"].append(ranked[0][1])
                out["not_stored"] += [f"{e['column']} (a second record of one variable)"
                                      for _, e in ranked[1:]]
            order_of = {name: i for i, name in enumerate(ds.variables)}
            out["variables"].sort(key=lambda e: order_of[e["name"]])
            out["pressure"] = next((e["name"] for e in out["variables"] if e["code"] == 25),
                                   None)
            depth = _number(_attr(ds, _DEPTH_ATTRS))
            if depth is not None:
                out["depth"], out["depth_source"] = depth, "the file's depth attribute"
            else:
                for name, v in ds.variables.items():
                    if name.lower() in ("depth", "z", "dep") and v.size == 1:
                        value = float(np.ma.filled(np.ma.asarray(v[...], dtype=float),
                                                   np.nan).ravel()[0])
                        units = str(getattr(v, "units", "")).strip().lower()
                        if not math.isfinite(value) or value <= -999 or value > 12000:
                            continue                    # an undeclared fill, not a depth
                        if units in _PRESSURE_DBAR:
                            lat = out["latitude"] if out["latitude"] is not None else 45.0
                            out["depth"] = round(pressure_to_depth(
                                value * _PRESSURE_DBAR[units], lat), 2)
                            out["depth_source"] = f"its nominal pressure, {value:g} {units}"
                        else:
                            out["depth"] = value
                            out["depth_source"] = f"its '{name}' variable"
                        break
    except ValueError as e:
        out["error"] = str(e)
    except (OSError, RuntimeError, KeyError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    if not out["error"] and not out["variables"]:
        out["error"] = "no variable WOD stores in MRB (Table 3a)"
    return out


def _chunks(ds, entries: Sequence[Dict[str, Any]]):
    """(seconds, {name: values in WOD's unit, flagged-bad values as NaN}) per chunk."""
    tname, factor, ref = _time(ds)
    tvar = ds.variables[tname]
    tdim = tvar.dimensions[0]
    n = len(tvar)

    def read(v, start, stop):
        cut = tuple(slice(start, stop) if d == tdim else 0 for d in v.dimensions)
        return np.ma.filled(np.ma.asarray(v[cut], dtype="float64"), np.nan).ravel()

    for start in range(0, n, CHUNK):
        stop = min(n, start + CHUNK)
        t = read(tvar, start, stop) * factor + ref
        values = {}
        for e in entries:
            x = read(ds.variables[e["name"]], start, stop) * e["factor"] - e["offset"]
            if e.get("flag") and e.get("bad_flags"):
                x[np.isin(read(ds.variables[e["flag"]], start, stop), e["bad_flags"])] = np.nan
            values[e["name"]] = x
        yield t, values


def wet_window(path, info: Dict[str, Any]) -> Optional[List[float]]:
    """[first, last] epoch seconds an instrument that records pressure was under water: when
    it first and last reads more than half its usual pressure. None without pressure."""
    return in_water(path, info)["window"]


def in_water(path, info: Dict[str, Any]) -> Dict[str, Any]:
    """`{"window": [first, last] epoch seconds under water, "pressure": its usual pressure
    under water, dbar}` for an instrument that records pressure; Nones without."""
    import netCDF4

    none = {"window": None, "pressure": None}
    entry = next((e for e in info.get("variables") or [] if e["name"] == info.get("pressure")),
                 None)
    if entry is None or info.get("error"):
        return none
    with netCDF4.Dataset(str(path)) as ds:
        v = ds.variables[entry["name"]]
        step = max(1, (info.get("samples") or 1) // 20000)
        cut = tuple(slice(None, None, step) if i == 0 else 0 for i in range(v.ndim))
        sample = np.ma.filled(np.ma.asarray(v[cut], dtype="float64"), np.nan).ravel()
        sample = sample * entry["factor"] - entry["offset"]
        # Median of readings above 0.5 dbar, so time spent on deck is ignored.
        under = sample[np.isfinite(sample) & (sample > 0.5)]
        if under.size == 0:
            return none
        usual = float(np.median(under))
        first = last = None
        for t, values in _chunks(ds, [entry]):
            wet = (values[entry["name"]] > 0.5 * usual) & np.isfinite(t)
            if wet.any():
                idx = np.flatnonzero(wet)
                first = float(t[idx[0]]) if first is None else first
                last = float(t[idx[-1]])
    return {"window": [first, last] if first is not None else None,
            "pressure": round(usual, 2) if first is not None else None}


def deployment(windows: Sequence[Optional[Sequence[float]]]) -> Dict[str, Any]:
    """A mooring's time in the water: first in to last out of its pressure instruments' wet
    windows. `{"window", "how"}`: how is "pressure", "none" (no pressure instrument) or
    "apart" (windows do not overlap: two deployments)."""
    wins = [list(w) for w in windows if w]
    if not wins:
        return {"window": None, "how": "none"}
    if max(w[0] for w in wins) >= min(w[1] for w in wins):
        return {"window": None, "how": "apart"}
    return {"window": [min(w[0] for w in wins), max(w[1] for w in wins)], "how": "pressure"}


# A generic `platform` value, not a mooring id.
_GENERIC_PLATFORM = re.compile(r"^(?:moor\w*|buoy|surface buoy|subsurface\w*|fixed\w*|"
                               r"station|platform|unknown|none|n/?a|-)(?:\s+\w+)?$", re.I)


def mooring_id(info: Dict[str, Any]) -> str:
    """The mooring an instrument's file names (its `platform`), or '' for none."""
    platform = str(info.get("platform") or "").strip()
    return "" if not platform or _GENERIC_PLATFORM.match(platform) else platform


def group_moorings(entries: Sequence[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """(label, entries) per mooring for triage entries (`{"path", "mooring"}`): by the
    mooring id the files name, else by folder and position (0.01 degree). A group whose
    records do not overlap in time is split per deployment, labelled by its first day."""
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    by_place: List[Tuple[str, Optional[Tuple[float, float]], List[Dict[str, Any]]]] = []
    loose = []
    for f in sorted(entries, key=lambda m: m["path"]):
        mid = mooring_id(f["mooring"])
        if mid:
            by_id.setdefault(mid, []).append(f)
        else:
            loose.append(f)
    # A file naming no mooring joins the mooring id its file name starts with.
    key = lambda text: re.sub(r"[^a-z0-9]", "", str(text).lower())
    for f in list(loose):
        name = key(f["path"].rsplit("/", 1)[-1])
        mid = next((m for m in sorted(by_id, key=len, reverse=True)
                    if len(key(m)) >= 4 and name.startswith(key(m))), None)
        if mid:
            by_id[mid].append(f)
            loose.remove(f)
    for f in loose:
        info = f["mooring"]
        folder = f["path"].rsplit("/", 1)[0] if "/" in f["path"] else ""
        pos = ((info["latitude"], info["longitude"]) if info.get("latitude") is not None
               and info.get("longitude") is not None else None)
        for g_folder, g_pos, g_members in by_place:
            if g_folder == folder and (pos is None or g_pos is None or
                                       max(abs(pos[0] - g_pos[0]), abs(pos[1] - g_pos[1]))
                                       < 0.01):
                g_members.append(f)
                break
        else:
            by_place.append((folder, pos, [f]))
    groups = [(mid, fs) for mid, fs in by_id.items()]
    for folder, _pos, fs in by_place:
        name = folder.rsplit("/", 1)[-1]
        groups.append(("" if name.lower() in ("0-data", "data", "") else name or "", fs))
    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    seen: set = set()
    for label, fs in groups:
        label = label or "mooring"
        parts = _deployments(fs)
        for part, name in ([(parts[0], label)] if len(parts) == 1 else
                           [(part, f"{label}_" + (iso(min(m["mooring"]["record"][0]
                                                         for m in part
                                                         if m["mooring"].get("record")))[:10]
                                                  if any(m["mooring"].get("record")
                                                         for m in part) else "undated"))
                            for part in parts]):
            # Two moorings in one folder share its name: each label names one mooring.
            unique, n = name, 2
            while unique in seen:
                unique, n = f"{name}_{n}", n + 1
            seen.add(unique)
            out.append((unique, part))
    return out


def _deployments(entries: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Entries split where their records stop overlapping in time; one without a record
    stays with the first."""
    timed = sorted((m for m in entries if (m["mooring"].get("record") or [None])[0] is not None),
                   key=lambda m: m["mooring"]["record"][0])
    parts: List[List[Dict[str, Any]]] = []
    end = None
    for m in timed:
        first, last = m["mooring"]["record"]
        if parts and first <= end:
            parts[-1].append(m)
            end = max(end, last)
        else:
            parts.append([m])
            end = last
    untimed = [m for m in entries if m not in timed]
    if not parts:
        return [untimed]
    parts[0] = sorted(parts[0] + untimed, key=lambda m: m["path"])
    return [sorted(p, key=lambda m: m["path"]) for p in parts]


def daily_means(path, info: Dict[str, Any], window: Optional[Sequence[float]] = None
                ) -> Dict[int, Dict[str, Any]]:
    """{day number (days since 1970-01-01, UTC): {"samples", "means": {code: mean},
    "coverage": {code: fraction of the day's hours it has a value in}}} for one instrument,
    from the samples within `window` (epoch seconds). Coverage counts hours (or the
    instrument's own step if longer), so a change of sampling rate is not a gap."""
    import netCDF4

    entries = info["variables"]
    step = max(3600.0, float(info.get("interval") or 0.0))
    per_day = max(1, round(DAY / step))
    sums: Dict[int, Dict[str, List[float]]] = {}
    slots: Dict[int, Dict[str, set]] = {}
    counts: Dict[int, int] = {}
    with netCDF4.Dataset(str(path)) as ds:
        for t, values in _chunks(ds, entries):
            keep = np.isfinite(t)
            if window is not None:
                keep &= (t >= window[0]) & (t <= window[1])
            if not keep.any():
                continue
            tk = t[keep]
            day = np.floor(tk / DAY).astype("int64")
            slot = np.floor(tk / step).astype("int64")
            days, inverse = np.unique(day, return_inverse=True)
            for d, c in zip(days.tolist(), np.bincount(inverse).tolist()):
                counts[d] = counts.get(d, 0) + int(c)
            for e in entries:
                x = values[e["name"]][keep]
                ok = np.isfinite(x)
                s = np.bincount(inverse[ok], weights=x[ok], minlength=days.size)
                c = np.bincount(inverse[ok], minlength=days.size)
                for i, d in enumerate(days.tolist()):
                    if c[i]:
                        acc = sums.setdefault(d, {}).setdefault(e["name"], [0.0, 0])
                        acc[0] += float(s[i])
                        acc[1] += int(c[i])
                for d, sl in set(zip(day[ok].tolist(), slot[ok].tolist())):
                    slots.setdefault(d, {}).setdefault(e["name"], set()).add(sl)
    code_of = {e["name"]: e["code"] for e in entries}
    return {d: {"samples": counts.get(d, 0),
                "means": {code_of[name]: total / k for name, (total, k) in by.items() if k},
                "coverage": {code_of[name]: min(1.0, len(s) / per_day)
                             for name, s in (slots.get(d) or {}).items()}}
            for d, by in sums.items()}


def mooring_days(paths: Sequence, instruments: Sequence[Dict[str, Any]],
                 window: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Every instrument's daily means within the deployment `window` (epoch seconds; None:
    the whole record): `{"window", "daily": [per instrument, as daily_means]}`."""
    return {"window": list(window) if window else None,
            "daily": [daily_means(p, i, window) if not i.get("error") else {}
                      for p, i in zip(paths, instruments)]}


def mooring_profiles(instruments: Sequence[Dict[str, Any]], days: Dict[str, Any],
                     depths: Dict[str, Any], latitude: float,
                     min_day_fraction: float = MIN_DAY_FRACTION,
                     placed: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One profile per day: each instrument at its depth, shallowest first, with each of its
    variables that covered at least `min_day_fraction` of the day. `depths` maps an
    instrument's file name to metres below the surface, or to "pressure" to place it by its
    daily mean pressure; `placed` says, per file name, where a depth in metres came from.

    Returns `{"profiles": [{"day": "YYYY-MM-DD", "levels": [{"depth", "means": {code: value},
    "instrument", "file", "samples", "placed_by"}]}], "left_out": [{"file", "day", "codes",
    "coverage"}], "window"}`."""
    by_day: Dict[int, List[Dict[str, Any]]] = {}
    left_out = []
    for info, daily in zip(instruments, days["daily"]):
        how = depths.get(info["name"])
        if not daily or how is None or str(how).strip() in ("", "?"):
            continue                    # no depth to place it at: the recipe check says so
        for day, d in sorted(daily.items()):
            means = {c: m for c, m in d["means"].items()
                     if d["coverage"].get(c, 0.0) >= min_day_fraction}
            short = sorted(c for c in d["means"] if c not in means)
            if short:
                left_out.append({"file": info["name"], "day": _date(day), "codes": short,
                                 "coverage": round(min(d["coverage"].get(c, 0.0)
                                                       for c in short), 2)})
            if how == "pressure":
                if 25 not in means:
                    continue
                depth = pressure_to_depth(means[25], latitude)
                where = f"its mean pressure, {means[25]:.1f} dbar"
            else:
                depth = float(how)
                where = "its stated depth" if not (placed or {}).get(info["name"]) else \
                    f"its depth, {placed[info['name']]}"
            if not [c for c in means if c != 25]:
                continue                # a depth with nothing measured at it
            by_day.setdefault(day, []).append({
                "depth": round(depth, 2), "means": means, "file": info["name"],
                "instrument": info.get("instrument"), "samples": d["samples"],
                "placed_by": where})
    profiles = [{"day": _date(day), "levels": sorted(levels, key=lambda lv: lv["depth"])}
                for day, levels in sorted(by_day.items())]
    window = days.get("window")
    return {"profiles": profiles, "left_out": left_out,
            "window": [iso(w) for w in window] if window else None}


def iso(seconds: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text: str) -> float:
    """Epoch seconds of an ISO time (UTC unless it says otherwise)."""
    import pandas as pd

    ts = pd.Timestamp(str(text).strip())
    ts = ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts
    return ts.value / 1e9


def _date(day: int) -> str:
    import datetime as dt

    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(day))).isoformat()
