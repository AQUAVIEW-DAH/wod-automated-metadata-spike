"""Write one staged cast in the form Phase 1 can read."""
import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# {k}: {v}" for k, v in (metadata or {}).items() if v not in (None, "")]
    lines += [f"# Note: {n}" for n in (notes or []) if n]
    lines += [f"LAT: {float(latitude):.6f}", f"LON: {float(longitude):.6f}", f"DATE: {date}"]
    if time:
        lines.append(f"TIME: {time}")

    n = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        w = csv.writer(f)
        w.writerow(columns)
        for row in rows:
            w.writerow(list(row))
            n += 1

    return {"output_path": str(output_path), "header_lines": len(lines),
            "columns": list(columns), "data_rows": n}
