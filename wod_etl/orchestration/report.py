"""The run report: `data/runs/<accession>/report.md` and `report.json`, written by every run
however it ended.

It answers, for a person who was not watching: did it finish, what was written, what was
left out and why, what needs doing next. The run log has the blow-by-blow; this is the
summary to read first.
"""
from typing import Any, Dict, List

from wod_etl.io_util import write_json_atomic, write_text_atomic

STATUS_WORDS = {0: "finished, output verified", 1: "FAILED", 2: "PAUSED: decisions needed",
                3: "finished, but the output is NOT verified", 130: "interrupted"}


def _problems(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    from wod_etl.orchestration.pipeline import STAGES

    out = []
    for s in STAGES:
        stage = state["stages"].get(s) or {}
        for p in stage.get("problems") or []:
            out.append(p)
    return out


def write_report(ws, state: Dict[str, Any], exit_code: int, interrupted: bool) -> Dict[str, Any]:
    from wod_etl.orchestration.pipeline import STAGES

    problems = _problems(state)
    stages = state["stages"]
    out = lambda s: (stages.get(s) or {}).get("outputs") or {}
    paths = {"run_dir": str(ws.run_dir), "log": str(ws.log_path),
             "decisions": str(ws.decisions_path), "report": str(ws.report_md),
             "state": str(ws.state_path),
             "profiles": str(ws.processed_dir / "profiles"),
             "taxa": str(ws.processed_dir / "taxa")}
    report = {"accession": ws.accession, "input": state.get("input"),
              "exit_code": exit_code, "status": STATUS_WORDS.get(exit_code, str(exit_code)),
              "invocation": state.get("last_invocation"), "git": state.get("git"),
              "updated_at": state.get("updated_at"),
              "stages": {s: {k: (stages.get(s) or {}).get(k)
                             for k in ("status", "summary", "finished_at")}
                         for s in STAGES if s in stages},
              "problems": problems,
              "resolution": {k: out("resolve").get(k) for k in ("mode", "source", "resolution",
                                                                "version")},
              "process": out("process").get("runs"),
              "taxa": {k: v for k, v in out("taxa").items() if k != "outputs"} or None,
              "verify": {k: out("verify").get(k) for k in ("output_verified", "finding_counts",
                                                           "findings")},
              "decisions": _decisions_applied(out("decide")),
              "paths": paths}
    write_json_atomic(ws.report_json, report)
    write_text_atomic(ws.report_md, render(report))
    return report


def _decisions_applied(decide: Dict[str, Any]) -> Dict[str, Any]:
    plan = decide.get("plan") or {}
    return {"profile_recipes": sorted((plan.get("profiles") or {}).keys()),
            "taxa_recipes": sorted((plan.get("taxa") or {}).keys()),
            "direct_files": len(plan.get("direct") or []),
            "skipped_by_decision": plan.get("skipped"),
            "station_overrides": plan.get("station_overrides"),
            "pending": [p.get("what") for p in decide.get("pending") or []]}


def _items(value: Any, limit: int = 12) -> List[str]:
    """A dict or list as indented bullet lines, longest-first for counts, capped at `limit`."""
    if isinstance(value, dict):
        entries = list(value.items())
        if all(isinstance(v, int) for _, v in entries):
            entries.sort(key=lambda kv: -kv[1])
        lines = [f"  - {k}: {v}" for k, v in entries[:limit]]
        more = len(entries) - limit
    else:
        entries = list(value or [])
        lines = [f"  - {v}" for v in entries[:limit]]
        more = len(entries) - limit
    if more > 0:
        lines.append(f"  - ... and {more} more (all of them are in report.json)")
    return lines


def render(report: Dict[str, Any]) -> str:
    lines = [f"# Accession {report['accession']} -- {report['status']}", "",
             f"Input: `{report['input']}`  ",
             f"Last run: {report.get('updated_at')} (invocation {report.get('invocation')}, "
             f"code {report.get('git')}), exit code {report['exit_code']}", ""]
    res = report.get("resolution") or {}
    if res.get("source"):
        lines += [f"Data from: {res['source']}"
                  + (f" (version {res['version']['chosen']} of {res['version']['available']})"
                     if res.get("version") else ""), ""]

    lines += ["## Stages", "", "| Stage | Status | Summary |", "| --- | --- | --- |"]
    for s, v in report["stages"].items():
        lines.append(f"| {s} | {v.get('status')} | {v.get('summary') or ''} |")
    lines.append("")

    blocking = [p for p in report["problems"] if p.get("blocking")]
    other = [p for p in report["problems"] if not p.get("blocking")]
    if blocking:
        lines += ["## What needs doing", ""]
        for n, p in enumerate(blocking, 1):
            lines += [f"{n}. **{p['what']}** ({p['stage']})",
                      f"   - Why: {p['why']}" if p.get("why") else "",
                      f"   - How to fix: {p['how_to_fix']}", ""]
    if other:
        lines += ["## Pending and warnings (do not stop the run)", ""]
        for p in other:
            lines.append(f"- {p['what']} -- {p['how_to_fix']}")
        lines.append("")

    for which, r in (report.get("process") or {}).items():
        lines += [f"## Profiles: {which} files", "",
                  f"- Status: {r.get('status')}, counts {r.get('counts')}",
                  f"- Dataset types: {r.get('dataset_types')}"]
        for field, label in (("skipped_reasons", "Skipped"),
                             ("unstorable_columns_dropped", "Columns dropped (no WOD code)"),
                             ("outputs_without_time", "No date"),
                             ("coded_headers_not_resolved", "Coded metadata kept as text")):
            if r.get(field):
                lines.append(f"- {label}:")
                lines += _items(r[field])
        if r.get("outputs_without_time_of_day"):
            lines.append(f"- Date but no time of day: {len(r['outputs_without_time_of_day'])} "
                         f"file(s) (written date-only; GMT_time is fill)")
        for f in r.get("failures") or []:
            lines.append(f"- FAILED `{f['key']}` at {f['stage']}: {f['error']}")
        lines.append("")
    taxa = report.get("taxa")
    if taxa:
        lines += ["## Plankton (Taxa-Records)", "",
                  f"- {taxa.get('taxa_records', 0)} record(s) in "
                  f"{taxa.get('staged_files', 0)} haul file(s), status {taxa.get('status')}"]
        for field, label in (("unresolved_taxa", "Taxa with no WOD code (records; name kept)"),
                             ("missing_biological_fields",
                              "Collection metadata not stated (hauls)"),
                             ("unmapped_columns", "Columns not written")):
            if taxa.get(field):
                lines.append(f"- {label}:")
                lines += _items(taxa[field])
        if taxa.get("taxa_resolved_at_higher_rank"):
            names = list(taxa["taxa_resolved_at_higher_rank"])
            lines.append(f"- Resolved at a higher rank (species lost): {len(names)} name(s)")
            lines += _items(names)
        lines.append("")
    v = report.get("verify") or {}
    if v.get("output_verified") is not None:
        lines += ["## Verification", "",
                  f"- output_verified: {v['output_verified']}, findings {v.get('finding_counts')}"]
        for f in v.get("findings") or []:
            lines.append(f"- [{f['severity']}] {f.get('variable') or f.get('file')}: "
                         f"{f['evidence']} -- {f['action']}")
        lines.append("")
    d = report.get("decisions") or {}
    if any(d.values()):
        lines += ["## Decisions applied", "",
                  f"- Profile recipes: {d.get('profile_recipes')}; taxa recipes: "
                  f"{d.get('taxa_recipes')}; files processed as they stand: "
                  f"{d.get('direct_files')}"]
        if d.get("skipped_by_decision"):
            lines.append(f"- Skipped by decision: {[s['key'] for s in d['skipped_by_decision']]}")
        for station, o in (d.get("station_overrides") or {}).items():
            for note in o.get("notes") or []:
                lines.append(f"- Station {station}: {note}")
        lines.append("")
    lines += ["## Files", ""] + [f"- {k}: `{p}`" for k, p in report["paths"].items()]
    return "\n".join(l for l in lines if l is not None) + "\n"
