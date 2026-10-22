import io
import re
from typing import Any, Dict, List, Optional
import pandas as pd

KNOWN_VAR_KEYWORDS = {
    "temperature": ["temp", "t_deg_c", "t", "temperature", "temp_c"],
    "salinity": ["sal", "salinity", "s", "psu", "sal_psu"],
    "depth": ["depth", "dep", "z", "press", "pressure", "d_m"],
    "oxygen": ["oxygen", "o2", "dissolved_o2", "d_o", "s_6_surface-do"],
    "chlorophyll": ["chlorophyll", "chla", "chl_a", "pigment"],
    "latitude": ["lat", "latitude", "lat_deg"],
    "longitude": ["lon", "long", "longitude", "lon_deg"]
}


def profile_raw_data(preview_text: str, file_url_or_path: str = "") -> Dict[str, Any]:
    """Profile a file preview: header block, delimiter, columns and recognised variables."""
    if not preview_text or not preview_text.strip():
        return {"error": "Empty preview text"}

    lines = preview_text.splitlines()

    # 1. Identify Header Comment Lines vs Data Lines
    comment_lines = []
    data_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "%", "*", "//")) or re.match(r"^[A-Za-z_\s]+:\s*.+", stripped):
            comment_lines.append(stripped)
        else:
            data_lines.append(stripped)

    raw_header_block = "\n".join(comment_lines)

    # raw_header_block keeps comments from anywhere in the file, but the count is used
    # downstream as a skip count, so it covers only the leading block.
    leading_comment_lines = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            leading_comment_lines += 1
            continue
        if stripped.startswith(("#", "%", "*", "//")) or re.match(r"^[A-Za-z_\s]+:\s*.+", stripped):
            leading_comment_lines += 1
            continue
        break

    if not data_lines:
        return {
            "source": file_url_or_path,
            "format_family": "unstructured_text_or_metadata",
            "raw_header_block": raw_header_block,
            "columns": [],
            "inferred_dtypes": {}
        }

    # 2. Sniff Delimiter
    delimiter = _sniff_delimiter(data_lines[0] if data_lines else "")

    # 2b. A delimited table may sit under 'Key,value' rows and carry a units row.
    layout = delimited_layout(lines, delimiter)
    if layout:
        data_lines = [l.strip() for l in lines[layout["header_line"]:] if l.strip()]
        data_lines = [l for l in data_lines if l.strip(delimiter + " ")]
        if layout["unit_row"] is not None:
            data_lines = data_lines[:1] + data_lines[2:]
        raw_header_block = "\n".join(
            ([raw_header_block] if raw_header_block else [])
            + [f"{k}: {v}" if v else f"Title: {k}" for k, v in layout["pairs"]])
        leading_comment_lines = layout["header_line"]
    sample_data_str = "\n".join(data_lines[:20])

    # 3. Parse tabular preview with Pandas
    try:
        df = pd.read_csv(
            io.StringIO(sample_data_str),
            sep=delimiter,
            engine="python",
            on_bad_lines="skip"
        )
        columns = [str(c).strip() for c in df.columns]
        if layout and len(layout["columns"]) == len(columns):
            columns = layout["columns"]
        dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}
    except Exception as e:
        return {
            "source": file_url_or_path,
            "format_family": "custom_ascii_or_fixed_width",
            "raw_header_block": raw_header_block,
            "error_parsing_table": str(e),
            "raw_first_data_line": data_lines[0] if data_lines else None
        }

    # 4. Map Column Names to Known Scientific Variables
    detected_vars = []
    for col in columns:
        col_lower = col.lower()
        for var_std, keywords in KNOWN_VAR_KEYWORDS.items():
            if any(kw == col_lower or kw in col_lower for kw in keywords):
                if var_std not in detected_vars:
                    detected_vars.append(var_std)

    return {
        "source": file_url_or_path,
        "format_family": "tabular_ascii",
        "detected_delimiter": repr(delimiter) if delimiter != " " else "whitespace",
        "header_comment_lines": leading_comment_lines,
        "table_header_line": layout["header_line"] if layout else None,
        "unit_row": layout["unit_row"] if layout else None,
        "table_columns": layout["columns"] if layout else None,
        "comment_lines_total": len(comment_lines),
        "raw_header_block": raw_header_block,
        "columns": columns,
        "inferred_dtypes": dtypes,
        "detected_scientific_variables": detected_vars
    }


_UNIT_TOKEN = re.compile(r"^[^\d\s][^,;\t]{0,19}$|^$")
# Values a data row holds that are not plain numbers: missing-value codes ('**NS', '*DL',
# '***NA'), dates, and degree-minute positions ('49-46.9858N').
_DATA_TOKEN = re.compile(r"^\*{1,4}[A-Za-z]{1,3}$|^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|"
                         r"^\d{1,3}-\d{1,2}\.\d+[NSEW]?$|^\d{1,3}-\d{1,2}[NSEW]$|"
                         r"^(?:na|nan|n/a|nd|n\.d\.|-)$|"
                         r"^not\s+(?:analy[sz]ed|measured|detected|sampled|available)$", re.I)


