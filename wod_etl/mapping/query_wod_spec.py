"""Match column names to WOD variables and resolve WOD datasets.

Uses Tables 2, 3a and 4a from knowledge/wod_code_tables/; fix values in the JSON, not here.
"""
import re
from typing import Dict, Any, List, Optional

from wod_etl.terms import english_terms
from wod_etl.config import HIGH_RES_DEPTH_INCREMENT_M
from wod_etl.reference import wod_datasets, wod_secondary_headers, wod_variables

# Matching hints of our own, not part of the WOD standard.
VARIABLE_ALIASES = {
    # Not potential temperature, which is derived (see NOT_STORED).
    1: ["temperature", "temp", "t", "temp_c", "t_deg_c", "sst", "water_temperature",
        "t090c", "t190c", "t068c", "t168c", "tv290c", "t090", "t190"],
    2: ["salinity", "sal", "psal", "psu", "practical_salinity", "sal00", "sal11", "psal00", "psal11",
        "salnty"],
    3: ["oxygen", "o2", "doxy", "oxy", "dissolved_oxygen", "do",
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
    25: ["pressure", "press", "pres", "dbar", "prdm", "prsm", "prdb", "prm", "pr"],
    # 'air_temp' stops the longest-alias fallback picking code 1's 'temp'.
    26: ["air_temperature", "airtemp", "atemp", "air_temp", "airtemperature",
         "dry_bulb", "dry_bulb_temperature"],
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

# Depth is the vertical axis, not a WOD variable (25 is Pressure): sentinel code 0, in metres.
DEPTH_SENTINEL_CODE = 0
DEPTH_ALIASES = ["depth", "dep", "z", "deph", "dpth", "depth_m", "depth_meters",
                 # Sea-Bird export names: depth in salt water / fresh water, metres or feet.
                 "depsm", "depfm", "depsf", "depff", "dpthm"]

# 'Depth' columns that are not the vertical axis (bottom depth is secondary header 10).
NOT_THE_DEPTH_AXIS = re.compile(
    r"^(?:bottom|water|station|sounding|seafloor|sea_floor|max|maximum|min|minimum|total|"
    r"cast|sensor|intake|calibration|target|nominal|upper|lower|start|end|bot)_"
    r"(?:.*_)?(?:depth|dep|z)(?:_|$)"
    r"|^(?:depth|dep)_(?:to_)?(?:bottom|seafloor|sea_floor|of_(?:station|sensor|water))(?:_|$)"
    r"|^sounding(?:_|$)")

# Variables whose standard unit is per volume, not per mass (Table 3a).
PER_VOLUME_CODES = {11, 17, 21}

# Columns that look like WOD variables but have no Table 3a code; never alias them.
# `reason` says why, `route` what to do instead.
NOT_STORED = {
    "potential_temperature": {
        "aliases": ["potential_temperature", "pot_temp", "pot_t", "ptmp", "ptemp", "theta",
                    "potemp", "potential_temp", "theta0", "tpot"],
        # Patterns catch spellings the alias list misses.
        "patterns": [r"^(?:pot|poten|potential)_?.*temp", r"^theta(_|$)", r"_theta$"],
        "reason": "Potential temperature is derived, not measured. Table 3a code 1 is "
                  "in-situ temperature; there is no code for theta.",
        "route": "If the file also has in-situ temperature, that is written and this is not. "
                 "If theta is all there is, convert it to in-situ during staging (it needs "
                 "pressure and salinity) and record the conversion. It is never mapped to "
                 "code 1: with both columns present the cast would claim Temperature twice, "
                 "and with theta alone it would report the wrong quantity.",
    },
    "size_fractionated_chlorophyll": {
        "aliases": [],
        # '20 µm Chlorophyll a' normalises to '20_m_chlorophyll_a' (µ is dropped).
        "patterns": [r"^(?:\d+_)*\d+_(?:um|m|micron)_.*chl", r"chl.*_\d+_?(?:um|micron)(?:_|$)"],
        "reason": "A size fraction of chlorophyll. WOD has one chlorophyll code (11), for the "
                  "total.",
        "route": "Not written; a total chlorophyll column is written as code 11.",
    },
    "oxygen_saturation": {
        "aliases": ["oxygen_saturation", "o2_saturation", "o2sat", "oxy_sat", "oxsat",
                    "doxy_sat", "do_sat", "percent_oxygen_saturation"],
        "patterns": [r"(?:^|_)(?:o2|oxy|oxygen|doxy|do)_?sat(?:uration)?(?:_|$)",
                     r"(?:^|_)saturation(?:_|$)"],
        "reason": "Percent saturation is derived from the oxygen concentration, temperature "
                  "and salinity. WOD stores the concentration (code 3).",
        "route": "Not written; the concentration column is written as code 3.",
    },
    "conductivity": {
        "aliases": ["conductivity", "cond", "c0s_m", "c1s_m", "c0ms_cm", "condwat", "cndc"],
        "reason": "WOD stores salinity, not conductivity. No Table 3a code.",
        "route": "Not written. Salinity is usually already derived in the same file; if it "
                 "is not, derive it during staging (it needs temperature and pressure).",
    },
    "density": {
        "aliases": ["density", "sigma_t", "sigma_theta", "sigma_0", "sigma0", "sigmat",
                    "sigma", "dens", "rho", "density_anomaly"],
        "patterns": [r"^sigma(_|\d|$)"],
        "reason": "Derived from temperature, salinity and pressure. No Table 3a code.",
        "route": "Not written.",
    },
    "sound_velocity": {
        "aliases": ["sound_velocity", "sound_speed", "svel", "svcm", "sv", "soundspeed"],
        "patterns": [r"sound_?(speed|velocity)", r"speed_of_sound"],
        "reason": "Derived. No Table 3a code.",
        "route": "Not written.",
    },
    "nitrite": {
        "aliases": ["nitrite", "no2"],
        "reason": "WOD deliberately excludes nitrite alone (manual p.49). Code 8 is nitrate, "
                  "or nitrate+nitrite.",
        "route": "A nitrite-only column is not written; a nitrate+nitrite column is written "
                 "as code 8 with variable specific second header 17 (Contains nitrite).",
    },
    "ammonium": {
        "aliases": ["ammonium", "ammonia", "nh4", "nh3"],
        "reason": "No Table 3a code.",
        "route": "Not written.",
    },
    "turbidity": {
        "aliases": ["turbidity", "ntu", "cdom", "fdom", "obs_turbidity"],
        "reason": "No Table 3a code. Code 24 is Transmissivity, a different measurement "
                  "that cannot stand in for it.",
        "route": "Not written.",
    },
}

# alias -> NOT_STORED key.
_NOT_STORED_BY_ALIAS = {a: k for k, v in NOT_STORED.items() for a in v["aliases"]}
_NOT_STORED_PATTERNS = [(re.compile(p), k)
                        for k, v in NOT_STORED.items() for p in v.get("patterns", [])]


def normalize_name(name: str) -> str:
    """A raw column name as a token for matching: 'Temp (C)' -> 'temp', 'Depth, m' -> 'depth'.

    `extract_unit` recovers the stripped unit.
    """
    s = english_terms(str(name)).strip().lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)   # drop bracketed unit text
    s = re.split(r"[,;]", s)[0]                    # drop trailing ", m" style units
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


# Typographic unit characters, folded for matching.
_UNIT_CHAR_FOLD = str.maketrans({
    "\u03bc": "\u00b5",                      # Greek mu -> micro sign (identical on screen)
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u207b", "-"),  # dashes, minus
    **dict(zip("\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079",
               "0123456789")),                # superscript digits, as in kg\u207b\u00b9
    **dict.fromkeys("\u2044\u2215", "/"),            # fraction and division slashes
    **dict.fromkeys("\u00b7\u2022\u22c5", "."),       # middle dots used as a separator
    **dict.fromkeys("\u00a0\u2009\u202f", " "),       # non-breaking and thin spaces
    "\u2103": "\u00b0C",                    # ℃ as one character
})


def canonical_unit_text(text: str) -> str:
    """Fold typographic unit characters onto one spelling, for matching only."""
    return str(text).translate(_UNIT_CHAR_FOLD)


def extract_unit(name: str) -> Optional[str]:
    """The unit text in a raw column name, verbatim, or None: `Temperature (deg C)` -> 'deg C'.

    Recorded as the originator's unit (variable specific second header 8).
    """
    raw = str(name).strip()
    for candidate in reversed(re.findall(r"[\(\[\{]\s*([^\)\]\}]*?)\s*[\)\]\}]", raw)):
        # A bracket may hold a scale and a unit ('[ITS-90, deg C]'): try each part.
        for part in [candidate] + [p.strip() for p in reversed(candidate.split(","))]:
            if _looks_like_unit(part):
                return part
    tail = re.split(r"[,;]", raw)
    if len(tail) > 1 and _looks_like_unit(tail[-1].strip()):
        return tail[-1].strip()
    return None


# Measurement scales in a column header: variable specific second header 3 (Appendix 3.1).
_SCALE_PATTERNS = [
    (r"\bits[-_\s]?90\b", "ITS-90"),
    (r"\bipts[-_\s]?68\b", "IPTS-68"),
    (r"\bits[-_\s]?68\b", "IPTS-68"),
    (r"\bpss[-_\s]?78\b", "PSS-78"),
    (r"\bteos[-_\s]?10\b", "TEOS-10"),
    (r"\beos[-_\s]?80\b", "EOS-80"),
    (r"\btotal\s+scale\b", "pH total scale"),
    (r"\bseawater\s+scale\b", "pH seawater scale"),
    (r"\bnbs\s+scale\b", "pH NBS scale"),
]


def extract_scale(name: str) -> Optional[str]:
    """The measurement scale a column header names, e.g. 'ITS-90' or 'PSS-78'."""
    raw = str(name).lower()
    for pattern, label in _SCALE_PATTERNS:
        if re.search(pattern, raw):
            return label
    return None


# Units are short and use unit characters; these words mark a qualifier or a scale.
_UNIT_LIKE = re.compile(r"^[A-Za-zµ°‰%/·^\-\d\s.]+$")
_NOT_UNITS = {"secondary", "primary", "bottle", "ctd", "sensor", "sensors", "corrected",
              "raw", "average", "mean", "adjusted", "calculated", "derived", "unknown",
              "downcast", "upcast", "flag", "flags", "quality", "qc", "note", "notes",
              "its", "ipts", "pss", "teos", "eos", "scale"}


def _looks_like_unit(text: str) -> bool:
    """Whether a bracketed suffix is a unit, not a qualifier ('bottle') or a scale."""
    text = canonical_unit_text(text or "").strip()
    if not text or len(text) > 24 or not _UNIT_LIKE.match(text):
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]
    if not words:
        return bool(re.fullmatch(r"[µ°‰%]+", text))   # symbol-only units: ‰, %, °
    return not any(w in _NOT_UNITS for w in words)


