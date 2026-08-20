"""WOD standard specifications, loaded from the reference tables in knowledge/wod_code_tables/.

The tables are transcribed from the World Ocean Database 2023 User's Manual with page
citations. Nothing here is hardcoded from memory -- if a value looks wrong, check the JSON
against the manual rather than editing this module.

  Table 2  (p.17) -- dataset names          -> table_2_datasets.json
  Table 3a (p.18) -- measured variables     -> table_3a_variables.json
  Table 4a (p.23) -- secondary header codes -> table_4a_secondary_headers.json
"""
import re
from typing import Dict, Any, List, Optional

from wod_etl.config import HIGH_RES_DEPTH_INCREMENT_M
from wod_etl.reference import wod_datasets, wod_secondary_headers, wod_variables

# Aliases are OUR matching hints, not part of the WOD standard. They are deliberately kept
# separate from the manual-derived tables above so the two never get confused.
VARIABLE_ALIASES = {
    1: ["temperature", "temp", "t", "temp_c", "t_deg_c", "sst", "water_temperature", "potential_temperature",
        "t090c", "t190c", "t068c", "t168c", "tv290c", "t090", "t190"],
    2: ["salinity", "sal", "psal", "psu", "practical_salinity", "sal00", "sal11", "psal00", "psal11"],
    3: ["oxygen", "o2", "doxy", "dissolved_oxygen", "do",
        "sbeox0ml_l", "sbeox1ml_l", "sbeox0mm_kg", "sbeox1mm_kg", "sbox0ml_l", "sbox1ml_l"],
    4: ["phosphate", "po4", "phos"],
    6: ["silicate", "sio3", "sio4", "sio2", "silicic_acid", "si"],
    8: ["nitrate", "no3", "nitrate_nitrite", "no2_no3", "nitrate_and_nitrite"],
    9: ["ph"],
    11: ["chlorophyll", "chl", "chla", "chl_a", "chlorophyll_a",
         "flsp", "flspp", "flecoafl", "flc", "fleco_afl", "chl_fluorescence"],
    17: ["alkalinity", "alk", "talk", "ta", "total_alkalinity"],
    20: ["pco2", "xco2", "partial_pressure_co2"],
    21: ["dic", "tco2", "dissolved_inorganic_carbon"],
    24: ["transmissivity", "beam_attenuation", "transmission"],
    25: ["pressure", "press", "pres", "dbar", "prdm", "prsm", "prdb", "prm"],
    26: ["air_temperature", "airtemp", "atemp"],
    29: ["air_pressure", "barometric_pressure", "baro"],
    30: ["latitude", "lat"],
    31: ["longitude", "lon", "long"],
    32: ["julian_day", "yearday", "year_day", "jday"],
    33: ["tritium"],
    34: ["helium", "he"],
    35: ["helium_3", "he3"],
    36: ["carbon_14", "c14"],
    37: ["carbon_13", "c13"],
    38: ["argon", "ar"],
    39: ["neon", "ne"],
    40: ["cfc_11", "cfc11", "chlorofluorocarbon_11"],
    41: ["cfc_12", "cfc12", "chlorofluorocarbon_12"],
    42: ["cfc_113", "cfc113", "chlorofluorocarbon_113"],
    43: ["oxygen_18", "o18", "18o", "d18o", "delta_18o", "delta_o18"],
}

# Depth is the vertical coordinate, not a WOD measured variable. Code 25 is Pressure, a
# distinct variable. We use a sentinel of 0 internally to mean "this column is the depth
# axis in meters".
DEPTH_SENTINEL_CODE = 0
DEPTH_ALIASES = ["depth", "dep", "z", "deph", "depth_m", "depth_meters",
                 # Sea-Bird export names: depth in salt water / fresh water, metres or feet.
                 "depsm", "depfm", "depsf", "depff", "dpthm"]

# Standard units that are per-VOLUME, not per-mass (Table 3a). A molar-to-mass conversion
# must never be applied to these.
PER_VOLUME_CODES = {11, 17, 21}


def normalize_name(name: str) -> str:
    """Reduce a raw column name to a comparable token.

    'Temp (C)' -> 'temp', 'Sal_PSU [psu]' -> 'sal_psu', 'Depth, m' -> 'depth'
    Unit suffixes in brackets or after a comma are stripped; the remainder is lowercased
    and reduced to underscore-separated words.
    """
    s = str(name).strip().lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)   # drop bracketed unit text
    s = re.split(r"[,;]", s)[0]                    # drop trailing ", m" style units
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


# Aliases shorter than this are matched only as a WHOLE column name, never as a word inside
# one. The single-character alias 't' is the dangerous one: 'Sigma-t (kg/m3)' normalises to
# 'sigma_t', and 't' matched it, handing density to WOD code 1, Temperature. '_t_' is a
# common qualifier token ('pot_t', 'cond_t') so it can never be trusted mid-name, while
# two-character aliases ('o2', 'do', 'ta') are chemistry symbols that do not occur as
# incidental tokens -- and 'o2_umol' must keep resolving. A column spelt as exactly a short
# alias is caught by the exact-match pass above regardless of this rule.
MIN_FALLBACK_ALIAS_LEN = 2


