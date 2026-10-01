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
    # NOT potential temperature. Theta is a DERIVED quantity -- in-situ temperature adjusted
    # for adiabatic compression -- and aliasing it here made a cast claim Temperature twice
    # when both columns were present, and silently mislabelled theta as in-situ when only
    # theta was. It can be converted back to in-situ temperature if pressure and salinity
    # are known, but that is a computation done during staging, not a rename. See NOT_STORED.
    1: ["temperature", "temp", "t", "temp_c", "t_deg_c", "sst", "water_temperature",
        "t090c", "t190c", "t068c", "t168c", "tv290c", "t090", "t190"],
    2: ["salinity", "sal", "psal", "psu", "practical_salinity", "sal00", "sal11", "psal00", "psal11",
        "salnty"],
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
    25: ["pressure", "press", "pres", "dbar", "prdm", "prsm", "prdb", "prm", "pr"],
    # 'air_temp' must be spelt out: the fallback picks the LONGEST matching alias, and
    # 'air_temperature' does not match the token 'air_temp' while code 1's 'temp' does --
    # so an air temperature column was resolving to water temperature.
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

# Depth is the vertical coordinate, not a WOD measured variable. Code 25 is Pressure, a
# distinct variable. We use a sentinel of 0 internally to mean "this column is the depth
# axis in meters".
DEPTH_SENTINEL_CODE = 0
DEPTH_ALIASES = ["depth", "dep", "z", "deph", "depth_m", "depth_meters",
                 # Sea-Bird export names: depth in salt water / fresh water, metres or feet.
                 "depsm", "depfm", "depsf", "depff", "dpthm"]

# Columns that contain the word 'depth' and are NOT the vertical axis. The fallback matched
# 'depth' inside 'bottom_depth' and handed the seafloor sounding to the depth axis, which
# then set every level of the cast to the same depth. Bottom depth is secondary header 10;
# the others are per-cast metadata too. See wod_etl/mapping/map_metadata.py.
NOT_THE_DEPTH_AXIS = re.compile(
    r"^(?:bottom|water|station|sounding|seafloor|sea_floor|max|maximum|min|minimum|total|"
    r"cast|sensor|intake|calibration|target|nominal|upper|lower|start|end|bot)_"
    r"(?:.*_)?(?:depth|dep|z)(?:_|$)"
    r"|^(?:depth|dep)_(?:to_)?(?:bottom|seafloor|sea_floor|of_(?:station|sensor|water))(?:_|$)"
    r"|^sounding(?:_|$)")

# Standard units that are per-VOLUME, not per-mass (Table 3a). A molar-to-mass conversion
# must never be applied to these.
PER_VOLUME_CODES = {11, 17, 21}

