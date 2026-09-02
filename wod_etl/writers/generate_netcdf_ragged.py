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
from typing import Dict, Any, List, Optional, Tuple

import netCDF4 as nc
import numpy as np
import pandas as pd

from wod_etl.config import CF_CONVENTIONS, NETCDF_FILL_VALUE, QC_FILL_VALUE, WOD_VERSION
from wod_etl.reference.load_tables import flag_meanings_attr, flag_values_attr
from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE

OBS_DIM = "z_obs"
CAST_DIM = "casts"

# CF flag attributes are generated from Table 12 rather than transcribed, so the file's
# flag_meanings can never disagree with the flags the QC phase actually assigned.
_OBS_FLAG_VALUES = flag_values_attr("observed_level")
_OBS_FLAG_MEANINGS = flag_meanings_attr("observed_level")
_DEPTH_FLAG_VALUES = flag_values_attr("depth")
_DEPTH_FLAG_MEANINGS = flag_meanings_attr("depth")


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
        _write_globals(ds, casts)

        ds.createDimension(CAST_DIM, n_casts)
        ds.createDimension(OBS_DIM, total_obs)

        # ---- instance (per-cast) variables ---------------------------------
        _cast_var(ds, "lat", "f8", [_coord(c, "latitude") for c in casts],
                  units="degrees_north", standard_name="latitude", long_name="cast latitude")
        _cast_var(ds, "lon", "f8", [_coord(c, "longitude") for c in casts],
                  units="degrees_east", standard_name="longitude", long_name="cast longitude")

        times, has_time = _cast_times(casts)
        tv = ds.createVariable("time", "f8", (CAST_DIM,), fill_value=np.nan)
        tv.units = "days since 1770-01-01 00:00:00"
        tv.standard_name = "time"
        tv.long_name = "cast time"
        tv.calendar = "gregorian"
        tv[:] = times
        if not has_time:
            tv.comment = "no usable date in the source header; values are fill"

        # NETCDF4 variable-length strings; "S1" would truncate each id to one character.
        idv = ds.createVariable("cast_id", str, (CAST_DIM,))
        idv.cf_role = "profile_id"
        idv.long_name = "cast identifier"
        for i, c in enumerate(casts):
            idv[i] = _cast_id(c, i)

        rs = ds.createVariable("z_row_size", "i4", (CAST_DIM,))
        rs.long_name = "number of observations for this cast"
        rs.sample_dimension = OBS_DIM
        rs[:] = np.array(row_sizes, dtype="i4")

        _write_varying_secondary_headers(ds, casts)

        # ---- observation (per-level) variables -----------------------------
        zv = ds.createVariable("z", "f4", (OBS_DIM,), fill_value=NETCDF_FILL_VALUE)
        zv.units = "meters"
        zv.positive = "down"
        zv.axis = "Z"
        zv.standard_name = "depth"
        zv.long_name = "depth below sea surface"
        zv[:] = _concat_depth(casts, total_obs)

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
            v.units = var["units"]
            v.long_name = var["wod_name"]
            v.wod_variable_code = np.int32(var["wod_code"])
            v.coordinates = "time lat lon z"
            if var.get("unit_abbrev"):
                v.unit_abbrev = var["unit_abbrev"]
            v[:] = data

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
            orig, orig_source = _concat_originator_flags(casts, total_obs, var)
            if orig is not None:
                oname = f"{name}_qc_originator"
                o = ds.createVariable(oname, "f4", (OBS_DIM,), fill_value=NETCDF_FILL_VALUE)
                o.long_name = (f"originator's quality flag for {var['wod_name']}, "
                               f"as submitted")
                o.comment = ("Values are the ORIGINATOR's, on the originator's scale. They "
                             "are not WOD Table 12 flags and must not be read as such. WOD "
                             "records which flag set was used as secondary header 96.")
                o.source_column = orig_source
                o.wod_secondary_header = np.int32(96)
                o.code_table = (
                    "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/"
                    "s_96_origflagset.html")
                o[:] = orig
                ancillary.append(oname)

            if ancillary:
                v.ancillary_variables = " ".join(ancillary)

            # setncattr, not setattr: netCDF4 reserves a set of Python attribute names on
            # Variable ('scale', 'mask', 'name', 'dtype'...) and rebinding one raises.
            for attr, value in _variable_metadata(casts, var).items():
                v.setncattr(attr, value)

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
    """The originator's flag column for one variable, concatenated across casts.

    Kept as float, not int: an originator flag is not always an integer -- some submitters
    use letters or decimals -- and coercing to int would silently rewrite them.
    """
    out = np.full(total, np.nan, dtype=float)
    at, found, source = 0, False, None
    for c in casts:
        n = len(c["df"])
        mapping = next((m for m in c["contract"].get("column_mapping", [])
                        if m.get("wod_code") == var["wod_code"]), None)
        fcol = (mapping or {}).get("originator_flag_column")
        if fcol and fcol in c["df"].columns:
            out[at:at + n] = pd.to_numeric(c["df"][fcol], errors="coerce").to_numpy(dtype=float)
            found = True
            source = source or fcol
        at += n
    return (out, source) if found else (None, None)


