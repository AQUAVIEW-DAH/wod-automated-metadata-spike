"""Decide what a file IS -- which WOD dataset, or not WOD profile data at all.

`dataset_type` used to be an argument to `process_accession`, which is only correct when
the whole accession is one instrument. Real accessions are not: a single submission holds
shipboard CTD, bottle nutrients, an ADCP section and a plankton haul, and one label applied
to all of them is wrong for most of them.

This reads the evidence a file already carries -- its path, its header text, its column
names, its depth spacing -- and returns a ranked verdict with the evidence attached, so a
wrong call is visible rather than silent.

    from wod_etl.discovery import classify_dataset, classify_accession

    classify_dataset(path="0287423/2-CTD/cast_01.cnv", columns=["prDM", "t090C", "sal00"])
    # {"dataset_type": "CTD", "confidence": "high", "evidence": [...], ...}

    classify_accession("data/raw/0287423")     # every file, grouped by folder

Two verdicts matter and they are different:
  * `is_wod_profile_data: False` -- this cannot become a profile. `exclusion` says why and,
    where the data still belongs in WOD, which path it belongs on instead.
  * `dataset_type: None` -- it looks like profile data but nothing identifies the
    instrument. `candidates` holds what it might be; do not guess silently.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl.config import HIGH_RES_DEPTH_INCREMENT_M

# --------------------------------------------------------------- signals
# Each entry is (regex, weight, what the match tells you). Weights are relative and only
# ever compared within one file: an explicit instrument name in a header beats a folder
# name, which beats a column-shape inference.
INSTRUMENT_SIGNALS: Dict[str, List[tuple]] = {
    "CTD": [
        (r"\bctd\b", 6, "names CTD"),
        (r"\bsea[\s_-]?bird\b|\bsbe\b|\bsbe\d{2}\b", 5, "Sea-Bird CTD instrument"),
        (r"\bdown[\s_-]?cast\b|\bup[\s_-]?cast\b", 3, "cast direction, a CTD idiom"),
        (r"\bt\d{3}c\b|\bprdm\b|\bsal\d{2}\b", 4, "Sea-Bird export column names"),
        (r"\bcnv\b", 3, "Sea-Bird .cnv export"),
    ],
    "XCTD": [(r"\bxctd\b", 6, "names XCTD")],
    "XBT": [
        (r"\bxbt\b", 6, "names XBT"),
        (r"\bsippican\b|\bdeep[\s_-]?blue\b|\bt-?[457]\b", 4, "XBT probe model"),
        (r"expendable\s+bathythermograph", 5, "expendable bathythermograph in prose"),
    ],
    "MBT": [
        (r"\bmbt\b|\bdbt\b", 5, "names MBT/DBT"),
        (r"mechanical\s+bathythermograph", 5, "mechanical bathythermograph in prose"),
    ],
    "OSD": [
        (r"\bbottle\b|\bbtl\b|\bniskin\b|\bnansen\b|\brosette\b", 5, "bottle sampling gear"),
        (r"\bhydro[\s_-]?cast\b|\bhydrography\b", 4, "hydrocast"),
        (r"\bosd\b", 5, "names OSD"),
        (r"\bdiscrete\b.*\bsample|\bwater\s+sample", 3, "discrete water samples"),
        (r"\bnutrient|\balkalinit|\bdic\b|\btco2\b", 2, "bottle-only chemistry"),
    ],
    "SUR": [
        (r"\bunderway\b|\btsg\b|thermosalinograph", 5, "underway/thermosalinograph"),
        (r"\bsurface[\s_-]?only\b|\bsur\b", 4, "names surface-only data"),
        (r"\bsst\b|\bsss\b", 2, "surface temperature/salinity"),
    ],
    "APB": [
        (r"\bpinniped\b|\belephant\s+seal\b|\bsea\s+lion\b|\bmeop\b", 6, "pinniped platform"),
        (r"\bapb\b", 5, "names APB"),
        (r"\bseal[\s_-]?tag\b|animal[\s_-]?borne", 5, "animal-borne sensor"),
    ],
    "MRB": [
        (r"\bmoor(ed|ing)\b", 6, "moored platform"),
        (r"\btao\b|\bpirata\b|\brama\b|\btriton\b", 5, "moored buoy array"),
        (r"\bmrb\b", 5, "names MRB"),
    ],
    "PFL": [
        (r"\bargo\b|\bbgc[\s_-]?argo\b", 6, "Argo programme"),
        (r"profiling\s+float|\bpfl\b", 6, "profiling float"),
        (r"\bapex\b|\bnavis\b|\bsolo\b|\bprovor\b|\bnemo\b", 4, "float model"),
        (r"\bwmo\b.*\bfloat\b|\bjuld\b|\bn_prof\b", 3, "Argo file conventions"),
    ],
    "DRB": [
        (r"drift(ing)?[\s_-]?buoy|\bdrifter\b", 6, "drifting buoy"),
        (r"\bsvp\b|\bdrb\b", 4, "drifter programme or names DRB"),
    ],
    "UOR": [
        (r"\buor\b|undulating\s+oceanographic", 6, "undulating recorder"),
        (r"\bscanfish\b|\bseasoar\b|\btowed\s+undulat", 5, "undulating tow body"),
    ],
    "GLD": [
        (r"\bglider\b|\bgld\b", 6, "glider"),
        (r"\bslocum\b|\bseaglider\b|\bspray\b", 5, "glider model"),
    ],
}

# Content that cannot become a WOD profile. `route` says where it does belong, if anywhere;
# `harvest` says what to take OUT of it before setting it aside, because "cannot be a
# profile" and "has nothing in it" are different claims. A moored ADCP section carries
# bottom depth, positions and times for casts taken on the same station -- excluding the
# folder and walking away discards metadata WOD has fields for.
#
# These are FILE-level: they describe what the whole file is. They are matched against the
# path and the column names, never against prose alone -- a readme that lists an accession's
# other folders mentions plankton and ADCP, and excluding the CTD file sitting beside it on
# that evidence loses real data.
EXCLUSION_SIGNALS: List[tuple] = [
    (r"\bplankton\b|\bzooplankton\b|\bphytoplankton\b|\btaxa\b|\btaxon\b|\bspecies\b|"
     r"\bbiomass\b|\babundance\b|\bbongo\b|\bmocness\b|\bnet[\s_-]?tow\b|"
     r"displacement\s+volume|\bitis\b|\bwet\s+weight\b",
     "plankton biomass or taxonomy",
     "Belongs in WOD, but through the Biological Header (Table 6a) and Taxa-Records "
     "(Table 7a) -- a depth RANGE, not a level. Not writable by the profile path. See "
     "knowledge/plankton_biomass.md.",
     "Capture the Table 6a collection metadata while the accession is open -- gear and tow "
     "type, mesh size, water volume filtered or tow distance, preservative, counting and "
     "weighing method. Only the submitter has it, and a future plankton writer needs all "
     "of it."),
    (r"\badcp\b|\bcurrent(s|_speed|_direction)?\b|\bu_vel|\bv_vel|\bvel_(east|north)\b|"
     r"\blowered\s+adcp\b|\bladcp\b",
     "current velocity",
     "Not in WOD Table 3a. WOD stores no velocity variable; there is no code to map it to.",
     "The VELOCITIES are dropped; the rest of the file often is not. Bottom depth (from "
     "bottom-tracking) is secondary header 10, and the navigation -- position and time "
     "along the track -- can supply or verify coordinates for casts taken on the same "
     "station. Harvest those into the profiles they align with, and cite the file you took "
     "them from."),
    (r"\bedna\b|\bmetabarcod|\bamplicon\b|\botu\b|\basv\b|\bsequenc(e|ing)\b|\bgenom",
     "genomic / eDNA data",
     "Not in WOD Table 3a.",
     "Sample position, depth and time may still identify the cast the sample came from. "
     "Nothing else here is usable."),
    (r"\bmultibeam\b|\bbathymetr|\bswath\b|\bsounding\b|\bgebco\b|\bsingle[\s_-]?beam\b",
     "bathymetry / seafloor depth",
     "Bottom depth is secondary header 10 on a cast, not a measured profile of its own.",
     "This is the clearest harvest case: a sounding at a cast's position IS that cast's "
     "secondary header 10. Match on position and take it, rather than leaving bottom depth "
     "empty. Record which file the value came from."),
    (r"\bcatch\b|\btrawl\b|\bcpue\b|length[\s_-]?frequency|\bfisher(y|ies)\b|\bstomach\b",
     "fisheries catch data",
     "Not in WOD Table 3a.",
     "Haul position, depth and time only, and only if they identify a cast."),
    (r"\bpah\b|\bpcb\b|\bmicroplastic|trace\s+metal|\bcontaminant",
     "contaminant chemistry",
     "Not in WOD Table 3a.",
     "Sample station, bottle depth and time, where they identify a cast that IS being "
     "written."),
]

# Columns WOD has no code for are NOT decided here. `explain_column` in
# wod_etl/mapping/query_wod_spec.py is the single registry -- it is what `match_variable`
# consults to refuse a match, so a second copy in this file would drift from the behaviour
# it claims to describe. `_unstorable_columns` below delegates to it.

CONFIDENCE_THRESHOLDS = ((8, "high"), (4, "medium"), (1, "low"))

# What a column set alone can say when nothing names an instrument.
_DEPTH_TOKENS = re.compile(r"^(depth|dep|z|deph|pres|press|pressure|prdm|depsm)", re.I)
_TEMP_TOKENS = re.compile(r"temp|^t\d{3}|^t$|sst", re.I)


def classify_dataset(
    path: Optional[str] = None,
    columns: Optional[List[str]] = None,
    header_text: str = "",
    depth_increment_m: Optional[float] = None,
    n_levels: Optional[int] = None,
    extra_context: str = "",
) -> Dict[str, Any]:
    """Identify a file's WOD dataset from whatever evidence it carries.

    Every argument is optional; pass what you have. `path` should be the file's path
    relative to the accession root, because the folder name is often the only label a
    submitter gives ("2-CTD/", "plankton/").
    """
    columns = [str(c) for c in (columns or [])]
    haystacks = {
        "path": (str(path or "").replace("\\", "/"), 1.0),
        "header": (header_text or "", 1.0),
        "columns": (" ".join(columns), 0.8),
        "context": (extra_context or "", 0.6),
    }

    exclusion, prose_flags = _find_exclusion(haystacks, columns)
    unstorable = _unstorable_columns(columns)
    scores, evidence = _score_instruments(haystacks)
    shape = _shape_evidence(columns, depth_increment_m, n_levels)
    for code, weight, why in shape:
        scores[code] = scores.get(code, 0.0) + weight
        evidence.setdefault(code, []).append(why)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = (ranked[0] if ranked else (None, 0.0))
    confidence = _confidence(best_score)

    result: Dict[str, Any] = {
        "source": str(path or ""),
        "verdict": "excluded" if exclusion else
                   ("profile" if confidence != "none" else "unidentified"),
        "dataset_type": best if confidence != "none" else None,
        "confidence": exclusion["confidence"] if exclusion else confidence,
        "score": round(best_score, 2),
        "evidence": evidence.get(best, []) if best else [],
        "candidates": [{"dataset_type": c, "score": round(s, 2),
                        "evidence": evidence.get(c, [])}
                       for c, s in ranked[:4]],
        "is_wod_profile_data": exclusion is None,
        "exclusion": exclusion,
        "unstorable_columns": unstorable,
        "possible_exclusions": prose_flags,
        "depth_increment_m": depth_increment_m,
    }
    if exclusion is not None:
        result["dataset_type"] = None
        result["evidence"] = [exclusion["matched"]]
    elif confidence == "none":
        result["note"] = (
            "Nothing in the path, header or columns names an instrument. Do NOT default "
            "silently: read the folder's readme, then pass dataset_type explicitly. If the "
            "file is bottle-like (coarse levels, chemistry columns) OSD is the usual "
            "answer, but say that you assumed it.")
    elif best in ("CTD", "XCTD") and depth_increment_m is None:
        result["note"] = (
            "CTD and XCTD route to CTD or OSD by the cast's depth increment, not by "
            "instrument (manual p.19). Pass depth_increment_m to settle it; without one "
            "the final dataset is decided later, per cast.")
    return result


def classify_accession(root: str, max_files: Optional[int] = None,
                       peek_bytes: int = 8192) -> Dict[str, Any]:
    """Classify every file under an accession root and group the verdict by folder.

    This is the triage step: what is in this accession, which folders hold WOD profile
    data, and what should be excluded and why. It reads each file's head, so it is cheap
    enough to run on a whole accession before processing any of it.
    """
    from wod_etl.discovery.sniff_format import sniff_format

    base = Path(root)
    if not base.exists():
        return {"root": str(root), "error": f"no such directory: {root}", "folders": {}}

    files = sorted(p for p in base.rglob("*") if p.is_file())
    if max_files:
        files = files[:max_files]

    folders: Dict[str, Dict[str, Any]] = {}
    for p in files:
        rel = p.relative_to(base).as_posix()
        folder = str(Path(rel).parent)
        fmt = sniff_format(str(p), num_bytes=peek_bytes)
        head = ""
        if fmt.get("encoding"):
            try:
                head = p.open("r", encoding=fmt["encoding"], errors="replace").read(peek_bytes)
            except OSError:
                head = ""

        columns = _columns_from_head(head, fmt)
        readable = bool(fmt.get("readable", False))
        # An unreadable file is not classified. A readme naming an accession's folders
        # scores every instrument in it, and a dataset_type on a file nothing can read is
        # noise that reads like a decision.
        verdict = (classify_dataset(path=rel, columns=columns, header_text=head) if readable
                   else {"dataset_type": None, "confidence": "none", "verdict": "unreadable",
                         "is_wod_profile_data": False, "exclusion": None, "evidence": [],
                         "unstorable_columns": []})
        density = _column_density(head, fmt, columns) if readable else None

        entry = folders.setdefault(folder, {"files": [], "dataset_types": {},
                                            "excluded": {}, "unreadable": [], "no_data": [],
                                            "harvest": []})
        entry["files"].append({
            "path": rel, "format": fmt["format_family"], "reader": fmt["reader"],
            "readable": readable,
            "dataset_type": verdict["dataset_type"], "confidence": verdict["confidence"],
            "is_wod_profile_data": verdict["is_wod_profile_data"],
            "exclusion": (verdict["exclusion"] or {}).get("reason"),
            "harvest": (verdict["exclusion"] or {}).get("harvest"),
            "unstorable_columns": [u["column"] for u in verdict.get("unstorable_columns", [])],
            "empty_columns": (density or {}).get("empty_columns"),
            "measurement_density": (density or {}).get("measurement_density"),
            # Kept per file so the folder-level shape check can compare files to each other.
            # One file per depth and one file per variable are invisible in any single file.
            "columns": columns or None,
            "sampled_rows": (density or {}).get("sampled_rows"),
            "evidence": verdict["evidence"][:3],
        })
        if not readable:
            entry["unreadable"].append(rel)
        elif density and density.get("measurement_density") == 0.0:
            entry["no_data"].append(rel)
        if verdict["exclusion"]:
            reason = verdict["exclusion"]["reason"]
            entry["excluded"][reason] = entry["excluded"].get(reason, 0) + 1
            if verdict["exclusion"].get("harvest"):
                entry["harvest"].append(verdict["exclusion"]["harvest"])
        elif verdict["dataset_type"]:
            dt = verdict["dataset_type"]
            entry["dataset_types"][dt] = entry["dataset_types"].get(dt, 0) + 1

    for folder, entry in folders.items():
        entry["verdict"] = _folder_verdict(folder, entry)
        entry["cross_file_shape"] = _cross_file_shape(entry)

    shapes = {f: e["cross_file_shape"] for f, e in folders.items() if e["cross_file_shape"]}
    return {
        "root": str(root),
        "file_count": len(files),
        "folder_count": len(folders),
        "folders": folders,
        "cross_file_shapes": shapes or None,
        "process": [f for f, e in folders.items() if e["verdict"]["action"] == "process"],
        "exclude": [f for f, e in folders.items() if e["verdict"]["action"] == "exclude"],
        "inspect": [f for f, e in folders.items() if e["verdict"]["action"] == "inspect"],
        "note": "A verdict per folder, from file heads only. Read each folder's readme "
                "before acting on it -- 'inspect' means the evidence did not decide.",
        "shape_note": ("cross_file_shapes flags folders whose files look like PARTS of one "
                       "cast rather than one cast each. Processed as they stand, those "
                       "produce a pile of single-level profiles or a set of half-empty ones. "
                       "Combine during staging first."
                       if shapes else None),
    }


# ---------------------------------------------------------------- internals
def _search(pattern, text):
    """Match against the text and against a separator-flattened copy of it.

    Paths and column names join words with underscores and hyphens, which are word
    characters to `re`, so `\bglider\b` does not match 'glider_slocum' and `\bctd\b` does
    not match '02-CTD-downcast'. Flattening those to spaces finds it. Both forms are tried
    because some signals are written around the separator ('u_vel', 'sea-bird').
    """
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m
    flattened = re.sub(r"[_\-./\\]+", " ", text)
    return re.search(pattern, flattened, re.IGNORECASE)


def _find_exclusion(haystacks, columns):
    """(exclusion or None, prose flags).

    An exclusion needs evidence about the file itself: its path, or its own columns. A
    match in prose is recorded as something to check, never as a verdict -- and a match in
    the columns only excludes when the file has no other mappable measurement, so a file
    carrying both a profile and something WOD cannot store keeps its profile.
    """
    profile_columns = _mappable_columns(columns)
    prose_flags = []

    for pattern, reason, route, harvest in EXCLUSION_SIGNALS:
        for field in ("path", "columns", "header", "context"):
            text = haystacks[field][0]
            m = _search(pattern, text)
            if not m:
                continue
            hit = f"{field} matches {m.group(0)!r}: {reason}"
            verdict = {"reason": reason, "route": route, "harvest": harvest,
                       "matched_in": field, "confidence": "high", "matched": hit,
                       "belongs_in_wod": reason == "plankton biomass or taxonomy"}
            if field == "path":
                return verdict, prose_flags
            if field == "columns":
                if not profile_columns:
                    return verdict, prose_flags
                prose_flags.append(
                    f"{hit} -- but the file also carries mappable profile columns "
                    f"({', '.join(profile_columns[:4])}), so it is processed as a profile "
                    f"and the {reason} columns are dropped. Report both.")
                break
            prose_flags.append(f"{hit} (mentioned in {field} text only, not evidence about "
                               f"this file's own content -- check it during triage)")
            break
    return None, prose_flags


def _mappable_columns(columns) -> List[str]:
    """Columns that resolve to a Table 3a code or the depth axis.

    Imported lazily: mapping is Phase 2 and discovery is Phase 1, and the dependency is
    only needed for this one judgement.
    """
    try:
        from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
    except ImportError:
        return []
    out = []
    for c in columns:
        code = match_variable(c)
        if code is not None and code != DEPTH_SENTINEL_CODE:
            out.append(str(c))
    return out


def _unstorable_columns(columns) -> List[Dict[str, str]]:
    """Columns WOD has no Table 3a code for, with the reason and what to do instead.

    Delegates to the mapping layer so there is one registry, not two: `explain_column` is
    what `match_variable` consults to REFUSE a match, so anything it lists is genuinely
    unmapped, and anything it does not list genuinely mapped. A column that resolves to a
    code -- 'Nitrate + Nitrite' does, to code 8 -- is never reported as dropped.
    """
    try:
        from wod_etl.mapping.query_wod_spec import explain_column
    except ImportError:
        return []
    out = []
    for c in columns:
        reason = explain_column(c)
        if reason:
            out.append({"column": str(c), "variable": reason["variable"].replace("_", " "),
                        "reason": reason["reason"], "action": reason["route"]})
    return out


def _score_instruments(haystacks):
    scores: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = {}
    for code, signals in INSTRUMENT_SIGNALS.items():
        for pattern, weight, why in signals:
            for field, (text, field_weight) in haystacks.items():
                m = _search(pattern, text)
                if not m:
                    continue
                scores[code] = scores.get(code, 0.0) + weight * field_weight
                evidence.setdefault(code, []).append(
                    f"{field} matches {m.group(0)!r}: {why}")
                break            # one hit per signal, not per field
    return scores, evidence


def _shape_evidence(columns, depth_increment_m, n_levels):
    """What the table's own shape implies when nothing names an instrument."""
    out = []
    has_depth = any(_DEPTH_TOKENS.search(c) for c in columns)
    has_temp = any(_TEMP_TOKENS.search(c) for c in columns)

    if columns and not has_depth and has_temp:
        out.append(("SUR", 3.0, "columns carry temperature but no depth or pressure axis: "
                                "a surface-only record"))
    if depth_increment_m is not None:
        if depth_increment_m < HIGH_RES_DEPTH_INCREMENT_M:
            out.append(("CTD", 3.0, f"depth increment {depth_increment_m:.2f} m is "
                                    f"high-resolution (< {HIGH_RES_DEPTH_INCREMENT_M} m)"))
        else:
            out.append(("OSD", 2.0, f"depth increment {depth_increment_m:.2f} m is "
                                    f"low-resolution (>= {HIGH_RES_DEPTH_INCREMENT_M} m), "
                                    f"which is bottle-like spacing"))
    if n_levels is not None and n_levels <= 3 and not has_depth:
        out.append(("SUR", 1.5, f"{n_levels} level(s) and no depth axis"))
    return out


