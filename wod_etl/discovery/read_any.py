"""One entry point that reads any file an accession can hold, whatever its extension.

`load_tabular` reads delimited text. Submissions are not only delimited text: NetCDF from a
float or glider programme, Sea-Bird `.cnv`, fixed-column ASCII from a 1970s cruise, an Excel
workbook, and any of those written with the wrong extension or none at all. This dispatches
on what `sniff_format` found in the bytes and returns the same shape for all of them.

    from wod_etl.discovery import read_any
    casts, info = read_any("data/raw/0287423/2-CTD/cast_01")

`casts` is a list of {"cast_id", "df", "header_block", "source_note"}. One entry for a
single-profile file, several for a file holding many. `header_block` is `Key: value` text in
the form `generate_field_mapping` already parses, so coordinates and times found in a NetCDF
attribute or a `.cnv` header reach the mapping contract by the same route as a `# Latitude:`
comment.

Never raises: an unreadable file returns ([], {"error": ...}).
"""
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wod_etl.config import NA_SENTINELS
from wod_etl.discovery.load_tabular import load_tabular
from wod_etl.discovery.profile_raw_data import profile_raw_data
from wod_etl.discovery.sniff_format import sniff_format
from wod_etl.discovery.split_casts import cast_as_profile_input, split_casts

# Dimension names that mean "levels within one cast" and "casts within one file". Matched
# case-insensitively against a substring, because conventions disagree: Argo writes
# N_LEVELS/N_PROF, CF discrete-sampling writes obs/profile, WOD's own NetCDF writes z/casts.
LEVEL_DIM_HINTS = ("level", "n_levels", "nlevels", "depth", "pres", "z", "obs",
                   "sample", "row", "nzmax", "maxz")
PROFILE_DIM_HINTS = ("prof", "n_prof", "cast", "station", "profile", "instance", "traj")

# NetCDF variables that are metadata about the cast rather than a measured level.
_HEADER_VAR_HINTS = ("lat", "lon", "juld", "time", "date", "platform", "cruise", "station",
                     "cast", "wmo", "project", "pi_name", "institution", "direction")

MAX_NETCDF_CELLS = 20_000_000

# A CF time variable is a count of units from an epoch: `seconds since 1970-01-01 00:00:00`.
_CF_TIME_UNITS = re.compile(r"^\s*[a-z]+\s+since\s+\S", re.IGNORECASE)

# The calendars CF defines. Anything else -- LeConte's files say `calendar: calendar` -- is
# read as the standard calendar, which is what CF says a missing attribute means.
_CF_CALENDARS = ("standard", "gregorian", "proleptic_gregorian", "julian", "noleap",
                 "365_day", "all_leap", "366_day", "360_day")

# A decoded time before this is a units error, not an early cast: no ocean profile is that
# old, and an epoch typed 1070 for 1970 puts a 2018 cast in the twelfth century.
EARLIEST_PLAUSIBLE_YEAR = 1700

# How much of a text file is read to profile its structure. The full file is read afterwards
# by load_tabular, which streams it through pandas.
PROFILE_BYTES = 65536


