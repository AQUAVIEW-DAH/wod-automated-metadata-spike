"""Failures as problems: what failed, why, and how to fix it (a decisions.toml key or command)."""
import re
from typing import Any, Dict, Optional

# Per-file stages whose failure a retry will not change; they need a decision, not a retry.
DETERMINISTIC_FILE_STAGES = {"sniff_format", "read_any", "mapping", "classify_dataset",
                             "generate_netcdf_ragged", "validate_intermediate_netcdf",
                             "map_haul", "write_taxa_netcdf", "validate_taxa_netcdf",
                             "unexpected"}


def problem(stage: str, what: str, why: str, how_to_fix: str, *,
            file: Optional[str] = None, decisions_key: Optional[str] = None,
            blocking: bool = True, kind: str = "failure") -> Dict[str, Any]:
    return {"stage": stage, "kind": kind, "what": what, "why": why,
            "how_to_fix": how_to_fix, "file": file, "decisions_key": decisions_key,
            "blocking": blocking}


def for_listing(listing: Dict[str, Any], source: str) -> Dict[str, Any]:
    err = str(listing.get("error") or "")
    if re.search(r"\b(429|500|502|503|504)\b|Timeout|ConnectError|timed out", err):
        how = ("NCEI did not answer (it was retried). Rerun later (the run continues from "
               "here), or download the accession and pass the local folder or tarball.")
    elif listing.get("hint"):
        how = listing["hint"]
    else:
        how = ("Check the input: a landing-page URL, an accession number, an NCEI data "
               "directory URL, a local folder or a .tar.gz.")
    return problem("resolve", f"could not list {source}", err or "no files found", how)


def for_listing_fallback(listing: Dict[str, Any], source: str, mirror) -> Dict[str, Any]:
    """A note: the listing failed, so the files downloaded earlier were used instead."""
    return problem("resolve", f"could not list {source}; used the files downloaded earlier "
                              f"to {mirror}", str(listing.get("error") or "no files found"),
                   "If an earlier download was interrupted or NCEI has changed the accession "
                   "since, files may be missing or out of date. Run again when NCEI answers: "
                   "it lists the accession and fetches only what is missing.",
                   blocking=False, kind="offline")


def for_download(key: str, error: str) -> Dict[str, Any]:
    return problem("fetch", f"could not download {key}", error,
                   "A network or server error; it is retried. Rerun later: files already "
                   "downloaded are kept.", file=key)


def for_file_failure(key: str, stage: str, error: str, staged: bool) -> Dict[str, Any]:
    """A file `process_accession` could not turn into output."""
    dkey = f'files."{key}"'
    leave_out = (f"To leave it out, add [{dkey}] with include = false to decisions.toml and "
                 f"confirm again.")
    if stage == "download_file":
        return problem("process", f"could not read {key}", error,
                       "A transient error; confirm again to retry it.", file=key)
    if stage == "unexpected":
        how = ("A bug in wod_etl, not in the data: the traceback is in run.log. "
               "Report it. " + leave_out)
    elif "pivot_long" in error or "long format" in error.lower():
        how = ("It names its variables in a cell: it needs a [profiles.<name>] recipe with "
               "kind = \"long\" (`wod-etl run` proposes one for a file it recognises as long "
               "format). " + leave_out)
    elif "missing required coordinate" in error:
        how = ("It carries no position or date of its own. Name the station list that locates "
               "it in a [profiles.<name>] recipe (kind = \"per_file\"). " + leave_out)
    elif "pressure but no depth" in error:
        how = ("Depth must be derived from pressure, which needs the cast's latitude: stage "
               "it with a kind = \"script\" recipe using staging.pressure_to_depth. "
               + leave_out)
    elif stage in ("sniff_format", "read_any"):
        how = ("It does not read as any table this package knows. If it holds data, find out "
               "what wrote it (the accession's readme or the submitter) and stage "
               "it with a script recipe. " + leave_out)
    elif staged:
        how = ("A recipe wrote this staged file: fix the recipe in decisions.toml (units, "
               "columns), or list the cast in its skip_casts, and confirm again.")
    else:
        how = ("If it was classified as the wrong dataset, set dataset = \"<dataset>\" in its "
               "entry. " + leave_out)
    return problem("process", f"{key} failed at {stage}", error, how, file=key,
                   decisions_key=dkey)


