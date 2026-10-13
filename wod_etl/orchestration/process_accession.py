"""End-to-end driver: an accession URL in, standardized NetCDF files out.

This is the entry point an agent calls. It walks every phase for every file, records each
outcome in a run manifest, and keeps going when an individual file fails. Nothing here
raises on bad data -- failures become manifest entries so an unattended run completes and
can be inspected afterwards.

    from wod_etl.orchestration import process_accession
    result = process_accession("https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/")

`dataset_type` is optional and normally omitted. An accession is rarely one instrument, so
each file is classified from its own path, header, columns and depth spacing, and files that
cannot become a WOD profile -- currents, plankton, empty columns, documentation -- are
skipped with a stated reason rather than failing four phases later.
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
    PROCESSED_DIR,
    RUN_WOD_QC,
    RAW_DIR,
    describe,
)
from wod_etl.discovery import (
    classify_dataset,
    download_file,
    list_remote_directory,
    read_any,
    sniff_format,
)
from wod_etl.discovery.classify_dataset import ncei_bundle_role
from wod_etl.mapping import generate_field_mapping, query_wod_spec
from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
from wod_etl.qc_transforms import apply_qc_auditing, convert_units_and_types
from wod_etl.verify import validate_intermediate_netcdf, verify_output
from wod_etl.writers import write_ragged_profiles
from wod_etl.orchestration import run_manifest as mf


def process_accession(
    url_or_path: str,
    dataset_type: Optional[str] = None,
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
    dataset_overrides: Optional[Dict[str, str]] = None,
    process_unidentified_as: Optional[str] = None,
    run_qc: Optional[bool] = None,
    verify: bool = True,
) -> Dict[str, Any]:
    """Process every data file in an accession directory.

    dataset_type   -- leave None (the default) to classify every file on its own evidence.
                      Pass a code ("CTD", "OSD", ...) only to FORCE one for the whole run,
                      which is right when the accession really is one instrument and wrong
                      the moment it is not.
    dataset_overrides -- {path_prefix: dataset_type}, e.g. {"3-XBT": "XBT"}. Applied to a
                      file whose key starts with the prefix, overriding classification for
                      that folder only. This is the answer to "the accession is mixed and
                      I know what two of the folders are".
    process_unidentified_as -- what to do with a file that is profile-shaped but names no
                      instrument. None (the default) skips it and says so. Set it to a
                      dataset code to process those files under a stated assumption.
    run_qc         -- whether to run WOD's own observed-level QC. Defaults to config
                      RUN_WOD_QC, which is OFF: QC is applied downstream, and a flag written
                      here is one more thing to reconcile. The ORIGINATOR's flag columns are
                      carried through either way, verbatim and untranslated.
    verify         -- read the written NetCDF back at the end and check every value against
                      the manual's acceptable range for that variable at that depth. On by
                      default, because the failure that matters most is the one that passes
                      structural validation: a variable in the wrong unit validates cleanly.
                      Sets `output_verified`; turn it off only for a run whose output you are
                      about to discard.

    Returns the run summary. Inspect `failures`, `skipped` and the manifest for anything
    that did not complete.
    """
    out_dir = Path(output_dir) if output_dir else PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_root = Path(raw_dir) if raw_dir else RAW_DIR

    listing = list_remote_directory(url_or_path, recursive=recursive,
                                    filter_extension=filter_extension)
    if listing.get("error"):
        # `hint` is the actionable half when the URL itself is the problem -- a metadata
        # landing page rather than a data directory. Dropping it leaves only "error".
        return {"status": "error", "stage": "list_remote_directory",
                "error": listing["error"], "source": url_or_path,
                "hint": listing.get("hint"),
                "accession_number": listing.get("accession_number")}

    candidates = [i for i in listing.get("items", []) if not i["is_dir"]]
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        return {"status": "empty", "source": url_or_path,
                # `hint` carries the diagnosis when the URL itself explains the emptiness --
                # a metadata landing page is not a directory, and "check the URL" sends the
                # reader hunting for a typo that is not there.
                "message": listing.get("hint")
                           or "no files found; check the URL or filter_extension",
                "hint": listing.get("hint"),
                "accession_number": listing.get("accession_number"),
                "listing_total": listing.get("total_items", 0)}

    rid = run_id or mf.new_run_id(url_or_path)
    # The key must identify the file, not just name it. A bare basename collapses every
    # subdirectory's data.csv onto one manifest entry, one cached download and one output
    # file -- so with recursive=True (the default) all but the last were silently lost.
    # Keys are relative to what was actually LISTED: a landing page resolves to its data
    # directory, a versioned accession to its latest version, an archive to its extraction.
    root = listing.get("source") or url_or_path
    items = [{"key": _item_key(root, i["path"], i["name"]),
              "url": i["path"], "size_bytes": i.get("size_bytes")}
             for i in candidates]
    qc = RUN_WOD_QC if run_qc is None else bool(run_qc)
    config = {**describe(), "dataset_type_requested": dataset_type or "auto",
              "dataset_overrides": dataset_overrides or {}, "run_wod_qc": qc}
    manifest = (mf.resume_or_create(rid, url_or_path, items, config)
                if resume else mf.create_manifest(rid, url_or_path, items, config))

    todo = mf.pending_items(manifest, retry_failed=retry_failed)
    if dry_run:
        return _dry_run(rid, url_or_path, todo, manifest, dataset_type, dataset_overrides)

    attempted = failed = 0
    for entry in todo:
        key = entry["key"]

        pre = _prefilter(key, dataset_type, dataset_overrides)
        if pre is not None:
            mf.mark(manifest, key, mf.SKIPPED, **pre)
            continue

        mf.mark(manifest, key, mf.RUNNING)
        try:
            result = process_one_file(entry["url"], dataset_type, out_dir, rid,
                                      raw_root, key=key,
                                      dataset_overrides=dataset_overrides,
                                      process_unidentified_as=process_unidentified_as,
                                      run_qc=qc)
        except Exception as e:                       # noqa: BLE001 - unattended run
            result = {"ok": False, "stage": "unexpected",
                      "error": f"{type(e).__name__}: {e}",
                      "traceback": traceback.format_exc(limit=4)}

        if result.get("skip"):
            # Not a failure: the file was read and found not to be WOD profile data. It must
            # not count toward the abort rate, or an accession that is mostly non-profile
            # data aborts a run that is working exactly as intended.
            mf.mark(manifest, key, mf.SKIPPED, reason=result["reason"],
                    evidence=result.get("evidence"), stage=result.get("stage"))
            continue

        attempted += 1
        if result["ok"]:
            mf.mark(manifest, key, mf.DONE,
                    output_path=result["output_path"], casts=result["casts"],
                    observations=result["observations"], validation=result["validation"],
                    qc_summary=result.get("qc_summary"),
                    dataset_types=result.get("dataset_types"),
                    classification=result.get("classification"),
                    unstorable_columns=result.get("unstorable_columns"),
                    format_family=result.get("format_family"),
                    casts_without_time_of_day=result.get("casts_without_time_of_day"),
                    coded_headers_not_resolved=result.get("coded_headers_not_resolved"))
        else:
            failed += 1
            mf.mark(manifest, key, mf.FAILED, stage=result["stage"], error=result["error"],
                    detail=result.get("detail"))
            if on_error == "stop":
                return _abort(manifest, f"on_error='stop' and {key} failed at "
                                        f"{result['stage']}")

            if (attempted >= ABORT_FAILURE_RATE_MIN_SAMPLE
                    and failed / attempted > ABORT_IF_FAILURE_RATE_ABOVE):
                return _abort(manifest,
                              f"failure rate {failed}/{attempted} exceeded "
                              f"{ABORT_IF_FAILURE_RATE_ABOVE:.0%}; this usually means "
                              f"something systematic is wrong rather than the data being "
                              f"individually bad")

    summary = mf.summarize(manifest)
    summary["status"] = "complete" if summary["complete"] else "incomplete"
    summary.update(_run_report(manifest))
    summary.update(_verify_written_output(out_dir, summary, verify))
    return summary


def _verify_written_output(out_dir: Path, summary: Dict[str, Any],
                           verify: bool) -> Dict[str, Any]:
    """Read the output back and judge the numbers, not just the structure.

    `status: "complete"` says every file was written and passed CF and ragged-array checks.
    It says nothing about whether the values are possible, and the expensive failures are
    exactly the ones that validate cleanly -- a column in the wrong unit, a bottom-depth
    column on the vertical axis, a variable that is entirely fill. So the run checks its own
    output rather than waiting to be asked.

    `output_verified` is a SEPARATE verdict from `status` on purpose: one is about the run
    finishing, the other about the data being possible, and a run can be complete and
    unverified. Report both.
    """
    if not verify:
        return {"output_verified": None,
                "output_verification": {"skipped": "verify=False"}}
    if not summary.get("outputs"):
        return {"output_verified": None,
                "output_verification": {"skipped": "no output was written"}}

    try:
        report = verify_output(str(out_dir))
    except Exception as e:                           # noqa: BLE001 - unattended run
        return {"output_verified": False,
                "output_verification": {"error": f"{type(e).__name__}: {e}"}}

    return {
        "output_verified": report["passed"],
        "output_verification": {
            "passed": report["passed"],
            "finding_counts": report["finding_counts"],
            # Only the findings that mean the data cannot be what it claims are lifted into
            # the summary; the rest stay in `text` so a run report is not drowned by the
            # handful of extreme levels every real cast has.
            "findings": [f for f in report["findings"] if f["severity"] in ("high", "medium")],
            "value_ranges": {n: {"min": v.get("min"), "max": v.get("max"),
                                 "units": v.get("units")}
                             for n, v in report["variables"].items()},
            "flag_distribution": report["flag_distribution"],
            "text": report["text"],
        },
    }


# ------------------------------------------------------------------ per file
def process_one_file(url: str, dataset_type: Optional[str], out_dir: Path, run_id: str,
                     raw_root: Optional[Path] = None,
                     key: Optional[str] = None,
                     dataset_overrides: Optional[Dict[str, str]] = None,
                     process_unidentified_as: Optional[str] = None,
                     run_qc: Optional[bool] = None) -> Dict[str, Any]:
    """Run every phase on one file. Returns {"ok": bool, ...} -- never raises.

    `key` is the file's identity within a run -- its path relative to the accession root.
    It keeps the raw copy, the output name and the classification evidence distinct for two
    files that share a basename. Defaults to the basename, right for a single ad-hoc file.
    """
    out_dir = Path(out_dir)          # callers pass a plain string more often than not
    key = key or os.path.basename(url.rstrip("/")) or url
    subdir = "/".join(filter(None, [run_id, os.path.dirname(key)]))

    # -- fetch and identify ------------------------------------------------
    dl = download_file(url, dest_dir=str(raw_root) if raw_root else None, subdir=subdir)
    if dl["status"] == "error":
        return _fail("download_file", dl["error"])
    local = dl["local_path"]

    fmt = sniff_format(local)
    if fmt.get("is_document"):
        return _skip("sniff_format",
                     f"{fmt['format_family']}: documentation or a metadata container, not "
                     f"measurements",
                     evidence=fmt.get("evidence"))
    if not fmt.get("readable", False):
        # Not a document, and still unreadable. That is a failure, not a skip: something put
        # this file in a data folder and it did not parse as any known format. A silent skip
        # here is how a real cast file with an unrecognised layout disappears from a run.
        return _fail("sniff_format",
                     f"{fmt['format_family']}: {fmt.get('note') or 'no tabular data found'}",
                     detail=fmt.get("evidence"))

    casts, read_info = read_any(local, fmt)
    if read_info.get("error"):
        return _fail("read_any", read_info["error"], detail=read_info.get("warnings"))

    # -- map, convert, QC per cast -----------------------------------------
    built: List[Dict[str, Any]] = []
    qc_summaries, classifications, skipped_casts = [], [], []
    unstorable: List[Dict[str, str]] = []
    for cast in casts:
        cast.setdefault("local_path", local)
        b, verdict = _build_cast(cast, key, dataset_type, dataset_overrides,
                                 process_unidentified_as,
                                 RUN_WOD_QC if run_qc is None else bool(run_qc))
        classifications.append(verdict)
        unstorable.extend(verdict.get("unstorable_columns", []))
        if b is None:
            skipped_casts.append({"cast_id": cast["cast_id"],
                                  "reason": verdict.get("skip_reason", "no usable mapping")})
            continue
        built.append(b)
        qc_summaries.append(b.pop("_qc_summary"))

    if not built:
        # A cast that was excluded on evidence, or holds no measurements at all, is a SKIP:
        # the pipeline read it correctly and there is nothing to standardize. A cast that
        # could not be mapped is a FAILURE and must be reported as one, because it usually
        # means the file needs staging rather than that it holds nothing.
        reasons = sorted({s["reason"] for s in skipped_casts})
        kinds = {c.get("skip_kind") for c in classifications if c.get("skip_kind")}
        if kinds and kinds <= {"excluded", "no_data", "unidentified", "sidecar"}:
            return _skip("classify_dataset" if "excluded" in kinds else "mapping",
                         "; ".join(reasons),
                         evidence=[c.get("evidence") for c in classifications][:3])
        return _fail("mapping",
                     "; ".join(reasons) if reasons else "no cast produced a usable contract",
                     detail=skipped_casts)

    # -- write and validate ------------------------------------------------
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

    types: Dict[str, int] = {}
    for b in built:
        code = b["contract"].get("wod_dataset_code") or "unresolved"
        types[code] = types.get(code, 0) + 1
    date_only = sum(1 for v in classifications
                    if v.get("date_known") and v.get("time_of_day_known") is False)
    code_lookups = _coded_header_report(built)

    return {
        "ok": True,
        "output_path": str(out_path),
        "casts": write["cast_count"],
        "observations": write["total_observations"],
        "validation": {"status": report["status"], "warnings": report["warnings"]},
        "qc_summary": qc_summaries[0] if qc_summaries else None,
        "dataset_types": types,
        "classification": classifications[0] if classifications else None,
        "unstorable_columns": unstorable or None,
        "format_family": fmt["format_family"],
        "skipped_casts": skipped_casts or None,
        "casts_without_time_of_day": date_only or None,
        "coded_headers_not_resolved": code_lookups or None,
    }


def _coded_header_report(built) -> List[Dict[str, Any]]:
    """Coded metadata whose value is not a resolved code, once per (field, value)."""
    seen, out = set(), []
    for b in built:
        contract = b["contract"]
        entries = [(f"secondary header {sid}", e)
                   for sid, e in (contract.get("secondary_headers") or {}).items()]
        for m in contract.get("column_mapping", []):
            for vid, h in (m.get("variable_specific_headers") or {}).items():
                entries.append((f"{m['raw_column']}: variable header {vid}", h))
        for where, e in entries:
            lookup = e.get("code_lookup") or {}
            if not lookup or lookup.get("status") in ("matched", "already_a_code"):
                continue
            k = (where.split(":")[-1], str(e.get("value")))
            if k in seen:
                continue
            seen.add(k)
            out.append({"field": where, "value": e.get("value"),
                        "lookup": lookup.get("summary")})
    return out


def _build_cast(cast, key, dataset_type, dataset_overrides, process_unidentified_as,
                run_qc=RUN_WOD_QC):
    """Classify, map, convert and QC one cast. Returns (built or None, verdict)."""
    df = cast["df"]
    header = cast.get("header_block", "")
    depth_inc = _depth_increment(df)

    verdict = classify_dataset(path=key, columns=[str(c) for c in df.columns],
                               header_text=header, depth_increment_m=depth_inc,
                               n_levels=len(df))
    override = _override_for(key, dataset_overrides)
    chosen, source = _choose_dataset_type(dataset_type, override, verdict,
                                          process_unidentified_as)
    verdict["chosen_dataset_type"] = chosen
    verdict["chosen_by"] = source

    if chosen is None:
        verdict["skip_kind"] = "excluded" if verdict.get("exclusion") else "unidentified"
        verdict["skip_reason"] = (
            f"not WOD profile data: {verdict['exclusion']['reason']}"
            if verdict.get("exclusion") else
            "no instrument identified from path, header, columns or depth spacing")
        return None, verdict

    # A long-format table names its variables in a cell, so its only measurement column is
    # 'value' and maps to nothing. Read as it stands it skipped as "a station list or
    # metadata sidecar", which is wrong twice: it holds measurements, and a file that needs
    # reshaping is a failure to report, not a skip to file away.
    if verdict.get("long_format"):
        verdict["skip_kind"] = "needs_staging"
        verdict["skip_reason"] = verdict["long_format"]["action"]
        return None, verdict

    spec = query_wod_spec(chosen, [str(c) for c in df.columns], depth_increment_m=depth_inc)
    profile = {"source": cast.get("local_path") or cast.get("source_note", ""),
               "source_format": cast.get("source_note", ""), "raw_header_block": header,
               "columns": [str(c) for c in df.columns]}
    contract = generate_field_mapping(profile, spec, df=df)
    pc = contract.get("parsed_coordinates") or {}
    verdict["time_of_day_known"] = pc.get("hour") is not None
    verdict["date_known"] = pc.get("year") is not None
    if not contract["column_mapping"]:
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            f"no column mapped to a WOD code. Columns read: "
            f"{', '.join(str(c) for c in list(df.columns)[:8])}. Either the header row was "
            f"not the row that was read, or none of these are Table 3a variables.")
        return None, verdict

    # Triage question 2, asked here so an unattended run reports what is actually wrong
    # instead of a validation error three phases later. Two different answers:
    measured, empty = _measurement_columns(df, contract)
    if not measured:
        verdict["skip_kind"] = "sidecar"
        verdict["skip_reason"] = (
            "no measured variable: this file carries coordinates and identifiers but "
            "nothing from Table 3a. It is a station list or metadata sidecar, not a cast -- "
            "use it to stage coordinates INTO the cast files that reference it (Phase 0, "
            "wod_etl/staging/), not as input on its own.")
        return None, verdict
    if empty:
        verdict["skip_kind"] = "no_data"
        verdict["skip_reason"] = (
            f"no measurements: every mapped measurement column is empty "
            f"({', '.join(empty)}). This is the 'Not analyzed' case -- the file is "
            f"well-formed and holds nothing to standardize.")
        return None, verdict

    # A cast with no position or no depth axis cannot become a valid profile. Writing it
    # anyway produces a NetCDF that fails validation with an error naming the symptom --
    # "'lat' is entirely missing" -- rather than the cause, so stop here and say where the
    # value has to come from. A missing TIME is only a validator warning, so it does not
    # block: it is recorded and reported instead.
    missing = contract.get("missing_required_coordinates") or []
    blocking = [c for c in missing if c in BLOCKING_COORDINATES]
    if blocking:
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            f"missing required coordinate(s): {', '.join(blocking)}. They are not in the "
            f"data file or its header block. Stage them in first -- from a sibling station "
            f"list, the folder readme, or a cruise report -- and record where each came "
            f"from (Phase 0, wod_etl/staging/).")
        return None, verdict
    verdict["missing_coordinates"] = missing or None

    # Pressure without depth is NOT reported as a missing coordinate -- the contract records
    # it as a warning, because the conversion exists and simply is not automatic. Left alone
    # it produces a NetCDF with no depth axis that fails validation with "'z' is entirely
    # missing", three phases from the thing that has to happen.
    if not any(m["wod_code"] == DEPTH_SENTINEL_CODE for m in contract["column_mapping"]):
        has_pressure = any(m["wod_code"] == 25 for m in contract["column_mapping"])
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            "pressure but no depth: depth is the vertical axis and there is no code for it, "
            "so this cast has none. Convert it during staging with "
            "wod_etl.staging.pressure_to_depth, which needs the cast's latitude, and record "
            "that depth was derived."
            if has_pressure else
            "no depth axis: no column resolves to depth, and there is no pressure column to "
            "derive one from. A profile cannot be written without one.")
        return None, verdict

    df, tlog = convert_units_and_types(df, contract)
    if run_qc:
        df, qc = apply_qc_auditing(df, contract)
    else:
        qc = {"wod_qc": "not run",
              "note": "WOD observed-level QC is off (config RUN_WOD_QC / run_qc=False). No "
                      "Table 12 flags were assigned. The originator's own flag columns are "
                      "carried through unchanged.",
              "originator_flag_columns": [f["flag_column"]
                                          for f in contract.get("originator_flags", [])]}
    return ({"df": df, "contract": contract, "transform_log": tlog, "_qc_summary": qc},
            verdict)


# Coordinates the writer cannot do without. `time` is deliberately absent: the validator
# treats a missing time as a warning, so blocking on it here would reject casts that WOD
# accepts -- and historical casts with a date but no clock time are common.
BLOCKING_COORDINATES = ("latitude", "longitude", "depth")

# Table 3a gives latitude, longitude and julian day variable codes, so the mapping contract
# calls them measurements. They locate a cast; they are not something measured in it, and a
# file whose only "measurements" are these has no data in it at all.
_COORDINATE_CODES = {30, 31, 32}


def _measurement_columns(df, contract):
    """(real measurement columns, those of them that are entirely empty).

    The second list is returned only when EVERY measurement column is empty; a file with
    one empty column among five is normal and must keep going.
    """
    import pandas as pd

    measured = [m["raw_column"] for m in contract["column_mapping"]
                if m.get("role") == "measurement"
                and m.get("wod_code") not in _COORDINATE_CODES
                and m["raw_column"] in df.columns]
    if not measured:
        return [], []
    empty = [c for c in measured
             if pd.to_numeric(df[c], errors="coerce").notna().sum() == 0]
    return measured, (empty if len(empty) == len(measured) else [])


def _choose_dataset_type(explicit, override, verdict, process_unidentified_as):
    """Which dataset type this cast is processed as, and on whose authority.

    Precedence: a per-folder override, then a run-wide explicit type, then the file's own
    evidence. An explicit type deliberately beats an exclusion -- an operator who says
    'this folder is XBT' has looked at it, and the classifier has not.
    """
    if override:
        return override, "dataset_overrides"
    if explicit:
        return explicit, "explicit dataset_type argument"
    if verdict.get("exclusion"):
        return None, "excluded by classification"
    if verdict.get("dataset_type"):
        return verdict["dataset_type"], f"classified ({verdict['confidence']} confidence)"
    if process_unidentified_as:
        return process_unidentified_as, "process_unidentified_as (assumed, not evidence)"
    return None, "unidentified"


def _override_for(key: str, overrides: Optional[Dict[str, str]]) -> Optional[str]:
    if not overrides:
        return None
    norm = str(key).replace("\\", "/")
    best = None
    for prefix, value in overrides.items():
        p = str(prefix).replace("\\", "/").strip("/")
        if norm == p or norm.startswith(p + "/") or p in norm.split("/"):
            if best is None or len(p) > len(best[0]):
                best = (p, value)
    return best[1] if best else None


# ------------------------------------------------------------------ helpers
def _prefilter(key: str, dataset_type, dataset_overrides) -> Optional[Dict[str, Any]]:
    """Skip a file before downloading it, from its path alone.

    Only two things are certain from a name: an extension that is documentation or an
    archive, and a folder that names content WOD cannot store. Everything else waits until
    the bytes are in hand.
    """
    if Path(key).suffix.lower() in NON_DATA_EXTENSIONS:
        return {"reason": f"{Path(key).suffix.lower()} is documentation or a container, "
                          f"not measurements",
                "stage": "prefilter"}
    # NCEI's own bundle files, by name. Downloaded and sniffed, the readme and journal are
    # prose that does not parse -- a FAILURE, since they are not documents by format -- and
    # `<accession>_lonlat.txt` is two numeric columns that parse as a table.
    bundle = ncei_bundle_role(Path(key).name)
    if bundle:
        return {"reason": f"NCEI bundle file: {bundle}", "stage": "prefilter"}
    if dataset_type or _override_for(key, dataset_overrides):
        return None                       # an operator named this folder; do not second-guess
    verdict = classify_dataset(path=key)
    if verdict.get("exclusion") and verdict["exclusion"]["matched_in"] == "path":
        return {"reason": f"not WOD profile data: {verdict['exclusion']['reason']}",
                "route": verdict["exclusion"]["route"],
                "evidence": verdict["exclusion"]["matched"],
                "stage": "prefilter"}
    return None


def _dry_run(rid, source, todo, manifest, dataset_type, dataset_overrides):
    """What would happen, with the dataset each file would be processed as.

    A local file is triaged from its head -- format, header text, columns -- exactly as
    `classify_accession` does, so the answer matches the real run. A dry run on a staged
    folder used to classify by file NAME only, and eleven CTD casts came back
    `dataset_type: null` while the real run called them CTD at high confidence. A remote
    file is still judged by its path, because nothing has been downloaded; each item says
    which it was.
    """
    from wod_etl.discovery import classify_file

    would, skipped, staging, undecided = [], [], [], []
    for entry in todo:
        key = entry["key"]
        pre = _prefilter(key, dataset_type, dataset_overrides)
        if pre is not None:
            skipped.append({"key": key, **pre})
            continue
        override = _override_for(key, dataset_overrides)
        local = os.path.exists(str(entry.get("url") or ""))
        if local:
            v = classify_file(entry["url"], rel=key)
            decided_by = "file head (format, header, columns)"
        else:
            cd = classify_dataset(path=key)
            v = {"action": ("exclude" if cd.get("exclusion") else
                            "process" if cd.get("dataset_type") else "inspect"),
                 "dataset_type": cd.get("dataset_type"), "confidence": cd["confidence"],
                 "reason": (cd.get("exclusion") or {}).get("reason"),
                 "evidence": cd["evidence"]}
            decided_by = "path only (remote; not downloaded)"

        forced = override or dataset_type
        if forced and v["action"] not in ("document",):
            would.append({"key": key, "dataset_type": forced,
                          "decided_by": "dataset_overrides" if override else "explicit argument",
                          "evidence": v.get("evidence", [])[:2]})
        elif v["action"] == "process":
            would.append({"key": key, "dataset_type": v["dataset_type"],
                          "decided_by": f"{decided_by} ({v['confidence']} confidence)",
                          "evidence": v.get("evidence", [])[:2]})
        elif v["action"] == "stage":
            staging.append({"key": key, "reason": v["reason"]})
        elif v["action"] in ("exclude", "document", "sidecar"):
            skipped.append({"key": key, "reason": v.get("reason") or v["action"],
                            "stage": "dry_run", "decided_by": decided_by})
        else:
            undecided.append({"key": key, "reason": v.get("reason"),
                              "decided_by": decided_by})
    return {"status": "dry_run", "run_id": rid, "source": source,
            "would_process": would, "would_skip": skipped,
            "needs_staging": staging, "undecided": undecided,
            "already_done": [k for k, v in manifest["items"].items()
                             if v["status"] in mf.TERMINAL],
            "note": "Local files are triaged from their heads, as the real run would. Remote "
                    "files are judged by path only -- the real run reclassifies them from "
                    "their content, so those types can change. `needs_staging` fails in a "
                    "real run; `undecided` is skipped unless process_unidentified_as is "
                    "set."}


def _run_report(manifest) -> Dict[str, Any]:
    """Dataset mix, skip reasons and dropped columns -- what a run must report."""
    types: Dict[str, int] = {}
    skips: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    no_time: List[str] = []
    date_only: List[str] = []
    unresolved_codes: Dict[str, Any] = {}
    for it in manifest["items"].values():
        if "time" in ((it.get("classification") or {}).get("missing_coordinates") or []):
            no_time.append(it["key"])
        if it.get("casts_without_time_of_day"):
            date_only.append(it["key"])
        for u in (it.get("coded_headers_not_resolved") or []):
            label = f"{u['field'].split(':')[-1].strip()} = {u['value']!r}"
            unresolved_codes.setdefault(label, u["lookup"])
        for code, n in (it.get("dataset_types") or {}).items():
            types[code] = types.get(code, 0) + n
        if it["status"] == mf.SKIPPED and it.get("reason"):
            skips[it["reason"]] = skips.get(it["reason"], 0) + 1
        for u in (it.get("unstorable_columns") or []):
            label = f"{u['column']} ({u['variable']})"
            dropped[label] = dropped.get(label, 0) + 1
    return {"dataset_types": types, "skipped_reasons": skips,
            "unstorable_columns_dropped": dropped,
            "outputs_without_time": no_time or None,
            # A date with no time of day. Not missing -- WOD accepts it -- but it is written
            # with a fill GMT_time, and a reader who looks only at `time` sees 00:00.
            "outputs_without_time_of_day": date_only or None,
            # Coded metadata written as the originator's text because no unique code matched.
            "coded_headers_not_resolved": unresolved_codes or None}


def _abort(manifest, reason: str) -> Dict[str, Any]:
    summary = mf.summarize(manifest)
    summary["status"] = "aborted"
    summary["abort_reason"] = reason
    summary.update(_run_report(manifest))
    # An aborted run still wrote files before it stopped, and those are the ones most worth
    # checking -- whatever went systematically wrong is usually visible in them.
    summary["output_verified"] = None
    summary["output_verification"] = {"skipped": "run aborted; verify the partial output "
                                                 "with verify_output(output_dir)"}
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


def _depth_increment(df) -> Optional[float]:
    """Median spacing of the depth column, used to route CTD/XCTD to CTD or OSD."""
    import numpy as np
    import pandas as pd

    for col in df.columns:
        if match_variable(col) == DEPTH_SENTINEL_CODE:
            z = pd.to_numeric(df[col], errors="coerce").dropna().to_numpy(dtype=float)
            if z.size < 2:
                return None
            diffs = np.diff(np.sort(z))
            diffs = diffs[diffs > 0]
            return float(np.median(diffs)) if diffs.size else None
    return None


def _fail(stage: str, error: str, detail=None) -> Dict[str, Any]:
    return {"ok": False, "stage": stage, "error": error, "detail": detail}


def _skip(stage: str, reason: str, evidence=None) -> Dict[str, Any]:
    """Read successfully, and found not to be WOD profile data. Not a failure."""
    return {"ok": False, "skip": True, "stage": stage, "reason": reason,
            "evidence": evidence}


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(RAW_DIR)
    print(json.dumps(process_accession(target, limit=5, dry_run=True), indent=2))
