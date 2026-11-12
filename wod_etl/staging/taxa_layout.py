"""Map a plankton table to WOD Taxa-Records from its header.

    Station,Upper depth (m),Lower depth (m),Taxon,Displacement volume (ml),Wet weight (g)

Each measurement column becomes its own Taxa-Record. Table 7a selects the smaller unit by sign,
so a value in mg is written negative. UNIT (field 20) is what a column is per (`m3` for
'ind/m3'), or `sample` when unstated.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wod_etl.staging.station_list import _ROLE_PATTERNS, normalise_station
from wod_etl.staging.tables import read_table
from wod_etl.staging.taxa_haul import taxa_record_field

# Table 7a measurement fields: (unit if positive, unit if negative). A count has none.
FIELD_UNITS: Dict[int, Tuple[Optional[str], Optional[str]]] = {
    10: (None, None), 11: ("m3", "ml"), 12: ("ml", "nl"), 13: ("g", "mg"), 14: ("g", "mg"),
    15: ("mg", "ng"), 25: ("g", "mg")}
FIELD_LABELS = {4: "biomass", 10: "count", 11: "sample volume", 12: "displacement volume",
                13: "wet weight", 14: "dry weight", 15: "ash-free dry weight",
                25: "carbon content"}

_MEASURE_ALIASES = {"ml": "ml", "cm3": "ml", "cc": "ml", "cm^3": "ml", "g": "g", "mg": "mg",
                    "ng": "ng", "nl": "nl", "m3": "m3", "m^3": "m3", "l": "l"}
_COUNT_WORDS = {"", "n", "no", "no.", "number", "ind", "ind.", "inds", "individuals", "#",
                "count", "counts"}
_PER = re.compile(r"(?:/|\bper\s+)\s*(1000\s*m\s*\^?3|m\s*\^?3|m\s*\^?2|l|liters?|litres?|"
                  r"samples?|tows?|hauls?)\s*$", re.I)
_PER_NEGATIVE = re.compile(r"\s(1000\s*)?m\s*\^?-\s*([23])\s*$", re.I)
_MISSING = {"", "na", "n/a", "nan", "-", "--", "nd", "none"}

# Whole-sample biomass, by its WOD variable number (Table 8; names from WOD's taxa list) and
# the unit that number states its value in. Ash-free is checked before dry.
BIOMASS = [(r"zoo.*displacement\s*vol", -401, "Zooplankton displacement volume", "ml"),
           (r"zoo.*settled\s*vol", -402, "Zooplankton settled volume", "ml"),
           (r"zoo.*wet\s*(?:weight|mass)", -403, "Zooplankton wet weight", "mg"),
           (r"zoo.*ash[\s-]*free", -405, "Zooplankton ashfree dry weight", "mg"),
           (r"zoo.*dry\s*(?:weight|mass)", -404, "Zooplankton dry weight", "mg"),
           (r"ichthyo.*displacement\s*vol", -501, "Ichthyoplankton displacement volume", "m3"),
           (r"ichthyo.*wet\s*(?:weight|mass)", -503, "Ichthyoplankton wet weight", "kg")]
# A net hauled vertically from a depth to the surface: one depth on the row is the tow's start.
_VERTICAL_NET = re.compile(r"norpac|vertical[\s_-]*(?:tow|haul|net)", re.I)


def _unit_text(column: str) -> str:
    m = re.search(r"[\(\[]([^\)\]]*)[\)\]]", column)
    return m.group(1).strip() if m else ""


def unit_parts(column: str) -> Tuple[str, Optional[str]]:
    """(measured unit, per-unit) from a column name: 'Wet weight (mg/m3)' -> ('mg', 'm3')."""
    text = _unit_text(column)
    per = None
    m = _PER.search(text) or _PER_NEGATIVE.search(" " + text)
    if m:
        if m.re is _PER:
            word = re.sub(r"\s+|\^", "", m.group(1).lower())
            per = {"liter": "l", "liters": "l", "litre": "l", "litres": "l"}.get(word, word)
            per = "sample" if re.fullmatch(r"(samples?|tows?|hauls?)", per) else per
            per = "1000 m3" if per == "1000m3" else per
        else:
            per = ("1000 m" if m.group(1) else "m") + m.group(2)
        text = text[:m.start()] if m.re is _PER else (" " + text)[:m.start()].strip()
    return re.sub(r"\s+", "", text.strip().lower()), per


def _role(columns: List[str], role: str) -> Optional[str]:
    return next((c for c in columns if re.search(_ROLE_PATTERNS[role], c.strip().lower())),
                None)


def describe_taxa_table(path) -> Dict[str, Any]:
    """`{"columns": {role: name}, "measurements", "stations", "rows", "missing"}`.

    An empty `missing` means the table can be staged once its hauls are located.
    """
    from wod_etl.staging.station_list import _header_row_below_title

    columns, rows, info = read_table(Path(path), delimiter=None,
                                     header_row=_header_row_below_title(path, ","))
    missing: List[str] = []
    if info.get("error") or not columns:
        return {"layout": None, "columns": {}, "measurements": [], "stations": [], "rows": 0,
                "missing": [f"it cannot be read as a table: {info.get('error') or 'no header'}"]}
    fields = {c: taxa_record_field(c) for c in columns}
    by_field = lambda fid: next((c for c, f in fields.items() if f == fid), None)
    roles = {"taxon": by_field(1), "upper": by_field(2), "lower": by_field(3),
             "stage": by_field(5), "sex": by_field(6),
             **{r: _role(columns, r) for r in ("station", "latitude", "longitude", "date",
                                                "time")}}
    biomass = []
    for c in columns:
        hit = next((b for b in BIOMASS if re.search(b[0], c, re.I)), None)
        if hit:
            measured, per = unit_parts(c)
            measured = re.sub(r"(?:ww|dw|wet|dry)$", "", measured)
            biomass.append({"column": c, "field": 4, "label": "biomass", "biomass": hit[1],
                            "taxon": hit[2], "unit": measured or None, "per_unit": per or "sample",
                            "negate": False})
            if measured and _MEASURE_ALIASES.get(measured, measured) != hit[3]:
                missing.append(f"{c!r} is in {measured}; WOD states {hit[2].lower()} in {hit[3]}")
    vertical = bool(_VERTICAL_NET.search(str(path)))
    if not (roles["upper"] or roles["lower"]) and vertical:
        depth = next((c for c in columns if re.fullmatch(r"depth|depth\s*\(m\)|tow\s*depth.*",
                                                        c.strip(), re.I)), None)
        if depth:
            roles.update(lower=depth, upper_zero="yes")
    if biomass:
        roles["taxon"] = roles["taxon"] or None
    if not roles["taxon"] and not biomass:
        missing.append("no column names the taxon")
    if not ((roles["upper"] or roles.get("upper_zero")) and roles["lower"]):
        missing.append("no upper and lower depth columns (Table 7a fields 2, 3)")

    measurements = list(biomass)
    for c, fid in fields.items():
        if c in {b["column"] for b in biomass}:
            continue
        if fid == 4:
            missing.append(f"{c!r} is a biomass value, which needs a WOD biomass code "
                           f"(Table 8)")
        if fid not in FIELD_UNITS:
            continue
        measured, per = unit_parts(c)
        unit, smaller = FIELD_UNITS[fid]
        if fid == 10:
            if measured not in _COUNT_WORDS:
                missing.append(f"{c!r} is a count in {measured!r}, not a number of individuals")
                continue
            negate = False
        else:
            measured = _MEASURE_ALIASES.get(measured, measured)
            if not measured:
                missing.append(f"{c!r} states no unit")
                continue
            if measured not in (unit, smaller):
                missing.append(f"{c!r} is in {measured}, which WOD cannot state for "
                               f"{FIELD_LABELS[fid]} (only {unit} or {smaller})")
                continue
            negate = measured == smaller
        measurements.append({"column": c, "field": fid, "label": FIELD_LABELS[fid],
                             "unit": measured or None, "per_unit": per or "sample",
                             "negate": negate})
    if not measurements and not biomass and not any(f == 4 for f in fields.values()):
        missing.append("no column is a measurement WOD takes as a Taxa-Record")

    stations = []
    if roles["station"]:
        i = columns.index(roles["station"])
        stations = sorted({normalise_station(r[i]) for r in rows
                           if i < len(r) and str(r[i]).strip()})
    return {"layout": "long", "columns": {k: v for k, v in roles.items() if v},
            "measurements": measurements, "stations": stations, "rows": len(rows),
            "missing": missing}


def is_missing(value: Any) -> bool:
    return str(value).strip().lower() in _MISSING
