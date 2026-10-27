"""Per-file engine behind `wod-etl confirm`: an accession in, WOD NetCDF files out.

Each file is classified and processed on its own, with every outcome recorded in the run
manifest; bad data never raises.
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

# Per-file results (NetCDF and cached casts) that the WOD files are assembled from.
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
    """Process every data file in an accession into WOD files, one per dataset and year.

    Each source file is processed and validated alone first, so failures stay per file and
    reruns resume; WOD files are assembled only from the files this call covers.

    dataset_type   -- one dataset for the whole run; None classifies each file.
    dataset_overrides -- {path_prefix: dataset_type}, overriding classification per folder.
    process_unidentified_as -- dataset for profile-shaped files naming no instrument; None skips.
    run_qc         -- WOD observed-level QC; None uses config RUN_WOD_QC (off).
    verify         -- check written values against the manual's ranges (`output_verified`).
    include        -- process only these keys; keys not found go to `include_missing`.
    accession      -- picks the run log (inferred if None); if given, written to casts naming none.
    file_options   -- {key: {"drop": [...], "metadata": {...}}} from the reviewed decisions.
    per_file_dir   -- results go in `<per_file_dir>/<run_id>/`; default `<output_dir>/per_file/`.
    assemble       -- write the WOD files at the end; False leaves it to `assemble_profiles`.

    Returns the run summary: `outputs` (WOD files), `per_file_outputs` (each source file's
    NetCDF), `per_file_casts` (cached casts), `failures` and `assembly`.
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
        # Keep `hint`: it explains a bad URL (e.g. a landing page, not a data directory).
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
    # Files this call answers for: listed and included, even past `limit`.
    listed = {_item_key(root0, i["path"], i["name"]) for i in candidates}
    answered = {_norm_source(i["path"]) for i in candidates}
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        return {"status": "empty", "source": url_or_path,
                # Prefer `hint` when the URL itself explains the emptiness.
                "message": listing.get("hint")
                           or "no files found; check the URL or filter_extension",
                "hint": listing.get("hint"),
                "accession_number": listing.get("accession_number"),
                "listing_total": listing.get("total_items", 0)}

    rid = run_id or mf.new_run_id(url_or_path)
    # A folder per run, so runs sharing an output_dir do not overwrite each other.
    per_dir = (Path(per_file_dir) if per_file_dir else out_dir / PER_FILE_DIR) / rid
    # Keys are root-relative paths, so same-named files in different folders stay distinct.
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
    # Redo a done file whose per-file result is gone, or whose key now names another file.
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
            # Not WOD profile data: a skip, not counted toward the abort rate.
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
                              f"{ABORT_IF_FAILURE_RATE_ABOVE:.0%}, which usually means a "
                              f"systematic problem, not bad data")

    summary = mf.summarize(manifest)
    summary["status"] = "complete" if summary["complete"] else "incomplete"
    summary.update(_run_report(manifest))
    summary["per_file_outputs"] = summary["outputs"]
    # Only this call's files, not whatever else a shared folder holds.
    summary["per_file_casts"] = list(dict.fromkeys(
        it["cache_path"] for key, it in manifest["items"].items()
        if key in listed and it["status"] == mf.DONE and it.get("cache_path")))
    if assemble and limit:
        # A `limit` trial is not assembled: it would replace a full run's WOD files.
        summary["outputs"] = summary["per_file_outputs"]
        summary["assembly"] = {"skipped": f"limit={limit}: a trial run is not assembled; "
                                          f"its per-file results are per_file_outputs"}
    elif assemble:
        _remove_old_layout(manifest, out_dir)
        scope = accession or accession_for(url_or_path) or folder_scope(root)
        base = _norm_source(root).rstrip("/\\")

        def owns(source: str) -> bool:
            """Whether this call may replace a source's casts: covered, or gone from root."""
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
    # How the input was resolved into a listing (landing page, version, archive).
    summary["listing"] = {k: listing[k] for k in ("resolved_from", "resolution", "version",
                                                  "extracted_from") if listing.get(k)} or None
    return summary


