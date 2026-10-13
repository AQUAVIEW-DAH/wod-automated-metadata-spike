"""Stage one haul of biological data -- plankton counts, biomass -- as WOD Taxa-Records.

WOD does not store plankton as a profile: a net haul samples a depth RANGE, and each
observation is a taxon with qualifiers rather than a value at a level. A haul is the
counterpart of a cast. It has a position and a date like a cast, a
Biological Header (Table 6a) describing how the sample was COLLECTED -- gear, tow type, mesh,
volume filtered -- and any number of Taxa-Records (Table 7a), each a taxon or biomass value
over a depth RANGE:

    # Record type: WOD taxa-records
    # Accession: 0303928
    # Platform: Shunyo-Maru
    # Station: 1
    # Bio Gear code: NORPAC Net
    # Bio Mesh size: 100
    # Bio Type of tow: vertical tow
    # Source: data/raw/0303928/data/0-data/U2015-015_NORPAC.csv
    # Note: blank cells in the source matrix read as 'not found in this sample'
    LAT: 21.000000
    LON: 142.300000
    DATE: 2015-11-09
    Taxon,Life stage,Upper depth (m),Lower depth (m),Count,Unit
    Acartia negligens,adult,0,150,100,number per sample

`write_staged_haul` writes that; `read_staged_haul` reads it back, and
`wod_etl.orchestration.process_taxa` maps, writes and validates a folder of them. Every field
name is checked against the manual's tables, and anything that is not a Table 6a or 7a field
is reported rather than dropped.
"""
import csv
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl import runlog
from wod_etl.reference import biological_headers, missing_biological_fields

log = logging.getLogger(__name__)

HAUL_MARKER = "Record type: WOD taxa-records"

# Taxa-Record columns, by Table 7a field id. The column name is matched case-insensitively
# with any bracketed unit removed. Field 1 (variable number) is not a column: it is resolved
# from the taxon name.
RECORD_COLUMNS: Dict[int, str] = {
    2: r"upper[\s_]*depth|depth[\s_]*(?:from|top|min)",
    3: r"lower[\s_]*depth|depth[\s_]*(?:to|bottom|max)",
    4: r"biomass(?:[\s_]*value)?",
    5: r"life[\s_]*stage|stage",
    6: r"sex",
    7: r"(?:taxon[\s_]*)?present|presence",
    10: r"count|number|abundance|individuals",
    11: r"sample[\s_]*volume",
    12: r"(?:taxon[\s_]*)?volume|displacement[\s_]*volume",
    13: r"wet[\s_]*weight",
    14: r"dry[\s_]*weight",
    15: r"ash[\s_-]*free[\s_]*(?:dry[\s_]*)?weight",
    18: r"size[\s_]*min",
    19: r"size[\s_]*max",
    20: r"unit|originator'?s?[\s_]*unit",
    21: r"radius",
    22: r"length",
    23: r"width",
    25: r"carbon(?:[\s_]*content)?",
}
TAXON_COLUMN = r"taxon|taxa(?:[\s_]*or[\s_]*species)?|species|scientific[\s_]*name|taxonomic[\s_]*name|name"


def _strip_unit(name: str) -> str:
    return re.sub(r"[\(\[].*?[\)\]]", "", str(name)).strip()


def biological_field(name_or_id) -> Optional[int]:
    """The Table 6a id for a field name ('Mesh size', 'mesh', 'b_3') or id, or None."""
    fields = biological_headers()
    text = str(name_or_id).strip()
    m = re.fullmatch(r"(?:b_?)?(\d+)", text, re.I)
    if m and int(m.group(1)) in fields:
        return int(m.group(1))
    norm = re.sub(r"[^a-z0-9]+", " ", _strip_unit(text).lower()).strip()
    for fid, f in fields.items():
        if re.sub(r"[^a-z0-9]+", " ", f["name"].lower()).strip() == norm:
            return fid
    aliases = {"gear": 7, "net": 7, "mesh": 3, "tow type": 4, "tow": 4,
               "volume filtered": 1, "filtered volume": 1, "duration": 2,
               "preservation": 10, "flowmeter": 18, "mouth area": 9}
    return aliases.get(norm)


def taxa_record_field(column: str) -> Optional[int]:
    """The Table 7a field id a staged column holds (1 for the taxon name), or None."""
    base = _strip_unit(column).lower()
    if re.fullmatch(TAXON_COLUMN, base):
        return 1
    for fid, pattern in RECORD_COLUMNS.items():
        if re.fullmatch(pattern, base):
            return fid
    return None


