"""What an input file holds, for the browser preview: its text, its sheets, or (read in the
worker, like an output file) a NetCDF's attributes and variables. Files are only read.

Text is also laid out to be read: a table as a header block and columns (split as the
pipeline splits it), XML and JSON indented, prose wrapped. The text as stored comes too.
"""
import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

TEXT_BYTES = 64 * 1024              # of a text file, shown
SHEET_ROWS = 50                     # of each spreadsheet sheet, shown
TABLE_ROWS = 500                    # of a text table, shown
INDENT_BYTES = 2 * 1024 * 1024      # XML or JSON up to this size is indented whole

_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def preview_input(path) -> Dict[str, Any]:
    """`{"kind": "text" | "sheets" | "netcdf" | "pdf" | "image" | "other", ...}`. A NetCDF
    file is only recognised here; `preview.describe_input` reads it. Text carries `text` as
    stored and `formatted`, its layout (None when it has none)."""
    from wod_etl.discovery import sniff_format

    p = Path(path)
    fmt = sniff_format(str(p))
    out = {"size": p.stat().st_size, "format": fmt.get("format_family"),
           "note": fmt.get("note")}
    family, reader = fmt.get("format_family"), fmt.get("reader")
    if reader == "netcdf":
        return {**out, "kind": "netcdf"}
    if family == "pdf":
        return {**out, "kind": "pdf"}
    if family == "image":
        return {**out, "kind": "image"}
    if reader == "spreadsheet":
        sheets = _sheets(p)
        if sheets is not None:
            return {**out, "kind": "sheets", "sheets": sheets, "rows_shown": SHEET_ROWS}
    if fmt.get("encoding"):
        with open(p, "rb") as f:
            head = f.read(TEXT_BYTES + 1)
        text = head[:TEXT_BYTES].decode(fmt["encoding"], errors="replace")
        truncated = len(head) > TEXT_BYTES
        return {**out, "kind": "text", "encoding": fmt["encoding"], "text": text,
                "truncated": truncated, "formatted": _formatted(p, fmt, text, truncated)}
    return {**out, "kind": "other"}


def _formatted(p: Path, fmt: Dict[str, Any], text: str, truncated: bool) -> Optional[Dict]:
    family = fmt.get("format_family")
    if family in ("xml", "json") or text.lstrip()[:1] in ("{", "["):
        try:
            indented = _indented(p, fmt, family)
        except Exception:                               # noqa: BLE001 - not well formed
            indented = None
        if indented or family in ("xml", "json"):
            return indented
    try:
        if family == "unstructured_text":
            return {"layout": "prose"}
        lines = text.splitlines()
        if truncated and lines:
            lines = lines[:-1]                  # cut mid-line by the 64 KB limit
        if family == "sea_bird_cnv":
            return _cnv_table(lines)
        if family == "wod_csv":
            return _wod_csv_table(lines)
        if fmt.get("reader") in ("delimited", "fixed_width"):
            return _table(lines, fmt, text, p)
        return _loose_table(lines)
    except Exception:                                   # noqa: BLE001 - shown as stored
        return None


def _indented(p: Path, fmt: Dict[str, Any], family) -> Optional[Dict]:
    """XML or JSON, indented, when the whole file is small enough to read."""
    if p.stat().st_size > INDENT_BYTES:
        return None
    raw = p.read_text(encoding=fmt.get("encoding") or "utf-8", errors="replace")
    if family == "xml":
        from xml.dom import minidom

        pretty = minidom.parseString(raw.encode("utf-8")).toprettyxml(indent="  ")
        return {"layout": "code",
                "text": "\n".join(line for line in pretty.splitlines() if line.strip())}
    return {"layout": "code", "text": json.dumps(json.loads(raw), indent=2, ensure_ascii=False)}


def _table(lines: List[str], fmt: Dict[str, Any], text: str, p: Path) -> Optional[Dict]:
    """A delimited or fixed-width table: the lines above it, its column names, its rows."""
    from wod_etl.discovery import profile_raw_data

    above = int(profile_raw_data(text, str(p)).get("header_comment_lines") or 0)
    body = [line for line in lines[above:] if line.strip()]
    delimiter = fmt.get("delimiter")
    rows = (list(csv.reader(body, delimiter=delimiter)) if delimiter
            else [line.split() for line in body])
    return _shaped(lines[:above], rows)


