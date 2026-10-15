"""A log per accession: what was run against it, by whom, and what happened.

Every run touching an accession -- `wod-etl run` or `confirm` typed by a person, or a
coding agent calling `process_accession` -- appends to the same two files:

    data/runs/<accession>/run.log        human-readable, one line per event
    data/runs/<accession>/events.jsonl   the same events as JSON, one object per line

Each invocation opens with a banner (invocation id, actor, arguments, git commit, effective
configuration) and closes with how it ended, so a later reader can tell one run from the
next and see which code produced an output.

    from wod_etl.runlog import accession_log

    with accession_log("0303928", actor="agent"):
        ...  # any wod_etl call made here is logged to data/runs/0303928/

The library's entry points (`process_accession`, `process_taxa`, `classify_accession`, the
staged writers) open the log on their own when they can tell which accession they are
working on, so an agent gets a log without asking for one. When they cannot tell, nothing
is logged -- a run never fails because of its log.
"""
import contextvars
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from wod_etl import config

LOGGER_NAME = "wod_etl"
TEXT_LOG = "run.log"
EVENTS_LOG = "events.jsonl"
# Set for a script recipe's subprocess: log to stderr, which the parent run records.
TO_STDERR_ENV = "WOD_LOG_TO_STDERR"

# The log open in this context, if any. Re-entrant: a pipeline that opens the log and then
# calls process_accession must not attach a second pair of handlers and write every line
# twice.
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("wod_etl_accession_log", default=None)


def run_dir(accession: str) -> Path:
    """`data/runs/<accession>/`, read from config at call time so tests can redirect it."""
    return Path(config.RUNS_DIR) / str(accession)


def active() -> Optional[Dict[str, Any]]:
    """The log open in this context: `{"accession", "invocation", "actor", "dir"}`, or None."""
    return _ACTIVE.get()


def event(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """Log `message`, carrying `fields` into events.jsonl as structured data."""
    logger.log(level, message, extra={"wod_event": fields})


class _JsonLines(logging.Handler):
    def __init__(self, path: Path, context: Dict[str, Any]):
        super().__init__()
        self.path = path
        self.context = context

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "at": dt.datetime.fromtimestamp(record.created, dt.timezone.utc)
                        .isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "invocation": self.context["invocation"],
                "actor": self.context["actor"],
                "message": record.getMessage(),
            }
            fields = getattr(record, "wod_event", None)
            if fields:
                entry["fields"] = fields
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:                               # noqa: BLE001 - never break a run
            self.handleError(record)


def _git_head() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(config.ROOT),
                             capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=str(config.ROOT), capture_output=True, text=True,
                               timeout=5)
        head = out.stdout.strip()
        return f"{head}{'+dirty' if dirty.stdout.strip() else ''}" if head else None
    except Exception:                                   # noqa: BLE001 - not a git checkout
        return None


@contextmanager
def accession_log(accession: str, actor: str = "agent",
                  arguments: Optional[Dict[str, Any]] = None,
                  banner: bool = True) -> Iterator[Dict[str, Any]]:
    """Log everything `wod_etl` does inside the block to the accession's run log.

    `actor` says who ran it: `"cli"` for the `wod-etl` command, `"agent"` or `"library"` for
    a call from Python; `arguments["command"]` (`run`, `confirm`) names what was run. The
    yielded dict has `status`, which the caller may set; it is written in the closing line.
    Nested calls reuse the log already open.
    """
    current = _ACTIVE.get()
    if current is not None:
        yield current
        return

    context = {"accession": str(accession), "actor": actor, "dir": None,
               "invocation": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                             + "-" + uuid.uuid4().hex[:6],
               "status": None}
    logger = logging.getLogger(LOGGER_NAME)
    handlers = _handlers(accession, context)
    old_level = logger.level
    for h in handlers:
        logger.addHandler(h)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    token = _ACTIVE.set(context)
    log = logging.getLogger(f"{LOGGER_NAME}.runlog")
    # Banners belong to a run folder. There is none under a parent run (a banner there would
    # open a second run inside the parent's) or when the folder cannot be written.
    banner = banner and context["dir"] is not None
    if banner:
        event(log, logging.INFO,
              f"===== {actor} {(arguments or {}).get('command') or 'run'} "
              f"{context['invocation']} on accession {accession} =====",
              kind="start", actor=actor, arguments=arguments or {}, git=_git_head(),
              config=config.describe())
    try:
        yield context
    except BaseException as e:
        context["status"] = context.get("status") or f"raised {type(e).__name__}"
        event(log, logging.ERROR, f"run raised {type(e).__name__}: {e}", kind="exception",
              error=f"{type(e).__name__}: {e}")
        raise
    finally:
        if banner:
            event(log, logging.INFO,
                  f"===== end of run {context['invocation']}: "
                  f"{context.get('status') or 'finished'} =====",
                  kind="end", status=context.get("status"))
        _ACTIVE.reset(token)
        for h in handlers:
            logger.removeHandler(h)
            h.close()
        logger.setLevel(old_level)


def _handlers(accession: str, context: Dict[str, Any]) -> List[logging.Handler]:
    """run.log and events.jsonl in the accession's folder, or stderr under a parent run.

    `wod-etl confirm` sets TO_STDERR_ENV for a script recipe's subprocess and copies what it
    prints into its own run log, under its own invocation. The subprocess opening the run
    folder as well would log a second invocation, under an accession guessed from a path.
    """
    if os.environ.get(TO_STDERR_ENV):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        return [h]
    folder = run_dir(accession)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        text = logging.FileHandler(folder / TEXT_LOG, encoding="utf-8")
    except OSError as e:
        print(f"wod_etl: no run log for accession {accession}: {e}", file=sys.stderr)
        return []
    text.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                        "%Y-%m-%dT%H:%M:%S%z"))
    context["dir"] = str(folder)
    return [text, _JsonLines(folder / EVENTS_LOG, context)]


def accession_log_for(source: Any, accession: Optional[str] = None, actor: str = "library",
                      arguments: Optional[Dict[str, Any]] = None, banner: bool = True):
    """The log for whatever accession `source` belongs to -- or a no-op.

    Used by the library's entry points. Inside an open log it does nothing extra; with no
    accession to be found (a test folder, an ad-hoc file) it does nothing at all.
    """
    if _ACTIVE.get() is not None:
        return nullcontext(_ACTIVE.get())
    from wod_etl.discovery.list_remote_directory import accession_for

    # An explicit id wins as given -- a local survey need not have an NCEI number.
    acc = (accession_for(accession) or str(accession)) if accession else accession_for(source)
    if not acc:
        return nullcontext(None)
    return accession_log(acc, actor=actor, arguments=arguments, banner=banner)
