"""Turn a failure into something a person can act on: what failed, why, and how to fix it.

Most of the "why" already exists where the failure happens -- a listing's `hint`, a file's
`skip_reason`, a verification finding's `action`. This adds the half that only the pipeline
knows: which key in decisions.toml settles it, or which command to run next.
"""
import re
from typing import Any, Dict, Optional

# Per-file stages whose failure will not change on a retry: the bytes are the same, so is the
# answer. These become a decision for the person instead of a retry loop.
DETERMINISTIC_FILE_STAGES = {"sniff_format", "read_any", "mapping", "classify_dataset",
                             "generate_netcdf_ragged", "validate_intermediate_netcdf",
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
        how = ("NCEI did not answer (it was retried). Rerun later -- the run continues from "
               "here -- or download the accession and pass the local folder or tarball.")
    elif listing.get("hint"):
        how = listing["hint"]
    else:
        how = ("Check the input: a landing-page URL, an accession number, an NCEI data "
               "directory URL, a local folder or a .tar.gz.")
    return problem("resolve", f"could not list {source}", err or "no files found", how)


def for_download(key: str, error: str) -> Dict[str, Any]:
    return problem("fetch", f"could not download {key}", error,
                   "A network or server error; it is retried. Rerun later -- files already "
                   "downloaded are kept.", file=key)


def for_file_failure(key: str, stage: str, error: str, staged: bool) -> Dict[str, Any]:
    """A file `process_accession` could not turn into output."""
    dkey = f'files."{key}"'
    if stage == "download_file":
        return problem("process", f"could not read {key}", error,
                       "A transient error; rerun to retry it.", file=key, blocking=True)
    if stage == "unexpected":
        how = ("This is a bug in wod_etl, not in the data: the traceback is in run.log. "
               f"Report it, or set action = \"skip\" under [{dkey}] in decisions.toml to "
               f"leave the file out and finish the rest.")
    elif "pivot_long" in error or "long format" in error.lower():
        how = ("It names its variables in a cell. Add a [profiles.<name>] recipe with "
               "kind = \"long\" for it in decisions.toml (rerun proposes one), or skip it.")
    elif "missing required coordinate" in error:
        how = ("It carries no position or date. Name the station list that locates it in a "
               "[profiles.<name>] recipe (kind = \"per_file\"), or skip it.")
    elif "pressure but no depth" in error:
        how = ("Depth must be derived from pressure, which needs the cast's latitude: stage "
               "it with a kind = \"script\" recipe using staging.pressure_to_depth.")
    elif stage in ("sniff_format", "read_any"):
        how = ("It does not read as any table this package knows. If it holds data, "
               "find out what wrote it (see .claude/skills/wod-etl/reading-unknown-files.md) "
               f"and stage it with a script recipe; otherwise set action = \"skip\" under "
               f"[{dkey}].")
    elif staged:
        how = (f"The staged file was written by a recipe; fix the recipe in decisions.toml "
               f"(units, columns), or set action = \"skip\" under [{dkey}].")
    else:
        how = (f"Set action = \"skip\" under [{dkey}] in decisions.toml to leave it out, or "
               f"action = \"process\" with process_as = \"<dataset>\" if it was not "
               f"recognised.")
    return problem("process", f"{key} failed at {stage}", error, how, file=key,
                   decisions_key=dkey)


def for_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    where = finding.get("variable") or finding.get("file") or "output"
    return problem("verify", f"{finding['finding']} in {where} ({finding['severity']})",
                   finding.get("evidence", ""), finding.get("action", ""),
                   blocking=finding.get("severity") == "high", kind="finding")


def for_exception(stage: str, exc: BaseException) -> Dict[str, Any]:
    return problem(stage, f"stage '{stage}' raised {type(exc).__name__}", str(exc),
                   "This is a bug, not a data problem: the traceback is in run.log. Fix it "
                   "(or report it) and rerun -- the run continues from this stage.")


def for_decisions_syntax(error: str) -> Dict[str, Any]:
    return problem("decide", "decisions.toml cannot be read", error,
                   "Fix the line named above -- a missing quote or bracket is the usual cause "
                   "-- and rerun.")


def for_open_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return problem("decide", item["what"], "",
                   f"In decisions.toml under [{item['decisions_key']}]: {item['how_to_fix']}",
                   decisions_key=item["decisions_key"], blocking=item["blocking"],
                   kind="decision" if item["blocking"] else "pending")
