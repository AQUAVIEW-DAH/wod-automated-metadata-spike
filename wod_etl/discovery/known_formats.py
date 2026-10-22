"""Readers for self-identifying text formats that generic readers misread.

- WOD's own CSV export: a block per cast of `Key,,value,unit` rows, then a `VARIABLES`
  table with a WOD flag (F) and an originator flag (O) per variable.
- XCTD and XBT `.dat` files with a one-line header and unlabelled columns:
  `# XCTD YK17-10 20170516 1251 17-00.14N 143-29.97E 1001`, then depth, temperature
  (and salinity) and a flag string per level.
- Tsurumi-Seiki XCTD exports.

Each returns casts in `read_any`'s shape: `{"cast_id", "df", "header_block", "source_note"}`.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# --------------------------------------------------------------------- WOD CSV
WOD_CSV_MARKERS = ("\nCAST", "VARIABLES ,", "END OF VARIABLES SECTION")

# Header rows whose value goes into the header block, under the key the mapper reads.
_WOD_CSV_KEYS = {"originators station id": "Station", "originators cruise id": "Cruise",
                 "latitude": "Latitude", "longitude": "Longitude", "platform": "Platform",
                 "institute": "Institution", "country": "Country", "probe_type": "Probe type",
                 "bottom depth": "Bottom depth", "project": "Project"}


def is_wod_csv(text: str) -> bool:
    return all(m in text for m in WOD_CSV_MARKERS[:2]) and "UNITS" in text


def _code(value: str) -> str:
    """'5423.' -> '5423': WOD CSV writes codes as floats."""
    return re.sub(r"^(-?\d+)\.0*$", r"\1", value.strip())


def _wod_name(short: str) -> str:
    """A WOD CSV variable name, which is cut at ten characters ('Temperatur'), in full."""
    from wod_etl.reference import wod_variables

    short = short.strip()
    for v in wod_variables().values():
        name = str(v.get("name") or "")
        if name and len(short) >= 4 and name.lower().startswith(short.lower()):
            return name
    return short


def read_wod_csv(path, encoding: str = "utf-8") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    text = open(path, encoding=encoding, errors="replace").read()
    casts, warnings, flags_dropped = [], [], 0
    blocks = re.split(r"\n(?=CAST\s*,)", "\n" + text)
    for n, block in enumerate(b for b in blocks if b.lstrip().startswith("CAST")):
        lines = [l for l in block.splitlines() if l.strip()]
        header, date, when = [], {}, None
        table_at = next((i for i, l in enumerate(lines) if l.startswith("VARIABLES")), None)
        if table_at is None:
            warnings.append(f"cast {n}: no VARIABLES section")
            continue
        for line in lines[:table_at]:
            f = [x.strip() for x in line.split(",")]
            if len(f) < 3 or not f[2]:
                continue
            key, value, unit = f[0].lower(), _code(f[2]), (f[3] if len(f) > 3 else "")
            if key in ("year", "month", "day"):
                date[key] = int(float(value))
            elif key == "time":
                zone = re.search(r"\(([A-Za-z]{1,5})\)", unit)
                if "minute" in unit.lower() and re.fullmatch(r"\d{1,4}", value):
                    when = f"{int(value) // 100:02d}:{int(value) % 100:02d}"
                else:
                    # Rounded to the second as a whole, so 19.995 h is 19:59:42, not 19:60.
                    seconds = min(round(float(value) * 3600), 86399)
                    when = "{:02d}:{:02d}:{:02d}".format(seconds // 3600, seconds // 60 % 60,
                                                         seconds % 60)
                if zone:
                    header.append(f"TIME_ZONE: {zone.group(1)}")
            elif key in _WOD_CSV_KEYS:
                header.append(f"{_WOD_CSV_KEYS[key]}: {value}")
        if {"year", "month", "day"} <= date.keys():
            header.append(f"Date: {date['year']:04d}-{date['month']:02d}-{date['day']:02d}")
        if when:
            header.append(f"Time: {when}")

        names = [x.strip() for x in lines[table_at].split(",")][1:]
        units = [x.strip() for x in lines[table_at + 1].split(",")][1:] \
            if table_at + 1 < len(lines) and lines[table_at + 1].startswith("UNITS") else []
        columns, keep = [], []
        current = None
        for i, name in enumerate(names):
            if name == "F":
                flags_dropped += 1
                continue
            if name == "O":
                if current:
                    columns.append(f"{current}_QC")
                    keep.append(i)
                continue
            if not name:
                continue
            current = _wod_name(name)
            unit = units[i] if i < len(units) else ""
            columns.append(f"{current} ({unit})" if unit else current)
            keep.append(i)
        rows = []
        for line in lines[table_at + 1:]:
            if line.startswith(("UNITS", "Prof-Flag")):
                continue
            if line.startswith("END OF VARIABLES"):
                break
            f = [x.strip() for x in line.split(",")][1:]
            rows.append([f[i] if i < len(f) else "" for i in keep])
        df = pd.DataFrame(rows, columns=columns)
        for c in df.columns:
            df[c] = pd.to_numeric(df[c].replace("", None), errors="coerce") \
                if not c.endswith("_QC") else df[c].replace("", None)
        df = df.dropna(axis=1, how="all")
        casts.append({"cast_id": str(n), "df": df, "header_block": "\n".join(header),
                      "source_note": f"WOD CSV, cast {n}"})
    if flags_dropped:
        warnings.append("WOD's own QC flags (F) were not carried: WOD applies its QC again; "
                        "the originator's flags (O) were")
    return casts, {"warnings": warnings}


# ----------------------------------------------------------------- XCTD / XBT
_PROBE_HEADER = re.compile(
    r"^#\s*(?P<probe>XCTD|XBT)\s+(?P<cruise>\S+)\s+(?P<date>\d{8})\s+(?P<time>\d{4})\s+"
    r"(?P<lat>\d{1,2}-\d{1,2}(?:\.\d+)?[NS])\s+(?P<lon>\d{1,3}-\d{1,2}(?:\.\d+)?[EW])"
    r"(?:\s+(?P<n>\d+))?", re.I)
# Unlabelled columns by probe. The flag string has one letter per column.
_PROBE_COLUMNS = {"XCTD": ["Depth (m)", "Temperature (deg C)", "Salinity"],
                  "XBT": ["Depth (m)", "Temperature (deg C)"]}
# The files mark a missing value as -5.
_PROBE_MISSING = -5.0


def probe_header(text: str) -> Optional[Dict[str, str]]:
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = _PROBE_HEADER.match(first)
    return m.groupdict() if m else None


def read_probe_dat(path, encoding: str = "utf-8") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from wod_etl.staging.parse import parse_coordinate

    lines = open(path, encoding=encoding, errors="replace").read().splitlines()
    head = probe_header("\n".join(lines[:1]))
    probe = head["probe"].upper()
    names = _PROBE_COLUMNS[probe]
    rows, flags = [], []
    for line in lines[1:]:
        f = line.split()
        if len(f) < len(names):
            continue
        try:
            values = [float(x) for x in f[:len(names)]]
        except ValueError:
            continue
        flag = f[len(names)] if len(f) > len(names) else ""
        # A value flagged N has no data, whatever number stands in it (-5, -9).
        rows.append([None if v == _PROBE_MISSING or (i < len(flag) and flag[i] == "N") else v
                     for i, v in enumerate(values)])
        flags.append(flag)
    df = pd.DataFrame(rows, columns=names)
    for i, name in enumerate(names[1:], start=1):
        df[f"{name.split(' (')[0]}_QC"] = [fl[i] if len(fl) > i else None for fl in flags]
    lat, lon = parse_coordinate(head["lat"]), parse_coordinate(head["lon"])
    d, t = head["date"], head["time"]
    header = [f"Cruise: {head['cruise']}", f"Probe type: {probe}",
              f"Latitude: {lat}", f"Longitude: {lon}",
              f"Date: {d[:4]}-{d[4:6]}-{d[6:]}", f"Time: {t[:2]}:{t[2:]}"]
    note = (f"{probe} file with unlabelled columns, read as {', '.join(names)} and a flag "
            f"string; a value flagged N, or -5, is missing")
    return [{"cast_id": "0", "df": df, "header_block": "\n".join(header),
             "source_note": note}], {"warnings": [note]}


# ------------------------------------------------------------ TSK XCTD export
# Tsurumi-Seiki XCTD export: a 256-byte header record
#   serial, material, YYYYMMDD, HHMMSS (UTC), DD-MM.MMMMN, DDD-MM.MMMMW, probe (CT1/CT4), n, step
# then one record per level: depth, temperature, conductivity, salinity[, sound velocity,
# density]. A cast may come twice: binned to 1 m and with every sample.
_TSK_HEADER = re.compile(
    r"^\s*(?P<serial>\d{5,9})\s*,\s*\d{1,6}\s*,\s*(?P<date>\d{8})\s*,\s*(?P<time>\d{6})\s*,"
    r"\s*(?P<lat>\d{1,2}-\d{1,2}\.\d+[NS])\s*,\s*(?P<lon>\d{1,3}-\d{1,2}\.\d+[EW])\s*,"
    r"\s*(?P<probe>CT\d|T-?\d+)\s*,\s*(?P<n>\d+)\s*,\s*(?P<step>[^,]*?)\s*,")
_TSK_COLUMNS = ["Depth (m)", "Temperature (deg C)", "Conductivity (mS/cm)", "Salinity",
                "Sound velocity (m/s)", "Density (kg/m3)"]


def tsk_header(text: str) -> Optional[Dict[str, str]]:
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = _TSK_HEADER.match(first)
    return m.groupdict() if m else None


def read_tsk(path, encoding: str = "utf-8") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from wod_etl.staging.parse import parse_coordinate

    lines = open(path, encoding=encoding, errors="replace").read().splitlines()
    head = tsk_header("\n".join(lines[:1]))
    rows = []
    for line in lines[1:]:
        f = [x.strip() for x in line.split(",") if x.strip()]
        try:
            rows.append([float(x) for x in f])
        except ValueError:
            continue
    width = max((len(r) for r in rows), default=0)
    df = pd.DataFrame([r + [None] * (width - len(r)) for r in rows],
                      columns=_TSK_COLUMNS[:width])
    d, t = head["date"], head["time"]
    probe = "XCTD" if head["probe"].upper().startswith("CT") else "XBT"
    header = [f"Probe type: {probe}", f"Instrument: {head['probe']} serial {head['serial']}",
              f"Latitude: {parse_coordinate(head['lat'])}",
              f"Longitude: {parse_coordinate(head['lon'])}",
              f"Date: {d[:4]}-{d[4:6]}-{d[6:]}", f"Time: {t[:2]}:{t[2:4]}:{t[4:]}",
              "TIME_ZONE: UTC"]
    return [{"cast_id": "0", "df": df, "header_block": "\n".join(header),
             "source_note": f"Tsurumi-Seiki {probe} export, depth step {head['step']}"}], \
        {"warnings": []}
