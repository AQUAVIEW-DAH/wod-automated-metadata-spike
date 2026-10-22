"""Deterministic unit and type conversions driven by the Phase 2 mapping contract.

Each conversion is logged with its constant and source, for the NetCDF metadata.
"""
import json
from typing import Dict, Any, Tuple, List

import numpy as np
import pandas as pd

from wod_etl.config import (
    ASSUMED_SEAWATER_DENSITY_KG_M3,
    CODE_TABLES_DIR,
    SEAWATER_DENSITY_PROVENANCE,
)
from wod_etl.mapping.generate_field_mapping import MOLAR_PREFIX_SCALE
from wod_etl.mapping.query_wod_spec import PER_VOLUME_CODES

FEET_TO_METERS = 0.3048
FATHOMS_TO_METERS = 1.8288


def _molar_volume_o2() -> float:
    """Molar volume of O2 at STP, litres per mole (Table 3b, p.19)."""
    data = json.loads((CODE_TABLES_DIR / "table_3b_unit_constants.json").read_text())
    return data["molar_volume_o2_stp_l_per_mole"]


def _molar_mass_o2() -> float:
    """Molar mass of O2, grams per mole, from the Table 3b atomic weights (p.19)."""
    data = json.loads((CODE_TABLES_DIR / "table_3b_unit_constants.json").read_text())
    return 2.0 * data["atomic_weights_g_per_mole"]["oxygen"]


def _molar_factor(source: str, target: str, density_kg_m3: float):
    """Multiplier taking a molar concentration from `source` to `target`, or None.

    Units are written 'umol/kg', 'mmol/l'. A basis change (l <-> kg) applies seawater density.
    """
    if not source or not target:
        return None
    try:
        src_prefix, src_basis = source.split("/")
        tgt_prefix, tgt_basis = target.split("/")
        factor = MOLAR_PREFIX_SCALE[src_prefix] / MOLAR_PREFIX_SCALE[tgt_prefix]
    except (ValueError, KeyError):
        return None

    kg_per_litre = density_kg_m3 / 1000.0
    if src_basis == "l" and tgt_basis == "kg":
        factor /= kg_per_litre
    elif src_basis == "kg" and tgt_basis == "l":
        factor *= kg_per_litre
    elif src_basis != tgt_basis:
        return None
    return factor


