"""Work out what a file actually is, from its bytes rather than its name.

An accession does not label its formats. Real submissions carry NetCDF written as `.nc`,
`.cdf`, `.nc4` or nothing at all; Sea-Bird `.cnv`; fixed-column ASCII as `.dat`, `.asc`,
`.000`, `.txt` or no extension; spreadsheets as `.xlsx`; and plain CSV named `.dat`. An
extension-driven reader gets all of those wrong in the same direction: it calls read_csv on
bytes that are not text and records a parse error instead of a format.

    from wod_etl.discovery import sniff_format
    sniff_format("data/raw/0287423/2-CTD/cast_01")
    # {"format_family": "netcdf", "reader": "netcdf", "confidence": "high", ...}

`reader` is the name of the strategy `read_any` will dispatch to. `format_family` is the
description; where the two differ it is because several families share one reader.
Never raises -- an unreadable file comes back as a family with `readable: False`.
"""
import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl.config import TEXT_ENCODINGS

# Leading bytes that identify a container outright. Order matters only in that longer
# signatures are checked first; none of these overlap.
MAGIC = [
    (b"\x89HDF\r\n\x1a\n", "netcdf4_hdf5", "netcdf",
     "HDF5 container -- NetCDF-4 and plain HDF5 share it"),
    (b"CDF\x01", "netcdf_classic", "netcdf", "NetCDF classic (CDF-1)"),
    (b"CDF\x02", "netcdf_64bit_offset", "netcdf", "NetCDF 64-bit offset (CDF-2)"),
    (b"CDF\x05", "netcdf_64bit_data", "netcdf", "NetCDF 64-bit data (CDF-5)"),
    (b"PK\x03\x04", "zip_container", "spreadsheet",
     "ZIP container: .xlsx/.ods are ZIPs, but so is an actual archive -- see `note`"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2_container", "spreadsheet",
     "OLE2 container: legacy .xls/.doc"),
    (b"%PDF", "pdf", "none", "PDF document"),
    (b"\x1f\x8b", "gzip", "none", "gzip stream -- decompress before reading"),
    (b"BZh", "bzip2", "none", "bzip2 stream -- decompress before reading"),
    (b"\xfd7zXZ\x00", "xz", "none", "xz stream -- decompress before reading"),
    (b"SQLite format 3\x00", "sqlite", "none", "SQLite database"),
    (b"\x00\x01\x00\x00Standard Jet DB", "access_db", "none", "MS Access database"),
]

SNIFF_BYTES = 65536

# Text that says "this is a Sea-Bird converted data file" -- the header is its own dialect
# (`# name 0 = prDM: Pressure, Digiquartz [db]`) and carries the column names.
_CNV_MARKERS = ("*END*", "# name 0 =", "* Sea-Bird", "* SBE ")

# Delimiters worth testing, in the order a tie is broken. Whitespace is last because it is
# also what separates fixed-width columns, and calling a fixed-width file whitespace-
# delimited is the failure that produces ragged rows and a phantom column.
_DELIMITERS = [(",", "comma"), ("\t", "tab"), (";", "semicolon"), ("|", "pipe")]

# Two data rows is the fewest that can agree on a shape. A one-cast file with two levels is
# real, and rejecting it as unstructured loses data; the fixed-width test keeps its own,
# stricter minimum because inferring column boundaries from two rows infers noise.
MIN_CONSISTENT_ROWS = 2
MIN_ROWS_FOR_FIXED_WIDTH = 4

# Below this share of numeric fields a block of lines is prose, not a table. Readmes and
# cruise logs are full of dates, station numbers and depths in sentences; without this test
# they sniff as whitespace-delimited data and fail three phases later as a parse error
# instead of being excluded during triage where they belong.
MIN_NUMERIC_FIELD_FRACTION = 0.5

# A delimited table earns a lower bar: a station list is legitimately station id, a
# coordinate written '49-46.9858S', a date and a time, of which only some parse as floats.
MIN_NUMERIC_FIELD_FRACTION_DELIMITED = 0.2


