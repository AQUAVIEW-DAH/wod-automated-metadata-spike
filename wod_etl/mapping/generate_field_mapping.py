"""Build the Mapping Contract: the artifact every later phase reads.

Takes the Phase 1 profile and the WOD spec and decides, per raw column, which WOD variable
it is, what unit conversion it needs, and what metadata the file header carries.
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

# Unit tokens looked for in the FULL raw column name, including any bracketed suffix.
# Order matters: the first match wins, so more specific patterns come first.
_UNIT_PATTERNS = [
    ("fahrenheit_to_celsius", r"(?:deg[\s_]*f\b|fahrenheit|\bdegf\b|°\s*f\b)"),
    ("feet_to_meters",        r"(?:\bfeet\b|\bfoot\b|\bft\b)"),
    ("fathoms_to_meters",     r"(?:\bfathom|\bfath\b|\bfm\b)"),
    ("ml_per_l_to_umol_per_kg", r"(?:ml\s*[/·.]?\s*l\b|ml\s*l-?1|milli?liter\s*per\s*liter)"),
    # mg/l is a very common oxygen unit and was previously undetected, which meant a
    # milligram-per-litre column was written unchanged into a umol/kg variable.
    ("mg_per_l_to_umol_per_kg", r"(?:\bmg\s*[/·.]?\s*l\b|\bmg\s*l-?1)"),
]

# Molar concentration units, split by SI prefix AND by basis, because the prefix is the
# whole point. A single [µumn]mol/l class conflated milli with micro, so alkalinity and DIC
# -- whose Table 3a standard unit IS mmol/l -- were detected as micromolar and divided by
# 1000. Milli and nano are matched before micro only for readability; they are disjoint.
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
    # Not 'cruise': a cruise code names a voyage, not the vessel (map_metadata routes it to
    # the character data). Kept in step with SECONDARY_HEADER_PATTERNS there.
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
# `TIME_ZONE: UTC+10` -- the zone of the DATE and TIME lines, when it is not UTC.
_TIME_ZONE_LINE = re.compile(r"\s*#?\s*time[_\s]*zone\s*[:=]\s*(.+)$", re.IGNORECASE)

# Secondary headers whose value is a measurement rather than an identifier.
_NUMERIC_HEADER_IDS = {10, 22, 23, 24}


def generate_field_mapping(
    profile_result: Dict[str, Any],
    spec_result: Dict[str, Any],
    df: Optional[Any] = None,
) -> Dict[str, Any]:
    """Produce the strict mapping contract used by Phases 3 and 4.

    `df` is optional and only used to read the VALUE of a metadata column -- a `Bottom
    Depth` column becomes secondary header 10 when it is constant down the cast. Without it
    the metadata field is still identified, just without its value.
    """
    raw_columns = profile_result.get("columns", []) or []
    raw_header_text = profile_result.get("raw_header_block", "") or ""
    variables = wod_variables()

    # Everything that is not a measurement: cast metadata (Table 4a), per-variable metadata
    # (Table 5a) and the originator's own quality flags. Run first, because it decides which
    # columns are consumed as metadata and so must not be reported as dropped.
    metadata = map_metadata(raw_header_text, raw_columns, df,
                            spec_result.get("wod_dataset_code"))
    consumed = {m["column"] for m in metadata["metadata_columns"] if m.get("column")}
    consumed |= {f["flag_column"] for f in metadata["originator_flags"]}

    column_mappings: List[Dict[str, Any]] = []
    unmapped_cols: List[str] = []
    dropped: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_codes = set()

    for col in raw_columns:
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
            # What the originator wrote, kept verbatim. Normalisation drops it in order to
            # match the name; the conversion applied below is only auditable against it, and
            # WOD has a field for it: variable specific second header 8.
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
                 if f.get("flags_column") == col), None),
        }
        fluor = fluorescence_rule(col)
        if fluor is not None and fluor.get("code") == code:
            mapping["comment"] = fluor["comment"]
            warnings.append(f"Column '{col}' is fluorometer output written as {var['name']} "
                            f"(code {code}) because its unit is a concentration. Say so "
                            f"when reporting: it is not extracted chlorophyll-a.")
        if conversion == "molar_rescale":
            # A parameterised conversion needs its endpoints; the label alone cannot say
            # whether this is umol/l -> umol/kg or mmol/kg -> mmol/l.
            source, target = molar_units(col, code)
            mapping["conversion_from"] = f"{source[0]}/{'l' if source[1] == 'volume' else 'kg'}"
            mapping["conversion_to"] = f"{target[0]}/{'l' if target[1] == 'volume' else 'kg'}"

        spec_var = next((s for s in spec_result.get("standard_variables", [])
                         if s.get("wod_code") == code), None)
        if spec_var is not None and spec_var.get("valid_for_dataset") is False:
            warnings.append(
                f"WOD code {code} ({var['name']}) is not stored in dataset "
                f"{spec_result.get('wod_dataset_code')} per Table 3a."
            )
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
    unresolved_flags = [f for f in metadata["originator_flags"] if f["scope"] != "variable"]
    if unresolved_flags:
        warnings.append(
            "Originator quality flag column(s) "
            + ", ".join(f"'{f['flag_column']}'" for f in unresolved_flags)
            + " could not be paired with a variable. They are kept and reported rather than "
              "dropped; say which variable they belong to once you know.")

    # Table 3a: 30 is Latitude, 31 is Longitude. Each requirement must be satisfied by ITS
    # OWN code -- checking membership of the pair let a latitude column stand in for a
    # missing longitude, so a file with no longitude anywhere reported ready_for_transform.
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
        # The Table 4a headers the OLD parser found, kept for compatibility with anything
        # reading this key; `secondary_headers` below is the full set, from the header block
        # AND from metadata columns, each with its published code list.
        "extracted_header_metadata": extracted["secondary_headers"],
        "secondary_headers": metadata["secondary_headers"],
        # Character data (manual p.30): the originator's cruise code and PI. Not secondary
        # headers; a cruise code written as the platform made a voyage look like a vessel.
        "character_data": metadata.get("character_data", {}),
        # Free-text `# Note:` lines and `# Source:` lines from a staged header. The staged
        # file is where every derivation is recorded, and these are what carries it into the
        # output -- before, it stopped at the staged CSV.
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
    """Coordinates, date/time, time zone, notes and sources from a `Key: value` header block.

    The same reader the profile contract uses, for a caller with a header and no table to
    map -- `map_haul` reads a staged haul's position and date this way.
    """
    return _parse_header_block(raw_header_text)


def molar_units(raw_col_name: str, wod_code: int):
    """Return (source, target) molar units for a column, or None if it is not a molar case.

    Each is a `(prefix, basis)` pair such as `("umol", "mass")`. The target comes from the
    variable's Table 3a standard unit -- never from a hardcoded assumption about which
    variables are per-mass, because WOD is not consistent: nutrients are umol/kg, alkalinity
    and DIC are mmol/l, and the noble gases are nmol/kg.
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
        # The variable's standard unit is not molar at all (chlorophyll is ug/l). Rescaling
        # a molar source onto it would be meaningless, so leave the values alone.
        return None
    return source, (m.group(1), "volume" if m.group(2) == "l" else "mass")