def _fields(line: str, delimiter: str) -> List[str]:
    import csv

    try:
        out = [f.strip() for f in next(csv.reader([line], delimiter=delimiter))]
    except (csv.Error, StopIteration):
        out = [f.strip() for f in line.split(delimiter)]
    while out and not out[-1]:
        out.pop()
    return out


def _data_share(fields: List[str]) -> float:
    def is_data(f):
        try:
            float(f)
            return True
        except ValueError:
            return bool(_DATA_TOKEN.match(f))
    vals = [f for f in fields if f]
    return sum(is_data(f) for f in vals) / len(vals) if vals else 0.0


def delimited_layout(lines: List[str], delimiter: str) -> Optional[Dict[str, Any]]:
    """Where a delimited table starts, when that is not its first line.

    Handles a title or 'Key,value' rows above the header (blank-line separated or not), a
    group label over some columns (`,,,,Chlorophyll a (mg m-3),,`), and a units row under
    the header. Returns `{"header_line", "columns", "pairs", "unit_row"}` or None for an
    ordinary table.
    """
    if delimiter not in (",", "\t", ";"):
        return None
    rows = [(i, _fields(l, delimiter)) for i, l in enumerate(lines)
            if l.strip() and not l.strip().startswith(("#", "%", "//"))]
    return table_layout(rows)


def table_layout(rows) -> Optional[Dict[str, Any]]:
    """`delimited_layout` for rows already split into fields: `[(row index, [field, ...])]`.
    Spreadsheets use it too."""
    rows = [(i, r) for i, r in rows if any(r)]
    for k in range(len(rows) - 1):
        i, head = rows[k]
        if len(head) < 2 or _data_share(head) > 0.2 or not head[0] and not any(head[1:2]):
            continue
        units, nxt = None, k + 1
        cand = rows[k + 1][1]
        # A units row may leave the unitless columns blank: ',,(m),,,Bq/L'.
        if (k + 2 < len(rows) and _data_share(cand) == 0 and len(cand) <= len(head)
                and (sum(bool(f) for f in cand) >= len(head) / 2
                     or any(cand) and _data_share(rows[k + 2][1]) >= 0.5)
                and all(_UNIT_TOKEN.match(f) for f in cand)):
            units, nxt = cand, k + 2
        data = rows[nxt][1]
        if abs(len(data) - len(head)) > 2 or _data_share(data) < 0.5:
            continue
        # Key-value rows can pass for a small table ('Cruise,KH-23-1' over 'Date,2023-07-02').
        # A run of rows that ends at another header with rows of its own is a preamble.
        end = nxt
        while end < len(rows) and _data_share(rows[end][1]) >= 0.5 \
                and abs(len(rows[end][1]) - len(head)) <= 2:
            end += 1
        if end + 1 < len(rows) and len(rows[end][1]) >= 2 and _data_share(rows[end][1]) <= 0.2 \
                and _data_share(rows[end + 1][1]) >= 0.5:
            continue
        above = [r for _, r in rows[:k]]
        # A data row above -- a value in its first field -- means another table, not a
        # preamble; 'Key,value' rows always start with a label.
        if any(len(r) >= len(head) and _data_share(r[:1]) == 1 and _data_share(r) >= 0.5
               for r in above):
            return None
        if not above and units is None:
            return None                        # an ordinary table
        columns = list(head)
        # A group label spans columns: its row has gaps, and its labels name a variable
        # ('Chlorophyll a (mg m-3)' over size fractions). A 'Key,,value' row does not.
        from wod_etl.mapping.query_wod_spec import match_variable

        group = above[-1] if above and len(above[-1]) >= 3 and "" in above[-1][1:] else []
        labels = [(j, g) for j, g in enumerate(group)
                  if j >= 1 and g and _data_share([g]) == 0 and match_variable(g) is not None]
        for n, (j, label) in enumerate(labels):
            stop = labels[n + 1][0] if n + 1 < len(labels) else len(columns)
            for c in range(j, min(stop, len(columns))):
                if columns[c] and label not in columns[c]:
                    columns[c] = f"{columns[c]} {label}"
        if units is not None:
            from wod_etl.staging.tables import fold_unit_row
            columns = fold_unit_row(columns, units)
        pairs = [(r[0], " ".join(f for f in r[1:] if f)) for r in above if r and r[0]]
        return {"header_line": i, "columns": columns, "pairs": pairs, "unit_row": units}
    return None


def _sniff_delimiter(line: str) -> str:
    if "," in line:
        return ","
    elif "\t" in line:
        return "\t"
    elif ";" in line:
        return ";"
    else:
        return r"\s+"
