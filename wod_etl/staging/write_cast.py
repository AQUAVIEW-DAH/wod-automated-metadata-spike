"""Write one staged cast in the form discovery reads."""
import csv
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from wod_etl import runlog

log = logging.getLogger(__name__)

# Coordinate keys the header parser scans for, written without the comment marker.
_COORD_KEYS = ("LAT:", "LON:", "DATE:", "TIME:")


def write_staged_cast(
    output_path,
    columns: List[str],
    rows: Iterable[Iterable[Any]],
    *,
    latitude: float,
    longitude: float,
    date: str,
    time: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    notes: Optional[List[str]] = None,
    sources: Optional[List[str]] = None,
    time_zone: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a header block plus a CSV table to `output_path`.

    `metadata` becomes `# Key: value` lines, mined for WOD metadata
    (Accession, Platform, Station, Cast, Bottom Depth, Instrument). `notes` record
    derivations not in the source (e.g. depth from pressure) and reach the NetCDF as
    `comment`; `sources` become `# Source:` lines and `source_files_raw`.

    Column names must carry their units (`Temp (deg C)`): unit detection reads only the
    name. `time_zone` states the zone of `date`/`time` when not UTC (`"UTC+10"`, `"local"`);
    a numeric offset with a time of day is converted to UTC by the writer.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# {k}: {v}" for k, v in (metadata or {}).items() if v not in (None, "")]
    lines += [f"# Source: {src}" for src in (sources or []) if src]
    lines += [f"# Note: {n}" for n in (notes or []) if n]
    lines += [f"LAT: {float(latitude):.6f}", f"LON: {float(longitude):.6f}", f"DATE: {date}"]
    if time:
        lines.append(f"TIME: {time}")
    if time_zone:
        lines.append(f"TIME_ZONE: {time_zone}")

    n = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        w = csv.writer(f)
        w.writerow(columns)
        for row in rows:
            w.writerow(list(row))
            n += 1

    # Logged to the accession's run log when the path names one.
    with runlog.accession_log_for(output_path, banner=False):
        runlog.event(log, logging.INFO,
                     f"staged {output_path}: {n} level(s) at {latitude}, {longitude} on {date}"
                     + (f" {time}" if time else ""),
                     kind="staged_cast", path=str(output_path), rows=n, columns=list(columns),
                     metadata=metadata or {}, notes=list(notes or []),
                     sources=list(sources or []))
    return {"output_path": str(output_path), "header_lines": len(lines),
            "columns": list(columns), "data_rows": n}


def write_staged_table(
    output_path,
    columns: List[str],
    rows: Iterable[Iterable[Any]],
    *,
    source: str,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Write a harvested table (e.g. a station list from a PDF) as CSV with its citation.

    Not a cast: `process_accession` skips it, and `read_station_list` reads it back. `source`
    is the citation, e.g. `info["source"]` from `extract_pdf_rows`.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# Source: {source}\n")
        for note in notes or []:
            f.write(f"# Note: {note}\n")
        w = csv.writer(f)
        w.writerow(columns)
        for row in rows:
            w.writerow(list(row))
            n += 1
    return {"output_path": str(output_path), "columns": list(columns), "data_rows": n}