def _verify_written_output(out_dir: Path, summary: Dict[str, Any],
                           verify: bool) -> Dict[str, Any]:
    """Read the output back and check that the values are possible, not only the structure.

    `output_verified` is separate from `status`: a run can be complete and unverified.
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
            # Only high and medium findings; the rest stay in `text`.
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
    """Write source files' cached casts as WOD files in `output_dir`, one per dataset and year.

    `caches` are `per_file_casts` paths; `scope` (the accession or a folder name) names the
    files. A file of this scope is replaced or removed only if `owns` holds for all its sources
    (`owns=None`: all). Each file is validated before it replaces the old one.

    Returns `{"files": [{"path", "name", "dataset", "year", "casts", "observations",
    "sources": [{"key", "casts"}], "warnings"}], "problems": [...], "removed": [...]}`.
    """
    if not str(scope or "").strip():
        raise ValueError("assemble_profiles needs a scope (the accession, or a folder's "
                         "name), or its files would carry WOD's own names")
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

    # Remove this scope's owned files not written now, and unfinished `.part` writes.
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
    """Source files of a WOD file this package wrote; None for any other or unreadable file."""
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
    """A comparable source: a URL as is, a path resolved (symlinks, relative paths)."""
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
    """Cache one file's built casts for `assemble_profiles` (re-reading the NetCDF is lossy)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump({"version": CACHE_VERSION, "key": key, "source": source, "casts": built},
                    f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _remove_old_layout(manifest: Dict[str, Any], out_dir: Path) -> None:
    """Remove this package's old-layout `<stem>.nc` files, which duplicate the WOD files' casts."""
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
    """Run every phase on one file. Returns {"ok": bool, ...}; never raises.

    `key` is the path relative to the accession root (default: basename). `accession` is
    written into casts that name none; `options` holds the reviewed `drop` and `metadata`;
    `stem` names the result files (default: from `key`).
    """
    out_dir = Path(out_dir)
    key = key or os.path.basename(url.rstrip("/")) or url
    subdir = "/".join(filter(None, [run_id, os.path.dirname(key)]))
    # From the full relative path with extension, so `x.cnv` and `x.btl` do not collide.
    stem = stem or _result_stem(key)
    out_path = out_dir / f"{stem}.nc"
    cache_path = out_dir / f"{stem}{CACHE_SUFFIX}"
    # Clear the last result so a failure now is not assembled from stale output.
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
        # Unreadable non-documents fail rather than skip, so unknown layouts are not lost.
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
        # Reviewed header values go first: the parser keeps the first value it meets.
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

    # Write exact duplicates (same time, position and values) once.
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
    # Count dropped columns and levels over written casts only.
    levels_dropped = from_pressure = 0
    for b in built:
        unstorable.extend(b.pop("_unstorable", []))
        levels_dropped += b.pop("_levels_dropped", 0)
        from_pressure += b.pop("_from_pressure", False)

    # Record in the output itself which columns and casts were not written.
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
        # Excluded or empty casts make a skip; unmappable ones a failure (usually needs staging).
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
    # Validate this file's casts alone, so a write failure stays this file's.
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
    # Saved last: a saved result marks the file done.
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

    `drop` names variables not to write (Table 3a or column names, any case)."""
    df, derived = _depth_from_pressure(cast["df"], cast.get("header_block", ""))
    header = cast.get("header_block", "") + (f"\n{derived}" if derived else "")
    df, reversed_note = _shallowest_first(df)
    if reversed_note:
        header += f"\nNote: {reversed_note}."
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
    df, header, surface = _at_the_surface(df, header, chosen, key)
    if surface:
        verdict["surface"] = surface
        depth_inc = None

    if chosen is None:
        verdict["skip_kind"] = "excluded" if verdict.get("exclusion") else "unidentified"
        verdict["skip_reason"] = (
            f"not WOD profile data: {verdict['exclusion']['reason']}"
            if verdict.get("exclusion") else
            "no instrument identified from path, header, columns or depth spacing")
        return None, verdict

    # A long-format table needs reshaping: report that, not a sidecar skip.
    if verdict.get("long_format"):
        verdict["skip_kind"] = "needs_staging"
        verdict["skip_reason"] = verdict["long_format"]["action"]
        return None, verdict

    spec = query_wod_spec(chosen, [str(c) for c in df.columns], depth_increment_m=depth_inc)
    # A single-level CTD cast is not high-resolution: it goes to OSD (manual p.19).
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
    # Every column not written: no WOD code, not in the dataset (Table 3a), or unrecognised.
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
    if df.empty:
        verdict["skip_kind"] = "empty"
        verdict["skip_reason"] = "no levels: the source cast has a header and no data rows"
        return None, verdict
    if not contract["column_mapping"]:
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            f"no column mapped to a WOD code. Columns read: "
            f"{', '.join(str(c) for c in list(df.columns)[:8])}. Either the header row was "
            f"not the row that was read, or none of these are Table 3a variables.")
        return None, verdict

    # Check for measurements now rather than fail validation later.
    measured, empty = _measurement_columns(df, contract)
    if not measured:
        verdict["skip_kind"] = "sidecar"
        verdict["skip_reason"] = (
            "no measured variable: this file carries coordinates and identifiers but "
            "nothing from Table 3a. It is a station list or metadata sidecar, not a cast: "
            "use it to stage coordinates into the cast files that reference it (Phase 0, "
            "wod_etl/staging/).")
        return None, verdict
    if empty:
        verdict["skip_kind"] = "no_data"
        verdict["skip_reason"] = (
            f"no measurements: every mapped measurement column is empty "
            f"({', '.join(empty)}), as with 'Not analyzed' values. The file is well-formed "
            f"but holds nothing to standardize.")
        return None, verdict

    # No position or depth: stop here and name the cause. A missing time only warns.
    missing = contract.get("missing_required_coordinates") or []
    blocking = [c for c in missing if c in BLOCKING_COORDINATES]
    if blocking:
        verdict["skip_kind"] = "unmappable"
        verdict["skip_reason"] = (
            f"missing required coordinate(s): {', '.join(blocking)}. They are not in the "
            f"data file or its header block. Stage them in first from a sibling station "
            f"list, the folder readme or a cruise report, noting where each came from "
            f"(Phase 0, wod_etl/staging/).")
        return None, verdict
    verdict["missing_coordinates"] = missing or None

    # Convertible pressure is already depth (_depth_from_pressure); stop if no depth axis.
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

    # Drop levels with no value (WOD has none); pressure, the level's coordinate, does not count.
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


def _shallowest_first(df):
    """Reverse a cast recorded deepest first (manual p.52), unless it goes both ways."""
    import pandas as pd

    from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable

    depth = next((c for c in df.columns if match_variable(str(c)) == DEPTH_SENTINEL_CODE), None)
    z = pd.to_numeric(df[depth], errors="coerce").dropna() if depth is not None else None
    if z is None or len(z) < 3:
        return df, None
    steps = z.diff().dropna()
    steps = steps[steps != 0]
    if len(steps) and (steps < 0).mean() >= 0.9 and z.iloc[0] > z.iloc[-1]:
        return (df.iloc[::-1].reset_index(drop=True),
                "levels recorded deepest first, reversed so the shallowest comes first "
                "(manual p.52)")
    return df, None


def _at_the_surface(df, header: str, dataset: Optional[str], key: str):
    """Give surface data a depth axis (manual p.20).

    - SUR track: one cast at 0 m; observations keep latitude and longitude (codes 30, 31) and
      gain a Julian year-day (code 32, p.37). The header takes the first observation's.
    - A discrete surface sample ('underway' or 'surface' in name or header): one level at 0 m.

    Returns (df, header, note); unchanged, with note None, when a depth axis exists."""
    import pandas as pd

    from wod_etl.discovery.read_any import SURFACE_WORDS
    from wod_etl.mapping.query_wod_spec import DEPTH_SENTINEL_CODE, match_variable
    from wod_etl.staging.parse import parse_coordinate
    from wod_etl.staging.tracks import clock_column, clock_times

    cols = [str(c) for c in df.columns]
    codes = {c: match_variable(c) for c in cols}
    if DEPTH_SENTINEL_CODE in codes.values():
        return df, header, None
    zero = "depth 0 m: surface data, and no intake or sampling depth is stated (manual p.20)"
    if dataset != "SUR":
        if len(df) == 1 and SURFACE_WORDS.search(f"{key} {header}"):
            return pd.concat([pd.Series([0.0], name="Depth (m)"), df.reset_index(drop=True)],
                             axis=1), "\n".join(filter(None, [header, f"Note: {zero}."])), zero
        return df, header, None
    numeric = lambda c: pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9
    lat = next((c for c in cols if codes[c] == 30 and numeric(c)), None)
    lon = next((c for c in cols if codes[c] == 31 and numeric(c)), None)
    if not (lat and lon):
        return df, header, None
    out = df.drop(columns=[c for c in cols if codes[c] in (30, 31) and c not in (lat, lon)])
    clock = clock_column(cols)
    lines = [header, f"Latitude: {parse_coordinate(df[lat].iloc[0])}",
             f"Longitude: {parse_coordinate(df[lon].iloc[0])}"]
    note = f"SUR: {len(df)} observations along the track, one cast; {zero}"
    if clock:
        when, stated_utc = clock_times(df[clock])
        if when.notna().any():
            first = when.dropna().iloc[0]
            jan1 = pd.Timestamp(year=first.year, month=1, day=1)
            out["Julian year-day"] = (when - jan1).dt.total_seconds() / 86400.0
            lines += [f"Date: {first:%Y-%m-%d}", f"Time: {first:%H:%M:%S}"]
            if stated_utc or "utc" in clock.lower():
                lines.append("TIME_ZONE: UTC")
            note += (f"; year-day from 0.00 at 00:00 1 January {first.year} (the manual's "
                     f"definition on p.37; its worked example implies one more)")
        out = out.drop(columns=[clock])
    out.insert(0, "Depth (m)", 0.0)
    return out, "\n".join(filter(None, lines + [f"Note: {note}."])), note


# Coordinates the writer cannot do without; not `time`, as WOD accepts casts without it.
BLOCKING_COORDINATES = ("latitude", "longitude", "depth")

# Table 3a codes that locate a cast rather than measure it (latitude, longitude, julian day).
_COORDINATE_CODES = {30, 31, 32}


def _measurement_columns(df, contract):
    """(measurement columns, empty ones); the second is non-empty only if all are empty."""
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
    """(dataset type, chosen_by): override, explicit type (beats an exclusion), then evidence."""
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
    """Skip by path, before download: non-data extensions, NCEI bundle files, excluded folders."""
    if Path(key).suffix.lower() in NON_DATA_EXTENSIONS:
        return {"reason": f"{Path(key).suffix.lower()} is documentation or a container, "
                          f"not measurements",
                "stage": "prefilter"}
    # NCEI bundle files would otherwise fail to parse or read as tables.
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
    """What a run would do. Local files are classified from their head, remote ones by path."""
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
                    "files are judged by path only; the real run reclassifies them from "
                    "their content, so those types can change. `needs_staging` fails in a "
                    "real run; `undecided` is skipped unless process_unidentified_as is "
                    "set."}


def _run_report(manifest) -> Dict[str, Any]:
    """Dataset mix, skip reasons and dropped columns for the run summary."""
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
        # Casts a done file left out (no position, nothing measured, duplicate).
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
            # Date but no time of day: written with a fill GMT_time, so `time` reads 00:00.
            "outputs_without_time_of_day": date_only or None,
            # Coded metadata written as the originator's text because no unique code matched.
            "coded_headers_not_resolved": unresolved_codes or None,
            "casts_not_written": casts_not_written or None,
            "casts_not_written_in": where_not_written or None,
            "levels_without_data": levels_without_data or None,
            "casts_depth_from_pressure": from_pressure or None}


def _cast_left_out(c: Dict[str, Any]) -> str:
    """'#98 (2017-09-13T22:03): no latitude': source profile index (from 0), when, why."""
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
    # An aborted run is not assembled; its per-file outputs are kept for inspection.
    summary["per_file_outputs"] = summary["outputs"]
    summary["outputs"] = []
    summary["output_verified"] = None
    summary["output_verification"] = {
        "skipped": "run aborted, so no WOD file was written (any in output_dir are from "
                   "an earlier run); check its per-file outputs with "
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
    """'stationA/data.csv' -> 'stationA__data.csv': result-file stem, extension kept."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key.replace("/", "__")).strip("_") or "cast"


