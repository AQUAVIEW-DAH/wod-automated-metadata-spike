"""Split a file containing several casts into individual casts.

Historical oceanographic files routinely concatenate many casts into one file, each
introduced by its own header block. Everything downstream assumes one cast per contract, so
this runs before mapping.

The signal is a header/comment line appearing AFTER data rows have started: that marks the
beginning of the next cast. A file with a single leading header block yields one cast, so
callers can run this unconditionally.

(Placed in discovery/ rather than writers/ because it interprets raw file structure, the
same job as profile_raw_data, and its output feeds Phase 2.)
"""
import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

_COMMENT_PREFIXES = ("#", "%", "*", "//", ";", "!")
_KEY_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\s\-]*:\s*\S")
_NUMERIC_ROW = re.compile(r"^[\s\d.,;\-+eE\t|]+$")


def split_casts(
    file_path_or_text: str,
    profile_result: Optional[Dict[str, Any]] = None,
    min_data_rows: int = 1,
) -> Dict[str, Any]:
    """Return the casts found in a file.

    Each cast is {"index", "header_block", "column_header_line", "data_text",
    "data_row_count", "start_line", "end_line"}.
    """
    text = _read(file_path_or_text)
    if text is None:
        return {"source": str(file_path_or_text), "error": "file not found or unreadable",
                "cast_count": 0, "casts": []}
    if not text.strip():
        return {"source": str(file_path_or_text), "error": "file is empty",
                "cast_count": 0, "casts": []}

    lines = text.splitlines()
    blocks: List[Dict[str, Any]] = []
    current = _new_block(0)
    seen_data = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            (current["data"] if seen_data else current["header"]).append(line)
            continue

        if _is_header_line(stripped):
            if seen_data:
                # A header after data means the previous cast ended here.
                current["end"] = i - 1
                blocks.append(current)
                current = _new_block(i)
                seen_data = False
            current["header"].append(stripped)
        else:
            if not seen_data and current["header"] and _looks_like_column_header(stripped):
                current["column_header"] = stripped
            seen_data = True
            current["data"].append(line)

    current["end"] = len(lines) - 1
    blocks.append(current)

    casts = []
    for b in blocks:
        rows = [d for d in b["data"] if d.strip()]
        body = [r for r in rows if r.strip() != (b.get("column_header") or "").strip()]
        if len(body) < min_data_rows:
            continue
        casts.append({
            "index": len(casts),
            "header_block": "\n".join(b["header"]).strip(),
            "column_header_line": b.get("column_header"),
            "data_text": "\n".join(rows),
            "data_row_count": len(body),
            "start_line": b["start"],
            "end_line": b["end"],
        })

    return {
        "source": str(file_path_or_text)[:200],
        "cast_count": len(casts),
        "multi_cast": len(casts) > 1,
        "casts": casts,
    }


def cast_as_profile_input(cast: Dict[str, Any]) -> str:
    """Rebuild a single cast as text that profile_raw_data can consume."""
    parts = [p for p in (cast.get("header_block"), cast.get("data_text")) if p]
    return "\n".join(parts)


def _new_block(start: int) -> Dict[str, Any]:
    return {"header": [], "data": [], "start": start, "end": start, "column_header": None}


def _is_header_line(s: str) -> bool:
    if s.startswith(_COMMENT_PREFIXES):
        return True
    if _NUMERIC_ROW.match(s):
        return False
    return bool(_KEY_VALUE.match(s))


def _looks_like_column_header(s: str) -> bool:
    """A column header row has letters but is not a numeric data row."""
    return bool(re.search(r"[A-Za-z]", s)) and not _NUMERIC_ROW.match(s)


def _read(file_path_or_text: str) -> Optional[str]:
    if "\n" in file_path_or_text or len(file_path_or_text) > 4096:
        return file_path_or_text
    p = Path(file_path_or_text)
    if not p.exists():
        # A single-line string that names a file is a PATH, and a missing one is an error
        # the caller must see. Returning it as its own content produced a phantom one-row
        # cast whose only datum was the path string.
        looks_like_a_path = "/" in file_path_or_text or os.sep in file_path_or_text or bool(p.suffix)
        if looks_like_a_path or not file_path_or_text.strip():
            return None
        return file_path_or_text
    for enc in ("utf-8", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError):
            continue
    return None


if __name__ == "__main__":
    import json
    multi = """# CRUISE: STOCS85A
# CAST: 1
LAT: 61.930 LON: -172.270
Depth, Temp_C
0.0, 9.13
10.0, 8.96
# CRUISE: STOCS85A
# CAST: 2
LAT: 62.100 LON: -172.500
Depth, Temp_C
0.0, 8.70
15.0, 8.40
25.0, 7.90
"""
    res = split_casts(multi)
    print(json.dumps({k: v for k, v in res.items() if k != "casts"}, indent=2))
    for c in res["casts"]:
        print(f"\n--- cast {c['index']} (lines {c['start_line']}-{c['end_line']}, "
              f"{c['data_row_count']} rows) ---")
        print(c["header_block"])
        print(c["data_text"])
