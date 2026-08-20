"""Single loading point for every WOD reference table.

Before this module existed the tables were read in three places and two of them
(table_12_qc_flags.json, table_11_range_variables.json) were not read at all -- the QC flag
meanings were transcribed by hand into two separate modules instead. That is exactly the
drift this project exists to avoid, so all access goes through here.
"""
import functools
import json
from typing import Any, Dict, List, Tuple

from wod_etl.config import CODE_TABLES_DIR

TABLE_FILES = {
    "table_2_datasets": "table_2_datasets.json",
    "table_3a_variables": "table_3a_variables.json",
    "table_3b_unit_constants": "table_3b_unit_constants.json",
    "table_4a_secondary_headers": "table_4a_secondary_headers.json",
    "table_11_range_variables": "table_11_range_variables.json",
    "table_12_qc_flags": "table_12_qc_flags.json",
    "table_13_gradients": "table_13_gradients.json",
    "appendix_11_ranges": "appendix_11_ranges.json",
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
    """Every table with its title and manual page, for an agent orienting itself."""
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
    """Table 12 section 2(b) -- the scale used for measured variables."""
    return {int(k): v for k, v in qc_flag_scales()["observed_level"]["flags"].items()}


def depth_flags() -> Dict[int, str]:
    """Table 12 section 2(a) -- the scale used for the depth column."""
    return {int(k): v for k, v in qc_flag_scales()["depth"]["flags"].items()}


_STOPWORDS = {"or", "of", "in", "the", "a", "an", "and", "to", "than", "outside", "same",
              "less", "check", "checks", "value", "recorded"}


def flag_meanings_attr(scale: str) -> str:
    """CF `flag_meanings` string for a scale: one short token per flag, in flag order.

    CF requires space-separated tokens matching flag_values one-for-one. The manual's
    wording is prose, so it is condensed to the words that carry the meaning -- 'duplicates
    or inversions in recorded depth (same or less than previous depth)' becomes
    'duplicates_inversions_depth'.
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