# Columns that LOOK like WOD variables and are not. Recognising them by name is the point:
# an unexplained entry in `unmapped_columns` is indistinguishable from a column nobody
# thought about, and the fix an agent reaches for -- adding an alias -- is exactly wrong for
# every one of these. `reason` says why there is no code; `route` says what to do instead.
NOT_STORED = {
    "potential_temperature": {
        "aliases": ["potential_temperature", "pot_temp", "pot_t", "ptmp", "ptemp", "theta",
                    "potemp", "potential_temp", "theta0", "tpot"],
        # An alias list cannot cover every spelling of a two-word name, and every miss here
        # falls through to the 'temperature' fallback and becomes code 1. The pattern does.
        "patterns": [r"^(?:pot|poten|potential)_?.*temp", r"^theta(_|$)", r"_theta$"],
        "reason": "Potential temperature is derived, not measured. Table 3a code 1 is "
                  "in-situ temperature; there is no code for theta.",
        "route": "If the file ALSO has in-situ temperature, map that and drop this. If theta "
                 "is all there is, convert it to in-situ during staging -- it needs pressure "
                 "and salinity -- and record the conversion. Never alias it to code 1: with "
                 "both columns present the cast claims Temperature twice, and with only "
                 "theta present the cast silently reports the wrong quantity.",
    },
    "conductivity": {
        "aliases": ["conductivity", "cond", "c0s_m", "c1s_m", "c0ms_cm", "condwat", "cndc"],
        "reason": "WOD stores salinity, not conductivity. No Table 3a code.",
        "route": "Drop it and report the drop. Salinity is usually already derived in the "
                 "same file; if it is not, deriving it needs temperature and pressure and "
                 "belongs in staging.",
    },
    "density": {
        "aliases": ["density", "sigma_t", "sigma_theta", "sigma_0", "sigma0", "sigmat",
                    "sigma", "dens", "rho", "density_anomaly"],
        "patterns": [r"^sigma(_|\d|$)"],
        "reason": "Derived from temperature, salinity and pressure. No Table 3a code.",
        "route": "Drop it and report the drop. Note that 'sigma-t' normalises to a token "
                 "ending in '_t', which is why the single-character alias 't' is barred "
                 "from fallback matching.",
    },
    "sound_velocity": {
        "aliases": ["sound_velocity", "sound_speed", "svel", "svcm", "sv", "soundspeed"],
        "patterns": [r"sound_?(speed|velocity)"],
        "reason": "Derived. No Table 3a code.",
        "route": "Drop it and report the drop.",
    },
    "nitrite": {
        "aliases": ["nitrite", "no2"],
        "reason": "WOD deliberately excludes nitrite alone (manual p.49). Code 8 is nitrate, "
                  "or nitrate+nitrite.",
        "route": "Drop a nitrite-only column. A nitrate+nitrite column IS storable: map it "
                 "to code 8 and set variable specific second header 17 (Contains nitrite).",
    },
    "ammonium": {
        "aliases": ["ammonium", "ammonia", "nh4", "nh3"],
        "reason": "No Table 3a code.",
        "route": "Drop it and report the drop.",
    },
    "turbidity": {
        "aliases": ["turbidity", "ntu", "cdom", "fdom", "obs_turbidity"],
        "reason": "No Table 3a code. Code 24 is Transmissivity, which is a different "
                  "measurement -- do not substitute one for the other.",
        "route": "Drop it and report the drop.",
    },
}

# alias -> registry key, built once so match failures can be explained in one lookup.
_NOT_STORED_BY_ALIAS = {a: k for k, v in NOT_STORED.items() for a in v["aliases"]}
_NOT_STORED_PATTERNS = [(re.compile(p), k)
                        for k, v in NOT_STORED.items() for p in v.get("patterns", [])]


def normalize_name(name: str) -> str:
    """Reduce a raw column name to a comparable token.

    'Temp (C)' -> 'temp', 'Sal_PSU [psu]' -> 'sal_psu', 'Depth, m' -> 'depth'
    Unit suffixes in brackets or after a comma are stripped; the remainder is lowercased
    and reduced to underscore-separated words.

    The stripped unit text is NOT lost -- `extract_unit` recovers it from the same name, and
    the mapping contract records it per column as the originator's unit (variable specific
    second header 8). Normalisation is for MATCHING only; nothing should treat the
    normalised form as the column's identity.
    """
    s = str(name).strip().lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)   # drop bracketed unit text
    s = re.split(r"[,;]", s)[0]                    # drop trailing ", m" style units
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def extract_unit(name: str) -> Optional[str]:
    """The unit text a raw column name carries, verbatim, or None.

    The counterpart to `normalize_name`: what that function throws away in order to match,
    this one keeps in order to record. `Temperature (deg C)` -> 'deg C', `DO [ml/l]` ->
    'ml/l', `Depth, m` -> 'm', `PRES (decibar)` -> 'decibar'.

    This is what the originator wrote, not a WOD unit. It is preserved because the
    conversion applied downstream is only auditable against the unit the source declared --
    and because WOD has a field for exactly this, variable specific second header 8.
    """
    raw = str(name).strip()
    for candidate in reversed(re.findall(r"[\(\[\{]\s*([^\)\]\}]*?)\s*[\)\]\}]", raw)):
        # A bracket may hold a scale AND a unit: '[ITS-90, deg C]'. Try the whole thing,
        # then each comma-separated part from the right, so the unit is found either way.
        for part in [candidate] + [p.strip() for p in reversed(candidate.split(","))]:
            if _looks_like_unit(part):
                return part
    tail = re.split(r"[,;]", raw)
    if len(tail) > 1 and _looks_like_unit(tail[-1].strip()):
        return tail[-1].strip()
    return None


