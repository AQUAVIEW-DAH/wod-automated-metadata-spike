"""Map a file's non-measurement content to WOD fields.

Cast metadata (Table 4a), per-variable metadata (Table 5a), character data and originator
flags, read from header lines, constant columns and measurement column names.
"""
import re
from typing import Any, Dict, List, Optional

from wod_etl.mapping.query_wod_spec import (
    extract_scale,
    extract_unit,
    match_variable,
    normalize_name,
)
from wod_etl.reference import (code_table_link, variable_specific_headers,
                               wod_secondary_headers, wod_variables)

_NUM = r"[-+]?\d+(?:\.\d+)?"

# ------------------------------------------------------------- Table 4a
# Cast metadata: secondary header ID -> key pattern. The first match claims a line, so
# specific keys come first ('sea surface temperature' before 'temperature').
SECONDARY_HEADER_PATTERNS: Dict[int, str] = {
    1:  r"(?:accession(?:[_\s]*(?:no|num|number))?|nodc[_\s]*accession|ncei[_\s]*accession)",
    2:  r"(?:project|programme|program|expedition)",
    # Not 'cruise': that names a voyage, not a vessel (character data).
    3:  r"(?:platform(?:[_\s]*name)?|ship(?:[_\s]*name)?|vessel(?:[_\s]*name)?|callsign|"
        r"call[_\s]*sign)",
    4:  r"(?:institut(?:e|ion)|agency|laborator(?:y|ies)|originator[_\s]*institution|data[_\s]*cent(?:er|re))",
    5:  r"(?:cast(?:[_\s]*(?:no|num|number|id))?|tow[_\s]*(?:no|num|number)|profile[_\s]*(?:no|number|id)|haul[_\s]*(?:no|number))",
    7:  r"(?:station(?:[_\s]*(?:no|num|number|id))?|stn(?:[_\s]*(?:no|id))?|originator'?s?[_\s]*station)",
    8:  r"(?:depth[_\s]*precision)",
    9:  r"(?:ocean[_\s]*weather[_\s]*station|owsapiens|ows\b)",
    10: r"(?:bottom[_\s]*depth|water[_\s]*depth|sounding|depth[_\s]*to[_\s]*bottom|"
        r"seafloor[_\s]*depth|max(?:imum)?[_\s]*depth[_\s]*of[_\s]*station|bot[_\s]*depth)",
    11: r"(?:cast[_\s]*duration)",
    12: r"(?:cast[_\s]*direction|updown|profile[_\s]*direction|down[_\s]*up)",
    14: r"(?:water[_\s]*colou?r|forel(?:[- _]ule)?)",
    15: r"(?:secchi(?:[_\s]*(?:depth|disk))?|water[_\s]*transparency)",
    16: r"(?:wave[_\s]*direction|swell[_\s]*direction)",
    17: r"(?:wave[_\s]*height|swell[_\s]*height|sig(?:nificant)?[_\s]*wave)",
    18: r"(?:sea[_\s]*state)",
    19: r"(?:wind[_\s]*force|beaufort)",
    20: r"(?:wave[_\s]*period|swell[_\s]*period)",
    21: r"(?:wind[_\s]*(?:direction|dir|from))",
    22: r"(?:wind[_\s]*(?:speed|spd|velocity))",
    23: r"(?:barometric[_\s]*pressure|air[_\s]*pressure|atmospheric[_\s]*pressure|baro|slp|mslp)",
    24: r"(?:dry[_\s]*bulb(?:[_\s]*temp(?:erature)?)?|air[_\s]*temp(?:erature)?)",
    25: r"(?:wet[_\s]*bulb(?:[_\s]*temp(?:erature)?)?)",
    26: r"(?:weather(?:[_\s]*conditions?)?|present[_\s]*weather)",
    27: r"(?:cloud[_\s]*type)",
    28: r"(?:cloud[_\s]*(?:cover|amount)|total[_\s]*cloud|octas?)",
    29: r"(?:probe[_\s]*type|instrument[_\s]*type|xbt[_\s]*type|probe\b)",
    30: r"(?:calibration[_\s]*depth)",
    31: r"(?:calibration[_\s]*temp(?:erature)?)",
    32: r"(?:recorder(?:[_\s]*type)?)",
    33: r"(?:depth[_\s]*correction|fall[_\s]*rate[_\s]*(?:correction|equation)|hanawa)",
    34: r"(?:bottom[_\s]*hit)",
    35: r"(?:digiti[sz]ation[_\s]*method)",
    36: r"(?:digiti[sz]ation[_\s]*interval)",
    37: r"(?:data[_\s]*(?:treatment|storage)(?:[_\s]*method)?)",
    38: r"(?:trace[_\s]*correction)",
    39: r"(?:temp(?:erature)?[_\s]*correction)",
    40: r"(?:reference[_\s]*temp(?:erature)?[_\s]*instrument|instrument[_\s]*for[_\s]*reference)",
    41: r"(?:horizontal[_\s]*visibility|visibility)",
    45: r"(?:absolute[_\s]*humidity|humidity)",
    46: r"(?:sea[_\s]*surface[_\s]*temp(?:erature)?|reference[_\s]*temp(?:erature)?|\bsst\b)",
    47: r"(?:sea[_\s]*surface[_\s]*salinity|\bsss\b)",
    48: r"(?:probe[_\s]*(?:manufacture[_\s]*)?year|year[_\s]*(?:of[_\s]*)?manufacture)",
    49: r"(?:ship[_\s]*speed|vessel[_\s]*speed|speed[_\s]*of[_\s]*ship)",
    54: r"(?:depth[_\s]*fix|needs[_\s]*depth[_\s]*fix)",
    71: r"(?:real[_\s]*time)",
    74: r"(?:ocean(?:ographic)?[_\s]*(?:measuring[_\s]*)?vehicle)",
    77: r"(?:xco2|mole[_\s]*fraction[_\s]*(?:of[_\s]*)?co2|headspace)",
    81: r"(?:pco2[_\s]*calc(?:ulation)?[_\s]*method)",
    82: r"(?:equilibrator[_\s]*type)",
    84: r"(?:argos[_\s]*fix)",
    87: r"(?:launch[_\s]*height|height[_\s]*of[_\s]*(?:xbt[_\s]*)?launch|deck[_\s]*height)",
    88: r"(?:sensor[_\s]*depth|depth[_\s]*of[_\s]*(?:sea[_\s]*surface[_\s]*)?sensor|intake[_\s]*depth)",
    91: r"(?:database[_\s]*id)",
    92: r"(?:ukho(?:[_\s]*(?:bib|reference|ref))?)",
    93: r"(?:tow[_\s]*segment|consecutive[_\s]*profile)",
    94: r"(?:wmo(?:[_\s]*(?:id|no|number|code|platform))?)",
    95: r"(?:originator'?s?[_\s]*depth[_\s]*unit|depth[_\s]*unit)",
    96: r"(?:originator'?s?[_\s]*flag(?:s|[_\s]*set)?|flag[_\s]*(?:scheme|set|convention)|"
        r"quality[_\s]*(?:flag[_\s]*)?(?:scheme|convention|system))",
    97: r"(?:water[_\s]*sampler|sampler(?:[_\s]*type)?|niskin|rosette|bottle[_\s]*type)",
    98: r"(?:argos[_\s]*id|platform[_\s]*transmitter|ptt\b)",
}

