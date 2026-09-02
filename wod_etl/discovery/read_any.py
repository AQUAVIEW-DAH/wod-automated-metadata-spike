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

    for c in casts:
        c["header_block"] = _header_block(global_attrs, c.pop("_scalars", {}))
        c["source_note"] = f"NetCDF ({fmt['format_family']})"
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
            scalars[name] = _scalar(v[:])
        elif _is_char_name(v, dims, profile_dim):
            per_profile[name] = v
        else:
            warnings.append(f"'{name}' has dimensions {dims}; not a level or profile "
                            f"variable, skipped")

    if not level_vars:
        return [], warnings + [f"no variable is shaped along the level dimension "
                               f"'{level_dim}'"]

    n_profiles = 1
    if profile_dim:
        for v in variables.values():
            if profile_dim in v.dimensions:
                n_profiles = v.shape[v.dimensions.index(profile_dim)]
                break

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
        casts.append({"cast_id": str(i), "df": df, "_scalars": cast_scalars})

    if not casts:
        warnings.append("every profile was empty after dropping all-missing levels")
    return casts, warnings


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
    """The dimension whose name matches a hint; fall back to the largest one in use."""
    used = {d for v in variables.values() for d in v.dimensions}
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
        return value.decode("utf-8", "replace").strip()
    if isinstance(value, np.ndarray) or hasattr(value, "mask"):
        flat = np.ma.filled(np.ma.asarray(value), np.nan).ravel()
        if flat.dtype.kind in "SU":
            return b"".join(bytes(c) for c in flat).decode("utf-8", "replace").strip() \
                if flat.dtype.kind == "S" else "".join(str(c) for c in flat).strip()
        return flat[0].item() if flat.size == 1 else [x.item() for x in flat[:10]]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _header_block(global_attrs: Dict[str, Any], scalars: Dict[str, Any]) -> str:
    """`Key: value` text, with coordinates and time normalised to parseable names.

    The mapper's header parser looks for `Latitude:`, `Longitude:`, `Date:` and `Time:`.
    NetCDF spells them LATITUDE, JULD, REFERENCE_DATE_TIME and a dozen other ways, so the
    recognised ones are re-emitted under the parsed name AND kept under their original name,
    so nothing is lost and the provenance stays visible.
    """
    lines = []
    for source, values in (("global attribute", global_attrs), ("variable", scalars)):
        for key, value in values.items():
            if value is None or value == "":
                continue
            lines.append(f"{key}: {value}")
            alias = _coordinate_alias(key, value)
            if alias:
                lines.append(f"{alias} (from {source} {key})")
    return "\n".join(lines)


def _coordinate_alias(key: str, value) -> Optional[str]:
    k = key.lower()
    if k in ("latitude", "lat", "geospatial_lat_min", "nominal_latitude"):
        return f"Latitude: {value}"
    if k in ("longitude", "lon", "geospatial_lon_min", "nominal_longitude"):
        return f"Longitude: {value}"
    if k in ("time_coverage_start", "date_created", "reference_date_time", "start_time"):
        text = str(value)
        if len(text) >= 8 and text[:8].isdigit():
            return f"Date: {text[:4]}-{text[4:6]}-{text[6:8]}"
        return f"Date: {text[:10]}"
    return None
