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
import hashlib
import json
import logging
import os
import pickle
import re
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
from wod_etl.discovery.list_remote_directory import accession_for
from wod_etl.mapping import generate_field_mapping, query_wod_spec
from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
from wod_etl.qc_transforms import apply_qc_auditing, convert_units_and_types
from wod_etl.verify import validate_intermediate_netcdf, verify_output
from wod_etl.writers import group_by_file, is_wod_file_name, write_ragged_profiles
from wod_etl.writers.generate_netcdf_ragged import TITLE as WRITER_TITLE
from wod_etl.writers.wod_files import folder_scope
from wod_etl.orchestration import run_manifest as mf
from wod_etl import runlog

log = logging.getLogger(__name__)

# Each source file's result, under `output_dir` unless `per_file_dir` names another place:
# its NetCDF (validated on its own) and its casts, from which the WOD files are assembled.
PER_FILE_DIR = "per_file"
CACHE_SUFFIX = ".casts.pkl"
CACHE_VERSION = 1


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
    include: Optional[List[str]] = None,
    accession: Optional[str] = None,
    file_options: Optional[Dict[str, Dict[str, Any]]] = None,
    per_file_dir: Optional[str] = None,
    assemble: bool = True,
) -> Dict[str, Any]:
    """Process every data file in an accession directory into WOD files.

    The output is WOD's layout: one file per WOD dataset and year, `wod_ctd_2008_0062621.nc`
    (`writers.wod_files`), in `output_dir`; input that is no accession is named for its folder.
    Each source file is processed and validated on its own first -- so a file that fails is
    that file's failure, and a rerun resumes file by file -- and the WOD files are assembled
    from the results of the files this call answers for (every file listed and included),
    never from anything else in a shared folder.

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
    include        -- process only these keys (paths relative to the listed root). Everything
                      else in the listing is left out of the manifest entirely. Keys asked for
                      and not found are returned in `include_missing`. The accession pipeline
                      uses this to process the files that need no staging in place.
    accession      -- the accession this run belongs to, for its log in data/runs/<acc>/,
                      and written into every cast that does not name its own. Worked out
                      from the source when omitted; with none, nothing is logged.
    file_options   -- {key: {"drop": [variable, ...], "metadata": {Key: value}}}: variables
                      of a file not to write, and header values for its casts -- what a
                      reviewed decisions.toml says about the file.
    per_file_dir   -- where each source file's own result goes, in a folder per run:
                      `<per_file_dir>/<run_id>/`, `<output_dir>/per_file/<run_id>/` by default.
    assemble       -- write the WOD files at the end (the default). The accession pipeline
                      turns it off to process two sets of files and assemble them together
                      (`assemble_profiles` with both runs' `per_file_casts`).

    Returns the run summary: `outputs` are the WOD files written, `per_file_outputs` each
    source file's own NetCDF and `per_file_casts` the results they were assembled from.
    Inspect `failures`, `skipped`, `assembly` and the manifest for anything that did not
    complete.
    """
    args = {k: v for k, v in locals().items()}
    owned = runlog.active() is None
    loggable = {k: v for k, v in args.items()
                if k == "url_or_path" or v not in (None, False, {}, [])}
    with runlog.accession_log_for(url_or_path, accession, actor="library",
                                  arguments={"call": "process_accession", **loggable}) as ctx:
        result = _process_accession(**args)
        _log_summary(result)
        if owned and ctx is not None:
            ctx["status"] = result.get("status")
    return result


