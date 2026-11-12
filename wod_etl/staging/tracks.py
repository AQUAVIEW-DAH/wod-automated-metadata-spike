"""Surface tracks (SUR): their clock, and streams on one clock combined into one cast.

WOD stores a cruise's surface data as one cast whose observations each carry a position and
time (manual pp.16, 37), so underway instruments logging the same instants (e.g. a
thermosalinograph and a weather station) are one cast, not one per file.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# A track's time column: 'time (UTC)', 'Date/Time'.
_CLOCK = re.compile(r"(?:date[\s_/]*)?time(?:\s*\(.*\))?|utc|date[\s_/]*time.*|datetime",
                    re.IGNORECASE)
# Two streams sharing this share of their stamps, both ways, are on one clock.
SAME_CLOCK = 0.9
# A longer stream joins a combined cast when this share of its stamps falls on that cast.
JOINS = 0.1


def clock_column(columns) -> Optional[str]:
    return next((str(c) for c in columns if _CLOCK.fullmatch(str(c).strip())), None)


def clock_times(values) -> Tuple[pd.Series, bool]:
    """(naive timestamps, whether they were stated with a zone). Stamps with an offset or 'Z'
    are converted to UTC; plain ones are left as written."""
    try:
        when = pd.to_datetime(values, errors="coerce")
    except ValueError:                                  # mixed offsets
        when = pd.to_datetime(values, errors="coerce", utc=True)
    if getattr(when.dt, "tz", None) is None:
        return when, False
    return when.dt.tz_convert("UTC").dt.tz_localize(None), True


def read_track(path) -> Optional[Dict[str, Any]]:
    """`{"df", "clock", "times", "utc"}` for a file holding one surface track, else None."""
    from wod_etl.discovery import read_any, sniff_format

    casts, _ = read_any(str(path), sniff_format(str(path)))
    if len(casts) != 1:
        return None
    df = casts[0]["df"]
    clock = clock_column(df.columns)
    if clock is None:
        return None
    times, zoned = clock_times(df[clock])
    times = times.dt.floor("s")
    if times.notna().sum() < 2:
        return None
    return {"df": df.reset_index(drop=True), "clock": clock,
            "times": times.reset_index(drop=True), "utc": zoned or "utc" in clock.lower()}


def _stamps(track) -> set:
    return set(track["times"].dropna())


def same_clock_groups(tracks: Dict[str, Dict[str, Any]]) -> List[List[str]]:
    """Files grouped by clock: two belong together when each has nearly all of the other's
    stamps. A file alone is its own group."""
    names = sorted(tracks)
    stamps = {n: _stamps(tracks[n]) for n in names}
    group = {n: n for n in names}
    root = lambda n: n if group[n] == n else root(group[n])
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = len(stamps[a] & stamps[b])
            if tracks[a]["utc"] == tracks[b]["utc"] and stamps[a] and stamps[b] \
                    and shared >= SAME_CLOCK * max(len(stamps[a]), len(stamps[b])):
                group[root(b)] = root(a)
    out: Dict[str, List[str]] = {}
    for n in names:
        out.setdefault(root(n), []).append(n)
    return list(out.values())


def joining(tracks: Dict[str, Dict[str, Any]], groups: List[List[str]]) -> List[str]:
    """Single files whose stamps fall enough on the combined groups to join them."""
    combined = set().union(*(_stamps(tracks[p]) for g in groups if len(g) > 1 for p in g))
    out = []
    for g in groups:
        if len(g) == 1:
            s = _stamps(tracks[g[0]])
            if s and len(s & combined) >= JOINS * len(s):
                out.append(g[0])
    return out


def _split(track: Dict[str, Any], name: str, taken: set):
    """(measurements, positions), each indexed by time. A measurement name another stream
    already uses is labelled with its file."""
    from wod_etl.mapping.query_wod_spec import match_variable

    df = track["df"].drop(columns=[track["clock"]])
    df.index = pd.DatetimeIndex(track["times"])
    df = df[~df.index.isna() & ~df.index.duplicated()]
    where = [c for c in df.columns if match_variable(str(c)) in (30, 31)]
    measured = df.drop(columns=where)
    measured = measured.rename(columns={c: f"{c} [{name}]" for c in measured.columns
                                        if c in taken})
    taken.update(measured.columns)
    return measured, df[where]


def combine_tracks(paths) -> List[Dict[str, Any]]:
    """One table per cast: files on one clock merged on time, each other file joined where
    its stamps fall and the rest of it a cast of its own. Each is `{"df", "sources", "utc"}`,
    `df` starting time, Latitude, Longitude."""
    tracks = {str(p): t for p in paths if (t := read_track(p)) is not None}
    groups = same_clock_groups(tracks)
    anchors = [g for g in groups if len(g) > 1]
    extra = joining(tracks, groups)
    consumed: Dict[str, set] = {s: set() for s in extra}
    out = []
    for group in anchors:
        taken: set = set()
        parts = [_split(tracks[p], _stem(p), taken) for p in group]
        measured = pd.concat([m for m, _ in parts], axis=1, join="outer").sort_index()
        sources = list(group)
        for s in extra:
            m, _ = _split(tracks[s], _stem(s), taken)
            hit = m.index.isin(measured.index)
            if hit.any():
                measured = measured.join(m[hit])
                consumed[s] |= set(m.index[hit])
                sources.append(s)
        out.append(_table(measured, [p for _, p in parts], sources,
                          all(tracks[p]["utc"] for p in group)))
    for g in groups:
        if len(g) > 1:
            continue
        s = g[0]
        m, where = _split(tracks[s], _stem(s), set())
        rest = ~m.index.isin(list(consumed.get(s, ())))
        if rest.any():
            out.append(_table(m[rest], [where[rest]], [s], tracks[s]["utc"]))
    return out


def _table(measured: pd.DataFrame, positions: List[pd.DataFrame], sources: List[str],
           utc: bool) -> Dict[str, Any]:
    """time, Latitude and Longitude (from the stream covering most stamps, filled from the
    others), then the measurements."""
    from wod_etl.mapping.query_wod_spec import match_variable

    df = measured.copy()
    for code, name in ((31, "Longitude"), (30, "Latitude")):
        found = [pd.to_numeric(p[c], errors="coerce").reindex(df.index)
                 for p in positions for c in p.columns if match_variable(str(c)) == code]
        found = sorted(found, key=lambda s: -s.notna().mean())
        if found:
            value = found[0]
            for other in found[1:]:
                value = value.combine_first(other)
            df.insert(0, name, value)
    df.insert(0, "time (UTC)" if utc else "time", df.index)
    return {"df": df.reset_index(drop=True), "sources": sources, "utc": utc}


def _stem(path: str) -> str:
    return str(path).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
