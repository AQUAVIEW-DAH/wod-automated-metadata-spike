"""Read any file an accession can hold, whatever its extension, dispatching on `sniff_format`.

Returns (casts, info); each cast is {"cast_id", "df", "header_block", "source_note"}, with
`header_block` as `Key: value` text. Never raises: a failure returns ([], {"error": ...}).
"""
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wod_etl.config import NA_SENTINELS
from wod_etl.discovery.known_formats import read_probe_dat, read_tsk, read_wod_csv
from wod_etl.discovery.load_tabular import load_tabular
from wod_etl.discovery.profile_raw_data import profile_raw_data
from wod_etl.discovery.sniff_format import sniff_format
from wod_etl.discovery.split_casts import cast_as_profile_input, split_casts

# Dimension-name substrings (case-insensitive) for levels within a cast and casts within a file.
LEVEL_DIM_HINTS = ("level", "n_levels", "nlevels", "depth", "pres", "z", "obs",
                   "sample", "row", "nzmax", "maxz")
PROFILE_DIM_HINTS = ("prof", "n_prof", "cast", "station", "profile", "instance", "traj")

# NetCDF variables that are metadata about the cast rather than a measured level.
_HEADER_VAR_HINTS = ("lat", "lon", "juld", "time", "date", "platform", "cruise", "station",
                     "cast", "wmo", "project", "pi_name", "institution", "direction")

MAX_NETCDF_CELLS = 100_000_000

# A CF time variable is a count of units from an epoch: `seconds since 1970-01-01 00:00:00`.
_CF_TIME_UNITS = re.compile(r"^\s*[a-z]+\s+since\s+\S", re.IGNORECASE)

# The calendars CF defines. Anything else is read as standard, as CF does for a missing one.
_CF_CALENDARS = ("standard", "gregorian", "proleptic_gregorian", "julian", "noleap",
                 "365_day", "all_leap", "366_day", "360_day")

# A decoded time before this is a units error (e.g. an epoch typed 1070 for 1970).
EARLIEST_PLAUSIBLE_YEAR = 1700

# How much of a text file is read to profile its structure; load_tabular reads the rest.
PROFILE_BYTES = 65536


