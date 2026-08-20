"""End-to-end driver: an accession URL in, standardized NetCDF files out.

This is the entry point an agent calls. It walks every phase for every file, records each
outcome in a run manifest, and keeps going when an individual file fails. Nothing here
raises on bad data -- failures become manifest entries so an unattended run completes and
can be inspected afterwards.

    from wod_etl.orchestration import process_accession
    result = process_accession("https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/")
"""
import os
import re
import traceback
from pathlib import Path
from typing import Dict, Any, List, Optional

from wod_etl.config import (
    ABORT_FAILURE_RATE_MIN_SAMPLE,
    ABORT_IF_FAILURE_RATE_ABOVE,
    NON_DATA_EXTENSIONS,
    ON_FILE_ERROR,
    PEEK_BYTES,
    PROCESSED_DIR,
    RAW_DIR,
    describe,
)
from wod_etl.discovery import (
    download_file,
    fetch_and_peek_file,
    list_remote_directory,
    load_tabular,
    profile_raw_data,
    split_casts,
)
from wod_etl.discovery.split_casts import cast_as_profile_input
from wod_etl.mapping import generate_field_mapping, query_wod_spec
from wod_etl.qc_transforms import apply_qc_auditing, convert_units_and_types
from wod_etl.verify import validate_intermediate_netcdf
from wod_etl.writers import write_ragged_profiles
from wod_etl.orchestration import run_manifest as mf


def process_accession(
    url_or_path: str,
    dataset_type: str = "OSD",
    output_dir: Optional[str] = None,
    raw_dir: Optional[str] = None,
    run_id: Optional[str] = None,
    recursive: bool = True,
    filter_extension: Optional[str] = None,
    limit: Optional[int] = None,
    resume: bool = True,
    retry_failed: bool = False,
    dry_run: bool = False,
    on_error: str = ON_FILE_ERROR,
) -> Dict[str, Any]:
    """Process every data file in an accession directory.

    dataset_type is the instrument/dataset hint from the user's instructions ("XCTD",
    "CTD", "OSD", ...). For XCTD and CTD the real WOD dataset is decided per cast from the
    observed depth increment, per manual p.19.

    Returns the run summary. Inspect `failures` and the manifest for anything that did not
    complete.
    """
    out_dir = Path(output_dir) if output_dir else PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_root = Path(raw_dir) if raw_dir else RAW_DIR

    listing = list_remote_directory(url_or_path, recursive=recursive,
                                    filter_extension=filter_extension)
    if listing.get("error"):
        return {"status": "error", "stage": "list_remote_directory",
                "error": listing["error"], "source": url_or_path}

    candidates = [i for i in listing.get("items", []) if not i["is_dir"]]
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        return {"status": "empty", "source": url_or_path,
                "message": "no files found; check the URL or filter_extension",
                "listing_total": listing.get("total_items", 0)}

    rid = run_id or mf.new_run_id(url_or_path)
    # The key must identify the file, not just name it. A bare basename collapses every
    # subdirectory's data.csv onto one manifest entry, one cached download and one output
    # file -- so with recursive=True (the default) all but the last were silently lost.
    items = [{"key": _item_key(url_or_path, i["path"], i["name"]),
              "url": i["path"], "size_bytes": i.get("size_bytes")}
             for i in candidates]
    manifest = (mf.resume_or_create(rid, url_or_path, items, describe())
                if resume else mf.create_manifest(rid, url_or_path, items, describe()))

    todo = mf.pending_items(manifest, retry_failed=retry_failed)
    if dry_run:
        return {"status": "dry_run", "run_id": rid, "source": url_or_path,
                "would_process": [i["key"] for i in todo],
                "already_done": [k for k, v in manifest["items"].items()
                                 if v["status"] in mf.TERMINAL]}

    attempted = failed = 0
    for entry in todo:
        key = entry["key"]

        if _is_non_data(key):
            mf.mark(manifest, key, mf.SKIPPED, reason="not a measurement file (metadata/doc extension)")
            continue

        mf.mark(manifest, key, mf.RUNNING)
        attempted += 1
        try:
            result = process_one_file(entry["url"], dataset_type, out_dir, rid,
                                      raw_root, key=key)
        except Exception as e:                       # noqa: BLE001 - unattended run
            result = {"ok": False, "stage": "unexpected",
                      "error": f"{type(e).__name__}: {e}",
                      "traceback": traceback.format_exc(limit=4)}

        if result["ok"]:
            mf.mark(manifest, key, mf.DONE,
                    output_path=result["output_path"], casts=result["casts"],
                    observations=result["observations"], validation=result["validation"],
                    qc_summary=result.get("qc_summary"))
        else:
            failed += 1
            mf.mark(manifest, key, mf.FAILED, stage=result["stage"], error=result["error"],
                    detail=result.get("detail"))
            if on_error == "stop":
                summary = mf.summarize(manifest)
                summary["status"] = "aborted"
                summary["abort_reason"] = f"on_error='stop' and {key} failed at {result['stage']}"
                return summary

            if (attempted >= ABORT_FAILURE_RATE_MIN_SAMPLE
                    and failed / attempted > ABORT_IF_FAILURE_RATE_ABOVE):
                summary = mf.summarize(manifest)
                summary["status"] = "aborted"
                summary["abort_reason"] = (
                    f"failure rate {failed}/{attempted} exceeded "
                    f"{ABORT_IF_FAILURE_RATE_ABOVE:.0%}; this usually means something "
                    f"systematic is wrong rather than individually bad files")
                return summary

    summary = mf.summarize(manifest)
    summary["status"] = "complete" if summary["complete"] else "incomplete"
    return summary