def _not_stored_key(norm: str, exact_only: bool = False) -> Optional[str]:
    """The NOT_STORED entry a normalized column name belongs to, if any."""
    key = _NOT_STORED_BY_ALIAS.get(norm)
    if key or exact_only:
        return key
    for pattern, k in _NOT_STORED_PATTERNS:
        if pattern.search(norm):
            return k
    for alias, k in _NOT_STORED_BY_ALIAS.items():
        if len(alias) >= MIN_FALLBACK_ALIAS_LEN and re.search(
                rf"(^|_){re.escape(alias)}($|_)", norm):
            return k
    return None


# ------------------------------------------------------------ fluorescence
# WOD has no fluorescence variable. A fluorometer column maps to Chlorophyll (code 11) when
# its unit is a mass concentration (ug/l, mg/m3, ppb), or with no unit when it is a Sea-Bird
# calibrated channel. Otherwise it is dropped and reported.
_FLUOR_NAME = re.compile(r"(?:^|_)(?:chl_?)?fluor(?:escence|ometer|o)?(?:_|$)|^fl(?:u)?(?:_|$)")
SEABIRD_FLUOR_CHANNELS = {"flsp", "flspp", "flecoafl", "fleco_afl", "flc", "flscufa",
                          "flspr", "wetstar", "chl_fluorescence"}