def _result_stems(keys) -> Dict[str, str]:
    """Every key's stem; keys that flatten to one stem (`a/b.csv`, `a__b.csv`) get a digest."""
    keys = list(keys)
    stems = {k: _result_stem(k) for k in keys}
    counts: Dict[str, int] = {}
    for st in stems.values():
        counts[st] = counts.get(st, 0) + 1
    return {k: (st if counts[st] == 1 else
                f"{st}-{hashlib.sha1(k.encode('utf-8')).hexdigest()[:8]}")
            for k, st in stems.items()}


def _output_stem(key: str) -> str:
    """'stationA/data.csv' -> 'stationA__data': old-layout output stem, used for taxa hauls."""
    stem = key[:-len(Path(key).suffix)] if Path(key).suffix else key
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem.replace("/", "__")).strip("_") or "cast"


def _when(pc: Dict[str, Any]) -> Optional[str]:
    """A cast's date and time for the report, as `YYYY-MM-DDTHH:MM`."""
    if pc.get("year") is None:
        return None
    text = f"{int(pc['year']):04d}-{int(pc.get('month') or 1):02d}-{int(pc.get('day') or 1):02d}"
    if pc.get("hour") is not None:
        text += f"T{int(pc['hour']):02d}:{int(pc.get('minute') or 0):02d}"
    return text


def _depth_from_pressure(df, header: str):
    """(df, note): add the depth column WOD requires to a cast on pressure surfaces.

    Depth uses the UNESCO 1983 formula (Fofonoff and Millard) at the cast's latitude. Unchanged
    if the cast has depth already, no pressure in decibars (unitless counts), or no latitude.
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
    # 'decibar = 10000 pascals' is decibars with an explanation attached.
    if pressure is None or (unit and not re.match(r"(?:dbar|decibars?|db)\b", unit.group(1),
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
