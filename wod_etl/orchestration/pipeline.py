"""Run one accession end to end, resumably, without an agent: what `wod-etl run` does.

    from wod_etl.orchestration.pipeline import run_pipeline
    result = run_pipeline("0303928")         # or a URL, a local folder, a .tar.gz
    result["exit_code"], result["problems"]

Nine stages, in order: resolve, fetch, triage, decide, stage, process, taxa, verify, report.
Each records its status, its outputs, any problems (what failed, why, how to fix it) and a
FINGERPRINT of its inputs in `data/runs/<accession>/state.json`. A rerun skips every stage
that is done and whose inputs are unchanged, and continues from the first that is not:

- a stage that failed reruns, and the process stage resumes per file;
- a stage whose inputs changed -- a file re-downloaded, a decision edited -- reruns from
  scratch, clearing only the outputs it owns, and so does everything after it that sees the
  change;
- the decide stage pauses the run (exit 2) until decisions.toml settles every judgement.

The report stage always runs, so `report.md` describes the run however it ended. Everything
is logged to the accession's run log. Nothing outside `data/runs/<acc>/`, the mirror
`data/raw/<acc>/` and the `pipeline/` folders under `data/staging/<acc>/` and
`data/processed/<acc>/` is ever written or cleared.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from wod_etl import config, runlog
from wod_etl.io_util import write_json_atomic
from wod_etl.orchestration import decisions as dec
from wod_etl.orchestration import remedies

log = logging.getLogger(__name__)

STAGES = ("resolve", "fetch", "triage", "decide", "stage", "process", "taxa", "verify",
          "report")

EXIT_OK, EXIT_FAILED, EXIT_DECISIONS, EXIT_UNVERIFIED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


class Locked(RuntimeError):
    pass


# --------------------------------------------------------------------- workspace
class Workspace:
    """Where one accession's run lives. Paths are read from config when built, so a test can
    point config at a temporary folder."""

    def __init__(self, accession: str):
        self.accession = accession
        self.run_dir = Path(config.RUNS_DIR) / accession
        self.mirror_dir = Path(config.RAW_DIR) / accession
        self.staging_dir = Path(config.STAGING_DIR) / accession / "pipeline"
        self.processed_dir = Path(config.PROCESSED_DIR) / accession / "pipeline"
        self.work_dir = self.run_dir / "work"
        self.state_path = self.run_dir / "state.json"
        self.decisions_path = self.run_dir / "decisions.toml"
        self.triage_path = self.run_dir / "triage.json"
        self.listing_path = self.run_dir / "listing.json"
        self.report_json = self.run_dir / "report.json"
        self.report_md = self.run_dir / "report.md"
        self.lock_path = self.run_dir / ".lock"
        self.log_path = self.run_dir / runlog.TEXT_LOG


def load_state(ws: Workspace) -> Dict[str, Any]:
    try:
        return json.loads(ws.state_path.read_text())
    except (OSError, ValueError):
        return {"accession": ws.accession, "stages": {}}


def save_state(ws: Workspace, state: Dict[str, Any]) -> None:
    state["updated_at"] = _now()
    write_json_atomic(ws.state_path, state)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _lock(ws: Workspace):
    """One run per accession at a time -- a person and an agent writing one state.json and
    one manifest would each undo the other. A lock left by a process that is gone is taken
    over."""
    ws.run_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(ws.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid = int(ws.lock_path.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _alive(pid):
                raise Locked(f"another run (pid {pid}) is working on accession "
                             f"{ws.accession}. Wait for it, or delete {ws.lock_path} if that "
                             f"process is gone.")
            ws.lock_path.unlink(missing_ok=True)
    else:
        raise Locked(f"could not take {ws.lock_path}")
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        yield
    finally:
        ws.lock_path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------- fingerprints
def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _tree(root, keys: Optional[List[str]] = None) -> List[List[Any]]:
    """(relative path, size, mtime) for every file under `root`, or for `keys` only.

    Nanosecond mtimes: a staged file rewritten within the same second at the same size
    (`PSU` -> `psu`) must still count as changed.
    """
    root = Path(root)
    if not root.exists():
        return []
    paths = ([root / k for k in keys] if keys is not None else
             [p for p in root.rglob("*") if p.is_file() and ".building" not in p.parts])
    out = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            out.append([p.relative_to(root).as_posix(), None, None])
            continue
        out.append([p.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns])
    return sorted(out)


def _git_head() -> Optional[str]:
    return runlog._git_head()


# --------------------------------------------------------------------- the run
class Run:
    """One invocation: the workspace, the input, the state, and what earlier stages found."""

    def __init__(self, ws: Workspace, source: str, state: Dict[str, Any],
                 progress: Callable[[str], None]):
        self.ws, self.source, self.state, self.progress = ws, source, state, progress
        self.invocation = (runlog.active() or {}).get("invocation", "")
        self.forced: set = set()          # stages named by --from: rebuilt whatever changed

    def out(self, stage: str) -> Dict[str, Any]:
        return (self.state["stages"].get(stage) or {}).get("outputs") or {}

    @property
    def data_root(self) -> Optional[str]:
        return self.out("fetch").get("data_root") or self.out("resolve").get("data_root")

    def triage(self) -> Dict[str, Any]:
        try:
            return json.loads(self.ws.triage_path.read_text())
        except (OSError, ValueError):
            return {}

    def decisions(self):
        return dec.load(self.ws.decisions_path)


def _outcome(status: str, summary: str, outputs: Optional[Dict[str, Any]] = None,
             problems: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"status": status, "summary": summary, "outputs": outputs or {},
            "problems": problems or []}


# --------------------------------------------------------------------- 1 resolve
def _fp_resolve(run: Run) -> str:
    return _hash({"input": run.source})


def stage_resolve(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.discovery import list_remote_directory
    from wod_etl.discovery.list_remote_directory import _is_archive

    src = run.source
    if os.path.isdir(src):
        root = str(Path(src).resolve())
        n = sum(1 for p in Path(root).rglob("*") if p.is_file())
        return _outcome("done", f"local folder {src} ({n} files, used in place)",
                        {"mode": "local", "data_root": root, "files": n})
    listing = list_remote_directory(src, recursive=True)
    if listing.get("error") or not listing.get("items"):
        return _outcome("failed", f"could not list {src}", problems=[
            remedies.for_listing(listing, src)])
    items = [{"key": _rel(listing["source"], i["path"]), "url": i["path"],
              "size": i.get("size_bytes"), "modified": i.get("last_modified")}
             for i in listing["items"] if not i["is_dir"]]
    if os.path.isfile(src) and _is_archive(src):
        return _outcome("done", f"archive {src} extracted to {listing['source']} "
                                f"({len(items)} files)",
                        {"mode": "archive", "data_root": listing["source"],
                         "files": len(items)})
    write_json_atomic(run.ws.listing_path, items)
    version = listing.get("version") or {}
    resolution = listing.get("resolution") or {}
    return _outcome(
        "done", f"{listing['source']} ({len(items)} files"
                + (f", version {version['chosen']} of {version['available']}" if version else "")
                + ")",
        {"mode": "remote", "source": listing["source"], "files": len(items),
         "resolution": resolution or None, "version": version or None})


def _rel(root: str, path: str) -> str:
    r = root.rstrip("/") + "/"
    return path[len(r):] if path.startswith(r) else path.rsplit("/", 1)[-1]


# --------------------------------------------------------------------- 2 fetch
def _fp_fetch(run: Run) -> str:
    if run.out("resolve").get("mode") != "remote":
        return _hash({"mode": run.out("resolve").get("mode"),
                      "root": run.out("resolve").get("data_root")})
    try:
        listing = json.loads(run.ws.listing_path.read_text())
    except (OSError, ValueError):
        listing = []
    return _hash({"listing": listing, "mirror": _tree(run.ws.mirror_dir)})


def stage_fetch(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.discovery import download_file

    resolved = run.out("resolve")
    if resolved.get("mode") != "remote":
        return _outcome("done", f"{resolved.get('files')} files (in place)",
                        {"data_root": resolved["data_root"]})
    items = json.loads(run.ws.listing_path.read_text())
    mirror = run.ws.mirror_dir
    fetched = cached = 0
    problems = []
    for it in items:
        key = it["key"]
        sub = str(Path(key).parent) if str(Path(key).parent) != "." else None
        dest = mirror / key
        stale = dest.exists() and it.get("modified") and \
            int(dest.stat().st_mtime) != int(_epoch(it["modified"]) or dest.stat().st_mtime)
        res = download_file(it["url"], dest_dir=str(mirror), subdir=sub,
                            overwrite=bool(stale))
        if res["status"] == "error":
            problems.append(remedies.for_download(key, res.get("error", "")))
            runlog.event(log, logging.WARNING, f"could not fetch {key}: {res.get('error')}",
                         kind="fetch", key=key, error=res.get("error"))
            continue
        if res["status"] == "cached":
            cached += 1
        else:
            fetched += 1
            when = _epoch(it.get("modified"))
            if when:
                os.utime(res["local_path"], (when, when))
    summary = f"{fetched} fetched, {cached} already here, {len(problems)} failed"
    if problems:
        return _outcome("failed", summary, {"data_root": str(mirror)}, problems)
    return _outcome("done", summary, {"data_root": str(mirror)})


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------- 3 triage
def _fp_triage(run: Run) -> str:
    return _hash({"root": run.data_root, "tree": _tree(run.data_root or "")})


def stage_triage(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.discovery import classify_accession

    report = classify_accession(run.data_root, accession=run.ws.accession)
    if report.get("error"):
        return _outcome("failed", "triage failed", problems=[remedies.problem(
            "triage", "could not triage the accession", report["error"],
            "Check that the fetch stage finished; rerun with --from fetch if files are "
            "missing.")])
    write_json_atomic(run.ws.triage_path, report)
    counts = {a: len(report.get(f"{a}_files") or []) for a in
              ("process", "stage", "sidecar", "exclude", "document", "inspect")}
    return _outcome("done", " · ".join(f"{n} {a}" for a, n in counts.items() if n),
                    {"counts": counts, "triage_hash": _hash(report)})


# --------------------------------------------------------------------- 4 decide
def _decision_sources(decisions: Dict[str, Any]) -> List[str]:
    keys = set()
    for r in list((decisions.get("profiles") or {}).values()) + \
            list((decisions.get("taxa") or {}).values()):
        keys |= set(r.get("files") or []) | {r.get("file"), r.get("station_list")}
    keys |= {c.get("file") for c in decisions.get("cross_check") or []}
    return sorted(k for k in keys if k and k != dec.PLACEHOLDER)


def _fp_decide(run: Run) -> str:
    decisions, err = run.decisions()
    root = Path(run.data_root or "")
    return _hash({"triage": run.out("triage").get("triage_hash"), "decisions": decisions,
                  "error": err,
                  "sources": _tree(root, [k for k in _decision_sources(decisions)
                                          if (root / k).exists()])})


def stage_decide(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.recipes import check_profile_recipe

    ws, triage = run.ws, run.triage()
    label = f"run {run.invocation or _now()}"
    decisions, err = run.decisions()
    if err:
        return _outcome("failed", "decisions.toml cannot be read",
                        problems=[remedies.for_decisions_syntax(err)])
    added = dec.append(ws.decisions_path, dec.propose(triage, decisions), ws.accession, label)
    decisions, err = run.decisions()
    if err:
        return _outcome("failed", "decisions.toml cannot be read",
                        problems=[remedies.for_decisions_syntax(err)])
    conflicts, warnings = dec.crosscheck(decisions, run.data_root)
    added += dec.append(ws.decisions_path, dec.conflict_sections(conflicts, decisions),
                        ws.accession, label)
    decisions, err = run.decisions()
    if err:
        return _outcome("failed", "decisions.toml cannot be read",
                        problems=[remedies.for_decisions_syntax(err)])
    checks = {name: check_profile_recipe(r, run.data_root)
              for name, r in (decisions.get("profiles") or {}).items()}
    items, plan = dec.validate(decisions, triage, checks, conflicts)
    problems = [remedies.for_open_item(i) for i in items]
    problems += [remedies.problem("decide", w, "a cross-check source could not be read",
                                  "Fix the [[cross_check]] entry, or remove it.",
                                  blocking=False, kind="warning") for w in warnings]
    for c in conflicts:
        entry = (decisions.get("conflicts") or {}).get(c["id"]) or {}
        if entry.get("resolution"):
            runlog.event(log, logging.INFO, f"conflict {c['id']} settled as "
                                            f"{entry['resolution']!r}: {c['what']}",
                         kind="decision", id=c["id"], resolution=entry["resolution"],
                         note=entry.get("note"))
    blocking = [i for i in items if i["blocking"]]
    outputs = {"plan": plan, "conflicts": conflicts, "recipe_checks": checks,
               "proposals_added": added,
               "pending": [i for i in items if not i["blocking"]]}
    if blocking:
        return _outcome("blocked", f"{len(blocking)} decision(s) needed"
                                   + (f", {len(items) - len(blocking)} pending"
                                      if len(items) > len(blocking) else ""),
                        outputs, problems)
    return _outcome("done", f"{len(plan['direct'])} file(s) direct, "
                            f"{len(plan['profiles'])} profile recipe(s), "
                            f"{len(plan['taxa'])} taxa recipe(s)"
                            + (f", {len(items)} pending" if items else ""),
                    outputs, problems)


# --------------------------------------------------------------------- 5 stage
def _part_fp(run: Run, part: str) -> str:
    plan = run.out("decide").get("plan") or {}
    recipes = plan.get(part) or {}
    root = Path(run.data_root or "")
    inputs = set()
    scripts = {}
    for r in recipes.values():
        inputs |= set(r.get("files") or []) | {r.get("file"), r.get("station_list")}
        if r.get("kind") == "script" and r.get("script"):
            sp = Path(r["script"]) if Path(r["script"]).is_absolute() else \
                Path(config.ROOT) / r["script"]
            scripts[r["script"]] = (hashlib.sha256(sp.read_bytes()).hexdigest()
                                    if sp.exists() else None)
    inputs = [k for k in inputs if k and (root / k).exists()]
    return _hash({"recipes": recipes, "metadata": plan.get("metadata"),
                  "overrides": plan.get("station_overrides"), "inputs": _tree(root, inputs),
                  "scripts": scripts})


def _fp_stage(run: Run) -> str:
    return _hash({"profiles": _part_fp(run, "profiles"), "taxa": _part_fp(run, "taxa")})


def stage_stage(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.recipes import run_profile_recipe, run_taxa_recipe

    plan = run.out("decide").get("plan") or {}
    previous = (run.state["stages"].get("stage") or {}).get("outputs") or {}
    outputs: Dict[str, Any] = {"parts": {}}
    problems = []
    for part, runner in (("profiles", run_profile_recipe), ("taxa", run_taxa_recipe)):
        fp = _part_fp(run, part)
        prev = (previous.get("parts") or {}).get(part) or {}
        final = run.ws.staging_dir / part
        if "stage" not in run.forced and prev.get("fingerprint") == fp and prev.get("ok") \
                and (final.exists() or not plan.get(part)):
            outputs["parts"][part] = prev
            continue
        building = run.ws.staging_dir / f"{part}.building"
        shutil.rmtree(building, ignore_errors=True)
        results, ok = [], True
        for name, recipe in (plan.get(part) or {}).items():
            try:
                results.append(runner(name, recipe, run.data_root, building,
                                      run.ws.accession, plan))
            except Exception as e:                      # noqa: BLE001 - reported to the person
                ok = False
                runlog.event(log, logging.ERROR, f"recipe {part}.{name} failed: {e}",
                             kind="recipe", recipe=f"{part}.{name}",
                             traceback=traceback.format_exc(limit=6))
                problems.append(remedies.problem(
                    "stage", f"recipe [{part}.{name}] could not be run",
                    f"{type(e).__name__}: {e}",
                    f"Fix [{part}.{name}] in decisions.toml (files, keys, station list, "
                    f"columns) and rerun.", decisions_key=f"{part}.{name}"))
        if ok:
            shutil.rmtree(final, ignore_errors=True)
            if building.exists():
                building.rename(final)
        else:
            shutil.rmtree(building, ignore_errors=True)
        outputs["parts"][part] = {"fingerprint": fp, "ok": ok,
                                  "recipes": [{k: v for k, v in r.items() if k != "written"}
                                              | {"files": len(r.get("written") or [])}
                                              for r in results]}
    n = {p: sum(r["files"] for r in v.get("recipes") or []) for p, v in outputs["parts"].items()}
    summary = f"{n.get('profiles', 0)} cast file(s), {n.get('taxa', 0)} haul file(s) staged"
    return _outcome("failed" if problems else "done", summary, outputs, problems)


# --------------------------------------------------------------------- 6 process
def _skipped(decisions: Dict[str, Any], key: str) -> bool:
    entry = dec.file_decision(decisions, key) or {}
    return str(entry.get("action", "")).strip() in ("skip", "exclude")


def _fp_process(run: Run) -> str:
    plan = run.out("decide").get("plan") or {}
    decisions, _ = run.decisions()
    root = Path(run.data_root or "")
    return _hash({"direct": _tree(root, [k for k in plan.get("direct") or []
                                         if (root / k).exists()]),
                  "overrides": plan.get("dataset_overrides"),
                  "staged": _tree(run.ws.staging_dir / "profiles"),
                  # Only the actions this stage reads: a skip decided for a staged cast. A
                  # direct file's skip already shows in `direct`; a plankton sheet's action
                  # is none of this stage's business.
                  "files": {k: v for k, v in (decisions.get("files") or {}).items()
                            if k.startswith(STAGED_PREFIX)},
                  "recipe_types": {n: r.get("dataset_type")
                                   for n, r in (plan.get("profiles") or {}).items()}})


STAGED_PREFIX = "staged/"


def stage_process(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.process_accession import _output_stem, process_accession

    ws = run.ws
    plan = run.out("decide").get("plan") or {}
    decisions, _ = run.decisions()
    out_dir = ws.processed_dir / "profiles"
    if changed:
        shutil.rmtree(out_dir, ignore_errors=True)
    direct = [k for k in plan.get("direct") or [] if not _skipped(decisions, k)]
    staged_root = ws.staging_dir / "profiles"
    staged = [p.relative_to(staged_root).as_posix() for p in sorted(staged_root.rglob("*.csv"))
              ] if staged_root.exists() else []
    staged = [k for k in staged if not _skipped(decisions, STAGED_PREFIX + k)]
    stems = [_output_stem(k) for k in direct + staged]
    clashes = sorted({s for s in stems if stems.count(s) > 1})
    if clashes:
        return _outcome("failed", "output names collide", problems=[remedies.problem(
            "process", "two inputs would write the same output file", ", ".join(clashes),
            "Rename the recipe (its name prefixes staged files) or skip one of the files.")])
    runs = {}
    common = dict(output_dir=str(out_dir), raw_dir=str(ws.work_dir), verify=False,
                  accession=ws.accession, resume=not changed, retry_failed=not changed)
    if direct:
        runs["direct"] = process_accession(
            run.data_root, run_id=f"{ws.accession}_direct", include=direct,
            dataset_overrides=plan.get("dataset_overrides") or None, **common)
    if staged:
        overrides = {n: r["dataset_type"] for n, r in (plan.get("profiles") or {}).items()
                     if r.get("dataset_type")}
        runs["staged"] = process_accession(
            str(staged_root), run_id=f"{ws.accession}_staged", include=staged,
            dataset_overrides=overrides or None, **common)
    if not runs:
        return _outcome("done", "nothing to process: no file needs no staging and no recipe "
                                "staged a cast", {"runs": {}})

    problems, needs_decision, transient = [], [], False
    new_items = []
    for which, res in runs.items():
        if res.get("status") in ("error", "empty"):
            problems.append(remedies.problem("process", f"the {which} run could not start",
                                             res.get("error") or res.get("message", ""),
                                             res.get("hint") or "See run.log."))
            transient = True
            continue
        for f in res.get("failures") or []:
            key = f["key"] if which == "direct" else STAGED_PREFIX + f["key"]
            p = remedies.for_file_failure(key, f.get("stage") or "", f.get("error") or "",
                                          staged=which == "staged")
            problems.append(p)
            if (f.get("stage") or "") in remedies.DETERMINISTIC_FILE_STAGES:
                needs_decision.append(key)
                if not dec.has_item(decisions, "files", key):
                    new_items.append(dec.section(
                        dec.item_header("files", key), {"action": ""},
                        comments=[f"FAILED at {f.get('stage')}: {f.get('error')}",
                                  f"How: {p['how_to_fix']}"]))
            else:
                transient = True
        if res.get("status") == "aborted":
            transient = True
            problems.append(remedies.problem("process", f"the {which} run aborted",
                                             res.get("abort_reason", ""),
                                             "Something systematic is wrong; read the first "
                                             "failure in run.log, fix it, then rerun."))
    if new_items:
        dec.append(ws.decisions_path, new_items, ws.accession,
                   f"run {run.invocation or _now()} (process failures)")
    keep = {k: v for k, v in (("direct", runs.get("direct")), ("staged", runs.get("staged")))
            if v}
    outputs = {"runs": {k: _process_summary(v) for k, v in keep.items()},
               "needs_decision": needs_decision}
    done = sum((v.get("counts") or {}).get("done", 0) for v in keep.values())
    failed = sum((v.get("counts") or {}).get("failed", 0) for v in keep.values())
    summary = f"{done} file(s) written, {failed} failed"
    if transient:
        return _outcome("failed", summary, outputs, problems)
    if needs_decision:
        return _outcome("blocked", summary + f"; {len(needs_decision)} need a decision",
                        outputs, problems)
    return _outcome("done", summary, outputs, problems)


def _process_summary(res: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("status", "counts", "outputs", "failures", "dataset_types", "skipped_reasons",
            "unstorable_columns_dropped", "outputs_without_time",
            "outputs_without_time_of_day", "coded_headers_not_resolved", "manifest_path",
            "include_missing", "abort_reason")
    return {k: res.get(k) for k in keys if res.get(k) is not None}


# --------------------------------------------------------------------- 7 taxa
def _fp_taxa(run: Run) -> str:
    return _hash({"staged": _tree(run.ws.staging_dir / "taxa")})


def stage_taxa(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.process_taxa import process_taxa

    staged = run.ws.staging_dir / "taxa"
    out_dir = run.ws.processed_dir / "taxa"
    pending = [i for i in run.out("decide").get("pending") or []
               if i["decisions_key"].startswith("taxa.")]
    if not staged.exists() or not any(staged.rglob("*.csv")):
        shutil.rmtree(out_dir, ignore_errors=True)
        return _outcome("done", "no confirmed taxa recipe"
                                + (f" ({len(pending)} pending)" if pending else ""),
                        {"pending": len(pending)})
    shutil.rmtree(out_dir, ignore_errors=True)
    res = process_taxa(staged, out_dir, accession=run.ws.accession)
    problems = [remedies.problem("taxa", f"haul {f['file']} failed at {f['stage']}",
                                 str(f["error"]), "Fix its [taxa.<name>] recipe in "
                                                  "decisions.toml and rerun.", file=f["file"])
                for f in res["failures"]]
    outputs = {k: v for k, v in res.items() if k != "note"}
    summary = (f"{len(res['outputs'])} haul(s), {res['taxa_records']} Taxa-Record(s), "
               f"{len(res['failures'])} failed")
    return _outcome("failed" if problems else "done", summary, outputs, problems)


# --------------------------------------------------------------------- 8 verify
def _fp_verify(run: Run) -> str:
    return _hash({"profiles": _tree(run.ws.processed_dir / "profiles"),
                  "taxa": _tree(run.ws.processed_dir / "taxa")})


def stage_verify(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.verify import verify_output

    profiles = run.ws.processed_dir / "profiles"
    if not profiles.exists() or not any(profiles.glob("*.nc")):
        return _outcome("done", "no profile output to verify", {"output_verified": None})
    report = verify_output(str(profiles))
    problems = [remedies.for_finding(f) for f in report["findings"]
                if f["severity"] in ("high", "medium")]
    outputs = {"output_verified": report["passed"], "finding_counts": report["finding_counts"],
               "findings": report["findings"], "variables": report["variables"],
               "text": report["text"]}
    summary = (f"{report['netcdf_files']} file(s), {report['casts']} cast(s): "
               + ("verified" if report["passed"] else "NOT verified")
               + f", findings {report['finding_counts']}")
    return _outcome("done", summary, outputs, problems)


FINGERPRINTS = {"resolve": _fp_resolve, "fetch": _fp_fetch, "triage": _fp_triage,
                "decide": _fp_decide, "stage": _fp_stage, "process": _fp_process,
                "taxa": _fp_taxa, "verify": _fp_verify}
RUNNERS = {"resolve": stage_resolve, "fetch": stage_fetch, "triage": stage_triage,
           "decide": stage_decide, "stage": stage_stage, "process": stage_process,
           "taxa": stage_taxa, "verify": stage_verify}


# --------------------------------------------------------------------- driver
def run_pipeline(source: str, accession_id: Optional[str] = None,
                 from_stage: Optional[str] = None,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run (or resume) every stage for one accession. Never raises on data; returns
    `{"accession", "exit_code", "stages", "problems", "paths"}`."""
    from wod_etl.discovery import accession_for
    from wod_etl.orchestration import report as reporting

    say = progress or (lambda line: None)
    accession = accession_id or accession_for(source)
    if not accession:
        p = remedies.problem("resolve", f"cannot tell which accession {source!r} is",
                             "no NCEI accession number in the URL, the number, or the path",
                             "Pass --accession-id <name> to name it.")
        return {"accession": None, "exit_code": EXIT_FAILED, "problems": [p], "stages": {},
                "paths": {}}
    if from_stage and from_stage not in STAGES:
        p = remedies.problem("resolve", f"unknown stage {from_stage!r}", "",
                             "Use one of: " + ", ".join(STAGES))
        return {"accession": accession, "exit_code": EXIT_FAILED, "problems": [p],
                "stages": {}, "paths": {}}
    ws = Workspace(accession)
    try:
        with _lock(ws):
            return _run_locked(ws, source, from_stage, say, reporting)
    except Locked as e:
        p = remedies.problem("resolve", "the accession is already being run", str(e),
                             "Wait for the other run to finish.")
        return {"accession": accession, "exit_code": EXIT_FAILED, "problems": [p],
                "stages": {}, "paths": {"run_dir": str(ws.run_dir)}}