def _variable_metadata(casts, var) -> Dict[str, Any]:
    """Table 5a variable specific headers for one variable, as NetCDF attributes.

    The originator's unit and the measurement scale are the two that always matter: a
    conversion is only auditable against the unit the source declared, and a temperature is
    ambiguous without knowing whether it is on ITS-90 or IPTS-68.
    """
    attrs: Dict[str, Any] = {}
    for c in casts:
        mapping = next((m for m in c["contract"].get("column_mapping", [])
                        if m.get("wod_code") == var["wod_code"]), None)
        if not mapping:
            continue
        if mapping.get("originator_unit") and "originator_units" not in attrs:
            attrs["originator_units"] = str(mapping["originator_unit"])
            attrs["wod_variable_specific_header_8"] = str(mapping["originator_unit"])
        if mapping.get("originator_scale") and "measurement_scale" not in attrs:
            attrs["measurement_scale"] = str(mapping["originator_scale"])
            attrs["wod_variable_specific_header_3"] = str(mapping["originator_scale"])
        for vid, header in (mapping.get("variable_specific_headers") or {}).items():
            key = f"wod_variable_specific_header_{vid}"
            if key not in attrs and header.get("value") is not None:
                attrs[key] = (str(header["value"]) if not isinstance(header["value"], (int, float))
                              else header["value"])
        if mapping.get("raw_column") and "originator_column_name" not in attrs:
            attrs["originator_column_name"] = str(mapping["raw_column"])
    return attrs


def _coord(cast, key) -> float:
    v = cast["contract"].get("parsed_coordinates", {}).get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def _cast_times(casts) -> Tuple[np.ndarray, bool]:
    epoch = pd.Timestamp("1770-01-01")
    out, any_time = [], False
    for c in casts:
        pc = c["contract"].get("parsed_coordinates", {})
        try:
            ts = pd.Timestamp(year=int(pc["year"]), month=int(pc["month"]), day=int(pc["day"]),
                              hour=int(pc.get("hour", 0)), minute=int(pc.get("minute", 0)))
            out.append((ts - epoch).total_seconds() / 86400.0)
            any_time = True
        except (KeyError, TypeError, ValueError):
            out.append(np.nan)
    return np.array(out, dtype=float), any_time


def _cast_id(cast, index) -> str:
    meta = _cast_secondary_headers(cast)
    platform = str(meta.get("3", {}).get("value") or "") or "unknown"
    return f"{platform}_{index:04d}"[:64]