_MASS_CONCENTRATION = re.compile(
    r"^(?:[µu]g\s*/\s*l|[µu]g\s*l-?1|mg\s*/\s*m\s*\^?\s*3|mg\s*m-?3|ppb|[µu]g/dm3)$", re.I)
# Non-chlorophyll fluorophores, which may share chlorophyll's units; the rule skips them.
_OTHER_FLUOROPHORE = re.compile(
    r"(?:^|_)(?:cdom|fdom|dom|phyco\w*|rhodamine|rwt|uranine|fluorescein|oil|crude|"
    r"hydrocarbons?|tryptophan|trp)(?:_|$)")


def fluorescence_rule(column_name: str) -> Optional[Dict[str, Any]]:
    """For a fluorometer column: the code it maps to (11 or None) and why. None otherwise."""
    norm = normalize_name(column_name)
    if not norm or not (norm in SEABIRD_FLUOR_CHANNELS or _FLUOR_NAME.search(norm)):
        return None
    if _OTHER_FLUOROPHORE.search(norm):
        return None
    unit = extract_unit(column_name)
    unit_text = canonical_unit_text(unit or "").strip()
    if unit_text and _MASS_CONCENTRATION.match(unit_text):
        return {"code": 11, "unit": unit,
                "comment": f"Chlorophyll from an in-situ fluorometer: the originator's column "
                           f"{column_name!r} is fluorescence reported as a concentration "
                           f"({unit}). It is not extracted chlorophyll-a, and its accuracy "
                           f"depends on the fluorometer's calibration."}
    if not unit_text and norm in SEABIRD_FLUOR_CHANNELS:
        return {"code": 11, "unit": None,
                "comment": f"Chlorophyll from an in-situ fluorometer: {column_name!r} is a "
                           f"Sea-Bird calibrated fluorometer channel, whose output is a "
                           f"concentration. Not extracted chlorophyll-a."}
    return {"code": None, "unit": unit,
            "reason": (f"Fluorescence in {unit!r} is not a chlorophyll concentration, and WOD "
                       f"has no fluorescence variable." if unit_text else
                       "Fluorescence with no stated unit: it may be a concentration or a raw "
                       "signal, and only a concentration can be written as Chlorophyll."),
            "route": ("Not written. If the submitter supplies a calibration to ug/l, apply it "
                      "in staging and name the column with that unit."
                      if unit_text else
                      "Find the unit (ISO metadata, readme, instrument export) and put it in "
                      "the staged column name: 'Fluorescence (ug/l)' maps to Chlorophyll, "
                      "'Fluorescence (RFU)' is dropped.")}


