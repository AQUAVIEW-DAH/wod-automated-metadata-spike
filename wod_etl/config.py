"""Global paths, environment settings, and tunable constants for the WOD ETL pipeline.

Every skill imports its defaults from here so an agent can change behaviour in one place
rather than passing the same arguments through every call.
"""
import os
from pathlib import Path

# ----------------------------------------------------------------- paths
# The project root cannot be derived by counting parents: an installed copy of the package
# sits elsewhere entirely. Walk up looking for the markers that define a WOD ETL workspace
# -- all of them tracked files, so a fresh clone resolves before data/ exists -- and fall
# back to the environment variable or the current directory.


def _find_root() -> Path:
    override = os.environ.get("WOD_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "knowledge" / "wod_code_tables").is_dir():
            return candidate
        if (candidate / "pyproject.toml").is_file() and (candidate / "knowledge").is_dir():
            return candidate
    return Path.cwd().resolve()


ROOT = _find_root()

def _folder(env: str, default: Path) -> Path:
    """A data folder, from the environment when set: `wod-etl web` runs each command as a
    child process and hands it the folders it was itself given."""
    value = os.environ.get(env, "").strip()
    return Path(value) if value else default


DATA_DIR = _folder("WOD_DATA_DIR", ROOT / "data")
RAW_DIR = _folder("WOD_RAW_DIR", DATA_DIR / "raw")
STAGING_DIR = _folder("WOD_STAGING_DIR", DATA_DIR / "staging")
PROCESSED_DIR = _folder("WOD_PROCESSED_DIR", DATA_DIR / "processed")
MANIFEST_DIR = _folder("WOD_MANIFEST_DIR", STAGING_DIR / "manifests")
# One folder per accession: its run log, pipeline state, decisions file and report, and
# work/, the copies of the inputs the process stage reads. Its lock and state describe the
# outputs under staging/ and processed/, so move it only with them.
RUNS_DIR = _folder("WOD_RUNS_DIR", DATA_DIR / "runs")

KNOWLEDGE_DIR = ROOT / "knowledge"
CODE_TABLES_DIR = KNOWLEDGE_DIR / "wod_code_tables"
WOD_README_PDF = KNOWLEDGE_DIR / "wod_readme.pdf"

for _d in (RAW_DIR, STAGING_DIR, PROCESSED_DIR, MANIFEST_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ HTTP
USER_AGENT = os.environ.get("WOD_USER_AGENT", "WOD-ETL-Agent/1.0")
HTTP_TIMEOUT = float(os.environ.get("WOD_HTTP_TIMEOUT", "30"))
PEEK_BYTES = int(os.environ.get("WOD_PEEK_BYTES", "8192"))
DOWNLOAD_CHUNK_BYTES = 1 << 16
MAX_DOWNLOAD_BYTES = int(os.environ.get("WOD_MAX_DOWNLOAD_BYTES", str(2 * 1024**3)))
HTTP_RETRIES = int(os.environ.get("WOD_HTTP_RETRIES", "3"))
HTTP_BACKOFF_SECONDS = 2.0

# --------------------------------------------------------- WOD constants
# Manual p.19: high-resolution casts have a depth increment < 2 m and belong to the CTD
# dataset; anything coarser is low-resolution and belongs to OSD.
HIGH_RES_DEPTH_INCREMENT_M = 2.0

# Manual p.54: gradient thresholds differ above and below 400 m.
GRADIENT_DEPTH_SPLIT_M = 400.0

# Manual p.54: for high-resolution instruments use a minimum depth difference of 3.0 m
# when computing gradients, so a tiny dz cannot inflate the result.
MIN_GRADIENT_DEPTH_DIFF_M = 3.0

# NOT a WOD constant. Table 3b supplies atomic weights and the molar volume of O2 only --
# it gives no seawater density. This is a conventional stand-in used when in-situ density
# is unavailable, and it is recorded in the output metadata whenever it is applied.
ASSUMED_SEAWATER_DENSITY_KG_M3 = 1025.0
SEAWATER_DENSITY_PROVENANCE = (
    "assumed constant 1025 kg/m3; not specified by WOD Table 3b; in-situ density unavailable"
)

# ---------------------------------------------------------------- output
NETCDF_FILL_VALUE = -9999.0
QC_FILL_VALUE = -1
CF_CONVENTIONS = "CF-1.8"
WOD_VERSION = "WOD23"

# ------------------------------------------------------- WOD code lists
# Coded metadata fields (platform, institution, instrument, probe type, originator's units,
# scale, gear, life stage...) hold a CODE from a list WOD publishes online. The originator's
# text is looked up in that list and written as a code only when exactly one entry matches;
# otherwise the text is kept beside the field as `<field>_originator_text` and the lookup
# result says why. See wod_etl/reference/code_lists.py.
#
# The lists are cached here with the date they were retrieved, and refetched after
# CODE_LIST_MAX_AGE_DAYS -- they change between WOD releases.
CODE_LIST_CACHE_DIR = _folder("WOD_CODE_LIST_CACHE_DIR",
                              DATA_DIR / "reference_cache" / "wod_code_lists")
CODE_LOOKUP = os.environ.get("WOD_CODE_LOOKUP", "1").strip().lower() in ("1", "true", "yes", "on")
CODE_LIST_FETCH = os.environ.get("WOD_CODE_LIST_FETCH", "1").strip().lower() in (
    "1", "true", "yes", "on")
CODE_LIST_MAX_AGE_DAYS = float(os.environ.get("WOD_CODE_LIST_MAX_AGE_DAYS", "30"))

# ------------------------------------------------- taxa-records (plankton)
# Table 6a Biological Header fields without which a count cannot be interpreted -- gear (7),
# tow type (4), mesh (3), volume filtered (1) or tow distance (14), preservative (10), count
# method (13). A JUDGEMENT, not a manual table: the ids are WOD's, the choice is ours. A haul
# that lacks one is reported, never filled in. Fields grouped in a tuple are alternatives:
# either satisfies the requirement.
KEY_BIOLOGICAL_FIELD_IDS = (7, 4, 3, (1, 14), 10, 13)

# --------------------------------------------------------- dataset routing
# What a file is processed as when nothing identifies it and the caller has asked for
# unidentified files to be processed anyway. It is an ASSUMPTION, never a classification:
# process_accession records which files were routed this way so a report can say so.
DEFAULT_DATASET_TYPE = os.environ.get("WOD_DEFAULT_DATASET_TYPE", "OSD")

# ------------------------------------------------------------------- QC
# Whether to run WOD's own observed-level quality control (manual Table 12, section III).
# OFF by default: WOD QC is applied downstream by the data centre, and a flag written here
# is one more thing to reconcile. The ORIGINATOR's own flag columns are carried through
# either way -- verbatim, on the submitter's scale, never translated to Table 12.
#
# Turn it on with WOD_RUN_QC=1, or per call with process_accession(run_qc=True).
RUN_WOD_QC = os.environ.get("WOD_RUN_QC", "0").strip().lower() in ("1", "true", "yes", "on")

# ------------------------------------------------------- failure policy
# How process_accession reacts to a per-file failure during an unattended run.
#   "skip"  -- record the failure in the manifest and continue (default)
#   "stop"  -- abort the whole run on the first failure
ON_FILE_ERROR = os.environ.get("WOD_ON_FILE_ERROR", "skip")

# A run aborts if this fraction of files fails, on the assumption that something
# systematic is wrong rather than the data being individually bad.
ABORT_IF_FAILURE_RATE_ABOVE = float(os.environ.get("WOD_ABORT_FAILURE_RATE", "0.5"))
ABORT_FAILURE_RATE_MIN_SAMPLE = 10

# ------------------------------------------------------- source text
# Originators write CSVs in whatever their spreadsheet defaults to. Japanese-authored
# files are routinely cp932 and will raise UnicodeDecodeError on utf-8. Tried in order.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "latin-1")

# Missing-value markers beyond the numeric sentinels load_tabular already knows. Real
# submissions use a legend of asterisk codes, and their meanings differ:
#   below-detection and not-detected are REAL information (the value lies under a stated
#   threshold) but WOD Table 12 has no observed-level flag for it, so they still become
#   missing. Count them and say so rather than dropping them quietly.
NA_SENTINELS = {
    "**NS": "not sampled",
    "*NS": "not sampled",
    "***NA": "not analysed",
    "**NA": "not analysed",
    "*NA": "not analysed",
    "Not analyzed": "not analysed",
    "Not analysed": "not analysed",
    "*DL": "below detection limit",
    "**DL": "below detection limit",
    "**ND": "not detected",
    "*ND": "not detected",
    "****ND": "no data",
}

# Extensions that are metadata/documentation rather than measurements.
NON_DATA_EXTENSIONS = {
    ".xml", ".pdf", ".html", ".htm", ".md", ".doc", ".docx", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".tar", ".sh",
}


def child_environment() -> dict:
    """Environment variables that give a child `wod-etl` process this process's folders and
    settings as they are now -- overrides made after import (a test's) included."""
    flag = lambda value: "1" if value else "0"
    return {"WOD_DATA_DIR": str(DATA_DIR), "WOD_RAW_DIR": str(RAW_DIR),
            "WOD_STAGING_DIR": str(STAGING_DIR), "WOD_PROCESSED_DIR": str(PROCESSED_DIR),
            "WOD_MANIFEST_DIR": str(MANIFEST_DIR), "WOD_RUNS_DIR": str(RUNS_DIR),
            "WOD_CODE_LIST_CACHE_DIR": str(CODE_LIST_CACHE_DIR),
            "WOD_CODE_LIST_FETCH": flag(CODE_LIST_FETCH), "WOD_CODE_LOOKUP": flag(CODE_LOOKUP),
            "WOD_RUN_QC": flag(RUN_WOD_QC)}


def describe() -> dict:
    """Return the effective configuration, for logging into a run manifest."""
    return {
        "root": str(ROOT),
        "raw_dir": str(RAW_DIR),
        "staging_dir": str(STAGING_DIR),
        "processed_dir": str(PROCESSED_DIR),
        "runs_dir": str(RUNS_DIR),
        "user_agent": USER_AGENT,
        "http_timeout": HTTP_TIMEOUT,
        "http_retries": HTTP_RETRIES,
        "peek_bytes": PEEK_BYTES,
        "high_res_depth_increment_m": HIGH_RES_DEPTH_INCREMENT_M,
        "gradient_depth_split_m": GRADIENT_DEPTH_SPLIT_M,
        "min_gradient_depth_diff_m": MIN_GRADIENT_DEPTH_DIFF_M,
        "assumed_seawater_density_kg_m3": ASSUMED_SEAWATER_DENSITY_KG_M3,
        "default_dataset_type": DEFAULT_DATASET_TYPE,
        "run_wod_qc": RUN_WOD_QC,
        "on_file_error": ON_FILE_ERROR,
        "text_encodings": list(TEXT_ENCODINGS),
        "code_lookup": CODE_LOOKUP,
        "code_list_fetch": CODE_LIST_FETCH,
        "code_list_cache_dir": str(CODE_LIST_CACHE_DIR),
    }
