"""Decide which WOD dataset a file belongs to, or that it is not WOD profile data.

Returns a ranked verdict with evidence. An `exclusion` means the file cannot become a profile;
`dataset_type: None` means nothing identifies the instrument and `candidates` lists the options.
"""
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl.terms import english_terms
from wod_etl import runlog
from wod_etl.config import HIGH_RES_DEPTH_INCREMENT_M
from wod_etl.discovery.read_any import SAMPLE_COLUMN, SURFACE_WORDS, honour_fill

log = logging.getLogger(__name__)

# --------------------------------------------------------------- signals
# Each entry is (regex, weight, what the match tells you). Weights are compared only within
# one file: an instrument named in a header beats a folder name, which beats column shape.
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
        # A sound velocity and temperature probe (SV&T) is a digital BT (manual p.19); any
        # salinity beside it is computed from sound speed and not stored.
        (r"\bsvp?\s?&\s?t\b|\bsound[\s_-]?(?:velocity|speed)[\s_-]?(?:and|&)[\s_-]?"
         r"temperature\b", 6, "a sound velocity and temperature probe: a digital BT"),
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
        # Not plain "SVP": in a hydrographic survey that is a sound velocity profile.
        (r"\bsvp[\s_-]?(?:drifter|buoy)s?\b|\bdrb\b", 4, "drifter programme or names DRB"),
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

# Content that cannot become a WOD profile. `route` says where it belongs, if anywhere;
# `harvest` says what metadata to take from it first.
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
        "route": "Belongs in WOD as a Biological Header (Table 6a) and Taxa-Records "
                 "(Table 7a), not a profile. `wod-etl confirm` writes tables it can locate; "
                 "anything else is staged with wod_etl.staging.write_staged_haul and written "
                 "with wod_etl.orchestration.process_taxa. See "
                 "knowledge/plankton_biomass.md.",
        "harvest": "Capture the Table 6a collection metadata while the accession is open: "
                   "gear and tow type, mesh size, water volume filtered or tow distance, "
                   "preservative, counting and weighing method. Only the submitter has it, "
                   "and the plankton writer records what is missing.",
        "belongs_in_wod": True,
        "label": "plankton: counts or biomass from net samples",
        "why": "plankton belongs in WOD as Taxa-Records (Tables 6a/7a), not as a profile",
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
                   "trawl table often re-states station positions; use it to cross-check "
                   "the station list (wod_etl.staging.reconcile_stations).",
        "belongs_in_wod": False,
        "label": "fish catch",
        "why": "fish catch is not WOD data (no Table 3a variable); it belongs in a fisheries "
               "archive",
    },
    "current velocity": {
        "signals": [
            (r"\badcp\b|\bladcp\b|\blowered\s+adcp\b", 6, "names ADCP"),
            (r"\bcurrent(?:s|_speed|_direction)?\b|\bu_vel|\bv_vel|\bvel_(?:east|north)\b|"
             r"\b(?:east|north|vertical|error)(?:ward)?[\s_-]+velocity\b",
             4, "velocity component"),
        ],
        "route": "Not in WOD Table 3a. WOD stores no velocity variable; there is no code to "
                 "map it to.",
        "harvest": "The velocities are dropped; the rest of the file often is not. Bottom depth "
                   "(from bottom-tracking) is secondary header 10, and the navigation "
                   "(position and time along the track) can supply or verify coordinates for "
                   "casts taken on the same station. Use them in the profiles they match, and "
                   "record which file they came from.",
        "belongs_in_wod": False,
        "label": "current velocity (ADCP)",
        "why": "WOD stores no current velocity: Table 3a has no variable code for it",
    },
    "genomic / eDNA data": {
        "signals": [(r"\bedna\b|\bmetabarcod|\bamplicon\b|\botu\b|\basv\b|"
                     r"\bsequenc(?:e|ing)\b|\bgenom", 6, "genomic data")],
        "route": "Not in WOD Table 3a.",
        "harvest": "Sample position, depth and time may still identify the cast the sample "
                   "came from. Nothing else here is usable.",
        "belongs_in_wod": False,
        "label": "genomic / eDNA data",
        "why": "WOD stores no genomic data (not in Table 3a)",
    },
    "bathymetry / seafloor depth": {
        "signals": [(r"\bmultibeam\b|\bbathymetr|\bswath\b|\bsounding\b|\bgebco\b|"
                     r"\bsingle[\s_-]?beam\b", 6, "seafloor depth survey")],
        "route": "Bottom depth is secondary header 10 on a cast, not a measured profile of "
                 "its own.",
        "harvest": "A sounding at a cast's position is that cast's bottom depth (secondary "
                   "header 10). Match on position and take it rather than leaving bottom depth "
                   "empty. Record which file the value came from.",
        "belongs_in_wod": False,
        "label": "bathymetry / seafloor depth",
        "why": "not a water-column profile: WOD keeps bottom depth only as a cast's header "
               "value (secondary header 10)",
    },
    "contaminant chemistry": {
        "signals": [(r"\bpah\b|\bpcb\b|\bmicroplastic|trace\s+metal|\bcontaminant", 6,
                     "contaminant chemistry")],
        "route": "Not in WOD Table 3a.",
        "harvest": "Sample station, bottle depth and time, where they identify a cast that is "
                   "being written.",
        "belongs_in_wod": False,
        "label": "contaminant chemistry",
        "why": "WOD stores no contaminant measurements (not in Table 3a)",
    },
    "seabed sediment": {
        "signals": [(r"\bgrain[\s_-]?size|\bsediment[\s_-]?(?:size|grain|core|sample|type)s?\b|"
                     r"\bseabed\b|\bsea[\s_-]?floor[\s_-]?sediment|\bgrab[\s_-]?sample|\bd50\b",
                     6, "seabed sediment samples")],
        "route": "Not in WOD Table 3a: WOD holds the water column, not the seabed.",
        "harvest": "Sample positions and times only, where they locate a cast that is being "
                   "written.",
        "belongs_in_wod": False,
        "label": "seabed sediment samples",
        "why": "not water-column data: WOD stores no seabed or sediment property (not in "
               "Table 3a)",
    },
}

# The first line of a haul written by wod_etl.staging.write_staged_haul (HAUL_MARKER there).
STAGED_HAUL_MARKER = "Record type: WOD taxa-records"

# Evidence both biological categories share: it counts toward each, so alone decides nothing.
SHARED_BIOLOGICAL_SIGNAL = (r"\bspecies\b|\btaxa\b|\btaxon\b|\bgenus\b|\bfamily\b",
                            1, "a taxonomic column, which plankton and fish catch both carry")
_SHARED_BY = ("plankton biomass or taxonomy", "fisheries catch data")

AMBIGUOUS_BIOLOGICAL = {
    "reason": "biological sample: plankton or fish catch (ambiguous)",
    "route": "Plankton belongs in WOD through Tables 6a/7a "
             "(wod_etl.orchestration.process_taxa); fish catch does not belong in WOD at all. "
             "Nothing in the path or columns separates them (both have a 'Species' column); "
             "the readme may.",
    "harvest": "If it is plankton, capture the Table 6a collection metadata (gear, tow, mesh, "
               "volume filtered, preservative, counting method). If it is catch, haul "
               "positions may cross-check the station list.",
    "belongs_in_wod": None,
    "label": "biological samples: plankton or fish catch",
    "why": "could be plankton or fish catch (not WOD data); left out until someone says "
           "which",
}

# A file that only locates casts (station, position, maybe a date) and measures nothing.
SIDECAR = {
    "reason": "station list or metadata sidecar",
    "route": "Not a cast. It supplies positions and dates for the casts it lists: read it "
             "with wod_etl.staging.read_station_list and join it to the casts with "
             "join_station_list, then write_staged_cast.",
    "harvest": "Position, date and (if present) time for every station it lists. Check them "
               "against any other source in the accession with reconcile_stations.",
    "belongs_in_wod": False,
    "label": "station list: positions and dates of stations",
    "why": "it measures nothing: it locates casts, and is read by the reshaping recipe that "
           "needs it rather than written itself",
}

