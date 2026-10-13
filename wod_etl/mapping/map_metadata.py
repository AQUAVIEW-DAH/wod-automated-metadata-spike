"""Map everything in a file that is NOT a measurement: cast metadata, per-variable metadata,
and the originator's own quality flags.

`generate_field_mapping` decides what each *measurement* column is. This decides what
everything else is. Without it, a file's bottom depth, probe type, ship, wind speed,
sampling gear, measurement scale, originator units and originator flags are all dropped --
they map to no Table 3a code, so they land in `unmapped_columns` and vanish. WOD has fields
for every one of them.

Four namespaces, and they are separate (see knowledge/cast_structure.md):

    Table 4a  secondary headers            describe the CAST      -> s_<id>_<name>
    Table 5a  variable specific headers    describe ONE VARIABLE  -> v_<id>_<name>
    Table 6a  biological headers           plankton collection    -> b_<id>_<name>
    Table 7a  taxa variables               plankton observations  -> t_<id>_<name>

Only the first two are produced here; the plankton pair needs a record model this pipeline
does not write (knowledge/plankton_biomass.md).

    from wod_etl.mapping import map_metadata
    meta = map_metadata(header_text=hdr, columns=list(df.columns), df=df)

Metadata arrives three ways and all three are handled: as `Key: value` lines in a header
block, as a whole column that is constant down the cast (a `Bottom Depth` column), and as
part of a measurement column's own name (`[ITS-90, deg C]` carries a scale and a unit).
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
# Cast metadata. The key is the secondary header ID; the value is what its key looks like in
# a header line or a column name. Far more than the pipeline used to recognise, because
# every one of these appears in real submissions and every one was previously dropped.
#
# Ordered dict semantics matter: the first pattern that matches a line claims it, so more
# specific keys come before the ones they contain ('sea surface temperature' before
# 'temperature', 'wave height' before 'height').
SECONDARY_HEADER_PATTERNS: Dict[int, str] = {
    1:  r"(?:accession(?:[_\s]*(?:no|num|number))?|nodc[_\s]*accession|ncei[_\s]*accession)",
    2:  r"(?:project|programme|program|expedition)",
    # NOT 'cruise'. A cruise identifier (STOCS85A, kah1104, SHU1505) names a voyage, not a
    # vessel, and header 3 is a coded vessel. It belongs to the character data section --
    # see CHARACTER_DATA_PATTERNS.
    3:  r"(?:platform|ship(?:[_\s]*name)?|vessel|callsign|call[_\s]*sign)",
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
# The originator's own identifiers that are not secondary headers: the cruise code and the
# principal investigator. Manual p.30, "Character data and principal investigator".
CHARACTER_DATA_PATTERNS: Dict[str, str] = {
    "originator_cruise_id": r"(?:originator'?s?[_\s]*)?cruise(?:[_\s]*(?:id|code|name|no|"
                            r"num|number))?",
    "principal_investigator": r"(?:principal[_\s]*investigator|pi(?:[_\s]*name)?|"
                              r"chief[_\s]*scientist)",
}

# Keys in a staged header that are not metadata to place: coordinates are parsed by
# generate_field_mapping, and Source lines are provenance it collects.
_NOT_METADATA_KEY = re.compile(
    r"^(?:lat|latitude|lon|long|longitude|date|time|time[_\s]*zone|tz|datetime|year|month|"
    r"day|hour|minute|source(?:[_\s]*file)?s?|history)$", re.IGNORECASE)

# Keys that describe a Sea-Bird .cnv file's own layout and processing, which read_any rewrites
# as `Key: value` lines: column definitions, spans, the fill value, each SBE Data Processing
# module's settings. They are not originator metadata, and reported as unresolved they
# buried the few keys that are under a dozen notes saying a pattern was missing.
_INSTRUMENT_FILE_KEY = re.compile(
    r"^(?:nquan|nvalues|units|name\s*\d+|span\s*\d+|interval|start_time|bad_flag|file_type|"
    r"sensor\s*\d+|filename|software\s+version|system\s+upload\s+time|"
    r"(?:datcnv|filter|alignctd|celltm|loopedit|derive|binavg|wildedit|sectionx|strip|"
    r"split|bottlesum|w_filter|seasave|translate)_\w+)$", re.IGNORECASE)

# Secondary headers whose value is a number, not an identifier or a code. Everything else
# stays a string: an accession, a station or a WMO number is an identifier, and '0287423'
# must not become 287423.
NUMERIC_SECONDARY_HEADERS = {8, 10, 11, 15, 17, 20, 22, 23, 24, 25, 28, 30, 31, 36, 41, 45,
                             46, 47, 48, 49, 87, 88}

# ------------------------------------------------------------- Table 5a
# Per-variable metadata. These attach to ONE measurement, not to the cast.
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

# A column whose name says it flags another column. Group 'var' is the variable it flags.
# WOD keeps originator flags as-is (secondary header 96) rather than translating them: they
# are on the submitter's scale, not WOD's, and mapping one onto Table 12 invents meaning.
_FLAG_SUFFIX = re.compile(
    r"^(?P<var>.+?)[\s_\-]*(?:qc|q_?c|qf|quality(?:[\s_]*(?:flag|control))?|flags?|"
    r"qualit[ye]|qual)$", re.IGNORECASE)
_FLAG_PREFIX = re.compile(
    r"^(?:qc|qf|q|flag|quality(?:[\s_]*flag)?)[\s_\-]+(?P<var>.+)$", re.IGNORECASE)

# Column names that are a flag but name no variable -- they flag the whole level or cast.
_BARE_FLAG = re.compile(r"^(?:qc|qf|quality(?:[\s_]*(?:flag|control))?|flags?|"
                        r"qc[\s_]*flag|data[\s_]*quality)$", re.IGNORECASE)

# Prose lines in a staged header, skipped before any key is mined out of them.
_NOTE_LINE = re.compile(r"\s*#?\s*(?:note|comment|remark)s?\s*[:=]", re.IGNORECASE)

# Column names that are identifiers or coordinates, never measurements or metadata values.
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

    header_text   -- the file's `Key: value` header block
    columns       -- the data column names
    df            -- the data itself; needed to read a metadata COLUMN's value and to tell a
                     constant column (metadata) from a varying one (a measurement)
    dataset_code  -- the resolved WOD dataset, recorded for context

    Returns secondary_headers, variable_specific_headers, metadata_columns,
    originator_flags, and unresolved -- everything that looked like metadata and was not
    placed, which is what to read when something has gone missing.
    """
    columns = [str(c) for c in (columns or [])]
    out: Dict[str, Any] = {
        "secondary_headers": {},
        "variable_specific_headers": {},
        "metadata_columns": [],
        "originator_flags": [],
        "character_data": {},
        "cast_level_variable_headers": {},
        "unresolved": [],
        "dataset_code": dataset_code,
    }

    _from_header_block(header_text or "", out)
    _from_columns(columns, df, out)
    _from_column_names(columns, out)
    _apply_cast_level_variable_headers(columns, out)
    _find_originator_flags(columns, out)
    _attach_code_tables(out)
    _resolve_codes(out)
    return out