def read_any(path: str, sniff: Optional[Dict[str, Any]] = None,
             profile: Optional[Dict[str, Any]] = None,
             max_casts: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read a file of any supported format into a list of casts.

    Pass `sniff` if you already called `sniff_format`; otherwise it is called here.
    """
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
    _apply_missing_codes(casts, info["warnings"])
    if max_casts:
        casts = casts[:max_casts]
    info["cast_count"] = len(casts)
    if not casts and "error" not in info:
        info["error"] = f"read as {fmt['format_family']} but produced no casts"
    return casts, info


# ------------------------------------------------------------- delimited text
def _read_delimited(path, fmt, profile):
    """The existing text path: profile the preview, split casts, load each one."""
    # Only the head is needed to profile the structure -- load_tabular reads the whole file
    # afterwards. Reading a multi-gigabyte cruise file into memory to look at its first
    # twenty lines is the kind of thing that only shows up on the one accession that has one.
    with Path(path).open("r", encoding=fmt.get("encoding", "utf-8"), errors="replace") as fh:
        head = fh.read(PROFILE_BYTES)
    prof = profile or profile_raw_data(head, str(path))
    if prof.get("error"):
        return [], {"error": f"profile_raw_data: {prof['error']}"}

    split = split_casts(path, prof)
    if split.get("error"):
        return [], {"error": f"split_casts: {split['error']}"}
    # Whitespace-separated station blocks read as such whichever way the head sniffed:
    # profiling each block's text on its own takes its column line for one column.
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
        # The reader's own warnings travel with the cast -- dropped rows are reported here
        # or nowhere.
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
    """Fixed-column ASCII, using the boundaries sniff_format inferred.

    The boundaries are a guess from where whitespace held in every sampled row. They are
    reported in `source_note` so a wrong split is visible in the staged copy rather than
    silently shifting every field.
    """
    colspecs = fmt.get("colspecs")
    text = Path(path).read_text(encoding=fmt.get("encoding", "utf-8"), errors="replace")
    split = split_casts(text)
    if split.get("multi_cast"):
        return _read_station_blocks(split)
    lines = text.splitlines()
    header_lines = [l for l in lines[:40] if l.strip().startswith(("#", "*", "%", "//", ";"))]
    skip = _leading_comment_count(lines)

    # Whether the first surviving line is a column header or the first level decides
    # `header`. Getting it wrong is silent and total: read_fwf's default consumes row one as
    # names, so a headerless cast loses its shallowest level AND labels every column with
    # that level's values.
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
                        f"col_{df.shape[1] - 1}. Nothing downstream can map col_N to a WOD "
                        f"code -- name them during staging, from the header block or the "
                        f"accession's format description, with units folded into the name.")
    else:
        df.columns = [str(c).strip() for c in df.columns]
    note = (f"fixed width, {len(colspecs)} inferred columns at {colspecs}"
            if colspecs else "fixed width, boundaries inferred by pandas")
    return [{"cast_id": "0", "df": df, "header_block": "\n".join(header_lines),
             "source_note": note}], {"warnings": warnings}


def _read_station_blocks(split: Dict[str, Any]):
    """Casts from a file of station blocks -- a header (`Stn: KD0309 ; Lat: 81.671N; ...`,
    `Number of levels: 2201`), a line of column names, rows of numbers, the next block
    straight after -- the layout expedition and historical CTD files share.

    What comes before the first block (`Experiment:`, `Missing value: -9`, `Instrument:`)
    is the file's own header, and belongs to every cast, not just the first. A block whose
    rows do not match its column names, or the number of levels it states, is reported."""
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
    """The first block's header lines that come before the key every block's header starts
    with: the file's own header."""
    if len(casts) < 2 or not casts[1]["header_block"]:
        return ""
    key = _line_key(casts[1]["header_block"].splitlines()[0])
    lines = casts[0]["header_block"].splitlines()
    at = next((i for i, l in enumerate(lines) if key and _line_key(l) == key), 0)
    return "\n".join(lines[:at])


def _line_key(line: str) -> Optional[str]:
    m = re.match(r"\s*([A-Za-z][\w\s-]*?)\s*[:=]", line)
    return m.group(1).lower() if m else None


# A missing-value code a file DECLARES applies to that file: `Missing value: -9`,
# `missing_value = -99.0`, `Fill value: -9999`. Unsafe as a global default -- a real air
# temperature of -9 would vanish -- but here the originator has said what it means.
_DECLARED_MISSING = re.compile(r"\b(?:missing|fill|no[\s_-]?data)[\s_-]*(?:values?|codes?)?\s*"
                               r"[:=]\s*(-?\d+(?:\.\d+)?)(?![\d.])", re.IGNORECASE)


# Below this a value is a float fill, not a measurement: -9.99e-29 written for T and S at
# once where an instrument logged nothing. No Table 3a variable is that small in its units.
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

    The long name and unit are folded into one column label, because unit detection
    downstream reads the full raw column name and nothing else.
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
    """Rewrite the Sea-Bird header into the `Key: value` form the mapper parses.

    `** Latitude: 34 15.00 N` and `* NMEA Latitude = 34 15.00 N` both carry a position;
    only the first form is parsed downstream, so normalise the second onto it.
    """
    out = []
    for line in header_lines:
        s = line.lstrip("#* \t")
        if "=" in s and ":" not in s.split("=")[0]:
            key, _, value = s.partition("=")
            s = f"{key.strip()}: {value.strip()}"
        out.append(s)
    return "\n".join(out)


# ------------------------------------------------------------- spreadsheets
def _read_spreadsheet(path, fmt, profile):
    """Every sheet in a workbook is a candidate table; each becomes one cast."""
    try:
        sheets = pd.read_excel(path, sheet_name=None, na_values=list(NA_SENTINELS))
    except ImportError as e:
        return [], {"error": f"reading spreadsheets needs an Excel engine: {e}. "
                             f"Install openpyxl, or export the sheet to CSV and stage that."}
    out = []
    for name, df in sheets.items():
        if df.empty:
            continue
        df.columns = [str(c).strip() for c in df.columns]
        out.append({"cast_id": name, "df": df, "header_block": "",
                    "source_note": f"{fmt['format_family']}, sheet '{name}'"})
    return out, {"warnings": ["a workbook carries no header block; coordinates that live in "
                              "a title row or a separate sheet must be staged in by hand"],
                 "sheets": list(sheets)}


# ------------------------------------------------------------------ NetCDF
def _read_netcdf(path, fmt, profile):
    """Read NetCDF/HDF5 into one cast per profile.

    Handles the three layouts that actually turn up: one profile per file (every variable
    along a single level dimension), many profiles per file (variables shaped
    profile x level, as Argo writes), and CF contiguous ragged arrays (a `row_size`
    variable carrying `sample_dimension`).
    """
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        ds.set_auto_mask(True)
        global_attrs = {a: _scalar(ds.getncattr(a)) for a in ds.ncattrs()}
        variables = {name: v for name, v in ds.variables.items()}
        # Read while the file is open: a cast carries only the number stored for it, and a
        # count of seconds is not a date until its units and calendar say from when.
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
            casts, warnings = _netcdf_rectangular(variables, level_dim, profile_dim)

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
    # Once per variable, not per cast: an epoch that is wrong is wrong for every cast it dates.
    for name, whens in undated.items():
        warnings.append(f"'{name}' has units '{clocks[name][0]}' and decodes to "
                        f"{_iso(whens[0])}: the epoch looks wrong. {len(whens)} cast(s) are "
                        f"left with no date rather than that one; correct the units in a "
                        f"staged copy to date them.")
    return casts, {"warnings": warnings, "global_attributes": global_attrs}


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
        df = pd.DataFrame({_label(name, v): _column(v[start:start + n])
                           for name, v in level_vars.items()})
        casts.append({"cast_id": str(i), "df": df,
                      "_scalars": {name: _scalar(v[i]) for name, v in inst_vars.items()}})
        start += n
    return casts, [f"CF contiguous ragged array via '{spec['count_var']}': "
                   f"{len(casts)} profile(s)"]


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
        # A lone cast whose clock runs down its levels -- a time stamp on every scan -- is
        # dated by its first level, when the cast began.
        scalars.update({name: _first_valid(v[:]) for name, v in level_vars.items()
                        if _is_clock(v)})

    n_profiles = 1
    if profile_dim:
        for v in variables.values():
            if profile_dim in v.dimensions:
                n_profiles = v.shape[v.dimensions.index(profile_dim)]
                break

    # A flag variable's own scheme -- flag_values [0 1], flag_meanings "valid invalid" -- is
    # what makes its numbers readable; it travels with the column.
    schemes = {_label(name, v): {"flag_values": _scalar(v.getncattr("flag_values"))
                                 if "flag_values" in v.ncattrs() else None,
                                 "flag_meanings": str(v.getncattr("flag_meanings"))}
               for name, v in level_vars.items() if "flag_meanings" in v.ncattrs()}
    casts = []
    for i in range(n_profiles):
        columns = {}
        for name, v in level_vars.items():
            data = v[:] if v.dimensions == (level_dim,) else (
                v[i, :] if v.dimensions[0] == profile_dim else v[:, i])
            columns[_label(name, v)] = _column(data)
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
    """What a container variable says -- NCEI's templates describe the instrument in one,
    `instrument:make_model = "MVP"`, and its value is empty."""
    if not re.match(r"^(?:instrument|sensor|platform)", name, re.IGNORECASE):
        return None
    parts = [str(_scalar(v.getncattr(a))) for a in ("long_name", "make_model", "serial_number")
             if a in v.ncattrs() and str(_scalar(v.getncattr(a))).strip()]
    # " / ", not ";": a header value ends at a delimiter, and 'SVP; MVP; AML ...' was read
    # as plain 'SVP'.
    return " / ".join(dict.fromkeys(parts)) or None


def _is_char_name(v, dims, profile_dim):
    """A fixed-length character string, e.g. PLATFORM_NUMBER(N_PROF, STRING8)."""
    return (v.dtype.kind in "SU" and profile_dim in dims and len(dims) == 2
            and dims[0] == profile_dim)


def _index(v, i):
    data = v[:]
    if getattr(data, "ndim", 0) == 0:
        return data
    if data.ndim == 2 and v.dtype.kind in "SU":
        return b"".join(bytes(c) for c in data[i] if c is not None)
    return data[i] if i < len(data) else None


def _pick_dim(ds, variables, hints, exclude=None, prefer=None):
    """The dimension whose name matches a hint; fall back to the largest one in use.

    Only dimensions that carry numbers count. A string's length is a dimension too --
    `profile_id_length` spells out each profile's id -- and it matches the hint 'profile'
    while being larger than the real profile dimension, so taking it made a one-cast file
    read as no casts at all."""
    used = {d for v in variables.values() if v.dtype.kind not in "SU" for d in v.dimensions}
    named = [d for d in ds.dimensions if d in used and d != exclude
             and any(h in d.lower() for h in hints)]
    if named:
        return max(named, key=lambda d: len(ds.dimensions[d]))
    if prefer == "largest":
        candidates = [d for d in used if d != exclude and len(ds.dimensions[d]) > 1]
        return max(candidates, key=lambda d: len(ds.dimensions[d])) if candidates else None
    return None


def _label(name: str, v) -> str:
    """Column label carrying its unit, because unit detection reads the name and nothing else."""
    units = getattr(v, "units", None) or getattr(v, "unit", None)
    if units and str(units).strip() and "since" not in str(units):
        return f"{name} ({str(units).strip()})"
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
    """`Key: value` text, with coordinates and time normalised to parseable names.

    The mapper's header parser looks for `Latitude:`, `Longitude:`, `Date:` and `Time:`.
    NetCDF spells them LATITUDE, JULD, REFERENCE_DATE_TIME and a dozen other ways, so the
    recognised ones are re-emitted under the parsed name AND kept under their original name,
    so nothing is lost and the provenance stays visible.

    The parser keeps the FIRST `Date:` it meets, so the cast's own variables are written
    before the file's attributes, and an attribute is aliased only for what the cast left
    unsaid. The other way round, `time_coverage_start` gave all 155 casts in a file one date.
    `times` is the cast's CF time variables decoded, None where missing or implausible.
    """
    times = times or {}
    lines, supplied = [], set()
    for source, values in (("variable", scalars), ("global attribute", global_attrs)):
        for key, value in values.items():
            if source == "variable" and key in times:
                lines += _clock_lines(key, value, times[key])
                # Claimed even when this cast's time is missing or implausible: a file-wide
                # date standing in for it is exactly the false date this exists to prevent.
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
    """A CF time variable's lines: its value in ISO form with `Date:` and `Time:` aliases,
    or only the stored number when it decoded to nothing a cast can be dated by."""
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

    cftime applies an offset written into the units (`... 00:00:00 0:00`), so the result
    is UTC whatever zone the epoch was given in.
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
    """Whether a decoded time could be when a cast was taken: no earlier than
    EARLIEST_PLAUSIBLE_YEAR and no later than tomorrow."""
    latest = (datetime.now(timezone.utc) + timedelta(days=1)).timetuple()[:6]
    stamp = (when.year, when.month, when.day, when.hour, when.minute, when.second)
    return when.year >= EARLIEST_PLAUSIBLE_YEAR and stamp <= latest


def _iso(when) -> str:
    """ISO 8601 to the second, in UTC. Built from the fields, so a date in any calendar or
    century -- including the twelfth-century ones a mistyped epoch gives -- formats alike."""
    return (f"{when.year:04d}-{when.month:02d}-{when.day:02d}"
            f"T{when.hour:02d}:{when.minute:02d}:{when.second:02d}Z")


def _coordinate_alias(key: str, value) -> Optional[str]:
    k = key.lower()
    if k in ("latitude", "lat", "geospatial_lat_min", "nominal_latitude"):
        return f"Latitude: {value}"
    if k in ("longitude", "lon", "geospatial_lon_min", "nominal_longitude"):
        return f"Longitude: {value}"
    # Not date_created, date_modified or date_issued: those say when the FILE was made,
    # which is never when a cast was taken.
    if k in ("time_coverage_start", "reference_date_time", "start_time"):
        text = str(value)
        if len(text) >= 8 and text[:8].isdigit():
            return f"Date: {text[:4]}-{text[4:6]}-{text[6:8]}"
        return f"Date: {text[:10]}"
    return None