# One instrument at one place recording for days. An MRB profile is the concurrent readings
# of a mooring's instruments (manual p.17), so such a file is one level of many profiles.
# Decided by the file's shape, not its words.
MOORED_SERIES = "moored instrument time series"
# Exclusion for a mooring file the recipe cannot stage (no MRB variable, or several depths).
MOORED = {
    "route": "Belongs in WOD as MRB (moored buoy), where a profile is the concurrent "
             "readings of a mooring's instruments at their depths at one time (manual p.17), "
             "but this file cannot become one of its levels.",
    "belongs_in_wod": True,
    "why": "a mooring's record that cannot become an MRB level",
}
# The same shape on a moving platform with no depth axis (keel CTD, thermosalinograph).
# In WOD each position along the track is its own SUR cast.
UNDERWAY_SERIES = "underway record along a track"
UNDERWAY = {
    "route": "Belongs in WOD as SUR (surface-only), one cast per position and time along the "
             "track. wod-etl does not split a track into casts yet: these files are not "
             "written.",
    "belongs_in_wod": True,
    "why": "a surface record along a path belongs in WOD as SUR, one cast per position and "
           "time; wod-etl cannot split a track into casts yet",
}
MIN_SERIES_DAYS = 1.0             # a CTD cast recorded against time spans minutes
MAX_POSITION_SPREAD_DEG = 0.01    # more than this along time and the platform moves

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
    instrument: str = "",
    vertical_axis: Optional[bool] = None,
) -> Dict[str, Any]:
    """Identify a file's WOD dataset from whatever evidence it carries.

    `path` is relative to the accession root, as a folder name ("2-CTD/") is often the only label.
    `instrument` is what a self-describing file says recorded it; it weighs like the path.
    """
    columns = [english_terms(str(c)) for c in (columns or [])]
    haystacks = {
        "path": (str(path or "").replace("\\", "/"), 1.0),
        "instrument": (instrument or "", 1.0),
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
                     "alternatives": None, "belongs_in_wod": True, "kind": "staged_haul",
                     "label": cat["label"], "why": cat["why"]}
    long_format = _long_format(columns)
    # A long table ('Station, Lat, Lon, parm, depth, value') would otherwise pass as a
    # station list, since its measurements are in 'value'.
    if exclusion is None and long_format is None:
        exclusion = _sidecar(columns)
    unstorable = _unstorable_columns(columns)
    scores, evidence = _score_instruments(haystacks)
    shape = _shape_evidence(columns, depth_increment_m, n_levels, vertical_axis)
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
        # True needs evidence for a profile (a named instrument or a depth axis); None
        # means nothing decided it.
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
            "Nothing in the path, header or columns names an instrument: give the dataset "
            "explicitly (the folder's readme may name it). A bottle-like file (coarse "
            "levels, chemistry columns) is usually OSD.")
    elif best in ("CTD", "XCTD") and depth_increment_m is None:
        result["note"] = (
            "CTD and XCTD route to CTD or OSD by the cast's depth increment, not by "
            "instrument (manual p.19). Pass depth_increment_m to settle it; without one "
            "the final dataset is decided later, per cast.")
    return result


# Files NCEI adds to every accession bundle. None is data, though `<accession>_lonlat.txt`
# sniffs as a table. Each is (name pattern, short label, what it is for).
NCEI_BUNDLE_FILES = [
    (r"^NCEI-Readme\.txt$", "NCEI readme", "NCEI bundle readme: explains the accession layout"),
    (r"^journal\.txt$", "NCEI accession journal",
     "NCEI accession journal: what NCEI did, and the submitter's source"),
    (r"^\d{5,7}\.md5$", "NCEI checksums", "NCEI checksums"),
    (r"^\d{5,7}_lonlat\.txt$", "NCEI position summary",
     "NCEI's position summary for the accession (often just a bounding box; NCEI may have "
     "corrected positions in it). Not data, and not a source of cast positions: at most a "
     "cross-check that every staged cast falls inside it"),
    (r"-ISO-19115-2\.xml$|^\d{5,7}\.\d+\.\d+\.xml$", "ISO 19115 metadata",
     "ISO 19115 metadata record: read it for variables, units, instruments and coverage"),
    (r"_preview\.(?:jpe?g|png|gif)$", "submitter's preview map",
     "submitter's preview map: read it for station layout"),
    (r"^\d{5,7}_map\.(?:jpe?g|png|gif)$", "NCEI map of the sample locations",
     "NCEI's map of the accession's sample locations"),
    (r"confirmation_email\.txt$|^\d*-?email\.txt$", "correspondence with the submitter",
     "correspondence with the submitter: read it for anything the data files do not say"),
    # Send2NCEI names a submission with six capitals and digits.
    (r"(?-i:^[A-Z0-9]{6}-report\.txt$)", "NCEI submission report",
     "NCEI submission report: the submitter's contacts and the metadata they entered"),
]


def _bundle_entry(name: str) -> Optional[tuple]:
    for entry in NCEI_BUNDLE_FILES:
        if re.search(entry[0], name, re.IGNORECASE):
            return entry
    return None


def ncei_bundle_role(name: str) -> Optional[str]:
    entry = _bundle_entry(name)
    return entry[2] if entry else None