def explain_column(column_name: str) -> Optional[Dict[str, Any]]:
    """Why a column has no WOD code: a reason and route, or None if it is unrecognised."""
    norm = normalize_name(column_name)
    fluor = fluorescence_rule(column_name)
    if fluor is not None and fluor["code"] is None:
        return {"column": column_name, "variable": "fluorescence", "reason": fluor["reason"],
                "route": fluor["route"], "do_not_alias": True}
    if not norm or match_variable(column_name) is not None:
        return None
    key = _not_stored_key(norm)
    if key is None:
        return None
    entry = NOT_STORED[key]
    return {"column": column_name, "variable": key, "reason": entry["reason"],
            "route": entry["route"], "do_not_alias": True}


# Shorter aliases match only a whole name, not a token ('t' would match 'sigma_t').
MIN_FALLBACK_ALIAS_LEN = 2


def match_variable(column_name: str) -> Optional[int]:
    """The WOD variable code for a raw column name, or None.

    Exact alias first, then the longest whole-word alias at least MIN_FALLBACK_ALIAS_LEN long.
    """
    norm = normalize_name(column_name)
    if not norm:
        return None
    if norm in DEPTH_ALIASES:
        return DEPTH_SENTINEL_CODE
    if NOT_THE_DEPTH_AXIS.match(norm):
        return None
    fluor = fluorescence_rule(column_name)
    if fluor is not None:
        return fluor["code"]

    # Block names WOD does not store before the fallback ('potential_temperature' contains
    # 'temperature'). Exact checks come first so 'nitrate_nitrite' still resolves to 8.
    if _not_stored_key(norm, exact_only=True):
        return None

    for code, aliases in VARIABLE_ALIASES.items():
        if norm in aliases:
            return code

    if _not_stored_key(norm):
        return None

    best = None
    for code, aliases in list(VARIABLE_ALIASES.items()) + [(DEPTH_SENTINEL_CODE, DEPTH_ALIASES)]:
        for alias in aliases:
            if len(alias) < MIN_FALLBACK_ALIAS_LEN:
                continue
            if re.search(rf"(^|_){re.escape(alias)}($|_)", norm):
                if best is None or len(alias) > best[1]:
                    best = (code, len(alias))
    return best[0] if best else None


