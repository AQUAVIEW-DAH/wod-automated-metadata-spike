"""Build the mapping contract that every later phase reads.

Decides, per raw column, the WOD variable and unit conversion, and maps the header metadata.
"""
import re
from typing import Dict, Any, List, Optional

from wod_etl.mapping.map_metadata import map_metadata
from wod_etl.mapping.query_wod_spec import (
    DEPTH_SENTINEL_CODE,
    PER_VOLUME_CODES,
    canonical_unit_text,
    explain_column,
    extract_scale,
    fluorescence_rule,
    extract_unit,
    match_variable,
    normalize_name,
    wod_secondary_headers,
    wod_variables,
)

# Unit tokens searched in the full raw column name; the first match wins.
_UNIT_PATTERNS = [
    ("fahrenheit_to_celsius", r"(?:deg[\s_]*f\b|fahrenheit|\bdegf\b|°\s*f\b)"),
    ("feet_to_meters",        r"(?:\bfeet\b|\bfoot\b|\bft\b)"),
    ("fathoms_to_meters",     r"(?:\bfathom|\bfath\b|\bfm\b)"),
    ("ml_per_l_to_umol_per_kg", r"(?:ml\s*[/·.]?\s*l\b|ml\s*l-?1|milli?liter\s*per\s*liter)"),
    ("mg_per_l_to_umol_per_kg", r"(?:\bmg\s*[/·.]?\s*l\b|\bmg\s*l-?1)"),
]

# Molar concentration units by SI prefix and basis (per volume or per mass).
_MOLAR_UNIT_PATTERNS = [
    ("mmol", "volume", r"(?:\bmmol\s*[/·.]?\s*l\b|\bmm\s*[/·.]?\s*l\b)"),
    ("nmol", "volume", r"(?:\bnmol\s*[/·.]?\s*l\b|\bnm\s*[/·.]?\s*l\b)"),
    ("umol", "volume", r"(?:[µu]mol\s*[/·.]?\s*l\b|[µu]m\s*[/·.]?\s*l\b)"),
    ("mmol", "mass",   r"(?:\bmmol\s*[/·.]?\s*kg)"),
    ("nmol", "mass",   r"(?:\bnmol\s*[/·.]?\s*kg)"),
    ("umol", "mass",   r"(?:[µu]mol\s*[/·.]?\s*kg)"),
]

# Amount-of-substance prefixes, as a multiple of mol.
MOLAR_PREFIX_SCALE = {"mmol": 1e-3, "umol": 1e-6, "nmol": 1e-9}

_MOLAR_ABBREV = re.compile(r"(mmol|umol|nmol)\s*/\s*(l|kg)")

# Header keys we recognise, mapped to Table 4a secondary header IDs.
_HEADER_PATTERNS = {
    1:  r"(?:accession|accession[_\s]*(?:no|number)|nodc[_\s]*accession)",
    2:  r"(?:project|programme|program)",
    # Not 'cruise', which names a voyage. Kept in step with map_metadata's
    # SECONDARY_HEADER_PATTERNS.
    3:  r"(?:platform|ship|vessel|callsign)",
    4:  r"(?:institut(?:e|ion)|agency|originator[_\s]*institution)",
    5:  r"(?:cast|cast[_\s]*(?:no|number)|tow[_\s]*(?:no|number))",
    7:  r"(?:station|station[_\s]*(?:no|number)|stn)",
    10: r"(?:bottom[_\s]*depth|water[_\s]*depth|sounding|depth[_\s]*to[_\s]*bottom)",
    22: r"(?:wind[_\s]*speed)",
    23: r"(?:barometric[_\s]*pressure|air[_\s]*pressure|baro)",
    24: r"(?:dry[_\s]*bulb|air[_\s]*temp(?:erature)?)",
    29: r"(?:probe[_\s]*type|instrument[_\s]*type)",
    34: r"(?:bottom[_\s]*hit)",
    94: r"(?:wmo[_\s]*(?:id|number|code)?)",
}

_NUM = r"[-+]?\d+(?:\.\d+)?"

# Prose lines in a staged header, skipped before any key is mined out of them.
_NOTE_LINE = re.compile(r"\s*#?\s*(?:note|comment|remark)s?\s*[:=]", re.IGNORECASE)

# `# Source: data/raw/.../file.csv` -- one per file a staged cast was built from.
_SOURCE_LINE = re.compile(r"\s*#?\s*source(?:[_\s]*file)?s?\s*[:=]\s*(.+)$", re.IGNORECASE)
# `TIME_ZONE: UTC+10` -- the zone of the date and time lines.
_TIME_ZONE_LINE = re.compile(r"\s*#?\s*time[_\s]*zone\s*[:=]\s*(.+)$", re.IGNORECASE)