def _process_accession(
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
    include: Optional[List[str]] = None,
    accession: Optional[str] = None,
    file_options: Optional[Dict[str, Dict[str, Any]]] = None,
    per_file_dir: Optional[str] = None,
    assemble: bool = True,
) -> Dict[str, Any]:
    """The body of `process_accession`, run inside the accession's log."""
    out_dir = Path(output_dir) if output_dir else PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_root = Path(raw_dir) if raw_dir else RAW_DIR

    listing = list_remote_directory(url_or_path, recursive=recursive,
                                    filter_extension=filter_extension)
    if listing.get("error"):
        runlog.event(log, logging.ERROR, f"could not list {url_or_path}: {listing['error']}",
                     kind="listing_error", hint=listing.get("hint"))
        # `hint` is the actionable half when the URL itself is the problem -- a metadata
        # landing page rather than a data directory. Dropping it leaves only "error".
        return {"status": "error", "stage": "list_remote_directory",
                "error": listing["error"], "source": url_or_path,
                "hint": listing.get("hint"),
                "accession_number": listing.get("accession_number")}

    candidates = [i for i in listing.get("items", []) if not i["is_dir"]]
    include_missing = None
    root0 = listing.get("source") or url_or_path
    present = {_norm_source(i["path"]) for i in candidates}
    if include is not None:
        wanted = {str(k).replace("\\", "/").strip("/") for k in include}
        candidates = [i for i in candidates
                      if _item_key(root0, i["path"], i["name"]) in wanted]
        found = {_item_key(root0, i["path"], i["name"]) for i in candidates}
        include_missing = sorted(wanted - found) or None
    # What this call answers for: every file listed and included, whether or not `limit`
    # lets it be processed now. Its WOD files hold these files' casts and no others.
    listed = {_item_key(root0, i["path"], i["name"]) for i in candidates}
    answered = {_norm_source(i["path"]) for i in candidates}
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
    # Each run's per-file results apart: two runs into one output_dir -- two folders of an
    # accession, each with its own cast.csv -- must not overwrite each other's.
    per_dir = (Path(per_file_dir) if per_file_dir else out_dir / PER_FILE_DIR) / rid
    # The key must identify the file, not just name it. A bare basename collapses every
    # subdirectory's data.csv onto one manifest entry, one cached download and one output
    # file -- so with recursive=True (the default) all but the last were silently lost.
    # Keys are relative to what was actually LISTED: a landing page resolves to its data
    # directory, a versioned accession to its latest version, an archive to its extraction.
    root = listing.get("source") or url_or_path
    items = [{"key": _item_key(root, i["path"], i["name"]),
              "url": i["path"], "size_bytes": i.get("size_bytes")}
             for i in candidates]
    stems = _result_stems(it["key"] for it in items)
    qc = RUN_WOD_QC if run_qc is None else bool(run_qc)
    config = {**describe(), "dataset_type_requested": dataset_type or "auto",
              "dataset_overrides": dataset_overrides or {}, "run_wod_qc": qc}
    manifest = (mf.resume_or_create(rid, url_or_path, items, config)
                if resume else mf.create_manifest(rid, url_or_path, items, config))
    # A file done in an earlier run whose result is gone -- deleted, or from before results
    # were kept per file -- is done again: the WOD files are assembled from those results.
    # So is a key that now names another file: a run id reused on another folder.
    urls = {it["key"]: it["url"] for it in items}
    for it in list(manifest["items"].values()):
        moved = it["key"] in urls and it.get("url") != urls[it["key"]]
        if moved or (it["status"] == mf.DONE
                     and not Path(it.get("cache_path") or "").is_file()):
            mf.mark(manifest, it["key"], mf.PENDING, output_path=None, cache_path=None,
                    url=urls.get(it["key"], it.get("url")),
                    note=("the key now names another file" if moved else
                          "its per-file result is not on disk") + "; processed again")

    todo = mf.pending_items(manifest, retry_failed=retry_failed)
    runlog.event(log, logging.INFO,
                 f"run {rid}: {len(manifest['items'])} file(s) listed under "
                 f"{listing.get('source') or url_or_path}; {len(todo)} to process"
                 + (" (dry run)" if dry_run else ""),
                 kind="run_start", run_id=rid, source=listing.get("source"),
                 listing={k: listing.get(k) for k in ("resolution", "version",
                                                      "extracted_from") if listing.get(k)},
                 to_process=len(todo))
    if dry_run:
        return _dry_run(rid, url_or_path, todo, manifest, dataset_type, dataset_overrides)

    attempted = failed = 0
    for entry in todo:
        key = entry["key"]

        pre = _prefilter(key, dataset_type, dataset_overrides)
        if pre is not None:
            mf.mark(manifest, key, mf.SKIPPED, **pre)
            runlog.event(log, logging.INFO, f"skipped {key}: {pre['reason']}", kind="file",
                         key=key, status="skipped", reason=pre["reason"])
            continue

        mf.mark(manifest, key, mf.RUNNING)
        try:
            result = process_one_file(entry["url"], dataset_type, per_dir, rid,
                                      raw_root, key=key, stem=stems.get(key),
                                      dataset_overrides=dataset_overrides,
                                      process_unidentified_as=process_unidentified_as,
                                      run_qc=qc, accession=accession,
                                      options=(file_options or {}).get(key))
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
            runlog.event(log, logging.INFO, f"skipped {key}: {result['reason']}", kind="file",
                         key=key, status="skipped", reason=result["reason"])
            continue

        attempted += 1
        if result["ok"]:
            mf.mark(manifest, key, mf.DONE,
                    output_path=result["output_path"], cache_path=result["cache_path"],
                    casts=result["casts"],
                    observations=result["observations"], validation=result["validation"],
                    qc_summary=result.get("qc_summary"),
                    dataset_types=result.get("dataset_types"),
                    classification=result.get("classification"),
                    unstorable_columns=result.get("unstorable_columns"),
                    format_family=result.get("format_family"),
                    casts_without_time_of_day=result.get("casts_without_time_of_day"),
                    coded_headers_not_resolved=result.get("coded_headers_not_resolved"),
                    skipped_casts=result.get("skipped_casts"),
                    levels_without_data=result.get("levels_without_data"),
                    casts_depth_from_pressure=result.get("casts_depth_from_pressure"))
            not_written = sorted({f"{u['column']} ({u['variable']})"
                                  for u in result.get("unstorable_columns") or []})
            runlog.event(log, logging.INFO,
                         f"done {key}: {result['casts']} cast(s), {result['observations']} "
                         f"level(s) as {result.get('dataset_types')} -> {result['output_path']}"
                         + (f"; not written: {'; '.join(not_written)}" if not_written else ""),
                         kind="file", key=key, status="done", output=result["output_path"],
                         dataset_types=result.get("dataset_types"),
                         columns_not_written=not_written or None,
                         casts_not_written=len(result.get("skipped_casts") or []) or None)
        else:
            failed += 1
            # The traceback of an unexpected exception is the one thing that says where the
            # code broke; it used to be built here and then dropped.
            mf.mark(manifest, key, mf.FAILED, stage=result["stage"], error=result["error"],
                    detail=result.get("detail"), traceback=result.get("traceback"))
            runlog.event(log, logging.WARNING,
                         f"FAILED {key} at {result['stage']}: {result['error']}", kind="file",
                         key=key, status="failed", stage=result["stage"],
                         error=result["error"], detail=result.get("detail"),
                         traceback=result.get("traceback"))
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
    summary["per_file_outputs"] = summary["outputs"]
    # The results of the files this call answers for -- never whatever else a shared folder
    # holds: another accession's, a renamed file's, a file no longer listed.
    summary["per_file_casts"] = list(dict.fromkeys(
        it["cache_path"] for key, it in manifest["items"].items()
        if key in listed and it["status"] == mf.DONE and it.get("cache_path")))
    if assemble and limit:
        # A trial on a few files is for looking at, not for output: assembled, it would
        # replace an earlier full run's WOD files with a few files' casts.
        summary["outputs"] = summary["per_file_outputs"]
        summary["assembly"] = {"skipped": f"limit={limit}: a trial run is not assembled; "
                                          f"its per-file results are per_file_outputs"}
    elif assemble:
        _remove_old_layout(manifest, out_dir)
        scope = accession or accession_for(url_or_path) or folder_scope(root)
        base = _norm_source(root).rstrip("/\\")

        def owns(source: str) -> bool:
            """Casts this call may replace: those of the files it answers for, and of files
            that were under its root and are gone from it. Another run's -- another folder,
            another include batch -- are left where they are."""
            src = _norm_source(source)
            under = src.startswith(base + "/") or src.startswith(base + os.sep)
            return src in answered or (under and src not in present)
        assembly = assemble_profiles(summary["per_file_casts"], out_dir, scope, owns=owns)
        summary["outputs"] = [f["path"] for f in assembly["files"]]
        summary["assembly"] = assembly
        if assembly["problems"]:
            summary["status"] = "incomplete"
    summary.update(_verify_written_output(out_dir, summary, verify))
    if include is not None:
        summary["include_missing"] = include_missing
    # How the input was turned into a listing: a landing page resolved, a version chosen, an
    # archive extracted. It used to be returned only when the listing failed.
    summary["listing"] = {k: listing[k] for k in ("resolved_from", "resolution", "version",
                                                  "extracted_from") if listing.get(k)} or None
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
        # The files this run wrote, not whatever else is in the folder.
        report = verify_output(str(out_dir), paths=summary["outputs"])
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