def sniff_format(path: str, num_bytes: int = SNIFF_BYTES) -> Dict[str, Any]:
    """Identify a file's format from its content.

    Returns {"format_family", "reader", "confidence", "readable", "evidence", ...}.
    `reader` is one of: "netcdf", "spreadsheet", "delimited", "fixed_width", "sea_bird_cnv",
    "none". "none" means there is no tabular data to read, not that the file is broken.
    """
    p = Path(path)
    if not p.exists():
        return _result("missing", "none", "high", readable=False,
                       evidence=[f"no such file: {path}"])
    try:
        size = p.stat().st_size
        head = p.open("rb").read(num_bytes)
    except OSError as e:
        return _result("unreadable", "none", "high", readable=False,
                       evidence=[f"could not read: {e}"])

    if size == 0:
        return _result("empty", "none", "high", readable=False, size_bytes=0,
                       evidence=["file is zero bytes"])

    for signature, family, reader, note in MAGIC:
        if head.startswith(signature):
            res = _result(family, reader, "high", size_bytes=size,
                          readable=reader != "none", evidence=[f"magic bytes {signature!r}"],
                          note=note)
            if family == "zip_container":
                res.update(_disambiguate_zip(p))
                res["is_document"] = res["format_family"] in DOCUMENT_FAMILIES
            return res

    text, encoding = _decode(head)
    if text is None:
        return _result("binary_unknown", "none", "medium", readable=False, size_bytes=size,
                       evidence=[f"no encoding in {list(TEXT_ENCODINGS)} decodes it",
                                 f"first bytes: {head[:16]!r}"],
                       note="An unrecognised binary. Ask the submitter or the accession "
                            "readme what wrote it before guessing a layout.")

    return {**_sniff_text(text, size), "encoding": encoding, "size_bytes": size}


# ----------------------------------------------------------------- text
def _sniff_text(text: str, size: int) -> Dict[str, Any]:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return _result("empty", "none", "high", readable=False,
                       evidence=["decodes as text but holds only blank lines"])

    if any(m in text for m in _CNV_MARKERS):
        return _result("sea_bird_cnv", "sea_bird_cnv", "high",
                       evidence=[f"Sea-Bird marker {m!r}" for m in _CNV_MARKERS if m in text],
                       note="Column names live in the '# name N = ' header lines, not in a "
                            "header row. Units are in that header too -- fold them into the "
                            "name so unit detection can see them.")

    structured = _structured_text(text)
    if structured:
        return structured

    body = _likely_data_lines(lines)

    # A header row plus ONE data row is a legible table, and it is exactly the shape of the
    # "one file per depth level" archive: each file holds a single level of one cast. The
    # data-row test needs a digit, so the header never counts and such a file scored one row
    # and sniffed as prose -- unreadable, and therefore invisible to the cross-file check
    # that would have spotted the shape.
    if len(body) == 1:
        pair = _header_plus_one_row(lines, body[0])
        if pair:
            char, name, count = pair
            return _result("delimited_text", "delimited", "medium",
                           evidence=[f"one data row under a {name}-separated header, both "
                                     f"splitting into {count} fields"],
                           delimiter=char, delimiter_name=name, row_consistency=1.0,
                           single_data_row=True,
                           note="One data row. If the folder holds many files like this, "
                                "they are levels of one cast rather than one cast each -- "
                                "combine them before staging.")

    if len(body) < MIN_CONSISTENT_ROWS:
        return _result("unstructured_text", "none", "medium", readable=False,
                       evidence=[f"only {len(body)} line(s) look like data rows"],
                       note="Readme, log or metadata. It may still hold coordinates or a "
                            "sentinel legend -- read it during triage, do not process it.")

    delim = _best_delimiter(body)
    numeric = (_numeric_field_fraction(body, delim[0]) if delim else _numeric_fraction(body))

    # A delimiter that holds across every row is stronger evidence of a table than numeric
    # density is, because a legitimate table may be mostly text columns -- a station list
    # is names, coordinate strings and dates. But prose is not rescued by punctuation: a
    # markdown table or a comma-heavy paragraph clears the delimiter test and nothing else.
    if delim and numeric >= MIN_NUMERIC_FIELD_FRACTION_DELIMITED:
        char, name, consistency = delim
        return _result("delimited_text", "delimited",
                       "high" if consistency >= 0.9 else "medium",
                       evidence=[f"{name} splits {consistency:.0%} of {len(body)} sampled "
                                 f"rows into the same field count",
                                 f"{numeric:.0%} of fields are numeric"],
                       delimiter=char, delimiter_name=name, row_consistency=consistency,
                       numeric_field_fraction=round(numeric, 3))

    if numeric < MIN_NUMERIC_FIELD_FRACTION:
        return _result("unstructured_text", "none", "medium", readable=False,
                       evidence=[f"{len(body)} candidate rows, but only {numeric:.0%} of "
                                 f"their fields are numeric",
                                 "no delimiter splits them consistently" if not delim
                                 else f"delimiter {delim[1]} found but the content is prose"],
                       note="Prose with numbers in it -- a readme, a cruise log, a station "
                            "narrative. Read it during triage: coordinates and sentinel "
                            "legends live in files like this. Do not process it as data.")

    fw = _fixed_width_columns(body)
    if fw:
        return _result("fixed_width_text", "fixed_width", "medium",
                       evidence=[f"{len(fw)} column boundaries hold across "
                                 f"{len(body)} sampled rows with no single delimiter"],
                       colspecs=fw,
                       note="Boundaries are inferred from where every sampled row has a "
                            "space. Verify them against the accession's format description "
                            "before trusting the split -- a column that is full in the "
                            "sample and empty later will shift every field after it.")

    return _result("ragged_text", "delimited", "low",
                   evidence=["numeric rows, but no delimiter and no stable column boundary"],
                   delimiter=r"\s+",
                   note="Rows do not agree on a shape. Usually several blocks concatenated, "
                        "or a wrapped record. Look at it by hand before staging.")


