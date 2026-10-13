"""Run ledger: what was processed, what failed, and what is left to do.

An unattended run over hundreds of files needs to survive a crash and be able to report
what it actually did. The manifest is a single JSON file per run, written atomically after
every update so a kill at any moment leaves a readable ledger.

Statuses
    pending  -- discovered, not yet attempted
    running  -- attempt in progress (a leftover 'running' entry means the run was killed)
    done     -- completed and validated
    failed   -- attempted and failed; `error` and `stage` say where
    skipped  -- deliberately not processed; `reason` says why
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from wod_etl.config import MANIFEST_DIR
from wod_etl.io_util import write_json_atomic

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"
TERMINAL = {DONE, SKIPPED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(run_id: str) -> Path:
    return MANIFEST_DIR / f"{run_id}.json"


def new_run_id(source: str, stamp: Optional[str] = None) -> str:
    """Build a stable, filesystem-safe run id from the source and a timestamp."""
    tail = source.rstrip("/").split("/")[-1] or "run"
    tail = "".join(c for c in tail if c.isalnum() or c in "._-")[:40]
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{tail}_{stamp}"


def create_manifest(run_id: str, source: str, items: List[Dict[str, Any]],
                    config_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Start a new manifest. Each item needs at least a unique `key`."""
    manifest = {
        "run_id": run_id,
        "source": source,
        "created_at": _now(),
        "updated_at": _now(),
        "config": config_snapshot or {},
        "items": {
            it["key"]: {**it, "status": PENDING, "attempts": 0, "history": []}
            for it in items
        },
    }
    save_manifest(manifest)
    return manifest


def load_manifest(run_id: str) -> Optional[Dict[str, Any]]:
    p = manifest_path(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def save_manifest(manifest: Dict[str, Any]) -> Path:
    """Write atomically so an interrupted run never leaves a half-written ledger."""
    manifest["updated_at"] = _now()
    return write_json_atomic(manifest_path(manifest["run_id"]), manifest)


def resume_or_create(run_id: str, source: str, items: List[Dict[str, Any]],
                     config_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load an existing manifest and fold in any newly discovered items."""
    existing = load_manifest(run_id)
    if existing is None:
        return create_manifest(run_id, source, items, config_snapshot)

    added = 0
    for it in items:
        if it["key"] not in existing["items"]:
            existing["items"][it["key"]] = {**it, "status": PENDING, "attempts": 0, "history": []}
            added += 1

    # A 'running' entry means the previous run was killed mid-item; make it retryable.
    for entry in existing["items"].values():
        if entry["status"] == RUNNING:
            entry["status"] = PENDING
            entry.setdefault("history", []).append(
                {"at": _now(), "status": PENDING, "note": "reset from 'running' after interrupted run"})

    if added:
        existing.setdefault("history", []).append({"at": _now(), "note": f"added {added} new item(s)"})
    save_manifest(existing)
    return existing


def pending_items(manifest: Dict[str, Any], retry_failed: bool = False) -> List[Dict[str, Any]]:
    """Items still to process, in discovery order."""
    out = []
    for entry in manifest["items"].values():
        if entry["status"] == PENDING or (retry_failed and entry["status"] == FAILED):
            out.append(entry)
    return out


def mark(manifest: Dict[str, Any], key: str, status: str, **fields) -> Dict[str, Any]:
    """Record a status transition for one item and persist immediately."""
    entry = manifest["items"].get(key)
    if entry is None:
        entry = {"key": key, "attempts": 0, "history": []}
        manifest["items"][key] = entry

    if status == RUNNING:
        entry["attempts"] = entry.get("attempts", 0) + 1

    entry["status"] = status
    entry.update(fields)
    entry.setdefault("history", []).append(
        {"at": _now(), "status": status, **{k: v for k, v in fields.items() if k != "history"}})
    save_manifest(manifest)
    return entry


def summarize(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Counts, failure rate, and the failures themselves -- what a run should report."""
    items = list(manifest["items"].values())
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1

    attempted = counts.get(DONE, 0) + counts.get(FAILED, 0)
    failures = [
        {"key": it["key"], "stage": it.get("stage"), "error": it.get("error")}
        for it in items if it["status"] == FAILED
    ]
    outputs = [it["output_path"] for it in items if it.get("output_path")]

    return {
        "run_id": manifest["run_id"],
        "source": manifest["source"],
        "total_items": len(items),
        "counts": counts,
        "failure_rate": (counts.get(FAILED, 0) / attempted) if attempted else 0.0,
        "complete": all(it["status"] in TERMINAL for it in items),
        "outputs": outputs,
        "failures": failures,
        "manifest_path": str(manifest_path(manifest["run_id"])),
    }


def list_runs() -> List[Dict[str, Any]]:
    """All known runs, newest first -- how an agent finds a run to resume."""
    out = []
    for p in MANIFEST_DIR.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        s = summarize(m)
        out.append({"run_id": m["run_id"], "source": m.get("source"),
                    "updated_at": m.get("updated_at"), "counts": s["counts"],
                    "complete": s["complete"]})
    return sorted(out, key=lambda r: r.get("updated_at") or "", reverse=True)


if __name__ == "__main__":
    run_id = new_run_id("https://example.org/archive/0287423/", stamp="demo")
    m = create_manifest(run_id, "https://example.org/archive/0287423/", [
        {"key": "a.csv", "url": "https://example.org/a.csv"},
        {"key": "b.csv", "url": "https://example.org/b.csv"},
        {"key": "c.xml", "url": "https://example.org/c.xml"},
    ])
    mark(m, "a.csv", RUNNING)
    mark(m, "a.csv", DONE, output_path="data/processed/a.nc", casts=1)
    mark(m, "b.csv", RUNNING)
    mark(m, "b.csv", FAILED, stage="load_tabular", error="parse failed: no columns")
    mark(m, "c.xml", SKIPPED, reason="metadata file, not measurements")
    print(json.dumps(summarize(m), indent=2))
    print("\npending after resume:", [i["key"] for i in pending_items(load_manifest(run_id))])
    print("retryable failures  :", [i["key"] for i in pending_items(load_manifest(run_id), retry_failed=True)])