def _confidence(score: float) -> str:
    for threshold, label in CONFIDENCE_THRESHOLDS:
        if score >= threshold:
            return label
    return "none"


def _columns_from_head(head: str, fmt) -> List[str]:
    """Best-effort column names from a file's first bytes, for triage only."""
    if fmt.get("reader") not in ("delimited", "fixed_width", "sea_bird_cnv"):
        return []
    delim = fmt.get("delimiter", ",")
    names = []
    for line in head.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# name "):
            names.append(s.partition("=")[2].strip())
            continue
        if s.startswith(("#", "*", "%", "//", ";", "!")):
            continue
        if not names and delim in s:
            return [c.strip() for c in s.split(delim)]
        if not names:
            return s.split()
    return names


# Columns that index or locate a level rather than measure anything. A file whose only
# populated columns are these has no measurements in it, however full it looks.
_NON_MEASUREMENT = re.compile(
    r"^(station|stn|cast|sample|bottle|niskin|id|seq|line|depth|dep|z|pres|press|pressure|"
    r"lat|latitude|lon|long|longitude|date|time|datetime|year|month|day|hour|julian|"
    r"yearday|flag|qc|quality)\b", re.I)


# A folder's files are usually one cast each. Two shapes break that, both silently:
#   * one file per DEPTH   -- identical columns, one or two rows each. Processed as they
#                             stand this is a pile of single-level profiles.
#   * one file per VARIABLE -- disjoint measurement columns sharing a depth column. Processed
#                             as they stand each cast is missing most of its variables.
# Neither is detectable in a single file, which is why nothing caught them: the evidence is
# the RELATIONSHIP between files. This is a cheap check over the heads already read.
MIN_FILES_FOR_SHAPE_CHECK = 3
MAX_ROWS_FOR_ONE_LEVEL_PER_FILE = 2