# ------------------------------------------------------------ header block
# A metadata block writes `key = value` in one of two shapes, and only the first was ever
# parsed. The second is what WOD's own CSV export uses, and what several originators copy:
#
#     Platform     ,,           5423.,OCL code,KAHAROA (R/V; call sign ZM7552; built 1981),
#     Bottom depth ,,              36,meters,,
#     Instrument   ,Temperature,  464.,WOD code,CTD: SBE 37-IM MicroCAT,
#     uncalibrated ,Salinity,       1.,,,
#
# Column 0 is the key, column 2 the value, and the rest a unit or code system plus a
# description. **Column 1 is the variable the key applies to**, and that is what makes the
# layout worth parsing properly: empty means the key describes the CAST (Table 4a), named
# means it describes ONE VARIABLE (Table 5a) and says which.
#
# Read with the `key[:=\s]+value` rule instead, the value stops at the first comma and
# captures a fragment of the LABEL -- 'Platform' yielded 'ZM7552' out of the parenthetical
# description, 'Accession Number' yielded the word 'Number'. Three headers, all three wrong,
# and every per-variable key missed, with no sign anything was off.
_DELIMITED_HEADER = re.compile(r"^\s*([^,\t]{2,48}?)\s*[,\t]\s*([^,\t]{0,48}?)\s*[,\t](.*)$")

