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
#
# Every category is SCORED and the strongest wins. They used to be tried in order with the
# first match taking the file, so a fish-catch sheet with a 'Species' column was labelled
# plankton -- which says it belongs in WOD -- because the plankton rule was listed first and
# 'species' is in it. A word both categories use now counts for both, and a file whose only
# evidence is that shared word is reported as ambiguous rather than given either label.
EXCLUSION_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "plankton biomass or taxonomy": {
        "signals": [
            (r"\b(?:zoo|phyto|ichthyo)?plankton\b", 6, "names plankton"),
            (r"\bnorpac\b|\bbongo\b|\bmocness\b|\bring[\s_-]?net\b|\bneuston\b|"
             r"\bnet[\s_-]?tow\b|\bmulti[\s_-]?net\b|\bplankton[\s_-]?net\b", 5,
             "plankton net"),
            (r"displacement\s+volume|settled\s+volume|\bwet\s+weight\b|\bbiomass\b", 4,
             "plankton biomass measure"),
            (r"\bmesh(?:[\s_-]?size)?\b", 3, "net mesh size"),
            (r"\bcopepod(?:ite)?s?\b|\bnaupli(?:i|us)\b|\bfish[\s_-]?larva(?:e)?\b|"
             r"\blarvae\b", 3, "plankton life stage or group"),
            (r"\babundance\b|\bitis\b", 2, "abundance or ITIS taxonomy"),
        ],
        "route": "Belongs in WOD, but through the Biological Header (Table 6a) and "
                 "Taxa-Records (Table 7a) -- a depth RANGE, not a level. Not writable by the "
                 "profile path: reshape it with wod_etl.staging.melt_matrix if it is a count "
                 "matrix, stage each haul with wod_etl.staging.write_staged_haul, and write "
                 "them with wod_etl.orchestration.process_taxa. See "
                 "knowledge/plankton_biomass.md.",
        "harvest": "Capture the Table 6a collection metadata while the accession is open -- "
                   "gear and tow type, mesh size, water volume filtered or tow distance, "
                   "preservative, counting and weighing method. Only the submitter has it, "
                   "and the plankton writer records what is missing.",
        "belongs_in_wod": True,
    },
    "fisheries catch data": {
        "signals": [
            (r"\bcatch(?:es)?\b|\bcpue\b|\bbycatch\b", 6, "names catch"),
            (r"\btrawl(?:s|ing)?\b|\blong[\s_-]?line\b|\bgill[\s_-]?net\b|"
             r"\bpole[\s_-]?(?:and|&)[\s_-]?line\b|\bpurse[\s_-]?seine\b", 5, "fishing gear"),
            (r"\bfork[\s_-]?length\b|\bstandard[\s_-]?length\b|\btotal[\s_-]?length\b|"
             r"length[\s_-]?frequency|\bbody[\s_-]?length\b", 5, "fish length measurement"),
            (r"\bfisher(?:y|ies)\b|\bstomach\b|\bgonad|\botolith", 4, "fisheries biology"),
            (r"\btotal[\s_-]?weight\b|\bweight[\s_-]*\(kg\)", 2, "catch weight"),
        ],
        "route": "Not in WOD Table 3a, and not plankton: fish catch has no route into WOD. "
                 "It belongs with a fisheries archive.",
        "harvest": "Haul position, depth and time only, and only if they identify a cast. A "
                   "trawl table often re-states station positions -- use it to cross-check "
                   "the station list (wod_etl.staging.reconcile_stations).",
        "belongs_in_wod": False,
    },
    "current velocity": {
        "signals": [
            (r"\badcp\b|\bladcp\b|\blowered\s+adcp\b", 6, "names ADCP"),
            (r"\bcurrent(?:s|_speed|_direction)?\b|\bu_vel|\bv_vel|\bvel_(?:east|north)\b",
             4, "velocity component"),
        ],
        "route": "Not in WOD Table 3a. WOD stores no velocity variable; there is no code to "
                 "map it to.",
        "harvest": "The VELOCITIES are dropped; the rest of the file often is not. Bottom depth "
                   "(from bottom-tracking) is secondary header 10, and the navigation -- "
                   "position and time along the track -- can supply or verify coordinates for "
                   "casts taken on the same station. Harvest those into the profiles they "
                   "align with, and cite the file you took them from.",
        "belongs_in_wod": False,
    },
    "genomic / eDNA data": {
        "signals": [(r"\bedna\b|\bmetabarcod|\bamplicon\b|\botu\b|\basv\b|"
                     r"\bsequenc(?:e|ing)\b|\bgenom", 6, "genomic data")],
        "route": "Not in WOD Table 3a.",
        "harvest": "Sample position, depth and time may still identify the cast the sample "
                   "came from. Nothing else here is usable.",
        "belongs_in_wod": False,
    },
    "bathymetry / seafloor depth": {
        "signals": [(r"\bmultibeam\b|\bbathymetr|\bswath\b|\bsounding\b|\bgebco\b|"
                     r"\bsingle[\s_-]?beam\b", 6, "seafloor depth survey")],
        "route": "Bottom depth is secondary header 10 on a cast, not a measured profile of "
                 "its own.",
        "harvest": "This is the clearest harvest case: a sounding at a cast's position IS that "
                   "cast's secondary header 10. Match on position and take it, rather than "
                   "leaving bottom depth empty. Record which file the value came from.",
        "belongs_in_wod": False,
    },
    "contaminant chemistry": {
        "signals": [(r"\bpah\b|\bpcb\b|\bmicroplastic|trace\s+metal|\bcontaminant", 6,
                     "contaminant chemistry")],
        "route": "Not in WOD Table 3a.",
        "harvest": "Sample station, bottle depth and time, where they identify a cast that IS "
                   "being written.",
        "belongs_in_wod": False,
    },
}