def classify_file(path, rel: Optional[str] = None, peek_bytes: int = 8192) -> Dict[str, Any]:
    """Triage one local file from its head: format, verdict, columns, and an `action`.

    `action` is `process` (profile data with a dataset type), `stage` (profile data to reshape or
    combine first), `sidecar` (a station list for the casts it locates), `exclude` (cannot become
    a profile; see `exclusion`), `document` (documentation, metadata or an NCEI bundle file) or
    `inspect` (the evidence did not decide). `rel` is the path relative to the accession root,
    used as evidence; it defaults to the file name.
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
    if readable and fmt.get("reader") in _OWN_READERS:
        # A format with its own reader names its columns there, not in a header row.
        from wod_etl.discovery.read_any import read_any

        casts, _ = read_any(str(p), fmt, max_casts=1)
        if casts:
            columns = [str(c) for c in casts[0]["df"].columns]
            head = casts[0]["header_block"] + "\n" + head
    bundle = _bundle_entry(p.name)
    nc = _netcdf_evidence(p) if readable and not bundle and fmt.get("reader") == "netcdf" \
        else None
    if nc:
        columns = nc["columns"]
    # Unreadable files and NCEI bundle files are not classified: a dataset_type on them
    # would read like a decision.
    increment = (nc or {}).get("depth_increment") if nc else \
        _text_depth_increment(head, columns) if readable and not bundle else None
    verdict = (classify_dataset(path=rel, columns=columns,
                                header_text=nc["header"] if nc else head,
                                instrument=(nc or {}).get("instrument", ""),
                                depth_increment_m=increment,
                                vertical_axis=(nc or {}).get("vertical"))
               if readable and not bundle else
               {"dataset_type": None, "confidence": "none",
                "verdict": "ncei_bundle" if bundle else "unreadable",
                "is_wod_profile_data": False, "exclusion": None, "evidence": [],
                "unstorable_columns": [], "long_format": None})
    series = (nc or {}).get("time_series")
    mooring = None
    if series and not verdict.get("exclusion"):
        what = (f"{'along a track' if series['moving'] else 'one instrument at one place'}"
                + (f" ({series['instrument']})" if series.get("instrument") else "")
                + f": {series['samples']} samples over {series['days']} days")
        # One instrument at one place: a level of the mooring's MRB profiles, if it can be.
        info = None if series["moving"] else _instrument(p)
        if info is not None and not info.get("error"):
            # The model, not the serial number: alike instruments share one decision.
            from wod_etl.staging.moorings import in_water

            water = in_water(p, info) if info.get("pressure") else {}
            mooring = {"platform": series.get("platform"),
                       "instrument": series.get("instrument") or info.get("model"),
                       "serial": info.get("serial"),
                       "days": series["days"], "samples": series["samples"],
                       "variables": [v["column"] for v in info["variables"]],
                       "pressure": bool(info.get("pressure")), "depth": info.get("depth"),
                       "depth_source": info.get("depth_source"),
                       "latitude": info.get("latitude"), "longitude": info.get("longitude"),
                       # For the deployment, and the depth of instruments without pressure.
                       "wet": water.get("window"), "usual_pressure": water.get("pressure"),
                       "record": info.get("record")}
            verdict = {**verdict, "dataset_type": "MRB", "is_wod_profile_data": True,
                       "confidence": "high",
                       "evidence": [what] + list(verdict.get("evidence") or [])}
        else:
            kind, cat = ((UNDERWAY_SERIES, UNDERWAY) if series["moving"] else
                         (MOORED_SERIES, {**MOORED, "why": f"a mooring's record, but "
                                                          f"{info['error']}"}))
            verdict = {**verdict, "is_wod_profile_data": False, "exclusion": {
                **cat, "reason": kind, "harvest": None, "matched_in": "shape",
                "confidence": "high", "matched": what, "evidence": [what]}}
    density = (_column_density(head, fmt, columns) if readable and not bundle and not nc
               else None)
    long_format = verdict.get("long_format")
    if long_format:
        long_format = {**long_format,
                       "variables_sampled": _sample_column_values(
                           head, fmt, columns, long_format["variable_column"])}

    exclusion = verdict.get("exclusion") or {}
    unstorable = verdict.get("unstorable_columns") or []
    if (readable and not bundle and not exclusion and not mooring and not long_format
            and verdict.get("dataset_type") and unstorable
            and not _variable_names(columns) and not _variable_names((nc or {}).get("names") or [])):
        held = _and(sorted({str(u.get("variable", "")).replace("_", " ") for u in unstorable}))
        exclusion = {"reason": "nothing WOD stores", "belongs_in_wod": False, "harvest": None,
                     "label": f"{held} only", "kind": "nothing_stored",
                     "why": f"it holds only {held}, which WOD does not store (no Table 3a "
                            f"code)"}
    if bundle:
        action, reason = "document", bundle[2]
    elif exclusion.get("kind") == "sidecar":
        action, reason = "sidecar", exclusion["route"]
    elif _DOCUMENT_NAME.search(p.name) and fmt.get("reader") != "netcdf":
        action, reason = "document", ("named as documentation (a readme, description or "
                                      "notes); read it, it is not processed")
    elif not readable and fmt.get("is_document"):
        action, reason = "document", f"{fmt['format_family']}: documentation or metadata"
    elif fmt.get("format_family") == "unstructured_text" and _prose_throughout(p, fmt,
                                                                               peek_bytes):
        action, reason = "document", ("prose all the way through: a readme, notes or a "
                                      "description. Read it; it is not processed.")
    elif not readable:
        action, reason = "inspect", (f"{fmt['format_family']}: does not read as any known "
                                     f"table. If it holds data, find out what wrote it.")
    elif exclusion.get("reason") in (MOORED_SERIES, UNDERWAY_SERIES):
        action, reason = "exclude", (f"{exclusion['reason']}, {exclusion['matched']}: "
                                     + ("one level of many MRB profiles, not a profile"
                                        if exclusion["reason"] == MOORED_SERIES else
                                        "a track of surface casts, not a profile"))
    elif exclusion:
        action, reason = "exclude", f"not WOD profile data: {exclusion['reason']}"
    elif mooring:
        action, reason = "stage", (f"{MOORED_SERIES}: one level of the mooring's MRB "
                                   f"profiles, one a day, built with its other instruments by "
                                   f"a mooring recipe")
    elif long_format:
        action, reason = "stage", long_format["action"]
    elif verdict.get("dataset_type"):
        action, reason = "process", (f"{verdict['dataset_type']} "
                                     f"({verdict['confidence']} confidence)")
    else:
        action, reason = "inspect", ("readable, but nothing names an instrument"
                                     + ("" if _has_depth_axis(columns) else
                                        " and there is no depth axis")
                                     + ". The readme may say what it is.")

    entry = {
        "path": rel, "format": fmt["format_family"], "reader": fmt["reader"],
        "readable": readable, "action": action, "reason": reason,
        "ncei_bundle": bundle[2] if bundle else None,
        "dataset_type": verdict["dataset_type"], "confidence": verdict["confidence"],
        "is_wod_profile_data": verdict["is_wod_profile_data"],
        "exclusion": exclusion.get("reason"),
        "belongs_in_wod": exclusion.get("belongs_in_wod"),
        "harvest": exclusion.get("harvest"),
        "long_format": long_format,
        "unstorable_columns": [u["column"] for u in verdict.get("unstorable_columns", [])],
        "empty_columns": (density or {}).get("empty_columns"),
        "measurement_density": (density or {}).get("measurement_density"),
        # Kept per file for the folder-level shape check, which compares files.
        "columns": columns or None,
        "sampled_rows": (density or {}).get("sampled_rows"),
        "time_series": series,
        "mooring": mooring,
        "copies": (nc or {}).get("copies") or None,
        "evidence": verdict["evidence"][:3],
    }
    entry.update(_summary(entry, verdict, exclusion, fmt, bundle,
                          instrument=(nc or {}).get("instrument", "")))
    empty = _all_measurements_empty(columns, density)
    if action in ("process", "inspect") and empty:
        none = (f"no value in any of {_and(empty)} in the first {density['sampled_rows']} "
                f"rows (e.g. 'Not analyzed'): nothing to write")
        entry.update(include=False,
                     why=f"{entry['why']}; {none}" if action == "process" else none)
    if action == "process" and entry["include"] and fmt.get("reader") in (
            "delimited", "fixed_width", "spreadsheet") and not _locates_itself(columns, head):
        entry.update(unlocated=entry["why"], include=False,
                     why=f"{entry['why']}; nothing in it gives a position (no latitude and "
                         f"longitude column or header line): join one in with a staging script")
    # Depthless samples are written at 0 m only as single surface samples: one row per sample,
    # named as underway or surface.
    if action == "process" and entry["include"] and entry.get("dataset") == "OSD" \
            and columns and not _has_depth_axis(columns) \
            and not (SURFACE_WORDS.search(f"{rel}\n{head[:4000]}")
                     and any(SAMPLE_COLUMN.match(str(c)) for c in columns)):
        entry.update(include=False,
                     why=f"{entry['why']}; no depth, and nothing says these are single surface "
                         f"samples: give each sample's depth with a staging script")
    return entry


def wod_dataset(dataset_type: Optional[str]) -> Optional[str]:
    """The WOD dataset (Table 2) a classification is written as.

    XCTD is an instrument, not a dataset: it is written as CTD, or OSD when coarse (manual p.19).
    """
    from wod_etl.reference import wod_datasets

    if not dataset_type:
        return None
    return dataset_type if dataset_type in wod_datasets() else {"XCTD": "CTD"}.get(dataset_type)


_SV_PROBE = re.compile(r"\bsvp?\s?&\s?t\b|\bsound[\s_-]?(?:velocity|speed)[\s_-]?(?:and|&)"
                       r"[\s_-]?temperature\b", re.IGNORECASE)


def _summary(entry, verdict, exclusion, fmt, bundle, instrument: str = "") -> Dict[str, Any]:
    """One-line `data`, `include` and `why` for review; `reason` keeps the long form for the log."""
    action, columns = entry["action"], entry["columns"] or []
    out = {"include": action in ("process", "stage"), "dataset": None}
    if action == "document":
        named = _DOCUMENT_NAME.search(entry["path"].rsplit("/", 1)[-1])
        return {**out, "data": bundle[1] if bundle else
                "text: a readme, notes or a description" if named else _DOCUMENT_LABELS.get(
                    fmt.get("format_family"), f"{fmt.get('format_family')} document"),
                "why": "documentation, not data: read it, nothing to process"}
    if action == "process":
        dataset = wod_dataset(verdict["dataset_type"])
        copies = entry.get("copies") or []
        # An SV&T probe's salinity is computed from sound speed: propose dropping it, so it
        # stays out whatever dataset the file is written as.
        if _SV_PROBE.search(instrument or "") and "Salinity" in _variable_names(columns):
            copies = copies + [{"copy": "Salinity", "of": "sound speed",
                                "computed": True}]
        copied = {c["copy"] for c in copies}
        unstorable = {str(u.get("variable", "")).replace("_", " ")
                      for u in verdict.get("unstorable_columns") or [] if u.get("variable")}
        names = _variable_names(columns)
        writable = _variable_names(columns, dataset, writable=True)
        refused = [n for n in _variable_names(columns, dataset) if n not in writable]
        stored = [n for n in writable if n not in copied]
        others = [n for n in names if n not in stored and n not in copied and n not in refused]
        sources = [_evidence_source(e) for e in verdict.get("evidence") or []][:2]
        # What decided the dataset, then what is not written and why. A file with nothing
        # to write is proposed out.
        notes = ([] if stored else [f"no column maps to a variable {dataset or 'WOD'} stores: "
                                    f"nothing to write"])
        if others:
            notes.append(f"not written: {_and(n.lower() for n in others)} "
                         f"(not stored in {dataset})")
        if unstorable:
            notes.append(f"not written: {_and(sorted(unstorable))} (no WOD code)")
        if refused:
            notes.append(f"not written: {_and(n.lower() for n in refused)} (a molar unit; "
                         f"WOD stores it by mass and the manual gives no conversion)")
        notes += [f"its {c['copy'].lower()} is computed from {c['of']} by a probe with no "
                  f"conductivity cell: left out (drop)" if c.get("computed") else
                  f"its {c['copy'].lower()} equals its {c['of'].lower()} at every level "
                  f"(a copy, not a measurement): left out (drop)" for c in copies]
        return {**out, "dataset": dataset, "include": bool(stored),
                "drop": sorted(c.lower() for c in copied) or None,
                "data": f"{dataset} profiles: " + (", ".join(n.lower() for n in stored)
                                                   or "no variable recognised"),
                "why": (f"{verdict['dataset_type']} by {' and '.join(sources)}"
                        if sources else str(verdict["dataset_type"]))
                       + "".join(f"; {n}" for n in notes)
                       + ("; a weak guess: check the dataset"
                          if verdict.get("confidence") == "low" else "")}
    if action == "stage" and entry.get("mooring"):
        m = entry["mooring"]
        names = ", ".join(n.lower() for n in _variable_names(m["variables"], "MRB"))
        return {**out, "dataset": "MRB",
                "data": f"moored instrument record: {m.get('instrument') or 'one instrument'} "
                        f"at one place, {m['days']} days: {names or 'nothing WOD stores'}",
                "why": MOORING_WHY}
    if action == "stage":
        # Variables are not named here: they differ file by file, and the recipe lists them.
        return {**out, "dataset": wod_dataset(verdict["dataset_type"]),
                "data": f"profiles in long format: one row per station, depth and "
                        f"{entry['long_format']['variable_column']!r}",
                "why": "measurements named in a column rather than a header row: they are "
                       "reshaped into one cast per station before processing"}
    if action in ("sidecar", "exclude"):
        series = entry.get("time_series") or {}
        if exclusion.get("reason") == MOORED_SERIES:
            data = (f"moored instrument record: {series.get('instrument') or 'one instrument'}"
                    f" at one place, {series.get('days')} days")
        elif exclusion.get("reason") == UNDERWAY_SERIES:
            data = (f"underway record along a track: "
                    f"{', '.join(n.lower() for n in _variable_names(columns)) or 'surface data'}"
                    f", {series.get('days')} days")
        else:
            data = exclusion.get("label") or exclusion.get("reason")
        return {**out, "data": data, "why": exclusion.get("why") or exclusion.get("route")}
    cols = [c for c in columns if c][:5]
    return {**out, "data": f"unidentified {str(fmt.get('format_family')).replace('_', ' ')}"
                           + (f": columns {', '.join(cols)}" if cols else ""),
            "why": (("nothing names an instrument" + ("" if _has_depth_axis(columns) else
                                                      " and there is no depth axis"))
                     if entry["readable"] else "nothing here can read it")
                   + ": left out until someone says what it is"}


MOORING_WHY = ("a mooring's instrument: with its others it makes WOD MRB profiles, one a day "
               "of daily means (manual pp.14, 17)")


def _place_moorings(base: Path, files: List[Dict[str, Any]]) -> None:
    """Place and propose a mooring's instruments together, since each depends on the others.

    The deployment comes from the instruments' pressure; one without pressure is placed by its file,
    file name or the mooring's description. Each `mooring` gets `group`, `deployed`, `deployment`,
    `depth`, `placed_by` and `depth_note`; one that cannot be placed is proposed left out.
    """
    from wod_etl.staging.mooring_layout import depth_from_name, find_layout, place_instruments
    from wod_etl.staging.moorings import deployment, group_moorings
    from wod_etl.staging.parse import pressure_to_depth

    members = [f for f in files if f["action"] == "stage" and f.get("mooring")]
    # A description is a small file that is not data: never a cast or a table to stage.
    texts = [f["path"] for f in files if not f.get("mooring")
             and f["action"] not in ("process", "stage")
             and (base / f["path"]).is_file()
             and (base / f["path"]).stat().st_size <= 2_000_000]
    for label, group in group_moorings(members):
        found = deployment([m["mooring"].get("wet") for m in group])
        records = [m["mooring"]["record"] for m in group if m["mooring"].get("record")]
        if found["how"] == "none" and records:
            # No deployment found: use the record minus its first and last day (maybe on deck).
            first = math.floor(min(r[0] for r in records) / 86400.0) + 1
            last = math.floor(max(r[1] for r in records) / 86400.0)
            if last > first:
                found = {"window": [first * 86400.0, last * 86400.0 - 1.0], "how": "record"}
        known: Dict[str, Dict[str, Any]] = {}
        for m in group:
            info, name = m["mooring"], m["path"].rsplit("/", 1)[-1]
            lat = info.get("latitude") if info.get("latitude") is not None else 45.0
            if info.get("usual_pressure"):
                depth = round(pressure_to_depth(info["usual_pressure"], lat), 1)
                known[m["path"]] = {"depth": depth, "placed_by": "pressure",
                                    "depth_note": f"by its pressure (usually "
                                                  f"{info['usual_pressure']:.1f} dbar, "
                                                  f"{depth:.1f} m)",
                                    "depth_short": "its pressure, day by day (usual depth)"}
            elif info.get("depth") is not None:
                known[m["path"]] = {"depth": float(info["depth"]), "placed_by": "file",
                                    "depth_note": f"{info['depth']:g} m, from "
                                                  f"{info.get('depth_source') or 'its file'}",
                                    "depth_short": f"from {info.get('depth_source') or 'its file'}"}
            elif depth_from_name(name) is not None:
                known[m["path"]] = {"depth": depth_from_name(name), "placed_by": "name",
                                    "depth_note": f"{depth_from_name(name):g} m, from its "
                                                  f"file name",
                                    "depth_short": "from its file name"}
        unplaced: Dict[str, str] = {}
        wanted = [m for m in group if m["path"] not in known]
        if wanted:
            folder = group[0]["path"].rsplit("/", 1)[0] if "/" in group[0]["path"] else ""
            layout = None
            # The description beside the files first, then in each folder above them.
            while layout is None:
                here = [t for t in texts if (t.rsplit("/", 1)[0] if "/" in t else "") == folder]
                layout = find_layout([(t.rsplit("/", 1)[-1], base / t) for t in here],
                                     [m["mooring"] for m in group])
                if not folder:
                    break
                folder = folder.rsplit("/", 1)[0] if "/" in folder else ""
            if layout is not None:
                res = place_instruments(layout, [
                    {"name": m["path"], "label": m["path"].rsplit("/", 1)[-1],
                     "serial": m["mooring"].get("serial"),
                     "depth": (known.get(m["path"]) or {}).get("depth"),
                     "by_pressure": (known.get(m["path"]) or {}).get("placed_by") == "pressure"}
                    for m in group])
                for path, p in res["placed"].items():
                    known[path] = {"depth": p["depth"], "placed_by": f"description:{p['how']}",
                                   "depth_note": f"{p['depth']:.1f} m: {p['detail']}",
                                   "depth_short": p["short"], "description": layout["file"]}
                unplaced.update(res["unplaced"])
        for m in group:
            info = m["mooring"]
            info.update({"group": label, "deployed": found["window"],
                         "deployment": found["how"], "placed_by": None, "depth_note": None,
                         "depth_short": None})
            info.update(known.get(m["path"]) or {})
            source = info.get("placed_by") or ""
            if found["how"] == "none":
                m.update(include=False, why=(
                    "a mooring's instrument, but none of its mooring's instruments records "
                    "pressure, and its record is too short to leave out the days it may have "
                    "spent on deck: when it was in the water cannot be told"))
            elif found["how"] == "apart":
                m.update(include=False, why=(
                    "a mooring's instrument, but its mooring's pressure records do not overlap "
                    "in time: which deployment it belongs to cannot be told"))
            elif info.get("depth") is None and source != "pressure":
                m.update(include=False, why=(
                    "a mooring's instrument, but its depth is not known: it records no "
                    "pressure, and its file states none"
                    + (f"; {unplaced[m['path']]}" if m["path"] in unplaced else
                       "; no description in the accession lists its serial number")))
            else:
                m["why"] = MOORING_WHY + ({
                    "pressure": "; placed by the pressure it records",
                    "file": "; at the depth its file states",
                    "name": "; at the depth its file name states",
                }.get(source) or (f"; at the depth {info.get('description')} gives it: "
                                  f"stated, or its place on the line fixed by the pressure of "
                                  f"an instrument on it (each listed above the entry)")) + (
                    "; no instrument on its mooring records pressure, so the first and last "
                    "day of the record (when it may have been on deck) are left out"
                    if found["how"] == "record" else "")


_KEY_IS_DEPTH = re.compile(r"^(depth|dep|z|pres|press|pressure|prdm|depsm)", re.IGNORECASE)


def _locate_long_format(base: Path, files: List[Dict[str, Any]]) -> None:
    """Locate long-format files by the station list beside them (`long_format["station_list"]`).

    Of several lists, the one naming most of their casts wins; with none, they are proposed out.
    """
    from wod_etl.staging import normalise_station, read_station_list, read_table

    parent = lambda path: path.rsplit("/", 1)[0] if "/" in path else ""
    folders: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        if f["action"] == "stage" and f.get("long_format") and not f.get("mooring"):
            folders.setdefault(parent(f["path"]), []).append(f)
    for folder, members in folders.items():
        lists = [f["path"] for f in files if f["action"] == "sidecar"
                 and parent(f["path"]) == folder]
        chosen, why = (lists[0], None) if len(lists) == 1 else (None, None)
        if not lists:
            why = "no station list beside it gives their positions and dates"
        elif len(lists) > 1:
            casts = set()
            for m in members:
                keys = m["long_format"]["key_columns"]
                key = next((k for k in keys if not _KEY_IS_DEPTH.match(k)), keys[0])
                try:
                    cols, rows = read_table(base / m["path"])[:2]
                    i = cols.index(key)
                except (OSError, ValueError, IndexError):
                    continue
                casts |= {normalise_station(r[i]) for r in rows
                          if i < len(r) and str(r[i]).strip()}
            scores = []
            for sl in lists:
                try:
                    stations, info = read_station_list(base / sl)
                except (OSError, ValueError):
                    continue
                if not info.get("error"):
                    scores.append((len(casts & set(stations)), sl))
            scores.sort(reverse=True)
            if scores and scores[0][0] and (len(scores) == 1 or scores[0][0] > scores[1][0]):
                chosen = scores[0][1]
            else:
                why = (f"{len(lists)} station lists beside it, and none names more of its "
                       f"casts than another")
        for m in members:
            m["long_format"]["station_list"] = chosen
            if chosen:
                m["why"] = f"{m['why']}; located by {chosen.rsplit('/', 1)[-1]}"
            else:
                m.update(include=False, why=f"measurements named in a column rather than a "
                                            f"header row, but its casts cannot be located: "
                                            f"{why}")


def _locate_by_station_list(base: Path, files: List[Dict[str, Any]]) -> None:
    """Locate casts that give no position by a station list in their folder or one above.

    The list names each cast's file stem or station (`per_file`), or the stations of one table
    of many casts (`by_station`). Located files are staged; the rest stay out.
    """
    from wod_etl.discovery import read_any, sniff_format
    from wod_etl.staging import normalise_station, read_station_list

    parent = lambda path: path.rsplit("/", 1)[0] if "/" in path else ""
    lists = [f["path"] for f in files if f["action"] == "sidecar"]
    pending = [f for f in files if f.get("unlocated")]
    if not lists or not pending:
        return
    keyed: Dict[str, Dict[str, set]] = {}
    for sl in lists:
        try:
            stations, info = read_station_list(base / sl)
        except (OSError, ValueError):
            continue
        if info.get("error") or not stations:
            continue
        columns = list(next(iter(stations.values()))["raw"])
        keyed[sl] = {c: {normalise_station(e["raw"].get(c)) for e in stations.values()} - {""}
                     for c in columns}

    def nearest(path):
        """Station lists in or above a file's folder, closest first, csv before a workbook copy."""
        folder = parent(path)
        near = [sl for sl in keyed if folder == parent(sl) or not parent(sl)
                or folder.startswith(parent(sl) + "/")]
        return sorted(near, key=lambda sl: (-parent(sl).count("/") - bool(parent(sl)),
                                            sl.lower().endswith((".xls", ".xlsx")), sl))

    for f in pending:
        stem = normalise_station(Path(f["path"]).stem)
        found = None
        for sl in nearest(f["path"]):
            col = next((c for c, keys in keyed[sl].items() if stem in keys and
                        (_FILE_COLUMN.match(c) or _STATION_COLUMN.search(c))), None)
            if col:
                found = {"kind": "per_file", "station_list": sl, "key": col}
                break
        station_col = next((c for c in f.get("columns") or [] if _STATION_COLUMN.search(c)),
                           None)
        if not found and station_col:
            try:
                casts, _ = read_any(str(base / f["path"]), sniff_format(str(base / f["path"])))
                values = {normalise_station(v) for c in casts for v in c["df"].get(
                    station_col, [])} - {""}
            except Exception:                           # noqa: BLE001 - the file stays out
                values = set()
            for sl in nearest(f["path"]):
                col = next((c for c, keys in keyed[sl].items() if _STATION_COLUMN.search(c)
                            and len(values & keys) >= max(1, len(values) // 2)), None)
                if col:
                    found = {"kind": "by_station", "station_list": sl, "key": col,
                             "station_column": station_col}
                    break
        if found:
            name = found["station_list"].rsplit("/", 1)[-1]
            f.update(action="stage", include=True, located_by=found,
                     why=f"{f['unlocated']}; located by {name} (its '{found['key']}' column)")
        elif nearest(f["path"]):
            f["why"] += (f"; {_and(sl.rsplit('/', 1)[-1] for sl in nearest(f['path']))} "
                         f"do not name it")


def _combine_split_tables(base: Path, files: List[Dict[str, Any]]) -> None:
    """Stage tables holding the same casts (same stations, dates and positions) together.

    One cast per station and date, with levels merged by depth.
    """
    from wod_etl.staging.combine import keyed_casts, same_casts

    tables = {}
    for f in files:
        if f["action"] == "process" and f.get("include") and f.get("dataset") \
                and f["reader"] in ("delimited", "spreadsheet") \
                and any(_STATION_COLUMN.search(c) for c in f.get("columns") or []):
            try:
                keys = keyed_casts(base / f["path"])
            except Exception:                           # noqa: BLE001 - left as it is
                continue
            if keys:
                tables[f["path"]] = (f, keys)
    group: Dict[str, str] = {p: p for p in tables}
    root = lambda p: p if group[p] == p else root(group[p])
    paths = sorted(tables)
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            fa, ka = tables[a]
            fb, kb = tables[b]
            if fa["dataset"] == fb["dataset"] and same_casts(ka, kb):
                group[root(b)] = root(a)
    members: Dict[str, List[str]] = {}
    for p in paths:
        members.setdefault(root(p), []).append(p)
    for together in members.values():
        if len(together) < 2:
            continue
        for p in together:
            f = tables[p][0]
            others = _and(q.rsplit("/", 1)[-1] for q in together if q != p)
            f.update(action="stage", combine={"files": together},
                     why=f"{f['why']}; combined with {others}: the same casts (stations, dates "
                         f"and positions), so one cast per station and date with the levels "
                         f"merged by depth")


def _combine_tracks(base: Path, files: List[Dict[str, Any]]) -> None:
    """Tracks logged on one clock are one SUR cast, staged and merged on time (manual pp.16, 37)."""
    from wod_etl.staging.tracks import joining, read_track, same_clock_groups

    candidates = {f["path"]: f for f in files
                  if f["action"] == "process" and f.get("include") and f.get("dataset") == "SUR"
                  and f["reader"] in ("delimited", "spreadsheet")}
    if len(candidates) < 2:
        return
    tracks = {}
    for path in candidates:
        try:
            t = read_track(base / path)
        except Exception:                               # noqa: BLE001 - left as it is
            t = None
        if t is not None:
            tracks[path] = t
    groups = same_clock_groups(tracks)
    together = [p for g in groups if len(g) > 1 for p in g] + joining(tracks, groups)
    if not any(len(g) > 1 for g in groups):
        return
    for p in together:
        f = candidates[p]
        others = _and(q.rsplit("/", 1)[-1] for q in together if q != p)
        f.update(action="stage", combine_tracks={"files": sorted(together)},
                 why=f"{f['why']}; logged on one clock with {others}: one SUR cast per "
                     f"stream, merged on time")


def _pair_resolutions(files: List[Dict[str, Any]]) -> None:
    """A cast exported binned (`X001`) and at every sample (`X001all`) is written once, binned."""
    by_path = {f["path"].lower(): f for f in files if f["action"] == "process"}
    for path, full in by_path.items():
        stem = re.sub(r"[._-]?all$", "", path)
        binned = by_path.get(stem) if stem != path else None
        if binned and binned["format"] == full["format"]:
            full.update(include=False,
                        why=f"the same cast as {binned['path'].rsplit('/', 1)[-1]}, at every "
                            f"sample rather than binned: one copy is written")


PLANKTON = "plankton biomass or taxonomy"


def _derive_taxa(base: Path, files: List[Dict[str, Any]]) -> None:
    """Stage plankton tables as OSD Taxa-Records when their layout is understood and their hauls
    can be located by a station list or their own positions; otherwise propose them out."""
    from wod_etl.staging import is_staged_haul, read_station_list
    from wod_etl.staging.taxa_haul import taxa_record_field
    from wod_etl.staging.taxa_layout import BIOMASS, FIELD_UNITS, describe_taxa_table

    parent = lambda path: path.rsplit("/", 1)[0] if "/" in path else ""
    plankton = [f for f in files if f.get("exclusion") == PLANKTON and f["action"] == "exclude"
                and f["readable"] and not is_staged_haul(base / f["path"])]
    measures = set(FIELD_UNITS) | {4}
    # Station lists flagged as plankton only by their path become sidecars; a table with a
    # taxon and a measurement stays plankton.
    for f in plankton:
        fields = {taxa_record_field(c) for c in f.get("columns") or []}
        biomass = any(re.search(b[0], c, re.I) for c in f.get("columns") or [] for b in BIOMASS)
        if _sidecar(f.get("columns") or []) and not (1 in fields and fields & measures) \
                and not biomass:
            f.update(action="sidecar", exclusion=SIDECAR["reason"], belongs_in_wod=False,
                     harvest=SIDECAR["harvest"], reason=SIDECAR["route"],
                     data=SIDECAR["label"], why=SIDECAR["why"], include=False)
    for f in plankton:
        if f["action"] != "exclude":
            continue
        layout = describe_taxa_table(base / f["path"])
        measured = _variable_names(f.get("columns") or [])
        if measured and not layout["columns"].get("taxon") and _has_depth_axis(f.get("columns") or []):
            # Named for plankton but holding Table 3a measurements by depth: profile data.
            f.update(action="process", dataset="OSD", include=True, exclusion=None,
                     belongs_in_wod=None, data=f"OSD profiles: {_and(n.lower() for n in measured)}",
                     why="Table 3a measurements by depth, not plankton counts: written as OSD "
                         "profiles")
            continue
        cols = layout["columns"]
        station_list, located = None, None
        lists = [g["path"] for g in files if g["action"] == "sidecar"
                 and parent(g["path"]) == parent(f["path"])]
        if cols.get("station") and lists:
            scores = []
            for sl in lists:
                try:
                    stations, info = read_station_list(base / sl)
                except (OSError, ValueError):
                    continue
                if not info.get("error"):
                    scores.append((len(set(layout["stations"]) & set(stations)), sl))
            scores.sort(reverse=True)
            if scores and scores[0][0] and (len(scores) == 1 or scores[0][0] > scores[1][0]):
                station_list = scores[0][1]
                located = f"located by {station_list.rsplit('/', 1)[-1]}"
        if not located and all(cols.get(k) for k in ("latitude", "longitude", "date")):
            located = "located by its own latitude, longitude and date columns"
        missing = list(layout["missing"])
        if not located:
            missing.append("its hauls cannot be located: no station list names its stations "
                           "and it has no position columns")
        measured = [m["label"] for m in layout["measurements"]]
        f.update(action="stage", dataset="OSD", include=not missing,
                 taxa={**layout, "missing": missing, "station_list": station_list},
                 data=("plankton net hauls: " + (_and(measured) if measured else "taxa")
                       + " by taxon and depth range"),
                 why=(f"plankton, written as OSD Taxa-Records; {located}" if not missing
                      else "plankton, but it cannot be written: " + "; ".join(missing)))


# A text file named as documentation is documentation, whatever its body sniffs as.
_DOCUMENT_NAME = re.compile(
    r"(?:^|[_\-. ])(?:readme|read[_-]?me|description|notes?|abstract|documentation|"
    r"instructions?)(?:[_\-. ][^.]*)?\.(?:txt|md|rst|text|rtf|docx?|pdf|csv|tsv|xlsx?)$",
    re.IGNORECASE)

_DOCUMENT_LABELS = {"image": "image: a photo, map or diagram", "pdf": "PDF document",
                    "word_document": "Word document", "presentation": "slide presentation",
                    "xml": "XML metadata", "json": "JSON metadata",
                    "unstructured_text": "text: a readme, notes or a description",
                    "archive_zip": "zip archive", "zip_container": "zip archive",
                    "gzip": "compressed file", "bzip2": "compressed file",
                    "xz": "compressed file", "sqlite": "SQLite database",
                    "access_db": "Access database"}
_EVIDENCE = re.compile(r"^(\w+) matches (['\"])(.*?)\2: (.*)$")
_EVIDENCE_FIELDS = {"path": "its path", "instrument": "its instrument attribute",
                    "header": "its header", "columns": "its columns", "context": "the readme"}


def _evidence_source(evidence: str) -> str:
    """Shorten evidence for `why`: 'path matches 'CTD': names CTD' -> "its path ('CTD')"."""
    m = _EVIDENCE.match(evidence)
    if m:
        return f"{_EVIDENCE_FIELDS.get(m.group(1), m.group(1))} ('{m.group(3)}')"
    # The spacing, said briefly: 'levels 0.50 m apart (< 2.0 m)'.
    m = re.match(r"depth increment ([\d.]+) m is (?:high|low)-resolution \(([<>=]+ [\d.]+) m\)",
                 evidence)
    return f"levels {m.group(1)} m apart ({m.group(2)} m)" if m else evidence


def _and(words) -> str:
    words = list(words)
    return " and ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " and " + words[-1]


def _variable_names(columns, dataset: Optional[str] = None,
                    writable: bool = False) -> List[str]:
    """Table 3a names of the measurements in `columns`, in order and once each; with `dataset`,
    only those it stores; with `writable`, not those in a unit that cannot be converted."""
    try:
        from wod_etl.mapping.generate_field_mapping import molar_to_mass
        from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
        from wod_etl.reference import wod_variables
    except ImportError:
        return []
    table, out = wod_variables(), []
    for c in columns:
        code = match_variable(c)
        if code is None or code == DEPTH_SENTINEL_CODE or code in _LOCATING_CODES:
            continue
        if writable and molar_to_mass(str(c), code):
            continue
        entry = table.get(code) or {}
        name = entry.get("name") or f"code {code}"
        if dataset and dataset not in (entry.get("datasets") or []):
            continue
        if name not in out:
            out.append(name)
    return out


def _prose_throughout(path: Path, fmt, peek_bytes: int) -> bool:
    """Whether a file that sniffed as prose is prose to its end, not data under a long header."""
    from wod_etl.discovery.sniff_format import _sniff_text

    size = fmt.get("size_bytes") or 0
    if size <= peek_bytes:
        return True
    try:
        with path.open("rb") as fh:
            fh.seek(size - peek_bytes)
            tail = fh.read().decode(fmt.get("encoding") or "utf-8", errors="replace")
    except OSError:
        return False
    tail = tail.split("\n", 1)[-1]                 # the first line is cut part way
    return _sniff_text(tail, size).get("format_family") == "unstructured_text"


FILE_ACTIONS = ("process", "stage", "sidecar", "exclude", "document", "inspect")


def classify_accession(root: str, max_files: Optional[int] = None,
                       peek_bytes: int = 8192,
                       accession: Optional[str] = None) -> Dict[str, Any]:
    """Classify every file under an accession root: a verdict per file, grouped by folder.

    Reads only each file's head. Per-file verdicts (`files`, `<action>_files`) decide; folder
    verdicts summarise them. Logged to the accession's run log (`accession`, or from `root`).
    """
    with runlog.accession_log_for(root, accession, actor="library",
                                  arguments={"call": "classify_accession", "root": str(root)}):
        report = _classify_accession(root, max_files, peek_bytes)
        if report.get("error"):
            runlog.event(log, logging.ERROR, f"triage failed: {report['error']}",
                         kind="triage", error=report["error"])
        else:
            counts = {a: len(report.get(f"{a}_files") or []) for a in FILE_ACTIONS}
            runlog.event(log, logging.INFO,
                         f"triage of {root}: {report['file_count']} file(s), "
                         + ", ".join(f"{n} {a}" for a, n in counts.items() if n),
                         kind="triage", counts=counts,
                         shapes=report.get("cross_file_shapes"))
        return report


def _classify_accession(root, max_files, peek_bytes) -> Dict[str, Any]:
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

    _place_moorings(base, files)
    _locate_long_format(base, files)
    _locate_by_station_list(base, files)
    _derive_taxa(base, files)
    _combine_split_tables(base, files)
    _combine_tracks(base, files)
    _pair_resolutions(files)
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
        "note": "The per-file verdicts (`files`, `<action>_files`) decide; the folder "
                "verdicts summarise them. 'inspect' means the evidence did not decide.",
        "shape_note": ("cross_file_shapes flags folders whose files look like parts of one "
                       "cast rather than one cast each, or tables that must be pivoted. "
                       "Processed as they stand, those give single-level profiles, "
                       "half-empty ones, or nothing at all: reshape them during staging."
                       if shapes else None),
    }


# ---------------------------------------------------------------- internals
def _search(pattern, text):
    """Match against the text, a separator-flattened copy, and a CamelCase-split copy.

    Word boundaries miss words joined by '_' ('glider_slocum') or by case ('TrawlInfo'); the
    original is tried too because some signals include the separator ('u_vel').
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

    The last split is a guess, so `_search` tries the unsplit forms first.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"(?<=[A-Z]{2})(?=[a-z]{3,})", " ", s)
    return s


def _score_exclusions(haystacks) -> Dict[str, Dict[str, Any]]:
    """Score each exclusion category separately by path, instrument attribute and columns.

    These carry different authority: a path excludes outright, columns exclude only a file
    with no profile measurement, and an instrument attribute needs matching columns too.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for reason, cat in EXCLUSION_CATEGORIES.items():
        signals = list(cat["signals"])
        if reason in _SHARED_BY:
            signals.append(SHARED_BIOLOGICAL_SIGNAL)
        entry = {"path": 0.0, "instrument": 0.0, "columns": 0.0, "evidence": []}
        for pattern, weight, why in signals:
            for field in ("path", "instrument", "columns"):
                m = _search(pattern, haystacks[field][0])
                if m:
                    entry[field] += weight
                    entry["evidence"].append(f"{field} matches {m.group(0)!r}: {why}")
                    break                       # one hit per signal, not per field
        if entry["path"] or entry["instrument"] or entry["columns"]:
            out[reason] = entry
    return out


def _find_exclusion(haystacks, columns):
    """(exclusion or None, prose flags).

    An exclusion needs evidence about the file itself (path or columns); a match in prose is only
    flagged. A tie between the two biological categories is reported as ambiguous.
    """
    profile_columns = _mappable_columns(columns)
    prose_flags: List[str] = []
    scores = _score_exclusions(haystacks)

    # A column-only hit on a file that carries a profile is reported, not acted on.
    eligible = {}
    for reason, e in scores.items():
        if e["path"] or (e["instrument"] and e["columns"]) or not profile_columns:
            eligible[reason] = e
        elif e["columns"]:
            prose_flags.append(
                f"{e['evidence'][0]}, but the file also carries mappable profile columns "
                f"({', '.join(profile_columns[:4])}), so it is processed as a profile "
                f"and the {reason} columns are dropped")

    for field in ("header", "context"):
        text = haystacks[field][0]
        for reason, cat in EXCLUSION_CATEGORIES.items():
            for pattern, _w, _why in cat["signals"]:
                m = _search(pattern, text)
                if m:
                    prose_flags.append(
                        f"{field} matches {m.group(0)!r}: {reason} (mentioned in {field} "
                        f"text only, not evidence about this file's own content; check it "
                        f"during triage)")
                    break

    if not eligible:
        return None, prose_flags

    total = {r: e["path"] + e["instrument"] + e["columns"] for r, e in eligible.items()}
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
            "matched_in": next(f for f in ("path", "instrument", "columns") if entry[f]),
            "confidence": confidence, "matched": evidence[0], "evidence": evidence,
            "score": best_score, "alternatives": alternatives or None,
            "belongs_in_wod": cat["belongs_in_wod"], "label": cat.get("label"),
            "why": cat.get("why")}, prose_flags


# Table 3a latitude, longitude and julian day codes locate a cast; they are not measurements.
_LOCATING_CODES = {30, 31, 32}


def _mappable_columns(columns) -> List[str]:
    """Columns that resolve to a Table 3a measurement code (not the depth axis or a coordinate)."""
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


# A list naming each cast's file is an index of casts, even with a measurement per station.
_FILE_COLUMN = re.compile(r"^(?:file[\s_-]*(?:name)?|filename|data[\s_-]*file)$", re.I)


def _sidecar(columns) -> Optional[Dict[str, Any]]:
    """A station list: station, latitude and longitude columns, and nothing measured (or a
    column naming each cast's file)."""
    cols = [c.strip() for c in columns if c and c.strip()]
    if not cols or (_mappable_columns(cols) and not any(_FILE_COLUMN.match(c) for c in cols)):
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


# A long-format table names the variable in a cell (`StNo, parm, depth, value`), so its
# 'value' column maps to nothing.
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
    """Columns WOD has no Table 3a code for, with the reason and what to do instead."""
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


def _shape_evidence(columns, depth_increment_m, n_levels, vertical_axis=None):
    """What the table's own shape implies when nothing names an instrument."""
    out = []
    # Structure first: a measured level spacing, or a NetCDF depth/pressure variable. Column
    # names alone miss long names such as 'sea_water_pressure'.
    has_depth = bool(vertical_axis) or depth_increment_m is not None or any(
        _DEPTH_TOKENS.search(c) for c in columns)
    has_temp = any(_TEMP_TOKENS.search(c) for c in columns)
    # Bottle chemistry without a depth axis is discrete surface samples: OSD, not SUR, which
    # would drop it. It outweighs 'underway' in a path.
    from wod_etl.mapping.query_wod_spec import match_variable
    samples = bool(columns) and not has_depth and any(match_variable(c) in _DISCRETE_CODES
                                                      for c in columns)
    if samples:
        out.append(("OSD", 15.0, "bottle chemistry without a depth axis: discrete surface "
                                 "samples, which are OSD"))
    if columns and not has_depth and has_temp and not samples:
        out.append(("SUR", 3.0, "columns carry temperature but no depth or pressure axis: "
                                "a surface-only record"))
    # A position on every row and no depth axis is a track, whatever instrument it names.
    if columns and not has_depth and not samples and any(_LAT_COLUMN.search(c) for c in columns) \
            and any(_LON_COLUMN.search(c) for c in columns):
        out.append(("SUR", 6.0, "a position on every row and no depth axis: a surface track"))
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


def _all_measurements_empty(columns, density) -> List[str]:
    """The measurement columns, when every one of them is empty in the rows sampled."""
    from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable

    per_column = (density or {}).get("per_column") or {}
    measured = [c for c in columns or [] if c in per_column
                and match_variable(c) not in (None, DEPTH_SENTINEL_CODE, *_LOCATING_CODES)]
    return measured if measured and all(per_column[c] == 0.0 for c in measured) else []


# Nutrients, alkalinity, DIC and the tracers: measured on discrete samples in a laboratory.
_DISCRETE_CODES = {4, 6, 8, 17, 21} | set(range(33, 44))


def _locates_itself(columns, head: str) -> bool:
    """Whether a table gives its own position: latitude and longitude columns, or lines."""
    from wod_etl.mapping.generate_field_mapping import parse_header_block

    from wod_etl.mapping.query_wod_spec import match_variable

    if {30, 31} <= {match_variable(str(c)) for c in columns}:
        return True
    names = [english_terms(str(c)) for c in columns]
    if any(_LAT_COLUMN.search(c) for c in names) and any(_LON_COLUMN.search(c) for c in names):
        return True
    # A CSV preamble writes its pairs as `Latitude,49.5`: read them as `Latitude: 49.5`.
    pairs = re.sub(r"(?m)^\s*([A-Za-z][^,:=\n]{0,40}?)\s*[,;\t]\s*([^,;\t\n]+?)[,;\t\s]*$",
                   r"\1: \2", head or "")
    coords = parse_header_block(pairs).get("coordinates") or {}
    return "latitude" in coords and "longitude" in coords


def _has_depth_axis(columns) -> bool:
    return any(_DEPTH_TOKENS.search(str(c).strip()) for c in columns)


def _confidence(score: float) -> str:
    for threshold, label in CONFIDENCE_THRESHOLDS:
        if score >= threshold:
            return label
    return "none"


_TIME_UNITS = re.compile(r"^\s*(second|minute|hour|day)s?\s+since\b", re.I)
_DAYS_PER = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24, "day": 1.0}
# degree_north, degrees_east, degree_W; not plain "degrees", which is a heading or a roll.
_POSITION_UNITS = re.compile(r"^degrees?_?(?:north|east|west|south)$|^degrees?_[nesw]$", re.I)
_PLATFORM_ATTRS = ("MOORING", "platform_code", "platform_name", "platform", "station_name")
# Attributes naming what recorded the file (EPIC INST_TYPE, ACDD instrument, RBR model).
_INSTRUMENT_ATTR = re.compile(r"^(?:inst_type|instrument(?:_?(?:sub)?type|_?model|_?name)?|"
                              r"make_model|model|sensor(?:_?(?:type|model))?)$", re.I)
