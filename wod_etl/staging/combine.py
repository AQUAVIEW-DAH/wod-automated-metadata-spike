"""Combine one cast's levels split over several files into one cast per station and date.

Casts from different tables with the same station, date and position are merged by depth.

    from wod_etl.staging.combine import combine_files
    casts, info = combine_files([path_a, path_b])
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Two files' casts are one only where their positions agree this closely (degrees).
POSITION_TOLERANCE = 0.05


def header_value(block: str, key: str) -> Optional[str]:
    m = re.search(rf"(?mi)^{re.escape(key)}:\s*(.+?)\s*$", block or "")
    return m.group(1) if m else None


def cast_key(cast: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(station, YYYY-MM-DD) of a cast split from a table, or None."""
    from wod_etl.staging.parse import parse_timestamp
    from wod_etl.staging.station_list import normalise_station

    station = header_value(cast.get("header_block", ""), "Station")
    date = header_value(cast.get("header_block", ""), "Date")
    stamp = parse_timestamp(date) if date else None
    if not station or stamp is None:
        return None
    return normalise_station(station), f"{stamp:%Y-%m-%d}"


def position(cast: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        return (float(header_value(cast["header_block"], "Latitude")),
                float(header_value(cast["header_block"], "Longitude")))
    except (TypeError, ValueError):
        return None


def keyed_casts(path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """A table's casts by (station, date); empty unless it splits into two or more."""
    from wod_etl.discovery import read_any, sniff_format

    casts, _ = read_any(str(path), sniff_format(str(path)))
    out = {}
    for c in casts if len(casts) > 1 else []:
        key = cast_key(c)
        if key and key not in out:
            out[key] = c
    return out


def same_casts(a: Dict, b: Dict) -> bool:
    """Whether two tables' casts are the same casts: most stations and dates shared, and each
    shared one at the same position."""
    shared = a.keys() & b.keys()
    if len(shared) < max(2, min(len(a), len(b)) / 2):
        return False
    for k in shared:
        pa, pb = position(a[k]), position(b[k])
        if not pa or not pb or abs(pa[0] - pb[0]) > POSITION_TOLERANCE \
                or abs(pa[1] - pb[1]) > POSITION_TOLERANCE:
            return False
    return True


def _depth_column(df) -> Optional[str]:
    from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable

    return next((c for c in df.columns if match_variable(str(c)) == DEPTH_SENTINEL_CODE), None)


def merge_levels(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """Levels of one cast from several tables, merged on depth. A depth sampled twice in one
    table stays two levels; a column in two tables keeps the first table's value where it
    has one."""
    depth = next((d for d in map(_depth_column, frames) if d), None)
    if depth is None:
        return pd.concat(frames, ignore_index=True)
    merged = None
    for df in frames:
        d = _depth_column(df)
        if d is None:
            continue
        df = df.rename(columns={d: depth}).copy()
        df[depth] = pd.to_numeric(df[depth], errors="coerce")
        df["_n"] = df.groupby(depth).cumcount()
        if merged is None:
            merged = df
            continue
        both = (set(df.columns) & set(merged.columns)) - {depth, "_n"}
        merged = merged.merge(df, on=[depth, "_n"], how="outer", suffixes=("", "__2"))
        for c in both:
            merged[c] = merged[c].combine_first(merged.pop(f"{c}__2"))
    merged = merged.sort_values([depth, "_n"], kind="stable").drop(columns="_n")
    return merged.reset_index(drop=True)


def combine_files(paths) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """[{"station", "date", "latitude", "longitude", "time", "df", "sources", "header_block"}]
    in the first file's order, and info naming the casts only one file has."""
    tables = [(str(p), keyed_casts(p)) for p in paths]
    order = list(dict.fromkeys(k for _, t in tables for k in t))
    out, single = [], []
    for key in order:
        parts = [(p, t[key]) for p, t in tables if key in t]
        if len(parts) == 1:
            single.append(f"{key[0]} {key[1]}")
        first = parts[0][1]
        lat, lon = position(first)
        out.append({"station": header_value(first["header_block"], "Station"),
                    "date": key[1], "latitude": lat, "longitude": lon,
                    "time": header_value(first["header_block"], "Time"),
                    "df": merge_levels([c["df"] for _, c in parts]),
                    "sources": [p for p, _ in parts],
                    "columns_from": {p: [str(c) for c in cast["df"].columns]
                                     for p, cast in parts}})
    return out, {"casts": len(out), "in_one_file_only": single}