# The first line of a haul written by wod_etl.staging.write_staged_haul (HAUL_MARKER there).
STAGED_HAUL_MARKER = "Record type: WOD taxa-records"

# Evidence both biological categories share. It counts toward each, so on its own it decides
# nothing -- which is the point.
SHARED_BIOLOGICAL_SIGNAL = (r"\bspecies\b|\btaxa\b|\btaxon\b|\bgenus\b|\bfamily\b",
                            1, "a taxonomic column -- plankton and fish catch both carry one")
_SHARED_BY = ("plankton biomass or taxonomy", "fisheries catch data")

AMBIGUOUS_BIOLOGICAL = {
    "reason": "biological sample: plankton or fish catch (ambiguous)",
    "route": "Read the readme before deciding. Plankton belongs in WOD through Tables 6a/7a "
             "(wod_etl.orchestration.process_taxa); fish catch does not belong in WOD at all. Nothing in the "
             "path or columns separates them -- a 'Species' column is in both.",
    "harvest": "If it is plankton, capture the Table 6a collection metadata (gear, tow, mesh, "
               "volume filtered, preservative, counting method). If it is catch, haul "
               "positions may cross-check the station list.",
    "belongs_in_wod": None,
}

# A file that only LOCATES casts: station, latitude, longitude, maybe a date -- and no
# measurement. It is not excluded for what it is but for what it is for.
SIDECAR = {
    "reason": "station list or metadata sidecar",
    "route": "Not a cast. It supplies positions and dates for the casts it lists: read it "
             "with wod_etl.staging.read_station_list and join it to the casts with "
             "join_station_list, then write_staged_cast.",
    "harvest": "Position, date and (if present) time for every station it lists. Check them "
               "against any other source in the accession with reconcile_stations.",
    "belongs_in_wod": False,
}

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
    if STAGED_HAUL_MARKER in (header_text or ""):
        # Staged by wod_etl.staging.write_staged_haul: say so, and where it goes.
        cat = EXCLUSION_CATEGORIES["plankton biomass or taxonomy"]
        exclusion = {"reason": "plankton biomass or taxonomy", "route": cat["route"],
                     "harvest": None, "matched_in": "header", "confidence": "high",
                     "matched": "a staged haul (Taxa-Records): write it with "
                                "wod_etl.orchestration.process_taxa",
                     "evidence": ["header marks a staged haul"], "score": None,
                     "alternatives": None, "belongs_in_wod": True, "kind": "staged_haul"}
    long_format = _long_format(columns)
    # A long table carrying its station's coordinates ('Station, Lat, Lon, parm, depth,
    # value') has no mappable measurement column either -- its measurements are in 'value'
    # -- so the sidecar test would file its profile data away as a station list.
    if exclusion is None and long_format is None:
        exclusion = _sidecar(columns)
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
        # Three values, not two. True needs evidence FOR a profile -- a named instrument, or
        # a depth axis to put levels on. None means nothing decided it either way: such a
        # file used to default to True, so a plankton matrix, a trawl log and NCEI's own
        # bounding-box file all passed triage as profile data.
        "is_wod_profile_data": (False if exclusion is not None else
                                True if confidence != "none" or _has_depth_axis(columns)
                                else None),
        "exclusion": exclusion,
        "long_format": long_format,
        "unstorable_columns": unstorable,
        "possible_exclusions": prose_flags,
        "depth_increment_m": depth_increment_m,
    }
    if exclusion is not None:
        result["dataset_type"] = None
        result["evidence"] = [exclusion["matched"]]
    elif long_format is not None:
        result["note"] = long_format["action"]
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


