"""Reading originator tables that pandas cannot open as they stand."""
import collections
import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wod_etl.config import NA_SENTINELS, TEXT_ENCODINGS


def read_table(
    path,
    header_row: int = 0,
    encoding: Optional[str] = None,
    drop_blank_rows: bool = True,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Read a delimited originator file as (columns, rows, info). Never raises on encoding.

    Handles three things that stop `load_tabular` before it starts:

    - **Encoding.** Tries `config.TEXT_ENCODINGS` in order. Japanese-authored CSVs are
      commonly cp932 and raise UnicodeDecodeError on utf-8.
    - **Blank rows.** Files written with \\r\\r\\n interleave an empty row between every
      real one, which shifts the header and doubles the row count.
    - **A title row above the header.** `header_row` is an index into the *non-blank*
      rows, so a file whose first line is a dataset title uses `header_row=1`.

    `info` reports the encoding used, the row counts before and after cleaning, and any
    duplicate column names -- all of which change how the file should be read.
    """
    path = Path(path)
    encodings = (encoding,) if encoding else TEXT_ENCODINGS
    text = None
    used = None
    for enc in encodings:
        try:
            text = path.read_text(encoding=enc)
            used = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = path.read_text(encoding="latin-1", errors="replace")
        used = "latin-1 (lossy)"

    raw = list(csv.reader(text.splitlines()))
    kept = [r for r in raw if any(c.strip() for c in r)] if drop_blank_rows else raw
    if header_row >= len(kept):
        return [], [], {"error": f"header_row {header_row} is past the end of the file "
                                 f"({len(kept)} non-blank rows)", "encoding": used}

    columns = [c.strip() for c in kept[header_row]]
    rows = kept[header_row + 1:]
    dupes = [c for c, n in collections.Counter(c for c in columns if c).items() if n > 1]
    return columns, rows, {
        "encoding": used,
        "rows_in_file": len(raw),
        "blank_rows_dropped": len(raw) - len(kept),
        "data_rows": len(rows),
        "duplicate_columns": dupes,
    }


def clean_sentinels(
    rows: List[List[str]],
    columns: List[str],
    extra: Optional[Dict[str, str]] = None,
) -> Tuple[List[List[str]], Dict[str, int]]:
    """Replace originator missing-value codes with empty fields, and count every one.

    Returns `(rows, counts)` where counts is keyed `"<column> :: <code>"`. Report those
    counts. Two of the codes are not really missing data:

    - **below detection limit** and **not detected** mean the value lies under a stated
      threshold. That is real information, but WOD Table 12 has no observed-level flag
      for it, so it still becomes missing. Say how much was lost this way.
    - **not sampled** and **not analysed** are genuinely absent.

    Substituting the detection limit for a below-detection marker would fabricate a
    measurement; this deliberately does not do that.
    """
    codes = dict(NA_SENTINELS)
    codes.update(extra or {})
    counts: Dict[str, int] = collections.Counter()
    out = []
    for row in rows:
        new = list(row)
        for i, cell in enumerate(new):
            v = cell.strip()
            if v in codes:
                counts[f"{columns[i] if i < len(columns) else i} :: {v}"] += 1
                new[i] = ""
        out.append(new)
    return out, dict(counts)


def group_rows(
    rows: List[List[str]],
    columns: List[str],
    key_columns: List[str],
) -> "collections.OrderedDict":
    """Split one cruise-wide table into per-cast groups, preserving file order.

    A single table holding every station is the normal shape for bottle, nutrient and
    plankton submissions. Group by whatever identifies a cast -- usually station, and
    station plus date when a station was occupied more than once.

    Rows whose key is entirely blank come back under the key `()`. That is not a
    sufficient filter for footer junk: sheets often end with a legend block whose text
    sits *in* the key column, so it groups as if it were a station. Drop a group when its
    position will not parse -- a legend line has no coordinates -- and log what was
    dropped rather than discarding it silently.
    """
    idx = {c: columns.index(c) for c in key_columns if c in columns}
    missing = [c for c in key_columns if c not in idx]
    if missing:
        raise KeyError(f"key column(s) not in table: {missing}; have {columns}")
    groups: "collections.OrderedDict" = collections.OrderedDict()
    for row in rows:
        key = tuple((row[idx[c]].strip() if idx[c] < len(row) else "") for c in key_columns)
        if not any(key):
            key = ()
        groups.setdefault(key, []).append(row)
    return groups


def fold_unit_row(
    columns: List[str],
    units: List[str],
) -> List[str]:
    """Merge a second header row of units into the column names.

    Originators very often write the variable on one row and its unit on the next:

        time (UTC),latitude,water temperature,chlorophyll
        ,degree N,degree C,ppb

    Unit detection reads the full column name and nothing else, so a units row left in
    place is invisible and the values are taken as already-standard. Folding it in is what
    makes `mg/l`, `deg F` and `ml/l` detectable at all.

        cols, rows, _ = read_table(path)          # header row
        cols = fold_unit_row(cols, rows[0])       # units row
        rows = rows[1:]                           # real data starts after it

    A column whose name already carries a bracketed unit is left alone, and a blank unit
    cell is skipped.
    """
    out = []
    for i, name in enumerate(columns):
        unit = (units[i].strip() if i < len(units) else "")
        if not name:
            out.append(name)
            continue
        if unit and not re.search(r"[\(\[\{].*[\)\]\}]", name):
            out.append(f"{name} ({unit})")
        else:
            out.append(name)
    return out


def combine_levels(
    tables: List[Tuple[List[str], List[List[str]]]],
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Concatenate same-schema tables into one cast. For data split one file per depth.

    A variable is sometimes archived as one file per depth level, each holding a single
    row. Those are levels of one cast, not separate casts, and must be combined before
    staging or the output is a pile of single-level profiles.

    Returns `(columns, rows, info)`. Schema mismatches are reported in
    `info["schema_mismatches"]` and those tables are dropped rather than silently
    misaligned -- check it.
    """
    tables = [t for t in tables if t and t[0]]
    if not tables:
        return [], [], {"error": "no tables supplied"}
    columns = list(tables[0][0])
    rows: List[List[str]] = []
    mismatches = []
    for i, (cols, rs) in enumerate(tables):
        if list(cols) != columns:
            mismatches.append({"table_index": i, "columns": list(cols)})
            continue
        rows.extend(rs)
    return columns, rows, {"tables_combined": len(tables) - len(mismatches),
                           "rows": len(rows), "schema_mismatches": mismatches}


def combine_on_key(
    tables: List[Tuple[List[str], List[List[str]]]],
    key: str,
    key_aliases: Optional[List[str]] = None,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Outer-join tables that each hold different variables for the same cast or track.

    One cast's variables are often split across files -- temperature in one, oxygen in
    another, chemistry in a third. They belong in a single cast, joined on whatever they
    share: depth for a profile, a timestamp for an underway track.

    `key` names the join column in the first table; `key_aliases` gives its name in the
    others when they differ (`["Depth (m)", "DEPTH", "depth_m"]`).

        cols, rows, info = combine_on_key([(c1, r1), (c2, r2)], key="Depth (m)")

    The join is an outer join in first-appearance order of the key, so a level present in
    only one file survives with the rest of its row empty. `info["key_overlap"]` reports
    how many key values each table shared with the result -- a near-zero overlap means the
    files do not actually share a key and the join is meaningless, which is the failure
    worth catching.
    """
    names = [key] + list(key_aliases or [])
    order: List[str] = []
    merged: Dict[str, Dict[str, str]] = {}
    out_columns: List[str] = [key]
    overlap = []
    collisions = []

    for cols, rows in tables:
        kcol = next((c for c in cols if c in names), None)
        if kcol is None:
            overlap.append({"key_column": None, "matched": 0,
                            "note": f"no key column among {names}"})
            continue
        ki = cols.index(kcol)
        value_cols = [c for j, c in enumerate(cols) if j != ki and c]
        for c in value_cols:
            if c in out_columns:
                collisions.append(c)
            else:
                out_columns.append(c)
        matched = 0
        for r in rows:
            if ki >= len(r):
                continue
            k = r[ki].strip()
            if not k:
                continue
            if k not in merged:
                merged[k] = {}
                order.append(k)
            else:
                matched += 1
            for j, c in enumerate(cols):
                if j == ki or not c:
                    continue
                merged[k][c] = (r[j].strip() if j < len(r) else "")
        overlap.append({"key_column": kcol, "matched_existing_keys": matched,
                        "rows": len(rows)})

    out_rows = [[k] + [merged[k].get(c, "") for c in out_columns[1:]] for k in order]
    return out_columns, out_rows, {
        "keys": len(order), "columns": len(out_columns),
        "per_table": overlap,
        "colliding_columns": sorted(set(collisions)),
    }
