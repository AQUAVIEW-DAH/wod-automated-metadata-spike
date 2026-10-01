"""Load a complete data file into a DataFrame -- the bridge between Phase 2 and Phase 3.

profile_raw_data only sees the first few kilobytes, so it can describe a file's structure
but cannot produce its data. This skill takes that description and reads the whole file,
which is what convert_units_and_types and apply_qc_auditing expect.
"""
import io
import re
import warnings
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd

# Values that historically stand in for "missing" in oceanographic ASCII files.
# Only sentinels no real measurement can collide with. '-9', '-99', '9999' and '99999' were
# here and are NOT safe: pandas expands them to their float forms, so an air temperature of
# -9.0, a longitude of -99.0 or a sounding of 9999 m silently became NaN. A sentinel left in
# the data shows up as an absurd value range in `audit_dataset`; a real value turned to NaN
# shows up as nothing at all, which is the worse failure.
DEFAULT_NA_VALUES = [
    "", " ", "-", "--", "NA", "N/A", "NaN", "nan", "null", "NULL",
    "-999", "-9999", "-99999", "-999.9", "-9999.9", "-99.999", "-1e34", "1e35",
]

_COMMENT_PREFIXES = ("#", "%", "*", "//", ";", "!")


def _strip_interior_comments(lines):
    """Drop whole-line comments that sit below the header, keeping the header itself.

    `read_csv` is deliberately called with comment=None, because '#' occurs inside real
    data (station '#5'). But a comment line left in the body parses as a row, giving a
    phantom level whose depth is the comment text. Only '#' and '//' are treated as markers:
    '*' and '%' begin real sentinel values such as '**NS' and cannot be assumed to be
    comments.
    """
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

    Returns (DataFrame, info). On failure the DataFrame is None and info carries `error`,
    so an unattended run can record the failure and move to the next file.
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
        # on_bad_lines="warn" drops malformed rows to stderr, where an unattended run never
        # sees them. Capture them instead: a dropped row is lost data and must be reported.
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

    # Drop rows that are entirely missing, and any trailing unnamed column left by a
    # line ending in the delimiter.
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
    """How many leading lines to skip before the column header row.

    Prefers counting the actual leading comment block in the full file, because the Phase 1
    count came from a truncated preview.
    """
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
    # No leading comment block in the FULL file means nothing to skip. The Phase 1 number is
    # only a fallback for a truncated preview, and it must never be applied here: it would
    # skip real header rows on a file whose only comments sit below the table.
    return 0


if __name__ == "__main__":
    import json
    import tempfile

    sample = """# CRUISE: STOCS85A
# DATE: 1985-06-12
LAT: 61.930 LON: -172.270
Depth (m), Temp (C), Salinity (PSU)
0.0, 9.13, 34.94
10.0, 8.96, 34.91
25.0, -999, 34.92
"""
    p = Path(tempfile.mkdtemp()) / "cast.txt"
    p.write_text(sample)
    df, info = load_tabular(str(p), {"detected_delimiter": "','", "header_comment_lines": 3})
    print(df.to_string(index=False))
    print()
    print(json.dumps(info, indent=2))
