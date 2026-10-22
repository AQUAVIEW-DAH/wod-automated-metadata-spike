"""Map a staged haul to WOD Taxa-Record codes: taxa, life stage, sex, collection fields.

Taxon names are resolved against WOD's taxa list (Table 7a field 1: an ITIS number or a
negative WOD code). A name resolved at a higher rank is flagged `matched_higher_rank`; the
originator's name is always kept.
"""
import re
from typing import Any, Dict, Optional

from wod_etl.mapping.generate_field_mapping import parse_header_block
from wod_etl.mapping.map_metadata import map_metadata
from wod_etl.reference.code_lists import describe_lookup, lookup_code

# Suffixes meaning "species not determined": the name is the genus.
_GENUS_ONLY = re.compile(r"\s+(?:spp?|sp|cf|aff|indet|n\.?\s*sp)\.?\s*$", re.I)
_MISSING = {"", "na", "n/a", "nan", "none", "-", "--", "unknown", "?"}


def _clean(name: Any) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    return "" if s.lower() in _MISSING else s


def resolve_taxon(name: Any) -> Dict[str, Any]:
    """`{"originator_name", "status", "code", "pgc", "rank", "label", "query", "note"}`."""
    original = _clean(name)
    out: Dict[str, Any] = {"originator_name": original, "status": "no_name", "code": None,
                           "pgc": None, "rank": None, "label": None, "query": None,
                           "note": None}
    if not original:
        return out

    query = _GENUS_ONLY.sub("", original).strip()
    tried = [(query, None)]
    for fallback in _higher_rank_candidates(query):
        tried.append((fallback, f"{original!r} is not in the WOD taxa list; resolved to "
                                f"{fallback!r}, a higher rank. The originator's name is kept."))

    last = None
    for q, note in tried:
        r = lookup_code("taxa_variable", 1, q)
        last = r
        if r["status"] == "matched":
            out.update(status="matched" if note is None else "matched_higher_rank",
                       code=int(r["code"]), label=r.get("label"), query=q,
                       pgc=_int(r.get("PGC code")), rank=r.get("Rank"), note=note)
            if note is None and query != original:
                out["note"] = f"written as {original!r}: genus level, species not determined"
            return out
        if r["status"] in ("ambiguous", "unavailable", "disabled"):
            break
    out.update(status=last["status"] if last else "no_match", query=query,
               note=describe_lookup(last) if last else None,
               candidates=(last or {}).get("candidates") or (last or {}).get("possible"))
    return out


def _higher_rank_candidates(query: str):
    """The name to fall back to when the full name is not in the list.

    Usually the genus (first word). If a later word is capitalised ('Zoea of Decapoda'), that
    word is the taxon. Parenthesised qualifiers are ignored.
    """
    words = re.sub(r"\(.*?\)", " ", query).split()
    if len(words) < 2:
        return []
    later_caps = [w for w in words[1:] if w[:1].isupper()]
    if later_caps:
        return [later_caps[-1]]
    return [words[0]]


def resolve_taxa_coded(field_id: int, value: Any) -> Dict[str, Any]:
    """A Table 7a coded qualifier -- life stage (5), sex (6), present (7)... -- or None."""
    text = _clean(value)
    if not text:
        # Same keys as a looked-up result; callers read them unconditionally.
        return {"status": "no_value", "code": None, "originator_text": None, "label": None,
                "summary": f"no value ({str(value).strip()!r} reads as missing)"}
    r = lookup_code("taxa_variable", field_id, text)
    return {"status": r["status"], "code": _int(r.get("code")) if r["status"] in
            ("matched", "already_a_code") else None,
            "originator_text": text, "label": r.get("label"),
            "summary": describe_lookup(r)}


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip().rstrip("."))
    except (TypeError, ValueError):
        return None


# Table 7a qualifiers that take a code from a published list.
_CODED_RECORD_FIELDS = (5, 6, 7, 8, 9, 16, 17, 26)


def map_haul(staged: Dict[str, Any]) -> Dict[str, Any]:
    """One staged haul as the writer needs it: contract, biological header, records.

    `staged` comes from `read_staged_haul`. The contract has the same keys as a profile's,
    so the writers share helpers.
    """
    parsed = parse_header_block(staged["header_text"])
    meta = map_metadata(staged["header_text"], columns=[])
    contract = {
        "source": staged.get("source", ""),
        "secondary_headers": meta["secondary_headers"],
        "character_data": meta.get("character_data", {}),
        "parsed_coordinates": parsed["coordinates"],
        "provenance_notes": parsed["notes"],
        "source_files": parsed["sources"],
        "unresolved_metadata": meta.get("unresolved", []),
    }

    bio = {}
    for fid, entry in staged["biological_header"].items():
        entry = dict(entry)
        if entry.get("appendix"):
            r = lookup_code("biological_header", fid, entry["value"])
            entry["code_lookup"] = {**{k: r.get(k) for k in ("status", "code", "label",
                                                             "candidates", "possible")},
                                    "summary": describe_lookup(r)}
            if r["status"] == "matched":
                entry["originator_value"], entry["value"] = entry["value"], r["code"]
        bio[fid] = entry

    taxa_cache: Dict[str, Dict[str, Any]] = {}
    coded_cache: Dict[Any, Dict[str, Any]] = {}
    records = []
    for row in staged["rows"]:
        cells = {f: (row[i].strip() if i < len(row) else "")
                 for i, f in enumerate(staged["fields"]) if f is not None}
        name = cells.pop(1, "")
        if name not in taxa_cache:
            taxa_cache[name] = resolve_taxon(name)
        coded = {}
        for fid in _CODED_RECORD_FIELDS:
            if cells.get(fid):
                key = (fid, cells[fid].lower())
                if key not in coded_cache:
                    coded_cache[key] = resolve_taxa_coded(fid, cells[fid])
                coded[fid] = coded_cache[key]
        records.append({"taxon": taxa_cache[name],
                        "fields": {f: v for f, v in cells.items() if v != ""},
                        "coded": coded})
    return {"contract": contract, "biological_header": bio, "records": records,
            "taxa": taxa_cache, "unmapped_columns": staged["unmapped_columns"],
            "life_stages": {k[1]: v for k, v in coded_cache.items() if k[0] == 5}}
