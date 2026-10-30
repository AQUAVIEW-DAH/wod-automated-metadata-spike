"""`wod-etl web`: run and confirm accessions from a local browser page.

Each command runs as a child `wod-etl` process, so a crash cannot take down the server.
Commands run one at a time; the rest wait in a queue, in order. Runs share
`data/runs/<accession>/` with the terminal. There is no login.
"""
import datetime as dt
import mimetypes
import multiprocessing
import os
import re
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from wod_etl import config
from wod_etl.io_util import write_text_atomic
from wod_etl.orchestration import decisions as dec
from wod_etl.orchestration import pipeline
from wod_etl.orchestration import run_manifest
from wod_etl.orchestration.pipeline import (CONFIRM_STAGES, RUN_STAGES, STAGES, Workspace,
                                            load_state)
from wod_etl.web import inputs, preview
from wod_etl.writers import is_output_file_name, is_taxa_file_name

UI_DIR = Path(__file__).with_name("static")
# An accession names folders: nothing that could climb out of them.
_ACCESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

app = FastAPI(title="wod-etl", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

_job_lock = threading.Lock()
_job: Dict[str, Any] = {}          # the command running now, or the last one
_jobs: Dict[str, Dict[str, Any]] = {}   # each accession's last command
_queue: List[Dict[str, Any]] = []       # commands waiting their turn, oldest first

_reader_lock = threading.Lock()
_reader: Optional[ProcessPoolExecutor] = None   # reads output files for the preview


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _workspace(accession: str) -> Workspace:
    if not _ACCESSION.fullmatch(accession or ""):
        raise HTTPException(400, f"not an accession id: {accession!r}")
    return Workspace(accession)


def _busy() -> Optional[Dict[str, Any]]:
    return _job if _job.get("running") else None


def _running(accession: str) -> bool:
    return bool(_job.get("running")) and _job.get("accession") == accession


def _waiting(accession: str) -> Optional[Dict[str, Any]]:
    """The accession's place in the queue (1 is next) and its command, or None."""
    for i, q in enumerate(_queue):
        if q["accession"] == accession:
            return {"position": i + 1, "command": q["command"]}
    return None


def _command_line(command: str, *args: str) -> List[str]:
    """The `wod-etl` command a job runs, as a process of its own."""
    return [sys.executable, "-m", "wod_etl", command, *args]


def _enqueue(accession: str, command: str, argv: List[str]) -> bool:
    """Queue `argv`, and start it if nothing is running. False, and nothing queued, when the
    accession already has a command running or waiting."""
    with _job_lock:
        if _running(accession) or _waiting(accession):
            return False
        _queue.append({"accession": accession, "command": command, "argv": argv})
        if not _busy():
            _start_next()
    return True


def _start_next() -> None:
    """Start the oldest queued command as a child process; its output lines become the job's
    activity log. Called with `_job_lock` held."""
    global _job
    while _queue:
        item = _queue.pop(0)
        job = {"accession": item["accession"], "command": item["command"], "running": True,
               "lines": [], "started_at": _now(), "finished_at": None, "exit_code": None,
               "error": None, "pid": None}
        _job = _jobs[item["accession"]] = job
        env = {**os.environ, **config.child_environment(),
               "WOD_MANIFEST_DIR": str(run_manifest.MANIFEST_DIR), "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.Popen(item["argv"], cwd=str(config.ROOT), env=env, text=True,
                                    encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=1)
        except OSError as e:
            job.update(running=False, finished_at=_now(), exit_code=pipeline.EXIT_FAILED,
                       error=f"{type(e).__name__}: {e}")
            continue
        job["pid"] = proc.pid
        threading.Thread(target=_collect, args=(proc, job), daemon=True,
                         name=f"wod-etl {item['command']} {item['accession']}").start()
        return


def _collect(proc: subprocess.Popen, job: Dict[str, Any]) -> None:
    try:
        for line in proc.stdout:
            job["lines"].append(line.rstrip("\n"))
        job["exit_code"] = proc.wait()
    except Exception as e:                           # noqa: BLE001 - shown on the page
        job["error"] = f"{type(e).__name__}: {e}"
        job["exit_code"] = pipeline.EXIT_FAILED
    finally:
        with _job_lock:
            job["finished_at"] = _now()
            job["running"] = False
            _start_next()


# ------------------------------------------------------------------ the page
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI_DIR / "index.html")