def _cnv_table(lines: List[str]) -> Optional[Dict]:
    """A Sea-Bird .cnv: the header to `*END*`, then the data, named by its `# name` lines or
    by the row of short names an ASCII export puts above it."""
    end = next((i for i, line in enumerate(lines) if line.strip().upper().startswith("*END*")),
               None)
    if end is None:
        return None
    names = [line.partition("=")[2].strip() for line in lines[:end]
             if line.strip().startswith("# name ")]
    rows = [line.split() for line in lines[end + 1:] if line.strip()]
    if names and rows and len(names) == len(rows[0]):
        rows = [names] + rows
    return _shaped(lines[:end + 1], rows)


def _wod_csv_table(lines: List[str]) -> Optional[Dict]:
    """WOD's own CSV export (manual p.79): the first cast's header rows as `Key: value`, then
    its VARIABLES table, each variable named with its UNITS."""
    def cells(line):
        return [c.strip() for c in next(csv.reader([line]))]

    starts = [i for i, line in enumerate(lines) if line.upper().startswith("VARIABLES")]
    if not starts:
        return None
    start = starts[0]
    names = cells(lines[start])[1:]
    units = (cells(lines[start + 1])[1:] if start + 1 < len(lines)
             and lines[start + 1].upper().startswith("UNITS") else [])
    units += [""] * (len(names) - len(units))
    rows = []
    for line in lines[start + 1:]:
        upper = line.upper()
        if upper.startswith("END OF VARIABLES"):
            break
        if line.strip() and not upper.startswith(("UNITS", "PROF-FLAG")):
            rows.append(cells(line))
    above = []
    for line in lines[:start]:
        c = cells(line) if line.strip() and not line.startswith("#") else []
        values = [x for x in c[1:] if x]
        if c and c[0]:
            above.append(f"{c[0]}: {' · '.join(values)}" if values else c[0])
    columns = ["level"] + [f"{n} ({u})" if u else n for n, u in zip(names, units)]
    keep = [j for j, name in enumerate(columns)
            if name or any(j < len(r) and r[j] for r in rows)]
    table = _shaped([], [[columns[j] for j in keep]]
                    + [[r[j] if j < len(r) else "" for j in keep] for r in rows])
    return table and {**table, "above": above, "casts": len(starts)}


def _loose_table(lines: List[str]) -> Optional[Dict]:
    """Any other text whose lines below a `#`, `*` or `%` header split into one number of
    whitespace-separated fields (an XCTD `.dat`); None otherwise."""
    above = 0
    while above < len(lines) and (not lines[above].strip()
                                  or lines[above].lstrip()[:1] in ("#", "*", "%")):
        above += 1
    rows = [line.split() for line in lines[above:] if line.strip()]
    widths = [len(r) for r in rows]
    if not rows or max(widths) < 2 or widths.count(max(set(widths), key=widths.count)) \
            < 0.8 * len(rows):
        return None
    return _shaped(lines[:above], rows)


def _shaped(above: List[str], rows: List[List[str]]) -> Optional[Dict]:
    """Columns from the first row, unless it is mostly numbers (a file with no header row)."""
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    cells = [c for c in rows[0] if c]
    named = sum(bool(_NUMBER.match(c)) for c in cells) < 0.5 * len(cells)
    columns = rows[0] if named else [f"column {i + 1}" for i in range(len(rows[0]))]
    data = rows[1:] if named else rows
    width = max([len(columns)] + [len(r) for r in data[:TABLE_ROWS]])
    columns = columns + [""] * (width - len(columns))
    return {"layout": "table", "above": above, "columns": columns, "named": named,
            "rows": data[:TABLE_ROWS], "rows_total": len(data)}


def _sheets(p: Path):
    """Each sheet's first rows as text cells, or None when the workbook cannot be read."""
    import pandas as pd

    try:
        book = pd.read_excel(p, sheet_name=None, header=None, nrows=SHEET_ROWS)
    except Exception:                                   # noqa: BLE001 - shown as "other"
        return None
    return [{"name": str(name), "rows": [[_cell(x) for x in row]
                                         for row in df.itertuples(index=False)]}
            for name, df in book.items()]


def _cell(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return str(x)
