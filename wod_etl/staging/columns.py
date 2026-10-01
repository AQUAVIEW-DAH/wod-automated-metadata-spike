"""Renaming staged columns without changing what they mean.

A column name in this pipeline is not a label -- it is the only carrier of three separate
facts. `match_variable` reads it for the WOD variable code, `extract_unit` reads it for the
originator's unit, and `extract_scale` reads it for the measurement scale. Nothing else
supplies any of the three. So a rename is a small edit with three ways to silently destroy
data, and every one of them still validates:

    "Temp (deg F)"              -> "Temperature"      the unit is gone; the values are
                                                      then read as already-Celsius
    "Temperature [ITS-90, C]"   -> "Temperature (C)"  the scale is gone; variable specific
                                                      second header 3 is lost
    "t090C"                     -> "Theta"            code 1 becomes no code at all

This module renames only a `columns` list on its way to `write_staged_cast`, never
`data/raw/`, and refuses by default any rename that changes one of those three facts.

    from wod_etl.staging import rename_columns
    cols, audit = rename_columns(cols, {"latitude": "Latitude"})

Renaming is not aliasing, and the two must not be confused. An alias in
`wod_etl.mapping.query_wod_spec.VARIABLE_ALIASES` teaches the whole pipeline that `T090C`
means Temperature, applies to every accession forever, and touches no data. A rename edits
one staged copy of one file. When a vendor's name will recur, add the alias; when this
file's header is wrong or missing, rename.
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
    """Rename columns in a staged header, preserving what the name has to carry.

    Keys may be an exact column name or a human's name for one (`"latitude"`), resolved
    with `resolve_column`, so the caller need not know the column is spelt `LAT`.

        cols, audit = rename_columns(["LAT", "LON", "Depth (m)"],
                                     {"latitude": "Latitude"})
        # (["Latitude", "LON", "Depth (m)"], {...})

    Four checks per rename, each computed by the function that consumes the name
    downstream, so a refusal means the same thing the pipeline would have meant:

    - the WOD variable code must be unchanged (`match_variable`)
    - the unit must be unchanged when `require_unit` (`extract_unit`)
    - the measurement scale must be unchanged (`extract_scale`)
    - the new name must not collide with another column, or with another rename

    `strict=True` (the default) returns the columns **unchanged** if any rename fails, so a
    bad rename cannot reach `write_staged_cast`. `strict=False` applies what it can and
    sets `applied_with_problems`; that is for naming `col_0 ... col_N` from a format
    description, where there is no old code, unit or scale to preserve.

    `audit["renames"]` explains every one, applied or refused, with the fix where there is
    one. Put it in your report: a renamed column is a judgement about the source.
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
                         f"column means, so nothing was applied. Fix them, or pass "
                         f"strict=False if you accept the loss and will record it.")
        return original, audit

    # strict=False applies the judgement calls but never the structural ones: an
    # unresolved query has no column to rename, and a collision would leave two columns
    # with one name, which loses one of them without saying so.
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

    # Only a change AWAY from a known code is a loss. `col_0` -> `Depth (m)` goes from no
    # code to code 0, which is the whole point of naming a headerless fixed-width column,
    # and refusing it would make the one rename the pipeline most needs the one that
    # requires an escape hatch.
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

    # A collision is corruption, not a judgement: two columns under one name means one is
    # lost with nothing said. It stays blocking even under strict=False.
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
    """Per column: the WOD variable it resolves to, and a canonical name if one is safe.

    A **proposal only.** Nothing here is applied, and it deliberately does not offer a
    canonical name for every column, because canonicalising is not always safe:

    - **Sensor-indexed pairs collide.** `t090C` and `t190C` are the primary and secondary
      temperature sensor and both resolve to code 1. There is no one canonical name that
      keeps both, so they are reported under `collisions` and no rename is suggested.
    - **A canonical name changes no output.** The NetCDF variable name comes from the WOD
      code via `wod_name`, not from the column name, so renaming is for the legibility of
      the staged file -- not a precondition for conversion.

    Pass `units` (a units row read from beneath the header) to have each suggestion carry
    its unit, which is the one rename that genuinely changes behaviour: it makes the unit
    visible to detection and conversion.
    """
    units = list(units or [])
    by_code: Dict[int, List[str]] = {}
    out: List[Dict[str, Any]] = []

    # Index into `columns`, never into a filtered copy. A units row is positionally
    # aligned to the original header, so dropping blank columns first shifts every unit
    # after the blank onto the wrong column -- and a wrong unit folded into a name is the
    # exact corruption this module exists to prevent, with nothing downstream to catch it:
    # `rename_columns` only guards a unit that was already there. `fold_unit_row` walks
    # the header the same way for the same reason.
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
                            f"{collisions[entry['wod_code']]} -- a primary and secondary "
                            f"sensor. No single canonical name keeps both.")

    return {"columns": out,
            "collisions": {str(k): v for k, v in collisions.items()},
            "unmapped": [e["column"] for e in out if e["wod_code"] is None],
            "note": "Suggestions only. Apply with rename_columns, which re-checks each "
                    "one against the code, unit and scale before writing."}
