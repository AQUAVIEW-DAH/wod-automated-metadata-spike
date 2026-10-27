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
    delimiter: Optional[str] = ",",
    comment_prefix: Optional[str] = None,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Read a delimited originator file as (columns, rows, info). Never raises on encoding.

    Tries `config.TEXT_ENCODINGS` in order and drops blank rows. `header_row` indexes the
    non-blank rows (1 when a title line precedes the header). `delimiter=None` sniffs it;
    rows starting with `comment_prefix` are dropped before the header is located. `info`
    gives the encoding, delimiter, row counts and duplicate column names.
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

    lines = text.splitlines()
    sep = delimiter if delimiter is not None else _sniff_delimiter(lines)
    raw = list(csv.reader(lines, delimiter=sep))
    kept = [r for r in raw if any(c.strip() for c in r)] if drop_blank_rows else raw
    comments = 0
    if comment_prefix:
        before = len(kept)
        kept = [r for r in kept if not (r and r[0].lstrip().startswith(comment_prefix))]
        comments = before - len(kept)
    if header_row >= len(kept):
        return [], [], {"error": f"header_row {header_row} is past the end of the file "
                                 f"({len(kept)} non-blank rows)", "encoding": used,
                        "delimiter": sep}

    columns = [c.strip() for c in kept[header_row]]
    rows = kept[header_row + 1:]
    dupes = [c for c, n in collections.Counter(c for c in columns if c).items() if n > 1]
    return columns, rows, {
        "encoding": used,
        "delimiter": sep,
        "rows_in_file": len(raw),
        "blank_rows_dropped": len(raw) - len(kept) - comments,
        "comment_rows_dropped": comments,
        "data_rows": len(rows),
        "duplicate_columns": dupes,
    }


# Whitespace is excluded: it also separates fixed-width columns, which belong to read_any.
_CANDIDATE_DELIMITERS = (",", "\t", ";", "|")


def _sniff_delimiter(lines: List[str], sample: int = 20) -> str:
    """The delimiter that splits the most sampled lines into the same field count > 1.

    Comma wins ties. Raw characters are counted, so decimal commas (`1,5;2,3`) can make
    comma beat semicolon; pass an explicit `delimiter` then.
    """
    body = [l for l in lines if l.strip()][:sample]
    if not body:
        return ","
    best, best_score = ",", (0.0, 0)
    for sep in _CANDIDATE_DELIMITERS:
        counts = [l.count(sep) for l in body]
        modal = max(set(counts), key=counts.count)
        if modal < 1:
            continue
        score = (counts.count(modal) / len(counts), modal)
        if score > best_score:
            best, best_score = sep, score
    return best


def clean_sentinels(
    rows: List[List[str]],
    columns: List[str],
    extra: Optional[Dict[str, str]] = None,
) -> Tuple[List[List[str]], Dict[str, int]]:
    """Replace originator missing-value codes with empty fields, and count every one.

    Returns `(rows, counts)`, counts keyed `"<column> :: <code>"`. Below-detection markers
    also become missing (WOD Table 12 has no flag for them); the detection limit is never
    substituted.
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

    Rows whose key is entirely blank are grouped under `()`. Footer text in the key column
    groups like a station, so drop groups whose position does not parse.
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

        time (UTC),latitude,water temperature,chlorophyll
        ,degree N,degree C,ppb

    Unit detection reads only the column name, so the unit must be folded in. Names that
    already carry a short bracketed unit, and blank unit cells, are left alone.
    """
    out = []
    for i, name in enumerate(columns):
        unit = (units[i].strip() if i < len(units) else "")
        unit = re.sub(r"^[\(\[]\s*(.*?)\s*[\)\]]$", r"\1", unit)      # '(m)' is 'm'
        if not name:
            out.append(name)
            continue
        # A name's own brackets may hold a unit ('(°C)') or a description ('(1-min average)').
        held = re.findall(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]", name)
        if unit and not any(len(h.strip()) <= 12 for h in held):
            out.append(f"{name} ({unit})")
        else:
            out.append(name)
    return out


def combine_levels(
    tables: List[Tuple[List[str], List[List[str]]]],
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Concatenate same-schema tables into one cast, for data split one file per depth.

    Returns `(columns, rows, info)`. Tables with a different schema are dropped and listed
    in `info["schema_mismatches"]`.
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
    """Outer-join tables that hold different variables for the same cast or track.

    `key` names the join column (e.g. depth or time); `key_aliases` are its other spellings
    (`["DEPTH", "depth_m"]`). Keys keep first-appearance order; a key in one table only
    leaves the rest of its row empty. `info["per_table"]` gives each table's `key_column`,
    `matched_existing_keys`, `distinct_keys` and `duplicate_keys_within_table` (a repeated
    key overwrites the earlier row); near-zero matches mean the tables share no key.

    Keys are compared as raw text (`0` and `0.0` differ); normalise them first if the files
    format them differently.
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
        # Matches against earlier tables are counted apart from duplicates within this one.
        matched = 0
        seen_here: set = set()
        duplicates = 0
        for r in rows:
            if ki >= len(r):
                continue
            k = r[ki].strip()
            if not k:
                continue
            if k in seen_here:
                duplicates += 1
            elif k in merged:
                matched += 1
            if k not in merged:
                merged[k] = {}
                order.append(k)
            seen_here.add(k)
            for j, c in enumerate(cols):
                if j == ki or not c:
                    continue
                merged[k][c] = (r[j].strip() if j < len(r) else "")
        overlap.append({"key_column": kcol, "matched_existing_keys": matched,
                        "distinct_keys": len(seen_here),
                        "duplicate_keys_within_table": duplicates,
                        "rows": len(rows)})

    out_rows = [[k] + [merged[k].get(c, "") for c in out_columns[1:]] for k in order]
    return out_columns, out_rows, {
        "keys": len(order), "columns": len(out_columns),
        "per_table": overlap,
        "colliding_columns": sorted(set(collisions)),
    }