# ------------------------------------------------------------------ commands
@app.post("/api/run")
def start_run(source: str = Body(..., embed=True),
              accession_id: Optional[str] = Body(None, embed=True)):
    """`wod-etl run <source>` for each source, separated by commas: resolve, fetch, triage
    and propose decisions. Nothing is queued unless every source names an accession."""
    from wod_etl.discovery import accession_for

    sources = [s.strip() for s in re.split(r"[,\n]", source) if s.strip()]
    if not sources:
        raise HTTPException(400, "give an accession: a landing-page URL, a number, an NCEI "
                                 "data directory or a local folder; several, separated by "
                                 "commas")
    accession_id = (accession_id or "").strip()
    if accession_id and len(sources) > 1:
        raise HTTPException(400, "an accession id names one input, not several")
    named: Dict[str, str] = {}
    unknown = []
    for s in sources:
        acc = accession_id or accession_for(s)
        if not acc:
            unknown.append(s)
        else:
            _workspace(acc)
            named.setdefault(acc, s)
    if unknown:
        raise HTTPException(400, f"cannot tell which accession {', '.join(map(repr, unknown))} "
                                 f"is: give its number as the accession id")
    queued = [acc for acc, s in named.items()
              if _enqueue(acc, "run", _command_line("run", s, "--accession-id", acc))]
    if not queued:
        raise HTTPException(409, f"{', '.join(named)}: a command is already running or "
                                 f"waiting")
    return {"accessions": list(named), "queued": queued,
            "skipped": [acc for acc in named if acc not in queued]}


@app.post("/api/confirm/{accession}")
def start_confirm(accession: str):
    """`wod-etl confirm <accession>`: write what the reviewed decisions include."""
    ws = _workspace(accession)
    if not ws.state_path.exists():
        raise HTTPException(404, f"no run of {accession} yet: Run it first")
    if not _enqueue(accession, "confirm", _command_line("confirm", accession)):
        raise HTTPException(409, f"{accession}: a command is already running or waiting")
    return {"accession": accession, "command": "confirm"}


@app.delete("/api/queue/{accession}")
def unqueue(accession: str):
    """Take a waiting command out of the queue; one already running carries on."""
    _workspace(accession)
    with _job_lock:
        waiting = _waiting(accession)
        if not waiting:
            raise HTTPException(404, f"nothing waiting for {accession}")
        del _queue[waiting["position"] - 1]
    return {"accession": accession, "removed": waiting["command"]}


# ------------------------------------------------------------------ what is there
@app.get("/api/accessions")
def accessions():
    """The accessions run here before, newest first."""
    out = []
    root = Path(config.RUNS_DIR)
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if (d / "state.json").is_file() and _ACCESSION.fullmatch(d.name):
            state = load_state(Workspace(d.name))
            out.append({"accession": d.name, "input": state.get("input"),
                        "last_command": state.get("last_command"),
                        "last_exit_code": state.get("last_exit_code"),
                        "updated_at": state.get("updated_at")})
    return sorted(out, key=lambda a: a.get("updated_at") or "", reverse=True)


@app.get("/api/status/{accession}")
def status(accession: str):
    """Every stage's status, what needs doing, and the command running now."""
    from wod_etl.orchestration.report import _status_words

    ws = _workspace(accession)
    s = pipeline.status(accession)
    state = load_state(ws)
    code, command = s.get("last_exit_code"), s.get("last_command")
    job = _jobs.get(accession)
    return {
        "accession": accession, "exists": s["exists"], "input": s.get("input"),
        "last_command": command, "last_exit_code": code,
        "outcome": (_status_words(command, code, state)
                    if command and code is not None else None),
        "stages": [{"name": n, "command": "run" if n in RUN_STAGES else
                    "confirm" if n in CONFIRM_STAGES else "both",
                    **(s["stages"].get(n) or _report_stage(ws, n, job))} for n in STAGES],
        "problems": s["problems"],
        "job": {k: v for k, v in job.items()} if job else None,
        "queued": _waiting(accession),
        "busy": ({"accession": _job["accession"], "command": _job["command"]}
                 if _busy() else None)}


@app.get("/api/batch")
def batch(ids: str = Query(..., description="accession ids, separated by commas")):
    """A line for each accession of a batch: what it is doing, or where it stopped."""
    from wod_etl.orchestration.report import _status_words

    out = []
    for acc in dict.fromkeys(i.strip() for i in ids.split(",") if i.strip()):
        ws = _workspace(acc)
        s, state = pipeline.status(acc), load_state(ws)
        command, code = s.get("last_command"), s.get("last_exit_code")
        verify = (state["stages"].get("verify") or {}).get("outputs") or {}
        job, waiting = _jobs.get(acc) or {}, _waiting(acc)
        blocking = sum(1 for p in s["problems"] if p.get("blocking"))
        if _running(acc):
            step = "running"
        elif waiting:
            step = "queued"
        elif job.get("error"):
            step = "failed"
        elif not command or code is None:
            step = "new"
        elif (command, code) in (("run", 0), ("confirm", pipeline.EXIT_DECISIONS)):
            step = "review"
        elif code == 0:
            step = "done" if verify.get("output_verified") else "empty"
        else:
            step = "failed"
        out.append({"accession": acc, "step": step, "blocking": blocking,
                    "command": (job.get("command") if step == "running" else
                                waiting["command"] if waiting else command),
                    "position": waiting["position"] if waiting else None,
                    "outcome": job.get("error") or (
                        _status_words(command, code, state)
                        if command and code is not None else None)})
    return out