def _structured_text(text: str) -> Optional[Dict[str, Any]]:
    """JSON and XML decode as text but are not tables."""
    import json

    stripped = text.lstrip()
    lead = stripped[:1]
    if lead in "{[":
        looks_json = False
        try:
            json.loads(stripped)
            looks_json = True
        except ValueError:
            # The sniff window truncates a large file mid-structure, so a parse failure is
            # not a verdict. Require a quoted key or a nested container instead.
            looks_json = bool(re.search(r'"[^"\n]{1,64}"\s*:', stripped[:4096]))
        if looks_json:
            return _result("json", "none", "medium", readable=False,
                           evidence=["parses as JSON, or carries quoted keys"],
                           note="Metadata sidecar, or data in a shape only its own "
                                "submitter documents. Read the keys, then stage a table "
                                "from it.")
    if lead == "<":
        family = "xml"
        note = ("XML. In an NCEI accession this is usually ISO 19115 metadata -- read it "
                "for coordinates, instrument and time coverage, not for measurements.")
        return _result(family, "none", "medium", readable=False,
                       evidence=["text begins with an XML/SGML tag"], note=note)
    return None


def _numeric_fraction(rows: List[str]) -> float:
    """Share of whitespace/comma-separated fields across sampled rows that parse as numbers."""
    total = numeric = 0
    for row in rows:
        for field in re.split(r"[\s,;|\t]+", row.strip()):
            if not field:
                continue
            total += 1
            try:
                float(field.replace("D", "E", 1) if "D" in field else field)
                numeric += 1
            except ValueError:
                pass
    return numeric / total if total else 0.0


def _numeric_field_fraction(rows: List[str], char: str) -> float:
    """Share of delimited fields that are numbers or coordinates, splitting as csv does.

    Splitting on every comma and space instead breaks one quoted remark into a dozen words
    and dilutes the count; and a station list's positions are written `142°18'E` or
    `49-46.9858S`, which are numbers in every sense but float()'s.
    """
    from wod_etl.staging.parse import parse_coordinate

    total = numeric = 0
    for row in rows:
        try:
            fields = next(csv.reader([row], delimiter=char))
        except (csv.Error, StopIteration):
            fields = row.split(char)
        for field in fields:
            f = field.strip()
            if not f:
                continue
            total += 1
            try:
                float(f)
                numeric += 1
            except ValueError:
                if re.search(r"\d", f) and re.search(r"[NSEW\u00b0]", f) \
                        and parse_coordinate(f) is not None:
                    numeric += 1
    return numeric / total if total else 0.0


# `LAT: 21.000000`, `DATE: 2015-11-09` -- a header line a staged cast writes above its table.
# It contains digits, so it used to count as a data row with no delimiter in it, and a staged
# cast of three levels (a bottle cast) failed the consistency test and sniffed as prose.
_KEY_VALUE_LINE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_ ]{0,30}\s*[:=]\s+\S")


def _likely_data_lines(lines: List[str], sample: int = 40) -> List[str]:
    """Lines that look like data rows: not comments, not `Key: value`, and with a number."""
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith(("#", "//", "%", "*", ";", "!")):
            continue
        if _KEY_VALUE_LINE.match(s) and not any(d in s for d, _n in _DELIMITERS):
            continue
        if any(c.isdigit() for c in s):
            out.append(line.rstrip("\n"))
        if len(out) >= sample:
            break
    return out


