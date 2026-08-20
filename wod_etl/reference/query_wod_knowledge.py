"""Ask the WOD reference tables a question and get an answer with its page citation.

The pipeline reads the tables directly through load_tables. This module is for the *agent*:
one call that resolves a code, name, flag, dataset or threshold and says which page of the
manual it came from, so an answer can be checked rather than trusted.

    query_wod_knowledge("temperature")     -> variable code 1, unit, datasets, page 18
    query_wod_knowledge(6, kind="flag")    -> "failed range and inversion checks", page 55
    query_wod_knowledge("probe type")      -> secondary header 29, page 23
"""
import re
from typing import Any, Dict, List, Optional, Union

from wod_etl.config import CODE_TABLES_DIR, WOD_README_PDF
from wod_etl.reference.load_tables import (
    available_tables,
    depth_flags,
    gradient_factors,
    load_table,
    observed_level_flags,
    qc_flag_scales,
    range_check_variables,
    range_levels,
    unit_constants,
    wod_datasets,
    wod_secondary_headers,
    wod_variables,
)

KINDS = ("variable", "dataset", "secondary_header", "flag", "gradient", "range", "constant")


def query_wod_knowledge(
    query: Union[str, int],
    kind: Optional[str] = None,
    depth_m: Optional[float] = None,
) -> Dict[str, Any]:
    """Look up WOD reference knowledge.

    query  -- a variable name or code, dataset name, header name or id, or flag value
    kind   -- restrict the search to one of KINDS; omit to search all of them
    depth_m-- for kind='range' or 'gradient', the depth the thresholds apply at

    Returns {"query", "matches": [...], "match_count", "sources"}. Every match carries a
    `citation` naming the table and manual page.
    """
    kinds = (kind,) if kind else KINDS
    if kind and kind not in KINDS:
        return {"query": query, "error": f"unknown kind '{kind}'. Valid: {', '.join(KINDS)}",
                "matches": [], "match_count": 0}

    matches: List[Dict[str, Any]] = []
    for k in kinds:
        matches.extend(_LOOKUPS[k](query, depth_m))

    return {
        "query": query,
        "kind": kind,
        "match_count": len(matches),
        "matches": matches,
        "sources": sorted({m["citation"] for m in matches}),
        "manual": str(WOD_README_PDF),
    }


# ---------------------------------------------------------------- lookups
def _variables(q, _depth) -> List[Dict[str, Any]]:
    out = []
    for code, v in wod_variables().items():
        if _hit(q, code, v["name"]):
            out.append({
                "kind": "variable", "wod_code": code, "name": v["name"],
                "standard_unit": v["standard_unit"], "unit_abbrev": v["unit_abbrev"],
                "datasets": v["datasets"],
                "citation": "Table 3a, WOD 2023 User's Manual p.18",
            })
    return out


def _datasets(q, _depth) -> List[Dict[str, Any]]:
    out = []
    for name, desc in wod_datasets().items():
        if _hit(q, None, name) or _hit(q, None, desc):
            out.append({"kind": "dataset", "code": name, "description": desc,
                        "citation": "Table 2, WOD 2023 User's Manual p.17"})
    if isinstance(q, str) and q.upper().strip() in ("XCTD", "STD", "XSTD") and not out:
        out.append({
            "kind": "dataset", "code": None, "description": None,
            "note": f"{q.upper().strip()} is not a WOD dataset. High-resolution "
                    f"(depth increment < 2 m) goes to CTD; low-resolution goes to OSD.",
            "citation": "WOD 2023 User's Manual p.19",
        })
    return out


def _secondary_headers(q, _depth) -> List[Dict[str, Any]]:
    return [
        {"kind": "secondary_header", "id": sid, "description": desc,
         "csv_field": f"s_{sid}_{re.sub(r'[^a-z0-9]+', '_', desc.lower()).strip('_')[:30]}",
         "citation": "Table 4a, WOD 2023 User's Manual p.23"}
        for sid, desc in wod_secondary_headers().items()
        if _hit(q, sid, desc)
    ]


def _flags(q, _depth) -> List[Dict[str, Any]]:
    out = []
    for scale_name, scale in qc_flag_scales().items():
        for value, meaning in scale["flags"].items():
            if _hit(q, int(value), meaning):
                out.append({
                    "kind": "flag", "scale": scale_name, "value": int(value),
                    "meaning": meaning, "applies_to": scale.get("applies_to"),
                    "citation": "Table 12, WOD 2023 User's Manual p.55",
                })
    return out