# Points sampled to see a record's span and whether its position moves.
_SERIES_SAMPLE = 10000


def _netcdf_evidence(path) -> Optional[Dict[str, Any]]:
    """Triage evidence from a NetCDF file's attributes and variables.

    Returns header text from global attributes, columns as long names with units, instrument
    attributes, and a `time_series` summary when it is one instrument's record.
    """
    try:
        import netCDF4
        import numpy as np

        with netCDF4.Dataset(str(path)) as ds:
            attrs = {a: _attr_text(ds.getncattr(a)) for a in ds.ncattrs()}
            variables = {name: honour_fill(v) for name, v in ds.variables.items()}
            columns = [_nc_column(name, v) for name, v in variables.items()
                       if not _container(name, v)]
            names = [f"{name} ({v.units})" if getattr(v, "units", None) else name
                     for name, v in variables.items() if not _container(name, v)]
            # The instrument may be described in a container variable (`instrument:make_model`).
            described = [_attr_text(v.getncattr(a)) for name, v in variables.items()
                         if _container(name, v) for a in v.ncattrs()
                         if a in ("long_name", "make_model", "serial_number", "comment")]
            series = _time_series(ds, variables, np)
            copies = _copies(variables, np)
            increment = _depth_increment(variables, np)
            vertical = any(_vertical(name, v) for name, v in variables.items()
                           if not _container(name, v))
    except Exception:                                   # noqa: BLE001 - the reader reports it
        return None
    named = [v for k, v in attrs.items() if v and _INSTRUMENT_ATTR.match(k)] + \
        [d for d in described if d]
    if series:
        series["platform"] = next((attrs[a] for a in _PLATFORM_ATTRS if attrs.get(a)), None)
        series["instrument"] = named[0] if named else None
    return {"header": "\n".join(f"{k}: {v}" for k, v in attrs.items() if v),
            "columns": columns, "instrument": " ".join(dict.fromkeys(named)),
            "time_series": series, "copies": copies, "depth_increment": increment,
            "vertical": vertical, "names": names}


