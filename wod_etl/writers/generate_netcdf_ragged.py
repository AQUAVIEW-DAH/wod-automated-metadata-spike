"""Write CF-compliant contiguous ragged array NetCDF files.

All profile variables share ONE observation dimension (`z_obs`) with a single `z_row_size`
count variable on the `casts` dimension. That is what makes the array a CF Discrete
Sampling Geometry: depth and every measurement are index-aligned, and `z_row_size` says how
many observations belong to each cast.

Variable names are sanitised to CF identifiers (letter first, then letters/digits/
underscore); the WOD name is preserved in `long_name`.
"""
import os
import re
from typing import Dict, Any, List, Optional

import netCDF4 as nc
import numpy as np
import pandas as pd

from wod_etl.config import CF_CONVENTIONS, NETCDF_FILL_VALUE, QC_FILL_VALUE, WOD_VERSION
from wod_etl.reference.load_tables import flag_meanings_attr, flag_values_attr
from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE

OBS_DIM = "z_obs"
CAST_DIM = "casts"
# Every file this writer makes says so: what a rerun may replace is only a file it wrote.
TITLE = "WOD Intermediate Standardized Ocean Profile"

# CF flag attributes are generated from Table 12 rather than transcribed, so the file's
# flag_meanings can never disagree with the flags the QC phase actually assigned.
_OBS_FLAG_VALUES = flag_values_attr("observed_level")
_OBS_FLAG_MEANINGS = flag_meanings_attr("observed_level")
_DEPTH_FLAG_VALUES = flag_values_attr("depth")
_DEPTH_FLAG_MEANINGS = flag_meanings_attr("depth")


# CF wants `units` a UDUNITS string. Table 3a's own wording ("Degrees Celsius") is not one,
# so it goes in `wod_units` and nothing a WOD reader relies on is lost. Keyed by Table 3a's
# standard unit; the values are what the data already are -- no conversion happens here.
CF_UNITS = {
    "Degrees Celsius": "degree_C", "Dimensionless": "1", "Micromole kilogram-1": "umol kg-1",
    "Microgram liter-1": "ug L-1", "Millimole liter-1": "mmol L-1", "Micro atmosphere": "uatm",
    "Per meter": "m-1", "Decibar": "dbar", "Millibar": "mbar", "Degrees": "degree",
    "Day": "day", "Tritium Unit": "1e-18", "Nanomole kilogram-1": "nmol kg-1",
    "Percent": "percent", "Per mille": "1e-3", "Picomole kilogram-1": "pmol kg-1",
}
# CF standard names for the Table 3a variables that have one, as WOD stores them. pH has no
# scale in WOD, so it gets none rather than a guessed one.
CF_STANDARD_NAMES = {
    1: "sea_water_temperature", 2: "sea_water_practical_salinity",
    3: "moles_of_oxygen_per_unit_mass_in_sea_water",
    4: "moles_of_phosphate_per_unit_mass_in_sea_water",
    6: "moles_of_silicate_per_unit_mass_in_sea_water",
    8: "moles_of_nitrate_and_nitrite_per_unit_mass_in_sea_water",
    11: "mass_concentration_of_chlorophyll_in_sea_water",
    17: "sea_water_alkalinity_expressed_as_mole_equivalent",
    20: "partial_pressure_of_carbon_dioxide_in_sea_water",
    21: "mole_concentration_of_dissolved_inorganic_carbon_in_sea_water",
    24: "volume_beam_attenuation_coefficient_of_radiative_flux_in_sea_water",
    25: "sea_water_pressure", 26: "air_temperature", 29: "air_pressure",
    30: "latitude", 31: "longitude",
}


# Units of the secondary headers that are measurements, for CF: Table 4a names them in the
# header's title ("Bottom depth (meters)").
_HEADER_UNITS = {10: "m"}


def cf_safe_name(name: str) -> str:
    """Turn a WOD variable name into a valid CF/NetCDF identifier.

    'Nitrate and Nitrate + Nitrite' -> 'nitrate_and_nitrate_nitrite'
    'Dissolved inorganic carbon'    -> 'dissolved_inorganic_carbon'
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "var"
    if not s[0].isalpha():
        s = "v_" + s
    return s


def is_cf_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(name)))


def generate_netcdf_ragged(
    df_qc: pd.DataFrame,
    mapping_contract: Dict[str, Any],
    output_nc_path: str,
    transform_log: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write a single cast. Thin wrapper over write_ragged_profiles."""
    return write_ragged_profiles(
        [{"df": df_qc, "contract": mapping_contract, "transform_log": transform_log}],
        output_nc_path,
    )