def _write_globals(ds, casts) -> None:
    first = casts[0]["contract"]
    ds.title = "WOD Intermediate Standardized Ocean Profile"
    ds.Conventions = CF_CONVENTIONS
    ds.featureType = "profile"
    ds.WOD_version = WOD_VERSION
    ds.wod_dataset_type = str(first.get("wod_dataset_code") or "unresolved")
    ds.source_file = str(first.get("source", "unknown"))
    ds.institution = "generated by wod-agent-etl"
    ds.references = "World Ocean Database 2023 User's Manual"

    _write_shared_secondary_headers(ds, casts)

    logs = [c.get("transform_log") for c in casts if c.get("transform_log")]
    applied = [t for lg in logs for t in lg.get("transformations_applied", [])]
    if applied:
        ds.unit_conversions = "; ".join(
            f"{t['column']}: {t.get('from','?')} -> {t.get('to','?')}" for t in applied)
    density = next((lg.get("seawater_density_kg_m3") for lg in logs
                    if lg.get("seawater_density_kg_m3")), None)
    if density:
        ds.seawater_density_kg_m3 = float(density)
        ds.seawater_density_provenance = next(
            lg.get("seawater_density_provenance") for lg in logs
            if lg.get("seawater_density_kg_m3"))


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


def _secondary_header_columns(casts):
    """Every Table 4a header seen across the casts, as (id, name, key, values-per-cast)."""
    per_cast = [_cast_secondary_headers(c) for c in casts]
    sec_ids = sorted({k for m in per_cast for k in m},
                     key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)))
    for sec_id in sec_ids:
        name = next(m[sec_id].get("name", sec_id) for m in per_cast if sec_id in m)
        key = f"secondary_header_{sec_id}_{cf_safe_name(name)}"
        entry = next(m[sec_id] for m in per_cast if sec_id in m)
        yield (sec_id, name, key, [m.get(sec_id, {}).get("value") for m in per_cast],
               entry.get("code_table") or {})


def _is_shared(values) -> bool:
    return len({str(v) for v in values}) == 1 and values[0] is not None


def _write_shared_secondary_headers(ds, casts) -> None:
    """Only headers every cast agrees on may be file-global attributes.

    Bottom depth, station number and bottom hit are per-cast by definition. Writing the first
    cast's value as a global attribute silently relabels every other cast with it and drops
    any header the first cast happens not to carry -- so those go to `casts`-dimension
    variables instead, in _write_varying_secondary_headers. A single-cast file agrees with
    itself on everything and so keeps the all-global layout it has always had.
    """
    for sec_id, _name, key, values, code_table in _secondary_header_columns(casts):
        if not _is_shared(values):
            continue
        ds.setncattr(key, str(values[0]))
        # A coded value is unusable without the list it is coded against, and WOD publishes
        # those online rather than in the manual. Carrying the URL means a reader of the
        # NetCDF alone can resolve the code.
        url = code_table.get("csv") or code_table.get("html") or code_table.get("pdf")
        if url:
            ds.setncattr(f"{key}_code_table", url)


def _write_varying_secondary_headers(ds, casts) -> None:
    """Headers that differ between casts, written on the `casts` dimension."""
    for sec_id, name, key, values, code_table in _secondary_header_columns(casts):
        if _is_shared(values):
            continue
        if all(v is None for v in values):
            # The header was identified but its value is not in the file -- header 96 when
            # flag columns are present but the flag SET is not named, for instance. A
            # variable of pure fill says nothing; an attribute says what is known.
            ds.setncattr(f"{key}_status",
                         "present in this file but its value is not stated in the source; "
                         "it must come from the submitter or the accession documentation")
            url = code_table.get("csv") or code_table.get("html") or code_table.get("pdf")
            if url:
                ds.setncattr(f"{key}_code_table", url)
            continue
        if all(v is None or isinstance(v, (int, float)) for v in values):
            v = ds.createVariable(key, "f8", (CAST_DIM,), fill_value=np.nan)
            v[:] = np.array([np.nan if x is None else float(x) for x in values], dtype=float)
        else:
            v = ds.createVariable(key, str, (CAST_DIM,))
            for i, x in enumerate(values):
                v[i] = "" if x is None else str(x)
        v.long_name = str(name)
        v.wod_secondary_header_id = str(sec_id)
        url = code_table.get("csv") or code_table.get("html") or code_table.get("pdf")
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