# Files every NCEI accession bundle carries, written by NCEI rather than the submitter. None
# of them is data, and one of them looks like it: `<accession>_lonlat.txt` is two columns of
# numbers -- NCEI's bounding box or position summary -- and sniffs as a table.
NCEI_BUNDLE_FILES = [
    (r"^NCEI-Readme\.txt$", "NCEI bundle readme: explains the accession layout"),
    (r"^journal\.txt$", "NCEI accession journal: what NCEI did, and the submitter's source"),
    (r"^\d{5,7}\.md5$", "NCEI checksums"),
    (r"^\d{5,7}_lonlat\.txt$",
     "NCEI's position summary for the accession (often just a bounding box; NCEI may have "
     "corrected positions in it). Not data, and not a source of cast positions -- at most a "
     "cross-check that every staged cast falls inside it"),
    (r"-ISO-19115-2\.xml$|^\d{5,7}\.\d+\.\d+\.xml$",
     "ISO 19115 metadata record: read it for variables, units, instruments and coverage"),
    (r"_preview\.(?:jpe?g|png|gif)$", "submitter's preview map: read it for station layout"),
    (r"confirmation_email\.txt$|^\d*-?email\.txt$",
     "correspondence with the submitter: read it for anything the data files do not say"),
]


def ncei_bundle_role(name: str) -> Optional[str]:
    for pattern, role in NCEI_BUNDLE_FILES:
        if re.search(pattern, name, re.IGNORECASE):
            return role
    return None