# Measurement scales named in a column header. These are variable specific second header 3
# (Scale, Appendix 3.1), not units -- recording them as units would be wrong, and dropping
# them loses the only statement of which standard the numbers are on.
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
    """The measurement scale a column header names, e.g. 'ITS-90' or 'PSS-78'.

    Sea-Bird writes `t090C: Temperature [ITS-90, deg C]`; the scale is half of what that
    bracket says and is not a unit. WOD has a field for it: variable specific second header
    3 (Appendix 3.1).
    """
    raw = str(name).lower()
    for pattern, label in _SCALE_PATTERNS:
        if re.search(pattern, raw):
            return label
    return None


# A bracketed suffix is not always a unit: 'Temperature (secondary sensor)' and
# 'Salinity (bottle)' are qualifiers. Units are short and made of unit-ish characters.
_UNIT_LIKE = re.compile(r"^[A-Za-zµ°%/·^\-\d\s.]+$")
_NOT_UNITS = {"secondary", "primary", "bottle", "ctd", "sensor", "sensors", "corrected",
              "raw", "average", "mean", "adjusted", "calculated", "derived", "unknown",
              "downcast", "upcast", "flag", "flags", "quality", "qc", "note", "notes",
              "its", "ipts", "pss", "teos", "eos", "scale"}


def _looks_like_unit(text: str) -> bool:
    """A bracketed suffix is a unit, a qualifier, or a scale. Only the first is a unit."""
    text = (text or "").strip()
    if not text or len(text) > 24 or not _UNIT_LIKE.match(text):
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]
    # A qualifier word anywhere disqualifies it: 'secondary sensor' is not a unit, and
    # neither is 'ITS-90' on its own -- that is a scale, and extract_scale claims it.
    return bool(words) and not any(w in _NOT_UNITS for w in words)


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


def explain_column(column_name: str) -> Optional[Dict[str, Any]]:
    """Why a column has no WOD code, when the reason is known.

    Returns None for a column that simply was not recognised -- that one may need an alias.
    Returns a reason and a route for the ones that must NOT be aliased, because they have no
    Table 3a code at all and mapping them onto an adjacent one is worse than dropping them.
    """
    norm = normalize_name(column_name)
    if not norm or match_variable(column_name) is not None:
        return None
    key = _not_stored_key(norm)
    if key is None:
        return None
    entry = NOT_STORED[key]
    return {"column": column_name, "variable": key, "reason": entry["reason"],
            "route": entry["route"], "do_not_alias": True}


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
    if NOT_THE_DEPTH_AXIS.match(norm):
        return None

    # A name WOD does not store must be blocked before the alias fallback, or it is captured
    # by the variable it is named after: 'potential_temperature' contains 'temperature', and
    # the fallback duly returned code 1. Exact matches are checked first in both directions
    # so 'nitrate_nitrite' still resolves to code 8 rather than being blocked as nitrite.
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
    """Which of these columns does a human's name for one of them refer to?

    "Rename the latitude column to Latitude" does not say that the column is spelt `LAT`,
    and the caller should not have to know. This answers that, and **refuses to choose
    between two candidates** rather than picking one.

        resolve_column("latitude", ["LAT", "LON", "Depth (m)"])
        # {"column": "LAT", "wod_code": 30, "matched_by": "wod_code",
        #  "confidence": "high", "ambiguous": False, ...}

    Four tiers, tried in order and reported in `matched_by`:

    | tier         | test                                        | catches                  |
    | ---          | ---                                         | ---                      |
    | `exact`      | string equality                             | the caller knew the name |
    | `normalized` | `normalize_name` equality                   | `latitude` -> `LATITUDE` |
    | `wod_code`   | both resolve to the same WOD variable code  | `latitude` -> `LAT`      |
    | `substring`  | normalised substring, `confidence: "low"`   | last resort              |

    Two or more hits at the winning tier gives `column: None` and `ambiguous: True` with
    the candidates listed. That is the real case for a Sea-Bird cast: `t090C` and `t190C`
    are the primary and secondary temperature sensor and **both resolve to code 1**, so
    "the temperature column" genuinely does not identify one of them.

    A query that lands on a column WOD does not store is refused with `explain_column`'s
    reason, so asking for "the temperature column" cannot quietly return a potential
    temperature.
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
                           f"t090C/t190C resolve to the same WOD code by design -- they "
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
