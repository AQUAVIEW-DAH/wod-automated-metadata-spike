"""Audit a generated NetCDF file against CF conventions and WOD intermediate-file rules.

The checks are deliberately capable of failing the files this project used to produce:
per-variable observation dimensions and non-CF variable names are both hard errors, not
warnings, because downstream tools cannot recover from either.
"""
import re
from typing import Dict, Any, List

import netCDF4 as nc
import numpy as np

CF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
OBS_DIM = "z_obs"
CAST_DIM = "casts"
REQUIRED_GLOBALS = ["Conventions", "featureType", "wod_dataset_type", "WOD_version"]


def validate_intermediate_netcdf(nc_file_path: str) -> Dict[str, Any]:
    """Return a pass/fail report with explicit error and warning lists."""
    errors: List[str] = []
    warnings: List[str] = []
    checks: List[str] = []

    try:
        with nc.Dataset(nc_file_path, "r") as ds:
            _check_globals(ds, errors, warnings, checks)
            _check_dimensions(ds, errors, checks)
            _check_names(ds, errors, checks)
            _check_coordinates(ds, errors, warnings, checks)
            _check_ragged(ds, errors, warnings, checks)
            _check_qc(ds, errors, warnings, checks)
    except OSError as e:
        return {"status": "FAIL", "file_path": nc_file_path, "passed": False,
                "fatal_error": f"Failed to open/read NetCDF file: {e}",
                "error_count": 1, "warning_count": 0, "errors": [str(e)], "warnings": []}

    passed = not errors
    return {
        "status": "PASS" if passed else "FAIL",
        "file_path": nc_file_path,
        "passed": passed,
        "checks_run": checks,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def _check_globals(ds, errors, warnings, checks):
    checks.append("global_attributes")
    for attr in REQUIRED_GLOBALS:
        if not hasattr(ds, attr):
            errors.append(f"Missing required global attribute: {attr}")
    ft = getattr(ds, "featureType", None)
    if ft is not None and ft != "profile":
        errors.append(f"featureType is '{ft}', expected 'profile'")
    conv = getattr(ds, "Conventions", "")
    if conv and not str(conv).upper().startswith("CF"):
        warnings.append(f"Conventions '{conv}' does not name CF")
    if getattr(ds, "wod_dataset_type", None) == "unresolved":
        warnings.append("wod_dataset_type is 'unresolved': the dataset could not be "
                        "determined, usually because the cast's depth increment was unknown")
    if getattr(ds, "wod_dataset_type", None) == "mixed":
        warnings.append("wod_dataset_type is 'mixed': the casts are of more than one WOD "
                        "dataset (the variable `dataset` says which). WOD keeps one dataset "
                        "per file")


def _check_dimensions(ds, errors, checks):
    checks.append("dimensions")
    for d in (CAST_DIM, OBS_DIM):
        if d not in ds.dimensions:
            errors.append(f"Missing required dimension: '{d}'")


def _check_names(ds, errors, checks):
    """CF section 2.3: names begin with a letter and use letters, digits, underscores."""
    checks.append("cf_variable_names")
    for name in ds.variables:
        if not CF_NAME.fullmatch(name):
            errors.append(
                f"Variable name '{name}' is not a valid CF/NetCDF identifier "
                f"(must start with a letter and contain only letters, digits, underscores)")
    for name in ds.dimensions:
        if not CF_NAME.fullmatch(name):
            errors.append(f"Dimension name '{name}' is not a valid CF identifier")


def _check_coordinates(ds, errors, warnings, checks):
    checks.append("coordinate_bounds")
    for name, lo, hi in (("lat", -90.0, 90.0), ("lon", -180.0, 180.0)):
        if name not in ds.variables:
            errors.append(f"Missing coordinate variable: '{name}'")
            continue
        vals = np.ma.filled(ds.variables[name][:], np.nan).astype(float)
        if np.all(np.isnan(vals)):
            errors.append(f"'{name}' is entirely missing: no usable position for any cast")
        elif np.any(np.isnan(vals)):
            warnings.append(f"'{name}' is missing for {int(np.isnan(vals).sum())} of {vals.size} cast(s)")
        finite = vals[np.isfinite(vals)]
        if finite.size and (np.any(finite < lo) or np.any(finite > hi)):
            errors.append(f"'{name}' has values outside [{lo}, {hi}]")

    if "z" not in ds.variables:
        errors.append("Missing depth variable: 'z'")
    else:
        z = np.ma.filled(ds.variables["z"][:], np.nan).astype(float)
        finite = z[np.isfinite(z)]
        # A depth axis of nothing but fill passes every structural check while making the
        # file useless: no measurement can be placed in the water column. This happens when
        # the depth column failed to map and no one looked at the values, so it is an error.
        if z.size and finite.size == 0:
            errors.append("'z' is entirely missing: the file has no usable depth axis. "
                          "The depth column probably did not map -- check the contract's "
                          "unmapped_columns and missing_required_coordinates.")
        elif np.any(np.isnan(z)):
            warnings.append(f"'z' is missing for {int(np.isnan(z).sum())} of {z.size} level(s)")
        if finite.size and np.any(finite < 0):
            warnings.append(f"'z' has {int((finite < 0).sum())} negative value(s); "
                            f"depth is positive down")
        if getattr(ds.variables["z"], "positive", None) != "down":
            errors.append("'z' must carry positive = 'down'")

    if "time" not in ds.variables:
        warnings.append("No 'time' variable")
    elif not hasattr(ds.variables["time"], "units"):
        errors.append("'time' has no units attribute")


def _check_ragged(ds, errors, warnings, checks):
    """The core DSG check: one shared observation dimension, counts that sum to it."""
    checks.append("ragged_array_structure")

    data_vars = [n for n, v in ds.variables.items()
                 if getattr(v, "wod_variable_code", None) is not None]
    if not data_vars:
        warnings.append("No measured variables found (none carry wod_variable_code)")

    # A variable whose every value is fill is not an error -- the source column existed and
    # was entirely unanalysed -- but it should never pass unremarked.
    for name in data_vars:
        vals = np.ma.filled(ds.variables[name][:], np.nan).astype(float)
        if vals.size and not np.isfinite(vals).any():
            warnings.append(f"'{name}' is entirely missing: the source column held no "
                            f"usable values")

    # Every measured variable and z must live on the SAME observation dimension.
    offenders = []
    for name in data_vars + (["z"] if "z" in ds.variables else []):
        dims = ds.variables[name].dimensions
        if dims != (OBS_DIM,):
            offenders.append(f"{name}{dims}")
    if offenders:
        errors.append(
            f"These variables are not on the shared observation dimension '{OBS_DIM}': "
            f"{', '.join(offenders)}. A contiguous ragged array requires depth and every "
            f"measurement to share one dimension so they are index-aligned.")

    stray = [d for d in ds.dimensions if d.endswith("_obs") and d != OBS_DIM]
    if stray:
        errors.append(f"Per-variable observation dimensions found: {', '.join(stray)}. "
                      f"There must be exactly one observation dimension ('{OBS_DIM}').")

    count_vars = [n for n in ds.variables if n.endswith("_row_size")]
    if "z_row_size" not in ds.variables:
        errors.append("Missing count variable 'z_row_size'")
    for cv in count_vars:
        v = ds.variables[cv]
        sd = getattr(v, "sample_dimension", None)
        if sd is None:
            errors.append(f"Count variable '{cv}' has no sample_dimension attribute")
        elif sd not in ds.dimensions:
            errors.append(f"Count variable '{cv}' names sample_dimension '{sd}', which does not exist")
        else:
            total = int(np.sum(np.ma.filled(v[:], 0)))
            actual = len(ds.dimensions[sd])
            if total != actual:
                errors.append(f"Row size mismatch for '{cv}': sum({total}) != len({sd})={actual}")
        if v.dimensions != (CAST_DIM,):
            errors.append(f"Count variable '{cv}' must be on ('{CAST_DIM}',), found {v.dimensions}")


def _check_qc(ds, errors, warnings, checks):
    checks.append("qc_flags")
    for name, v in ds.variables.items():
        if getattr(v, "wod_variable_code", None) is None:
            continue
        qc = f"{name}_qc"
        if qc not in ds.variables:
            # No WOD flags is normal: QC is off by default and applied downstream. Say which
            # of the two situations this is, because "no flags at all" and "the originator's
            # flags only" are different things to a reader.
            if f"{name}_qc_originator" in ds.variables:
                warnings.append(
                    f"Measured variable '{name}' carries the originator's flags "
                    f"('{name}_qc_originator') and no WOD Table 12 flags. Expected when WOD "
                    f"QC is off; the two scales are not interchangeable.")
            else:
                warnings.append(f"Measured variable '{name}' has no quality flags at all: "
                                f"neither WOD '{qc}' nor originator flags")
            continue
        q = ds.variables[qc]
        if q.dimensions != v.dimensions:
            errors.append(f"QC array '{qc}' dims {q.dimensions} do not match '{name}' {v.dimensions}")
        if not hasattr(q, "flag_values") or not hasattr(q, "flag_meanings"):
            warnings.append(f"QC array '{qc}' lacks flag_values/flag_meanings")
        vals = np.ma.filled(q[:], 0).astype(int)
        bad = vals[(vals < 0) | (vals > 9)]
        if bad.size:
            errors.append(f"QC array '{qc}' has {bad.size} value(s) outside the 0-9 flag range")


if __name__ == "__main__":
    import json
    for p in ("data/processed/demo_single.nc", "data/processed/demo_multi.nc"):
        r = validate_intermediate_netcdf(p)
        print(f"{p}: {r['status']}  errors={r['error_count']} warnings={r['warning_count']}")
        for e in r["errors"]:
            print("   ERROR:", e)
        for w in r["warnings"]:
            print("   warn :", w)