# 'NODC code', 'OCL code', 'WOD code' in the field after the value say the value is ALREADY a
# code in a published list -- worth recording, because it means no lookup is needed and no
# code should be invented for it.
_CODE_SYSTEM = re.compile(r"\b(NODC|OCL|WOD|WMO|ISO|NCEI)\s*code\b", re.IGNORECASE)

# Keys that are coordinates or time. They are parsed elsewhere and would otherwise flood
# `unresolved` with things that are not missing at all.
_COORDINATE_KEY = re.compile(
    r"^(lat|latitude|lon|long|longitude|year|month|day|hour|minute|second|date|time|"
    r"datetime|julian|yearday|depth|pressure)\b", re.IGNORECASE)

# Lines that are structural markers in a block rather than key/value pairs.
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
            # The key must start the line or follow a delimiter or a column gap -- never sit
            # mid-sentence, where any English use of 'cast' or 'station' would match. The
            # value runs to the end of the line, a delimiter, or a 2+ space column gap.
            m = re.search(rf"(?:^|[,;|]|\s{{2,}})\s*#?\s*({key_pat})\b(\s*[:=]\s*|\s+)"
                          rf"([^\s,;|][^,;|]*?)(?=\s{{2,}}|[,;|]|$)", line, re.IGNORECASE)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(3).strip().strip("\"'")
            # 'Weather observer: K.' is not header 26 'Weather' = 'observer: K.'. A key joined
            # to its value by a bare space is only that when the value holds no separator
            # of its own; otherwise the real key is longer than the pattern matched.
            if not re.search(r"[:=]", m.group(2)) and re.search(r"[:=]", value):
                continue
            if _NOT_METADATA_KEY.match(key) or _character_data_key(key):
                continue
            # One key, one header: 'Cast duration: 30' must not also be read as a cast
            # number 'duration: 30' by the shorter pattern that matches its first word.
            if any(a <= m.start(1) < b or m.start(1) <= a < m.end(1) for a, b in spans):
                continue
            spans.append((m.start(1), m.end(3)))
            placed_keys.add(_norm_key(key))
            held = out["secondary_headers"].get(str(sec_id))
            if held is not None:
                # A second key claiming a header that is already held used to vanish here
                # without a word. It is recorded, like every other key that is not placed.
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


# `Key: value` as a staged header writes it, possibly several per line separated by a
# column gap. Only a colon or equals sign counts here: the pattern pass above also accepts a
# bare space, which is right for a KNOWN key and far too loose for an unknown one.
_KEY_VALUE = re.compile(
    r"(?:^|[,;|]|\s{2,})\s*#?\s*([A-Za-z][A-Za-z0-9 _.'/()&-]{0,47}?)\s*[:=]\s*"
    r"([^,;|]*?)\s*(?=\s{2,}[A-Za-z][A-Za-z0-9 _.'/()&-]{0,47}?\s*[:=]|[,;|]|$)")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _character_data_key(key: str) -> Optional[str]:
    for field, pattern in CHARACTER_DATA_PATTERNS.items():
        if re.fullmatch(pattern, key.strip().replace("_", " "), re.IGNORECASE):
            return field
    return None


