"""Read a sibling station list and join it to the casts it locates.

    from wod_etl.staging import read_station_list, join_station_list

    stations, info = read_station_list("0-data/stations.csv")
    matched, report = join_station_list(["1", "2", "11"], stations)

Neither raises; rows that are not stations and casts without a station are reported.
"""
import collections
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from wod_etl.terms import english_terms
from wod_etl.time_zones import ZONE_NAMES
from wod_etl.staging.parse import parse_coordinate, parse_timestamp
from wod_etl.staging.tables import read_table

# Column-name patterns, tried against the lower-cased header. Ordered: the first column that
# matches a role takes it, so 'Station No.' is the station and not a second candidate.
_ROLE_PATTERNS = {
    "station": r"^(?:station|stn|st|stno|st_no|station_?(?:no|num|number|id|name)?)\b"
               r"|^st\.?\s*no|^station\s+no",
    "latitude": r"^lat(?:itude)?\b|latitude",
    "longitude": r"^lon(?:g(?:itude)?)?\b|longitude",
    "date": r"\bdate\b|^day$",
    "time": r"\btime\b(?!\s*zone)",
    "year": r"^year$|^yr$",
}

# A zone named in a header: 'Date time (UTC)', 'start time (local)', 'Time [JST]'.
_ZONE_IN_HEADER = re.compile(
    r"[\(\[]\s*(utc|gmt|ut|z|local|lt|ship'?s?\s*time|"
    + "|".join(re.escape(z) for z in ZONE_NAMES) + r")\s*[\)\]]", re.I)


def normalise_station(value: Any) -> str:
    """A comparable station key: 'St-01', 'stn 1', 'Station 1' and '1' are all '1'.

    A non-numeric key keeps its letters, lower-cased.
    """
    s = str(value or "").strip().lower()
    s = re.sub(r"^(?:station|stn|st)[\s._#-]*(?:no\.?)?[\s._#-]*", "", s)
    s = re.sub(r"[\s_-]+", "", s)
    if re.fullmatch(r"\d+(?:\.0+)?", s):
        return str(int(float(s)))
    return s


def _header_row_below_title(path, delimiter) -> int:
    """The header row of a list that opens with a title line, as an index into the
    non-blank rows; 0 when it opens with its header."""
    from wod_etl.config import TEXT_ENCODINGS
    from wod_etl.discovery.profile_raw_data import delimited_layout

    for enc in TEXT_ENCODINGS:
        try:
            lines = Path(path).read_text(encoding=enc).splitlines()
            break
        except (UnicodeDecodeError, OSError):
            continue
    else:
        return 0
    layout = delimited_layout(lines, delimiter or ",")
    if not layout:
        return 0
    # Counted as read_table counts rows: a line of bare delimiters (',,,') is blank.
    return sum(1 for line in lines[:layout["header_line"]]
               if line.replace(delimiter or ",", "").strip())


