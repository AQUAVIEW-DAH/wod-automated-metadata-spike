"""Paths, environment settings and tunable constants for the WOD ETL pipeline.

Most defaults can be overridden through environment variables.
"""
import os
from pathlib import Path

# ----------------------------------------------------------------- paths
# The project root is found by its markers, as an installed package lives elsewhere.


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
    """A data folder from `env` when set, so child processes inherit the parent's folders."""
    value = os.environ.get(env, "").strip()
    return Path(value) if value else default


DATA_DIR = _folder("WOD_DATA_DIR", ROOT / "data")
RAW_DIR = _folder("WOD_RAW_DIR", DATA_DIR / "raw")
STAGING_DIR = _folder("WOD_STAGING_DIR", DATA_DIR / "staging")
PROCESSED_DIR = _folder("WOD_PROCESSED_DIR", DATA_DIR / "processed")
MANIFEST_DIR = _folder("WOD_MANIFEST_DIR", STAGING_DIR / "manifests")
# One folder per accession: run log, pipeline state, decisions, report and work/ inputs.
# Its state describes the outputs under staging/ and processed/, so move them together.
RUNS_DIR = _folder("WOD_RUNS_DIR", DATA_DIR / "runs")

KNOWLEDGE_DIR = ROOT / "knowledge"
CODE_TABLES_DIR = KNOWLEDGE_DIR / "wod_code_tables"
WOD_README_PDF = KNOWLEDGE_DIR / "wod_readme.pdf"

for _d in (RAW_DIR, STAGING_DIR, PROCESSED_DIR, MANIFEST_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------- identity
# Written into each output's `source` and `history`, and sent as the User-Agent.
TOOL_NAME = "wod-etl"


def _tool_version() -> str:
    try:
        from importlib.metadata import version
        return version(TOOL_NAME)
    except Exception:                                   # noqa: BLE001 - not installed
        return "dev"


TOOL_VERSION = _tool_version()

# ------------------------------------------------------------------ HTTP
USER_AGENT = os.environ.get("WOD_USER_AGENT", f"{TOOL_NAME}/{TOOL_VERSION}")
HTTP_TIMEOUT = float(os.environ.get("WOD_HTTP_TIMEOUT", "30"))
PEEK_BYTES = int(os.environ.get("WOD_PEEK_BYTES", "8192"))
DOWNLOAD_CHUNK_BYTES = 1 << 16
MAX_DOWNLOAD_BYTES = int(os.environ.get("WOD_MAX_DOWNLOAD_BYTES", str(2 * 1024**3)))
HTTP_RETRIES = int(os.environ.get("WOD_HTTP_RETRIES", "3"))
HTTP_BACKOFF_SECONDS = 2.0

# --------------------------------------------------------- WOD constants
# Manual p.19: a depth increment under 2 m is high-resolution (CTD); coarser is OSD.
HIGH_RES_DEPTH_INCREMENT_M = 2.0

# Manual p.54: gradient thresholds differ above and below 400 m.
GRADIENT_DEPTH_SPLIT_M = 400.0

# Manual p.54: minimum depth difference for gradients on high-resolution instruments.
MIN_GRADIENT_DEPTH_DIFF_M = 3.0

# Density WOD assumes for per-volume to per-mass conversion when none is reported (manual p.15).
ASSUMED_SEAWATER_DENSITY_KG_M3 = 1025.0
SEAWATER_DENSITY_PROVENANCE = (
    "constant 1025 kg/m3, as WOD uses when the sample density is not reported (manual p.15)"
)

# ---------------------------------------------------------------- output
NETCDF_FILL_VALUE = -9999.0
QC_FILL_VALUE = -1
CF_CONVENTIONS = "CF-1.8"
WOD_VERSION = "WOD23"

# ------------------------------------------------------- WOD code lists
# Coded fields use WOD's online code lists; text becomes a code only on a unique match.
CODE_LIST_CACHE_DIR = _folder("WOD_CODE_LIST_CACHE_DIR",
                              DATA_DIR / "reference_cache" / "wod_code_lists")
CODE_LOOKUP = os.environ.get("WOD_CODE_LOOKUP", "1").strip().lower() in ("1", "true", "yes", "on")
CODE_LIST_FETCH = os.environ.get("WOD_CODE_LIST_FETCH", "1").strip().lower() in (
    "1", "true", "yes", "on")
CODE_LIST_MAX_AGE_DAYS = float(os.environ.get("WOD_CODE_LIST_MAX_AGE_DAYS", "30"))

# ------------------------------------------------- taxa-records (plankton)
# Table 6a Biological Header fields needed to interpret a count (our choice, not a manual
# table). A tuple lists alternatives. A haul missing one is reported, never filled in.
KEY_BIOLOGICAL_FIELD_IDS = (7, 4, 3, (1, 14), 10, 13)

# --------------------------------------------------------- dataset routing
# Dataset type assumed for unidentified files when the caller asks to process them anyway.
DEFAULT_DATASET_TYPE = os.environ.get("WOD_DEFAULT_DATASET_TYPE", "OSD")

# ------------------------------------------------------------------- QC
# WOD observed-level QC (manual Table 12, section III). Off by default, as the data centre
# applies it downstream; originator flags are always kept.
RUN_WOD_QC = os.environ.get("WOD_RUN_QC", "0").strip().lower() in ("1", "true", "yes", "on")

# ------------------------------------------------------- failure policy
# On a per-file failure, "skip" records it and continues; "stop" aborts the run.
ON_FILE_ERROR = os.environ.get("WOD_ON_FILE_ERROR", "skip")

# Abort when this fraction of files fails, which suggests a systematic problem.
ABORT_IF_FAILURE_RATE_ABOVE = float(os.environ.get("WOD_ABORT_FAILURE_RATE", "0.5"))
ABORT_FAILURE_RATE_MIN_SAMPLE = 10

# ------------------------------------------------------- source text
# Text encodings tried in order; cp932 covers Japanese-authored files.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "latin-1")

# Missing-value markers beyond load_tabular's numeric sentinels. Below-detection and
# not-detected also become missing (Table 12 has no flag for them) but are counted.
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

# Images, video and audio: never WOD data and often large, so listed but not downloaded.
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".png", ".jpg", ".jpeg",
    ".gif", ".tif", ".tiff", ".bmp", ".heic", ".mp3", ".wav",
}

# Extensions that are metadata/documentation rather than measurements.
NON_DATA_EXTENSIONS = {
    ".xml", ".pdf", ".html", ".htm", ".md", ".doc", ".docx", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".tar", ".sh",
}


def child_environment() -> dict:
    """Environment variables passing this process's current folders and settings to a child."""
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