def _detect_unit_conversion(raw_col_name: str, wod_code: int) -> str:
    """Infer the conversion implied by the units written into a raw column name.

    Inspects the FULL name including bracketed units, which is where the unit text lives:
    'Temp (deg F)', 'Depth [ft]', 'Oxygen (ml/l)'.
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
        # Free-text notes are prose, and staged files put them above the coordinate lines.
        # Left in scope, a note reading "cast positions fall on the track" yields cast number
        # "positions fall", and a note quoting a source header supplies the wrong date. They
        # are collected instead, for the output's `comment`.
        if _NOTE_LINE.match(line):
            notes.append(_NOTE_LINE.sub("", line, count=1).strip())
            continue
        m = _SOURCE_LINE.match(line)
        if m:
            sources.append(m.group(1).strip())
            continue
        m = _TIME_ZONE_LINE.match(line)
        if m:
            coords.setdefault("time_zone", m.group(1).strip())
            continue

        _parse_position(line, coords)
        _parse_datetime(line, coords)

        for sec_id, key_pat in _HEADER_PATTERNS.items():
            if str(sec_id) in sec_headers:
                continue
            # The key must start the line or follow a delimiter or a column gap -- never sit
            # mid-sentence, where any English use of "cast" or "station" would match.
            # The value runs to the end of the line, a delimiter, or a 2+ space column gap
            # -- NOT to a fixed word count. A two-word cap silently truncated every longer
            # value ('University of Alaska Fairbanks' -> 'University of'), and a truncated
            # institution or platform is indistinguishable from what the originator wrote.
            m = re.search(rf"(?:^|[,;|]|\s{{2,}})\s*#?\s*{key_pat}\b(\s*[:=]\s*|\s+)"
                          rf"([^\s,;|][^,;|]*?)(?=\s{{2,}}|[,;|]|$)",
                          line, re.IGNORECASE)
            if not m:
                continue
            value: Any = m.group(2).strip().strip('"\'')
            if not re.search(r"[:=]", m.group(1)) and re.search(r"[:=]", value):
                continue                     # a longer key: 'Cast duration: 30' is not a cast
            # Only genuinely numeric headers are coerced. Accession, station and WMO codes
            # are identifiers: '0287423' must not become 287423.
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

        # Plain decimal, integer allowed: "LAT: 61", "LAT = -17.3", "Latitude 61.930 N"
        m = re.search(rf"\b{kw}\b[:=\s]+({_NUM})\s*([NSEW])?\b", line, re.IGNORECASE)
        if m:
            coords[key] = _apply_hemisphere(float(m.group(1)), m.group(2))


def _apply_hemisphere(value: float, hemi: Optional[str]) -> float:
    if hemi and hemi.upper() in ("S", "W"):
        return -abs(value)
    return value


def _parse_datetime(line: str, coords: Dict[str, Any]) -> None:
    if "year" not in coords:
        m = re.search(r"\b(?:date|datetime|obs[_\s]*date)\b[:=\s]+(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})",
                      line, re.IGNORECASE)
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
        m = re.search(r"\b(?:time|utc|gmt)\b[:=\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?", line, re.IGNORECASE)
        if m:
            coords["hour"] = int(m.group(1))
            coords["minute"] = int(m.group(2))

    for key, kw in (("year", "year"), ("month", "month"), ("day", "day")):
        if key not in coords:
            m = re.search(rf"\b{kw}\b[:=\s]+(\d{{1,4}})\b", line, re.IGNORECASE)
            if m:
                coords[key] = int(m.group(1))


if __name__ == "__main__":
    import json
    from wod_etl.mapping.query_wod_spec import query_wod_spec

    mock_profile = {
        "source": ".../5-XCTD/xctd_01",
        "raw_header_block": ("CRUISE: STOCS85A\nDATE: 1985-06-12\nLAT: 61.930 LON: -172.270\n"
                             "BOTTOM_DEPTH: 2500\nACCESSION: 0287423"),
        "columns": ["Depth (m)", "Temp (deg F)", "Salinity [PSU]", "Oxygen (ml/l)", "Cond"],
    }
    spec = query_wod_spec("XCTD", mock_profile["columns"], depth_increment_m=0.5)
    print(json.dumps(generate_field_mapping(mock_profile, spec), indent=2))