def _depth_increment(variables, np) -> Optional[float]:
    """The median spacing of a NetCDF file's depth axis.

    WOD splits CTD from OSD by depth increment (< 2 m is high-resolution, manual p.19).
    """
    for name, v in variables.items():
        described = " ".join([name] + [str(getattr(v, a, "") or "")
                                        for a in ("standard_name", "long_name")]).lower()
        if v.ndim == 0 or not re.search(r"(?<![a-z])depth(?![a-z])|^z\b|^dpth\b|^deph\b",
                                        described) \
                or _NOT_AXIS.search(described) or v.size > MAX_COPY_CHECK_VALUES:
            continue
        z = np.ma.filled(np.ma.asarray(v[:], dtype="float64"), np.nan)
        z = z.reshape(-1, z.shape[-1]) if z.ndim > 1 else z.reshape(1, -1)
        steps = np.abs(np.diff(z, axis=-1))
        steps = steps[np.isfinite(steps) & (steps > 0)]
        return round(float(np.median(steps)), 3) if steps.size else None
    return None


# Above this many values a variable is not read whole at triage.
MAX_COPY_CHECK_VALUES = 5_000_000


def _copies(variables, np) -> List[Dict[str, str]]:
    """Measured variables equal to an earlier one at every level (e.g. salinity filled with
    temperature); the later one is taken as the copy."""
    try:
        from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
        from wod_etl.reference import wod_variables
    except ImportError:
        return []
    table = wod_variables()
    measured = []
    for name, v in variables.items():
        code = match_variable(_nc_column(name, v))
        if code is None or code == DEPTH_SENTINEL_CODE or code in _LOCATING_CODES \
                or v.size > MAX_COPY_CHECK_VALUES or v.ndim == 0:
            continue
        measured.append((name, (table.get(code) or {}).get("name", name),
                         np.ma.filled(np.ma.asarray(v[:], dtype="float64"), np.nan)))
    out = []
    for i, (a, a_name, x) in enumerate(measured):
        for b, b_name, y in measured[i + 1:]:
            # Two names for one variable (e.g. TEMPP901, TEMPS901) are not a copy.
            if a_name == b_name or any(c["copy"] == b_name for c in out):
                continue
            both = np.isfinite(x) & np.isfinite(y)
            if x.shape == y.shape and both.sum() >= 10 and np.array_equal(x[both], y[both]) \
                    and np.array_equal(np.isfinite(x), np.isfinite(y)):
                out.append({"copy": b_name, "of": a_name})
    return out


