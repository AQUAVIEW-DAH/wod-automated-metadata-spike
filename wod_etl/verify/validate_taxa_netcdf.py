"""Check a Taxa-Record file (plankton, biomass): structure, depth ranges, and what a reader
would miss. The counterpart of `validate_intermediate_netcdf` for `write_taxa_netcdf` output.

Errors make the file not output. Warnings are what a report must say: taxa that did not
resolve to a WOD variable number, and Biological Header fields without which a count per
sample cannot be turned into a concentration.
"""
from typing import Any, Dict

import numpy as np

from wod_etl.reference import missing_biological_fields

_MEASUREMENTS = ("t_4_", "t_10_", "t_11_", "t_12_", "t_13_", "t_14_", "t_15_", "t_25_")
_PER_UNIT = ("t_10_", "t_11_", "t_12_", "t_13_", "t_14_", "t_15_", "t_25_")


def validate_taxa_netcdf(path: str) -> Dict[str, Any]:
    import netCDF4 as nc

    errors, warnings = [], []
    try:
        ds = nc.Dataset(path)
    except Exception as e:                              # noqa: BLE001 - reported
        return {"status": "FAIL", "passed": False, "errors": [f"unreadable: {e}"],
                "warnings": []}
    with ds:
        v = ds.variables
        for name in ("lat", "lon", "time", "cast_id", "taxa_row_size", "t_1_variable_number",
                     "taxon_name_originator"):
            if name not in v:
                errors.append(f"missing required variable {name!r}")
        if errors:
            return _result(errors, warnings)

        n_obs = len(ds.dimensions["taxa_obs"])
        if int(np.sum(v["taxa_row_size"][:])) != n_obs:
            errors.append("taxa_row_size does not sum to the taxa_obs dimension")
        for coord in ("lat", "lon"):
            if np.all(~np.isfinite(np.ma.filled(v[coord][:], np.nan))):
                errors.append(f"{coord!r} is entirely missing")

        names = list(v)
        upper = _get(v, "t_2_")
        lower = _get(v, "t_3_")
        if upper is None or lower is None:
            errors.append("every Taxa-Record needs an upper and a lower depth (Table 7a 2 and "
                          "3); at least one is absent")
        else:
            both = np.isfinite(upper) & np.isfinite(lower)
            if (~both).any():
                errors.append(f"{int((~both).sum())} record(s) have no depth range")
            if (upper[both] > lower[both]).any():
                errors.append(f"{int((upper[both] > lower[both]).sum())} record(s) have an "
                              f"upper depth below their lower depth")
            if (upper[both] < 0).any() or (lower[both] < 0).any():
                errors.append("negative depth in a depth range")

        measured = [n for n in names if n.startswith(_MEASUREMENTS) and not n.endswith("_text")]
        if not measured:
            errors.append("no measurement field: a Taxa-Record needs a count, biomass, volume "
                          "or weight")
        else:
            stack = np.vstack([np.ma.filled(v[n][:].astype(float), np.nan) for n in measured])
            empty = ~np.isfinite(stack).any(axis=0)
            if empty.any():
                errors.append(f"{int(empty.sum())} record(s) carry no measurement")
            if (stack[np.isfinite(stack)] < 0).any() and "t_10_taxon_count" in measured:
                counts = np.ma.filled(v["t_10_taxon_count"][:].astype(float), np.nan)
                if (counts[np.isfinite(counts)] < 0).any():
                    errors.append("negative taxon count")
        if any(n.startswith(_PER_UNIT) for n in measured) and not _get_any(v, "t_20_"):
            errors.append("a 'per UNIT' field is present but no originator's unit (Table 7a "
                          "field 20) says what UNIT is")

        codes = np.ma.filled(v["t_1_variable_number"][:], -2147483647)
        unresolved = int((codes == -2147483647).sum())
        if unresolved:
            warnings.append(f"{unresolved} of {n_obs} record(s) have no WOD variable number: "
                            f"the taxon name did not resolve. The originator's name is kept.")
        stated = [int(n.split("_")[1]) for n in names
                  if n.startswith("b_") and n.split("_")[1].isdigit()]
        missing = missing_biological_fields(stated)
        if missing:
            warnings.append("Biological Header fields not stated: " + ", ".join(missing)
                            + ". Without volume filtered or tow distance, counts stay per "
                              "sample and cannot become concentrations.")
    return _result(errors, warnings, missing_biological_fields=missing if not errors else None)


def _get(v, prefix):
    name = next((n for n in v if n.startswith(prefix) and not n.endswith("_text")), None)
    return None if name is None else np.ma.filled(v[name][:].astype(float), np.nan)


def _get_any(v, prefix):
    return next((n for n in v if n.startswith(prefix)), None)


def _result(errors, warnings, **extra) -> Dict[str, Any]:
    return {"status": "FAIL" if errors else "PASS", "passed": not errors,
            "errors": errors, "warnings": warnings, **extra}