def write_ragged_profiles(casts: List[Dict[str, Any]], output_nc_path: str) -> Dict[str, Any]:
    """Write one or more casts into a single contiguous ragged array file.

    Each cast is {"df": DataFrame, "contract": mapping_contract, "transform_log": dict}.
    Casts may have different column sets; the union is written and absent values are fill.
    """
    if not casts:
        return {"status": "error", "error": "no casts supplied", "output_path": output_nc_path}

    parent = os.path.dirname(os.path.abspath(output_nc_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    plan = _plan_variables(casts)
    row_sizes = [len(c["df"]) for c in casts]
    total_obs = int(sum(row_sizes))
    n_casts = len(casts)

    with nc.Dataset(output_nc_path, "w", format="NETCDF4") as ds:
        # Dimensions first: the globals can write per-cast variables (notes, character data
        # that differ between casts), and those need the `casts` dimension to exist.
        ds.createDimension(CAST_DIM, n_casts)
        ds.createDimension(OBS_DIM, total_obs)

        _write_globals(ds, casts)

        # ---- instance (per-cast) variables ---------------------------------
        _cast_var(ds, "lat", "f8", [_coord(c, "latitude") for c in casts],
                  units="degrees_north", standard_name="latitude", long_name="cast latitude")
        _cast_var(ds, "lon", "f8", [_coord(c, "longitude") for c in casts],
                  units="degrees_east", standard_name="longitude", long_name="cast longitude")

        times = _cast_times(casts)
        tv = ds.createVariable("time", "f8", (CAST_DIM,), fill_value=np.nan)
        tv.units = "days since 1770-01-01 00:00:00"
        tv.standard_name = "time"
        tv.long_name = "cast time"
        tv.calendar = "gregorian"
        tv[:] = times["time"]
        _describe_time(tv, times)

        # WOD's own ragged files carry the date and the time of day separately, because they
        # are known separately: a cast with a date and no clock time has `date` and a fill
        # `GMT_time`. Folding both into `time` alone wrote such a cast as 00:00 UTC -- a
        # precise-looking midnight nobody measured.
        dv = ds.createVariable("date", "i4", (CAST_DIM,), fill_value=np.int32(-2147483647))
        dv.long_name = "cast date, YYYYMMDD (UTC when the time of day is known)"
        dv[:] = np.array(times["date"], dtype="i4")
        gv = ds.createVariable("GMT_time", "f4", (CAST_DIM,), fill_value=NETCDF_FILL_VALUE)
        gv.long_name = "cast time of day, decimal hours UTC; fill when not stated"
        gv.units = "hours"
        gv[:] = np.array(times["hours"], dtype="f4")
        _write_time_zones(ds, casts, times)

        # NETCDF4 variable-length strings; "S1" would truncate each id to one character.
        idv = ds.createVariable("cast_id", str, (CAST_DIM,))
        idv.cf_role = "profile_id"
        idv.long_name = "cast identifier"
        idv.comment = ("accession, originator's cruise, cast or station, and date -- unique "
                       "within this file; verify_output checks it across a run")
        for i, cid in enumerate(_cast_ids(casts)):
            idv[i] = cid
        # An id a suffix made unique within this file, though casts of two SOURCE files had it:
        # one cast exported twice, or two that cannot be told apart. Said here so verify
        # finds it, as it did when each source had a file of its own.
        shared = _ids_shared_by_sources(casts)
        if shared:
            idv.repeated_across_source_files = "; ".join(shared)

        rs = ds.createVariable("z_row_size", "i4", (CAST_DIM,))
        rs.long_name = "number of observations for this cast"
        rs.sample_dimension = OBS_DIM
        rs[:] = np.array(row_sizes, dtype="i4")

        # Per cast, as WOD's own files carry it: a cast keeps its dataset when files are
        # merged, which a file-level attribute does not survive.
        dv_ = ds.createVariable("dataset", str, (CAST_DIM,))
        dv_.long_name = "WOD dataset (Table 2)"
        for i, code in enumerate(_dataset_codes(casts)):
            dv_[i] = code

        _write_varying_secondary_headers(ds, casts)

        # ---- observation (per-level) variables -----------------------------
        zv = ds.createVariable("z", "f4", (OBS_DIM,), fill_value=NETCDF_FILL_VALUE)
        zv.units = "m"
        zv.positive = "down"
        zv.axis = "Z"
        zv.standard_name = "depth"
        zv.long_name = "depth below sea surface"
        zv[:] = np.ma.masked_invalid(_concat_depth(casts, total_obs))

        depth_flags = _concat_flags(casts, total_obs, depth=True)
        if depth_flags is not None:
            zq = ds.createVariable("z_qc", "i1", (OBS_DIM,), fill_value=QC_FILL_VALUE)
            zq.long_name = "WOD depth quality flag (Table 12 section 2a)"
            zq.flag_values = np.array(_DEPTH_FLAG_VALUES, dtype=np.int8)
            zq.flag_meanings = _DEPTH_FLAG_MEANINGS
            zq[:] = depth_flags
            zv.ancillary_variables = "z_qc"

        written = []
        for var in plan:
            name = var["nc_name"]
            data = _concat_values(casts, var, total_obs)

            v = ds.createVariable(name, "f4", (OBS_DIM,), fill_value=NETCDF_FILL_VALUE)
            v.units = CF_UNITS.get(var["units"], var["units"])
            v.wod_units = var["units"]
            if var["wod_code"] in CF_STANDARD_NAMES:
                v.standard_name = CF_STANDARD_NAMES[var["wod_code"]]
            v.long_name = var["wod_name"]
            v.wod_variable_code = np.int32(var["wod_code"])
            v.coordinates = "time lat lon z"
            if var.get("unit_abbrev"):
                v.unit_abbrev = var["unit_abbrev"]
            # Missing is the declared _FillValue, not NaN under it: a CF reader masks the
            # one and sees a number in the other.
            v[:] = np.ma.masked_invalid(data)

            ancillary = []
            flags = _concat_flags(casts, total_obs, var=var)
            if flags is not None:
                qname = f"{name}_qc"
                q = ds.createVariable(qname, "i1", (OBS_DIM,), fill_value=QC_FILL_VALUE)
                q.long_name = f"WOD observed level quality flag for {var['wod_name']} (Table 12 section 2b)"
                q.flag_values = np.array(_OBS_FLAG_VALUES, dtype=np.int8)
                q.flag_meanings = _OBS_FLAG_MEANINGS
                q[:] = flags
                ancillary.append(qname)

            # The originator's own flags, carried through verbatim. They are NOT WOD Table
            # 12 flags -- a submitter's 1 may mean 'good' where WOD's 1 means a failed range
            # check -- so they get their own variable, no flag_values/flag_meanings, and an
            # attribute pointing at the code table that names the scheme. Translating them
            # would invent meaning; dropping them would lose the only QC the file has.
            orig, orig_sources, schemes = _concat_originator_flags(casts, total_obs, var)
            if orig is not None:
                oname = f"{name}_qc_originator"
                o = ds.createVariable(oname, "f4", (OBS_DIM,), fill_value=NETCDF_FILL_VALUE)
                o.long_name = (f"originator's quality flag for {var['wod_name']}, "
                               f"as submitted")
                o.comment = ("Values are the ORIGINATOR's, on the originator's scale. They "
                             "are not WOD Table 12 flags and must not be read as such. WOD "
                             "records which flag set was used as secondary header 96.")
                o.wod_secondary_header = np.int32(96)
                o.code_table = (
                    "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/"
                    "s_96_origflagset.html")
                _write_originator_flag_source(ds, o, oname, orig_sources, schemes)
                o[:] = np.ma.masked_invalid(np.asarray(orig, dtype=float))
                ancillary.append(oname)

            if ancillary:
                v.ancillary_variables = " ".join(ancillary)

            # setncattr, not setattr: netCDF4 reserves a set of Python attribute names on
            # Variable ('scale', 'mask', 'name', 'dtype'...) and rebinding one raises.
            shared, varying = _variable_metadata(casts, var)
            for attr, value in shared.items():
                v.setncattr(attr, value)
            # What differs between the casts -- two source files naming the column, or
            # stating its unit, differently -- is said per cast rather than the first
            # cast's value claimed for all of them.
            per_cast = [_per_cast_text(ds, f"{name}_{attr}", values,
                                       f"{var['wod_name']}: {attr.replace('_', ' ')}")
                        for attr, values in varying.items()]
            if per_cast:
                v.per_cast_attributes = " ".join(per_cast)

            written.append({"nc_name": name, "wod_name": var["wod_name"],
                            "wod_code": var["wod_code"], "units": var["units"]})

    return {
        "status": "success",
        "output_path": output_nc_path,
        "cast_count": n_casts,
        "total_observations": total_obs,
        "row_sizes": row_sizes,
        "variables_written": [w["nc_name"] for w in written],
        "variable_detail": written,
    }


# ------------------------------------------------------------------ helpers
def _plan_variables(casts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Union of measured variables across casts, keyed by WOD code, with CF-safe names."""
    by_code: Dict[int, Dict[str, Any]] = {}
    for c in casts:
        for m in c["contract"].get("column_mapping", []):
            code = m.get("wod_code")
            if code is None or code == DEPTH_SENTINEL_CODE:
                continue
            if m.get("raw_column") not in c["df"].columns:
                continue
            by_code.setdefault(code, {
                "wod_code": code,
                "wod_name": m.get("wod_name", f"code_{code}"),
                "units": m.get("target_unit", ""),
                "unit_abbrev": m.get("unit_abbrev"),
                "nc_name": cf_safe_name(m.get("wod_name", f"code_{code}")),
            })

    seen = set()
    for v in by_code.values():
        base = v["nc_name"]
        n = 2
        while v["nc_name"] in seen:
            v["nc_name"] = f"{base}_{n}"
            n += 1
        seen.add(v["nc_name"])
    return [by_code[k] for k in sorted(by_code)]


def _raw_col(cast: Dict[str, Any], code: int) -> Optional[str]:
    for m in cast["contract"].get("column_mapping", []):
        if m.get("wod_code") == code:
            col = m.get("raw_column")
            return col if col in cast["df"].columns else None
    return None


def _concat_values(casts, var, total_obs) -> np.ndarray:
    out = np.full(total_obs, np.nan, dtype=float)
    at = 0
    for c in casts:
        n = len(c["df"])
        col = _raw_col(c, var["wod_code"])
        if col is not None:
            out[at:at + n] = pd.to_numeric(c["df"][col], errors="coerce").to_numpy(dtype=float)
        at += n
    return out


def _concat_depth(casts, total_obs) -> np.ndarray:
    out = np.full(total_obs, np.nan, dtype=float)
    at = 0
    for c in casts:
        n = len(c["df"])
        col = _raw_col(c, DEPTH_SENTINEL_CODE)
        if col is not None:
            out[at:at + n] = pd.to_numeric(c["df"][col], errors="coerce").to_numpy(dtype=float)
        at += n
    return out


def _concat_flags(casts, total_obs, var=None, depth=False) -> Optional[np.ndarray]:
    out = np.full(total_obs, QC_FILL_VALUE, dtype=int)
    at = 0
    found = False
    for c in casts:
        n = len(c["df"])
        code = DEPTH_SENTINEL_CODE if depth else var["wod_code"]
        col = _raw_col(c, code)
        if col is not None:
            fcol = f"{col}_qc_flag"
            if fcol in c["df"].columns:
                out[at:at + n] = pd.to_numeric(c["df"][fcol], errors="coerce").fillna(QC_FILL_VALUE).to_numpy(dtype=int)
                found = True
        at += n
    return out if found else None


def _concat_originator_flags(casts, total, var):
    """The originator's flag column for one variable, concatenated across casts, with each
    cast's flag column name and stated flag scheme (None for a cast without flags).

    Kept as float, not int: an originator flag is not always an integer -- some submitters
    use letters or decimals -- and coercing to int would silently rewrite them.
    """
    out = np.full(total, np.nan, dtype=float)
    at, found = 0, False
    sources: List[Optional[str]] = []
    schemes: List[Optional[Dict[str, Any]]] = []
    for c in casts:
        n = len(c["df"])
        mapping = next((m for m in c["contract"].get("column_mapping", [])
                        if m.get("wod_code") == var["wod_code"]), None)
        fcol = (mapping or {}).get("originator_flag_column")
        if fcol and fcol in c["df"].columns:
            out[at:at + n] = pd.to_numeric(c["df"][fcol], errors="coerce").to_numpy(dtype=float)
            found = True
            sources.append(str(fcol))
            schemes.append((c.get("column_attributes") or {}).get(fcol))
        else:
            sources.append(None)
            schemes.append(None)
        at += n
    return (out, sources, schemes) if found else (None, None, None)


def _write_originator_flag_source(ds, o, oname, sources, schemes) -> None:
    """Which column the originator's flags came from, and the originator's own scheme where
    the source states it (CF 3.5: what their 0 and 1 mean, so the numbers can be read).

    Variable attributes when every flagged cast agrees -- one source file always does. Casts
    from sources that differ get them per cast: one file's scheme is not another's."""
    flagged = [i for i, s in enumerate(sources) if s is not None]
    named = {sources[i] for i in flagged}
    if len(named) == 1:
        o.source_column = named.pop()
    else:
        _per_cast_text(ds, f"{oname}_source_column", ["" if s is None else s for s in sources],
                       "column the originator's flags were read from")
    stated = [schemes[i] for i in flagged]
    readable = lambda s: bool(s and s.get("flag_meanings"))
    if not any(readable(s) for s in stated):
        return
    values = lambda s: (" ".join(str(x) for x in np.atleast_1d(s["flag_values"]))
                        if readable(s) and s.get("flag_values") is not None else "")
    if all(readable(s) for s in stated) and \
            len({(values(s), str(s["flag_meanings"])) for s in stated}) == 1:
        scheme = stated[0]
        if scheme.get("flag_values") is not None:
            o.flag_values = np.asarray(np.atleast_1d(scheme["flag_values"]), dtype="f4")
        o.flag_meanings = scheme["flag_meanings"]
        return
    _per_cast_text(ds, f"{oname}_flag_values", [values(s) for s in schemes],
                   "the originator's flag values for this cast's source")
    _per_cast_text(ds, f"{oname}_flag_meanings",
                   [str(s["flag_meanings"]) if readable(s) else "" for s in schemes],
                   "what the originator's flag values mean for this cast's source")


def _per_cast_text(ds, name: str, values: List[str], long_name: str) -> str:
    """A text value per cast, as a `casts` variable. Returns the name it was written as."""
    base, n = name, 2
    while name in ds.variables:
        name, n = f"{base}_{n}", n + 1
    v = ds.createVariable(name, str, (CAST_DIM,))
    v.long_name = long_name
    for i, x in enumerate(values):
        v[i] = "" if x is None else str(x)
    return name


def _cast_variable_metadata(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """One cast's Table 5a variable specific headers for one variable, as attributes.

    The originator's unit and the measurement scale are the two that always matter: a
    conversion is only auditable against the unit the source declared, and a temperature is
    ambiguous without knowing whether it is on ITS-90 or IPTS-68.
    """
    attrs: Dict[str, Any] = {}
    headers = mapping.get("variable_specific_headers") or {}
    if mapping.get("originator_unit"):
        attrs["originator_units"] = str(mapping["originator_unit"])
        if "8" not in headers:
            attrs["wod_variable_specific_header_8"] = str(mapping["originator_unit"])
    if mapping.get("originator_scale"):
        attrs["measurement_scale"] = str(mapping["originator_scale"])
        if "3" not in headers:
            attrs["wod_variable_specific_header_3"] = str(mapping["originator_scale"])
    for vid, header in headers.items():
        key = f"wod_variable_specific_header_{vid}"
        if key in attrs or f"{key}_originator_text" in attrs:
            continue
        value, text, summary = split_coded_value(header)
        if value is not None:
            attrs[key] = (str(value) if not isinstance(value, (int, float)) else value)
        if text is not None:
            attrs[f"{key}_originator_text"] = str(text)
        if summary is not None:
            attrs[f"{key}_code_lookup"] = str(summary)
    if mapping.get("comment"):
        attrs["comment"] = str(mapping["comment"])
    if mapping.get("raw_column"):
        attrs["originator_column_name"] = str(mapping["raw_column"])
    return attrs


def _variable_metadata(casts, var):
    """(attributes every cast holding the variable agrees on, {attribute: per-cast text} for
    those that differ). A cast without the variable has '' in a per-cast list."""
    per_cast: List[Optional[Dict[str, Any]]] = []
    for c in casts:
        mapping = next((m for m in c["contract"].get("column_mapping", [])
                        if m.get("wod_code") == var["wod_code"]), None)
        held = mapping is not None and mapping.get("raw_column") in c["df"].columns
        per_cast.append(_cast_variable_metadata(mapping) if held else None)
    present = [a for a in per_cast if a is not None]
    shared: Dict[str, Any] = {}
    varying: Dict[str, List[str]] = {}
    for key in dict.fromkeys(k for a in present for k in a):
        values = [a.get(key) for a in present]
        if all(v is not None for v in values) and len({repr(v) for v in values}) == 1:
            shared[key] = values[0]
        else:
            varying[key] = ["" if a is None or a.get(key) is None else str(a[key])
                            for a in per_cast]
    return shared, varying


def _coord(cast, key) -> float:
    v = cast["contract"].get("parsed_coordinates", {}).get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


_EPOCH = pd.Timestamp("1770-01-01")


def utc_offset_hours(zone: Any) -> Optional[float]:
    """Hours to ADD to UTC to get the zone's local time, or None if the zone is not numeric.

    'UTC', 'GMT', 'Z' -> 0; 'UTC+10', 'GMT-3:30', '+09:00', '+0900' -> the offset. Named
    zones ('JST', 'local', 'ship time') return None: converting on a guess about a name
    is how a whole cruise shifts by a day.
    """
    z = str(zone or "").strip().upper()
    if z in ("UTC", "GMT", "Z", "UT"):
        return 0.0
    m = re.fullmatch(r"(?:UTC|GMT|UT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", z)
    if not m:
        return None
    hours = int(m.group(2)) + int(m.group(3) or 0) / 60.0
    return hours if m.group(1) == "+" else -hours


def _cast_times(casts) -> Dict[str, Any]:
    """Per cast: `time` (days since 1770), `date` (YYYYMMDD), `hours` (UTC, or fill).

    A cast with a date and no time of day keeps its date and gets a fill time of day; `time`
    sits at 00:00 of that date because CF needs a value, and the variable's comment says so.
    A stated numeric zone with a known time of day is converted to UTC; a named zone, or a
    date with no time, is recorded and NOT converted -- there is no hour to shift.
    """
    out = {"time": [], "date": [], "hours": [], "time_known": [], "date_known": [],
           "zone": [], "converted": []}
    for c in casts:
        pc = c["contract"].get("parsed_coordinates", {})
        zone = pc.get("time_zone")
        try:
            day = pd.Timestamp(year=int(pc["year"]), month=int(pc["month"]), day=int(pc["day"]))
        except (KeyError, TypeError, ValueError):
            for k, v in (("time", np.nan), ("date", -2147483647), ("hours", NETCDF_FILL_VALUE),
                         ("time_known", False), ("date_known", False), ("zone", zone),
                         ("converted", False)):
                out[k].append(v)
            continue
        known = pc.get("hour") is not None
        ts = day + pd.Timedelta(hours=int(pc.get("hour") or 0),
                                minutes=int(pc.get("minute") or 0),
                                seconds=float(pc.get("second") or 0))
        offset = utc_offset_hours(zone) if zone else None
        converted = bool(known and offset)
        if converted:
            ts = ts - pd.Timedelta(hours=offset)
        out["time"].append((ts - _EPOCH).total_seconds() / 86400.0)
        out["date"].append(int(ts.strftime("%Y%m%d")))
        out["hours"].append(ts.hour + ts.minute / 60.0 + (ts.second + ts.microsecond / 1e6)
                            / 3600.0 if known else NETCDF_FILL_VALUE)
        out["time_known"].append(known)
        out["date_known"].append(True)
        out["zone"].append(zone)
        out["converted"].append(converted)
    out["time"] = np.array(out["time"], dtype=float)
    return out


def _describe_time(tv, times) -> None:
    n = len(times["time_known"])
    no_date = n - sum(times["date_known"])
    date_only = sum(1 for d, t in zip(times["date_known"], times["time_known"]) if d and not t)
    parts = []
    if no_date == n:
        parts.append("no usable date in the source header; values are fill")
    elif no_date:
        parts.append(f"{no_date} of {n} cast(s) have no usable date; their time is fill")
    if date_only:
        parts.append(f"{date_only} of {n} cast(s) have a date but no time of day: their time "
                     f"is 00:00 of that date by convention and GMT_time is fill. Do not read "
                     f"it as midnight")
    converted = sum(times["converted"])
    if converted:
        parts.append(("" if converted == n else f"{converted} of {n} cast(s): ")
                     + "converted to UTC from the stated time zone (see time_zone_of_source)")
    if parts:
        tv.comment = "; ".join(parts)


def _write_time_zones(ds, casts, times) -> None:
    zones = times["zone"]
    if not any(zones):
        return
    if len(set(zones)) == 1:
        ds.setncattr("time_zone_of_source", str(zones[0]))
    else:
        v = ds.createVariable("time_zone_of_source", str, (CAST_DIM,))
        v.long_name = "time zone the originator's date and time were stated in"
        for i, z in enumerate(zones):
            v[i] = "" if z is None else str(z)
    unconverted = [z for z, k, c in zip(zones, times["time_known"], times["converted"])
                   if z and not c and utc_offset_hours(z) != 0.0]
    if unconverted:
        n = len(zones)
        ds.setncattr("time_zone_note",
                     ("" if len(unconverted) == n else f"{len(unconverted)} of {n} cast(s): ")
                     + "the source date/time is not UTC and was NOT converted -- either no "
                     "time of day was stated, or the zone is not a numeric offset. A local "
                     "date may be a different UTC date.")


def _cast_id(cast, index) -> str:
    """A cast identifier that means something outside this file.

    It used to be the platform plus the cast's position within its own file, so every
    one-cast file got `<platform>_0000` and a run of eleven casts had eleven identical ids.
    Built now from what identifies a cast to a reader -- the accession (or, with none, the
    platform), the originator's cruise, the cast or station (or, with neither, the source
    file's name), and the date with its time of day when known.
    """
    meta = _cast_secondary_headers(cast)
    contract = cast["contract"]
    value = lambda sid: str((meta.get(sid) or {}).get("originator_value")
                            or (meta.get(sid) or {}).get("value") or "").strip()
    cruise = str(((contract.get("character_data") or {}).get("originator_cruise_id")
                  or {}).get("value") or "").strip()
    cast_or_station = value("5") or (f"st{value('7')}" if value("7") else "")
    if not cast_or_station:
        # Nothing names the cast, so the file it came from does: several XBT drops on one
        # day from one ship would otherwise share platform-and-date.
        src = str(contract.get("source") or "")
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", src.replace("\\", "/").rsplit("/", 1)[-1])
        cast_or_station = stem if stem not in ("", "unknown") else ""
    pc = contract.get("parsed_coordinates") or {}
    try:
        date = f"{int(pc['year']):04d}{int(pc['month']):02d}{int(pc['day']):02d}"
        if pc.get("hour") is not None:
            date += f"T{int(pc['hour']):02d}{int(pc.get('minute') or 0):02d}"
    except (KeyError, TypeError, ValueError):
        date = ""
    # The accession scopes the id; without one the platform does, so ids from two
    # accessions' files cannot meet on a bare station number.
    scope = value("1") or value("3")
    parts = [scope, cruise if cruise and cruise not in cast_or_station else "",
             cast_or_station]
    if not any(parts):
        parts = ["unknown"]
    parts.append(date)
    cid = "_".join(re.sub(r"[^A-Za-z0-9.-]+", "-", p).strip("-") for p in parts if p)
    return (cid or f"cast_{index:04d}")[:64]


def _ids_shared_by_sources(casts) -> List[str]:
    """Cast ids (before a suffix makes them unique) that casts of different sources share."""
    sources: Dict[str, set] = {}
    for i, c in enumerate(casts):
        sources.setdefault(_cast_id(c, i), set()).add(str(c["contract"].get("source") or ""))
    return sorted(cid for cid, s in sources.items() if len(s) > 1)


def _cast_ids(casts) -> List[str]:
    """Every cast's id, made unique within the file by suffixing repeats."""
    ids, seen = [], {}
    for i, c in enumerate(casts):
        cid = _cast_id(c, i)
        if cid in seen:
            seen[cid] += 1
            cid = f"{cid}_{seen[cid]:02d}"[:64]
        else:
            seen[cid] = 1
        ids.append(cid)
    return ids


def _dataset_codes(casts) -> List[str]:
    return [str(c["contract"].get("wod_dataset_code") or "unresolved") for c in casts]


def cast_year(cast: Dict[str, Any]) -> Optional[int]:
    """The year of a cast's date as it is written -- UTC once a stated numeric zone is
    applied -- which is the year WOD files the cast under. None when it has no date."""
    times = _cast_times([cast])
    return int(times["date"][0]) // 10000 if times["date_known"][0] else None


def cast_time(cast: Dict[str, Any]) -> float:
    """The cast's time as written (days since 1770), NaN when it has no date."""
    return float(_cast_times([cast])["time"][0])


def _shared_or_per_cast(ds, name: str, values: List[Optional[str]], long_name: str) -> None:
    """A value every cast shares is a global attribute; values that differ are a `casts`
    variable of the same name, so a file of several sources says which cast came from which.
    Nothing is written when no cast has one."""
    values = ["" if v is None else str(v) for v in values]
    if len(set(values)) == 1:
        if values[0]:
            ds.setncattr(name, values[0])
        return
    _per_cast_text(ds, name, values, long_name)


def _write_globals(ds, casts) -> None:
    ds.title = TITLE
    ds.Conventions = CF_CONVENTIONS
    ds.featureType = "profile"
    ds.WOD_version = WOD_VERSION
    # WOD keeps one dataset per file; a file that mixes them says so here, and the per-cast
    # `dataset` variable says which cast is which. It used to take the FIRST cast's dataset,
    # labelling a single-level OSD cast written beside CTD casts as CTD.
    codes = set(_dataset_codes(casts))
    ds.wod_dataset_type = codes.pop() if len(codes) == 1 else "mixed"
    # The file that was read -- a path, not the reader's description of it. That moved to
    # `source_format`; it used to fill this attribute with 'delimited_text, delimiter comma'.
    _shared_or_per_cast(ds, "source_file",
                        [str(c["contract"].get("source") or "unknown") for c in casts],
                        "the source file this cast was read from")
    _shared_or_per_cast(ds, "source_format",
                        [c["contract"].get("source_format") for c in casts],
                        "how this cast's source file was read")
    _shared_or_per_cast(ds, "source_files_raw",
                        ["; ".join(dict.fromkeys(c["contract"].get("source_files") or []))
                         for c in casts],
                        "the accession's own files this cast was staged from")
    _write_provenance(ds, casts)
    _write_character_data(ds, casts)
    # CF: institution is whose data this is, not what wrote the file. Secondary header 4
    # carries it when the originator stated it; the writer belongs in `source`.
    institution = _shared_secondary_header_value(casts, 4)
    if institution:
        ds.institution = institution
    ds.source = "generated by wod-agent-etl"

    _write_shared_secondary_headers(ds, casts)

    # Each cast's own conversions, each once: shared by every cast (one source's casts are)
    # it is the file's attribute, and per cast when two sources converted differently -- a
    # file of a thousand casts used to say it a thousand times, and of two sources the union.
    logs = [c.get("transform_log") or {} for c in casts]
    _shared_or_per_cast(ds, "unit_conversions",
                        ["; ".join(dict.fromkeys(
                            f"{t['column']}: {t.get('from', '?')} -> {t.get('to', '?')}"
                            for t in lg.get("transformations_applied", []))) for lg in logs],
                        "unit conversions applied to this cast's columns")
    _write_density(ds, logs)


def _write_density(ds, logs) -> None:
    """The seawater density a per-volume to per-mass conversion assumed, where one did: the
    file's attribute when every cast used the same one, per cast otherwise (fill for a cast
    that converted nothing with it)."""
    used = [(float(lg["seawater_density_kg_m3"]), str(lg.get("seawater_density_provenance")
                                                       or ""))
            if lg.get("seawater_density_kg_m3") else None for lg in logs]
    stated = {u for u in used if u is not None}
    if not stated:
        return
    if len(stated) == 1 and None not in used:
        density, provenance = stated.pop()
        ds.seawater_density_kg_m3 = density
        if provenance:
            ds.seawater_density_provenance = provenance
        return
    v = ds.createVariable("seawater_density_kg_m3", "f8", (CAST_DIM,),
                          fill_value=NETCDF_FILL_VALUE)
    v.units = "kg m-3"
    v.long_name = "seawater density this cast's per-volume values were converted with"
    v[:] = np.ma.masked_invalid(np.array([u[0] if u else np.nan for u in used], dtype=float))
    _per_cast_text(ds, "seawater_density_provenance", [u[1] if u else "" for u in used],
                   "where this cast's seawater density came from")


def _write_provenance(ds, casts) -> None:
    """The staged header's notes, as CF `comment`, and the run, as CF `history`.

    Every derivation and judgement a stager recorded -- a position taken from a station list,
    fluorescence written as chlorophyll, a date chosen between two sources -- lived in the
    staged CSV and nowhere else, so the NetCDF a reader actually receives said none of it.
    Notes every cast shares are global; notes that differ go to a per-cast variable.
    """
    import datetime as _dt

    per_cast = [list(c["contract"].get("provenance_notes") or []) for c in casts]
    shared = [n for n in per_cast[0] if all(n in other for other in per_cast[1:])]
    if shared:
        ds.comment = "\n".join(shared)
    own = [[n for n in notes if n not in shared] for notes in per_cast]
    if any(own):
        v = ds.createVariable("cast_notes", str, (CAST_DIM,))
        v.long_name = "originator and staging notes for this cast"
        for i, notes in enumerate(own):
            v[i] = "\n".join(notes)
    stamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    sources = list(dict.fromkeys(str(c["contract"].get("source") or "unknown") for c in casts))
    ds.history = (f"{stamp} wod-agent-etl: standardized to WOD intermediate NetCDF from "
                  + (sources[0] if len(sources) == 1 else
                     f"{len(sources)} source files (variable source_file)"))
    # Header keys with no WOD field, kept so they are not lost: those every cast shares in
    # the file's attributes, the rest per cast -- two sources' `title` are two titles.
    keys = [list(dict.fromkeys(f"{u.get('key')}={u.get('value')!r}"
                               for u in (c["contract"].get("unresolved_metadata") or [])))
            for c in casts]
    shared = [k for k in keys[0] if all(k in other for other in keys[1:])]
    if shared:
        ds.metadata_not_placed = (
            "header keys with no WOD field, kept here so they are not lost: "
            + "; ".join(shared[:40])
            + (f" (and {len(shared) - 40} more)" if len(shared) > 40 else ""))
    own = [[k for k in ks if k not in shared] for ks in keys]
    if any(own):
        _per_cast_text(ds, "cast_metadata_not_placed", ["; ".join(ks) for ks in own],
                       "this cast's header keys with no WOD field, kept so they are not lost")


def _write_character_data(ds, casts) -> None:
    """Character data (manual p.30): originator's cruise code and principal investigator."""
    fields = sorted({f for c in casts for f in (c["contract"].get("character_data") or {})})
    for field in fields:
        values = [str(((c["contract"].get("character_data") or {}).get(field) or {})
                      .get("value") or "") for c in casts]
        if len(set(values)) == 1:
            ds.setncattr(field, values[0])
        else:
            v = ds.createVariable(field, str, (CAST_DIM,))
            v.long_name = field.replace("_", " ")
            for i, x in enumerate(values):
                v[i] = x


def _cast_secondary_headers(cast) -> Dict[str, Any]:
    """One cast's Table 4a headers, from the full metadata map or the older key.

    `secondary_headers` comes from map_metadata and covers the header block AND metadata
    columns; `extracted_header_metadata` is the narrower set the original parser produced.
    Both are read so a contract built by either path still writes its metadata.
    """
    contract = cast["contract"]
    full = contract.get("secondary_headers") or {}
    legacy = contract.get("extracted_header_metadata") or {}
    return {**legacy, **full}


# Lookup outcomes under which a coded field's value IS a code and may be written as one.
_CODE_OK = ("matched", "already_a_code")


def split_coded_value(entry: Dict[str, Any]):
    """(value to write in the field, originator text to write beside it, lookup summary).

    A coded field holds a code. When the value was looked up and is not one -- ambiguous, no
    entry, list unavailable -- the field itself is left unwritten and the originator's text
    goes beside it with the reason. An entry that was never looked up (a contract built by
    hand) is written as it stands.
    """
    if not entry:
        return None, None, None
    lookup = entry.get("code_lookup")
    if lookup is None:
        return entry.get("value"), None, None
    if lookup.get("status") in _CODE_OK:
        return entry.get("value"), entry.get("originator_value"), (
            lookup.get("summary") if lookup.get("status") == "matched" else None)
    return None, entry.get("value"), lookup.get("summary")


def _secondary_header_columns(casts):
    """Every Table 4a header seen across the casts:
    (id, name, key, values, code_table, originator_texts, lookup_summaries), one per cast."""
    per_cast = [_cast_secondary_headers(c) for c in casts]
    sec_ids = sorted({k for m in per_cast for k in m},
                     key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)))
    for sec_id in sec_ids:
        name = next(m[sec_id].get("name", sec_id) for m in per_cast if sec_id in m)
        key = f"secondary_header_{sec_id}_{cf_safe_name(name)}"
        entry = next(m[sec_id] for m in per_cast if sec_id in m)
        split = [split_coded_value(m.get(sec_id) or {}) for m in per_cast]
        yield (sec_id, name, key, [x[0] for x in split], entry.get("code_table") or {},
               [x[1] for x in split], [x[2] for x in split])


def _is_shared(values) -> bool:
    return len({str(v) for v in values}) == 1 and values[0] is not None


def _shared_secondary_header_value(casts, sec_id: int):
    """One Table 4a header's value AS THE ORIGINATOR WROTE IT, when every cast agrees on it.

    For the human-readable CF attributes (`institution`): a header whose text was resolved
    to a code holds the code in `value` and the text in `originator_value`, and a CF
    `institution` of '1234' names nobody.
    """
    per_cast = [_cast_secondary_headers(c) for c in casts]
    entries = [m.get(str(sec_id), m.get(sec_id, {})) for m in per_cast]
    values = [e.get("originator_value") or e.get("value") for e in entries]
    return str(values[0]) if _is_shared(values) else None


def _write_shared_secondary_headers(ds, casts) -> None:
    """Only headers every cast agrees on may be file-global attributes.

    Bottom depth, station number and bottom hit are per-cast by definition. Writing the first
    cast's value as a global attribute silently relabels every other cast with it and drops
    any header the first cast happens not to carry -- so those go to `casts`-dimension
    variables instead, in _write_varying_secondary_headers. A single-cast file agrees with
    itself on everything and so keeps the all-global layout it has always had.
    """
    for sec_id, _name, key, values, code_table, texts, lookups in _secondary_header_columns(casts):
        url = code_table.get("csv") or code_table.get("html") or code_table.get("pdf")
        if _is_shared(texts):
            ds.setncattr(f"{key}_originator_text", str(texts[0]))
        if _is_shared(lookups):
            ds.setncattr(f"{key}_code_lookup", str(lookups[0]))
        if (_is_shared(texts) or _is_shared(lookups)) and url:
            ds.setncattr(f"{key}_code_table", url)
        if not _is_shared(values):
            continue
        ds.setncattr(key, str(values[0]))
        # A coded value is unusable without the list it is coded against, and WOD publishes
        # those online rather than in the manual. Carrying the URL means a reader of the
        # NetCDF alone can resolve the code.
        if url:
            ds.setncattr(f"{key}_code_table", url)


def _write_varying_secondary_headers(ds, casts) -> None:
    """Headers that differ between casts, written on the `casts` dimension."""
    for sec_id, name, key, values, code_table, texts, lookups in _secondary_header_columns(casts):
        url = code_table.get("csv") or code_table.get("html") or code_table.get("pdf")
        for suffix, series in (("_originator_text", texts), ("_code_lookup", lookups)):
            if any(x is not None for x in series) and not _is_shared(series):
                tv = ds.createVariable(f"{key}{suffix}", str, (CAST_DIM,))
                tv.long_name = (f"{name}: originator's text, not a code" if suffix ==
                                "_originator_text" else f"{name}: code lookup result")
                # The list the text was looked up in, as a shared header carries it.
                if url:
                    tv.code_table = url
                for i, x in enumerate(series):
                    tv[i] = "" if x is None else str(x)
        if _is_shared(values):
            continue
        if all(v is None for v in values) and any(t is not None for t in texts):
            continue                         # nothing coded to write; the text went beside it
        if all(v is None for v in values):
            # The header was identified but its value is not in the file -- header 96 when
            # flag columns are present but the flag SET is not named, for instance. A
            # variable of pure fill says nothing; an attribute says what is known.
            ds.setncattr(f"{key}_status",
                         "present in this file but its value is not stated in the source; "
                         "it must come from the submitter or the accession documentation")
            if url:
                ds.setncattr(f"{key}_code_table", url)
            continue
        if all(v is None or isinstance(v, (int, float)) for v in values):
            # The declared fill for a cast that does not state it, like every other numeric
            # variable here -- not NaN.
            v = ds.createVariable(key, "f8", (CAST_DIM,), fill_value=NETCDF_FILL_VALUE)
            v[:] = np.ma.masked_invalid(np.array([np.nan if x is None else float(x)
                                                  for x in values], dtype=float))
            if str(sec_id).isdigit() and int(sec_id) in _HEADER_UNITS:
                v.units = _HEADER_UNITS[int(sec_id)]
        else:
            v = ds.createVariable(key, str, (CAST_DIM,))
            for i, x in enumerate(values):
                v[i] = "" if x is None else str(x)
        v.long_name = str(name)
        v.wod_secondary_header_id = str(sec_id)
        if url:
            v.code_table = url


def _cast_var(ds, name, dtype, values, **attrs):
    v = ds.createVariable(name, dtype, (CAST_DIM,), fill_value=np.nan)
    for k, val in attrs.items():
        setattr(v, k, val)
    v[:] = np.array(values, dtype=float)
    return v


if __name__ == "__main__":
    import json

    df = pd.DataFrame({
        "Depth": [0.0, 10.0, 20.0],
        "Depth_qc_flag": [0, 0, 0],
        "Temp_C": [21.0, 19.5, 18.0],
        "Temp_C_qc_flag": [0, 0, 1],
        "DIC": [2100.0, 2105.0, 2110.0],
        "DIC_qc_flag": [0, 0, 0],
    })
    contract = {
        "source": "test_xctd.txt",
        "wod_dataset_code": "CTD",
        "parsed_coordinates": {"latitude": 61.93, "longitude": -172.27,
                               "year": 1985, "month": 6, "day": 12},
        "extracted_header_metadata": {"3": {"name": "Platform Code", "value": "STOCS85A"}},
        "column_mapping": [
            {"raw_column": "Depth", "wod_code": 0, "wod_name": "Depth", "target_unit": "meters"},
            {"raw_column": "Temp_C", "wod_code": 1, "wod_name": "Temperature",
             "target_unit": "Degrees Celsius"},
            {"raw_column": "DIC", "wod_code": 21, "wod_name": "Dissolved inorganic carbon",
             "target_unit": "Millimole liter-1"},
        ],
    }
    res = generate_netcdf_ragged(df, contract, "data/processed/demo_single.nc")
    print(json.dumps(res, indent=2))

    res2 = write_ragged_profiles(
        [{"df": df, "contract": contract}, {"df": df.iloc[:2], "contract": contract}],
        "data/processed/demo_multi.nc")
    print(json.dumps({k: res2[k] for k in ("cast_count", "total_observations", "row_sizes")}, indent=2))