def _cross_file_shape(entry) -> Optional[Dict[str, Any]]:
    """Whether a folder's files look like parts of one cast rather than one cast each."""
    readable = [f for f in entry["files"] if f.get("readable") and f.get("columns")]
    if len(readable) < MIN_FILES_FOR_SHAPE_CHECK:
        return None

    column_sets = [tuple(f["columns"]) for f in readable]
    rows = [f.get("sampled_rows") or 0 for f in readable]

    identical = len(set(column_sets)) == 1
    tiny = [r for r in rows if 0 < r <= MAX_ROWS_FOR_ONE_LEVEL_PER_FILE]
    if identical and len(tiny) >= len(readable) * 0.8:
        return {
            "shape": "one file per depth level",
            "files": len(readable),
            "evidence": f"{len(readable)} files share identical columns "
                        f"{list(column_sets[0])[:5]} and hold "
                        f"{MAX_ROWS_FOR_ONE_LEVEL_PER_FILE} rows or fewer each",
            "action": "These are levels of ONE cast, not separate casts. Combine with "
                      "wod_etl.staging.combine_levels, sort by depth, then stage one file. "
                      "Left alone the output is a pile of single-level profiles.",
        }

    # One file per variable: each file has its own measurement column but they share a key.
    shared = set(column_sets[0])
    for cols in column_sets[1:]:
        shared &= set(cols)
    key = next((c for c in shared if _DEPTH_TOKENS.search(str(c))
                or re.search(r"time|date|station", str(c), re.I)), None)
    if key is None or identical:
        return None
    measurement_sets = [frozenset(c for c in cols if c not in shared) for cols in column_sets]
    distinct = {s for s in measurement_sets if s}
    if len(distinct) >= max(2, len(readable) - 1):
        return {
            "shape": "one file per variable",
            "files": len(readable),
            "join_key": key,
            "evidence": f"{len(readable)} files share only {sorted(shared)[:4]} and each "
                        f"carries a different measurement column",
            "action": f"These may be variables of ONE cast. Join with "
                      f"wod_etl.staging.combine_on_key(key={key!r}) and CHECK "
                      f"info['per_table'] -- a near-zero key overlap means they do not "
                      f"actually share a key and the join is meaningless, which looks like "
                      f"success. Confirm against the folder readme before joining.",
        }
    return None