def classify_file(path, rel: Optional[str] = None, peek_bytes: int = 8192) -> Dict[str, Any]:
    """Triage one local file from its head: format, verdict, columns, and an `action`.

    `action` is the per-file decision triage exists to make, and it is the one to act on:

    | action     | means                                                               |
    | ---        | ---                                                                 |
    | `process`  | profile data with a dataset type -- give it to `process_accession`  |
    | `stage`    | profile data in a shape that must be reshaped first (long format)   |
    | `sidecar`  | a station list -- join it INTO the casts it locates                 |
    | `exclude`  | cannot become a profile; `exclusion` says why and where it belongs  |
    | `document` | documentation, metadata or an NCEI bundle file -- read, not processed |
    | `inspect`  | the evidence did not decide; read the readme and say what it is     |

    `rel` is the path relative to the accession root, used as classification evidence;
    it defaults to the file name.
    """
    from wod_etl.discovery.sniff_format import sniff_format

    p = Path(path)
    rel = rel or p.name
    fmt = sniff_format(str(p), num_bytes=peek_bytes)
    head = ""
    if fmt.get("encoding"):
        try:
            head = p.open("r", encoding=fmt["encoding"], errors="replace").read(peek_bytes)
        except OSError:
            head = ""

    columns = _columns_from_head(head, fmt)
    readable = bool(fmt.get("readable", False))
    bundle = ncei_bundle_role(p.name)
    # An unreadable file is not classified. A readme naming an accession's folders scores
    # every instrument in it, and a dataset_type on a file nothing can read is noise that
    # reads like a decision. Neither is an NCEI bundle file, whatever its shape.
    verdict = (classify_dataset(path=rel, columns=columns, header_text=head)
               if readable and not bundle else
               {"dataset_type": None, "confidence": "none",
                "verdict": "ncei_bundle" if bundle else "unreadable",
                "is_wod_profile_data": False, "exclusion": None, "evidence": [],
                "unstorable_columns": [], "long_format": None})
    density = _column_density(head, fmt, columns) if readable and not bundle else None
    long_format = verdict.get("long_format")
    if long_format:
        long_format = {**long_format,
                       "variables_sampled": _sample_column_values(
                           head, fmt, columns, long_format["variable_column"])}

    exclusion = verdict.get("exclusion") or {}
    if bundle:
        action, reason = "document", bundle
    elif not readable and fmt.get("is_document"):
        action, reason = "document", f"{fmt['format_family']}: documentation or metadata"
    elif not readable:
        action, reason = "inspect", (f"{fmt['format_family']}: does not read as any known "
                                     f"table. If it holds data, find out what wrote it.")
    elif exclusion.get("kind") == "sidecar":
        action, reason = "sidecar", exclusion["route"]
    elif exclusion:
        action, reason = "exclude", f"not WOD profile data: {exclusion['reason']}"
    elif long_format:
        action, reason = "stage", long_format["action"]
    elif verdict.get("dataset_type"):
        action, reason = "process", (f"{verdict['dataset_type']} "
                                     f"({verdict['confidence']} confidence)")
    else:
        action, reason = "inspect", ("readable, but nothing names an instrument"
                                     + ("" if _has_depth_axis(columns) else
                                        " and there is no depth axis")
                                     + ". Read the readme, then say what it is.")

    return {
        "path": rel, "format": fmt["format_family"], "reader": fmt["reader"],
        "readable": readable, "action": action, "reason": reason,
        "ncei_bundle": bundle,
        "dataset_type": verdict["dataset_type"], "confidence": verdict["confidence"],
        "is_wod_profile_data": verdict["is_wod_profile_data"],
        "exclusion": exclusion.get("reason"),
        "belongs_in_wod": exclusion.get("belongs_in_wod"),
        "harvest": exclusion.get("harvest"),
        "long_format": long_format,
        "unstorable_columns": [u["column"] for u in verdict.get("unstorable_columns", [])],
        "empty_columns": (density or {}).get("empty_columns"),
        "measurement_density": (density or {}).get("measurement_density"),
        # Kept per file so the folder-level shape check can compare files to each other.
        # One file per depth and one file per variable are invisible in any single file.
        "columns": columns or None,
        "sampled_rows": (density or {}).get("sampled_rows"),
        "evidence": verdict["evidence"][:3],
    }


FILE_ACTIONS = ("process", "stage", "sidecar", "exclude", "document", "inspect")


