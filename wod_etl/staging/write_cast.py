"""Write one staged cast in the form Phase 1 can read."""
import csv
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from wod_etl import runlog

log = logging.getLogger(__name__)

# Keys Phase 1's header parser recognises as coordinates. Written bare, without the
# comment marker, because that is the `Key: value` form it scans for.
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

    The header block is what gives a cast its position and time when the data file has
    none of its own. `metadata` becomes `# Key: value` lines, which
    `generate_field_mapping` mines for Table 4a secondary headers -- Accession, Platform,
    Station, Cast, Bottom Depth and Instrument are all recognised. `notes` are free text.

    **Record every derivation here.** A depth computed from pressure, a position taken
    from a readme PDF, a depth of 0 applied by convention, an identification inferred
    across two files: each belongs in `notes`, because the staged file is the only place a
    later reader can see that the value was not in the source.

        write_staged_cast(
            out / "C145-001.csv",
            columns=["Depth (m)", "T090C", "Sal00"], rows=levels,
            latitude=62.16, longitude=-170.500167, date="2023-06-20", time="14:55",
            metadata={"Accession": "0287423", "Platform": "Oshoro maru",
                      "Station": "a01", "Bottom Depth": 47},
            notes=["Depth computed from PrDM by UNESCO 1983 at the cast latitude"])

    Column names must carry their units -- `Temp (deg C)`, `DO (mg/L)`, `Depth (m)` --
    because unit detection reads the full raw column name and nothing else.

    `sources` names the raw files the cast was built from, one `# Source:` line each; they
    reach the NetCDF as `source_files_raw`, and `notes` reach it as `comment`. `time_zone`
    states the zone of `date` and `time` when it is not UTC (`"UTC+10"`, `"local"`): a
    numeric offset with a time of day is converted to UTC by the writer, anything else is
    recorded and left alone.
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

    # Logged to the accession's run log when the path names one: a staged cast is a
    # judgement, and its notes are the record of it.
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
    """Write a harvested table -- a station list read out of a PDF, a trawl log -- as CSV.

    Not a cast: it has no position of its own and `process_accession` skips it as a sidecar
    or by its contents. It exists so a value taken from a report can be read back, cited and
    cross-checked (`read_station_list`, `reconcile_stations`) like any other file. `source`
    is the citation -- `extract_pdf_rows` returns one in `info["source"]`.
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