# Secondary headers whose value is a measurement rather than an identifier.
_NUMERIC_HEADER_IDS = {10, 22, 23, 24}


# A second sensor ('Temperature of the water body, 2', 'secondary temperature').
_SECONDARY = re.compile(r",\s*2\s*(?:\(|$)|\bsecondary\b|\b2nd\b|\bsensor\s*2\b", re.I)


def _preferred_first(columns: List[str]) -> List[str]:
    """Columns in file order, with the one to write first among those sharing a variable.

    Primary sensor before secondary, ITS-90 before IPTS-68.
    """
    groups: Dict[Any, List[str]] = {}
    for c in columns:
        code = match_variable(c)
        groups.setdefault(code if code is not None else ("col", c), []).append(c)
    rank = lambda c: (bool(_SECONDARY.search(c)), extract_scale(c) == "IPTS-68")
    out, done = [], set()
    for c in columns:
        code = match_variable(c)
        key = code if code is not None else ("col", c)
        if key in done:
            continue
        done.add(key)
        out += sorted(groups[key], key=rank)
    return out


def generate_field_mapping(
    profile_result: Dict[str, Any],
    spec_result: Dict[str, Any],
    df: Optional[Any] = None,
) -> Dict[str, Any]:
    """Produce the mapping contract used by Phases 3 and 4.

    `df`, if given, supplies metadata column values (e.g. a constant `Bottom Depth` column).
    """
    raw_columns = profile_result.get("columns", []) or []
    raw_header_text = profile_result.get("raw_header_block", "") or ""
    variables = wod_variables()

    # Metadata first: it decides which columns are consumed and so not reported as dropped.
    metadata = map_metadata(raw_header_text, raw_columns, df,
                            spec_result.get("wod_dataset_code"))
    consumed = {m["column"] for m in metadata["metadata_columns"] if m.get("column")}
    consumed |= {f["flag_column"] for f in metadata["originator_flags"]}

    column_mappings: List[Dict[str, Any]] = []
    unmapped_cols: List[str] = []
    dropped: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_codes = set()
    # A flag column naming no variable (`flag`, `QC`) applies to every variable at a level.
    level_flags = [f["flag_column"] for f in metadata["originator_flags"]
                   if f["scope"] == "level or cast"]
    level_flag = level_flags[0] if len(level_flags) == 1 else None

    for col in _preferred_first(raw_columns):
        if col in consumed:
            continue

        code = match_variable(col)

        if code is None:
            unmapped_cols.append(col)
            reason = explain_column(col)
            if reason:
                dropped.append(reason)
            continue

        if code == DEPTH_SENTINEL_CODE:
            conv = _detect_unit_conversion(col, code)
            column_mappings.append({
                "raw_column": col,
                "originator_unit": extract_unit(col),
                "wod_code": DEPTH_SENTINEL_CODE,
                "wod_name": "Depth",
                "role": "depth_axis",
                "target_unit": "meters",
                "conversion_needed": conv if conv in ("feet_to_meters", "fathoms_to_meters") else "none",
            })
            continue

        var = variables.get(code)
        if var is None:
            unmapped_cols.append(col)
            continue

        # Table 3a lists the datasets storing each variable; others drop it and report why.
        dataset = spec_result.get("wod_dataset_code")
        spec_var = next((s for s in spec_result.get("standard_variables", [])
                         if s.get("wod_code") == code), None)
        if spec_var is not None and spec_var.get("valid_for_dataset") is False:
            dropped.append({
                "column": col, "variable": f"{var['name']}, not stored in {dataset}",
                "kind": "not_in_dataset",
                "reason": f"WOD stores {var['name']} in {', '.join(var['datasets'])}, not in "
                          f"{dataset} (Table 3a).",
                "route": "If it was measured and belongs in WOD, write the file as a dataset "
                         "that stores it: set `dataset` in its decisions.toml entry."})
            unmapped_cols.append(col)
            continue

        if code in seen_codes:
            warnings.append(
                f"Column '{col}' maps to WOD code {code} ({var['name']}), already claimed by "
                f"another column. Only the first is written."
            )
            unmapped_cols.append(col)
            continue
        seen_codes.add(code)

        conversion = _detect_unit_conversion(col, code)
        mapping = {
            "raw_column": col,
            # Kept verbatim to audit the conversion (variable specific second header 8).
            "originator_unit": extract_unit(col),
            "originator_scale": extract_scale(col),
            "wod_code": code,
            "wod_name": var["name"],
            "role": "measurement",
            "target_unit": var["standard_unit"],
            "unit_abbrev": var["unit_abbrev"],
            "conversion_needed": conversion,
            "variable_specific_headers":
                metadata["variable_specific_headers"].get(col, {}).get("headers", {}),
            "originator_flag_column": next(
                (f["flag_column"] for f in metadata["originator_flags"]
                 if f.get("flags_column") == col), None) or level_flag,
        }
        fluor = fluorescence_rule(col)
        if fluor is not None and fluor.get("code") == code:
            mapping["comment"] = fluor["comment"]
            warnings.append(f"Column '{col}' is fluorometer output written as {var['name']} "
                            f"(code {code}) because its unit is a concentration. Say so "
                            f"when reporting: it is not extracted chlorophyll-a.")
        if conversion == "molar_rescale":
            # Record the endpoints; the label alone does not give them.
            source, target = molar_units(col, code)
            mapping["conversion_from"] = f"{source[0]}/{'l' if source[1] == 'volume' else 'kg'}"
            mapping["conversion_to"] = f"{target[0]}/{'l' if target[1] == 'volume' else 'kg'}"

        column_mappings.append(mapping)

    extracted = _parse_header_block(raw_header_text)
    parsed_coords = extracted["coordinates"]
    if parsed_coords.get("year") is not None and "hour" not in parsed_coords:
        warnings.append(
            "The cast has a date but no time of day. It is written at 00:00 of that date with "
            "GMT_time left as fill, so it reads as date-only rather than as midnight."
            + (f" The date's time zone is {parsed_coords['time_zone']}; a local date may be "
               f"a different UTC date." if parsed_coords.get("time_zone") else
               " The date's time zone is not stated."))

    for entry in dropped:
        warnings.append(f"Column '{entry['column']}' is {entry['variable'].replace('_', ' ')}: "
                        f"{entry['reason']} {entry['route']}")
    unresolved_flags = [f for f in metadata["originator_flags"]
                        if f["scope"] == "unresolved"
                        or (f["scope"] == "level or cast" and level_flag is None)]
    if unresolved_flags:
        warnings.append(
            "Originator quality flag column(s) "
            + ", ".join(f"'{f['flag_column']}'" for f in unresolved_flags)
            + " could not be paired with a variable. They are kept and reported rather than "
              "dropped; say which variable they belong to once you know.")

    # Table 3a: 30 is Latitude, 31 is Longitude. Each must be found by its own code.
    _COORD_CODE = {"latitude": 30, "longitude": 31}
    missing_coords = []
    for req in ("latitude", "longitude"):
        in_header = req in parsed_coords
        in_columns = any(match_variable(c) == _COORD_CODE[req] for c in raw_columns)
        if not in_header and not in_columns:
            missing_coords.append(req)
    if not any(k in parsed_coords for k in ("year", "month", "day")):
        missing_coords.append("time")

    has_depth = any(m["wod_code"] == DEPTH_SENTINEL_CODE for m in column_mappings)
    has_pressure = any(m["wod_code"] == 25 for m in column_mappings)
    if not has_depth:
        if has_pressure:
            warnings.append(
                "No depth column found, but pressure (code 25) is present. Converting "
                "pressure to depth needs latitude and is not done automatically."
            )
        else:
            missing_coords.append("depth")

    if spec_result.get("wod_dataset_code") is None:
        warnings.append(
            "Dataset is unresolved: " + str(spec_result.get("dataset_resolution", {}).get("note", ""))
        )

    return {
        "source": profile_result.get("source", "unknown"),
        "source_format": profile_result.get("source_format"),
        "dataset_type": spec_result.get("dataset_type", ""),
        "wod_dataset_code": spec_result.get("wod_dataset_code"),
        "dataset_resolution": spec_result.get("dataset_resolution", {}),
        "column_mapping": column_mappings,
        # Header-block Table 4a values, kept for compatibility; `secondary_headers` is the
        # full set with code lookups.
        "extracted_header_metadata": extracted["secondary_headers"],
        "secondary_headers": metadata["secondary_headers"],
        # Character data (manual p.30): the originator's cruise code and PI.
        "character_data": metadata.get("character_data", {}),
        # `# Note:` and `# Source:` lines from a staged header, carried into the output.
        "provenance_notes": extracted["notes"],
        "source_files": extracted["sources"],
        "unresolved_metadata": metadata.get("unresolved", []),
        "metadata_columns": metadata["metadata_columns"],
        "originator_flags": metadata["originator_flags"],
        "parsed_coordinates": parsed_coords,
        "missing_required_coordinates": missing_coords,
        "unmapped_columns": unmapped_cols,
        "dropped_columns": dropped,
        "warnings": warnings,
        "ready_for_transform": bool(column_mappings) and not missing_coords,
    }