def _from_key_value_line(line: str, placed_keys, out: Dict[str, Any]) -> None:
    """Every `Key: value` on a line that the secondary-header pass did not place.

    Before this, a header line whose key matched no Table 4a pattern disappeared: no
    secondary header, no entry in `unresolved`, nothing. A staged `# Instrument: SBE ...`
    and `# Cruise: SHU1505` both went that way. Now each lands somewhere a reader will see:
    the character data, a per-variable header for every measured variable, or
    `unresolved`.
    """
    for m in _KEY_VALUE.finditer(line):
        key, value = m.group(1).strip(), m.group(2).strip().strip("\"'")
        if (not value or _NOT_METADATA_KEY.match(key) or _INSTRUMENT_FILE_KEY.match(key)
                or _norm_key(key) in placed_keys):
            continue
        evidence = line.strip()[:120]

        field = _character_data_key(key)
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
            # Said once for the whole cast -- '# Instrument: SBE 911plus' -- so it applies to
            # every measured variable. A per-variable value, where one differs, belongs in
            # the delimited form that names the variable in column 1.
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
            "note": "no WOD field matched this key. Either it has no WOD home -- report it "
                    "as not carried -- or a pattern is missing from map_metadata."})


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

    Needs at least three columns. A bare `key,value` is refused: a table's column header row
    has that shape too, and reading `Depth,Temp,Sal` as a metadata pair is worse than missing
    the key.
    """
    if _BLOCK_MARKER.match(line):
        return None
    m = _DELIMITED_HEADER.match(line)
    if not m:
        return None
    key = m.group(1).strip().strip("#*").strip()
    if not key or not re.search(r"[A-Za-z]", key) or _COORDINATE_KEY.match(key):
        return None

    rest = [f.strip() for f in m.group(3).split(",")]
    # The value is column 2, not "the next non-empty column". Scanning forward past an empty
    # value slot picks up the code-system label instead: `Accession Number ,, ,NODC code,,`
    # has no accession number in it, and reported one reading 'NODC code'.
    value = rest[0] if rest else ""
    if not value:
        return {"key": key, "variable": m.group(2).strip() or None, "value": None,
                "unit": None, "code_system": None, "description": None, "blank": True}

    # The two columns after the value are, in this export's order, a unit OR a code system,
    # then a description. Deciding per field on length instead put a short description in the
    # unit slot: 'CTD: SBE 37-IM MicroCAT' is 23 characters and became the unit of a probe
    # type, which has no unit at all.
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
    """Does an isolated metadata key name this header?

    A search, not a full match: the key is already isolated to its own column, so there is no
    prose for a loose match to catch, and 'Originators Station ID' has to reach header 7.
    """
    return bool(re.search(rf"\b{pattern}\b", key.replace("_", " "), re.IGNORECASE))


def _place_pair(pair, line, names, out) -> None:
    """File one delimited pair into the WOD field that holds it, or record it as unplaced."""
    key, value, variable = pair["key"], pair["value"], pair["variable"]
    evidence = line.strip()[:120]

    if pair["blank"]:
        out["unresolved"].append({
            "key": key, "value": None, "variable": variable, "evidence": evidence,
            "matches": None,
            "note": "the key is present in the block but its value column is empty. Nothing "
                    "was recorded for it; if WOD needs it, it has to come from elsewhere."})
        return

    # Column 1 names the variable, so a per-variable key can be attached to it directly.
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
            # The value is already a code in a published list. Saying so stops a reader
            # treating '464.' as a free-text probe name, or looking up a code that is the
            # answer already.
            entry["code_system"] = pair["code_system"]
            entry["value_is_already_a_code"] = True

        held = out["secondary_headers"].get(str(sec_id))
        if held is None:
            out["secondary_headers"][str(sec_id)] = entry
            return
        # Two keys claim one header. Prefer the one whose value is a published code: for
        # header 3, 'Originators Cruise ID = kah1104' and 'Platform = 5423. (OCL code)' both
        # match, and WOD's platform code is the second. The loser is recorded, never dropped.
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
                        "it during staging, or leave it out and say so."})
            return

    out["unresolved"].append({
        "key": key, "value": value, "variable": variable, "evidence": evidence,
        "matches": None,
        "note": "no WOD field matched this key. Either it has no WOD home -- report it as "
                "not carried -- or the pattern is missing from SECONDARY_HEADER_PATTERNS."})


def _coerce(value: str, sec_id: int):
    if sec_id in NUMERIC_SECONDARY_HEADERS and re.fullmatch(_NUM, value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------- columns
def _from_columns(columns: List[str], df, out: Dict[str, Any]) -> None:
    """A whole column can BE a secondary header: `Bottom Depth (m)` is header 10.

    The test is that the column carries no WOD variable code and its name matches a
    secondary-header key. Its value is taken only when it is constant down the cast -- a
    varying 'bottom depth' along an underway track is a per-level measurement WOD has no
    place for, and collapsing it to one number would be a fabrication.
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
                    f"'{col}' varies down the cast, so no single value is recorded. A "
                    f"secondary header describes the whole cast; keep the column in the "
                    f"staged copy and split the file per cast if the variation is between "
                    f"casts rather than within one.")
            # An existing header-block value wins: it was stated about the cast, while a
            # column value is inferred from the data.
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
    """A measurement column's own name carries per-variable metadata.

    `t090C: Temperature [ITS-90, deg C]` states a scale (variable specific header 3) and the
    originator's unit (header 8). Both are dropped by name normalisation -- which is correct
    for MATCHING and wrong for recording, so they are recovered here.
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
        # Table 5a header 17: a nitrate column that is really nitrate + nitrite. This is how
        # WOD records the combination, and without it the two are indistinguishable.
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

    Kept verbatim, never translated. The submitter's scale is not WOD Table 12 -- an
    originator's '1' may mean good where WOD's 1 means a failed range check -- so mapping one
    onto the other invents meaning. WOD's own answer is secondary header 96, which names the
    flag SET so a reader can look the scheme up:
    https://www.ncei.noaa.gov/access/world-ocean-database/CODES/s_96_origflagset.html
    """
    by_norm = {normalize_name(c): c for c in columns}
    # A flag column names its variable plainly ('Temperature_QC') while the measurement
    # column may be an instrument export ('t090C: Temperature [ITS-90, deg C]'), so a
    # name-to-name match misses. Matching on the resolved WOD code instead pairs them.
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
                "note": "names no variable, so it flags the whole level or cast. Check the "
                        "file's own documentation before assuming which."})
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
                    f"names '{target_raw}', which is not a column in this file. Keep the "
                    f"flag anyway and say what it was called.",
        })
    if out["originator_flags"] and "96" not in out["secondary_headers"]:
        out["secondary_headers"]["96"] = {
            "name": "Originator's flags",
            "value": None,
            "source": "inferred from flag columns",
            "note": "This file carries the originator's own quality flags. WOD records "
                    "WHICH flag set was used as secondary header 96; the set is not stated "
                    "in the file, so it must come from the submitter or the accession "
                    "documentation. The flag VALUES are kept verbatim and are NOT WOD "
                    "Table 12 flags.",
        }