def for_wod_file_failure(name: str, sources, stage: str, error: str) -> Dict[str, Any]:
    """A WOD file (one dataset, one year) that failed although each source file wrote alone."""
    shown = ", ".join(sources[:5]) + (f" and {len(sources) - 5} more" if len(sources) > 5
                                      else "")
    return problem("process", f"{name} could not be written", f"{stage}: {error}",
                   f"Each source file was written on its own but not together: a bug in "
                   f"wod_etl, detailed in run.log. Report it. To write the rest, set "
                   f"include = false on one of its source files and confirm again: {shown}.")


def for_haul_failure(key: str, stage: str, error: Any) -> Dict[str, Any]:
    """A staged haul (or a year's Taxa-Record file) that could not be written."""
    error = "; ".join(error) if isinstance(error, list) else str(error)
    return problem("process", f"haul {key} could not be written", f"{stage}: {error}",
                   "The other hauls were written. Fix the source rows, or set include = false "
                   "on the source file, then confirm again.", file=key)


def taxa_notes(taxa: Dict[str, Any]) -> list:
    """Non-blocking notes about the Taxa-Records written."""
    notes = []
    note = lambda what, why, how: notes.append(problem("process", what, why, how,
                                                       blocking=False, kind="warning"))
    if taxa.get("taxa_list_unavailable"):
        note("WOD's taxa list could not be read, so taxa are written by name only",
             "the list is fetched from the WOD website",
             "Confirm again online with --from process to resolve them.")
    unresolved = taxa.get("unresolved_taxa") or {}
    if unresolved and not taxa.get("taxa_list_unavailable"):
        shown = ", ".join(list(unresolved)[:8]) + (" ..." if len(unresolved) > 8 else "")
        note(f"{len(unresolved)} taxon name(s) not in WOD's taxa list: kept by name", shown,
             "Nothing to do; check the spelling if one looks wrong.")
    higher = taxa.get("taxa_resolved_at_higher_rank") or {}
    if higher:
        note(f"{len(higher)} taxon name(s) resolved one rank up", ", ".join(list(higher)[:8]),
             "Nothing to do; the original name is kept.")
    # Missing Biological Header fields are already reported at review time.
    return notes


def for_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    where = finding.get("variable") or finding.get("file") or "output"
    return problem("verify", f"{finding['finding']} in {where} ({finding['severity']})",
                   finding.get("evidence", ""), finding.get("action", ""),
                   blocking=finding.get("severity") == "high", kind="finding")


def for_exception(stage: str, exc: BaseException) -> Dict[str, Any]:
    return problem(stage, f"stage '{stage}' raised {type(exc).__name__}", str(exc),
                   "A bug, not a data problem: the traceback is in run.log. Fix or report "
                   "it, then run the same command again: it continues from this stage.")


def for_decisions_syntax(error: str, stage: str = "decide") -> Dict[str, Any]:
    return problem(stage, "decisions.toml cannot be read", error,
                   "Fix the line named above (a missing quote or bracket is the usual cause) "
                   "and run the same command again.")


def for_open_item(item: Dict[str, Any], stage: str = "decide") -> Dict[str, Any]:
    key = item.get("decisions_key")
    return problem(stage, item["what"], "",
                   (f"In decisions.toml under [{key}]: " if key else "") + item["how_to_fix"],
                   decisions_key=key, blocking=item["blocking"],
                   kind="decision" if item["blocking"] else "pending")