def parse_header_block(raw_header_text: str) -> Dict[str, Any]:
    """Coordinates, date/time, time zone, notes and sources from a `Key: value` header block."""
    return _parse_header_block(raw_header_text)


def molar_units(raw_col_name: str, wod_code: int):
    """Return (source, target) molar units for a column, or None if it is not a molar case.

    Each is a `(prefix, basis)` pair such as `("umol", "mass")`. The target comes from the
    variable's Table 3a standard unit, which varies (umol/kg, mmol/l, nmol/kg).
    """
    name = canonical_unit_text(raw_col_name).lower()
    source = None
    for prefix, basis, pattern in _MOLAR_UNIT_PATTERNS:
        if re.search(pattern, name):
            source = (prefix, basis)
            break
    if source is None:
        return None

    variable = wod_variables().get(wod_code) or {}
    abbrev = str(variable.get("unit_abbrev", "")).lower().replace("\u00b5", "u")
    m = _MOLAR_ABBREV.search(abbrev)
    if not m:
        # The standard unit is not molar (chlorophyll is ug/l): leave the values alone.
        return None
    return source, (m.group(1), "volume" if m.group(2) == "l" else "mass")


def _detect_unit_conversion(raw_col_name: str, wod_code: int) -> str:
    """Infer the conversion implied by the units written into a raw column name.

    Inspects the full name including bracketed units: 'Temp (deg F)', 'Depth [ft]'.
    """
    name = canonical_unit_text(raw_col_name).lower()
    for label, pattern in _UNIT_PATTERNS:
        if re.search(pattern, name):
            if label == "ml_per_l_to_umol_per_kg":
                return "ml_per_l_to_umol_per_kg" if wod_code == 3 else "none"

            if label == "mg_per_l_to_umol_per_kg":
                return "mg_per_l_to_umol_per_kg" if wod_code == 3 else "none"

            if label in ("feet_to_meters", "fathoms_to_meters"):
                return label if wod_code == DEPTH_SENTINEL_CODE else "none"

            if label == "fahrenheit_to_celsius":
                return label if wod_code in (1, 26) else "none"

            return label

    units = molar_units(raw_col_name, wod_code)
    if units is None:
        return "none"
    source, target = units
    return "none" if source == target else "molar_rescale"