def _container(name: str, v) -> bool:
    """A variable that holds attributes, not data: `instrument`, `instrument_ctd`, `crs`."""
    return v.ndim == 0 and bool(re.match(r"^(?:instrument|sensor|platform)", name, re.I))


def _attr_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return " ".join(str(value).split())[:300]


def _nc_column(name: str, v) -> str:
    label = str(getattr(v, "long_name", "") or getattr(v, "standard_name", "") or name).strip()
    units = str(getattr(v, "units", "") or "").strip()
    if units and "since" not in units and f"({units.lower()})" not in label.lower():
        label = f"{label} ({units})"
    return label


def _instrument(path) -> Dict[str, Any]:
    """What a mooring recipe reads from one instrument's file, or its error."""
    from wod_etl.staging.moorings import read_instrument

    return read_instrument(path)


def _time_series(ds, variables, np) -> Optional[Dict[str, Any]]:
    """Summarise a single-instrument time series spanning a day or more, else None.

    Returns {"samples", "days", "moving"}; `moving` means along a track, not on a mooring.
    """
    time_name = next((n for n, v in variables.items() if len(v.dimensions) == 1
                      and _TIME_UNITS.match(str(getattr(v, "units", "")))), None)
    if time_name is None:
        return None
    tvar = variables[time_name]
    tdim = tvar.dimensions[0]
    n = len(ds.dimensions[tdim])
    step = max(1, n // _SERIES_SAMPLE)

    def sample(v):
        cut = tuple(slice(None, None, step) if d == tdim else slice(None) for d in v.dimensions)
        values = np.ma.compressed(np.ma.asarray(v[cut], dtype="float64"))
        return np.append(values, np.ma.compressed(np.ma.asarray(v[-1:], dtype="float64"))) \
            if v.dimensions == (tdim,) else values

    t = sample(tvar)
    if t.size < 2:
        return None
    days = float(t.max() - t.min()) * _DAYS_PER[_TIME_UNITS.match(tvar.units).group(1).lower()]
    if days < MIN_SERIES_DAYS:
        return None
    coordinates = {name for name, v in variables.items() if v.dimensions == (name,)}
    data = {name: v for name, v in variables.items() if name not in coordinates and v.ndim
            and getattr(v.dtype, "kind", "U") not in "SU"}          # not a name or a label
    if not data:
        return None
    for v in data.values():
        if tdim not in v.dimensions or any(len(ds.dimensions[d]) != 1
                                           for d in v.dimensions if d != tdim):
            return None
    moving = False
    for v in variables.values():
        if _POSITION_UNITS.match(str(getattr(v, "units", ""))) and tdim in v.dimensions:
            pos = sample(v)
            if pos.size and float(pos.max() - pos.min()) > MAX_POSITION_SPREAD_DEG:
                moving = True
    if moving and any(_vertical(name, v) for name, v in data.items()):
        return None
    return {"samples": n, "days": round(days, 1), "moving": moving}


# Depths that are not a vertical axis: the sea floor's, a sounding's.
_NOT_AXIS = re.compile(r"bottom|sea[\s_]?floor|sounding|bathy", re.I)


def _vertical(name: str, v) -> bool:
    """A depth or pressure axis (not a bottom depth). On a moving platform it marks a glider
    or a profiler rather than a surface record."""
    described = " ".join(str(getattr(v, a, "") or "") for a in ("standard_name", "long_name"))
    if _NOT_AXIS.search(f"{name} {described}"):
        return False
    units = str(getattr(v, "units", "")).strip().lower()
    return bool(_DEPTH_TOKENS.search(name) or re.match(r"dpth\b", name, re.I)
                or re.search(r"(?<![a-z])(depth|pressure)(?![a-z])", described, re.I)
                or units.startswith(("dbar", "decibar")))


_OWN_READERS = ("wod_csv", "probe_dat", "tsk_xctd", "spreadsheet")


def _columns_from_head(head: str, fmt) -> List[str]:
    """Best-effort column names from a file's first bytes, for triage only."""
    import csv

    if fmt.get("reader") not in ("delimited", "fixed_width", "sea_bird_cnv"):
        return []
    delim = fmt.get("delimiter", ",")
    from wod_etl.discovery.profile_raw_data import delimited_layout

    layout = delimited_layout(head.splitlines(), delim) if delim else None
    if layout:
        return layout["columns"]
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
            return _header_above_data(head) or s.split()
    return names


def _header_above_data(head: str) -> List[str]:
    """A whitespace table's column names: the line above its first two data rows."""
    lines = [l.strip() for l in head.splitlines() if l.strip()]
    for i in range(1, len(lines) - 1):
        if _numeric_row(lines[i]) and _numeric_row(lines[i + 1]):
            return [] if _numeric_row(lines[i - 1]) else lines[i - 1].split()
    return []


def _numeric_row(line: str) -> bool:
    fields = line.replace(",", " ").split()
    try:
        return bool(fields) and all(float(f) is not None for f in fields)
    except ValueError:
        return False


def _text_depth_increment(head: str, columns) -> Optional[float]:
    """The median depth (or pressure) spacing in a text table's sampled rows.

    WOD routes CTD to CTD or OSD by it (< 2 m is high-resolution, manual p.19).
    """
    import numpy as np

    try:
        from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
    except ImportError:
        return None
    # Depth, else pressure (`PR`, `prDM`): decibars are metres to within the 2 m rule.
    at = next((i for code in (DEPTH_SENTINEL_CODE, 25) for i, c in enumerate(columns or [])
               if match_variable(str(c)) == code), None)
    if at is None:
        return None
    values = []
    for line in head.splitlines():
        fields = line.replace(",", " ").replace(";", " ").split()
        if len(fields) == len(columns) and _numeric_row(" ".join(fields)):
            values.append(float(fields[at]))
        elif values and len(values) >= 3 and not _numeric_row(line):
            break                         # the next station's header: one cast is enough
    steps = np.abs(np.diff(np.array(values)))
    steps = steps[steps > 0]
    return round(float(np.median(steps)), 3) if steps.size >= 2 else None


def _sample_column_values(head: str, fmt, columns, column: str, limit: int = 12) -> List[str]:
    """Distinct values of one column in the sampled rows (a long table's variable names)."""
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


# Columns that index or locate a level rather than measure anything.
_NON_MEASUREMENT = re.compile(
    r"^(station|stn|cast|sample|bottle|niskin|id|seq|line|depth|dep|z|pres|press|pressure|"
    r"lat|latitude|lon|long|longitude|date|time|datetime|year|month|day|hour|julian|"
    r"yearday|flag|qc|quality)\b", re.I)


# Folder shapes that break one-cast-per-file, visible only across files: one file per depth
# (identical columns, one or two rows each) or per variable (disjoint columns sharing a key).
MIN_FILES_FOR_SHAPE_CHECK = 3
MAX_ROWS_FOR_ONE_LEVEL_PER_FILE = 2


def _cross_file_shape(entry) -> Optional[Dict[str, Any]]:
    """Whether a folder's files look like parts of one cast rather than one cast each."""
    readable = [f for f in entry["files"] if f.get("readable") and f.get("columns")
                and f.get("action") not in ("document", "sidecar", "exclude")]

    # Long format first: its files share column names, which the checks below would misread.
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
            "action": "Pivot with wod_etl.staging.pivot_long, passing every file together "
                      "as a list of tables (with key_aliases for any key column spelt "
                      "differently between them), then split the result per cast with "
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
            "action": "These are levels of one cast, not separate casts. Combine them with "
                      "wod_etl.staging.combine_levels, sort by depth, then stage one file; "
                      "otherwise each becomes a single-level profile.",
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
            "action": f"These may be variables of one cast. Join them with "
                      f"wod_etl.staging.combine_on_key(key={key!r}) and check "
                      f"info['per_table']: a near-zero key overlap means they share no key "
                      f"and the join, though it runs, is meaningless. Confirm against the "
                      f"folder readme before joining.",
        }
    return None