def convert_units_and_types(
    df: pd.DataFrame,
    mapping_contract: Dict[str, Any],
    seawater_density_kg_m3: float = ASSUMED_SEAWATER_DENSITY_KG_M3,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Apply the conversions named in the contract and coerce mapped columns to numeric."""
    df_clean = df.copy()
    log: List[Dict[str, Any]] = []
    warnings: List[str] = []
    density_used = False

    for mapping in mapping_contract.get("column_mapping", []):
        col = mapping.get("raw_column")
        conversion = mapping.get("conversion_needed", "none")
        code = mapping.get("wod_code")

        if col not in df_clean.columns:
            warnings.append(f"Contract references column '{col}' which is not in the data.")
            continue

        before = df_clean[col]
        df_clean[col] = pd.to_numeric(before, errors="coerce")
        coerced = int((~pd.isna(before) & pd.isna(df_clean[col])).sum())
        if coerced:
            warnings.append(f"{coerced} value(s) in '{col}' could not be parsed as numeric "
                            f"and became NaN.")

        if conversion in (None, "none"):
            continue

        entry = {"column": col, "wod_code": code, "conversion": conversion}

        if conversion == "molar_rescale":
            # SI-prefix rescale, plus a seawater-density factor if the basis changes.
            src, tgt = mapping.get("conversion_from"), mapping.get("conversion_to")
            factor = _molar_factor(src, tgt, seawater_density_kg_m3)
            if factor is None:
                warnings.append(
                    f"Cannot rescale '{col}' from {src!r} to {tgt!r}: the contract did not "
                    f"name both endpoints as molar units. Left as is.")
                continue
            df_clean[col] = df_clean[col] * factor
            entry.update({"from": src, "to": tgt, "constant": factor,
                          "provenance": "exact decimal scaling" if src.split("/")[1] == tgt.split("/")[1]
                          else "exact decimal scaling; basis change via " + SEAWATER_DENSITY_PROVENANCE})
            density_used = density_used or src.split("/")[1] != tgt.split("/")[1]

        elif conversion == "ml_per_l_to_umol_per_kg":
            # ml/l -> umol/l via the molar volume of O2 at STP (Table 3b), then -> umol/kg.
            mv = _molar_volume_o2()
            umol_per_l = df_clean[col] * (1000.0 / mv)
            df_clean[col] = umol_per_l / (seawater_density_kg_m3 / 1000.0)
            entry.update({"from": "ml/l", "to": "umol/kg",
                          "constant": mv, "constant_units": "l/mol",
                          "provenance": "molar volume of O2 at STP, WOD Table 3b p.19 "
                                        "(Garcia and Gordon, 1992); then " + SEAWATER_DENSITY_PROVENANCE})
            density_used = True

        elif conversion == "mg_per_l_to_umol_per_kg":
            # mg/l -> umol/l via the molar mass of O2 (Table 3b), then -> umol/kg.
            mm = _molar_mass_o2()
            umol_per_l = df_clean[col] * (1000.0 / mm)
            df_clean[col] = umol_per_l / (seawater_density_kg_m3 / 1000.0)
            entry.update({"from": "mg/l", "to": "umol/kg",
                          "constant": mm, "constant_units": "g/mol",
                          "provenance": "molar mass of O2 from WOD Table 3b atomic weights "
                                        "p.19 (Lide, 1992); then " + SEAWATER_DENSITY_PROVENANCE})
            density_used = True

        elif conversion == "feet_to_meters":
            df_clean[col] = df_clean[col] * FEET_TO_METERS
            entry.update({"from": "feet", "to": "meters", "constant": FEET_TO_METERS,
                          "provenance": "exact definition"})

        elif conversion == "fathoms_to_meters":
            df_clean[col] = df_clean[col] * FATHOMS_TO_METERS
            entry.update({"from": "fathoms", "to": "meters", "constant": FATHOMS_TO_METERS,
                          "provenance": "exact definition"})

        elif conversion == "fahrenheit_to_celsius":
            df_clean[col] = (df_clean[col] - 32.0) * (5.0 / 9.0)
            entry.update({"from": "degrees Fahrenheit", "to": "degrees Celsius",
                          "provenance": "exact definition"})

        else:
            warnings.append(f"Unknown conversion '{conversion}' for column '{col}'; left as is.")
            continue

        log.append(entry)

    df_clean = _attach_time(df_clean, mapping_contract, warnings)

    return df_clean, {
        "transformations_applied": log,
        "warnings": warnings,
        "seawater_density_kg_m3": seawater_density_kg_m3 if density_used else None,
        "seawater_density_provenance": SEAWATER_DENSITY_PROVENANCE if density_used else None,
    }


def _attach_time(df: pd.DataFrame, contract: Dict[str, Any], warnings: List[str]) -> pd.DataFrame:
    """Add an iso_date column when the contract carries a usable date."""
    c = contract.get("parsed_coordinates", {}) or {}
    if not all(k in c for k in ("year", "month", "day")):
        return df
    try:
        ts = pd.Timestamp(
            year=int(c["year"]), month=int(c["month"]), day=int(c["day"]),
            hour=int(c.get("hour", 0)), minute=int(c.get("minute", 0)),
        ) + pd.Timedelta(seconds=float(c.get("second") or 0))
    except (ValueError, TypeError) as e:
        warnings.append(f"Could not build a timestamp from {c}: {e}")
        return df
    df["iso_date"] = ts
    return df
