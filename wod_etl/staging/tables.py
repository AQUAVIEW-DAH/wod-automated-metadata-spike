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
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Read a delimited originator file as (columns, rows, info). Never raises on encoding.

    Handles four things that stop `load_tabular` before it starts:

    - **Encoding.** Tries `config.TEXT_ENCODINGS` in order. Japanese-authored CSVs are
      commonly cp932 and raise UnicodeDecodeError on utf-8.
    - **Blank rows.** Files written with \\r\\r\\n interleave an empty row between every
      real one, which shifts the header and doubles the row count.
    - **A title row above the header.** `header_row` is an index into the *non-blank*
      rows, so a file whose first line is a dataset title uses `header_row=1`.
    - **The delimiter.** `","` by default. Pass `delimiter=None` to sniff it from the
      header instead, which is what a folder of tab- or semicolon-separated files needs:
      read as CSV they come back as one column per row, which then reads downstream as a
      file with no measurements rather than as the wrong delimiter.

    `info` reports the encoding used, the delimiter, the row counts before and after
    cleaning, and any duplicate column names -- all of which change how the file should
    be read.
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
        "blank_rows_dropped": len(raw) - len(kept),
        "data_rows": len(rows),
        "duplicate_columns": dupes,
    }


# Whitespace is deliberately absent: it is also what separates fixed-width columns, and
# calling a fixed-width file whitespace-delimited is the failure that produces ragged rows
# and a phantom column. A fixed-width file belongs to read_any, not here.
_CANDIDATE_DELIMITERS = (",", "\t", ";", "|")


def _sniff_delimiter(lines: List[str], sample: int = 20) -> str:
    """The delimiter that splits the most sampled lines into the same field count > 1.

    Comma wins ties, so a file this would have read correctly before still does.

    **It counts raw characters, including inside quoted fields**, which is where it can be
    fooled: a semicolon-separated European file whose numbers use a decimal comma
    (`1,5;2,3;4,5`) has more commas than semicolons on every line, both perfectly
    consistent, so comma wins and the columns come back wrong. Check `len(columns)` against
    what the file should have, and pass an explicit `delimiter` when it disagrees.
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
    only one file survives with the rest of its row empty. `info["per_table"]` reports, for
    each table, its `key_column`, how many of its keys `matched_existing_keys` already
    contributed by an earlier table, its `distinct_keys`, and its
    `duplicate_keys_within_table`.

    **Two failures worth catching, and both need `per_table` to see.** A near-zero
    `matched_existing_keys` means the files do not actually share a key and the join is
    meaningless -- which looks like success, because every row survives in its own block.
    A non-zero `duplicate_keys_within_table` means one file repeats a key: the later row
    overwrites the earlier one and that measurement is gone. Divide by `distinct_keys`
    rather than by `rows` when judging overlap, or a file that overwrites itself reads as
    healthy.

    **Keys are compared as raw text**, so `0`, `0.0` and `00.0` are three different keys.
    That makes the trap bidirectional: files that genuinely share every depth can report
    **zero** overlap purely because one wrote `0` and the other `0.0`. Normalise the key
    column yourself before calling this when the files spell their depths differently --
    format every key to the same number of decimals -- and check `per_table` afterwards to
    confirm the overlap is what you expected.
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
        # A key already in `merged` means one of two very different things: an earlier
        # table contributed it (a genuine match, which is what overlap measures) or THIS
        # table already used it (a duplicate, whose row is about to be overwritten and
        # lost). Counting both as `matched` inflated the very metric that exists to catch
        # a bad join, so the two are tracked apart.
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