def classify_accession(root: str, max_files: Optional[int] = None,
                       peek_bytes: int = 8192) -> Dict[str, Any]:
    """Classify every file under an accession root: a verdict per FILE, grouped by folder.

    This is the triage step: what is in this accession, which files hold WOD profile data,
    and what should be excluded and why. It reads each file's head, so it is cheap enough to
    run on a whole accession before processing any of it.

    **The per-file verdict is the answer** -- `files`, and the `<action>_files` lists. NCEI
    archives a submission as one `data/0-data` folder holding CTD casts, station lists,
    plankton sheets and cruise reports together, so a folder-level verdict of 'process'
    describes three files of seventeen. The folder verdicts are still returned, as a summary.
    """
    base = Path(root)
    if not base.exists():
        return {"root": str(root), "error": f"no such directory: {root}", "folders": {},
                "files": []}

    paths = sorted(p for p in base.rglob("*") if p.is_file())
    if max_files:
        paths = paths[:max_files]

    folders: Dict[str, Dict[str, Any]] = {}
    files: List[Dict[str, Any]] = []
    for p in paths:
        rel = p.relative_to(base).as_posix()
        folder = str(Path(rel).parent)
        entry_file = classify_file(p, rel=rel, peek_bytes=peek_bytes)
        files.append(entry_file)

        entry = folders.setdefault(folder, {"files": [], "dataset_types": {},
                                            "excluded": {}, "unreadable": [], "no_data": [],
                                            "harvest": [],
                                            "by_action": {a: [] for a in FILE_ACTIONS}})
        entry["files"].append(entry_file)
        entry["by_action"][entry_file["action"]].append(rel)
        if not entry_file["readable"] or entry_file["ncei_bundle"]:
            entry["unreadable"].append(rel)
        elif entry_file.get("measurement_density") == 0.0:
            entry["no_data"].append(rel)
        if entry_file["exclusion"]:
            reason = entry_file["exclusion"]
            entry["excluded"][reason] = entry["excluded"].get(reason, 0) + 1
            if entry_file.get("harvest"):
                entry["harvest"].append(entry_file["harvest"])
        elif entry_file["dataset_type"]:
            dt = entry_file["dataset_type"]
            entry["dataset_types"][dt] = entry["dataset_types"].get(dt, 0) + 1

    for folder, entry in folders.items():
        entry["by_action"] = {a: v for a, v in entry["by_action"].items() if v}
        entry["verdict"] = _folder_verdict(folder, entry)
        entry["cross_file_shape"] = _cross_file_shape(entry)

    shapes = {f: e["cross_file_shape"] for f, e in folders.items() if e["cross_file_shape"]}
    by_action = {a: [f["path"] for f in files if f["action"] == a] for a in FILE_ACTIONS}
    return {
        "root": str(root),
        "file_count": len(paths),
        "folder_count": len(folders),
        "files": files,
        **{f"{a}_files": v for a, v in by_action.items()},
        "folders": folders,
        "cross_file_shapes": shapes or None,
        "process": [f for f, e in folders.items() if e["verdict"]["action"] == "process"],
        "exclude": [f for f, e in folders.items() if e["verdict"]["action"] == "exclude"],
        "inspect": [f for f, e in folders.items() if e["verdict"]["action"] == "inspect"],
        "stage": [f for f, e in folders.items() if e["verdict"]["action"] == "stage"],
        "note": "Act on the per-file verdicts (`files`, `<action>_files`); the folder "
                "verdicts summarise them. Read each folder's readme before acting -- "
                "'inspect' means the evidence did not decide.",
        "shape_note": ("cross_file_shapes flags folders whose files look like PARTS of one "
                       "cast rather than one cast each, or tables that must be pivoted. "
                       "Processed as they stand, those produce a pile of single-level "
                       "profiles, a set of half-empty ones, or nothing at all. Reshape "
                       "during staging first."
                       if shapes else None),
    }


# ---------------------------------------------------------------- internals
def _search(pattern, text):
    """Match against the text, a separator-flattened copy, and a CamelCase-split copy.

    Paths and column names join words with underscores and hyphens, which are word
    characters to `re`, so `\bglider\b` does not match 'glider_slocum' and `\bctd\b` does
    not match '02-CTD-downcast'. Flattening those to spaces finds it. File names also join
    words by case -- `SHU1505_TrawlInfo.csv`, `U2015-015_2mRingNet.csv`, `PLsamples` -- and
    `\btrawl\b` cannot match inside 'TrawlInfo' at all, so that file passed triage as
    profile data. Splitting on case finds those. All forms are tried because some signals
    are written around the separator ('u_vel', 'sea-bird').
    """
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m
    flattened = re.sub(r"[_\-./\\]+", " ", text)
    m = re.search(pattern, flattened, re.IGNORECASE)
    if m:
        return m
    split = _split_camel(flattened)
    return re.search(pattern, split, re.IGNORECASE) if split != flattened else None