# ------------------------------------------------------------------ WOD files
def assemble_profiles(caches: List[str], output_dir, scope: str,
                      owns: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    """Source files' results, written as WOD files in `output_dir`: one per WOD dataset and
    year, `wod_ctd_2008_<scope>.nc` (`writers.wod_files`).

    `caches` are the results to write -- each source file's `<stem>.casts.pkl`, as
    `process_accession` returns them in `per_file_casts` -- and nothing else is assembled.
    `scope` names the files: the accession, or for input that is not one, its folder's name.
    It is required: without it the names would be WOD's own (`wod_ctd_2008.nc`).
    `owns(source)` says whether this call answers for a source file's casts. A WOD file of this
    scope already in `output_dir` is replaced or removed only if it does for every source the
    file holds; one holding another run's sources is left as it is, and said. `None`: the
    caller owns every file of this scope there (the accession pipeline does).

    The casts go through the same writer a single source's do, so what every cast of a file
    shares is a file attribute and what differs -- the source file, its notes, a column's
    name -- is written per cast. Each file is validated before it replaces the last one of its
    name. Files are written one at a time, so an accession is never held in memory whole.

    Returns `{"files": [{"path", "name", "dataset", "year", "casts", "observations",
    "sources": [{"key", "casts"}], "warnings"}], "problems": [...], "removed": [...]}`.
    """
    if not str(scope or "").strip():
        raise ValueError("assemble_profiles needs a scope -- the accession, or a folder's "
                         "name -- or its files would carry WOD's own names")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    owned = (lambda path: True) if owns is None else (lambda path: _holds_only(path, owns))
    # Which WOD file each source's casts belong in, read first without keeping the casts.
    index, problems = [], []
    for path in map(Path, caches):
        try:
            payload = _load_casts(path)
        except Exception as e:                       # noqa: BLE001 - reported, not raised
            problems.append({"source": path.name[:-len(CACHE_SUFFIX)], "stage": "assemble",
                             "error": f"its per-file result cannot be read back: "
                                      f"{type(e).__name__}: {e}"})
            continue
        index.append((path, payload["key"],
                      {g["name"] for g in group_by_file([payload], scope)}))
        del payload

    files, written = [], set()
    for name in sorted(set().union(*(names for _, _, names in index))):
        target = out / name
        keys = [key for _, key, names in index if name in names]
        if target.exists() and not owned(target):
            problems.append({"file": name, "sources": keys, "stage": "assemble",
                             "error": f"{target} holds casts of source files this run does "
                                      f"not cover, so it was left as it is. Write each set of "
                                      f"files to its own output_dir, or process them in one "
                                      f"run"})
            continue
        sources = []
        for path, key, names in index:
            if name in names:
                payload = _load_casts(path)
                sources += [{"key": key, "casts": g["casts"]}
                            for g in group_by_file([payload], scope) if g["name"] == name]
        [group] = group_by_file(sources, scope)
        part = target.with_name(target.name + ".part")
        problem = None
        write = write_ragged_profiles(group["casts"], str(part))
        if write.get("status") != "success":
            problem = {"stage": "generate_netcdf_ragged",
                       "error": write.get("error", "write failed")}
        else:
            check = validate_intermediate_netcdf(str(part))
            if not check["passed"]:
                problem = {"stage": "validate_intermediate_netcdf",
                           "error": f"{check['error_count']} validation error(s)",
                           "detail": check["errors"]}
        if problem:
            part.unlink(missing_ok=True)
            problems.append({"file": name, "sources": keys, **problem})
            runlog.event(log, logging.WARNING, f"FAILED {name} at {problem['stage']}: "
                                               f"{problem['error']}",
                         kind="wod_file", name=name, status="failed", **problem)
            continue
        os.replace(part, target)
        written.add(name)
        files.append({"path": str(target), "name": name, "dataset": group["dataset"],
                      "year": group["year"], "casts": write["cast_count"],
                      "observations": write["total_observations"],
                      "sources": group["sources"], "warnings": check["warnings"] or None})
        runlog.event(log, logging.INFO,
                     f"wrote {name}: {write['cast_count']} {group['dataset']} cast(s) of "
                     f"{group['year'] or 'no date'} from {len(group['sources'])} source "
                     f"file(s)", kind="wod_file", name=name, status="written",
                     dataset=group["dataset"], year=group["year"], casts=write["cast_count"],
                     sources=[s["key"] for s in group["sources"]])

    # A file of this scope not written now is not this run's output -- a year whose casts
    # moved, a dataset the review changed, a file that failed above -- if it is this run's
    # to remove. A `.part` is a write that did not finish.
    removed = []
    for p in sorted(out.iterdir()):
        if not p.is_file():
            continue
        unfinished = p.name.endswith(".part") and is_wod_file_name(p.name[:-5], scope)
        if unfinished or (is_wod_file_name(p.name, scope) and p.name not in written
                          and not any(q.get("file") == p.name for q in problems
                                      if q.get("stage") == "assemble")
                          and owned(p)):
            p.unlink()
            removed.append(p.name)
    if removed:
        runlog.event(log, logging.INFO, f"removed {len(removed)} earlier WOD file(s) this run "
                                        f"does not write: {', '.join(removed)}",
                     kind="wod_file", status="removed", names=removed)
    return {"files": files, "problems": problems, "removed": removed or None}


def _sources_of(path: Path) -> Optional[List[str]]:
    """The source files a WOD file this package wrote holds casts from; None for any other
    file -- WOD's own, one written by something else, one that does not open."""
    import netCDF4

    try:
        with netCDF4.Dataset(str(path)) as ds:
            if getattr(ds, "title", None) != WRITER_TITLE:
                return None
            if "source_file" in ds.variables:
                return [str(x) for x in ds.variables["source_file"][:]]
            return [str(ds.source_file)] if "source_file" in ds.ncattrs() else None
    except (OSError, RuntimeError, KeyError, ValueError):
        return None


def _holds_only(path: Path, owns: Callable[[str], bool]) -> bool:
    """Whether every source the file holds casts from is one `owns` answers for."""
    sources = _sources_of(path)
    return sources is not None and all(owns(s) for s in sources)


def _norm_source(source: str) -> str:
    """A source as it can be compared: a URL as it is, a path resolved -- the same folder
    reached through a symlink, or spelled relative, is the same folder."""
    text = str(source)
    return text if "://" in text else os.path.realpath(text)


def _load_casts(path: Path) -> Dict[str, Any]:
    with open(path, "rb") as f:
        payload = pickle.load(f)
    if payload.get("version") != CACHE_VERSION:
        raise ValueError(f"written by another version (cache version "
                         f"{payload.get('version')}, this one reads {CACHE_VERSION})")
    return payload


def _save_casts(path: Path, key: str, source: str, built: List[Dict[str, Any]]) -> None:
    """One file's casts as the writer takes them, for `assemble_profiles`: re-reading the
    NetCDF would lose what the writer decides only when it sees every cast of a file. The
    pipeline's own working state -- written and read here only."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump({"version": CACHE_VERSION, "key": key, "source": source, "casts": built},
                    f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _remove_old_layout(manifest: Dict[str, Any], out_dir: Path) -> None:
    """Output this function wrote before the WOD files: one `<source path>.nc` per source
    file, directly in `output_dir`. Removed only where it is exactly that -- written by this
    package, from that item's own source file -- since left, it would be read beside the WOD
    files holding the same casts."""
    for key, item in manifest["items"].items():
        old = out_dir / f"{_output_stem(key)}.nc"
        if not old.is_file() or is_wod_file_name(old.name):
            continue
        if _sources_of(old) != [str(item.get("url"))]:
            continue
        old.unlink()
        runlog.event(log, logging.INFO, f"removed {old.name}: one source file's output as "
                                        f"written before the WOD files",
                     kind="wod_file", status="removed", names=[old.name])


# ------------------------------------------------------------------ per file
def process_one_file(url: str, dataset_type: Optional[str], out_dir: Path, run_id: str,
                     raw_root: Optional[Path] = None,
                     key: Optional[str] = None,
                     dataset_overrides: Optional[Dict[str, str]] = None,
                     process_unidentified_as: Optional[str] = None,
                     run_qc: Optional[bool] = None,
                     accession: Optional[str] = None,
                     options: Optional[Dict[str, Any]] = None,
                     stem: Optional[str] = None) -> Dict[str, Any]:
    """Run every phase on one file. Returns {"ok": bool, ...} -- never raises.

    `key` is the file's identity within a run -- its path relative to the accession root.
    It keeps the raw copy, the output name and the classification evidence distinct for two
    files that share a basename. Defaults to the basename, right for a single ad-hoc file.
    `accession` is written into every cast that does not name its own (secondary header 1);
    `options` carries the file's reviewed `drop` and `metadata` (see process_accession).
    `stem` names its result files, `<stem>.nc` and `<stem>.casts.pkl`; from the key by default.
    """
    out_dir = Path(out_dir)          # callers pass a plain string more often than not
    key = key or os.path.basename(url.rstrip("/")) or url
    subdir = "/".join(filter(None, [run_id, os.path.dirname(key)]))
    # Keyed on the full relative path, extension and all: two stationX/data.csv, or a .cnv
    # and a .btl of one cast, must not write each other's result.
    stem = stem or _result_stem(key)
    out_path = out_dir / f"{stem}.nc"
    cache_path = out_dir / f"{stem}{CACHE_SUFFIX}"
    # A file processed again replaces its last result or loses it: what fails now must not
    # be assembled from what was written before.
    for stale in (out_path, cache_path):
        stale.unlink(missing_ok=True)

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
        # The file as it is in the accession, not the working copy it was read from.
        cast.setdefault("origin", url)
        if accession and not re.search(r"^\s*#?\s*accession\b", cast.get("header_block") or "",
                                       re.IGNORECASE | re.MULTILINE):
            cast["header_block"] = (cast.get("header_block") or "") + f"\nACCESSION: {accession}"
        # Header values the review supplied go BEFORE the file's own lines: the parser keeps
        # the first value it meets, and a reviewed decision corrects what the file states
        # (a generic `Instrument: CTD` becomes the model the reviewer names).
        review = "\n".join(f"{k}: {v}" for k, v in ((options or {}).get("metadata")
                                                    or {}).items())
        if review:
            cast["header_block"] = review + "\n" + (cast.get("header_block") or "")
        b, verdict = _build_cast(cast, key, dataset_type, dataset_overrides,
                                 process_unidentified_as,
                                 RUN_WOD_QC if run_qc is None else bool(run_qc),
                                 drop=(options or {}).get("drop"))
        classifications.append(verdict)
        if b is None:
            skipped_casts.append({"cast_id": cast["cast_id"], "when": verdict.get("when"),
                                  "reason": verdict.get("skip_reason", "no usable mapping")})
            runlog.event(log, logging.INFO, f"{key}: cast {cast['cast_id']} not written: "
                                            f"{skipped_casts[-1]['reason']}",
                         kind="cast_skipped", key=key, cast=cast["cast_id"],
                         reason=skipped_casts[-1]["reason"])
            continue
        b["_source_cast"] = cast["cast_id"]
        b["_unstorable"] = verdict.get("unstorable_columns", [])
        b["column_attributes"] = cast.get("column_attributes")
        b["_levels_dropped"] = verdict.get("levels_without_data") or 0
        b["_from_pressure"] = bool(verdict.get("depth_from_pressure"))
        built.append(b)
        qc_summaries.append(b.pop("_qc_summary"))

    # An exact duplicate -- the same time, position and values as a cast already built -- is
    # written once: a source that lists a cast twice does not hold two casts.
    seen: Dict[str, str] = {}
    unique = []
    for b in built:
        cast_id = b.pop("_source_cast")
        pc = b["contract"].get("parsed_coordinates") or {}
        when_where = {k: pc.get(k) for k in ("year", "month", "day", "hour", "minute", "second",
                                             "latitude", "longitude")}
        fingerprint = json.dumps(when_where, sort_keys=True, default=str) + \
            b["df"].to_csv(index=False)
        if fingerprint in seen:
            for k in ("_unstorable", "_levels_dropped", "_from_pressure"):
                b.pop(k, None)
            skipped_casts.append({"cast_id": cast_id, "duplicate_of": seen[fingerprint],
                                  "when": _when(pc),
                                  "reason": "an exact duplicate of an earlier cast: the same "
                                            "time, position and values"})
            runlog.event(log, logging.INFO, f"{key}: cast {cast_id} not written: a duplicate "
                                            f"of cast {seen[fingerprint]}",
                         kind="cast_skipped", key=key, cast=cast_id,
                         reason=skipped_casts[-1]["reason"])
            continue
        seen[fingerprint] = cast_id
        unique.append(b)
    built = unique
    # Columns and levels dropped are counted over the casts that are written: a cast left
    # out takes nothing out of the output.
    levels_dropped = from_pressure = 0
    for b in built:
        unstorable.extend(b.pop("_unstorable", []))
        levels_dropped += b.pop("_levels_dropped", 0)
        from_pressure += b.pop("_from_pressure", False)

    # What the source held that the output does not, said in the output itself: a reader
    # of the NetCDF should not need the run log to learn a column or a cast was left out.
    file_notes = []
    not_written = sorted({f"{u['column']} ({u['variable']})" for u in unstorable})
    if not_written:
        file_notes.append("Not written from the source file: " + "; ".join(not_written) + ".")
    if skipped_casts:
        file_notes.append(
            f"{len(skipped_casts)} cast(s) of the source file not written: "
            + "; ".join(f"cast {c['cast_id']}: {c['reason'].split('. ')[0]}"
                        for c in skipped_casts[:10])
            + (" ..." if len(skipped_casts) > 10 else "") + ".")
    for b in built:
        b["contract"]["provenance_notes"] = list(b["contract"].get("provenance_notes")
                                                 or []) + file_notes

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
    # One file's casts, validated on their own so a file that cannot be written is that
    # file's failure. The WOD files are assembled from these (`assemble_profiles`).
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
    # Last, once nothing more can fail: a file whose result is saved is a file that is done.
    _save_casts(cache_path, key, url, built)

    return {
        "ok": True,
        "output_path": str(out_path),
        "cache_path": str(cache_path),
        "casts": write["cast_count"],
        "observations": write["total_observations"],
        "validation": {"status": report["status"], "warnings": report["warnings"]},
        "qc_summary": qc_summaries[0] if qc_summaries else None,
        "dataset_types": types,
        "classification": classifications[0] if classifications else None,
        "unstorable_columns": unstorable or None,
        "format_family": fmt["format_family"],
        "skipped_casts": skipped_casts or None,
        "levels_without_data": levels_dropped or None,
        "casts_depth_from_pressure": from_pressure or None,
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
                run_qc=RUN_WOD_QC, drop=None):
    """Classify, map, convert and QC one cast. Returns (built or None, verdict).

    `drop` names variables the review left out of this file (Table 3a names or the file's
    own column names, any case): they are not written, and the report says so."""
    df, derived = _depth_from_pressure(cast["df"], cast.get("header_block", ""))
    header = cast.get("header_block", "") + (f"\n{derived}" if derived else "")
    depth_inc = _depth_increment(df)

    verdict = classify_dataset(path=key, columns=[str(c) for c in df.columns],
                               header_text=header, depth_increment_m=depth_inc,
                               n_levels=len(df))
    override = _override_for(key, dataset_overrides)
    chosen, source = _choose_dataset_type(dataset_type, override, verdict,
                                          process_unidentified_as)
    verdict["chosen_dataset_type"] = chosen
    verdict["chosen_by"] = source
    verdict["depth_from_pressure"] = bool(derived)

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
    # One level has no increment and is no high-resolution profile: a CTD cast of a single
    # level goes to OSD with the rest of the low-resolution data (manual p.19).
    if spec.get("wod_dataset_code") == "CTD" and depth_inc is None and _levels(df) < 2:
        spec = query_wod_spec("OSD", [str(c) for c in df.columns], depth_increment_m=None)
        verdict["single_level_to_osd"] = True
    profile = {"source": cast.get("origin") or cast.get("local_path")
                         or cast.get("source_note", ""),
               "source_format": cast.get("source_note", ""), "raw_header_block": header,
               "columns": [str(c) for c in df.columns]}
    contract = generate_field_mapping(profile, spec, df=df)
    left_out = {str(d).strip().lower() for d in drop or []}
    for d in contract.get("dropped_columns") or []:
        named = {d["column"].lower(), str(d["variable"]).split(",")[0].lower()}
        if d.get("kind") == "not_in_dataset" and named & left_out:
            d.update(kind="dropped_by_decision",
                     variable=d["variable"].split(",")[0] + ", left out by decisions.toml "
                              "(and " + d["variable"].split(", ", 1)[1] + ")")
    if left_out:
        kept, removed = [], []
        for m in contract["column_mapping"]:
            named = {str(m.get("wod_name", "")).lower(), str(m["raw_column"]).lower()}
            (removed if m.get("role") != "depth_axis" and named & left_out else kept).append(m)
        contract["column_mapping"] = kept
        contract.setdefault("dropped_columns", []).extend(
            {"column": m["raw_column"], "kind": "dropped_by_decision",
             "variable": f"{m['wod_name']}, left out by decisions.toml",
             "reason": "the review said not to write it (drop)"} for m in removed)
    # Every column that is not written is said: one WOD has no code for (from triage), one
    # the dataset does not store (Table 3a), and one nothing recognised at all.
    explained = {d["column"] for d in contract.get("dropped_columns") or []} | {
        u["column"] for u in verdict.get("unstorable_columns") or []}
    verdict["unstorable_columns"] = list(verdict.get("unstorable_columns") or []) + [
        d for d in contract.get("dropped_columns") or []
        if d.get("kind") in ("not_in_dataset", "dropped_by_decision")
    ] + [{"column": c, "variable": "not recognised as a WOD variable", "kind": "unrecognised",
          "reason": "no Table 3a variable matches its name"}
         for c in contract.get("unmapped_columns") or [] if c not in explained]
    pc = contract.get("parsed_coordinates") or {}
    verdict["when"] = _when(pc)
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

    # Pressure in decibars with a latitude has become depth already (_depth_from_pressure).
    # What is left here cannot: written anyway, it produces a NetCDF with no depth axis that
    # fails validation with "'z' is entirely missing", three phases from the cause.
    if not any(m["wod_code"] == DEPTH_SENTINEL_CODE for m in contract["column_mapping"]):
        has_pressure = any(m["wod_code"] == 25 for m in contract["column_mapping"])
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            "pressure but no depth: the pressure is not in decibars, so depth was not "
            "computed from it. Convert it during staging with "
            "wod_etl.staging.pressure_to_depth, which needs the cast's latitude, and record "
            "that depth was derived."
            if has_pressure else
            "no depth axis: no column resolves to depth, and there is no pressure column to "
            "derive one from. A profile cannot be written without one.")
        return None, verdict

    # A level where nothing was measured -- a gridded file's padding below the cast, a bin
    # the instrument never reached, a pressure the originator blanked T and S at -- carries
    # only its vertical position, and WOD has no level without a value. Pressure does not
    # count as a value here: on a CTD it is the coordinate the level was taken at.
    import pandas as pd

    values = [c for c in measured if next((m["wod_code"] for m in contract["column_mapping"]
                                           if m["raw_column"] == c), None) != 25] or measured
    has_value = df[values].apply(pd.to_numeric, errors="coerce").notna().any(axis=1)
    if not has_value.all():
        verdict["levels_without_data"] = int((~has_value).sum())
        df = df[has_value].reset_index(drop=True)

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
    casts_not_written: Dict[str, int] = {}
    where_not_written: Dict[str, List[str]] = {}
    levels_without_data = from_pressure = 0
    for it in manifest["items"].values():
        # Casts a written file left out -- no position, nothing measured, a duplicate. The
        # file is done, so without this they would vanish from the report.
        if it["status"] == mf.DONE:
            levels_without_data += it.get("levels_without_data") or 0
            from_pressure += it.get("casts_depth_from_pressure") or 0
            for c in it.get("skipped_casts") or []:
                why = c["reason"].split(". ")[0]
                casts_not_written[why] = casts_not_written.get(why, 0) + 1
                where_not_written.setdefault(it["key"], []).append(_cast_left_out(c))
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
            "coded_headers_not_resolved": unresolved_codes or None,
            "casts_not_written": casts_not_written or None,
            "casts_not_written_in": where_not_written or None,
            "levels_without_data": levels_without_data or None,
            "casts_depth_from_pressure": from_pressure or None}


def _cast_left_out(c: Dict[str, Any]) -> str:
    """'#98 (2017-09-13T22:03): no latitude, longitude' -- which source profile (counted from
    0, as the source stores them), when, and why."""
    reason = c["reason"].split(". ")[0]
    m = re.match(r"missing required coordinate\(s\): (.+)", reason)
    reason = (f"duplicate of #{c['duplicate_of']}" if c.get("duplicate_of") is not None else
              f"no {m.group(1)}" if m else reason)
    return f"#{c['cast_id']}" + (f" ({c['when']})" if c.get("when") else "") + f": {reason}"


def _log_summary(result: Dict[str, Any]) -> None:
    """One closing line per run, with the fields a report must carry."""
    if result.get("status") in ("error", "empty", "dry_run"):
        runlog.event(log, logging.INFO, f"process_accession ended: {result.get('status')}",
                     kind="run_end", status=result.get("status"),
                     error=result.get("error"), hint=result.get("hint"))
        return
    verification = result.get("output_verification") or {}
    level = (logging.WARNING if result.get("status") != "complete"
             or result.get("output_verified") is False else logging.INFO)
    runlog.event(log, level,
                 f"process_accession ended: {result.get('status')}, {result.get('counts')}, "
                 f"output_verified={result.get('output_verified')}",
                 kind="run_end", status=result.get("status"), counts=result.get("counts"),
                 failures=result.get("failures"), dataset_types=result.get("dataset_types"),
                 skipped_reasons=result.get("skipped_reasons"),
                 output_verified=result.get("output_verified"),
                 findings=verification.get("findings"),
                 abort_reason=result.get("abort_reason"),
                 manifest=result.get("manifest_path"))


def _abort(manifest, reason: str) -> Dict[str, Any]:
    runlog.event(log, logging.ERROR, f"run aborted: {reason}", kind="abort", reason=reason)
    summary = mf.summarize(manifest)
    summary["status"] = "aborted"
    summary["abort_reason"] = reason
    summary.update(_run_report(manifest))
    # Nothing is assembled from a run that stopped on a systematic failure: the WOD files in
    # output_dir, if any, are an earlier run's. The files it did write are the ones most
    # worth checking -- whatever went wrong is usually visible in them.
    summary["per_file_outputs"] = summary["outputs"]
    summary["outputs"] = []
    summary["output_verified"] = None
    summary["output_verification"] = {
        "skipped": "run aborted, and no WOD file was written from it (any in output_dir are "
                   "an earlier run's); check the files it did write with "
                   "verify_output(output_dir, paths=result['per_file_outputs'])"}
    return summary


def _item_key(root: str, path: str, name: str) -> str:
    """A file's identity within a run: its path relative to the accession root."""
    root_norm = root.replace("\\", "/").rstrip("/")
    path_norm = path.replace("\\", "/")
    if path_norm.startswith(root_norm + "/"):
        return path_norm[len(root_norm) + 1:]
    return name


def _result_stem(key: str) -> str:
    """'stationA/data.csv' -> 'stationA__data.csv': a source file's result files, named by
    its path, extension included -- `x.cnv` and `x.btl` are two files."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key.replace("/", "__")).strip("_") or "cast"


def _result_stems(keys) -> Dict[str, str]:
    """Every key's stem, made unique: two keys that flatten to one name (`a/b.csv` and
    `a__b.csv`) each get a digest of their key."""
    keys = list(keys)
    stems = {k: _result_stem(k) for k in keys}
    counts: Dict[str, int] = {}
    for st in stems.values():
        counts[st] = counts.get(st, 0) + 1
    return {k: (st if counts[st] == 1 else
                f"{st}-{hashlib.sha1(k.encode('utf-8')).hexdigest()[:8]}")
            for k, st in stems.items()}


def _output_stem(key: str) -> str:
    """'stationA/data.csv' -> 'stationA__data'. A flat key is unchanged. The name output was
    written under before the WOD files (and still is for a taxa haul)."""
    stem = key[:-len(Path(key).suffix)] if Path(key).suffix else key
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem.replace("/", "__")).strip("_") or "cast"


def _when(pc: Dict[str, Any]) -> Optional[str]:
    """A cast's date and time as the report names it -- a source index alone does not say
    which cast it was."""
    if pc.get("year") is None:
        return None
    text = f"{int(pc['year']):04d}-{int(pc.get('month') or 1):02d}-{int(pc.get('day') or 1):02d}"
    if pc.get("hour") is not None:
        text += f"T{int(pc['hour']):02d}:{int(pc.get('minute') or 0):02d}"
    return text


def _depth_from_pressure(df, header: str):
    """(df, note): a cast measured on pressure surfaces, given the depth column WOD requires.

    Not a judgement call: "Casts with measured data on pressure surfaces have their depths
    computed, so depth is always present and the pressure value is stored as a variable"
    (WOD manual, cast structure). Depth is the UNESCO 1983 formula
    (Fofonoff and Millard) at the cast's own latitude, and the note returned goes into the
    output's comment. The cast is returned unchanged -- and refused later, saying why --
    when it has a depth column already, no pressure in decibars (a CTD's pressure with no
    unit is decibars), or no latitude to compute with.
    """
    import numpy as np
    import pandas as pd

    from wod_etl.mapping.generate_field_mapping import parse_header_block
    from wod_etl.staging.parse import pressure_to_depth

    columns = [str(c) for c in df.columns]
    if any(match_variable(c) == DEPTH_SENTINEL_CODE for c in columns):
        return df, None
    pressure = next((c for c in columns if match_variable(c) == 25), None)
    unit = re.search(r"[(\[]\s*([^)\]]*?)\s*[)\]]", pressure or "")
    if pressure is None or (unit and not re.fullmatch(r"dbar|decibars?|db", unit.group(1),
                                                      re.IGNORECASE)):
        return df, None
    latitude = (parse_header_block(header).get("coordinates") or {}).get("latitude")
    if latitude is None:
        return df, None
    p = pd.to_numeric(df[pressure], errors="coerce").to_numpy(dtype=float)
    name = "Depth (m)" if "Depth (m)" not in columns else "Depth from pressure (m)"
    out = df.copy()
    out.insert(0, name, np.round(pressure_to_depth(p, float(latitude)), 3))
    return out, (f"Note: depth computed from {pressure} (decibars) with the UNESCO 1983 "
                 f"formula (Fofonoff and Millard) at the cast's latitude, {latitude}; the "
                 f"pressure is kept as variable 25, as WOD does for casts measured on "
                 f"pressure surfaces.")


def _levels(df) -> int:
    """How many levels of the cast have a depth."""
    import pandas as pd

    for col in df.columns:
        if match_variable(col) == DEPTH_SENTINEL_CODE:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    return len(df)


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
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(RAW_DIR)
    print(json.dumps(process_accession(target, limit=5, dry_run=True), indent=2))