# ------------------------------------------------- character data (p.30)
# Originator identifiers that are not secondary headers (manual p.30).
CHARACTER_DATA_PATTERNS: Dict[str, str] = {
    "originator_cruise_id": r"(?:originator'?s?[_\s]*)?cruise(?:[_\s]*(?:id|code|name|no|"
                            r"num|number))?",
    # ACDD's creator_name is the PI in an originator's NetCDF.
    "principal_investigator": r"(?:principal[_\s]*investigator|pi(?:[_\s]*name)?|"
                              r"chief[_\s]*scientist|creator[_\s]*name)",
}

# ------------------------------------------------- primary header (Table 10a)
# The cast's country code (Appendix 1), by rank: stated country, country of origin, then a
# creator's or institution's country (for platforms that are not ships, p.17).
COUNTRY_PATTERNS = [
    r"(?:country(?:[_\s]*(?:code|name))?|iso[_\s]*3166(?:[_\s]*\w+)*|nation(?:ality)?|"
    r"ship[_\s]*country|flag[_\s]*state)",
    r"(?:country[_\s]*of[_\s]*origin)",
    r"(?:(?:creator|institution|publisher)[_\s]*country)",
]

# A platform name outranks a type ('research vessel'); a cruise id outranks a cruise name.
_PLATFORM_TYPE = re.compile(
    r"^(?:research|oceanographic|fishing|survey|naval|coast\s*guard)?\s*(?:vessel|ship|boat|"
    r"buoy|mooring|glider|float|aircraft|auv|uav|ice\s*camp|platform)s?$|"
    r"ships?\s+of\s+opportunity", re.IGNORECASE)
_CRUISE_ID = re.compile(r"(?:id|code|no|num|number)$", re.IGNORECASE)

# Keys generate_field_mapping parses (coordinates, time, sources).
_NOT_METADATA_KEY = re.compile(
    r"^(?:lat|latitude|lon|long|longitude|date|time|time[_\s]*zone|tz|datetime|year|month|"
    r"day|hour|minute|source(?:[_\s]*file)?s?|history)$", re.IGNORECASE)

# Sea-Bird .cnv layout and processing keys: not originator metadata.
_INSTRUMENT_FILE_KEY = re.compile(
    r"^(?:nquan|nvalues|units|name\s*\d+|span\s*\d+|interval|start_time|bad_flag|file_type|"
    r"sensor\s*\d+|filename|software\s+version|system\s+upload\s+time|"
    r"(?:datcnv|filter|alignctd|celltm|loopedit|derive|binavg|wildedit|sectionx|strip|"
    r"split|bottlesum|w_filter|seasave|translate)_\w+)$", re.IGNORECASE)