def read_any(path: str, sniff: Optional[Dict[str, Any]] = None,
             profile: Optional[Dict[str, Any]] = None,
             max_casts: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read a file of any supported format into casts; `sniff` is an earlier sniff_format result."""
    fmt = sniff or sniff_format(path)
    info: Dict[str, Any] = {"source": str(path), "sniff": fmt, "reader": fmt["reader"],
                            "warnings": []}

    if not fmt.get("readable", False):
        return [], {**info, "error": f"{fmt['format_family']}: "
                                     f"{fmt.get('note') or 'no tabular data to read'}"}

    readers = {
        "delimited": _read_delimited,
        "fixed_width": _read_fixed_width,
        "sea_bird_cnv": _read_sea_bird_cnv,
        "netcdf": _read_netcdf,
        "spreadsheet": _read_spreadsheet,
        "wod_csv": lambda p, f, _: read_wod_csv(p, f.get("encoding") or "utf-8"),
        "probe_dat": lambda p, f, _: read_probe_dat(p, f.get("encoding") or "utf-8"),
        "tsk_xctd": lambda p, f, _: read_tsk(p, f.get("encoding") or "utf-8"),
    }
    reader = readers.get(fmt["reader"])
    if reader is None:
        return [], {**info, "error": f"no reader for format '{fmt['format_family']}'"}

    try:
        casts, extra = reader(path, fmt, profile)
    except Exception as e:                                # noqa: BLE001 - unattended run
        return [], {**info, "error": f"{type(e).__name__} while reading as "
                                     f"{fmt['format_family']}: {e}"}

    info.update(extra)
    info.setdefault("warnings", [])
    if fmt["reader"] in ("delimited", "spreadsheet"):
        casts = [part for c in casts for part in split_by_station(c, info["warnings"])]
    _apply_missing_codes(casts, info["warnings"])
    if max_casts:
        casts = casts[:max_casts]
    info["cast_count"] = len(casts)
    if not casts and "error" not in info:
        info["error"] = f"read as {fmt['format_family']} but produced no casts"
    return casts, info


# ------------------------------------------------------ cruise-wide tables
# Columns and words shared with triage, so it proposes only what confirm can write.
SAMPLE_COLUMN = re.compile(r"^\s*sample", re.I)
SURFACE_WORDS = re.compile(r"underway|surface", re.I)
_HEADER_KEYS = {"latitude", "longitude", "station", "date", "time"}


def split_by_station(cast: Dict[str, Any], warnings: List[str]) -> List[Dict[str, Any]]:
    """Split a table of every station's levels into one cast per station (and date).

    Position, date and station move into each cast's header. Without a depth axis, only a
    sample column splits it.
    """
    from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
    from wod_etl.staging.parse import parse_coordinate
    from wod_etl.staging.station_list import _ROLE_PATTERNS
    from wod_etl.terms import english_terms

    df = cast["df"]
    cols = [str(c) for c in df.columns]
    if len(set(cols)) != len(cols):
        return [cast]                          # unnamed or repeated columns: not a table to split
    codes = {c: match_variable(c) for c in cols}
    role = lambda r: next((c for c in cols
                           if re.search(_ROLE_PATTERNS[r], english_terms(c).strip().lower())), None)
    lat = next((c for c in cols if codes[c] == 30 and pd.api.types.is_numeric_dtype(df[c])),
               next((c for c in cols if codes[c] == 30), None))
    lon = next((c for c in cols if codes[c] == 31 and pd.api.types.is_numeric_dtype(df[c])),
               next((c for c in cols if codes[c] == 31), None))
    station, date, time = role("station"), role("date"), role("time")
    sample = next((c for c in cols if SAMPLE_COLUMN.match(c)), None)
    has_depth = bool({DEPTH_SENTINEL_CODE, 25} & set(codes.values()))       # depth or pressure
    # Only a station (or, without depth, a sample) names a cast. A position or clock logged
    # on every scan is one cast's track.
    key = station if has_depth else sample
    if not (lat and lon and key) or len(df) < 2:
        return [cast]
    df = df.copy()
    # A fill-down table leaves a cast's key cells blank below its first row.
    blank = lambda s: s.isna() | s.astype(str).str.strip().eq("")
    carried = [c for c in (key, date, time, lat, lon) if c]
    below = blank(df[key])
    if below.any():
        df.loc[below, carried] = df[carried].mask(df[carried].apply(blank)).ffill()[below]
    # A date splits casts; a date-time stamped on every row does not.
    day = date and has_depth and not df[date].astype(str).str.contains(r"\d:\d").any()
    keys = [key] + ([date] if day else [])
    groups = df.groupby([df[k].astype(str) for k in keys], sort=False, dropna=False)
    if groups.ngroups < 2:
        return [cast]
    used = {c for c in (station, date, time, key) if c} | {c for c in cols if codes[c] in (30, 31)}
    preamble = [line for line in (cast.get("header_block") or "").splitlines()
                if line.split(":", 1)[0].strip().lower() not in _HEADER_KEYS]
    out = []
    for n, (_, rows) in enumerate(groups):
        first = rows.iloc[0]
        # The station's own values first: the header parser takes the first of each key.
        header = [f"Latitude: {parse_coordinate(first[lat])}",
                  f"Longitude: {parse_coordinate(first[lon])}"]
        if station and pd.notna(first[station]):
            header.append(f"Station: {first[station]}")
        if date and pd.notna(first[date]):
            header.append(f"Date: {first[date]}")
        if time and pd.notna(first[time]):
            header.append(f"Time: {first[time]}")
        label = "_".join(str(first[k]) for k in keys)
        out.append({**cast, "cast_id": label, "df": rows.drop(columns=list(used)).reset_index(drop=True),
                    "header_block": "\n".join(header + preamble),
                    "source_note": f"{cast.get('source_note', '')}, station {label}"})
    warnings.append(f"one table of {len(out)} stations, split into a cast per station"
                    + (" and date" if day else "") + "; each cast's position is its first row's")
    return out


# ------------------------------------------------------------- delimited text
def _read_delimited(path, fmt, profile):
    """Profile the preview, split casts, and load each one."""
    with Path(path).open("r", encoding=fmt.get("encoding", "utf-8"), errors="replace") as fh:
        head = fh.read(PROFILE_BYTES)
    prof = profile or profile_raw_data(head, str(path))
    if prof.get("error"):
        return [], {"error": f"profile_raw_data: {prof['error']}"}

    split = split_casts(path, prof)
    if split.get("error"):
        return [], {"error": f"split_casts: {split['error']}"}
    # Whitespace-separated station blocks get their own reader: profiling each block alone
    # would read its column line as one column.
    if split["multi_cast"] and fmt.get("delimiter") in (r"\s+", " ", None):
        return _read_station_blocks(split)

    out, warnings = [], []
    for cast in split["casts"]:
        if split["multi_cast"]:
            cast_prof = profile_raw_data(cast_as_profile_input(cast), str(path))
            if cast_prof.get("error"):
                warnings.append(f"cast {cast['index']}: {cast_prof['error']}")
                continue
            cast_prof["raw_header_block"] = cast["header_block"] or cast_prof.get("raw_header_block", "")
            df, load_info = _frame_from_text(cast["data_text"], cast_prof)
        else:
            df, load_info = load_tabular(path, prof)
            cast_prof = prof
        if df is None:
            warnings.append(f"cast {cast['index']}: {load_info.get('error')}")
            continue
        # Carry the loader's warnings so dropped rows are reported.
        prefix = f"cast {cast['index']}: " if split["multi_cast"] else ""
        warnings.extend(prefix + w for w in load_info.get("warnings", []))
        out.append({
            "cast_id": str(cast["index"]),
            "df": df,
            "header_block": cast_prof.get("raw_header_block", ""),
            "source_note": f"{fmt['format_family']}, "
                           f"delimiter {fmt.get('delimiter_name', 'inferred')}",
        })
    return out, {"multi_cast": split["multi_cast"], "warnings": warnings}


def _frame_from_text(data_text: str, prof):
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "cast.txt"
    tmp.write_text(data_text)
    return load_tabular(str(tmp), prof, header_lines=0)


# ------------------------------------------------------------- fixed width
def _read_fixed_width(path, fmt, profile):
    """Fixed-column ASCII, split at sniff_format's boundaries (shown in `source_note`)."""
    colspecs = fmt.get("colspecs")
    text = Path(path).read_text(encoding=fmt.get("encoding", "utf-8"), errors="replace")
    split = split_casts(text)
    if split.get("multi_cast"):
        return _read_station_blocks(split)
    lines = text.splitlines()
    header_lines = [l for l in lines[:40] if l.strip().startswith(("#", "*", "%", "//", ";"))]
    skip = _leading_comment_count(lines)

    # Detect a headerless file, or read_fwf would consume its first level as column names.
    first = next((l for l in lines[skip:] if l.strip()), "")
    headerless = _all_numeric(first)
    df = pd.read_fwf(path, colspecs=colspecs or "infer", skiprows=skip,
                     header=None if headerless else 0,
                     encoding=fmt.get("encoding", "utf-8"),
                     na_values=list(NA_SENTINELS))
    warnings = ["fixed-width column boundaries are inferred, not declared; check them "
                "against the accession's format description before trusting the split"]
    if headerless:
        df.columns = [f"col_{i}" for i in range(df.shape[1])]
        warnings.append(f"no column header row: {df.shape[1]} columns named col_0..."
                        f"col_{df.shape[1] - 1}. No col_N maps to a WOD code: name them "
                        f"during staging, from the header block or the accession's format "
                        f"description, with units folded into the name.")
    else:
        df.columns = [str(c).strip() for c in df.columns]
    note = (f"fixed width, {len(colspecs)} inferred columns at {colspecs}"
            if colspecs else "fixed width, boundaries inferred by pandas")
    return [{"cast_id": "0", "df": df, "header_block": "\n".join(header_lines),
             "source_note": note}], {"warnings": warnings}


def _read_station_blocks(split: Dict[str, Any]):
    """Casts from a file of station blocks: a header, a line of column names, then rows.

    Lines before the first block are the file's own header and go to every cast. Rows that
    do not match the column names, or the stated number of levels, are reported.
    """
    casts = split["casts"]
    preamble = _preamble(casts)
    out, warnings = [], []
    for c in casts:
        names_line = (c.get("column_header_line") or "").strip()
        names = names_line.split()
        rows = [r.split() for r in c["data_text"].splitlines()
                if r.strip() and r.strip() != names_line]
        good = [r for r in rows if len(r) == len(names)]
        if not names or not good:
            warnings.append(f"cast {c['index']}: no column names above its rows; not read")
            continue
        if len(good) < len(rows):
            warnings.append(f"cast {c['index']}: {len(rows) - len(good)} row(s) do not have "
                            f"the {len(names)} fields its column names say; dropped")
        header = c["header_block"]
        if c["index"] and preamble:
            header = preamble + "\n" + header
        stated = re.search(r"number\s+of\s+(?:levels|records|rows|scans)\s*[:=]\s*(\d+)",
                           c["header_block"], re.IGNORECASE)
        if stated and int(stated.group(1)) != len(good):
            warnings.append(f"cast {c['index']}: its header states {stated.group(1)} levels, "
                            f"{len(good)} were read")
        df = pd.DataFrame(good, columns=names).apply(pd.to_numeric, errors="coerce")
        out.append({"cast_id": str(c["index"]), "df": df, "header_block": header,
                    "source_note": "station blocks: a header, a line of column names and "
                                   "whitespace-separated rows per cast"})
    return out, {"multi_cast": True, "warnings": warnings}


def _preamble(casts) -> str:
    """The file's own header: the first block's lines before the key each block starts with."""
    if len(casts) < 2 or not casts[1]["header_block"]:
        return ""
    key = _line_key(casts[1]["header_block"].splitlines()[0])
    lines = casts[0]["header_block"].splitlines()
    at = next((i for i, l in enumerate(lines) if key and _line_key(l) == key), 0)
    return "\n".join(lines[:at])


def _line_key(line: str) -> Optional[str]:
    m = re.match(r"\s*([A-Za-z][\w\s-]*?)\s*[:=]", line)
    return m.group(1).lower() if m else None


# A missing-value code the file declares (`Missing value: -9`): unsafe globally, valid here.
_DECLARED_MISSING = re.compile(r"\b(?:missing|fill|no[\s_-]?data)[\s_-]*(?:values?|codes?)?\s*"
                               r"[:=]\s*(-?\d+(?:\.\d+)?)(?![\d.])", re.IGNORECASE)


# Below this a value is a float fill (e.g. -9.99e-29), not a measurement; no Table 3a
# variable is that small in its units.
FLOAT_FILL_MAGNITUDE = 1e-20


def _apply_missing_codes(casts: List[Dict[str, Any]], warnings: List[str]) -> None:
    """Declared missing-value codes, and float fills, read as missing."""
    for c in casts:
        codes = {float(m.group(1)) for m in _DECLARED_MISSING.finditer(c.get("header_block")
                                                                        or "")}
        df = c["df"]
        numeric = [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]
        if not numeric:
            continue
        values = df[numeric]
        declared = values.isin(codes) if codes else values.isna() & False
        tiny = values.abs().lt(FLOAT_FILL_MAGNITUDE) & values.ne(0)
        for hits, what in ((declared, f"equal to the declared missing value "
                                      f"{', '.join(f'{v:g}' for v in sorted(codes))}"),
                           (tiny, f"of magnitude below {FLOAT_FILL_MAGNITUDE:g} (a float fill)")):
            n = int(hits.to_numpy().sum())
            if n:
                df = df.mask(hits.reindex(columns=df.columns, fill_value=False))
                warnings.append(f"cast {c['cast_id']}: {n} value(s) {what} read as missing")
        c["df"] = df


def _all_numeric(line: str) -> bool:
    fields = line.split()
    if not fields:
        return False
    for f in fields:
        try:
            float(f)
        except ValueError:
            return False
    return True


def _leading_comment_count(lines) -> int:
    n = 0
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", "*", "%", "//", ";", "!")):
            n += 1
            continue
        break
    return n


# ------------------------------------------------------------- Sea-Bird .cnv
def _read_sea_bird_cnv(path, fmt, profile):
    """Sea-Bird converted data: names in `# name N = short: long [unit]`, data after *END*.

    Long name and unit are folded into the column label, where unit detection reads them.
    """
    text = Path(path).read_text(encoding=fmt.get("encoding", "utf-8"), errors="replace")
    lines = text.splitlines()

    names, header, end = [], [], None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.upper().startswith("*END*"):
            end = i
            break
        if s.startswith(("#", "*")):
            header.append(s)
            if s.startswith("# name "):
                _, _, rest = s.partition("=")
                names.append(rest.strip() or f"col_{len(names)}")

    if end is None:
        return [], {"error": "no *END* marker: this is not a complete Sea-Bird .cnv"}

    body = "\n".join(lines[end + 1:])
    if not body.strip():
        return [], {"error": "Sea-Bird header ends with no data rows after *END*"}

    # An ASCII export has a row of short names ('Pr T068 Sal00') above the data, not '# name' lines.
    rows = [l for l in lines[end + 1:] if l.strip()]
    if not names and rows and not any(_all_numeric(t) for t in rows[0].split()):
        seen: Dict[str, int] = {}
        for n in rows[0].split():
            seen[n] = seen.get(n, 0) + 1
            names.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
        body = "\n".join(rows[1:])
    df = pd.read_csv(io.StringIO(body), sep=r"\s+", header=None, engine="python",
                     na_values=list(NA_SENTINELS))
    if names and len(names) == df.shape[1]:
        df.columns = names
    else:
        df.columns = [f"col_{i}" for i in range(df.shape[1])]

    warnings = []
    if names and len(names) != df.shape[1]:
        warnings.append(f"{len(names)} '# name' lines but {df.shape[1]} data columns; "
                        f"columns left unnamed rather than misaligned")
    return [{"cast_id": "0", "df": df, "header_block": _cnv_header_block(header),
             "source_note": "Sea-Bird .cnv"}], {"warnings": warnings}


def _cnv_header_block(header_lines) -> str:
    """Rewrite the Sea-Bird header's `Key = value` lines as the `Key: value` the mapper parses."""
    out = []
    for line in header_lines:
        s = line.lstrip("#* \t")
        if "=" in s and ":" not in s.split("=")[0]:
            key, _, value = s.partition("=")
            s = f"{key.strip()}: {value.strip()}"
        out.append(s)
    return "\n".join(out + _cnv_time(out))


# Where a .cnv states when the cast began, best first; True where the clock is UTC. An
# upload time is the logging PC's clock, in whatever zone it was set to.
_CNV_CLOCKS = [("nmea utc (time)", True), ("system utc", True), ("start_time", False),
               ("nmea upload time", False), ("system upload time", False)]
_CNV_STAMP = re.compile(r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")


def _cnv_time(lines) -> List[str]:
    """`Date:` and `Time:` lines from Sea-Bird's 'Feb 01 2002 12:21:49' stamps."""
    import datetime as dt

    for key, utc in _CNV_CLOCKS:
        line = next((l for l in lines if l.lower().startswith(key + ":")), None)
        m = _CNV_STAMP.search(line or "")
        if not m:
            continue
        try:
            when = dt.datetime.strptime(" ".join(m.groups()[:3]) + " " + ":".join(m.groups()[3:]),
                                        "%b %d %Y %H:%M:%S")
        except ValueError:
            continue
        name = line.split(":", 1)[0]
        return [f"Date: {when:%Y-%m-%d} (from {name})", f"Time: {when:%H:%M:%S} (from {name})"] \
            + (["TIME_ZONE: UTC"] if utc else [])
    return []


# ------------------------------------------------------------- spreadsheets
def _read_spreadsheet(path, fmt, profile):
    """Read each sheet of a workbook as one cast; RBR Ruskin exports go to `_read_ruskin`."""
    try:
        names = pd.ExcelFile(path).sheet_names
    except Exception:                                   # noqa: BLE001 - read_excel says why
        names = []
    if "Profile annotation" in names and "Metadata" in names:
        return _read_ruskin(path, names)
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, na_values=list(NA_SENTINELS))
    except ImportError as e:
        return [], {"error": f"reading spreadsheets needs an Excel engine: {e}. "
                             f"Install openpyxl, or export the sheet to CSV and stage that."}
    out = []
    for name, raw in sheets.items():
        if raw.empty:
            continue
        df = _sheet_table(raw)
        if df.empty:
            continue
        out.append({"cast_id": name, "df": df, "header_block": "",
                    "source_note": f"{fmt['format_family']}, sheet '{name}'"})
    return out, {"warnings": ["a workbook carries no header block; coordinates that live in "
                              "a title row or a separate sheet must be staged in by hand"],
                 "sheets": list(sheets)}


def _sheet_table(raw: "pd.DataFrame") -> "pd.DataFrame":
    """A headerless sheet as a table, its header row found as for a CSV, else its first row."""
    from wod_etl.discovery.profile_raw_data import table_layout

    cells = []
    for row in raw.itertuples(index=False):
        r = [("" if pd.isna(v) else str(v).strip()) for v in row]
        while r and not r[-1]:
            r.pop()
        cells.append(r)
    layout = table_layout(list(enumerate(cells)))
    at = layout["header_line"] if layout else 0
    names = layout["columns"] if layout else cells[0]
    df = raw.iloc[at + 1:].reset_index(drop=True)
    if layout and layout["unit_row"] is not None:
        df = df.iloc[1:].reset_index(drop=True)
    width = min(len(names), df.shape[1])
    df = df.iloc[:, :width]
    df.columns = [str(c).strip() for c in names[:width]]
    for c in df.columns:
        try:
            df[c] = pd.to_numeric(df[c])
        except (ValueError, TypeError):
            pass
    return df.dropna(how="all").dropna(axis=1, how="all")


def _ruskin_table(path, sheet):
    """A Ruskin sheet as a table: its header is the row that starts with 'Time'."""
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    at = next((i for i, v in enumerate(raw.iloc[:, 0]) if str(v).strip() == "Time"), None)
    if at is None:
        return None
    df = raw.iloc[at + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() for c in raw.iloc[at]]
    return df.dropna(axis=1, how="all")


def _read_ruskin(path, names):
    """An RBR Ruskin workbook as one cast per downcast.

    Downcast windows come from the 'Profile annotation' sheet; a cast's position is its first
    sample's and its time the downcast's start.
    """
    from wod_etl.staging.parse import parse_coordinate

    notes = pd.read_excel(path, sheet_name="Profile annotation", header=None)
    windows = [(pd.Timestamp(r[0]), pd.Timestamp(r[1])) for r in notes.itertuples(index=False)
               if str(r[3]).strip().upper() == "DOWN"]
    meta = pd.read_excel(path, sheet_name="Metadata", header=None)
    instrument = None
    for i, row in meta.iterrows():
        if str(row[0]).strip() == "Serial" and i + 1 < len(meta):
            instrument = f"{meta.iloc[i + 1, 1]} {meta.iloc[i + 1, 0]}"
            break
    casts = []
    for sheet in names:
        if sheet in ("Metadata", "Profile annotation", "Events"):
            continue
        df = _ruskin_table(path, sheet)
        if df is None or "Time" not in df.columns:
            continue
        times = pd.to_datetime(df["Time"], errors="coerce")
        for start, end in windows:
            rows = df[(times >= start) & (times <= end)]
            if "Depth" in rows.columns:
                rows = rows[pd.to_numeric(rows["Depth"], errors="coerce") >= 0]
            if rows.empty:
                continue
            first = rows.iloc[0]
            lat = parse_coordinate(first.get("Latitude"))
            lon = parse_coordinate(first.get("Longitude"))
            data = rows.drop(columns=[c for c in ("Time", "Latitude", "Longitude", "Pressure")
                                      if c in rows.columns])
            data = data.rename(columns={"Sea pressure": "Sea pressure (dbar)",
                                        "Depth": "Depth (m)"}).apply(pd.to_numeric,
                                                                     errors="coerce")
            header = [f"Latitude: {lat}", f"Longitude: {lon}",
                      f"Date: {start:%Y-%m-%d}", f"Time: {start:%H:%M:%S}",
                      f"Station: {sheet.split(' - ')[0]}"]
            if instrument:
                header.append(f"Instrument: {instrument}")
            casts.append({"cast_id": f"{sheet} {start:%H%M%S}", "df": data.reset_index(drop=True),
                          "header_block": "\n".join(header),
                          "source_note": f"Ruskin workbook, sheet '{sheet}', downcast "
                                         f"{start:%H:%M:%S}-{end:%H:%M:%S}"})
    return casts, {"warnings": [
        "Ruskin workbook: one cast per downcast in 'Profile annotation'; upcasts are not "
        "written. Pressure is sea pressure (absolute pressure minus the atmosphere). The "
        "workbook states no time zone."]}


# ------------------------------------------------------------------ NetCDF
def _read_netcdf(path, fmt, profile):
    """Read NetCDF/HDF5 into one cast per profile.

    Handles single profiles, profile x level variables, flattened tables and CF ragged arrays.
    """
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        ds.set_auto_mask(True)
        global_attrs = {a: _scalar(ds.getncattr(a)) for a in ds.ncattrs()}
        variables = {name: honour_fill(v) for name, v in ds.variables.items()}
        # Keep each clock's units and calendar to decode cast times after the file closes.
        clocks = {name: (str(v.units), getattr(v, "calendar", None))
                  for name, v in variables.items() if _is_clock(v)}

        total = sum(int(v.size) for v in variables.values())
        if total > MAX_NETCDF_CELLS:
            return [], {"error": f"{total:,} cells exceeds the {MAX_NETCDF_CELLS:,} guard; "
                                 f"subset the file before reading it"}

        ragged = _ragged_spec(variables)
        if ragged:
            casts, warnings = _netcdf_ragged(ds, variables, ragged)
        else:
            level_dim = _pick_dim(ds, variables, LEVEL_DIM_HINTS, prefer="largest")
            profile_dim = _pick_dim(ds, variables, PROFILE_DIM_HINTS, exclude=level_dim)
            if level_dim is None:
                return [], {"error": "no dimension looks like a set of levels; the file may "
                                     "be gridded or a trajectory, neither of which is a "
                                     "profile"}
            flattened = None if profile_dim else _flattened_keys(ds, global_attrs, level_dim)
            casts, warnings = (_netcdf_flattened(ds, variables, level_dim, flattened)
                               if flattened else
                               _netcdf_rectangular(variables, level_dim, profile_dim))
        upward = _upward_labels(variables)

    _depth_from_height(casts, upward, warnings)
    undated: Dict[str, List[Any]] = {}
    for c in casts:
        scalars = c.pop("_scalars", {})
        times = {}
        for name in scalars.keys() & clocks.keys():
            when = _decode_time(scalars[name], *clocks[name])
            if when is not None and not _plausible(when):
                undated.setdefault(name, []).append(when)
                when = None
            times[name] = when
        c["header_block"] = _header_block(global_attrs, scalars, times)
        c["source_note"] = f"NetCDF ({fmt['format_family']})"
    # Warn once per variable: a wrong epoch is wrong for every cast.
    for name, whens in undated.items():
        warnings.append(f"'{name}' has units '{clocks[name][0]}' and decodes to "
                        f"{_iso(whens[0])}: the epoch looks wrong. {len(whens)} cast(s) are "
                        f"left with no date rather than that one; correct the units in a "
                        f"staged copy to date them.")
    return casts, {"warnings": warnings, "global_attributes": global_attrs}


def _upward_labels(variables) -> set:
    """Labels of vertical coordinates measured upward (CF `positive = "up"`)."""
    return {_label(n, v, variables) for n, v in variables.items()
            if str(getattr(v, "positive", "")).strip().lower() == "up"
            and (str(getattr(v, "axis", "")).upper() == "Z"
                 or re.search(r"^(?:z|depth|height|altitude|elevation)$",
                              str(getattr(v, "standard_name", "") or n), re.I))}


def _depth_from_height(casts, up: set, warnings) -> None:
    """Flip an upward vertical coordinate (values at or below 0) into depth."""
    for c in casts:
        for label in up & set(c["df"].columns):
            values = pd.to_numeric(c["df"][label], errors="coerce")
            if values.notna().any() and (values.dropna() <= 0).mean() >= 0.9:
                c["df"][label] = -values
                warnings.append(f"'{label}' is positive up: written as depth (its sign "
                                f"flipped)")


# Fill-value attribute names netCDF4 does not apply; unmasked, a sentinel such as 9999.99
# reads as data.
_NONSTANDARD_FILL = ("FillValue", "fillValue", "fill_value", "FILL_VALUE", "missingValue")


class _Masked:
    """A NetCDF variable whose non-standard fill value reads as masked."""

    def __init__(self, v, fills):
        self._v, self._fills = v, fills

    def __getattr__(self, name):
        return getattr(self._v, name)

    def __getitem__(self, key):
        import numpy as np

        data = np.ma.asarray(self._v[key])
        values = np.ma.filled(data.astype("float64"), np.nan)
        bad = np.zeros(values.shape, dtype=bool)
        for fill in self._fills:
            bad |= np.isclose(values, fill)
        return np.ma.masked_where(bad, data)


def _kind(v) -> str:
    """A variable's dtype kind; a NetCDF-4 variable-length string's dtype is `str`."""
    return getattr(v.dtype, "kind", "O")


def honour_fill(v):
    """`v`, masking any fill value it declares under a non-standard attribute name."""
    attrs = set(v.ncattrs())
    if _kind(v) not in "fiu" or attrs & {"_FillValue", "missing_value"}:
        return v
    fills = []
    for a in attrs & set(_NONSTANDARD_FILL):
        try:
            fills.append(float(v.getncattr(a)))
        except (TypeError, ValueError):
            continue
    return _Masked(v, fills) if fills else v


def _ragged_spec(variables):
    """CF discrete-sampling contiguous ragged array: a count variable naming its samples."""
    for name, v in variables.items():
        if "sample_dimension" in v.ncattrs():
            return {"count_var": name, "sample_dim": v.getncattr("sample_dimension"),
                    "instance_dim": v.dimensions[0] if v.dimensions else None}
    return None


def _netcdf_ragged(ds, variables, spec):
    counts = [int(x) for x in variables[spec["count_var"]][:]]
    sample_dim, instance_dim = spec["sample_dim"], spec["instance_dim"]

    level_vars = {n: v for n, v in variables.items()
                  if v.dimensions == (sample_dim,) and n != spec["count_var"]}
    inst_vars = {n: v for n, v in variables.items()
                 if v.dimensions == (instance_dim,) and n != spec["count_var"]}

    casts, start = [], 0
    for i, n in enumerate(counts):
        if n <= 0:
            start += max(n, 0)
            continue
        df = pd.DataFrame({_label(name, v, variables): _column(v[start:start + n])
                           for name, v in level_vars.items()})
        casts.append({"cast_id": str(i), "df": df,
                      "_scalars": {name: _scalar(v[i]) for name, v in inst_vars.items()}})
        start += n
    return casts, [f"CF contiguous ragged array via '{spec['count_var']}': "
                   f"{len(casts)} profile(s)"]


# CF DSG attributes listing the variables constant along a profile, time series or trajectory.
_INSTANCE_LISTS = ("cdm_profile_variables", "cdm_timeseries_variables",
                   "cdm_trajectory_variables")
_UPCAST = re.compile(r"^(?:up|upward|upcast|ascending|asc|u)$", re.IGNORECASE)


def _flattened_keys(ds, global_attrs, level_dim) -> Optional[List[str]]:
    """Per-profile variables of a flattened table (a row per level, as ERDDAP writes profiles)."""
    listed = ",".join(str(global_attrs.get(a) or "") for a in _INSTANCE_LISTS)
    names = [n for n in dict.fromkeys(x.strip() for x in listed.split(","))
             if n in ds.variables and ds.variables[n].dimensions[:1] == (level_dim,)]
    return names or None


def _texts(v):
    """A text variable as one string per row, whether stored as chars or decoded already."""
    import netCDF4
    import numpy as np

    data = v[:]
    if np.ma.isMaskedArray(data):
        data = data.filled(b"" if data.dtype.kind == "S" else "")
    data = np.asarray(data)
    if data.ndim == 2:
        data = netCDF4.chartostring(data)
    return np.char.strip(data.astype(str))


def _netcdf_flattened(ds, variables, level_dim, keys):
    """A cast per run of rows over which the profile variables are constant. When downcasts
    are there, upcasts are not written: WOD stores the downcast."""
    import numpy as np

    instance = {}
    for name in keys:
        v = ds.variables[name]
        instance[name] = (_texts(v) if _kind(v) in "SUO" else
                          np.ma.filled(np.ma.asarray(v[:], dtype="float64"), np.nan))
    frame = pd.DataFrame(instance)
    text = frame.astype(str)
    starts = list(np.flatnonzero(text.ne(text.shift()).any(axis=1).to_numpy())) + [len(frame)]
    level_vars = {n: v for n, v in variables.items()
                  if v.dimensions == (level_dim,) and n not in keys and _kind(v) not in "SUO"}
    table = pd.DataFrame({_label(n, v, variables): _column(v[:]) for n, v in level_vars.items()})
    clocks = [n for n, v in level_vars.items() if _is_clock(v)]
    schemes = {_label(n, v, variables): {"flag_values": _scalar(v.getncattr("flag_values"))
                              if "flag_values" in v.ncattrs() else None,
                              "flag_meanings": str(v.getncattr("flag_meanings"))}
               for n, v in level_vars.items() if "flag_meanings" in v.ncattrs()}
    direction = next((k for k in keys if "direction" in k.lower()), None)
    downcasts = direction is not None and any(
        not _UPCAST.match(str(x)) for x in frame[direction].unique() if str(x))
    casts, upcasts = [], 0
    for i, (s, e) in enumerate(zip(starts, starts[1:])):
        if downcasts and _UPCAST.match(str(frame[direction].iat[s])):
            upcasts += 1
            continue
        df = table.iloc[s:e].reset_index(drop=True).dropna(how="all")
        if df.empty:
            continue
        scalars = {k: _scalar(frame[k].iat[s]) for k in keys if str(frame[k].iat[s]) not in
                   ("", "nan")}
        scalars.update({n: _first_valid(variables[n][s:e]) for n in clocks})
        casts.append({"cast_id": str(i), "df": df, "_scalars": scalars,
                      "column_attributes": schemes or None})
    warnings = [f"one table of {len(starts) - 1} profile(s), split by "
                f"{', '.join(keys[:4])}{'…' if len(keys) > 4 else ''}"]
    if upcasts:
        warnings.append(f"{upcasts} upcast(s) not written ('{direction}'): WOD stores the "
                        f"downcast")
    return casts, warnings


def _netcdf_rectangular(variables, level_dim, profile_dim):
    """One cast per profile index, or a single cast when there is no profile dimension."""
    warnings = []
    level_vars, per_profile, scalars = {}, {}, {}
    for name, v in variables.items():
        dims = v.dimensions
        if dims == (level_dim,):
            level_vars[name] = v
        elif profile_dim and dims == (profile_dim, level_dim):
            level_vars[name] = v
        elif profile_dim and dims == (level_dim, profile_dim):
            warnings.append(f"'{name}' is stored level-major; transposed to profile-major")
            level_vars[name] = v
        elif profile_dim and dims == (profile_dim,):
            per_profile[name] = v
        elif not dims:
            value = _container_text(name, v) or _scalar(v[:])
            if value is not None and not (isinstance(value, float) and value != value):
                scalars[name] = value
        elif _is_char_name(v, dims, profile_dim):
            per_profile[name] = v
        else:
            warnings.append(f"'{name}' has dimensions {dims}; not a level or profile "
                            f"variable, skipped")

    if not level_vars:
        return [], warnings + [f"no variable is shaped along the level dimension "
                               f"'{level_dim}'"]

    if not profile_dim:
        # A single cast with a time on every level is dated by its first level.
        scalars.update({name: _first_valid(v[:]) for name, v in level_vars.items()
                        if _is_clock(v)})

    n_profiles = 1
    if profile_dim:
        for v in variables.values():
            if profile_dim in v.dimensions:
                n_profiles = v.shape[v.dimensions.index(profile_dim)]
                break

    schemes = {_label(name, v, variables): {"flag_values": _scalar(v.getncattr("flag_values"))
                                 if "flag_values" in v.ncattrs() else None,
                                 "flag_meanings": str(v.getncattr("flag_meanings"))}
               for name, v in level_vars.items() if "flag_meanings" in v.ncattrs()}
    casts = []
    for i in range(n_profiles):
        columns = {}
        for name, v in level_vars.items():
            data = v[:] if v.dimensions == (level_dim,) else (
                v[i, :] if v.dimensions[0] == profile_dim else v[:, i])
            columns[_label(name, v, variables)] = _column(data)
        df = pd.DataFrame(columns).dropna(how="all")
        if df.empty:
            continue
        cast_scalars = dict(scalars)
        cast_scalars.update({name: _scalar(_index(v, i)) for name, v in per_profile.items()})
        casts.append({"cast_id": str(i), "df": df, "_scalars": cast_scalars,
                      "column_attributes": schemes or None})

    if not casts:
        warnings.append("every profile was empty after dropping all-missing levels")
    return casts, warnings


def _container_text(name: str, v) -> Optional[str]:
    """Text from a container variable's attributes, e.g. `instrument:make_model = "MVP"`."""
    if not re.match(r"^(?:instrument|sensor|platform)", name, re.IGNORECASE):
        return None
    parts = [str(_scalar(v.getncattr(a))) for a in ("long_name", "make_model", "serial_number")
             if a in v.ncattrs() and str(_scalar(v.getncattr(a))).strip()]
    # Join with " / ", not ";", which would end the header value.
    return " / ".join(dict.fromkeys(parts)) or None


def _is_char_name(v, dims, profile_dim):
    """A fixed-length character string, e.g. PLATFORM_NUMBER(N_PROF, STRING8)."""
    return (_kind(v) in "SU" and profile_dim in dims and len(dims) == 2
            and dims[0] == profile_dim)


def _index(v, i):
    data = v[:]
    if getattr(data, "ndim", 0) == 0:
        return data
    if data.ndim == 2 and _kind(v) in "SU":
        return b"".join(bytes(c) for c in data[i] if c is not None)
    return data[i] if i < len(data) else None


def _pick_dim(ds, variables, hints, exclude=None, prefer=None):
    """The dimension whose name matches a hint; fall back to the largest one in use.

    Only numeric variables' dimensions count, so a string length is never picked.
    """
    used = {d for v in variables.values() if _kind(v) not in "SU" for d in v.dimensions}
    named = [d for d in ds.dimensions if d in used and d != exclude
             and any(h in d.lower() for h in hints)]
    if named:
        return max(named, key=lambda d: len(ds.dimensions[d]))
    if prefer == "largest":
        candidates = [d for d in used if d != exclude and len(ds.dimensions[d]) > 1]
        return max(candidates, key=lambda d: len(ds.dimensions[d])) if candidates else None
    return None


def _label(name: str, v, variables=None) -> str:
    """Column label carrying its unit, since unit detection reads only the name.

    A name WOD does not recognise (e.g. TEMPS901) is replaced by a long_name it does. A flag
    named in a variable's `ancillary_variables` becomes '<that variable>_QC'.
    """
    flagged = next((d for d, dv in (variables or {}).items()
                    if name in str(getattr(dv, "ancillary_variables", "") or "").split()
                    and d != name), None)
    if flagged:
        return f"{_base_name(flagged, variables[flagged])}_QC"
    name = _base_name(name, v)
    units = getattr(v, "units", None) or getattr(v, "unit", None)
    if units and str(units).strip() and "since" not in str(units) \
            and f"({str(units).strip().lower()})" not in name.lower():
        return f"{name} ({str(units).strip()})"
    return name


def _base_name(name: str, v) -> str:
    from wod_etl.mapping.query_wod_spec import match_variable

    long_name = str(getattr(v, "long_name", "") or "").strip()
    if long_name and not re.search(r"flag|quality|\bqc\b", f"{name} {long_name}", re.I) \
            and "flag_meanings" not in getattr(v, "ncattrs", lambda: [])() \
            and match_variable(name) is None and match_variable(long_name) is not None:
        return long_name
    return name


def _column(data):
    import numpy as np

    arr = np.ma.filled(np.ma.asarray(data).astype("float64"), np.nan) \
        if np.ma.asarray(data).dtype.kind in "fiu" else np.asarray(data)
    return pd.Series(np.ravel(arr))


def _scalar(value):
    """Reduce a NetCDF value or attribute to something JSON- and text-friendly."""
    import numpy as np

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00 \t\r\n")
    if isinstance(value, np.ndarray) or hasattr(value, "mask"):
        flat = np.ma.filled(np.ma.asarray(value), np.nan).ravel()
        if flat.dtype.kind in "SU":
            # A fixed-width char array is padded with NULs, which are not text.
            return (b"".join(bytes(c) for c in flat).decode("utf-8", "replace")
                    if flat.dtype.kind == "S" else "".join(str(c) for c in flat)
                    ).strip("\x00 \t\r\n")
        return flat[0].item() if flat.size == 1 else [x.item() for x in flat[:10]]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _header_block(global_attrs: Dict[str, Any], scalars: Dict[str, Any],
                  times: Optional[Dict[str, Any]] = None) -> str:
    """`Key: value` text, with coordinates and time also under the names the mapper parses.

    Cast variables come before global attributes, since the parser keeps the first `Date:`.
    `times` holds the cast's decoded CF times, None where missing or implausible.
    """
    times = times or {}
    lines, supplied = [], set()
    for source, values in (("variable", scalars), ("global attribute", global_attrs)):
        for key, value in values.items():
            if source == "variable" and key in times:
                lines += _clock_lines(key, value, times[key])
                # Claimed even when the time is missing, so no file-wide date stands in.
                supplied |= {"Date", "Time"}
                continue
            if value is None or value == "":
                continue
            lines.append(f"{key}: {value}")
            alias = _coordinate_alias(key, value)
            kind = alias.partition(":")[0] if alias else None
            if alias and (source == "variable" or kind not in supplied):
                supplied.add(kind)
                lines.append(f"{alias} (from {source} {key})")
    return "\n".join(lines)


def _clock_lines(key: str, raw, when) -> List[str]:
    """A CF time as ISO text with `Date:`/`Time:` aliases, or the raw number if it gave no date."""
    if when is None:
        return [] if raw is None or raw == "" else [f"{key}: {raw}"]
    iso = _iso(when)
    return [f"{key}: {iso}", f"Date: {iso[:10]} (from variable {key})",
            f"Time: {iso[11:19]} (from variable {key})"]


def _is_clock(v) -> bool:
    """A CF time variable: numbers counted from the epoch its units name."""
    return (getattr(v.dtype, "kind", "S") in "fiu"
            and bool(_CF_TIME_UNITS.match(str(getattr(v, "units", "") or ""))))


def _first_valid(data):
    """The first value that is neither masked nor NaN, or None when there is none."""
    import numpy as np

    valid = np.ma.masked_invalid(np.ma.asarray(data, dtype="float64")).compressed()
    return valid[0].item() if valid.size else None


def _decode_time(value, units: str, calendar) -> Optional[Any]:
    """A CF time value as a datetime, or None when it is missing or will not decode.

    cftime applies any offset in the units (`... 00:00:00 0:00`), so the result is UTC.
    """
    from netCDF4 import num2date

    if not isinstance(value, (int, float)) or pd.isna(value):
        return None
    calendar = str(calendar or "standard").strip().lower()
    try:
        return num2date(value, units,
                        calendar=calendar if calendar in _CF_CALENDARS else "standard")
    except (ValueError, OverflowError):
        return None


def _plausible(when) -> bool:
    """True if a decoded time is between EARLIEST_PLAUSIBLE_YEAR and tomorrow."""
    latest = (datetime.now(timezone.utc) + timedelta(days=1)).timetuple()[:6]
    stamp = (when.year, when.month, when.day, when.hour, when.minute, when.second)
    return when.year >= EARLIEST_PLAUSIBLE_YEAR and stamp <= latest


def _iso(when) -> str:
    """ISO 8601 to the second in UTC, built from the fields so any calendar or century works."""
    return (f"{when.year:04d}-{when.month:02d}-{when.day:02d}"
            f"T{when.hour:02d}:{when.minute:02d}:{when.second:02d}Z")


def _coordinate_alias(key: str, value) -> Optional[str]:
    k = key.lower()
    if k in ("latitude", "lat", "geospatial_lat_min", "nominal_latitude"):
        return f"Latitude: {value}"
    if k in ("longitude", "lon", "geospatial_lon_min", "nominal_longitude"):
        return f"Longitude: {value}"
    # Not date_created/date_modified/date_issued: those date the file, not the cast.
    if k in ("time_coverage_start", "reference_date_time", "start_time"):
        text = str(value)
        if len(text) >= 8 and text[:8].isdigit():
            return f"Date: {text[:4]}-{text[4:6]}-{text[6:8]}"
        return f"Date: {text[:10]}"
    return None
