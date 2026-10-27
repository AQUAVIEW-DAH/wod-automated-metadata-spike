"""Rename staged columns without changing the WOD code, unit or scale the name carries.

A column name is the only source of its variable code, unit and scale, so a rename that
drops any of them is refused by default. A vendor name that recurs belongs in
`VARIABLE_ALIASES` instead.
"""
from typing import Any, Dict, List, Optional, Tuple

from wod_etl.mapping.query_wod_spec import (extract_scale, extract_unit, match_variable,
                                            resolve_column)


def rename_columns(
    columns: List[str],
    renames: Dict[str, str],
    *,
    require_unit: bool = True,
    strict: bool = True,
) -> Tuple[List[str], Dict[str, Any]]:
    """Rename columns in a staged header, keeping the code, unit and scale each name carries.

    Keys may be an exact column name or a descriptive one (`"latitude"`), resolved with
    `resolve_column`. Each rename must keep the WOD code, the unit (when `require_unit`)
    and the scale, and must not collide with another column.

    `strict=True` returns the columns unchanged if any rename fails; `strict=False` applies
    all non-colliding renames and sets `applied_with_problems`. `audit["renames"]` records
    every rename, applied or refused.
    """
    original = list(columns)
    entries: List[Dict[str, Any]] = []
    claimed: Dict[str, str] = {}          # every rename planned, for collision checking

    for query, new_name in (renames or {}).items():
        entry = _plan_one(query, new_name, original, claimed, require_unit)
        entries.append(entry)
        if not entry["blocking"]:
            claimed[entry["from"]] = new_name

    failed = [e for e in entries if not e["ok"]]
    blocked = [e for e in entries if e["blocking"]]
    audit: Dict[str, Any] = {
        "renames": entries,
        "applied": {},
        "refused": [e["from"] or e["query"] for e in failed],
        "blocked": [e["from"] or e["query"] for e in blocked],
        "applied_with_problems": False,
        "problems": [p for e in failed for p in e["problems"]],
    }

    if failed and strict:
        audit["note"] = (f"{len(failed)} of {len(entries)} rename(s) would change what a "
                         f"column means, so nothing was applied; strict=False applies "
                         f"them anyway.")
        return original, audit

    # Unresolved queries and collisions are never applied, even with strict=False.
    applied = {e["from"]: e["to"] for e in entries if not e["blocking"]}
    out = [applied.get(c, c) for c in original]
    audit["applied"] = applied
    audit["applied_with_problems"] = bool(failed)
    if blocked:
        audit["note"] = (f"{len(blocked)} rename(s) could not be applied at all "
                         f"(no such column, or a name collision) and were skipped even "
                         f"with strict=False.")
    return out, audit


def _plan_one(query: str, new_name: str, columns: List[str],
              already: Dict[str, str], require_unit: bool) -> Dict[str, Any]:
    """Resolve one rename and check it. Never mutates anything."""
    entry: Dict[str, Any] = {"query": query, "from": None, "to": new_name,
                             "resolved_by": None, "ok": False, "blocking": True,
                             "problems": []}

    if not new_name or not str(new_name).strip():
        entry["problems"].append(f"the new name for {query!r} is empty")
        return entry

    if query in columns:
        old, entry["resolved_by"] = query, "exact"
    else:
        hit = resolve_column(query, columns)
        if hit["column"] is None:
            entry["problems"].append(hit["note"] or f"no column matches {query!r}")
            return entry
        old, entry["resolved_by"] = hit["column"], hit["matched_by"]
    entry["from"] = old

    if old in already:
        entry["problems"].append(f"{old!r} is already being renamed to "
                                 f"{already[old]!r} in this call")
        return entry

    code_before, code_after = match_variable(old), match_variable(new_name)
    unit_before, unit_after = extract_unit(old), extract_unit(new_name)
    scale_before, scale_after = extract_scale(old), extract_scale(new_name)
    entry.update({"wod_code_before": code_before, "wod_code_after": code_after,
                  "unit_before": unit_before, "unit_after": unit_after,
                  "scale_before": scale_before, "scale_after": scale_after})

    # Only losing a known code is a problem; naming `col_0` -> `Depth (m)` gains one.
    if code_before is not None and code_before != code_after:
        entry["problems"].append(
            f"{old!r} resolves to WOD code {code_before} but {new_name!r} resolves to "
            f"{code_after}. A rename must not change which variable a column is; add an "
            f"alias to VARIABLE_ALIASES if the pipeline should learn this name.")

    if require_unit and unit_before and unit_after != unit_before:
        suggestion = f"{new_name} ({unit_before})"
        entry["problems"].append(
            f"{old!r} carries the unit {unit_before!r} and {new_name!r} carries "
            f"{unit_after!r}. Unit detection reads the column name and nothing else, so "
            f"the values would be taken as already converted. Use {suggestion!r}.")

    if scale_before and scale_after != scale_before:
        entry["problems"].append(
            f"{old!r} states the scale {scale_before!r} and {new_name!r} does not. The "
            f"scale is variable specific second header 3 and a temperature is ambiguous "
            f"without it. Keep it in the name.")

    # A duplicate name loses a column, so a collision blocks even under strict=False.
    taken = {c for c in columns if c != old} | set(already.values())
    if new_name in taken:
        entry["problems"].append(f"{new_name!r} is already the name of another column; "
                                 f"a duplicate header loses one of them silently")
        return entry

    entry["blocking"] = False
    entry["ok"] = not entry["problems"]
    return entry


def suggest_column_names(columns: List[str],
                         units: Optional[List[str]] = None) -> Dict[str, Any]:
    """Per column: the WOD code it resolves to and a suggested name. Nothing is applied.

    Columns sharing a code (e.g. primary and secondary sensors) are reported under
    `collisions`. Pass `units` (a units row below the header) to suggest folding each unit
    into its column name so unit detection can see it.
    """
    units = list(units or [])
    by_code: Dict[int, List[str]] = {}
    out: List[Dict[str, Any]] = []

    # The units row aligns with the header by position, blanks included.
    for i, col in enumerate(columns):
        if not col or not col.strip():
            continue
        code = match_variable(col)
        unit = extract_unit(col) or (units[i].strip() if i < len(units) else None) or None
        entry: Dict[str, Any] = {"column": col, "wod_code": code,
                                 "unit": unit, "suggested": None, "why": None}
        if code is not None:
            by_code.setdefault(code, []).append(col)
        if not extract_unit(col) and unit:
            entry["suggested"] = f"{col} ({unit})"
            entry["why"] = ("the unit is not in the name, so unit detection cannot see "
                            "it; folding it in is what makes the conversion correct")
        out.append(entry)

    collisions = {code: names for code, names in by_code.items() if len(names) > 1}
    for entry in out:
        if entry["wod_code"] in collisions and entry["suggested"] is None:
            entry["why"] = (f"WOD code {entry['wod_code']} is claimed by "
                            f"{collisions[entry['wod_code']]}, a primary and secondary "
                            f"sensor. No single canonical name keeps both.")

    return {"columns": out,
            "collisions": {str(k): v for k, v in collisions.items()},
            "unmapped": [e["column"] for e in out if e["wod_code"] is None],
            "note": "Suggestions only. Apply with rename_columns, which re-checks each "
                    "one against the code, unit and scale before writing."}
