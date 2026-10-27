"""Single loading point for the WOD reference tables in knowledge/wod_code_tables/."""
import functools
import json
from typing import Any, Dict, List, Optional, Tuple

from wod_etl.config import CODE_TABLES_DIR

TABLE_FILES = {
    "table_2_datasets": "table_2_datasets.json",
    "table_3a_variables": "table_3a_variables.json",
    "table_3b_unit_constants": "table_3b_unit_constants.json",
    "table_4a_secondary_headers": "table_4a_secondary_headers.json",
    "table_5a_variable_specific_headers": "table_5a_variable_specific_headers.json",
    "table_6a_biological_headers": "table_6a_biological_headers.json",
    "table_7a_taxa_biomass": "table_7a_taxa_biomass.json",
    "code_table_links": "code_table_links.json",
    "table_11_range_variables": "table_11_range_variables.json",
    "table_12_qc_flags": "table_12_qc_flags.json",
    "table_13_gradients": "table_13_gradients.json",
    "appendix_11_ranges": "appendix_11_ranges.json",
    "appendix_1_country_codes": "appendix_1_country_codes.json",
}


@functools.lru_cache(maxsize=None)
def load_table(name: str) -> Dict[str, Any]:
    """Load one reference table by short name (e.g. 'table_3a_variables')."""
    filename = TABLE_FILES.get(name, name if name.endswith(".json") else f"{name}.json")
    path = CODE_TABLES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Missing WOD reference table {path}. These are extracted from "
            f"knowledge/wod_readme.pdf; see knowledge/wod_code_tables/"
            f"standardization_rules.md. Known tables: {', '.join(sorted(TABLE_FILES))}."
        )
    return json.loads(path.read_text())


def available_tables() -> List[Dict[str, Any]]:
    """Every table with its title and manual page."""
    out = []
    for name in sorted(TABLE_FILES):
        d = load_table(name)
        out.append({
            "name": name,
            "title": d.get("table", name),
            "pages": d.get("pages", d.get("page")),
        })
    return out


# ------------------------------------------------------------------ Table 2
def wod_datasets() -> Dict[str, str]:
    """The eleven WOD dataset names (Table 2, p.17)."""
    return load_table("table_2_datasets")["datasets"]


# ----------------------------------------------------------------- Table 3a
def wod_variables() -> Dict[int, Dict[str, Any]]:
    """Measured variables keyed by WOD variable code (Table 3a, p.18)."""
    return {v["code"]: v for v in load_table("table_3a_variables")["variables"]}


# ----------------------------------------------------------------- Table 3b
def unit_constants() -> Dict[str, Any]:
    """Atomic weights and the molar volume of O2 at STP (Table 3b, p.19)."""
    return load_table("table_3b_unit_constants")


# ----------------------------------------------------------------- Table 4a
def wod_secondary_headers() -> Dict[int, str]:
    """Secondary header metadata codes (Table 4a, p.23)."""
    return {int(k): v for k, v in load_table("table_4a_secondary_headers")["secondary_headers"].items()}


# ----------------------------------------------------------------- Table 5a
def variable_specific_headers() -> Dict[int, Dict[str, Any]]:
    """Variable specific second headers keyed by ID (Table 5a/5b, pp.28-29).

    Per variable, and a separate namespace from Table 4a: ID 5 is 'Instrument' here.
    """
    return {int(k): v for k, v in
            load_table("table_5a_variable_specific_headers")["variable_specific_headers"].items()}


# ----------------------------------------------------------------- Table 6a
def biological_headers() -> Dict[int, Dict[str, Any]]:
    """Biological (plankton sampling) header variables keyed by ID (Table 6a/6b, pp.30-31)."""
    return {int(k): v for k, v in
            load_table("table_6a_biological_headers")["biological_headers"].items()}


# ----------------------------------------------------------------- Table 7a
def taxa_biomass_variables() -> Dict[int, Dict[str, Any]]:
    """Taxa-Record fields keyed by ID (Table 7a/7b, pp.33-35).

    Some fields switch unit on the value's sign; see `unit_convention` in the table file.
    """
    return {int(k): v for k, v in
            load_table("table_7a_taxa_biomass")["taxa_variables"].items()}


def taxa_code_ranges() -> Dict[str, Any]:
    """Value ranges for taxa variable 1 (Table 8, p.35) plus the PI-code note."""
    return load_table("table_7a_taxa_biomass")["taxa_code_ranges"]


def plankton_grouping_code() -> Dict[str, Any]:
    """The PGC and its multiply-and-truncate usage (Table 9, pp.35-36)."""
    return load_table("table_7a_taxa_biomass")["plankton_grouping_code"]


def classify_taxa_code(code: int) -> Dict[str, Any]:
    """Which Table 8 range a taxa variable number falls in, or an unassigned verdict."""
    for r in taxa_code_ranges()["ranges"]:
        if r["from"] <= code <= r["to"]:
            return {"code": code, "label": r["label"], "description": r["description"],
                    "range": [r["from"], r["to"]], "is_biomass": "Biomass" in r["label"],
                    "citation": "Table 8, WOD 2023 User's Manual p.35"}
    return {"code": code, "label": None,
            "description": "outside every Table 8 range; not a recognised WOD taxa code",
            "range": None, "is_biomass": False,
            "citation": "Table 8, WOD 2023 User's Manual p.35"}


