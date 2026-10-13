"""Take rows out of a table in a PDF -- a cruise report, a station log -- with a citation.

Positions, dates and the odd measurement are often stated ONLY in a report: 0303928's trawl
positions and sea-surface temperatures are in Table 2 of the cruise report and nowhere else.
Skipped as documentation, they could not be used even to cross-check a station list.

PDF text comes out as a run of words with the table's layout gone, so a table is read by
describing ONE row as a regular expression, one group per column, and finding every match
on the page:

    from wod_etl.staging import extract_pdf_rows

    cols, rows, info = extract_pdf_rows(
        "0-data/Final.Report.U2015-015.JAPAN.pdf", page=8,
        row_pattern=r"(\\d+) (Nov\\. \\d{2}, \\d{4}) (\\S+E) (\\S+N) (\\d+\\.\\d) (\\d+)",
        columns=["Station", "Date", "Long", "Lat", "SST (deg C)", "Tow depth (m)"])
    info["source"]     # 'Final.Report.U2015-015.JAPAN.pdf, PDF page 8'

Check `info["matches"]` against the number of rows the printed table has. A row the pattern
misses is silently absent, so count them. `page` is the PDF's own page index (1-based), not
the number printed on the page; `info` carries both when the printed one can be read.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def pdf_page_text(path, pages: Optional[Sequence[int]] = None) -> Tuple[Dict[int, str], Dict[str, Any]]:
    """`({page_number: text}, info)` for a PDF, 1-based. Whitespace is collapsed to single
    spaces, because pypdf separates words with tabs and runs of spaces at random."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}, {"error": "pypdf is not installed; `uv sync` installs it"}
    import logging

    # pypdf logs every repaired cross-reference ("Ignoring wrong pointing object"); harmless
    # in a report PDF, and noise in a staging log.
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