def write_staged_haul(
    output_path,
    records,
    *,
    latitude: float,
    longitude: float,
    date: str,
    time: Optional[str] = None,
    time_zone: Optional[str] = None,
    upper_depth: Optional[float] = None,
    lower_depth: Optional[float] = None,
    biological_header: Optional[Dict[Any, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    notes: Optional[List[str]] = None,
    sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Write one haul. `records` is `(columns, rows)` or a list of dicts.

    `upper_depth`/`lower_depth` fill the depth range for every record that does not carry
    its own -- a NORPAC haul's 0-150 m applies to every taxon in it. `biological_header` is
    keyed by Table 6a name or id (`{"Gear code": "NORPAC Net", "Mesh size": 100}`); a key
    that is not a Table 6a field is NOT written as one -- it becomes a note and is listed in
    `unknown_biological_fields`.

    Returns the path, the record count, and `missing_biological_fields`: the Table 6a fields
    a reader needs to interpret counts that this haul does not state. Report them.
    """
    if isinstance(records, tuple):
        columns, rows = list(records[0]), [list(r) for r in records[1]]
    else:
        records = list(records)
        columns = list(dict.fromkeys(k for r in records for k in r))
        rows = [[r.get(c, "") for c in columns] for r in records]

    fields = [taxa_record_field(c) for c in columns]
    for fid, value, label in ((2, upper_depth, "Upper depth (m)"),
                              (3, lower_depth, "Lower depth (m)")):
        if fid not in fields:
            if value is None:
                continue
            columns.append(label)
            fields.append(fid)
            rows = [r + [value] for r in rows]

    bio_lines, unknown = [], []
    stated = set()
    names = biological_headers()
    for key, value in (biological_header or {}).items():
        if value in (None, ""):
            continue
        fid = biological_field(key)
        if fid is None:
            unknown.append(key)         # as given: '99' is not the dict's key 99
            continue
        stated.add(fid)
        bio_lines.append(f"# Bio {names[fid]['name']}: {value}")
    all_notes = list(notes or []) + [f"biological field {str(k)!r} has no Table 6a home: "
                                     f"{biological_header[k]}" for k in unknown]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {HAUL_MARKER}"]
    lines += [f"# {k}: {v}" for k, v in (metadata or {}).items() if v not in (None, "")]
    lines += bio_lines
    lines += [f"# Source: {s}" for s in (sources or []) if s]
    lines += [f"# Note: {n}" for n in all_notes if n]
    lines += [f"LAT: {float(latitude):.6f}", f"LON: {float(longitude):.6f}", f"DATE: {date}"]
    if time:
        lines.append(f"TIME: {time}")
    if time_zone:
        lines.append(f"TIME_ZONE: {time_zone}")
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(r)

    missing = missing_biological_fields(stated)
    with runlog.accession_log_for(output_path, banner=False):
        runlog.event(log, logging.INFO,
                     f"staged haul {output_path}: {len(rows)} Taxa-Record(s)"
                     + (f"; not stated: {', '.join(missing)}" if missing else ""),
                     kind="staged_haul", path=str(output_path), records=len(rows),
                     missing_biological_fields=missing, notes=all_notes,
                     sources=list(sources or []))
    return {"output_path": str(output_path), "records": len(rows), "columns": columns,
            "unmapped_columns": [c for c, f in zip(columns, fields) if f is None],
            "unknown_biological_fields": [str(k) for k in unknown] or None,
            "missing_biological_fields": missing or None}


def is_staged_haul(path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return HAUL_MARKER in f.readline()
    except OSError:
        return False


def read_staged_haul(path) -> Dict[str, Any]:
    """`{"header_text", "biological_header", "columns", "rows", "fields", "unmapped_columns"}`.

    `fields` gives each column's Table 7a id (1 for the taxon name), None for a column that
    is not a Taxa-Record field.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    header, body_start = [], 0
    for i, line in enumerate(lines):
        if line.startswith("#") or re.match(r"^[A-Z_]+:\s", line):
            header.append(line)
            continue
        body_start = i
        break
    reader = list(csv.reader(lines[body_start:]))
    columns = [c.strip() for c in reader[0]] if reader else []
    rows = [r for r in reader[1:] if any(c.strip() for c in r)]

    names = biological_headers()
    bio: Dict[int, Dict[str, Any]] = {}
    other_header = []
    for line in header:
        m = re.match(r"#\s*Bio\s+(.+?):\s*(.*)$", line)
        if m:
            fid = biological_field(m.group(1))
            if fid is not None:
                bio[fid] = {"name": names[fid]["name"], "value": m.group(2).strip(),
                            "unit": names[fid].get("unit"),
                            "appendix": names[fid].get("appendix")}
                continue
        if HAUL_MARKER in line:
            continue
        other_header.append(line)
    fields = [taxa_record_field(c) for c in columns]
    return {"source": str(path), "header_text": "\n".join(other_header), "biological_header": bio,
            "columns": columns, "rows": rows, "fields": fields,
            "unmapped_columns": [c for c, f in zip(columns, fields) if f is None]}