def _parse_header_block(raw_header_text: str) -> Dict[str, Any]:
    """Scan the header comment block for coordinates, date and secondary header metadata."""
    sec_headers: Dict[str, Any] = {}
    coords: Dict[str, Any] = {}
    notes: List[str] = []
    sources: List[str] = []
    names = wod_secondary_headers()

    for line in raw_header_text.splitlines():
        # Notes are prose: collect them for `comment` instead of parsing keys from them.
        if _NOTE_LINE.match(line):
            notes.append(_NOTE_LINE.sub("", line, count=1).strip())
            continue
        m = _SOURCE_LINE.match(line)
        # Only a path is a source file; other `Source:` values name a project.
        if m and re.fullmatch(r"\S*(?:[/\\]\S+|\.[A-Za-z0-9]{2,5})", m.group(1).split()[0]):
            sources.append(m.group(1).strip())
            continue
        m = _TIME_ZONE_LINE.match(line)
        if m:
            coords.setdefault("time_zone", m.group(1).strip())
            continue

        _parse_position(line, coords)
        _parse_datetime(line, coords)
        _parse_station_depth(line, sec_headers, names)

        for sec_id, key_pat in _HEADER_PATTERNS.items():
            if str(sec_id) in sec_headers:
                continue
            # The key starts the line or follows a delimiter or column gap, never mid-sentence.
            # The value runs to the end of the line, a delimiter, or a 2+ space gap.
            m = re.search(rf"(?:^|[,;|]|\s{{2,}})\s*#?\s*{key_pat}\b(\s*[:=]\s*|\s+)"
                          rf"([^\s,;|][^,;|]*?)(?=\s{{2,}}|[,;|]|$)",
                          line, re.IGNORECASE)
            if not m:
                continue
            value: Any = m.group(2).strip().strip('"\'')
            if not re.search(r"[:=]", m.group(1)) and re.search(r"[:=]", value):
                continue                     # a longer key: 'Cast duration: 30' is not a cast
            # Only numeric headers are coerced; identifiers keep leading zeros ('0287423').
            if sec_id in _NUMERIC_HEADER_IDS and re.fullmatch(_NUM, value):
                try:
                    value = float(value)
                except ValueError:
                    pass
            sec_headers[str(sec_id)] = {"name": names.get(sec_id, f"code {sec_id}"), "value": value}

    return {"secondary_headers": sec_headers, "coordinates": coords, "notes": notes,
            "sources": sources}