# Secondary headers whose value is a number; identifiers stay strings ('0287423').
NUMERIC_SECONDARY_HEADERS = {8, 10, 11, 15, 17, 20, 22, 23, 24, 25, 28, 30, 31, 36, 41, 45,
                             46, 47, 48, 49, 87, 88}

# ------------------------------------------------------------- Table 5a
# Per-variable metadata, attached to one measurement rather than the cast.
VARIABLE_HEADER_PATTERNS: Dict[int, str] = {
    3:  r"(?:scale)",
    5:  r"(?:instrument|sensor(?:[_\s]*(?:type|model))?|analy[sz]er)",
    6:  r"(?:method|protocol|technique|analysis[_\s]*method)",
    8:  r"(?:originator'?s?[_\s]*units?|original[_\s]*units?|reported[_\s]*units?)",
    10: r"(?:equilibrator[_\s]*type)",
    11: r"(?:filter[_\s]*(?:type|size))",
    12: r"(?:incubation[_\s]*time)",
    16: r"(?:uncalibrated)",
    17: r"(?:contains[_\s]*nitrite|nitrate[_\s]*(?:\+|and|plus)[_\s]*nitrite)",
    18: r"(?:standard[_\s]*seawater[_\s]*batch|iapso[_\s]*batch|\bp\d{2,3}\b)",
    19: r"(?:adjust(?:ment|ed)?)",
    22: r"(?:reported[_\s]*uncertainty|uncertainty)",
    24: r"(?:\bmode\b|data[_\s]*mode|delayed[_\s]*mode)",
}

# A column that flags another column; group 'var' is the flagged variable.
_FLAG_SUFFIX = re.compile(
    r"^(?P<var>.+?)[\s_\-]*(?:qc|q_?c|qf|quality(?:[\s_]*(?:flag|control))?|flags?|"
    r"qualit[ye]|qual)$", re.IGNORECASE)
_FLAG_PREFIX = re.compile(
    r"^(?:qc|qf|q|flag|quality(?:[\s_]*flag)?)[\s_\-]+(?P<var>.+)$", re.IGNORECASE)

# Flag columns that name no variable: they flag the whole level or cast.
_BARE_FLAG = re.compile(r"^(?:qc|qf|quality(?:[\s_]*(?:flag|control))?|flags?|"
                        r"qc[\s_]*flag|data[\s_]*quality)$", re.IGNORECASE)

# Prose lines in a staged header; no keys are read from them.
_NOTE_LINE = re.compile(r"\s*#?\s*(?:note|comment|remark)s?\s*[:=]", re.IGNORECASE)

# Coordinate and time columns, never metadata values.
_SKIP_AS_METADATA = re.compile(
    r"^(?:date|time|datetime|year|month|day|hour|minute|second|lat|latitude|lon|long|"
    r"longitude|depth|pressure|z)\b", re.IGNORECASE)