def _report_stage(ws: Workspace, name: str, job) -> Dict[str, Any]:
    """The report is written at the end of every command, not recorded as a stage."""
    if name == "report" and ws.report_md.is_file() and not (job or {}).get("running"):
        return {"status": "done", "summary": "report.md written"}
    return {"status": None}


@app.get("/api/files/{accession}")
def files(accession: str):
    """The accession's files grouped by decision section, each with its decision."""
    ws = _workspace(accession)
    state = load_state(ws)
    run = pipeline.Run(ws, state.get("input") or "", state, lambda _: None)
    triage = run.triage()
    decisions, error = dec.load(ws.decisions_path)
    root = Path(run.data_root or ws.mirror_dir)
    groups: Dict[str, list] = {}
    for f in triage.get("files", []):
        key = dec.decision_key(decisions, f["path"]) if not error else None
        entry = (decisions.get("files") or {}).get(key) if key else None
        sec = dec.section_of(f, entry)
        path = root / f["path"]
        mooring = f.get("mooring") or {}
        groups.setdefault(sec, []).append({
            "path": f["path"], "name": f["path"].rsplit("/", 1)[-1], "data": f.get("data"),
            "include": (entry or f).get("include"),
            "dataset": (entry or {}).get("dataset") or f.get("dataset"),
            "why": (entry or {}).get("why") or f.get("why"), "decided_by": key,
            # A mooring's logger: where wod-etl places it, and how.
            "depth": mooring.get("depth") if mooring.get("placed_by") else None,
            "depth_note": mooring.get("depth_note"),
            # A plankton table wod-etl can write as Taxa-Records (not one it proposes out).
            "taxa": bool(f.get("taxa")) and not f["taxa"].get("missing"),
            "size": path.stat().st_size if path.is_file() else None})
    headings = dict(dec.SECTIONS)
    return {"accession": accession, "root": str(root), "decisions_error": error,
            "sections": [{"section": s, "heading": headings[s], "files": groups[s]}
                         for s, _ in dec.SECTIONS if groups.get(s)]}


# ------------------------------------------------------------------ decisions.toml
@app.get("/api/decisions/{accession}")
def get_decisions(accession: str):
    p = _workspace(accession).decisions_path
    if not p.is_file():
        return {"exists": False, "text": "", "mtime": None}
    return {"exists": True, "text": p.read_text(encoding="utf-8"), "mtime": p.stat().st_mtime_ns}


@app.put("/api/decisions/{accession}")
def put_decisions(accession: str, text: str = Body(..., embed=True),
                  mtime: Optional[int] = Body(None, embed=True)):
    """Save reviewed decisions: valid TOML only, not over a newer file, not during a command."""
    ws = _workspace(accession)
    p = ws.decisions_path
    if _running(accession):
        raise HTTPException(409, f"a command is running for {accession}: save when it has "
                                 f"finished")
    if not p.is_file():
        raise HTTPException(404, f"no decisions.toml for {accession}: Run it first")
    if mtime is not None and p.stat().st_mtime_ns != mtime:
        raise HTTPException(409, "decisions.toml changed on disk since it was opened: reload "
                                 "it, then make the change again")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise HTTPException(422, f"not valid TOML, nothing saved: {e}")
    write_text_atomic(p, text)
    return {"saved": True, "mtime": p.stat().st_mtime_ns}