def _item_key(root: str, path: str, name: str) -> str:
    """A file's identity within a run: its path relative to the accession root."""
    root_norm = root.replace("\\", "/").rstrip("/")
    path_norm = path.replace("\\", "/")
    if path_norm.startswith(root_norm + "/"):
        return path_norm[len(root_norm) + 1:]
    return name


def _output_stem(key: str) -> str:
    """'stationA/data.csv' -> 'stationA__data'. A flat key is unchanged."""
    stem = key[:-len(Path(key).suffix)] if Path(key).suffix else key
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem.replace("/", "__")).strip("_") or "cast"


def process_one_file(url: str, dataset_type: str, out_dir: Path, run_id: str,
                     raw_root: Optional[Path] = None,
                     key: Optional[str] = None) -> Dict[str, Any]:
    """Run all four phases on one file. Returns {"ok": bool, ...} -- never raises.

    `key` is the file's identity within a run -- its path relative to the accession root.
    It keeps the raw copy, and the output name, distinct for two files that share a
    basename. Defaults to the basename, which is right for a single ad-hoc file.
    """
    key = key or os.path.basename(url.rstrip("/")) or url
    subdir = "/".join(filter(None, [run_id, os.path.dirname(key)]))

    # -- Phase 1: fetch ----------------------------------------------------
    dl = download_file(url, dest_dir=str(raw_root) if raw_root else None, subdir=subdir)
    if dl["status"] == "error":
        return _fail("download_file", dl["error"])
    local = dl["local_path"]

    peek = fetch_and_peek_file(local, num_bytes=PEEK_BYTES)
    if peek.get("error"):
        return _fail("fetch_and_peek_file", peek["error"])

    profile = profile_raw_data(peek["preview_text"], local)
    if profile.get("error"):
        return _fail("profile_raw_data", profile["error"])
    if profile.get("format_family") == "unstructured_text_or_metadata":
        return _fail("profile_raw_data", "no tabular data found; looks like documentation")

    # -- split casts -------------------------------------------------------
    split = split_casts(local, profile)
    if split.get("error") or not split["casts"]:
        return _fail("split_casts", split.get("error", "no casts found"))

    built: List[Dict[str, Any]] = []
    qc_summaries = []
    for cast in split["casts"]:
        b = _build_cast(cast, local, dataset_type, profile, single=not split["multi_cast"])
        if b is None:
            continue
        built.append(b)
        qc_summaries.append(b.pop("_qc_summary"))

    if not built:
        return _fail("mapping", "no cast produced a usable mapping contract; "
                                "check unmapped_columns and missing_required_coordinates")

    # -- Phase 4: write and validate ---------------------------------------
    # Flattened so the output stays one directory that `audit_dataset` can glob, but keyed
    # on the full relative path so two stationX/data.csv do not overwrite each other.
    out_path = out_dir / f"{_output_stem(key)}.nc"
    write = write_ragged_profiles(built, str(out_path))
    if write.get("status") != "success":
        return _fail("generate_netcdf_ragged", write.get("error", "write failed"))

    report = validate_intermediate_netcdf(str(out_path))
    if not report["passed"]:
        return _fail("validate_intermediate_netcdf",
                     f"{report['error_count']} validation error(s)",
                     detail=report["errors"])

    return {
        "ok": True,
        "output_path": str(out_path),
        "casts": write["cast_count"],
        "observations": write["total_observations"],
        "validation": {"status": report["status"], "warnings": report["warnings"]},
        "qc_summary": qc_summaries[0] if qc_summaries else None,
    }


def _build_cast(cast, local, dataset_type, file_profile, single: bool):
    """Profile, map, convert and QC one cast. Returns None if it cannot be mapped."""
    text = cast_as_profile_input(cast)
    profile = profile_raw_data(text, local) if not single else dict(file_profile)
    if profile.get("error"):
        return None
    profile["raw_header_block"] = cast["header_block"] or profile.get("raw_header_block", "")

    if single:
        df, info = load_tabular(local, profile)
    else:
        df, info = _frame_from_text(cast, profile)
    if df is None:
        return None

    depth_inc = _depth_increment(df)
    spec = query_wod_spec(dataset_type, list(df.columns), depth_increment_m=depth_inc)
    contract = generate_field_mapping({**profile, "columns": list(df.columns)}, spec)
    if not contract["column_mapping"]:
        return None

    df, tlog = convert_units_and_types(df, contract)
    df, qc = apply_qc_auditing(df, contract)
    return {"df": df, "contract": contract, "transform_log": tlog, "_qc_summary": qc}


def _frame_from_text(cast, profile):
    """Parse one cast's data block, which lives in memory rather than on disk."""
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "cast.txt"
    tmp.write_text(cast["data_text"])
    return load_tabular(str(tmp), profile, header_lines=0)


def _depth_increment(df) -> Optional[float]:
    """Median spacing of the depth column, used to route CTD/XCTD to CTD or OSD."""
    import numpy as np
    import pandas as pd
    from wod_etl.mapping.query_wod_spec import match_variable, DEPTH_SENTINEL_CODE

    for col in df.columns:
        if match_variable(col) == DEPTH_SENTINEL_CODE:
            z = pd.to_numeric(df[col], errors="coerce").dropna().to_numpy(dtype=float)
            if z.size < 2:
                return None
            diffs = np.diff(np.sort(z))
            diffs = diffs[diffs > 0]
            return float(np.median(diffs)) if diffs.size else None
    return None


def _is_non_data(name: str) -> bool:
    return Path(name).suffix.lower() in NON_DATA_EXTENSIONS


def _fail(stage: str, error: str, detail=None) -> Dict[str, Any]:
    return {"ok": False, "stage": stage, "error": error, "detail": detail}


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(RAW_DIR)
    print(json.dumps(process_accession(target, dataset_type="CTD", limit=5, dry_run=True), indent=2))