def _detect_columns(columns: List[str], given: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    out = dict(given)
    taken = {c for c in given.values() if c}
    for role, pattern in _ROLE_PATTERNS.items():
        if out.get(role):
            continue
        for c in columns:
            if c in taken or not c:
                continue
            if re.search(pattern, english_terms(c).strip().lower()):
                out[role] = c
                taken.add(c)
                break
    return out


def read_station_list(
    source,
    *,
    station: Optional[str] = None,
    latitude: Optional[str] = None,
    longitude: Optional[str] = None,
    date: Optional[str] = None,
    time: Optional[str] = None,
    year: Optional[str] = None,
    time_zone: Optional[str] = None,
    header_row: int = 0,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = ",",
) -> Tuple["collections.OrderedDict", Dict[str, Any]]:
    """Parse a station list into `{station_key: entry}`, in file order.

    `source` is a path, or `(columns, rows)` from `read_table`. Column roles are detected
    from the header (`Station No.`, `Lat`, `Long`, `Date`, `Year`, `Time`) or named here.

    Each entry holds `station` (as written), `latitude`, `longitude`, `date` (YYYY-MM-DD),
    `time` (HH:MM, or None), `time_known`, `time_zone`, `row` and `raw` (cells as written).

    `info` records the column roles, dropped rows and why, and repeated station keys. A
    repeated key is a re-occupied station, kept as `key#2`, `key#3`; group its casts on
    station and date.

    `time_zone` is recorded, not converted; a zone named in a header (`Date time (UTC)`)
    takes precedence.
    """
    if isinstance(source, (str, Path)) and header_row == 0:
        header_row = _header_row_below_title(source, delimiter)
    if isinstance(source, (str, Path)):
        columns, rows, read_info = read_table(source, header_row=header_row,
                                              encoding=encoding, delimiter=delimiter,
                                              comment_prefix="#")
        origin = Path(source).name
    else:
        columns, rows = source
        read_info = {}
        origin = "table"
    columns = [str(c).strip() for c in columns]

    roles = _detect_columns(columns, {"station": station, "latitude": latitude,
                                      "longitude": longitude, "date": date, "time": time,
                                      "year": year})
    info: Dict[str, Any] = {"source": origin, "columns_used": roles, "rows": len(rows),
                            "parsed": 0, "dropped": [], "duplicates": [],
                            "read": read_info or None}
    missing = [r for r in ("station", "latitude", "longitude") if not roles.get(r)]
    if missing:
        info["error"] = (f"no column found for {missing}. Columns: {columns}. Name them "
                         f"explicitly, e.g. read_station_list(path, latitude='Lat N').")
        return collections.OrderedDict(), info

    zone = time_zone
    zone_source = "argument" if time_zone else None
    for role in ("time", "date"):
        col = roles.get(role)
        m = _ZONE_IN_HEADER.search(col or "")
        if m:
            zone, zone_source = m.group(1).upper(), f"column header {col!r}"
            break
    info["time_zone"] = zone
    info["time_zone_source"] = zone_source

    idx = {c: i for i, c in enumerate(columns) if c}
    cell = lambda r, role: (r[idx[roles[role]]].strip()
                            if roles.get(role) and idx[roles[role]] < len(r) else "")

    def position(r, role):
        """A coordinate in one cell, or split over the unnamed cells after it: 62 | 9.6 | N.
        Those cells are read only when they hold minutes (after whole degrees) or a
        hemisphere."""
        j = columns.index(roles[role])
        cell = lambda k: r[k].strip() if k < len(r) else ""
        degrees, rest = cell(j), []
        for k in range(j + 1, min(j + 3, len(columns))):
            if columns[k]:
                break
            rest.append(cell(k))
        minutes = hemisphere = None
        if rest and re.fullmatch(r"-?\d{1,3}", degrees) \
                and re.fullmatch(r"\d{1,2}(?:\.\d+)?", rest[0]) and float(rest[0]) < 60:
            minutes = rest.pop(0)
        if rest and re.fullmatch(r"[NSEW]", rest[0], re.I):
            hemisphere = rest[0]
        if minutes is not None:
            return parse_coordinate(degrees, minutes, hemisphere)
        return parse_coordinate(f"{degrees} {hemisphere}" if hemisphere else degrees)

    stations: "collections.OrderedDict" = collections.OrderedDict()
    seen = collections.Counter()
    for i, r in enumerate(rows):
        raw_station = cell(r, "station")
        lat = position(r, "latitude")
        lon = position(r, "longitude")
        if not raw_station or lat is None or lon is None:
            text = " | ".join(c for c in r if c.strip())[:120]
            reason = ("no station key" if not raw_station else
                      f"position does not parse ({cell(r, 'latitude')!r}, "
                      f"{cell(r, 'longitude')!r})")
            info["dropped"].append({"row": i, "reason": reason, "text": text})
            continue
        stamp = parse_timestamp(cell(r, "date"), cell(r, "time") or None,
                                year=cell(r, "year") or None) if cell(r, "date") else None
        if cell(r, "date") and stamp is None:
            info.setdefault("unparsed_dates", []).append(
                {"row": i, "station": raw_station, "date": cell(r, "date")})
        key = normalise_station(raw_station)
        seen[key] += 1
        if seen[key] > 1:
            info["duplicates"].append({"station": raw_station, "row": i,
                                       "occurrence": seen[key]})
            key = f"{key}#{seen[key]}"
        # The time may be in its own column or inside a combined date-time cell.
        time_known = stamp is not None and bool(
            cell(r, "time") or re.search(r"\d:\d{2}|\dT\d{2}", cell(r, "date")))
        stations[key] = {
            "station": raw_station,
            "latitude": lat,
            "longitude": lon,
            "date": f"{stamp:%Y-%m-%d}" if stamp else None,
            "time": f"{stamp:%H:%M}" if stamp and time_known else None,
            "time_known": time_known,
            "time_zone": zone,
            "row": i,
            "raw": {c: (r[j].strip() if j < len(r) else "") for c, j in idx.items()},
            "source": f"{origin} row {i}",
        }
        info["parsed"] += 1

    if info["duplicates"]:
        info["note"] = ("Some station keys repeat: a station occupied more than once. Match "
                        "casts on station AND date, not station alone.")
    return stations, info


def join_station_list(
    cast_keys: Iterable[Any],
    stations: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[Any, Dict[str, Any]], Dict[str, Any]]:
    """Match casts to station-list entries on a normalised station key.

    Returns `(matched, report)`: `matched` maps each cast key to its station entry; `report`
    names casts with no station and stations with no cast (often one cast spelt two ways).
    """
    by_norm = {k: v for k, v in stations.items()}
    matched: Dict[Any, Dict[str, Any]] = {}
    unmatched_casts = []
    used = set()
    for key in cast_keys:
        norm = normalise_station(key)
        entry = by_norm.get(norm)
        if entry is None:
            unmatched_casts.append(key)
            continue
        matched[key] = entry
        used.add(norm)
    unused = [v["station"] for k, v in stations.items() if k not in used and "#" not in k]
    return matched, {
        "matched": len(matched),
        "casts_without_station": unmatched_casts,
        "stations_without_cast": unused,
        "note": (None if not (unmatched_casts or unused) else
                 "Reconcile both lists before staging: an unmatched name on each side is "
                 "usually the same cast spelt two ways. Never skip one silently."),
    }