def _column_density(head: str, fmt, columns) -> Optional[Dict[str, Any]]:
    """Per-column fill rate over the sampled rows.

    `measurement_density` ignores columns that index or locate a level, so a file whose
    measurements are all `Not analyzed` scores 0.0.
    """
    from wod_etl.config import NA_SENTINELS

    if fmt.get("reader") not in ("delimited", "fixed_width", "sea_bird_cnv"):
        return None
    from wod_etl.discovery.profile_raw_data import delimited_layout

    delim = fmt.get("delimiter")
    lines = head.splitlines()
    # A title, 'Key,value' rows or a units row above the data: start after them.
    layout = delimited_layout(lines, delim) if delim else None
    if layout:
        lines = lines[layout["header_line"] + (2 if layout.get("unit_row") else 1):]
    rows, seen_header = [], bool(layout)
    for line in lines:
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
    """process / stage / exclude / inspect, with the reason."""
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
                          f"sampled rows: every measurement column is sentinel or blank "
                          f"while the station and depth columns are full. The sample is only "
                          f"the first few KB: count real values over the whole file before "
                          f"excluding it."}
    by_action = entry.get("by_action") or {}
    # Long-format files add nothing to `types`; staged files keep the folder from 'exclude'.
    if excluded and not types and not by_action.get("stage"):
        reasons = ", ".join(f"{r} ({n})" for r, n in sorted(excluded.items()))
        harvest = sorted({h for h in entry.get("harvest", []) if h})
        return {"action": "exclude", "reason": f"not WOD profile data: {reasons}",
                # Excluding a folder is not the same as having nothing to take from it.
                "harvest": harvest or None,
                "note": ("This folder is excluded from the profile path but still holds "
                         "metadata for casts that are being written: see `harvest`."
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
                       + (f". The folder also holds " + ", ".join(
                           f"{n} to {a}" for a, n in sorted(others.items()))
                          + "; the per-file verdicts decide, not this summary"
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
                          + (f" The folder also holds " + ", ".join(
                              f"{n} to {a}" for a, n in sorted(others.items()))
                             + "; the per-file verdicts decide." if others else "")}
    return {"action": "inspect",
            "reason": f"{len(readable)} readable file(s), but nothing names an instrument: "
                      f"the dataset must be given by hand."}