def _column_density(head: str, fmt, columns) -> Optional[Dict[str, Any]]:
    """Per-column fill rate over the sampled rows -- triage question 2, "is there any data?"

    A column of `Not analyzed` or `***NA` parses perfectly, so nothing downstream notices
    that the file holds no measurements. `measurement_density` ignores the columns that
    index or locate a level: a nutrient file whose station and depth are full and whose
    three nutrient columns are entirely `Not analyzed` scores 0.0 here, which is the
    answer, while an overall density would score 0.4 and look like data.
    """
    from wod_etl.config import NA_SENTINELS

    if fmt.get("reader") not in ("delimited", "fixed_width", "sea_bird_cnv"):
        return None
    delim = fmt.get("delimiter")
    rows, seen_header = [], False
    for line in head.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "*", "%", "//", ";", "!")):
            continue
        if not seen_header and not any(ch.isdigit() for ch in s):
            seen_header = True
            continue
        rows.append(s)
        if len(rows) >= 40:
            break
    if not rows:
        return None

    blank = {"", "na", "n/a", "nan", "null", "-", "--"} | {k.lower() for k in NA_SENTINELS}
    width = max(len((r.split(delim) if delim else r.split())) for r in rows)
    names = [columns[i] if i < len(columns) else f"col_{i}" for i in range(width)]

    filled = [0] * width
    for row in rows:
        cells = row.split(delim) if delim else row.split()
        for i, cell in enumerate(cells[:width]):
            if cell.strip().lower() not in blank:
                filled[i] += 1

    per_column = {names[i]: round(filled[i] / len(rows), 3) for i in range(width)}
    measured = [n for n in names if not _NON_MEASUREMENT.match(str(n).strip())]
    measurement_density = (round(sum(per_column[n] for n in measured) / len(measured), 3)
                           if measured else None)
    return {
        "per_column": per_column,
        "empty_columns": [n for n in names if per_column[n] == 0.0],
        "measurement_columns": measured,
        "measurement_density": measurement_density,
        "sampled_rows": len(rows),
    }