def _split_camel(text: str) -> str:
    """'TrawlInfo' -> 'Trawl Info', '2mRingNet' -> '2m Ring Net', 'PLsamples' -> 'PL samples'.

    Three boundaries: lower-or-digit to upper; an acronym to a capitalised word
    ('CTDCast' -> 'CTD Cast'); and an acronym run straight into a lower-case word of three or
    more letters ('PLsamples'). The last is a guess at intent, which is why the unsplit forms
    are always tried first.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"(?<=[A-Z]{2})(?=[a-z]{3,})", " ", s)
    return s


def _score_exclusions(haystacks) -> Dict[str, Dict[str, Any]]:
    """Per category: the score and evidence from the file's PATH and its COLUMNS, kept apart.

    The two are kept apart because they carry different authority: a path that names
    plankton excludes the file outright, while a column that does only excludes a file with
    no profile measurement beside it.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for reason, cat in EXCLUSION_CATEGORIES.items():
        signals = list(cat["signals"])
        if reason in _SHARED_BY:
            signals.append(SHARED_BIOLOGICAL_SIGNAL)
        entry = {"path": 0.0, "columns": 0.0, "evidence": []}
        for pattern, weight, why in signals:
            for field in ("path", "columns"):
                m = _search(pattern, haystacks[field][0])
                if m:
                    entry[field] += weight
                    entry["evidence"].append(f"{field} matches {m.group(0)!r}: {why}")
                    break                       # one hit per signal, not per field
        if entry["path"] or entry["columns"]:
            out[reason] = entry
    return out