# ------------------------------------------------------------------ what was written
@app.get("/api/outputs/{accession}")
def outputs(accession: str):
    """The WOD files written, with what each holds, and the verification."""
    ws = _workspace(accession)
    state = load_state(ws)
    process = (state.get("stages", {}).get("process") or {}).get("outputs") or {}
    verify = (state.get("stages", {}).get("verify") or {}).get("outputs") or {}
    about = {f.get("name"): f for f in process.get("files") or []}
    taxa = {f.get("name"): f for f in process.get("taxa_files") or []}
    out = []
    for p in pipeline.wod_files(ws):
        if p.name in taxa or is_taxa_file_name(p.name, accession):
            f = taxa.get(p.name, {})
            out.append({"name": p.name, "size": p.stat().st_size, "kind": "taxa",
                        "dataset": "OSD", "year": f.get("year"), "hauls": f.get("hauls"),
                        "records": f.get("records"), "resolved": f.get("resolved"),
                        "higher_rank": f.get("higher_rank"), "by_name": f.get("by_name"),
                        "sources": f.get("sources") or []})
            continue
        f = about.get(p.name, {})
        out.append({"name": p.name, "size": p.stat().st_size, "kind": "profiles",
                    "dataset": f.get("dataset"), "year": f.get("year"), "casts": f.get("casts"),
                    "observations": f.get("observations"),
                    "sources": [s.get("key") for s in f.get("sources") or []]})
    notes = process.get("taxa") or {}
    return {"accession": accession, "folder": str(ws.processed_dir), "files": out,
            "taxa_not_stated": list(notes.get("missing_biological_fields") or {}) or None,
            "taxa_list_unavailable": bool(notes.get("taxa_list_unavailable")),
            "verified": verify.get("output_verified"),
            "finding_counts": verify.get("finding_counts"),
            "written_at": (state.get("stages", {}).get("process") or {}).get("finished_at"),
            "report": ws.report_md.is_file()}


def _output_path(accession: str, name: str) -> Path:
    path = _workspace(accession).processed_dir / name
    if not is_output_file_name(name, accession) or not path.is_file():
        raise HTTPException(404, f"no WOD file {name!r} for {accession}")
    return path


@app.get("/api/outputs/{accession}/{name}")
def download(accession: str, name: str):
    return FileResponse(_output_path(accession, name), media_type="application/x-netcdf",
                        filename=name)


@app.get("/api/preview/{accession}/{name}")
def preview_file(accession: str, name: str):
    """A written file's attributes, dimensions and variables, and its casts."""
    return _read(name, preview.describe, str(_output_path(accession, name)))


@app.get("/api/preview/{accession}/{name}/casts/{index}")
def preview_cast(accession: str, name: str, index: int):
    """One cast of a written file: its fields and its levels."""
    return _read(name, preview.cast, str(_output_path(accession, name)), index)


def _read(name: str, fn, *args):
    """Run a read in a worker process: HDF5 stays off the server's threads, and a crash in it
    costs a new worker, not the page."""
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"),
                                          initializer=preview.exit_with, initargs=(os.getpid(),))
        reader = _reader
    try:
        return reader.submit(fn, *args).result(timeout=120)
    except IndexError as e:
        raise HTTPException(404, f"{name}: {e}")
    except OSError as e:
        raise HTTPException(409, f"cannot read {name}: {e}")
    except BrokenProcessPool:
        with _reader_lock:
            if _reader is reader:
                _reader = None
        raise HTTPException(500, f"the reader stopped while reading {name}: try again")


# ------------------------------------------------------------------ input files
def _input_path(accession: str, path: str) -> Path:
    """A file the accession's triage lists, inside its data folder; 404 for anything else."""
    ws = _workspace(accession)
    state = load_state(ws)
    run = pipeline.Run(ws, state.get("input") or "", state, lambda _: None)
    listed = {f["path"] for f in run.triage().get("files", [])}
    root = Path(run.data_root or ws.mirror_dir).resolve()
    full = (root / path).resolve()
    if path not in listed or not full.is_relative_to(root) or not full.is_file():
        raise HTTPException(404, f"no input file {path!r} for {accession}")
    return full


@app.get("/api/input-preview/{accession}/{path:path}")
def input_preview(accession: str, path: str):
    """An input file's text, sheets or NetCDF variables."""
    full = _input_path(accession, path)
    out = {"path": path, "name": full.name, **inputs.preview_input(full)}
    if out["kind"] == "netcdf":
        out.update(_read(full.name, preview.describe_input, str(full)))
    return out


@app.get("/api/input-file/{accession}/{path:path}")
def input_file(accession: str, path: str):
    """An input file as it is, shown in the page (a PDF, an image) or downloaded."""
    full = _input_path(accession, path)
    return FileResponse(full, media_type=mimetypes.guess_type(full.name)[0]
                        or "application/octet-stream", filename=full.name,
                        content_disposition_type="inline")


@app.get("/api/report/{accession}", response_class=PlainTextResponse)
def report(accession: str):
    p = _workspace(accession).report_md
    if not p.is_file():
        raise HTTPException(404, "no report yet")
    return p.read_text(encoding="utf-8")


@app.get("/api/log/{accession}", response_class=PlainTextResponse)
def log(accession: str, lines: int = Query(300, ge=1, le=5000)):
    """The end of run.log, which details every stage, file and decision."""
    p = _workspace(accession).log_path
    if not p.is_file():
        return ""
    return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    print(f"wod-etl web: http://{host}:{port}  (Ctrl-C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
