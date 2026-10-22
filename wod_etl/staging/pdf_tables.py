"""Extract table rows from a PDF page (e.g. a cruise report), with a citation.

PDF text loses its layout, so a row is described by a regular expression with one group per
column, and every match on the page is a row:

    from wod_etl.staging import extract_pdf_rows

    cols, rows, info = extract_pdf_rows(
        "0-data/cruise_report.pdf", page=8,
        row_pattern=r"(\\d+) (Nov\\. \\d{2}, \\d{4}) (\\S+E) (\\S+N) (\\d+\\.\\d) (\\d+)",
        columns=["Station", "Date", "Long", "Lat", "SST (deg C)", "Tow depth (m)"])
    info["source"]     # 'cruise_report.pdf, PDF page 8'

Check `info["matches"]` against the printed table: a missed row is silently absent. `page`
is the 1-based PDF page index, not the printed page number.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def pdf_page_text(path, pages: Optional[Sequence[int]] = None) -> Tuple[Dict[int, str], Dict[str, Any]]:
    """`({page_number: text}, info)` for a PDF, 1-based, with whitespace collapsed."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}, {"error": "pypdf is not installed; `uv sync` installs it"}
    import logging

    # Silence pypdf's warnings about repaired cross-references.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(str(path))
    except Exception as e:                              # noqa: BLE001 - reported, not raised
        return {}, {"error": f"{type(e).__name__}: {e}"}
    n = len(reader.pages)
    wanted = list(pages) if pages else list(range(1, n + 1))
    out: Dict[int, str] = {}
    for p in wanted:
        if not 1 <= p <= n:
            continue
        try:
            text = reader.pages[p - 1].extract_text() or ""
        except Exception:                               # noqa: BLE001 - one bad page
            text = ""
        out[p] = re.sub(r"\s+", " ", text).strip()
    return out, {"pages": n, "source": Path(path).name}


def extract_pdf_rows(
    path,
    page: int,
    row_pattern: str,
    columns: List[str],
    *,
    flags: int = 0,
) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """Every match of `row_pattern` on one PDF page, as rows of `columns`.

    The pattern is matched against the page text with whitespace collapsed to single
    spaces, so write single spaces in it. It needs exactly one group per column.
    """
    texts, info = pdf_page_text(path, [page])
    if info.get("error"):
        return list(columns), [], info
    text = texts.get(page, "")
    pattern = re.compile(row_pattern, flags)
    if pattern.groups != len(columns):
        return list(columns), [], {**info, "error": f"the pattern has {pattern.groups} "
                                                    f"group(s) and {len(columns)} column(s) "
                                                    f"were named"}
    rows = [[g.strip() for g in m.groups()] for m in pattern.finditer(text)]
    printed = re.match(r"^(\d{1,4})\b", text)
    source = f"{Path(path).name}, PDF page {page}"
    if printed:
        source += f" (printed page {printed.group(1)})"
    return list(columns), rows, {
        **info, "page": page, "matches": len(rows), "source": source,
        "text_excerpt": None if rows else text[:400],
        "note": "Compare `matches` with the rows the printed table shows: a row the pattern "
                "misses is absent, not flagged.",
    }