def resolve_column(query: str, columns: List[str]) -> Dict[str, Any]:
    """The column a plain name refers to ('latitude' -> 'LAT'), by tiers tried in order.

    Several hits at one tier give `ambiguous: True`; a column WOD does not store is refused.
    """
    cols = [c for c in columns if c and c.strip()]
    out: Dict[str, Any] = {"query": query, "column": None, "wod_code": None,
                           "matched_by": None, "confidence": "none",
                           "candidates": [], "ambiguous": False, "note": None}
    if not query or not cols:
        out["note"] = "nothing to match" if cols else "no columns given"
        return out

    nq = normalize_name(query)
    q_code = match_variable(query)

    tiers = [
        ("exact", [c for c in cols if c == query], "high"),
        ("normalized", [c for c in cols if normalize_name(c) == nq], "high"),
        ("wod_code", [c for c in cols if q_code is not None
                      and match_variable(c) == q_code] if q_code is not None else [], "high"),
        ("substring", [c for c in cols if nq and nq in normalize_name(c)], "low"),
    ]

    for tier, hits, confidence in tiers:
        hits = list(dict.fromkeys(hits))          # de-dup, keep order
        if not hits:
            continue
        out["candidates"] = hits
        out["matched_by"] = tier
        if len(hits) > 1:
            out["ambiguous"] = True
            out["note"] = (f"{len(hits)} columns match {query!r} at the {tier} tier: "
                           f"{hits}. Name the one you mean. Sensor-indexed pairs such as "
                           f"t090C/t190C resolve to the same WOD code by design: they "
                           f"are the primary and secondary sensor, not duplicates.")
            return out
        column = hits[0]
        refusal = explain_column(column)
        if refusal is not None:
            out["candidates"] = hits
            out["note"] = (f"{query!r} resolves to column {column!r}, which WOD does not "
                           f"store: {refusal['reason']} {refusal['route']}")
            return out
        out.update({"column": column, "wod_code": match_variable(column),
                    "confidence": confidence})
        return out

    out["note"] = f"no column matches {query!r}. Columns present: {cols}"
    return out


def resolve_dataset(dataset_type: str, depth_increment_m: Optional[float] = None) -> Dict[str, Any]:
    """Resolve an instrument/dataset label to a WOD dataset code (Table 2).

    CTD-like labels are routed by resolution (manual p.19): a depth increment below 2 m is
    CTD, coarser is OSD. With no increment the routing is reported as unresolved.
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
    """WOD's definition of a dataset and of each queried variable.

    `variable_query` takes column names or keywords; omit it for every Table 3a variable.
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