def _run_locked(ws: Workspace, source: str, from_stage: Optional[str],
                say: Callable[[str], None], reporting) -> Dict[str, Any]:
    with runlog.accession_log(ws.accession, actor="cli",
                              arguments={"source": source, "from": from_stage}) as logctx:
        state = load_state(ws)
        if state.get("input") and state["input"] != source:
            runlog.event(log, logging.WARNING,
                         f"input changed from {state['input']!r} to {source!r}: starting "
                         f"again from resolve", kind="input_changed")
            from_stage = from_stage or "resolve"
        state.update({"accession": ws.accession, "input": source})
        state.setdefault("created_at", _now())
        state.setdefault("stages", {})
        for s in state["stages"].values():
            if s.get("status") == "running":
                s["status"] = "pending"
        forced = set(STAGES[STAGES.index(from_stage):]) if from_stage else set()
        head = runlog._git_head()
        if state.get("git") and head and state["git"] != head:
            runlog.event(log, logging.WARNING,
                         f"the code changed since the last run ({state['git']} -> {head}); "
                         f"finished stages are not rerun -- use --from process to redo the "
                         f"output with the new code", kind="code_changed")
            say(f"note: the code changed since the last run ({state['git']} -> {head}). "
                f"Rerun with --from process to rebuild the output with it.")
        state["git"] = head
        save_state(ws, state)

        run = Run(ws, source, state, say)
        run.forced = forced
        exit_code = EXIT_OK
        interrupted = False
        for i, name in enumerate(STAGES[:-1], start=1):
            prior = state["stages"].get(name) or {}
            try:
                fp = FINGERPRINTS[name](run)
            except Exception:                           # noqa: BLE001 - inputs vanished
                fp = None
            if prior.get("status") == "done" and prior.get("fingerprint") == fp \
                    and name not in forced:
                say(f"[{i}/{len(STAGES)}] {name:<8} ✓ {prior.get('summary', '')} (unchanged)")
                continue
            changed = name in forced or prior.get("fingerprint") != fp
            state["stages"][name] = {**prior, "status": "running", "started_at": _now(),
                                     "finished_at": None}
            save_state(ws, state)
            runlog.event(log, logging.INFO, f"stage {name}: starting"
                                            + (" (inputs changed)" if changed and prior else ""),
                         kind="stage_start", stage=name, changed=changed)
            try:
                outcome = RUNNERS[name](run, changed)
            except KeyboardInterrupt:
                state["stages"][name]["status"] = "pending"
                save_state(ws, state)
                runlog.event(log, logging.WARNING, f"stage {name}: interrupted",
                             kind="stage_end", stage=name, status="interrupted")
                interrupted = True
                exit_code = EXIT_INTERRUPTED
                break
            except Exception as e:                      # noqa: BLE001 - a bug, reported
                runlog.event(log, logging.ERROR, f"stage {name} raised {type(e).__name__}: {e}",
                             kind="stage_error", stage=name,
                             traceback=traceback.format_exc(limit=8))
                outcome = _outcome("failed", f"raised {type(e).__name__}",
                                   problems=[remedies.for_exception(name, e)])
            try:
                fp_after = FINGERPRINTS[name](run) if outcome["status"] == "done" else fp
            except Exception:                           # noqa: BLE001
                fp_after = fp
            state["stages"][name] = {"status": outcome["status"], "summary": outcome["summary"],
                                     "outputs": outcome["outputs"],
                                     "problems": outcome["problems"], "fingerprint": fp_after,
                                     "started_at": state["stages"][name]["started_at"],
                                     "finished_at": _now(), "git": head}
            # A stage that reran invalidates nothing by itself: each later stage compares
            # its own inputs. But a later stage recorded as done from an EARLIER attempt of
            # this one must not be trusted blindly -- its fingerprint will say.
            save_state(ws, state)
            mark = {"done": "✓", "failed": "✗", "blocked": "⏸"}.get(outcome["status"], "?")
            say(f"[{i}/{len(STAGES)}] {name:<8} {mark} {outcome['summary']}")
            runlog.event(log, logging.WARNING if outcome["status"] != "done" else logging.INFO,
                         f"stage {name}: {outcome['status']} -- {outcome['summary']}",
                         kind="stage_end", stage=name, status=outcome["status"],
                         problems=outcome["problems"])
            if outcome["status"] == "failed":
                exit_code = EXIT_FAILED
                break
            if outcome["status"] == "blocked":
                exit_code = EXIT_DECISIONS
                break
        else:
            if run.out("verify").get("output_verified") is False:
                exit_code = EXIT_UNVERIFIED

        state["last_exit_code"] = exit_code
        state["last_invocation"] = run.invocation
        save_state(ws, state)
        report = reporting.write_report(ws, state, exit_code, interrupted)
        say(f"[{len(STAGES)}/{len(STAGES)}] report   ✓ {ws.report_md}")
        logctx["status"] = f"exit {exit_code}"
        return {"accession": ws.accession, "exit_code": exit_code, "stages": state["stages"],
                "problems": report["problems"], "paths": report["paths"]}


def status(accession: str) -> Dict[str, Any]:
    """The last recorded state of an accession's run, read-only."""
    ws = Workspace(accession)
    state = load_state(ws)
    problems = [p for s in STAGES for p in (state["stages"].get(s) or {}).get("problems") or []]
    return {"accession": accession, "input": state.get("input"),
            "last_exit_code": state.get("last_exit_code"),
            "stages": {s: {k: (state["stages"].get(s) or {}).get(k)
                           for k in ("status", "summary", "finished_at")}
                       for s in STAGES if s in state["stages"]},
            "problems": problems,
            "paths": {"run_dir": str(ws.run_dir), "log": str(ws.log_path),
                      "decisions": str(ws.decisions_path), "report": str(ws.report_md)},
            "exists": ws.state_path.exists()}