def _find_exclusion(haystacks, columns):
    """(exclusion or None, prose flags).

    An exclusion needs evidence about the file itself: its path, or its own columns. A
    match in prose is recorded as something to check, never as a verdict -- and a match in
    the columns only excludes when the file has no other mappable measurement, so a file
    carrying both a profile and something WOD cannot store keeps its profile.

    Categories are scored, not tried in order: the strongest wins, and a tie between the
    two biological categories is reported as ambiguous rather than broken arbitrarily.
    """
    profile_columns = _mappable_columns(columns)
    prose_flags: List[str] = []
    scores = _score_exclusions(haystacks)

    # A column-only hit on a file that carries a profile is reported, not acted on.
    eligible = {}
    for reason, e in scores.items():
        if e["path"] or not profile_columns:
            eligible[reason] = e
        else:
            prose_flags.append(
                f"{e['evidence'][0]} -- but the file also carries mappable profile columns "
                f"({', '.join(profile_columns[:4])}), so it is processed as a profile "
                f"and the {reason} columns are dropped. Report both.")

    for field in ("header", "context"):
        text = haystacks[field][0]
        for reason, cat in EXCLUSION_CATEGORIES.items():
            for pattern, _w, _why in cat["signals"]:
                m = _search(pattern, text)
                if m:
                    prose_flags.append(
                        f"{field} matches {m.group(0)!r}: {reason} (mentioned in {field} "
                        f"text only, not evidence about this file's own content -- check it "
                        f"during triage)")
                    break

    if not eligible:
        return None, prose_flags

    total = {r: e["path"] + e["columns"] for r, e in eligible.items()}
    ranked = sorted(total.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    tied = [r for r, sc in ranked if sc == best_score]
    entry = eligible[best]
    alternatives = [{"reason": r, "score": sc, "evidence": eligible[r]["evidence"]}
                    for r, sc in ranked[1:]]

    if len(tied) > 1 and set(tied) <= set(_SHARED_BY):
        cat = AMBIGUOUS_BIOLOGICAL
        reason = cat["reason"]
        confidence = "low"
        evidence = sorted({ev for r in tied for ev in eligible[r]["evidence"]})
    else:
        cat = EXCLUSION_CATEGORIES[best]
        reason = best
        evidence = entry["evidence"]
        margin = best_score - (ranked[1][1] if len(ranked) > 1 else 0)
        confidence = "high" if best_score >= 4 and margin >= 2 else "medium"

    return {"reason": reason, "route": cat["route"], "harvest": cat["harvest"],
            "matched_in": "path" if entry["path"] else "columns",
            "confidence": confidence, "matched": evidence[0], "evidence": evidence,
            "score": best_score, "alternatives": alternatives or None,
            "belongs_in_wod": cat["belongs_in_wod"]}, prose_flags


# Table 3a gives latitude, longitude and julian day variable codes. They locate a cast; they
# are not something measured in it, so they never make a file 'carry a profile'.
_LOCATING_CODES = {30, 31, 32}


def _mappable_columns(columns) -> List[str]:
    """Columns that resolve to a Table 3a MEASUREMENT code (not the depth axis or a coordinate).

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
        if code is not None and code != DEPTH_SENTINEL_CODE and code not in _LOCATING_CODES:
            out.append(str(c))
    return out


_STATION_COLUMN = re.compile(r"^(?:station|stn|st|stno|st_?no|cast|site)\b|^station\s+no",
                             re.I)
_LAT_COLUMN = re.compile(r"^lat(?:itude)?\b|latitude", re.I)
_LON_COLUMN = re.compile(r"^lon(?:g(?:itude)?)?\b|longitude", re.I)


def _sidecar(columns) -> Optional[Dict[str, Any]]:
    """A station list: station, latitude and longitude columns, and nothing measured."""
    cols = [c.strip() for c in columns if c and c.strip()]
    if not cols or _mappable_columns(cols):
        return None
    has = {"station": any(_STATION_COLUMN.search(c) for c in cols),
           "latitude": any(_LAT_COLUMN.search(c) for c in cols),
           "longitude": any(_LON_COLUMN.search(c) for c in cols)}
    if not all(has.values()):
        return None
    return {**SIDECAR, "matched_in": "columns", "confidence": "high",
            "matched": f"columns hold station, latitude and longitude and no measurement: "
                       f"{cols[:6]}",
            "evidence": [f"columns {cols[:8]}"], "score": None, "alternatives": None,
            "kind": "sidecar"}


# A long-format table names the variable in a CELL: `StNo, parm, depth, value`. Read as it
# stands, its one measurement column is called 'value' and maps to nothing, so it looks like
# a station list or an empty file rather than what it is.
_LONG_VARIABLE_COLUMN = re.compile(
    r"^(?:parm|param|parameter|parameter_?name|variable|var_?name|analyte|determinand|"
    r"measurement_?type|property|quantity)$", re.I)
_LONG_VALUE_COLUMN = re.compile(
    r"^(?:value|val|result|data_?value|measurement_?value|obs_?value|reading)$", re.I)


def _long_format(columns) -> Optional[Dict[str, Any]]:
    cols = [str(c).strip() for c in columns]
    var = next((c for c in cols if _LONG_VARIABLE_COLUMN.match(c)), None)
    val = next((c for c in cols if _LONG_VALUE_COLUMN.match(c)), None)
    if not var or not val:
        return None
    keys = [c for c in cols if c not in (var, val) and (
        _DEPTH_TOKENS.search(c) or _STATION_COLUMN.search(c)
        or re.search(r"time|date", c, re.I))]
    return {"variable_column": var, "value_column": val, "key_columns": keys,
            "action": f"Long format: the variable is named in the {var!r} column, not in a "
                      f"header. Pivot it with wod_etl.staging.pivot_long(tables, "
                      f"keys={keys!r}, variable={var!r}, value={val!r}), then split it per "
                      f"cast with group_rows and stage each cast."}


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


def _has_depth_axis(columns) -> bool:
    return any(_DEPTH_TOKENS.search(str(c).strip()) for c in columns)


def _confidence(score: float) -> str:
    for threshold, label in CONFIDENCE_THRESHOLDS:
        if score >= threshold:
            return label
    return "none"


def _columns_from_head(head: str, fmt) -> List[str]:
    """Best-effort column names from a file's first bytes, for triage only.

    A row of nothing but delimiters (`,,,,,,` -- a blank spreadsheet row exported above the
    header) is skipped, and quoted fields are split as csv does.
    """
    import csv

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
        if not names and delim and len(delim) == 1 and delim in s:
            try:
                fields = next(csv.reader([s], delimiter=delim))
            except (csv.Error, StopIteration):
                fields = s.split(delim)
            if not any(f.strip() for f in fields):
                continue
            return [c.strip() for c in fields]
        if not names:
            return s.split()
    return names


def _sample_column_values(head: str, fmt, columns, column: str, limit: int = 12) -> List[str]:
    """Distinct values of one column in the sampled rows -- a long table's variable names."""
    import csv

    if column not in (columns or []) or not fmt.get("delimiter") or len(fmt["delimiter"]) != 1:
        return []
    i = columns.index(column)
    seen: List[str] = []
    header_seen = False
    lines = head.splitlines()
    if head and not head.endswith(("\n", "\r")):
        lines = lines[:-1]                 # the peek cut the last line mid-value
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", "*", "%", "//", ";", "!")):
            continue
        try:
            fields = next(csv.reader([s], delimiter=fmt["delimiter"]))
        except (csv.Error, StopIteration):
            continue
        if not header_seen:
            if [f.strip() for f in fields] == list(columns):
                header_seen = True
            continue
        if i < len(fields) and fields[i].strip() and fields[i].strip() not in seen:
            seen.append(fields[i].strip())
            if len(seen) >= limit:
                break
    return seen


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
    readable = [f for f in entry["files"] if f.get("readable") and f.get("columns")
                and f.get("action") not in ("document", "sidecar", "exclude")]

    # Long format first: these files' column NAMES are near-identical by construction, so
    # the two checks below either miss them or call them one file per depth.
    long_files = [f for f in entry["files"] if f.get("long_format")]
    if long_files:
        lf = long_files[0]["long_format"]
        variables = {f["path"]: (f["long_format"].get("variables_sampled") or [])
                     for f in long_files}
        distinct = {tuple(v) for v in variables.values() if v}
        per_variable = len(long_files) > 1 and len(distinct) == len(long_files)
        return {
            "shape": ("long format, one file per variable" if per_variable
                      else "long format"),
            "files": len(long_files),
            "variable_column": lf["variable_column"],
            "value_column": lf["value_column"],
            "key_columns": lf["key_columns"],
            "variables_sampled": variables,
            "evidence": f"{len(long_files)} file(s) name the variable in a "
                        f"{lf['variable_column']!r} column and hold it in "
                        f"{lf['value_column']!r}"
                        + (f"; each file holds a different variable" if per_variable else ""),
            "action": "Pivot with wod_etl.staging.pivot_long -- pass every file together "
                      "as a list of tables, with key_aliases for any key column spelt "
                      "differently between them -- then split the result per cast with "
                      "group_rows, join positions with join_station_list, and stage each "
                      "cast. Check info['conflicts'] and info['levels_missing_variable'].",
        }
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
    readable = [f for f in entry["files"] if f["readable"] and not f.get("ncei_bundle")]

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
    by_action = entry.get("by_action") or {}
    # Long-format files often name no instrument, so they add nothing to `types`; without
    # this check one trawl sheet beside them turned the whole folder into 'exclude'.
    if excluded and not types and not by_action.get("stage"):
        reasons = ", ".join(f"{r} ({n})" for r, n in sorted(excluded.items()))
        harvest = sorted({h for h in entry.get("harvest", []) if h})
        return {"action": "exclude", "reason": f"not WOD profile data: {reasons}",
                # Excluding a folder is not the same as having nothing to take from it.
                "harvest": harvest or None,
                "note": ("This folder is excluded from the profile path but still holds "
                         "metadata for casts that ARE being written. See `harvest`."
                         if harvest else None)}
    if types and (by_action.get("process") or "by_action" not in entry):
        dominant = max(types, key=types.get)
        mixed = len(types) > 1
        others = {a: len(v) for a, v in by_action.items() if a != "process"}
        return {
            "action": "process",
            "dataset_types": types,
            "dominant": dominant,
            "reason": (f"{sum(types.values())} file(s) classify as "
                       + ", ".join(f"{n} {t}" for t, n in sorted(types.items()))
                       + (f"; mixed, so classify per file rather than passing "
                          f"dataset_type='{dominant}' for the folder" if mixed else "")
                       + (f". The folder ALSO holds " + ", ".join(
                           f"{n} to {a}" for a, n in sorted(others.items()))
                          + " -- act on the per-file verdicts, not this summary"
                          if others else "")),
            "excluded_alongside": excluded or None,
            "empty_files": empty or None,
            "files_by_action": by_action or None,
        }
    staged = by_action.get("stage")
    if staged:
        others = {a: len(v) for a, v in by_action.items() if a != "stage"}
        return {"action": "stage", "files": staged, "files_by_action": by_action,
                "reason": f"{len(staged)} file(s) hold profile data in a shape that must be "
                          f"reshaped before processing (long format). See cross_file_shape."
                          + (f" The folder ALSO holds " + ", ".join(
                              f"{n} to {a}" for a, n in sorted(others.items()))
                             + " -- act on the per-file verdicts." if others else "")}
    return {"action": "inspect",
            "reason": f"{len(readable)} readable file(s) but nothing names an instrument. "
                      f"Read the folder's readme, then classify by hand."}