def _header_plus_one_row(lines: List[str], row: str):
    """(delimiter, name, field_count) when a non-comment header precedes a lone data row."""
    header = None
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", "//", "%", "*", ";", "!")):
            continue
        if s == row.strip():
            break
        header = s
    if header is None:
        return None
    for char, name in _DELIMITERS:
        n = _separator_count(header, char)
        if n >= 1 and _separator_count(row, char) == n:
            return char, name, n + 1
    return None


def _separator_count(line: str, char: str) -> int:
    """How many times `char` separates fields in `line`, ignoring any inside quotes.

    A raw character count is fooled by a quoted free-text column: a station list whose last
    field is `"CTD, trawl, NORPAC, 2m-ring net"` on one row and `"CTD, NORPAC"` on the next
    has a different comma count on every row, failed the consistency test, and was filed as
    prose -- unreadable, so triage never saw the positions in it.
    """
    if char not in line:
        return 0
    if '"' not in line:
        return line.count(char)
    try:
        return len(next(csv.reader([line], delimiter=char))) - 1
    except (csv.Error, StopIteration):
        return line.count(char)


def _best_delimiter(rows: List[str]):
    """The delimiter that splits the most rows into an identical, >1 field count."""
    best = None
    for char, name in _DELIMITERS:
        counts = [_separator_count(r, char) for r in rows]
        if not any(counts):
            continue
        modal = max(set(counts), key=counts.count)
        if modal < 1:
            continue
        consistency = counts.count(modal) / len(counts)
        if consistency < 0.6:
            continue
        if best is None or (consistency, modal) > (best[2], best[3]):
            best = (char, name, consistency, modal)
    return best[:3] if best else None


def _fixed_width_columns(rows: List[str], min_gap: int = 2):
    """Column boundaries where every sampled row has whitespace, as pandas colspecs."""
    rows = [r for r in rows if r.strip()]
    if len(rows) < MIN_ROWS_FOR_FIXED_WIDTH:
        return None
    width = max(len(r) for r in rows)
    padded = [r.ljust(width) for r in rows]
    blank = [all(row[i] == " " for row in padded) for i in range(width)]

    gaps, start = [], None
    for i, is_blank in enumerate(blank):
        if is_blank and start is None:
            start = i
        elif not is_blank and start is not None:
            if i - start >= min_gap:
                gaps.append((start, i))
            start = None

    if not gaps:
        return None
    specs, pos = [], 0
    for gap_start, gap_end in gaps:
        if gap_start > pos:
            specs.append((pos, gap_start))
        pos = gap_end
    if pos < width:
        specs.append((pos, width))
    return specs if len(specs) > 1 else None


def _decode(raw: bytes):
    """Decode as text if any configured encoding accepts it without control-byte noise."""
    if b"\x00" in raw:
        return None, None
    for enc in TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        control = sum(1 for c in text if ord(c) < 32 and c not in "\r\n\t")
        if control / max(len(text), 1) > 0.05:
            return None, None
        return text, enc
    return None, None


def _disambiguate_zip(path: Path) -> Dict[str, Any]:
    """A ZIP is a spreadsheet, an OpenDocument file, or an actual archive."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile) as e:
        return {"format_family": "zip_container", "reader": "none", "readable": False,
                "note": f"claims to be a ZIP but will not open: {e}"}
    if any(n.startswith("xl/") for n in names):
        return {"format_family": "spreadsheet_xlsx", "reader": "spreadsheet",
                "note": "Excel workbook. Every sheet is a candidate table; name the one "
                        "you read in the staged copy's provenance."}
    if "mimetype" in names:
        return {"format_family": "opendocument", "reader": "spreadsheet",
                "note": "OpenDocument spreadsheet."}
    return {"format_family": "archive_zip", "reader": "none", "readable": False,
            "member_count": len(names), "members": names[:20],
            "note": "A real archive. Extract it into data/raw/ and list the members; do "
                    "not process the archive itself."}


# Families that are documentation or a container by nature. A caller skips these; anything
# else that will not read is a failure, because something put it in a data folder and it did
# not parse -- and a silent skip there is how a real cast file with an unrecognised layout
# disappears from a run.
DOCUMENT_FAMILIES = {"pdf", "gzip", "bzip2", "xz", "sqlite", "access_db", "archive_zip",
                     "json", "xml", "zip_container"}


def _result(family: str, reader: str, confidence: str, readable: Optional[bool] = None,
            evidence: Optional[List[str]] = None, **extra) -> Dict[str, Any]:
    out = {"format_family": family, "reader": reader, "confidence": confidence,
           "readable": readable if readable is not None else reader != "none",
           "evidence": evidence or [], **extra}
    out["is_document"] = out["format_family"] in DOCUMENT_FAMILIES
    return out