# ------------------------------------------------------- online code tables
def missing_biological_fields(stated_ids) -> List[str]:
    """The key Table 6a fields a haul does not state, as `"<id> <name>"`.

    Key fields are `config.KEY_BIOLOGICAL_FIELD_IDS`; an alternative group is listed in full
    when none of it is stated.
    """
    from wod_etl.config import KEY_BIOLOGICAL_FIELD_IDS

    stated = {int(i) for i in stated_ids}
    names = biological_headers()
    out: List[str] = []
    for entry in KEY_BIOLOGICAL_FIELD_IDS:
        group = entry if isinstance(entry, tuple) else (entry,)
        if not stated & set(group):
            out += [f"{i} {names[i]['name']}" for i in group]
    return out


def code_table_links() -> Dict[str, Any]:
    """Every WOD online code-table link, plus the hub URLs.

    Rebuild with `uv run python -m wod_etl.reference.extract_code_table_links`.
    """
    return load_table("code_table_links")


def code_table_link(namespace: str, code: int) -> Optional[Dict[str, Any]]:
    """The published code list for one metadata field, or None if it has no coded values.

    `namespace` is 'secondary_header' (Table 4a), 'variable_specific_header' (5a),
    'biological_header' (6a), 'taxa_variable' (7a), 'primary_header' (6: country) or
    'character_data' (3: principal investigator). Returns e.g.
    `{"field": "s_29_probe_type", "name": "Probe Type", "html": ..., "csv": ...}`.
    """
    return code_table_links()["links"].get(namespace, {}).get(str(int(code)))


def code_table_hub(name: str) -> Optional[str]:
    """A named hub URL: 'all_wod_code_tables', 'country_codes', 'originator_flag_sets', ..."""
    return code_table_links()["hubs"].get(name)


# ----------------------------------------------------------------- Table 11
def range_check_variables() -> List[Dict[str, Any]]:
    """Which variables get range checks, and which appendix holds them (Table 11, p.54)."""
    return load_table("table_11_range_variables")["variables"]


# ----------------------------------------------------------------- Table 12
def qc_flag_scales() -> Dict[str, Dict[str, Any]]:
    """All five QC flag scales (Table 12, p.55).

    Keys: entire_cast, depth, observed_level, standard_level, biological.
    """
    return load_table("table_12_qc_flags")["scales"]


def observed_level_flags() -> Dict[int, str]:
    """Table 12 section 2(b): the scale for measured variables."""
    return {int(k): v for k, v in qc_flag_scales()["observed_level"]["flags"].items()}


def depth_flags() -> Dict[int, str]:
    """Table 12 section 2(a): the scale for the depth column."""
    return {int(k): v for k, v in qc_flag_scales()["depth"]["flags"].items()}


_STOPWORDS = {"or", "of", "in", "the", "a", "an", "and", "to", "than", "outside", "same",
              "less", "check", "checks", "value", "recorded"}


def flag_meanings_attr(scale: str) -> str:
    """CF `flag_meanings` string for a scale: one short token per flag, in flag order.

    The manual's prose is condensed to key words, e.g. 'duplicates_inversions_depth'.
    """
    import re as _re

    flags = qc_flag_scales()[scale]["flags"]
    tokens, seen = [], set()
    for code in sorted(flags, key=int):
        text = _re.sub(r"\(.*?\)", " ", flags[code]).lower()
        words = [w for w in _re.split(r"[^a-z0-9]+", text) if w and w not in _STOPWORDS]
        token = "_".join(words[:4]) or f"flag_{code}"
        while token in seen:
            token = f"{token}_{code}"
        seen.add(token)
        tokens.append(token)
    return " ".join(tokens)


def flag_values_attr(scale: str) -> List[int]:
    return sorted(int(k) for k in qc_flag_scales()[scale]["flags"])


# ----------------------------------------------------------------- Table 13
def gradient_factors() -> Dict[int, Dict[str, Any]]:
    """MGV / MIV / ZSI keyed by WOD variable code (Table 13, p.56)."""
    out: Dict[int, Dict[str, Any]] = {}
    for f in load_table("table_13_gradients")["factors"]:
        # 'Nitrate' and 'Nitrate + Nitrite' share code 8 with identical values.
        out.setdefault(f["wod_code"], f)
    return out


# ------------------------------------------------------------- Appendix 11
def range_levels() -> Dict[int, List[Tuple[float, float, float]]]:
    """Acceptable ranges as {code: [(depth_m, min, max), ...]} sorted by depth."""
    data = load_table("appendix_11_ranges")
    return {
        int(code): [(lv["depth_m"], lv["min"], lv["max"]) for lv in v["levels"]]
        for code, v in data["variables"].items()
    }
