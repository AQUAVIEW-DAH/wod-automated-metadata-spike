"""WOD quality control auditing.

Implements the checks described in the WOD 2023 User's Manual pp.52-56 and writes the
official flag values from Table 12. Two separate flag scales are produced:

  <depth>_qc_flag  -- Table 12 (2)(a) Depth Flags
                      0 accepted, 1 duplicate/inversion, 2 density inversion
  <var>_qc_flag    -- Table 12 (2)(b) Observed Level Flags
                      0 accepted, 1 range, 2 inversion, 3 gradient, 4 zero-sensitivity,
                      5 gradient+inversion, 6 range+inversion, 7 range+gradient,
                      8 range+questionable, 9 range+gradient+inversion

The same integer means different things on the two scales; that is per the manual, not a
bug. Nothing is deleted -- flagged levels are excluded later at interpolation time.
"""
import json
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import pandas as pd

from wod_etl.config import GRADIENT_DEPTH_SPLIT_M, MIN_GRADIENT_DEPTH_DIFF_M
from wod_etl.reference import gradient_factors as _gradient_factors
from wod_etl.reference import range_levels as _range_levels
from wod_etl.reference import depth_flags, observed_level_flags
from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE

# Flag values are defined by Table 12 and loaded from knowledge/wod_code_tables/, not
# transcribed here. The names below are asserted against the loaded meanings at import, so
# a table edit that moves a flag fails loudly instead of silently mislabelling data.
FLAG_ACCEPTED = 0
FLAG_RANGE = 1
FLAG_INVERSION = 2
FLAG_GRADIENT = 3
FLAG_ZERO_SENSITIVITY = 4
FLAG_GRADIENT_INVERSION = 5
FLAG_RANGE_INVERSION = 6
FLAG_RANGE_GRADIENT = 7
FLAG_RANGE_GRADIENT_INVERSION = 9

# Manual p.37: surface-only data are "treated differently than profile data" -- each
# observation is an independent surface sample, so the vertical checks (depth duplication
# and inversion, excessive gradient, excessive inversion) describe a relationship that does
# not exist. Applied anyway, a track sitting at a constant 0 m flags every level as a
# duplicate depth and every variable as an inversion.
NON_PROFILE_DATASETS = {"SUR"}

# Manual p.49: "Air pressure, Julian year-day, latitude, and longitude, included as
# variables and parameters for the sole purpose of identifying the surface-only, APB and
# UOR data, were not quality controlled beyond basic data range checks."
RANGE_CHECK_ONLY_CODES = {29, 30, 31, 32}

DEPTH_FLAG_ACCEPTED = 0
DEPTH_FLAG_INVERSION = 1
DEPTH_FLAG_DENSITY_INVERSION = 2


def _verify_flag_constants() -> None:
    """Fail at import if the constants above drift from Table 12."""
    obs, dep = observed_level_flags(), depth_flags()
    expect = [
        (obs, FLAG_ACCEPTED, "accepted"), (obs, FLAG_RANGE, "range"),
        (obs, FLAG_INVERSION, "inversion"), (obs, FLAG_GRADIENT, "gradient"),
        (obs, FLAG_GRADIENT_INVERSION, "gradient"), (obs, FLAG_RANGE_INVERSION, "range"),
        (obs, FLAG_RANGE_GRADIENT, "range"), (obs, FLAG_RANGE_GRADIENT_INVERSION, "range"),
        (dep, DEPTH_FLAG_ACCEPTED, "accepted"), (dep, DEPTH_FLAG_INVERSION, "inversion"),
        (dep, DEPTH_FLAG_DENSITY_INVERSION, "density inversion"),
    ]
    for table, value, word in expect:
        meaning = table.get(value, "")
        if word not in meaning.lower():
            raise AssertionError(
                f"Table 12 disagrees with this module: flag {value} is {meaning!r}, "
                f"which does not mention {word!r}. Reconcile "
                f"knowledge/wod_code_tables/table_12_qc_flags.json with the constants "
                f"in apply_qc_auditing.py.")


_verify_flag_constants()


def range_at_depth(wod_code: int, depth_m: float) -> Optional[Tuple[float, float]]:
    """Acceptable (min, max) for a variable at a depth, using the manual's lookup rule.

    A depth between two listed levels takes the nearest listed level; if equidistant, the
    shallower one. The deepest listed level covers everything below it.
    """
    levels = _range_levels().get(wod_code)
    if not levels:
        return None
    if depth_m is None or not np.isfinite(depth_m):
        depth_m = 0.0
    if depth_m >= levels[-1][0]:
        return levels[-1][1], levels[-1][2]

    best = levels[0]
    best_dist = abs(depth_m - levels[0][0])
    for lv in levels[1:]:
        dist = abs(depth_m - lv[0])
        # Strictly less keeps the shallower level when distances tie.
        if dist < best_dist:
            best, best_dist = lv, dist
    return best[1], best[2]


