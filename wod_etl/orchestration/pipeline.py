"""Run one accession in two resumable steps: `wod-etl run` and `wod-etl confirm`.

`run` resolves, fetches and triages the input and writes decisions.toml for review; `confirm`
stages, processes and verifies what the review included. Each stage records its status, outputs,
problems and an input fingerprint in `state.json`, and reruns only when those change.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import socket
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from wod_etl import config, runlog
from wod_etl.io_util import write_json_atomic
from wod_etl.orchestration import decisions as dec
from wod_etl.orchestration import remedies

log = logging.getLogger(__name__)

RUN_STAGES = ("resolve", "fetch", "triage", "decide")
CONFIRM_STAGES = ("check", "stage", "process", "verify")
STAGES = RUN_STAGES + CONFIRM_STAGES + ("report",)

EXIT_OK, EXIT_FAILED, EXIT_DECISIONS, EXIT_UNVERIFIED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


class Locked(RuntimeError):
    pass


# --------------------------------------------------------------------- workspace
class Workspace:
    """Paths of one accession's run, read from config when built."""

    def __init__(self, accession: str):
        self.accession = accession
        self.run_dir = Path(config.RUNS_DIR) / accession
        self.mirror_dir = Path(config.RAW_DIR) / accession
        self.staging_dir = Path(config.STAGING_DIR) / accession / "pipeline"
        # WOD files, one per dataset and year; only names from writers.wod_files are touched.
        self.processed_dir = Path(config.PROCESSED_DIR) / accession
        # Legacy output folder, removed when the accession is processed.
        self.legacy_output_dir = Path(config.PROCESSED_DIR) / accession / "pipeline"
        self.work_dir = self.run_dir / "work"
        # Each source file's own result, assembled into the WOD files.
        self.per_file_dir = self.work_dir / "per_file"
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
    """One run per accession at a time; a lock left by a dead process is taken over."""
    ws.run_dir.mkdir(parents=True, exist_ok=True)
    here = socket.gethostname()
    for _ in range(2):
        try:
            fd = os.open(ws.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid_text, _, host = ws.lock_path.read_text().strip().partition(" ")
                pid = int(pid_text or 0)
            except (OSError, ValueError):
                pid, host = 0, ""
            # A pid only means something on the machine that wrote it.
            if pid and host and host != here:
                raise Locked(f"a run on {host} (pid {pid}) is working on accession "
                             f"{ws.accession}. Wait for it, or delete {ws.lock_path} if it "
                             f"is not running.")
            if pid and pid != os.getpid() and _alive(pid):
                raise Locked(f"another run (pid {pid}) is working on accession "
                             f"{ws.accession}. Wait for it, or delete {ws.lock_path} if that "
                             f"process is gone.")
            ws.lock_path.unlink(missing_ok=True)
    else:
        raise Locked(f"could not take {ws.lock_path}")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{os.getpid()} {here}")
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

    Nanosecond mtimes catch a same-size rewrite within one second.
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
        self.command = "run"

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
        # A folder holding the workspace would make the pipeline ingest its own output.
        own = [str(p) for p in (run.ws.run_dir, run.ws.staging_dir, run.ws.processed_dir)
               if p.resolve().is_relative_to(root)]
        if own:
            return _outcome("failed", f"{src} contains this accession's workspace", problems=[
                remedies.problem("resolve", f"the input folder {src} contains the "
                                            f"pipeline's own folders", ", ".join(own),
                                 "Pass the accession's own folder (for example "
                                 "data/raw/<accession>), not one that holds data/runs, "
                                 "data/staging or data/processed.")])
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
    media = [it for it in items if Path(it["key"]).suffix.lower() in config.MEDIA_EXTENSIONS]
    if media:
        runlog.event(log, logging.INFO, f"not downloaded: {len(media)} image, video or audio "
                                        f"file(s)", kind="fetch", skipped=[m["key"] for m in media])
    for it in items:
        if it in media:
            continue
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
    if media:
        size = sum(m.get("size") or 0 for m in media) / 1e6
        summary += f"; {len(media)} image/video/audio file(s) not downloaded ({size:,.0f} MB)"
    outputs = {"data_root": str(mirror), "media_not_downloaded": [m["key"] for m in media]}
    if problems:
        return _outcome("failed", summary, outputs, problems)
    return _outcome("done", summary, outputs)


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
# Code hashed into stage fingerprints, so a code change reruns the stages it affects.
TRIAGE_CODE = ("wod_etl/discovery", "wod_etl/mapping", "wod_etl/reference", "wod_etl/staging",
               "wod_etl/config.py", "wod_etl/terms.py", "wod_etl/time_zones.py",
               "knowledge/wod_code_tables")
OUTPUT_CODE = TRIAGE_CODE + ("wod_etl/qc_transforms", "wod_etl/writers",
                             "wod_etl/verify", "wod_etl/orchestration/process_accession.py",
                             "wod_etl/orchestration/process_taxa.py",
                             "wod_etl/orchestration/recipes.py")
_CODE_HASHES: Dict[tuple, str] = {}


def _code_hash(parts: tuple) -> str:
    """A hash of the source and reference tables under `parts`, computed once per process."""
    if parts not in _CODE_HASHES:
        h = hashlib.sha256()
        for part in parts:
            root = Path(config.ROOT) / part
            for p in sorted(root.rglob("*")) if root.is_dir() else [root]:
                if p.is_file() and p.suffix in (".py", ".json", ".csv") \
                        and "__pycache__" not in p.parts:
                    h.update(p.relative_to(config.ROOT).as_posix().encode())
                    h.update(p.read_bytes())
        _CODE_HASHES[parts] = h.hexdigest()[:16]
    return _CODE_HASHES[parts]


def _fp_triage(run: Run) -> str:
    return _hash({"root": run.data_root, "tree": _tree(run.data_root or ""),
                  "code": _code_hash(TRIAGE_CODE)})


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
    for f in report["files"]:
        runlog.event(log, logging.INFO, f"triage {f['path']}: {f['action']} -- {f['data']}",
                     kind="triage_file", path=f["path"], action=f["action"],
                     include=f["include"], dataset=f["dataset"], data=f["data"],
                     why=f["why"], reason=f["reason"], evidence=f["evidence"])
    return _outcome("done", f"{report['file_count']} files: "
                            + " · ".join(f"{n} {a}" for a, n in counts.items() if n),
                    {"counts": counts, "triage_hash": _hash(report),
                     "decision_hash": _decision_hash(report)})


def _decision_hash(report: Dict[str, Any]) -> str:
    """Hash of the triage proposals the decisions rest on, excluding `data`/`why` wording."""
    return _hash([(f["path"], f["action"], f.get("include"), f.get("dataset"),
                   f.get("exclusion"), f.get("drop"),
                   {k: (f.get("long_format") or {}).get(k)
                    for k in ("key_columns", "variable_column", "value_column")})
                  for f in report.get("files") or []])


# --------------------------------------------------------------------- 4 decide, 5 check
def _decision_sources(decisions: Dict[str, Any]) -> List[str]:
    """Files the decisions name beyond the triaged ones: script recipe and cross-check inputs."""
    keys = set()
    for r in (decisions.get("profiles") or {}).values():
        if isinstance(r, dict):
            keys |= set(r.get("files") or []) | {r.get("station_list")}
    keys |= {c.get("file") for c in decisions.get("cross_check") or [] if isinstance(c, dict)}
    return sorted(k for k in keys if k and k != dec.PLACEHOLDER)


def _fp_decisions(run: Run) -> str:
    decisions, err = run.decisions()
    root = Path(run.data_root or "")
    return _hash({"triage": run.out("triage").get("triage_hash"), "decisions": decisions,
                  "error": err,
                  "sources": _tree(root, [k for k in _decision_sources(decisions)
                                          if (root / k).exists()])})


def _settle(run: Run, stage: str, propose: bool) -> Dict[str, Any]:
    """Append entries for undecided files (if `propose`), then validate and build the plan.

    Returns `{"outputs", "problems", "blocking"}`, or `{"error"}` if the file cannot be read.
    """
    from wod_etl.orchestration.recipes import check_profile_recipe, check_taxa_recipe

    ws, triage = run.ws, run.triage()
    label = f"{run.command} {run.invocation or _now()}"
    added = 0
    decisions, err = run.decisions()
    if err:
        return {"error": err}
    if propose:
        added += dec.append(ws.decisions_path, dec.propose(triage, decisions), ws.accession,
                            label, triage=triage)
        decisions, err = run.decisions()
        if err:
            return {"error": err}
    draft = dec.build_plan(decisions, triage, [])
    recipes = draft["profiles"]
    conflicts, warnings = dec.crosscheck(decisions, run.data_root, recipes, triage)
    # Checked as the plan will run them: only the files still included.
    checks = {name: check_profile_recipe(r, run.data_root) for name, r in recipes.items()}
    taxa_checks = {name: check_taxa_recipe(r, run.data_root)
                   for name, r in draft["taxa"].items()}
    items, plan = dec.validate(decisions, triage, checks, conflicts, taxa_checks)
    problems = [remedies.for_open_item(i, stage) for i in items]
    problems += [remedies.problem(stage, w, "a source of station positions could not be read",
                                  "Nothing to do: the check is skipped.",
                                  blocking=False, kind="warning") for w in warnings]
    for c in conflicts:
        runlog.event(log, logging.WARNING, f"station check {c['id']}: {c['what']} -- the "
                                           f"station list is kept",
                     kind="station_check", id=c["id"], values=c.get("values"))
    included = len(plan["direct"]) + sum(len(r.get("files") or [])
                                         for r in list(plan["profiles"].values())
                                         + list(plan["taxa"].values()))
    return {"outputs": {"plan": plan, "conflicts": conflicts, "recipe_checks": checks,
                        "taxa_checks": taxa_checks, "entries_added": added,
                        "included_files": included},
            "problems": problems,
            "blocking": [i for i in items if i["blocking"]]}


def stage_decide(run: Run, changed: bool) -> Dict[str, Any]:
    """Last `wod-etl run` stage: propose entries and list what to settle; never blocks."""
    res = _settle(run, "decide", propose=True)
    if res.get("error"):
        return _outcome("failed", "decisions.toml cannot be read",
                        problems=[remedies.for_decisions_syntax(res["error"], "decide")])
    out, plan = res["outputs"], res["outputs"]["plan"]
    for e in plan["entries"]:
        runlog.event(log, logging.INFO,
                     f"decision {e['key']} ({e['files']} file(s)): "
                     f"{'include as ' + str(e['dataset']) if e['include'] else 'leave out'}"
                     f" -- {e['why']}", kind="decision", key=e["key"], files=e["files"],
                     include=e["include"], dataset=e["dataset"], data=e["data"], why=e["why"])
    summary = (f"{out['included_files']} file(s) to include, {len(plan['left_out'])} to leave "
               f"out ({out['entries_added']} entr{'y' if out['entries_added'] == 1 else 'ies'} "
               f"added)")
    if res["blocking"]:
        summary += f"; {len(res['blocking'])} to settle before confirming"
    return _outcome("done", summary, out, res["problems"])


def stage_check(run: Run, changed: bool) -> Dict[str, Any]:
    """First `wod-etl confirm` stage: validate the decisions and build the plan. Blocks
    (exit 2) while anything is open."""
    res = _settle(run, "check", propose=False)
    if res.get("error"):
        return _outcome("failed", "decisions.toml cannot be read",
                        problems=[remedies.for_decisions_syntax(res["error"], "check")])
    out, plan = res["outputs"], res["outputs"]["plan"]
    if res["blocking"]:
        return _outcome("blocked", f"{len(res['blocking'])} decision(s) to settle",
                        out, res["problems"])
    for p, ds in plan["dataset_overrides"].items():
        options = (plan.get("file_options") or {}).get(p) or {}
        runlog.event(log, logging.INFO, f"{p}: written as {ds}"
                     + (f", without {', '.join(options['drop'])}" if options.get("drop") else "")
                     + (f", with {options['metadata']}" if options.get("metadata") else "")
                     + ", as decisions.toml says",
                     kind="decision", key=p, dataset=ds, drop=options.get("drop") or None,
                     metadata=options.get("metadata") or None)
    return _outcome("done", f"{len(plan['direct'])} file(s) to process as they stand, "
                            f"{len(plan['profiles']) + len(plan.get('taxa') or {})} recipe(s) "
                            f"to stage, "
                            f"{len(plan['left_out'])} file(s) left out",
                    out, res["problems"])


def _not_ready(run: Run) -> Optional[Dict[str, Any]]:
    """Why confirm cannot proceed: no decisions yet, or the files or triage code changed since."""
    stages = run.state["stages"]
    again = f"uv run wod-etl run {run.source}" if run.source else "uv run wod-etl run <input>"
    if (stages.get("decide") or {}).get("status") != "done" \
            or not run.ws.decisions_path.exists():
        return remedies.problem(
            "check", "there are no decisions to confirm yet",
            "`wod-etl run` writes them, and it has not finished for this accession",
            f"Run `{again}`, review {run.ws.decisions_path}, then confirm.", kind="decision")
    for name in ("fetch", "triage"):
        prior = stages.get(name) or {}
        try:
            fp = FINGERPRINTS[name](run)
        except Exception:                               # noqa: BLE001 - inputs vanished
            fp = None
        current = prior.get("status") == "done" and prior.get("fingerprint") == fp \
            and prior.get("left") == _left_behind(run, name)
        if not current and name == "triage" and prior.get("status") == "done" \
                and fp is not None and _triages_as_reviewed(run):
            # The code changed but triages the files as reviewed: no new review needed, and
            # the new triage replaces the old.
            prior.update({"fingerprint": fp, "left": _left_behind(run, name)})
            prior.setdefault("outputs", {})["triage_hash"] = _hash(run.triage())
            runlog.event(log, logging.INFO, "triage: the code changed, but it triages the "
                                            "files as reviewed", kind="triage_rechecked")
            current = True
        if not current:
            return remedies.problem(
                "check", "the files, or the code that triages them, changed since the "
                         "decisions were written",
                f"the {name} stage would not give what was reviewed",
                f"Run `{again}` again: it triages afresh and adds entries for any file that "
                f"has none, without touching yours. Review them, then confirm.",
                kind="decision")
    return None


def _triages_as_reviewed(run: Run) -> bool:
    """Whether a fresh triage matches the one the decisions came from; if so, it is saved."""
    from wod_etl.discovery import classify_accession

    recorded = run.out("triage").get("decision_hash")
    try:
        report = classify_accession(run.data_root, accession=run.ws.accession)
    except Exception:                                   # noqa: BLE001 - then it is not
        return False
    same = bool(recorded) and not report.get("error") and _decision_hash(report) == recorded
    if same:
        write_json_atomic(run.ws.triage_path, report)
    return same


# --------------------------------------------------------------------- 6 stage
def _plan(run: Run) -> Dict[str, Any]:
    return run.out("check").get("plan") or {}


def _fp_stage(run: Run) -> str:
    plan = _plan(run)
    recipes = {**(plan.get("profiles") or {}),
               **{f"taxa.{n}": r for n, r in (plan.get("taxa") or {}).items()}}
    root = Path(run.data_root or "")
    inputs = set()
    scripts = {}
    for r in recipes.values():
        inputs |= set(r.get("files") or []) | {r.get("station_list")}
        if r.get("kind") == "script" and r.get("script"):
            sp = Path(r["script"]) if Path(r["script"]).is_absolute() else \
                Path(config.ROOT) / r["script"]
            scripts[r["script"]] = (hashlib.sha256(sp.read_bytes()).hexdigest()
                                    if sp.exists() else None)
    inputs = [k for k in inputs if k and (root / k).exists()]
    return _hash({"recipes": recipes, "metadata": plan.get("metadata"),
                  "overrides": plan.get("station_overrides"), "inputs": _tree(root, inputs),
                  "scripts": scripts, "code": _code_hash(OUTPUT_CODE)})


STAGED_KINDS = ("profiles", "taxa")


def stage_stage(run: Run, changed: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.recipes import run_profile_recipe, run_taxa_recipe

    plan = _plan(run)
    runners = {"profiles": run_profile_recipe, "taxa": run_taxa_recipe}
    building = {k: run.ws.staging_dir / f"{k}.building" for k in STAGED_KINDS}
    for d in building.values():
        shutil.rmtree(d, ignore_errors=True)
    results, problems = [], []
    for kind in STAGED_KINDS:
        for name, recipe in (plan.get(kind) or {}).items():
            try:
                results.append(runners[kind](name, recipe, run.data_root, building[kind],
                                             run.ws.accession, plan))
            except Exception as e:                      # noqa: BLE001 - reported to the person
                runlog.event(log, logging.ERROR, f"recipe {kind}.{name} failed: {e}",
                             kind="recipe", recipe=f"{kind}.{name}",
                             traceback=traceback.format_exc(limit=6))
                files = ", ".join(recipe.get("files") or [])
                problems.append(remedies.problem(
                    "stage", f"recipe [{kind}.{name}] could not be run",
                    f"{type(e).__name__}: {e}",
                    f"Set include = false in decisions.toml on the file(s) it cannot stage "
                    f"({files}), and confirm again.", decisions_key=f"{kind}.{name}"))
    if problems:
        for d in building.values():
            shutil.rmtree(d, ignore_errors=True)
        return _outcome("failed", f"{len(problems)} recipe(s) failed", problems=problems)
    for kind in STAGED_KINDS:
        final = run.ws.staging_dir / kind
        shutil.rmtree(final, ignore_errors=True)
        if building[kind].exists():
            building[kind].rename(final)
    casts = sum(len(r.get("written") or []) for r in results if r.get("kind") != "taxa")
    hauls = sum(len(r.get("written") or []) for r in results if r.get("kind") == "taxa")
    staged = ", ".join(x for x in (f"{casts} cast file(s)" if casts else "",
                                   f"{hauls} haul file(s)" if hauls else "") if x)
    return _outcome("done", f"{staged or 'nothing'} staged by {len(results)} recipe(s)"
                            if results else "nothing to reshape",
                    {"recipes": [{k: v for k, v in r.items() if k != "written"}
                                 | {"files": len(r.get("written") or [])} for r in results]})


# --------------------------------------------------------------------- 7 process
def _fp_process(run: Run) -> str:
    plan = _plan(run)
    root = Path(run.data_root or "")
    return _hash({"direct": _tree(root, [k for k in plan.get("direct") or []
                                         if (root / k).exists()]),
                  "overrides": plan.get("dataset_overrides"),
                  "options": plan.get("file_options"),
                  "staged": _tree(run.ws.staging_dir / "profiles"),
                  "staged_taxa": _tree(run.ws.staging_dir / "taxa"),
                  "recipe_types": {n: r.get("dataset_type")
                                   for n, r in (plan.get("profiles") or {}).items()},
                  "code": _code_hash(OUTPUT_CODE)})


STAGED_PREFIX = "staged/"


def stage_process(run: Run, changed: bool) -> Dict[str, Any]:
    """Process every included file (direct or staged) into WOD files, one per dataset and year.

    Each file is processed and validated alone; one that fails is reported and the rest are
    still written."""
    from wod_etl.orchestration.process_accession import assemble_profiles, process_accession
    from wod_etl.orchestration.process_taxa import assemble_taxa

    ws = run.ws
    plan = _plan(run)
    if ws.legacy_output_dir.is_dir():
        shutil.rmtree(ws.legacy_output_dir, ignore_errors=True)
        runlog.event(log, logging.INFO, f"removed {ws.legacy_output_dir}: the output as it was "
                                        f"written before the WOD files",
                     kind="wod_file", status="removed", names=[str(ws.legacy_output_dir)])
    if changed:
        shutil.rmtree(ws.per_file_dir, ignore_errors=True)
    direct = list(plan.get("direct") or [])
    staged_root = ws.staging_dir / "profiles"
    staged = [p.relative_to(staged_root).as_posix() for p in sorted(staged_root.rglob("*.csv"))
              ] if staged_root.exists() else []
    runs = {}
    common = dict(output_dir=str(ws.processed_dir), per_file_dir=str(ws.per_file_dir),
                  assemble=False, raw_dir=str(ws.work_dir), verify=False,
                  accession=ws.accession, resume=not changed, retry_failed=not changed)
    if direct:
        runs["direct"] = process_accession(
            run.data_root, run_id=f"{ws.accession}_direct", include=direct,
            dataset_overrides=plan.get("dataset_overrides") or None,
            file_options=plan.get("file_options") or None, **common)
    if staged:
        overrides = {n: r["dataset_type"] for n, r in (plan.get("profiles") or {}).items()
                     if r.get("dataset_type")}
        runs["staged"] = process_accession(
            str(staged_root), run_id=f"{ws.accession}_staged", include=staged,
            dataset_overrides=overrides or None, **common)
    taxa_root = ws.staging_dir / "taxa"
    hauls_staged = taxa_root.is_dir() and any(taxa_root.rglob("*.csv"))
    if not runs and not hauls_staged:
        shutil.rmtree(ws.per_file_dir, ignore_errors=True)
        # With nothing to assemble, the WOD files of an earlier confirm are removed.
        assemble_profiles([], ws.processed_dir, ws.accession)
        assemble_taxa(None, ws.processed_dir, ws.accession)
        return _outcome("done", "nothing to process: the decisions include no file",
                        {"runs": {}, "files": [], "taxa_files": []})

    problems, transient = [], False
    for which, res in runs.items():
        if res.get("status") in ("error", "empty"):
            problems.append(remedies.problem("process", f"the {which} run could not start",
                                             res.get("error") or res.get("message", ""),
                                             res.get("hint") or "See run.log."))
            transient = True
            continue
        for f in res.get("failures") or []:
            key = f["key"] if which == "direct" else STAGED_PREFIX + f["key"]
            problems.append(remedies.for_file_failure(key, f.get("stage") or "",
                                                      f.get("error") or "",
                                                      staged=which == "staged"))
            transient = transient or (f.get("stage") or "") not in \
                remedies.DETERMINISTIC_FILE_STAGES
        if res.get("status") == "aborted":
            transient = True
            problems.append(remedies.problem("process", f"the {which} run aborted",
                                             res.get("abort_reason", ""),
                                             "Something systematic is wrong; read the first "
                                             "failure in run.log, fix it, then confirm "
                                             "again."))

    # Both runs together: direct and staged casts of one dataset and year share a WOD file.
    caches = [c for r in runs.values() for c in r.get("per_file_casts") or []]
    assembly = assemble_profiles(caches, ws.processed_dir, ws.accession)
    label = lambda key: STAGED_PREFIX + key if key in staged else key
    for p in assembly["problems"]:
        if p.get("file"):
            problems.append(remedies.for_wod_file_failure(
                p["file"], [label(k) for k in p["sources"]], p["stage"], p["error"]))
        else:
            transient = True
            problems.append(remedies.problem(
                "process", f"the processed result of {p['source']} cannot be read back",
                p["error"], f"Process every file again: uv run wod-etl confirm {ws.accession} "
                            f"--from process"))
    files = [{**f, "sources": [{**s, "key": label(s["key"])} for s in f["sources"]]}
             for f in assembly["files"]]
    # Plankton: one Taxa-Record file per year.
    taxa = assemble_taxa(taxa_root if hauls_staged else None, ws.processed_dir, ws.accession)
    problems += [remedies.for_haul_failure(f["file"], f["stage"], f["error"])
                 for f in taxa["failures"]]
    if taxa["files"]:
        problems += remedies.taxa_notes(taxa["taxa"])
    outputs = {"runs": {k: _process_summary(v) for k, v in runs.items()}, "files": files,
               "taxa_files": taxa["files"], "taxa": taxa["taxa"] if taxa["files"] else None,
               "removed": (assembly["removed"] or []) + taxa["removed"] or None}
    done = sum((v.get("counts") or {}).get("done", 0) for v in runs.values())
    failed = sum((v.get("counts") or {}).get("failed", 0) for v in runs.values())
    casts = sum(f["casts"] for f in files)
    summary = (f"{done} file(s) processed into {len(files)} WOD file(s) ({casts} cast(s))"
               + (f", {failed} failed" if failed else ""))
    if hauls_staged:
        n_hauls = sum(f["hauls"] for f in taxa["files"])
        summary += (f"; {n_hauls} haul(s) into {len(taxa['files'])} Taxa-Record file(s)"
                    + (f", {len(taxa['failures'])} failed" if taxa["failures"] else ""))
    # Deterministic failures are reported and the stage is done; transient ones fail the
    # stage so the next confirm retries them.
    return _outcome("failed" if transient else "done", summary, outputs, problems)


def _process_summary(res: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("status", "counts", "per_file_outputs", "failures", "dataset_types",
            "skipped_reasons",
            "unstorable_columns_dropped", "outputs_without_time",
            "outputs_without_time_of_day", "coded_headers_not_resolved", "casts_not_written",
            "casts_not_written_in", "levels_without_data", "casts_depth_from_pressure",
            "manifest_path", "include_missing", "abort_reason")
    return {k: res.get(k) for k in keys if res.get(k) is not None}


# --------------------------------------------------------------------- 8 verify
def wod_files(ws: Workspace) -> List[Path]:
    """This accession's WOD profile and Taxa-Record files in the output folder."""
    from wod_etl.writers import is_output_file_name

    if not ws.processed_dir.is_dir():
        return []
    return sorted(p for p in ws.processed_dir.iterdir()
                  if p.is_file() and is_output_file_name(p.name, ws.accession))


def _fp_verify(run: Run) -> str:
    return _hash({"files": _tree(run.ws.processed_dir,
                                 [p.name for p in wod_files(run.ws)]),
                  "code": _code_hash(OUTPUT_CODE)})


def stage_verify(run: Run, changed: bool) -> Dict[str, Any]:
    """Check profile files with verify_output and Taxa-Record files with
    validate_taxa_netcdf. The output is verified only if every file passes."""
    from wod_etl.verify import validate_taxa_netcdf, verify_output
    from wod_etl.writers import is_taxa_file_name

    files = wod_files(run.ws)
    if not files:
        return _outcome("done", "no output to verify", {"output_verified": None})
    taxa = [p for p in files if is_taxa_file_name(p.name, run.ws.accession)]
    profiles = [p for p in files if p not in taxa]
    outputs: Dict[str, Any] = {}
    problems, parts, passed = [], [], []
    if profiles:
        report = verify_output(str(run.ws.processed_dir), paths=[str(p) for p in profiles])
        problems += [remedies.for_finding(f) for f in report["findings"]
                     if f["severity"] in ("high", "medium")]
        outputs.update({"profiles_verified": report["passed"],
                        "finding_counts": report["finding_counts"],
                        "findings": report["findings"], "variables": report["variables"],
                        "netcdf_files": report["netcdf_files"], "casts": report["casts"],
                        "text": report["text"]})
        passed.append(report["passed"])
        parts.append(f"{report['netcdf_files']} file(s), {report['casts']} cast(s): "
                     + ("verified" if report["passed"] else "NOT verified")
                     + f", findings {report['finding_counts']}")
    if taxa:
        checks = {p.name: validate_taxa_netcdf(str(p)) for p in taxa}
        for name, c in checks.items():
            if not c["passed"]:
                problems.append(remedies.problem(
                    "verify", f"{name} is not a valid Taxa-Record file", "; ".join(c["errors"]),
                    "A bug in wod_etl (each haul passed on its own); see run.log.",
                    kind="finding"))
        ok = all(c["passed"] for c in checks.values())
        outputs.update({"taxa_verified": ok,
                        "taxa_checks": {n: {k: c.get(k) for k in ("passed", "errors",
                                                                  "warnings")}
                                        for n, c in checks.items()}})
        passed.append(ok)
        parts.append(f"{len(taxa)} Taxa-Record file(s): " + ("verified" if ok else
                                                             "NOT verified"))
    outputs["output_verified"] = all(passed)
    return _outcome("done", "; ".join(parts), outputs, problems)


FINGERPRINTS = {"resolve": _fp_resolve, "fetch": _fp_fetch, "triage": _fp_triage,
                "decide": _fp_decisions, "check": _fp_decisions, "stage": _fp_stage,
                "process": _fp_process, "verify": _fp_verify}
RUNNERS = {"resolve": stage_resolve, "fetch": stage_fetch, "triage": stage_triage,
           "decide": stage_decide, "check": stage_check, "stage": stage_stage,
           "process": stage_process, "verify": stage_verify}


def _left_behind(run: Run, name: str) -> Optional[str]:
    """Hash of a stage's outputs on disk; a change (e.g. deleted by hand) makes it rerun."""
    ws = run.ws
    trees = {"resolve": lambda: _tree(ws.run_dir, [ws.listing_path.name]),
             "triage": lambda: _tree(ws.run_dir, [ws.triage_path.name]),
             "stage": lambda: [_tree(ws.staging_dir / k) for k in STAGED_KINDS],
             "process": lambda: [_tree(ws.processed_dir, [p.name for p in wod_files(ws)]),
                                 _tree(ws.per_file_dir)]}
    return _hash(trees[name]()) if name in trees else None


# --------------------------------------------------------------------- drivers
def run_pipeline(source: str, accession_id: Optional[str] = None,
                 from_stage: Optional[str] = None,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """`wod-etl run`: resolve, fetch, triage and write decisions.toml for review.

    Never raises on data; returns `{"accession", "command", "exit_code", "stages", "problems",
    "paths"}`."""
    from wod_etl.discovery import accession_for

    accession = accession_id or accession_for(source)
    if not accession:
        return _refused(None, "resolve", f"cannot tell which accession {source!r} is",
                        "no NCEI accession number in the URL, the number, or the path",
                        "Pass --accession-id <name> to name it.")
    if from_stage and from_stage not in RUN_STAGES:
        return _refused(accession, "resolve", f"unknown stage {from_stage!r} for run", "",
                        "Use one of: " + ", ".join(RUN_STAGES))
    return _locked(Workspace(accession), source, "run", RUN_STAGES, from_stage, progress)


def confirm_pipeline(accession: str, from_stage: Optional[str] = None,
                     progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """`wod-etl confirm`: stage, process and verify what decisions.toml includes. Returns as
    run_pipeline."""
    if from_stage and from_stage not in CONFIRM_STAGES:
        return _refused(accession, "check", f"unknown stage {from_stage!r} for confirm", "",
                        "Use one of: " + ", ".join(CONFIRM_STAGES))
    ws = Workspace(accession)
    source = load_state(ws).get("input")
    if not source:
        return _refused(accession, "check", f"no run recorded for accession {accession}",
                        f"nothing in {ws.run_dir}",
                        "Run `uv run wod-etl run <input>` first, review the decisions it "
                        "writes, then confirm.", code=EXIT_DECISIONS)
    return _locked(ws, source, "confirm", CONFIRM_STAGES, from_stage, progress)


def _refused(accession, stage, what, why, how, code=EXIT_FAILED) -> Dict[str, Any]:
    return {"accession": accession, "command": None, "exit_code": code,
            "problems": [remedies.problem(stage, what, why, how)], "stages": {},
            "paths": {"run_dir": str(Workspace(accession).run_dir)} if accession else {}}


def _locked(ws: Workspace, source: str, command: str, stages: tuple,
            from_stage: Optional[str], progress) -> Dict[str, Any]:
    from wod_etl.orchestration import report as reporting

    say = progress or (lambda line: None)
    try:
        with _lock(ws):
            return _drive(ws, source, command, stages, from_stage, say, reporting)
    except Locked as e:
        return _refused(ws.accession, stages[0], "the accession is already being run", str(e),
                        "Wait for the other run to finish.")
    except OSError as e:          # the lock, state.json or the report: stages catch their own
        return _refused(ws.accession, stages[0], f"cannot write the run folder {ws.run_dir}",
                        f"{type(e).__name__}: {e}",
                        "Check that the folder is writable and the disk is not full, then "
                        "run the same command again -- it continues from where it stopped.")


def _drive(ws: Workspace, source: str, command: str, stages: tuple,
           from_stage: Optional[str], say: Callable[[str], None], reporting) -> Dict[str, Any]:
    with runlog.accession_log(ws.accession, actor="cli",
                              arguments={"command": command, "source": source,
                                         "from": from_stage}) as logctx:
        state = load_state(ws)
        if command == "run" and state.get("input") and state["input"] != source:
            runlog.event(log, logging.WARNING,
                         f"input changed from {state['input']!r} to {source!r}: starting "
                         f"again from resolve", kind="input_changed")
            from_stage = from_stage or "resolve"
        state.update({"accession": ws.accession, "input": source, "last_command": command})
        state.setdefault("created_at", _now())
        state.setdefault("stages", {})
        for s in state["stages"].values():
            if s.get("status") == "running":
                s["status"] = "pending"
        forced = set(stages[stages.index(from_stage):]) if from_stage else set()
        head, code = runlog._git_head(), _code_hash(OUTPUT_CODE)
        if state.get("code") and state["code"] != code:
            runlog.event(log, logging.WARNING,
                         f"the code changed since the last run ({state['code']} -> {code}): "
                         f"every stage that depends on it runs again", kind="code_changed")
        state["git"], state["code"] = head, code
        save_state(ws, state)

        run = Run(ws, source, state, say)
        run.forced, run.command = forced, command
        total = len(stages) + 1
        exit_code, interrupted, stopped = EXIT_OK, False, False
        if command == "confirm":
            problem = _not_ready(run)
            if problem:
                state["stages"]["check"] = {"status": "blocked", "summary": problem["what"],
                                            "outputs": {}, "problems": [problem],
                                            "fingerprint": None, "left": None,
                                            "started_at": _now(), "finished_at": _now(),
                                            "git": head}
                save_state(ws, state)
                say(f"[1/{total}] check    ⏸ {problem['what']}")
                runlog.event(log, logging.WARNING, f"confirm refused: {problem['what']}",
                             kind="stage_end", stage="check", status="blocked",
                             problems=[problem])
                exit_code, stopped = EXIT_DECISIONS, True
        for i, name in enumerate(() if stopped else stages, start=1):
            prior = state["stages"].get(name) or {}
            try:
                fp = FINGERPRINTS[name](run)
            except Exception:                           # noqa: BLE001 - inputs vanished
                fp = None
            done = prior.get("status") == "done"
            intact = not done or prior.get("left") == _left_behind(run, name)
            if done and intact and prior.get("fingerprint") == fp and name not in forced:
                say(f"[{i}/{total}] {name:<8} ✓ {prior.get('summary', '')} (unchanged)")
                runlog.event(log, logging.INFO, f"stage {name}: up to date, reused -- "
                                                f"{prior.get('summary', '')}",
                             kind="stage_reused", stage=name)
                continue
            if not intact:
                runlog.event(log, logging.WARNING, f"stage {name}: its output changed or went "
                                                   f"missing since it finished; rebuilding it",
                             kind="output_changed", stage=name)
            changed = name in forced or prior.get("fingerprint") != fp or not intact
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
                interrupted, stopped, exit_code = True, True, EXIT_INTERRUPTED
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
                                     "left": (_left_behind(run, name)
                                              if outcome["status"] == "done" else None),
                                     "started_at": state["stages"][name]["started_at"],
                                     "finished_at": _now(), "git": head}
            save_state(ws, state)
            mark = {"done": "✓", "failed": "✗", "blocked": "⏸"}.get(outcome["status"], "?")
            say(f"[{i}/{total}] {name:<8} {mark} {outcome['summary']}")
            runlog.event(log, logging.WARNING if outcome["status"] != "done" else logging.INFO,
                         f"stage {name}: {outcome['status']} -- {outcome['summary']}",
                         kind="stage_end", stage=name, status=outcome["status"],
                         problems=outcome["problems"])
            # Also write each problem to the text log, not only events.jsonl.
            for p in outcome["problems"]:
                log.log(logging.WARNING if p.get("blocking") else logging.INFO,
                        "  %s: %s%s -- fix: %s", "problem" if p.get("blocking") else "note",
                        p["what"], f" ({p['why']})" if p.get("why") else "", p["how_to_fix"])
            if outcome["status"] == "failed":
                exit_code, stopped = EXIT_FAILED, True
                break
            if outcome["status"] == "blocked":
                exit_code, stopped = EXIT_DECISIONS, True
                break
        if not stopped and command == "confirm":
            # Unwritten files fail the command even though the rest were written.
            if any(p.get("blocking") and p.get("kind") == "failure"
                   for p in (state["stages"].get("process") or {}).get("problems") or []):
                exit_code = EXIT_FAILED
            elif run.out("verify").get("output_verified") is False:
                exit_code = EXIT_UNVERIFIED

        state[f"last_{command}_exit_code"] = state["last_exit_code"] = exit_code
        state["last_invocation"] = run.invocation
        save_state(ws, state)
        report = reporting.write_report(ws, state, exit_code, interrupted, command, stages)
        say(f"[{total}/{total}] report   ✓ {ws.report_md}")
        logctx["status"] = f"exit {exit_code}"
        return {"accession": ws.accession, "command": command, "exit_code": exit_code,
                "stages": state["stages"], "problems": report["problems"],
                "paths": report["paths"]}


def status(accession: str) -> Dict[str, Any]:
    """The last recorded state of an accession's run, read-only."""
    ws = Workspace(accession)
    state = load_state(ws)
    from wod_etl.orchestration.report import current_problems

    last = RUN_STAGES if state.get("last_command") != "confirm" else CONFIRM_STAGES
    problems = current_problems(state["stages"], last)
    return {"accession": accession, "input": state.get("input"),
            "last_command": state.get("last_command"),
            "last_exit_code": state.get("last_exit_code"),
            "stages": {s: {k: (state["stages"].get(s) or {}).get(k)
                           for k in ("status", "summary", "finished_at")}
                       for s in STAGES if s in state["stages"]},
            "problems": problems,
            "paths": {"run_dir": str(ws.run_dir), "log": str(ws.log_path),
                      "decisions": str(ws.decisions_path), "report": str(ws.report_md)},
            "exists": ws.state_path.exists()}