def _folder_verdict(folder: str, entry) -> Dict[str, Any]:
    """process / exclude / inspect, with the reason an agent should report."""
    types = entry["dataset_types"]
    excluded = entry["excluded"]
    readable = [f for f in entry["files"] if f["readable"]]

    if not readable:
        return {"action": "exclude", "reason": "no readable data file in this folder "
                                               "(documentation, imagery or archives only)"}
    empty = entry.get("no_data", [])
    if empty and len(empty) == len(readable):
        return {"action": "exclude", "empty_files": empty,
                "reason": f"every readable file ({len(empty)}) has no measurements in its "
                          f"sampled rows -- every measurement column is sentinel or blank "
                          f"while the station and depth columns are full. Count real values "
                          f"over the WHOLE file before excluding: the sample is only the "
                          f"first few KB."}
    if excluded and not types:
        reasons = ", ".join(f"{r} ({n})" for r, n in sorted(excluded.items()))
        harvest = sorted({h for h in entry.get("harvest", []) if h})
        return {"action": "exclude", "reason": f"not WOD profile data: {reasons}",
                # Excluding a folder is not the same as having nothing to take from it.
                "harvest": harvest or None,
                "note": ("This folder is excluded from the profile path but still holds "
                         "metadata for casts that ARE being written. See `harvest`."
                         if harvest else None)}
    if types:
        dominant = max(types, key=types.get)
        mixed = len(types) > 1
        return {
            "action": "process",
            "dataset_types": types,
            "dominant": dominant,
            "reason": (f"{sum(types.values())} file(s) classify as "
                       + ", ".join(f"{n} {t}" for t, n in sorted(types.items()))
                       + (f"; mixed, so classify per file rather than passing "
                          f"dataset_type='{dominant}' for the folder" if mixed else "")),
            "excluded_alongside": excluded or None,
            "empty_files": empty or None,
        }
    return {"action": "inspect",
            "reason": f"{len(readable)} readable file(s) but nothing names an instrument. "
                      f"Read the folder's readme, then classify by hand."}