def _gradients(q, depth_m) -> List[Dict[str, Any]]:
    out = []
    for code, f in gradient_factors().items():
        if not _hit(q, code, f["variable"]):
            continue
        entry = {"kind": "gradient", "wod_code": code, "variable": f["variable"],
                 "shallow_z_lt_400m": f["shallow_z_lt_400m"],
                 "deep_z_gt_400m": f["deep_z_gt_400m"], "ZSI": f["ZSI"],
                 "citation": "Table 13, WOD 2023 User's Manual p.56"}
        if depth_m is not None:
            band = "deep_z_gt_400m" if depth_m > 400 else "shallow_z_lt_400m"
            entry["applicable_at_depth"] = {"depth_m": depth_m, **f[band]}
        out.append(entry)
    return out


def _ranges(q, depth_m) -> List[Dict[str, Any]]:
    out = []
    levels = range_levels()
    for v in range_check_variables():
        code = v["code"]
        if not _hit(q, code, v["variable"]) or code not in levels:
            continue
        entry = {"kind": "range", "wod_code": code, "variable": v["variable"],
                 "unit": v["unit"], "appendix": v["appendix"],
                 "level_count": len(levels[code]),
                 "citation": f"Appendix {v['appendix']}, WOD 2023 User's Manual pp.87-113",
                 "caveat": "union across all 30 ocean basins; basin-specific bounds need "
                           "range_area.msk, which is not in the PDF"}
        if depth_m is not None:
            from wod_etl.qc_transforms.apply_qc_auditing import range_at_depth
            bounds = range_at_depth(code, depth_m)
            if bounds:
                entry["applicable_at_depth"] = {"depth_m": depth_m,
                                                "min": bounds[0], "max": bounds[1]}
        out.append(entry)
    return out


def _constants(q, _depth) -> List[Dict[str, Any]]:
    data = unit_constants()
    out = []
    if _hit(q, None, "molar volume oxygen o2"):
        out.append({"kind": "constant", "name": "molar volume of O2 at STP",
                    "value": data["molar_volume_o2_stp_l_per_mole"], "units": "l/mol",
                    "citation": "Table 3b, WOD 2023 User's Manual p.19"})
    for element, weight in data["atomic_weights_g_per_mole"].items():
        if _hit(q, None, f"{element} atomic weight"):
            out.append({"kind": "constant", "name": f"atomic weight of {element}",
                        "value": weight, "units": "g/mol",
                        "citation": "Table 3b, WOD 2023 User's Manual p.19"})
    return out


_LOOKUPS = {
    "variable": _variables,
    "dataset": _datasets,
    "secondary_header": _secondary_headers,
    "flag": _flags,
    "gradient": _gradients,
    "range": _ranges,
    "constant": _constants,
}


def _hit(query, code: Optional[int], text: str) -> bool:
    """Match a numeric code exactly, or a word of the query against the text."""
    if isinstance(query, int) or (isinstance(query, str) and query.strip().isdigit()):
        return code is not None and int(query) == code
    q = str(query).lower().strip()
    if not q:
        return False
    t = str(text).lower()
    if q in t:
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", q) if len(w) > 2]
    return bool(words) and all(w in t for w in words)


# ------------------------------------------------------------ full text
def search_manual(term: str, max_hits: int = 8, context_chars: int = 240) -> Dict[str, Any]:
    """Keyword search over the manual's prose, for anything not in a table.

    Requires the extracted page text cache. Build it with:
        uv run python -m wod_etl.reference.index_manual
    """
    from wod_etl.reference.index_manual import load_page_cache

    pages = load_page_cache()
    if pages is None:
        return {"term": term, "error":
                "page text cache not built. Run: uv run python -m wod_etl.reference.index_manual",
                "hits": [], "hit_count": 0}

    hits = []
    needle = term.lower()
    for page_no, text in enumerate(pages, start=1):
        low = text.lower()
        start = low.find(needle)
        if start == -1:
            continue
        a = max(0, start - context_chars // 2)
        excerpt = " ".join(text[a:a + context_chars].split())
        hits.append({"page": page_no, "excerpt": excerpt,
                     "citation": f"WOD 2023 User's Manual p.{page_no}"})
        if len(hits) >= max_hits:
            break

    return {"term": term, "hit_count": len(hits), "hits": hits,
            "note": "page numbers are PDF pages; read them with the Read tool on "
                    f"{WOD_README_PDF}"}


if __name__ == "__main__":
    import json
    print("### tables available ###")
    print(json.dumps(available_tables(), indent=2))
    for q, k in [("temperature", "variable"), (6, "flag"), ("probe type", "secondary_header"),
                 ("XCTD", "dataset")]:
        r = query_wod_knowledge(q, kind=k)
        print(f"\n### query_wod_knowledge({q!r}, kind={k!r}) -> {r['match_count']} match ###")
        print(json.dumps(r["matches"][:1], indent=2))
