"""Reshape tables whose layout is not one row per level and one column per variable.

Two shapes arrive often enough to need a primitive each, and both used to need a
hand-written script:

* **Long format** -- `Cruise, StNo, parm, depth, value`. The variable is named in a CELL,
  not a header, so every file looks like one 'value' column with no Table 3a variable in
  it. `pivot_long` turns the variable column into columns.
* **A matrix** -- taxa down the rows, stations (and a second key such as mesh size) across
  the columns, as plankton count sheets are typed. `melt_matrix` turns it into one row per
  cell, which is the shape a Taxa-Record needs.

    from wod_etl.staging import read_table, pivot_long, melt_matrix

    tables = [read_table(p)[:2] for p in files]
    cols, rows, info = pivot_long(tables, keys=["StNo", "depth"], variable="parm",
                                  value="value", key_aliases={"depth": ["dep"]})

Neither raises. Both return `(columns, rows, info)`, and `info` is where a conflict or a
silently-blank cell would otherwise disappear.
"""
import collections
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

Table = Tuple[List[str], List[List[str]]]


def _canonical_key(text: str) -> str:
    """'4.9690' and '4.969' are one depth; '01' and '1' one station. Text keys are stripped.

    Keys are joined as text, and two files that spell the same number differently would
    otherwise share no key at all -- an outer join that looks complete and is two disjoint
    blocks.
    """
    s = str(text).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    return repr(int(f)) if f.is_integer() else repr(f)


def _find_column(columns: List[str], name: str, aliases: Sequence[str] = ()) -> Optional[int]:
    wanted = [name, *aliases]
    for w in wanted:
        for i, c in enumerate(columns):
            if c.strip() == w:
                return i
    lowered = [w.lower() for w in wanted]
    for i, c in enumerate(columns):
        if c.strip().lower() in lowered:
            return i
    return None