def _parse_position(line: str, coords: Dict[str, Any]) -> None:
    """Parse latitude/longitude, decimal or degrees-minutes, with or without a hemisphere."""
    for key, kw in (("latitude", r"lat(?:itude)?"), ("longitude", r"lon(?:g(?:itude)?)?")):
        if key in coords:
            continue

        # Degrees + decimal minutes, e.g. "LAT: 61 55.8 N"
        m = re.search(rf"\b{kw}\b[:=\s]+({_NUM})\s+({_NUM})\s*([NSEW])?\b", line, re.IGNORECASE)
        if m and abs(float(m.group(1))) <= 180:
            deg, minutes = float(m.group(1)), float(m.group(2))
            val = abs(deg) + minutes / 60.0
            val = -val if deg < 0 else val
            coords[key] = _apply_hemisphere(val, m.group(3))
            continue

        # Degrees and minutes joined by a hyphen or degree sign: "Latitude: 49-46.9858N".
        m = re.search(rf"\b{kw}\b[:=\s]+(-?\d{{1,3}}\s*[-°º]\s*\d{{1,2}}(?:\.\d+)?\s*['′]?"
                      rf"\s*[NSEW]?)", line, re.IGNORECASE)
        if m:
            from wod_etl.staging.parse import parse_coordinate

            value = parse_coordinate(m.group(1))
            if value is not None:
                coords[key] = value
                continue

        # Plain decimal, integer allowed: "LAT: 61", "LAT = -17.3", "Latitude 61.930 N"
        m = re.search(rf"\b{kw}\b[:=\s]+({_NUM})\s*([NSEW])?\b", line, re.IGNORECASE)
        if m:
            coords[key] = _apply_hemisphere(float(m.group(1)), m.group(2))


def _parse_station_depth(line: str, sec_headers: Dict[str, Any], names) -> None:
    """A depth on the station's position line is its water depth (secondary header 10).

    e.g. `Lat: 81.529N; Lon: 30.857E; Depth: 900m`. A negative depth is a missing-value code.
    """
    if "10" in sec_headers or not (re.search(r"\blat(?:itude)?\s*[:=]", line, re.IGNORECASE)
                                   and re.search(r"\blon(?:g(?:itude)?)?\s*[:=]", line,
                                                 re.IGNORECASE)):
        return
    m = re.search(r"\bdepth\s*[:=]\s*(-?\d+(?:\.\d+)?)\s*(?:m|meters?)?\b", line, re.IGNORECASE)
    if m and float(m.group(1)) > 0:
        sec_headers["10"] = {"name": names.get(10, "code 10"), "value": float(m.group(1)),
                             "source": "the depth on the station's position line"}


def _apply_hemisphere(value: float, hemi: Optional[str]) -> float:
    if hemi and hemi.upper() in ("S", "W"):
        return -abs(value)
    return value


def _parse_datetime(line: str, coords: Dict[str, Any]) -> None:
    if "year" not in coords:
        # Some formats label the date `Time` and pad it with spaces: `Time: 2009. 8.18`.
        m = re.search(r"\b(?:date|datetime|obs[_\s]*date|time)\b[:=\s]+(\d{4})\s*[-/.]\s*"
                      r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\b", line, re.IGNORECASE)
        if not m:
            m = re.search(r"\b(?:date)\b[:=\s]+(\d{4})(\d{2})(\d{2})\b", line, re.IGNORECASE)
        if m:
            coords["year"], coords["month"], coords["day"] = (
                int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            m = re.search(r"\b(?:date)\b[:=\s]+(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", line, re.IGNORECASE)
            if m:
                coords["day"], coords["month"], coords["year"] = (
                    int(m.group(1)), int(m.group(2)), int(m.group(3)))

    if "hour" not in coords:
        m = re.search(r"\b(?:time|utc|gmt)\b[:=\s]+(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?",
                      line, re.IGNORECASE)
        if m:
            coords["hour"] = int(m.group(1))
            coords["minute"] = int(m.group(2))
            # Seconds are kept, not rounded.
            if m.group(3):
                coords["second"] = float(m.group(3))

    for key, kw in (("year", "year"), ("month", "month"), ("day", "day")):
        if key not in coords:
            m = re.search(rf"\b{kw}\b[:=\s]+(\d{{1,4}})\b", line, re.IGNORECASE)
            if m:
                coords[key] = int(m.group(1))