def match_variable(column_name: str) -> Optional[int]:
    """Return the WOD variable code for a raw column name, or None.

    Matching is on the normalized name, exact alias first, then a whole-word alias match.
    Longer aliases win so 'air_temperature' is not captured by 'temp', and aliases shorter
    than MIN_FALLBACK_ALIAS_LEN never take part in the fallback at all.
    """
    norm = normalize_name(column_name)
    if not norm:
        return None
    if norm in DEPTH_ALIASES:
        return DEPTH_SENTINEL_CODE

    for code, aliases in VARIABLE_ALIASES.items():
        if norm in aliases:
            return code

    best = None
    for code, aliases in list(VARIABLE_ALIASES.items()) + [(DEPTH_SENTINEL_CODE, DEPTH_ALIASES)]:
        for alias in aliases:
            if len(alias) < MIN_FALLBACK_ALIAS_LEN:
                continue
            if re.search(rf"(^|_){re.escape(alias)}($|_)", norm):
                if best is None or len(alias) > best[1]:
                    best = (code, len(alias))
    return best[0] if best else None


def resolve_dataset(dataset_type: str, depth_increment_m: Optional[float] = None) -> Dict[str, Any]:
    """Resolve an instrument/dataset label to a real WOD dataset code (Table 2).

    XCTD and CTD are routed by RESOLUTION, not instrument (manual p.19): a depth increment
    below 2 m is high-resolution and belongs to CTD, anything coarser belongs to OSD. When
    depth_increment_m is unknown the routing is reported as unresolved rather than guessed.
    """
    datasets = wod_datasets()
    label = (dataset_type or "").upper().strip()

    if label in datasets:
        if label == "CTD" and depth_increment_m is not None and depth_increment_m >= HIGH_RES_DEPTH_INCREMENT_M:
            return {"wod_dataset_code": "OSD", "resolved_by": "depth_increment",
                    "description": datasets["OSD"],
                    "note": f"depth increment {depth_increment_m:.2f} m >= "
                            f"{HIGH_RES_DEPTH_INCREMENT_M} m, so this is low-resolution CTD -> OSD"}
        return {"wod_dataset_code": label, "resolved_by": "exact_match",
                "description": datasets[label]}

    if label in ("XCTD", "STD", "XSTD"):
        if depth_increment_m is None:
            return {"wod_dataset_code": None, "resolved_by": "unresolved",
                    "description": None,
                    "note": f"{label} is not a WOD dataset. Routing needs the cast's depth "
                            f"increment: < {HIGH_RES_DEPTH_INCREMENT_M} m -> CTD, else OSD."}
        code = "CTD" if depth_increment_m < HIGH_RES_DEPTH_INCREMENT_M else "OSD"
        return {"wod_dataset_code": code, "resolved_by": "depth_increment",
                "description": datasets[code],
                "note": f"{label} with depth increment {depth_increment_m:.2f} m -> {code}"}

    return {"wod_dataset_code": None, "resolved_by": "unknown",
            "description": None,
            "note": f"'{dataset_type}' is not a WOD dataset name. Valid: {', '.join(sorted(datasets))}."}


def query_wod_spec(
    dataset_type: str,
    variable_query: Optional[List[str]] = None,
    depth_increment_m: Optional[float] = None,
) -> Dict[str, Any]:
    """Query WOD standard specifications for a dataset and set of variables.

    variable_query accepts raw column names or keywords; each is matched to a WOD code.
    Omit it to get every Table 3a variable.
    """
    resolved = resolve_dataset(dataset_type, depth_increment_m)
    variables = wod_variables()

    if variable_query:
        codes, unmatched = [], []
        for q in variable_query:
            c = match_variable(q)
            if c is None or c == DEPTH_SENTINEL_CODE:
                if c is None:
                    unmatched.append(q)
                continue
            if c not in codes:
                codes.append(c)
        selected = [variables[c] for c in codes if c in variables]
    else:
        selected, unmatched = list(variables.values()), []

    std_vars = [
        {
            "wod_code": v["code"],
            "wod_name": v["name"],
            "standard_unit": v["standard_unit"],
            "unit_abbrev": v["unit_abbrev"],
            "datasets": v["datasets"],
            "per_volume": v["code"] in PER_VOLUME_CODES,
            "aliases": VARIABLE_ALIASES.get(v["code"], []),
        }
        for v in selected
    ]

    stored_here = resolved["wod_dataset_code"]
    if stored_here:
        for sv in std_vars:
            sv["valid_for_dataset"] = stored_here in sv["datasets"]

    return {
        "dataset_type": (dataset_type or "").upper().strip(),
        "wod_dataset_code": stored_here,
        "dataset_resolution": resolved,
        "description": resolved.get("description"),
        "required_coordinates": ["latitude", "longitude", "depth", "time"],
        "standard_variables": std_vars,
        "unmatched_variable_queries": unmatched,
        "secondary_header_rules": {str(k): v for k, v in wod_secondary_headers().items()},
        "source": "WOD 2023 User's Manual, Tables 2/3a/4a via knowledge/wod_code_tables/",
    }


if __name__ == "__main__":
    import json

    print(json.dumps(query_wod_spec("XCTD", ["Temp (C)", "Sal_PSU", "Depth (m)"],
                                    depth_increment_m=0.5), indent=2)[:1400])
    print("\n--- XCTD with unknown resolution ---")
    print(json.dumps(query_wod_spec("XCTD")["dataset_resolution"], indent=2))