# ------------------------------------------------------------ code tables
def _attach_code_tables(out: Dict[str, Any]) -> None:
    """Add the published code list to every field that has one.

    A coded metadata value is unusable without its code list, and the manual publishes those
    online rather than in the PDF -- so the link is the answer, not a convenience.
    """
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
    """Look every coded value up in its published list, and say what was found.

    A coded field is written as a code only when its value is one: a number already, or text
    that names exactly one entry. Anything else keeps the originator's text, and
    `code_lookup` says why -- ambiguous, no entry, list unavailable -- so the writer can put
    the text beside the field rather than in it.
    """
    from wod_etl.reference.code_lists import lookup_code

    for sid, entry in out["secondary_headers"].items():
        if not entry.get("is_coded") or entry.get("value") in (None, ""):
            continue
        result = lookup_code("secondary_header", int(sid), entry["value"])
        _apply_lookup(entry, result)
    for entry in out["variable_specific_headers"].values():
        for vid, header in entry["headers"].items():
            if not header.get("code_table") or header.get("value") in (None, ""):
                continue
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


if __name__ == "__main__":
    import json

    import pandas as pd

    frame = pd.DataFrame({
        "Depth (m)": [0, 10, 25],
        "t090C: Temperature [ITS-90, deg C]": [15.8, 15.4, 14.1],
        "Temperature_QC": [1, 1, 3],
        "Nitrate + Nitrite (umol/kg)": [0.2, 0.4, 1.1],
        "Bottom Depth (m)": [2500, 2500, 2500],
        "Conductivity (S/m)": [4.41, 4.38, 4.27],
    })
    header = ("CRUISE: STOCS85A\nSTATION: 001\nPROBE TYPE: T-7\n"
              "WIND SPEED: 12.5\nWATER SAMPLER: Niskin\nACCESSION: 0287423")
    print(json.dumps(map_metadata(header, list(frame.columns), frame, "CTD"), indent=2))