def gradient_limits(wod_code: int, depth_m: float) -> Optional[Dict[str, float]]:
    """MGV/MIV/ZSI for a variable at a depth, honouring the 400 m split (manual p.54)."""
    f = _gradient_factors().get(wod_code)
    if not f:
        return None
    band = "deep_z_gt_400m" if (depth_m is not None and depth_m > GRADIENT_DEPTH_SPLIT_M) else "shallow_z_lt_400m"
    return {"MGV": f[band]["MGV"], "MIV": f[band]["MIV"], "ZSI": f["ZSI"]}


def apply_qc_auditing(
    df: pd.DataFrame,
    mapping_contract: Dict[str, Any],
    high_resolution: Optional[bool] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Run the WOD observed-level QC checks and append flag columns.

    high_resolution enables the manual's 3.0 m minimum depth difference when computing
    gradients. When None it is inferred from the dataset code (CTD and XBT are
    high-resolution instruments).
    """
    df_qc = df.copy()
    n = len(df_qc)
    summary: Dict[str, Any] = {
        "total_levels": n,
        "depth_flags": {},
        "flags_assigned": {},
        "checks_applied": [],
        "notes": [],
    }

    if high_resolution is None:
        high_resolution = mapping_contract.get("wod_dataset_code") in ("CTD", "XBT")
    min_dz = MIN_GRADIENT_DEPTH_DIFF_M if high_resolution else 0.0
    summary["high_resolution"] = bool(high_resolution)
    summary["min_gradient_depth_diff_m"] = min_dz

    dataset_code = mapping_contract.get("wod_dataset_code")
    vertical_checks = dataset_code not in NON_PROFILE_DATASETS
    summary["vertical_checks_applied"] = vertical_checks

    depth_col = _find_depth_column(df_qc, mapping_contract)
    depths = (pd.to_numeric(df_qc[depth_col], errors="coerce").to_numpy(dtype=float)
              if depth_col else np.full(n, np.nan))

    # ---- 1. Depth inversion and duplication (manual p.52) --------------------
    if vertical_checks:
        depth_flags = _depth_error_flags(depths)
    else:
        depth_flags = np.zeros(n, dtype=int)
        summary["notes"].append(
            f"{dataset_code} is not profile data (manual p.37): depth duplication, "
            f"inversion and gradient checks were not applied. Range checks were.")
    if depth_col:
        df_qc[f"{depth_col}_qc_flag"] = depth_flags
        summary["depth_flags"] = _counts(depth_flags)
        if vertical_checks:
            summary["checks_applied"].append("depth_inversion_duplication")
    elif vertical_checks:
        summary["notes"].append("No depth column: inversion, gradient and depth-dependent "
                                "range checks were skipped.")

    bad_depth = depth_flags != DEPTH_FLAG_ACCEPTED

    # ---- 2/3. Range, gradient and inversion checks per variable --------------
    for mapping in mapping_contract.get("column_mapping", []):
        code = mapping.get("wod_code")
        col = mapping.get("raw_column")
        if col not in df_qc.columns or code == DEPTH_SENTINEL_CODE or code is None:
            continue

        vals = pd.to_numeric(df_qc[col], errors="coerce").to_numpy(dtype=float)
        flags = np.zeros(n, dtype=int)
        has_range = code in _range_levels()
        # Range-only variables (p.49) and non-profile datasets (p.37) get no vertical check.
        use_vertical = vertical_checks and code not in RANGE_CHECK_ONLY_CODES
        has_grad = code in _gradient_factors() and use_vertical

        for i in range(n):
            v = vals[i]
            if not np.isfinite(v):
                continue

            z = depths[i] if np.isfinite(depths[i]) else None
            failed_range = False
            failed_grad = False
            failed_inv = False
            failed_zero = False

            if has_range:
                bounds = range_at_depth(code, z if z is not None else 0.0)
                if bounds and (v < bounds[0] or v > bounds[1]):
                    failed_range = True

            if has_grad and depth_col:
                lim = gradient_limits(code, z if z is not None else 0.0)
                # A level is checked against BOTH neighbours, so it can fail an excessive
                # decrease on one side and an excessive increase on the other. This is what
                # makes flag 5 (combined gradient and inversion) reachable.
                for j in (i - 1, i + 1):
                    if j < 0 or j >= n:
                        continue
                    g = _gradient(vals, depths, i, j, min_dz)
                    if g is None or lim is None:
                        continue
                    if g < 0 and abs(g) > lim["MGV"]:
                        failed_grad = True
                        if v == 0.0 and abs(g) > lim["MGV"] * lim["ZSI"]:
                            failed_zero = True
                    elif g > 0 and g > lim["MIV"]:
                        failed_inv = True

            # Levels sitting at a bad depth cannot be trusted vertically. The manual
            # excludes them from interpolation; we surface that as an inversion failure so
            # they are not silently accepted.
            if use_vertical and bad_depth[i]:
                failed_inv = True

            flags[i] = _combine(failed_range, failed_grad, failed_inv, failed_zero)

        df_qc[f"{col}_qc_flag"] = flags
        summary["flags_assigned"][col] = _counts(flags)

    if any(m.get("wod_code") in _range_levels() for m in mapping_contract.get("column_mapping", [])):
        summary["checks_applied"].append("broad_range_by_depth")
        if vertical_checks:
            summary["checks_applied"] += ["excessive_gradient_MGV",
                                          "excessive_inversion_MIV", "zero_sensitivity"]
    summary["notes"].append(
        "Range bounds are the union across all 30 ocean basins at each depth. Basin-specific "
        "bounds need range_area.msk from the WOD website and are not applied."
    )
    return df_qc, summary


def _combine(rng: bool, grad: bool, inv: bool, zero: bool) -> int:
    """Map failed checks onto Table 12 (2)(b) observed level flags."""
    if rng and grad and inv:
        return FLAG_RANGE_GRADIENT_INVERSION
    if rng and grad:
        return FLAG_RANGE_GRADIENT
    if rng and inv:
        return FLAG_RANGE_INVERSION
    if grad and inv:
        return FLAG_GRADIENT_INVERSION
    if zero:
        return FLAG_ZERO_SENSITIVITY
    if grad:
        return FLAG_GRADIENT
    if inv:
        return FLAG_INVERSION
    if rng:
        return FLAG_RANGE
    return FLAG_ACCEPTED


def _gradient(vals, depths, i: int, j: int, min_dz: float) -> Optional[float]:
    """Gradient between levels i and j, oriented as (deeper - shallower) / dz.

    Returns None when either value or depth is missing. For high-resolution instruments a
    minimum depth difference is enforced so a tiny dz cannot inflate the result (p.54).
    """
    if not (np.isfinite(vals[i]) and np.isfinite(vals[j])):
        return None
    if not (np.isfinite(depths[i]) and np.isfinite(depths[j])):
        return None
    shallow, deep = (i, j) if depths[i] <= depths[j] else (j, i)
    dz = depths[deep] - depths[shallow]
    if dz <= 0:
        return None
    dz = max(dz, min_dz)
    return (vals[deep] - vals[shallow]) / dz


def _depth_error_flags(depths: np.ndarray) -> np.ndarray:
    """Table 12 (2)(a) depth flags, following the manual's three rules (p.52).

    a) second of two successive depths is shallower  -> flag that depth
    b) three successive depths shallower than the first -> flag every subsequent depth
    c) two successive depths equal -> flag the second
    """
    n = len(depths)
    flags = np.zeros(n, dtype=int)
    if n < 2:
        return flags

    for i in range(1, n):
        if not (np.isfinite(depths[i]) and np.isfinite(depths[i - 1])):
            continue
        if depths[i] <= depths[i - 1]:
            flags[i] = DEPTH_FLAG_INVERSION

    # Rule (b): once three successive readings are shallower than an earlier one, every
    # following level is suspect -- this is the signature of two profiles concatenated
    # with no separating header.
    for i in range(n - 3):
        if not np.isfinite(depths[i]):
            continue
        window = depths[i + 1:i + 4]
        if np.all(np.isfinite(window)) and np.all(window < depths[i]):
            flags[i + 1:] = DEPTH_FLAG_INVERSION
            break
    return flags


def _find_depth_column(df: pd.DataFrame, mapping_contract: Dict[str, Any]) -> str:
    for mapping in mapping_contract.get("column_mapping", []):
        if mapping.get("wod_code") == DEPTH_SENTINEL_CODE:
            return mapping["raw_column"]
    for mapping in mapping_contract.get("column_mapping", []):
        if mapping.get("wod_code") == 25:  # pressure standing in for depth
            return mapping["raw_column"]
    for col in df.columns:
        if str(col).lower() in ("depth", "z", "pressure", "press"):
            return col
    return ""


def _counts(arr: np.ndarray) -> Dict[str, int]:
    vals, counts = np.unique(arr, return_counts=True)
    return {str(int(v)): int(c) for v, c in zip(vals, counts)}


def wod_col_is_depth(wod_code: int) -> bool:
    """Kept for callers written against the earlier API."""
    return wod_code == DEPTH_SENTINEL_CODE


if __name__ == "__main__":
    test_df = pd.DataFrame({
        "Depth":  [0.0, 10.0, 20.0, 15.0, 40.0, 50.0],
        "Temp_C": [20.0, 19.5, -10.0, 19.0, 50.0, 18.0],
    })
    contract = {
        "wod_dataset_code": "OSD",
        "column_mapping": [
            {"raw_column": "Depth", "wod_code": 0, "wod_name": "Depth"},
            {"raw_column": "Temp_C", "wod_code": 1, "wod_name": "Temperature"},
        ],
    }
    out, summary = apply_qc_auditing(test_df, contract)
    print(out.to_string(index=False))
    print()
    print(json.dumps(summary, indent=2))