def map_metadata(
    header_text: str = "",
    columns: Optional[List[str]] = None,
    df: Optional[Any] = None,
    dataset_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Every non-measurement fact in a file, placed in the WOD field that holds it.

    `df` supplies metadata column values. What could not be placed is in `unresolved`.
    """
    columns = [str(c) for c in (columns or [])]
    out: Dict[str, Any] = {
        "secondary_headers": {},
        "variable_specific_headers": {},
        "metadata_columns": [],
        "originator_flags": [],
        "character_data": {},
        "primary_header": {},
        "cast_level_variable_headers": {},
        "unresolved": [],
        "dataset_code": dataset_code,
    }

    _from_header_block(header_text or "", out)
    _from_columns(columns, df, out)
    _from_column_names(columns, out)
    _probe_model(out)
    _apply_cast_level_variable_headers(columns, out)
    _find_originator_flags(columns, out)
    _attach_code_tables(out)
    _resolve_codes(out)
    return out


# ------------------------------------------------------------ header block
# Delimited metadata lines, as in WOD's CSV export ('Bottom depth ,,36,meters,,'): column 0
# is the key, 1 the variable (empty for Table 4a cast metadata), 2 the value, then a unit or
# code system and a description.
_DELIMITED_HEADER = re.compile(r"^\s*([^,\t]{2,48}?)\s*[,\t]\s*([^,\t]{0,48}?)\s*[,\t](.*)$")

# A code system after the value ('OCL code') means the value is already a code.
_CODE_SYSTEM = re.compile(r"\b(NODC|OCL|WOD|WMO|ISO|NCEI)\s*code\b", re.IGNORECASE)

# Coordinate and time keys, parsed elsewhere.
_COORDINATE_KEY = re.compile(
    r"^(lat|latitude|lon|long|longitude|year|month|day|hour|minute|second|date|time|"
    r"datetime|julian|yearday|depth|pressure)\b", re.IGNORECASE)

# Structural marker lines, not key/value pairs.
_BLOCK_MARKER = re.compile(
    r"^\s*(#|-{3,}|=|CAST\b|METADATA\b|VARIABLES\b|UNITS\b|END OF\b|Prof-Flag\b)",
    re.IGNORECASE)


def _from_header_block(header_text: str, out: Dict[str, Any]) -> None:
    names = wod_secondary_headers()
    for line in header_text.splitlines():
        if _NOTE_LINE.match(line):
            continue

        pair = _delimited_pair(line)
        if pair is not None:
            _place_pair(pair, line, names, out)
            continue

        placed_keys, spans = set(), []
        for sec_id, key_pat in SECONDARY_HEADER_PATTERNS.items():
            # The key starts the line or follows a delimiter or column gap, never mid-sentence.
            # The value runs to the end of the line, a delimiter, or a 2+ space gap.
            m = re.search(rf"(?:^|[,;|]|\s{{2,}})\s*#?\s*({key_pat})\b(\s*[:=]\s*|\s+)"
                          rf"([^\s,;|][^,;|]*?)(?=\s{{2,}}|[,;|]|$)", line, re.IGNORECASE)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(3).strip().strip("\"'")
            # A space-joined value holding ':' or '=' means a longer key ('Weather observer:').
            if not re.search(r"[:=]", m.group(2)) and re.search(r"[:=]", value):
                continue
            if _NOT_METADATA_KEY.match(key) or _character_data_key(key):
                continue
            # One key, one header: 'Cast duration' must not also match 'cast'.
            if any(a <= m.start(1) < b or m.start(1) <= a < m.end(1) for a, b in spans):
                continue
            spans.append((m.start(1), m.end(3)))
            placed_keys.add(_norm_key(key))
            held = out["secondary_headers"].get(str(sec_id))
            if held is not None and sec_id == 3 \
                    and _PLATFORM_TYPE.search(str(held.get("value", "")).strip()) \
                    and not _PLATFORM_TYPE.search(value):
                out["unresolved"].append({
                    "key": held["originator_key"], "value": held["value"], "variable": None,
                    "evidence": held.get("evidence"), "matches": "secondary header 3",
                    "note": f"a platform type; the platform is {key} {value!r}"})
                held = None
            if held is not None:
                if held.get("value") != _coerce(value, sec_id):
                    out["unresolved"].append({
                        "key": key, "value": value, "variable": None,
                        "evidence": line.strip()[:120],
                        "matches": f"secondary header {sec_id}",
                        "note": f"header {sec_id} is already held by "
                                f"{held.get('evidence', held.get('source'))!r}. Both are in "
                                f"the file; decide which WOD wants."})
                continue
            out["secondary_headers"][str(sec_id)] = {
                "name": names.get(sec_id, f"code {sec_id}"),
                "value": _coerce(value, sec_id),
                "source": "header block",
                "originator_key": key,
                "evidence": line.strip()[:120],
            }
        _from_key_value_line(line, placed_keys, out)


# `Key: value` pairs (':' or '='), several per line. A value ends at the next key, `;`, `|`
# or line end; a comma ends it only before another key (`LAT: 61.9, LON: -172.2`).
_KEY_VALUE = re.compile(
    r"(?:^|[,;|]|\s{2,})\s*#?\s*([A-Za-z][A-Za-z0-9 _.'/()&-]{0,47}?)\s*[:=]\s*"
    r"(.*?)\s*(?=(?:\s{2,}|[,;|]\s*)[A-Za-z][A-Za-z0-9 _.'/()&-]{0,47}?\s*[:=]|[;|]|$)")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _place_country(key: str, value: str, evidence: str, out: Dict[str, Any]) -> bool:
    """Hold a country stated under `key`, the highest-ranked key winning. True if `key` is
    a country key."""
    k = key.strip().replace("_", " ")
    rank = next((i for i, pat in enumerate(COUNTRY_PATTERNS)
                 if re.fullmatch(pat, k, re.IGNORECASE)), None)
    if rank is None:
        return False
    held = out["primary_header"].get("country")
    if held is None or rank < held["rank"]:
        out["primary_header"]["country"] = {"value": value, "originator_key": key,
                                            "rank": rank, "source": "header block",
                                            "evidence": evidence}
    return True


def _character_data_key(key: str) -> Optional[str]:
    for field, pattern in CHARACTER_DATA_PATTERNS.items():
        if re.fullmatch(pattern, key.strip().replace("_", " "), re.IGNORECASE):
            return field
    return None


def _from_key_value_line(line: str, placed_keys, out: Dict[str, Any]) -> None:
    """Place the `Key: value` pairs the secondary-header pass left: as the country, character
    data, a cast-level variable header, or `unresolved`."""
    # On a station position line, `Depth:` is the bottom depth (generate_field_mapping).
    position_line = bool(re.search(r"\blat(?:itude)?\s*[:=]", line, re.IGNORECASE)
                         and re.search(r"\blon(?:g(?:itude)?)?\s*[:=]", line, re.IGNORECASE))
    for m in _KEY_VALUE.finditer(line):
        key, value = m.group(1).strip(), m.group(2).strip().strip("\"'")
        if (not value or _NOT_METADATA_KEY.match(key) or _INSTRUMENT_FILE_KEY.match(key)
                or _norm_key(key) in placed_keys
                or (position_line and key.lower() == "depth")):
            continue
        evidence = line.strip()[:120]
        if _place_country(key, value, evidence, out):
            continue

        field = _character_data_key(key)
        held = out["character_data"].get(field) if field else None
        if field == "originator_cruise_id" and held and _CRUISE_ID.search(key) \
                and not _CRUISE_ID.search(held["originator_key"]):
            out["unresolved"].append({
                "key": held["originator_key"], "value": held["value"], "variable": None,
                "evidence": held.get("evidence"), "matches": "character data",
                "note": f"a cruise name; the cruise id is {key} {value!r}"})
            del out["character_data"][field]
        if field:
            if field in out["character_data"]:
                out["unresolved"].append({
                    "key": key, "value": value, "variable": None, "evidence": evidence,
                    "matches": f"character data {field}",
                    "note": f"{field} is already {out['character_data'][field]['value']!r}."})
            else:
                out["character_data"][field] = {"value": value, "originator_key": key,
                                                "source": "header block",
                                                "evidence": evidence}
            continue

        var_id = next((vid for vid, pat in VARIABLE_HEADER_PATTERNS.items()
                       if _key_matches(key, pat)), None)
        if var_id is not None:
            # Stated once for the cast, so it applies to every measured variable.
            out["cast_level_variable_headers"].setdefault(str(var_id), {
                "name": (variable_specific_headers().get(var_id) or {}).get("name", key),
                "value": value, "originator_key": key, "evidence": evidence,
                "source": "header block, stated for the whole cast"})
            continue

        if any(re.fullmatch(pat, key.replace("_", " "), re.IGNORECASE)
               for pat in SECONDARY_HEADER_PATTERNS.values()):
            continue                     # a known key the pattern pass already handled
        out["unresolved"].append({
            "key": key, "value": value, "variable": None, "evidence": evidence,
            "matches": None,
            "note": "no WOD field matched this key: WOD may not store it, or map_metadata "
                    "lacks a pattern for it."})


def _probe_model(out: Dict[str, Any]) -> None:
    """A probe type stated as a model ('T-7') is two WOD fields: its category in secondary
    header 29 (XBT) and the instrument in variable header 5 (XBT: T7)."""
    from wod_etl.reference.code_lists import lookup_code

    held = out["secondary_headers"].get("29")
    text = str((held or {}).get("value") or "").strip()
    if not text or re.fullmatch(r"\d+(?:\.0*)?", text) or \
            lookup_code("secondary_header", 29, text)["status"] in ("matched", "already_a_code"):
        return
    model = lookup_code("variable_specific_header", 5, text)
    if model.get("status") != "matched" or ":" not in model["label"]:
        return
    category = lookup_code("secondary_header", 29, model["label"].split(":", 1)[0])
    if category.get("status") != "matched":
        return
    held.update(value=category["code"], originator_value=text)
    out["cast_level_variable_headers"].setdefault("5", {
        "name": "Instrument", "value": model["code"], "originator_key": held.get("originator_key"),
        "source": f"probe type {text!r}: {model['label']}"})


# Depth units as the depth-unit list (s_95) spells them.
_DEPTH_UNITS = {"ft": "foot", "feet": "foot", "foot": "foot", "fm": "fathom",
                "fms": "fathom", "fathoms": "fathom", "fathom": "fathom"}

# XBT depth-time equation, secondary header 33: 0 original (Sippican), 1 corrected (Hanawa et
# al. 1995 or later), manual p.61. Tried in order, so 'no correction' is 0, not 1.
_FALL_RATE = [(re.compile(r"hanawa|kizu|cheng", re.I), 1),
              (re.compile(r"sippican|original|manufacturer|\bold\b|uncorrected|\bno\b|"
                          r"\bnot\b|\bnone\b", re.I), 0),
              (re.compile(r"correct|amend|revis|\bnew\b", re.I), 1)]


def _apply_cast_level_variable_headers(columns: List[str], out: Dict[str, Any]) -> None:
    """Give a cast-level Table 5a value to every measured variable that lacks its own."""
    cast_level = out.get("cast_level_variable_headers") or {}
    if not cast_level:
        return
    variables = wod_variables()
    for col in columns:
        code = match_variable(col)
        if code is None or code == 0 or code in (30, 31, 32):
            continue
        entry = out["variable_specific_headers"].setdefault(col, {
            "raw_column": col, "wod_code": code,
            "wod_name": variables.get(code, {}).get("name"), "headers": {}})
        for vid, header in cast_level.items():
            entry["headers"].setdefault(vid, {**header,
                                              "note": "stated once for the cast; applied to "
                                                      "every measured variable"})


def _delimited_pair(line: str):
    """A `key,variable,value,...` line as a dict, or None if the line is not one.

    Needs at least three columns: a bare `key,value` could be a table's column header row.
    """
    if _BLOCK_MARKER.match(line):
        return None
    # `key: a, b, c` is one key and value, commas and all.
    if re.match(r"^\s*#?\s*[A-Za-z][\w .()/-]{0,47}?\s*[:=]\s*\S", line) and \
            line.find(":") < line.find(","):
        return None
    m = _DELIMITED_HEADER.match(line)
    if not m:
        return None
    key = m.group(1).strip().strip("#*").strip()
    if not key or not re.search(r"[A-Za-z]", key) or _COORDINATE_KEY.match(key):
        return None

    rest = [f.strip() for f in m.group(3).split(",")]
    # The value is column 2 even when empty; scanning forward would pick up the code system.
    value = rest[0] if rest else ""
    if not value:
        return {"key": key, "variable": m.group(2).strip() or None, "value": None,
                "unit": None, "code_system": None, "description": None, "blank": True}

    # After the value: a unit or code system, then a description.
    tail = [f for f in rest[1:] if f]
    code_system = unit = description = None
    if tail:
        if _CODE_SYSTEM.search(tail[0]):
            code_system = tail[0]
        elif len(tail[0]) <= 24:
            unit = tail[0]
        else:
            description = tail[0]
    if len(tail) > 1 and description is None:
        description = tail[1]
    return {"key": key, "variable": m.group(2).strip() or None, "value": value,
            "unit": unit, "code_system": code_system, "description": description,
            "blank": False}


def _key_matches(key: str, pattern: str) -> bool:
    """Whether an isolated metadata key names this header (a search, not a full match)."""
    return bool(re.search(rf"\b{pattern}\b", key.replace("_", " "), re.IGNORECASE))


def _place_pair(pair, line, names, out) -> None:
    """File one delimited pair into the WOD field that holds it, or record it as unplaced."""
    key, value, variable = pair["key"], pair["value"], pair["variable"]
    evidence = line.strip()[:120]

    if pair["blank"]:
        out["unresolved"].append({
            "key": key, "value": None, "variable": variable, "evidence": evidence,
            "matches": None,
            "note": "the key is present in the block but its value column is empty, so "
                    "nothing was recorded for it."})
        return

    # Column 1 names the variable, so a per-variable key attaches to it.
    if variable:
        for var_id, key_pat in VARIABLE_HEADER_PATTERNS.items():
            if not _key_matches(key, key_pat):
                continue
            entry = out["variable_specific_headers"].setdefault(
                variable, {"raw_column": variable, "wod_code": match_variable(variable),
                           "wod_name": variable, "headers": {}})
            entry["headers"][str(var_id)] = {
                "name": (variable_specific_headers().get(var_id) or {}).get("name", key),
                "value": value, "source": f"header block, column 1 names '{variable}'",
                "originator_key": key,
                **({"code_system": pair["code_system"],
                    "value_is_already_a_code": True} if pair["code_system"] else {}),
                **({"description": pair["description"]} if pair["description"] else {}),
            }
            return

    if not variable and _place_country(key, value, evidence, out):
        return

    field = _character_data_key(key)
    if field and not variable:
        out["character_data"].setdefault(field, {"value": value, "originator_key": key,
                                                 "source": "header block",
                                                 "evidence": evidence})
        return

    for sec_id, key_pat in SECONDARY_HEADER_PATTERNS.items():
        if not _key_matches(key, key_pat):
            continue
        entry = {
            "name": names.get(sec_id, f"code {sec_id}"),
            "value": _coerce(value, sec_id),
            "source": "header block",
            "originator_key": key,
            "evidence": evidence,
        }
        if pair["unit"]:
            entry["unit"] = pair["unit"]
        if pair["description"]:
            entry["description"] = pair["description"]
        if pair["code_system"]:
            # The value is already a published code; no lookup needed.
            entry["code_system"] = pair["code_system"]
            entry["value_is_already_a_code"] = True

        held = out["secondary_headers"].get(str(sec_id))
        if held is None:
            out["secondary_headers"][str(sec_id)] = entry
            return
        # Two keys claim one header: a published code wins, else the first seen.
        winner, loser = ((entry, held) if entry.get("value_is_already_a_code")
                         and not held.get("value_is_already_a_code") else (held, entry))
        out["secondary_headers"][str(sec_id)] = winner
        out["unresolved"].append({
            "key": loser.get("originator_key", key), "value": loser.get("value"),
            "variable": None, "evidence": evidence,
            "matches": f"secondary header {sec_id}",
            "note": f"also matched header {sec_id}, which is held by "
                    f"'{winner.get('originator_key')}' = {winner.get('value')!r}"
                    + (" because that value is a published code" if
                       winner.get("value_is_already_a_code") else " because it was seen first")
                    + ". Both are in the file; decide which WOD wants."})
        return

    for var_id, key_pat in VARIABLE_HEADER_PATTERNS.items():
        if _key_matches(key, key_pat):
            out["unresolved"].append({
                "key": key, "value": value, "variable": variable, "evidence": evidence,
                "matches": f"variable specific second header {var_id}",
                "note": "recognised, but nothing says which variable it applies to. Attach "
                        "it to a variable during staging, or it is left out."})
            return

    out["unresolved"].append({
        "key": key, "value": value, "variable": variable, "evidence": evidence,
        "matches": None,
        "note": "no WOD field matched this key: WOD may not store it, or "
                "SECONDARY_HEADER_PATTERNS lacks it."})


def _coerce(value: str, sec_id: int):
    if sec_id in NUMERIC_SECONDARY_HEADERS and re.fullmatch(_NUM, value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------- columns
def _from_columns(columns: List[str], df, out: Dict[str, Any]) -> None:
    """Map columns that are secondary headers (`Bottom Depth (m)` is header 10).

    A value is recorded only when the column is constant down the cast.
    """
    names = wod_secondary_headers()
    for col in columns:
        if match_variable(col) is not None or _SKIP_AS_METADATA.match(str(col).strip()):
            continue
        norm = normalize_name(col)
        if not norm:
            continue
        for sec_id, key_pat in SECONDARY_HEADER_PATTERNS.items():
            if not re.search(rf"(?:^|_){key_pat}(?:_|$)", norm, re.IGNORECASE):
                continue
            entry = {
                "name": names.get(sec_id, f"code {sec_id}"),
                "source": f"column '{col}'",
                "column": col,
                "unit": extract_unit(col),
            }
            value, constant = _column_value(df, col)
            entry["value"] = value
            if not constant:
                entry["value"] = None
                entry["note"] = (
                    f"'{col}' varies down the cast, so no single value is recorded: a "
                    f"secondary header describes the whole cast. If it varies between casts, "
                    f"split the file per cast in staging and keep the column.")
            # A header-block value wins over one inferred from a column.
            existing = out["secondary_headers"].get(str(sec_id))
            if existing and existing.get("value") not in (None, ""):
                entry["superseded_by"] = existing["source"]
                out["metadata_columns"].append({**entry, "secondary_header": sec_id})
            else:
                out["secondary_headers"][str(sec_id)] = entry
                out["metadata_columns"].append({**entry, "secondary_header": sec_id})
            break


def _column_value(df, col):
    """(value, is_constant) for a metadata column, or (None, False) with no data."""
    if df is None or col not in getattr(df, "columns", []):
        return None, False
    try:
        import pandas as pd

        series = df[col].dropna()
        if series.empty:
            return None, False
        uniques = series.unique()
        if len(uniques) != 1:
            return None, False
        value = uniques[0]
        numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        return (float(numeric) if pd.notna(numeric) else str(value)), True
    except Exception:                                # noqa: BLE001 - metadata is best-effort
        return None, False


# ------------------------------------------------------ measurement names
def _from_column_names(columns: List[str], out: Dict[str, Any]) -> None:
    """Recover per-variable metadata from measurement column names.

    `Temperature [ITS-90, deg C]` gives a scale (header 3) and the originator's unit (header 8).
    """
    variables = wod_variables()
    for col in columns:
        code = match_variable(col)
        if code is None:
            continue
        headers: Dict[str, Any] = {}
        unit = extract_unit(col)
        if unit:
            headers["8"] = {"name": "Originator's units", "value": unit,
                            "source": f"column name '{col}'"}
        scale = extract_scale(col)
        if scale:
            headers["3"] = {"name": "Scale", "value": scale,
                            "source": f"column name '{col}'"}
        # Table 5a header 17: a nitrate column that is really nitrate + nitrite.
        if code == 8 and re.search(r"nitrite", str(col), re.IGNORECASE):
            headers["17"] = {"name": "Contains nitrite", "value": 1,
                             "source": f"column name '{col}'",
                             "note": "value is nitrate + nitrite, per Table 5b code 17"}
        if not headers:
            continue
        out["variable_specific_headers"][col] = {
            "raw_column": col,
            "wod_code": code,
            "wod_name": "Depth" if code == 0 else variables.get(code, {}).get("name"),
            "headers": headers,
        }


# ------------------------------------------------------- originator flags
def _find_originator_flags(columns: List[str], out: Dict[str, Any]) -> None:
    """Pair each originator quality-flag column with the variable it flags.

    Flags are kept verbatim, not translated to Table 12; secondary header 96 names the set.
    """
    by_norm = {normalize_name(c): c for c in columns}
    # Also pair on the resolved WOD code, since flag and measurement names often differ.
    by_code = {}
    for c in columns:
        code = match_variable(c)
        if code is not None:
            by_code.setdefault(code, c)

    for col in columns:
        text = str(col).strip()
        if _BARE_FLAG.match(text):
            out["originator_flags"].append({
                "flag_column": col, "flags_column": None, "flags_wod_code": None,
                "scope": "level or cast",
                "note": "names no variable, so it flags the whole level or cast; see the "
                        "file's documentation for which."})
            continue
        m = _FLAG_SUFFIX.match(text) or _FLAG_PREFIX.match(text)
        if not m:
            continue
        target_raw = m.group("var").strip(" _-:")
        target = by_norm.get(normalize_name(target_raw))
        code = match_variable(target or target_raw)
        if target is None and code is not None:
            target = by_code.get(code)
        out["originator_flags"].append({
            "flag_column": col,
            "flags_column": target,
            "flags_wod_code": code,
            "scope": "variable" if target else "unresolved",
            "note": None if target else
                    f"names '{target_raw}', which is not a column in this file. The flag "
                    f"is kept.",
        })
    if out["originator_flags"] and "96" not in out["secondary_headers"]:
        out["secondary_headers"]["96"] = {
            "name": "Originator's flags",
            "value": None,
            "source": "inferred from flag columns",
            "note": "This file carries the originator's own quality flags. WOD records "
                    "which flag set was used as secondary header 96; the file does not state "
                    "it, so it must come from the submitter or the accession documentation. "
                    "The flag values are kept verbatim and are not WOD Table 12 flags.",
        }


# ------------------------------------------------------------ code tables
def _attach_code_tables(out: Dict[str, Any]) -> None:
    """Add the published code list link to every coded field."""
    for sid, entry in out["secondary_headers"].items():
        link = code_table_link("secondary_header", int(sid))
        if link:
            entry["code_table"] = {k: v for k, v in link.items() if k in ("html", "csv", "pdf")}
            entry["is_coded"] = True
        else:
            entry["is_coded"] = False

    known = variable_specific_headers()
    for entry in out["variable_specific_headers"].values():
        for vid, header in entry["headers"].items():
            link = code_table_link("variable_specific_header", int(vid))
            header["appendix"] = (known.get(int(vid)) or {}).get("appendix")
            if link:
                header["code_table"] = {k: v for k, v in link.items()
                                        if k in ("html", "csv", "pdf")}


def _resolve_codes(out: Dict[str, Any]) -> None:
    """Look up every coded value in its published list.

    A value becomes a code only if it is a number or names exactly one entry; otherwise the
    originator's text is kept and `code_lookup` says why.
    """
    from wod_etl.reference import resolve_country
    from wod_etl.reference.code_lists import (describe_lookup, lookup_code,
                                              lookup_investigator, lookup_unit, unit_key)

    fall_rate = out["secondary_headers"].get("33")
    if fall_rate and not re.fullmatch(r"-?\d+(?:\.0*)?", str(fall_rate.get("value")).strip()):
        code = next((c for pat, c in _FALL_RATE if pat.search(str(fall_rate["value"]))), None)
        if code is not None:
            fall_rate.update(originator_value=fall_rate["value"], value=code)
    country = out["primary_header"].get("country")
    if country:
        r = resolve_country(country["value"])
        country["code_lookup"] = {"status": r["status"], "field": "country_list",
                                  "summary": (f"{r['code']} {r['label']} (Appendix 1)"
                                              if r["code"] else
                                              f"no WOD country code for {r['value']!r} "
                                              f"(Appendix 1)")}
        if r["code"]:
            country.update(originator_value=country["value"], value=r["code"],
                           label=r["label"])
        else:
            out["unresolved"].append({
                "key": country["originator_key"], "value": country["value"], "variable": None,
                "evidence": country.get("evidence"), "matches": "primary header country",
                "note": "no Appendix 1 code for it: set country in decisions.toml [metadata]"})
            del out["primary_header"]["country"]
    pi = out["character_data"].get("principal_investigator")
    if pi and pi.get("value"):
        r = lookup_investigator(pi["value"])
        pi["code_lookup"] = {"status": r["status"], "summary": describe_lookup(r)}
        if r["status"] == "matched":
            out["character_data"]["principal_investigator_code"] = {
                "value": r["code"], "source": f"primary_investigator_list: {r['label']}"}

    for sid, entry in out["secondary_headers"].items():
        if not entry.get("is_coded") or entry.get("value") in (None, ""):
            continue
        result = lookup_code("secondary_header", int(sid), entry["value"])
        _apply_lookup(entry, result)
    for entry in out["variable_specific_headers"].values():
        unit = entry["headers"].get("8")
        if entry.get("wod_code") == 0 and unit:
            # The depth axis's unit is the cast's: secondary header 95, and only when not m.
            del entry["headers"]["8"]
            if unit_key(unit["value"]) not in ("m", "meter", "meters", "metre", "metres") \
                    and "95" not in out["secondary_headers"]:
                held = {"name": "Originator's Depth unit", "value": unit["value"],
                        "source": f"column name '{entry['raw_column']}'", "is_coded": True}
                spelled = _DEPTH_UNITS.get(unit_key(unit["value"]), unit["value"])
                _apply_lookup(held, lookup_code("secondary_header", 95, spelled))
                out["secondary_headers"]["95"] = held
    for entry in out["variable_specific_headers"].values():
        for vid, header in list(entry["headers"].items()):
            if not header.get("code_table") or header.get("value") in (None, ""):
                continue
            if vid == "8":
                result = lookup_unit(header["value"], entry.get("wod_code"))
                if result["status"] == "standard_unit":
                    # Already WOD's unit: nothing to record.
                    del entry["headers"][vid]
                    continue
            else:
                result = lookup_code("variable_specific_header", int(vid), header["value"])
            _apply_lookup(header, result)


def _apply_lookup(entry: Dict[str, Any], result: Dict[str, Any]) -> None:
    from wod_etl.reference.code_lists import describe_lookup

    entry["code_lookup"] = {k: v for k, v in result.items()
                            if k in ("status", "code", "label", "candidates", "possible",
                                     "field", "code_list", "retrieved", "verified", "error")}
    entry["code_lookup"]["summary"] = describe_lookup(result)
    if result.get("status") == "matched":
        entry["originator_value"] = entry["value"]
        entry["value"] = result["code"]
