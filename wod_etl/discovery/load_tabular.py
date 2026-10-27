"""Load a complete data file into a DataFrame, using the structure profile_raw_data inferred."""
import io
import re
import warnings
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd

# Missing-value sentinels; '-9', '-99' and '9999' are left out because they can be real values.
DEFAULT_NA_VALUES = [
    "", " ", "-", "--", "NA", "N/A", "NaN", "nan", "null", "NULL",
    "-999", "-9999", "-99999", "-999.9", "-9999.9", "-99.999", "-1e34", "1e35",
]

_COMMENT_PREFIXES = ("#", "%", "*", "//", ";", "!")


def _strip_interior_comments(lines):
    """Drop '#' and '//' lines below the header; '*' and '%' may start a sentinel such as '**NS'."""
    if not lines:
        return lines, 0
    head, rest = lines[:1], lines[1:]
    kept = [l for l in rest if not l.lstrip().startswith(("#", "//"))]
    return head + kept, len(rest) - len(kept)


def load_tabular(
    file_path: str,
    profile_result: Optional[Dict[str, Any]] = None,
    delimiter: Optional[str] = None,
    header_lines: Optional[int] = None,
    na_values: Optional[List[str]] = None,
    max_rows: Optional[int] = None,
    encoding: Optional[str] = None,
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """Read a full tabular file using the structure Phase 1 inferred.

    Returns (DataFrame, info). On failure the DataFrame is None and info carries `error`.
    """
    info: Dict[str, Any] = {"source": str(file_path), "warnings": []}
    path = Path(file_path)
    if not path.exists():
        return None, {**info, "error": f"file not found: {file_path}"}

    profile_result = profile_result or {}
    sep = delimiter if delimiter is not None else _sep_from_profile(profile_result)
    enc = encoding or _encoding_from_profile(profile_result)

    try:
        text = path.read_text(encoding=enc, errors="replace")
    except (OSError, LookupError) as e:
        return None, {**info, "error": f"could not read file: {e}"}

    if not text.strip():
        return None, {**info, "error": "file is empty"}

    skip = header_lines if header_lines is not None else _header_lines_for(text, profile_result)
    kept, commented = _strip_interior_comments(text.splitlines()[skip:])
    body = "\n".join(kept)
    if not body.strip():
        return None, {**info, "error": f"nothing left after skipping {skip} header line(s)"}

    info.update({"delimiter": sep, "header_lines_skipped": skip, "encoding": enc})
    if commented:
        info["warnings"].append(f"dropped {commented} interior comment line(s) from the table body")

    try:
        # Capture malformed-row warnings so dropped rows are reported, not lost on stderr.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            df = pd.read_csv(
                io.StringIO(body),
                sep=sep,
                engine="python",
                skipinitialspace=True,
                comment=None,
                na_values=na_values if na_values is not None else DEFAULT_NA_VALUES,
                keep_default_na=True,
                nrows=max_rows,
                on_bad_lines="warn",
            )
    except Exception as e:                          # noqa: BLE001 - reported, not raised
        return None, {**info, "error": f"parse failed: {type(e).__name__}: {e}"}

    bad_lines = sorted({int(n) for w in caught
                        for n in re.findall(r"[Ss]kipping line (\d+)", str(w.message))})
    if bad_lines:
        shown = ", ".join(str(n + skip) for n in bad_lines[:10])
        info["warnings"].append(
            f"dropped {len(bad_lines)} malformed row(s) whose field count did not match "
            f"the header, at source line(s) {shown}"
            f"{', ...' if len(bad_lines) > 10 else ''}")
        info["malformed_rows_dropped"] = len(bad_lines)

    df.columns = [str(c).strip() for c in df.columns]
    if profile_result.get("unit_row") is not None and len(df):
        # The profile folded the units row into the names; it is not data.
        units = [str(u).strip() for u in profile_result["unit_row"]]

        def is_units(row) -> bool:
            # Most cells, not all: a 'µ' can decode differently here than in the profile.
            cells = [str(v).strip() if pd.notna(v) else "" for v in row][:len(units)]
            same = sum(a == b for a, b in zip(cells, units) if b)
            return same >= max(1, 0.8 * sum(bool(u) for u in units))

        first = next((n for n, (_, r) in enumerate(df.head(3).iterrows()) if is_units(r)), None)
        if first is not None:
            df = df.drop(index=df.index[first]).reset_index(drop=True)
        for c in df.columns:
            try:
                df[c] = pd.to_numeric(df[c])
            except (ValueError, TypeError):
                pass
        info["warnings"].append("a units row under the header was folded into the column names")
    names = profile_result.get("table_columns")
    if names and len(names) == len(df.columns):
        df.columns = list(names)

    # Drop all-empty rows and the empty column left by a trailing delimiter.
    before = len(df)
    df = df.dropna(how="all")
    dropped = before - len(df)
    if dropped:
        info["warnings"].append(f"dropped {dropped} all-empty row(s)")

    junk = [c for c in df.columns if c.startswith("Unnamed:") and df[c].isna().all()]
    if junk:
        df = df.drop(columns=junk)
        info["warnings"].append(f"dropped {len(junk)} empty unnamed column(s)")

    info.update({
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "all_na_columns": [c for c in df.columns if df[c].isna().all()],
    })

    profiled = profile_result.get("columns")
    if profiled and list(df.columns) != list(profiled):
        info["warnings"].append(
            f"columns differ from the Phase 1 profile: profile saw {list(profiled)}, "
            f"full file has {list(df.columns)}")

    if len(df) == 0:
        return None, {**info, "error": "no data rows after parsing"}
    return df, info


def _sep_from_profile(profile: Dict[str, Any]) -> str:
    """Turn the profile's reported delimiter back into a pandas separator."""
    d = profile.get("detected_delimiter")
    if not d:
        return r"\s+"
    if d in ("whitespace", r"\s+", "\\s+"):
        return r"\s+"
    # profile_raw_data reports repr(delimiter), e.g. "','"
    s = str(d)
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1]
    return {"\\t": "\t"}.get(s, s)


def _encoding_from_profile(profile: Dict[str, Any]) -> str:
    enc = str(profile.get("detected_encoding", "utf-8"))
    return "latin-1" if "latin" in enc else "utf-8"


def _header_lines_for(text: str, profile: Dict[str, Any]) -> int:
    """Count the leading lines to skip before the column header row, using the full file."""
    if profile.get("table_header_line") is not None:
        return int(profile["table_header_line"])
    lines = text.splitlines()
    count = 0
    for line in lines:
        s = line.strip()
        if not s:
            count += 1
            continue
        if s.startswith(_COMMENT_PREFIXES) or re.match(r"^[A-Za-z_][A-Za-z0-9_\s]*:\s*\S", s):
            count += 1
            continue
        break
    if count:
        return count
    # No leading comment block: skip nothing, whatever the preview's count says.
    return 0