def pivot_long(
    tables,
    keys: List[str],
    variable: str,
    value: str,
    *,
    key_aliases: Optional[Dict[str, List[str]]] = None,
    columns: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Pivot long-format rows -- one row per (keys, variable) -- into one row per key.

    `tables` is one `(columns, rows)` pair or a list of them: the common case is one file per
    variable, each in long format, as in

        Cruise,StNo,ObsItem,parm,dep,value        <- temperature file
        Cruise,StNo,ObsItem,parm,depth,value      <- salinity file: 'depth', not 'dep'

    `keys` names the columns that identify a level (`["StNo", "depth"]`); `key_aliases` gives
    their other spellings (`{"depth": ["dep"]}`). Numeric keys are compared as numbers, so
    `4.969` and `4.9690` meet.

    `columns` maps a variable as written in the cells to the column name to give it --
    **this is where the unit goes**, because unit detection reads only the column name:
    `{"Temperature": "Temperature (deg C)"}`. A variable not named here keeps its cell text.

    `info` reports, per variable, how many levels carry it and how many levels lack it;
    `conflicts` -- the same key and variable with two different values, which the pivot
    cannot resolve and does not guess (the first is kept, every one is listed); and
    `per_table` -- which key columns each table used. Read all three.
    """
    if tables and isinstance(tables, tuple) and len(tables) == 2 and \
            tables[0] and isinstance(tables[0][0], str):
        tables = [tables]
    aliases = key_aliases or {}
    names = columns or {}

    order: List[Tuple[str, ...]] = []
    shown: Dict[Tuple[str, ...], List[str]] = {}
    cells: Dict[Tuple[str, ...], Dict[str, str]] = {}
    variables: List[str] = []
    conflicts: List[Dict[str, Any]] = []
    repeats = 0
    per_table = []

    for t, (cols, rows) in enumerate(tables):
        cols = [str(c).strip() for c in cols]
        k_idx = [_find_column(cols, k, aliases.get(k, ())) for k in keys]
        v_idx = _find_column(cols, variable, aliases.get(variable, ()))
        x_idx = _find_column(cols, value, aliases.get(value, ()))
        missing = [k for k, i in zip(keys, k_idx) if i is None]
        if missing or v_idx is None or x_idx is None:
            per_table.append({"table": t, "error": f"missing column(s): "
                              f"{missing + ([variable] if v_idx is None else []) + ([value] if x_idx is None else [])}",
                              "columns": cols})
            continue
        seen_vars = collections.Counter()
        for r in rows:
            if max(k_idx + [v_idx, x_idx]) >= len(r):
                continue
            raw_key = [r[i].strip() for i in k_idx]
            if not all(raw_key):
                continue
            key = tuple(_canonical_key(k) for k in raw_key)
            var = names.get(r[v_idx].strip(), r[v_idx].strip())
            if not var:
                continue
            val = r[x_idx].strip()
            if key not in cells:
                cells[key] = {}
                order.append(key)
                shown[key] = raw_key
            if var not in variables:
                variables.append(var)
            held = cells[key].get(var)
            if held is not None:
                if held != val:
                    conflicts.append({"key": dict(zip(keys, raw_key)), "variable": var,
                                      "kept": held, "also": val, "table": t})
                else:
                    repeats += 1
                continue
            cells[key][var] = val
            seen_vars[var] += 1
        per_table.append({"table": t, "key_columns": [cols[i] for i in k_idx],
                          "variable_column": cols[v_idx], "value_column": cols[x_idx],
                          "rows": len(rows), "variables": dict(seen_vars)})

    out_cols = list(keys) + variables
    out_rows = [shown[k] + [cells[k].get(v, "") for v in variables] for k in order]
    lacking = {v: sum(1 for k in order if v not in cells[k]) for v in variables}
    return out_cols, out_rows, {
        "levels": len(order),
        "variables": {v: len(order) - lacking[v] for v in variables},
        "levels_missing_variable": {v: n for v, n in lacking.items() if n},
        "conflicts": conflicts,
        "identical_repeats": repeats,
        "per_table": per_table,
        "note": ("Two different values for the same level and variable: the first was "
                 "kept. Find out which is right before staging." if conflicts else None),
    }


def melt_matrix(
    rows: List[List[str]],
    *,
    header_rows: int,
    label_columns: List[str],
    header_names: List[str],
    fill_down: Sequence[str] = (),
    fill_across: bool = True,
    thousands: Optional[str] = ",",
    blank: str = "absent",
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Turn a matrix sheet into one row per cell.

    A plankton count sheet is typed as taxa down the rows and samples across the columns,
    with one or more header rows naming each sample:

        ,,,Station,1,,3,,                          <- header row 0: station, spans 2 cols
        Order,Family,Taxa,mesh size,"0,1mm",0.335mm,"0,1mm",0.335mm    <- header row 1
        Calanoida,Acartiidae,Acartia danae,adult,,8,,                  <- data
        Calanoida,,Acartia negligens,adult,100,16,,84

    `header_rows` is how many rows at the top are headers. `label_columns` names the leading
    columns that describe the row (`["Order", "Family", "Taxon", "Stage"]`) -- named here
    because the header text above them is usually a label for the header rows, not for the
    data. `header_names` names what each header row holds (`["Station", "Mesh"]`).

    `fill_across` carries a header value right over blank cells, which is how a merged
    spreadsheet cell exports (station 1 spans both of its mesh columns). `fill_down` names
    label columns whose blank means "same as the row above" -- a Family written once per
    group. `thousands` strips a thousands separator from values (`"1,700"`).

    **A blank value cell is not a zero and not a missing measurement**: in a count sheet it
    usually means the taxon was not found in that sample. `blank="absent"` (the default)
    writes no row for it and counts it in `info["blank_cells"]`; `blank="zero"` writes 0,
    which is a claim the originator must have made. Decide from the readme, and say which.
    """
    if len(rows) <= header_rows:
        return [], [], {"error": f"only {len(rows)} row(s); expected {header_rows} header "
                                 f"row(s) and data"}
    width = max(len(r) for r in rows)
    pad = lambda r: list(r) + [""] * (width - len(r))
    n_labels = len(label_columns)

    headers = []
    for h in range(header_rows):
        row = pad(rows[h])[n_labels:]
        if fill_across:
            last = ""
            for i, v in enumerate(row):
                if v.strip():
                    last = v.strip()
                else:
                    row[i] = last
        headers.append([v.strip() for v in row])

    out_cols = list(label_columns) + list(header_names) + ["value"]
    out_rows: List[List[str]] = []
    info: Dict[str, Any] = {"data_rows": 0, "cells_written": 0, "blank_cells": 0,
                            "non_numeric_cells": [], "blank_policy": blank,
                            "samples": []}
    carried = {c: "" for c in fill_down}
    fill_idx = {c: label_columns.index(c) for c in fill_down if c in label_columns}
    samples = []
    for j in range(width - n_labels):
        samples.append(tuple(headers[h][j] for h in range(header_rows)))
    info["samples"] = [dict(zip(header_names, s)) for s in dict.fromkeys(samples)]

    for r in rows[header_rows:]:
        r = pad(r)
        labels = [c.strip() for c in r[:n_labels]]
        if not any(labels) and not any(c.strip() for c in r[n_labels:]):
            continue
        for name, i in fill_idx.items():
            if labels[i]:
                carried[name] = labels[i]
            else:
                labels[i] = carried[name]
        info["data_rows"] += 1
        for j, cell in enumerate(r[n_labels:]):
            v = cell.strip()
            # A leading group of 0 is never thousands grouping: '0,335' is a decimal comma
            # (these sheets head columns '0,1mm'), and stripping it wrote 335 for 0.335.
            if thousands and re.fullmatch(
                    rf"[1-9]\d{{0,2}}(?:{re.escape(thousands)}\d{{3}})+(?:\.\d+)?", v):
                v = v.replace(thousands, "")
            if not v:
                info["blank_cells"] += 1
                if blank == "zero":
                    v = "0"
                else:
                    continue
            try:
                float(v)
            except ValueError:
                info["non_numeric_cells"].append({"labels": labels, "sample": samples[j],
                                                  "value": v})
            out_rows.append(labels + list(samples[j]) + [v])
            info["cells_written"] += 1
    if blank == "absent":
        info["note"] = (f"{info['blank_cells']} blank cell(s) were read as 'not found in "
                        f"this sample' and written as no row. If the readme says a blank is "
                        f"'not counted', that is different -- say which.")
    return out_cols, out_rows, info
