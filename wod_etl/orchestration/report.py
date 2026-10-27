"""Run summary: `data/runs/<accession>/report.md` and `report.json`.

Written after every `wod-etl run` and `wod-etl confirm`: what the accession holds, what was
included and why, what was written and verified, and what needs doing next.
"""
import re
from typing import Any, Dict, List, Optional

from wod_etl.io_util import write_json_atomic, write_text_atomic


def _status_words(command: str, exit_code: int, state: Dict[str, Any]) -> str:
    stages = state["stages"]
    out = lambda s: (stages.get(s) or {}).get("outputs") or {}
    if exit_code == 130:
        return "interrupted"
    if command == "run":
        return ("decisions written: review them, then confirm" if exit_code == 0 else "FAILED")
    if exit_code == 0:
        return ("finished: output written and verified" if out("verify").get("output_verified")
                else "finished: nothing to write")
    if exit_code == 2:
        return "waiting for decisions"
    if exit_code == 3:
        return "finished, but the output is NOT verified"
    process = stages.get("process") or {}
    if process.get("status") == "done":
        failed = sum(1 for p in process.get("problems") or [] if p.get("file"))
        return f"finished, but {failed} file(s) could not be written"
    return "FAILED"


def current_problems(recorded: Dict[str, Any], stages) -> List[Dict[str, Any]]:
    """Problems of the stages up to the one the command stopped at; later ones are stale."""
    out: List[Dict[str, Any]] = []
    for s in stages:
        st = recorded.get(s) or {}
        out += st.get("problems") or []
        if st.get("status") != "done":
            break
    return out


def write_report(ws, state: Dict[str, Any], exit_code: int, interrupted: bool,
                 command: str = "run", stages: Optional[tuple] = None) -> Dict[str, Any]:
    from wod_etl.orchestration.pipeline import CONFIRM_STAGES, RUN_STAGES

    stages = stages or (CONFIRM_STAGES if command == "confirm" else RUN_STAGES)
    recorded = state["stages"]
    out = lambda s: (recorded.get(s) or {}).get("outputs") or {}
    problems = current_problems(recorded, stages)
    plan = (out("check").get("plan") if command == "confirm" else None) \
        or out("decide").get("plan") or {}
    paths = {"run_dir": str(ws.run_dir), "decisions": str(ws.decisions_path),
             "report": str(ws.report_md), "log": str(ws.log_path),
             "events": str(ws.run_dir / "events.jsonl"), "state": str(ws.state_path),
             "triage": str(ws.triage_path),
             "output": str(ws.processed_dir)}
    report = {"accession": ws.accession, "input": state.get("input"), "command": command,
              "exit_code": exit_code, "status": _status_words(command, exit_code, state),
              "invocation": state.get("last_invocation"), "git": state.get("git"),
              "code": state.get("code"), "updated_at": state.get("updated_at"),
              "stages": {s: {k: (recorded.get(s) or {}).get(k)
                             for k in ("status", "summary", "finished_at")}
                         for s in stages if s in recorded},
              "problems": problems,
              "resolution": {k: out("resolve").get(k) for k in ("mode", "source", "resolution",
                                                                "version")},
              "triage": out("triage").get("counts"),
              "decisions": {"entries": plan.get("entries") or [],
                            "direct": len(plan.get("direct") or []),
                            "recipes": sorted((plan.get("profiles") or {}).keys())
                                       + sorted(f"taxa.{n}" for n in plan.get("taxa") or {}),
                            "left_out": len(plan.get("left_out") or []),
                            "dataset_overrides": plan.get("dataset_overrides") or {},
                            "metadata": plan.get("metadata") or {},
                            "file_options": plan.get("file_options") or {},
                            "station_overrides": plan.get("station_overrides") or {},
                            "moorings": {name: {"deployed": r.get("deployed"),
                                                "depths": r.get("depths") or {},
                                                "placed": r.get("placed") or {}}
                                         for name, r in (plan.get("profiles") or {}).items()
                                         if r.get("kind") == "mooring"}},
              "stage": out("stage").get("recipes") if command == "confirm" else None,
              "process": out("process").get("runs") if command == "confirm" else None,
              "files": out("process").get("files") if command == "confirm" else None,
              "taxa_files": (out("process").get("taxa_files") if command == "confirm"
                             else None),
              "taxa": out("process").get("taxa") if command == "confirm" else None,
              "files_written_at": (recorded.get("process") or {}).get("finished_at"),
              "files_removed": (out("process").get("removed") if command == "confirm"
                                else None),
              "verify": ({k: out("verify").get(k) for k in
                          ("output_verified", "netcdf_files", "casts", "finding_counts",
                           "findings", "variables", "profiles_verified", "taxa_verified",
                           "taxa_checks")}
                         if command == "confirm" else None),
              "wod_qc": _wod_qc(),
              "paths": paths}
    write_json_atomic(ws.report_json, report)
    write_text_atomic(ws.report_md, render(report))
    return report


def _wod_qc() -> bool:
    from wod_etl import config

    return bool(config.RUN_WOD_QC)


def _items(value: Any, limit: int = 12) -> List[str]:
    """A dict or list as indented bullet lines, largest count first, capped at `limit`."""
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


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render(report: Dict[str, Any]) -> str:
    cmd = report["command"]
    lines = [f"# Accession {report['accession']} — {report['status']}", "",
             f"Input: `{report['input']}`  ",
             f"Last command: `wod-etl {cmd}`, {report.get('updated_at')} (invocation "
             f"{report.get('invocation')}, git {report.get('git')}, code {report.get('code')}), "
             f"exit code {report['exit_code']}", ""]
    res = report.get("resolution") or {}
    if res.get("source"):
        lines += [f"Data from: {res['source']}"
                  + (f" (version {res['version']['chosen']} of {res['version']['available']})"
                     if res.get("version") else ""), ""]

    lines += ["## Stages", "", "| Stage | Status | Summary |", "| --- | --- | --- |"]
    for s, v in report["stages"].items():
        lines.append(f"| {s} | {v.get('status')} | {_cell(v.get('summary'))} |")
    lines.append("")

    blocking = [p for p in report["problems"] if p.get("blocking")]
    other = [p for p in report["problems"] if not p.get("blocking")]
    if blocking:
        lines += ["## Before confirming" if cmd == "run" else "## What needs doing", ""]
        for n, p in enumerate(blocking, 1):
            lines += [f"{n}. **{p['what']}** ({p['stage']})",
                      f"   - Why: {p['why']}" if p.get("why") else None,
                      f"   - How to fix: {p['how_to_fix']}", ""]
    if other:
        lines += ["## Notes and warnings (they do not stop anything)", ""]
        for p in other:
            lines.append(f"- {p['what']}; {p['how_to_fix']}")
        lines.append("")

    lines += _render_decisions(report)
    if cmd == "confirm":
        lines += _render_output(report)
    lines += ["## Files", ""] + [f"- {k}: `{p}`" for k, p in report["paths"].items()]
    return "\n".join(l for l in lines if l is not None) + "\n"


def _render_decisions(report: Dict[str, Any]) -> List[str]:
    d = report.get("decisions") or {}
    entries = d.get("entries") or []
    if not entries:
        return []
    files = lambda es: sum(e["files"] for e in es)
    data = [e for e in entries if not e.get("documentation")]
    docs = [e for e in entries if e.get("documentation")]
    included = [e for e in data if e.get("include")]
    lines = ["## Decisions (decisions.toml)", "",
             f"{files(included)} file(s) included, {files(data) - files(included)} left out, "
             f"{files(docs)} documentation"
             + (f"; {len(d['recipes'])} reshaping recipe(s): {', '.join(d['recipes'])}"
                if d.get("recipes") else "") + ".", ""]
    if data:
        lines += ["| Files | Holds | Include | Why |", "| --- | --- | --- | --- |"]
        for e in data:
            lines.append(f"| `{_cell(e['key'])}`" + (f" ({e['files']})" if e["files"] > 1 else "")
                         + f" | {_cell(e.get('data'))} | "
                         + (f"yes, as {e.get('dataset') or '?'}" if e.get("include") else "no")
                         + f" | {_cell(e.get('why'))} |")
        lines.append("")
    if docs:
        lines.append("Documentation, read and not processed: "
                     + "; ".join(f"`{e['key']}`" + (f" ({e['files']})" if e["files"] > 1 else "")
                                 + f" {e.get('data')}" for e in docs) + ".")
        lines.append("")
    if d.get("metadata"):
        lines.append("Header values from the review, for every cast: "
                     + ", ".join(f"{k} = {v}" for k, v in d["metadata"].items()) + ".")
    for path, o in (d.get("file_options") or {}).items():
        own = {k: v for k, v in (o.get("metadata") or {}).items()
               if (d.get("metadata") or {}).get(k) != v}
        if own or o.get("drop"):
            lines.append(f"- `{path}`: "
                         + ", ".join([f"{k} = {v}" for k, v in own.items()]
                                     + [f"drop {', '.join(o['drop'])}"] * bool(o.get("drop"))))
    for station, o in (d.get("station_overrides") or {}).items():
        for note in o.get("notes") or []:
            lines.append(f"- Station {station}: {note}")
    if d.get("metadata") or d.get("file_options") or d.get("station_overrides"):
        lines.append("")
    for name, m in (d.get("moorings") or {}).items():
        deployed = m.get("deployed")
        lines += [f"### Mooring {name}: where wod-etl places its instruments", "",
                  ("Deployed " + " to ".join(deployed) + ", by the pressure its instruments "
                   "record." if isinstance(deployed, list) else
                   "Deployed: the whole record (no instrument records pressure)."), "",
                  "| Instrument | Depth | How |", "| --- | --- | --- |"]
        rows = sorted(m["depths"].items(), key=lambda kv: (kv[1] != "pressure", kv[1]
                                                            if kv[1] != "pressure" else 0,
                                                            kv[0]))
        for file, depth in rows:
            how = re.sub(r"^\d+(?:\.\d+)? m: ", "", m["placed"].get(file) or "")
            lines.append(f"| `{_cell(file)}` | "
                         + ("day by day" if depth == "pressure" else f"{depth:.1f} m")
                         + f" | {_cell(how)} |")
        lines.append("")
    return lines


def _render_wod_files(report: Dict[str, Any]) -> List[str]:
    """The files written: one per WOD dataset and year, and the source files in each."""
    files = report.get("files")
    if files is None:
        return []
    folder = (report.get("paths") or {}).get("output")
    lines = ["## WOD files written", "",
             f"In `{folder}`, written {report.get('files_written_at')}: one file per WOD "
             f"dataset and year, named as WOD names its own (`wod_<dataset>_<year>`) with the "
             f"accession. Each cast says which source file it came from.", ""]
    if not files:
        # Taxa-Record files are listed in their own section.
        lines = lines[:2] + (["No profile files.", ""] if report.get("taxa_files")
                             else ["None.", ""])
    else:
        lines += ["| File | Dataset | Year | Casts | Levels | From |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for f in files:
            sources = f.get("sources") or []
            shown = ", ".join(f"`{_cell(s['key'])}` ({s['casts']})" for s in sources[:6])
            if len(sources) > 6:
                shown += f" and {len(sources) - 6} more"
            lines.append(f"| `{f['name']}` | {f['dataset']} | {f['year'] or 'no date'} | "
                         f"{f['casts']} | {f['observations']} | {shown} |")
        lines.append("")
    if report.get("files_removed"):
        lines += ["Removed, as this confirm no longer writes them: "
                  + ", ".join(f"`{n}`" for n in report["files_removed"]) + ".", ""]
    return lines


def _render_taxa_files(report: Dict[str, Any]) -> List[str]:
    """The Taxa-Record files written, and what was not resolved or stated."""
    files = report.get("taxa_files")
    if not files:
        return []
    lines = ["## Taxa-Record files written", "",
             "Plankton as WOD Taxa-Records (Tables 6a/7a), one file per year.", "",
             "| File | Year | Hauls | Records | Resolved | Higher rank | By name only | From |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in files:
        lines.append(f"| `{f['name']}` | {f['year'] or 'no date'} | {f['hauls']} | "
                     f"{f['records']} | {f['resolved']} | {f['higher_rank']} | {f['by_name']} "
                     f"| {', '.join(f'`{_cell(s)}`' for s in f['sources'][:4])} |")
    lines.append("")
    taxa = report.get("taxa") or {}
    by_name = ("Kept by name, taxa list unavailable (records)"
               if taxa.get("taxa_list_unavailable") else
               "Not in WOD's taxa list, kept by name (records)")
    for field, label in (("unresolved_taxa", by_name),
                         ("taxa_resolved_at_higher_rank", "Resolved one rank up"),
                         ("missing_biological_fields",
                          "Biological Header fields not stated (hauls)"),
                         ("unmapped_columns", "Columns that are not Taxa-Record fields")):
        if taxa.get(field):
            lines.append(f"- {label}:")
            lines += _items(taxa[field])
    return lines + [""]


def _render_output(report: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for r in report.get("stage") or []:
        what = "haul" if r.get("kind") == "taxa" else "cast"
        lines.append(f"- Recipe {r.get('recipe')}: {r.get('files')} {what} file(s) staged"
                     + (f", {len(r['skipped'])} skipped" if r.get("skipped") else ""))
    if lines:
        lines = ["## Staged", ""] + lines + [""]
    lines += _render_wod_files(report)
    lines += _render_taxa_files(report)
    for which, r in (report.get("process") or {}).items():
        counts = r.get("counts") or {}
        lines += [f"## Source files processed: {which}", "",
                  f"- {counts.get('done', 0)} file(s) processed, {counts.get('failed', 0)} "
                  f"failed, {counts.get('skipped', 0)} skipped (status {r.get('status')})",
                  f"- Casts by WOD dataset: {r.get('dataset_types') or {}}"]
        if r.get("casts_depth_from_pressure"):
            lines.append(f"- Depth computed from pressure (UNESCO 1983, at each cast's "
                         f"latitude; pressure kept as variable 25): "
                         f"{r['casts_depth_from_pressure']} cast(s)")
        for field, label in (("skipped_reasons", "Files skipped"),
                             ("casts_not_written", "Casts not written (casts)"),
                             ("unstorable_columns_dropped",
                              "Columns not written: no WOD code, or not stored in the "
                              "dataset (casts)"),
                             ("outputs_without_time", "No date"),
                             ("coded_headers_not_resolved", "Coded metadata kept as text")):
            if r.get(field):
                lines.append(f"- {label}:")
                lines += _items(r[field])
            if field == "casts_not_written":
                for path, casts in (r.get("casts_not_written_in") or {}).items():
                    lines.append(f"  - in `{path}` (source profiles, counted from 0): "
                                 + "; ".join(casts[:15]) + (" ..." if len(casts) > 15 else ""))
        if r.get("levels_without_data"):
            lines.append(f"- Levels with nothing measured (padding, blanked values), not "
                         f"written: {r['levels_without_data']}")
        if r.get("outputs_without_time_of_day"):
            lines.append(f"- Date but no time of day: {len(r['outputs_without_time_of_day'])} "
                         f"file(s) (written date-only; GMT_time is fill)")
        for f in r.get("failures") or []:
            lines.append(f"- FAILED `{f['key']}` at {f['stage']}: {f['error']}")
        lines.append("")
    v = report.get("verify") or {}
    if v.get("taxa_verified") is not None:
        lines += ["## Verification of the Taxa-Record files", "",
                  "- " + ("verified" if v["taxa_verified"] else "NOT verified")
                  + ": every record has a depth range, a measurement and its unit."]
        for name, c in (v.get("taxa_checks") or {}).items():
            for e in c.get("errors") or []:
                lines.append(f"- `{name}`: {e}")
        lines.append("")
    if v.get("netcdf_files") is not None:
        lines += ["## Verification", "",
                  f"- {v.get('casts')} cast(s) in {v.get('netcdf_files')} file(s): "
                  + ("verified" if v.get("profiles_verified", v["output_verified"])
                     else "NOT verified")
                  + f", findings {v.get('finding_counts')}. Verification checks that the "
                    f"values are possible (Appendix 11 ranges, fill, copies, depth axis).",
                  "- WOD quality control (Table 12 flags): "
                  + ("run here." if report.get("wod_qc") else
                     "not run here, so no WOD flags are set; WOD applies its own QC "
                     "downstream (set WOD_RUN_QC=1 to run it here). The originator's own "
                     "flags, where the source has them, are carried verbatim."), ""]
        if v.get("variables"):
            lines += ["| Variable | WOD code | Units | Values | Range-checked | Min | Max |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
            for name, s in v["variables"].items():
                checked = s.get("levels_range_checked")
                lines.append(f"| {name} | {s.get('wod_code')} | {_cell(s.get('units'))} | "
                             f"{s.get('n_finite')} | "
                             + ("all" if checked == s.get("n_finite") else
                                f"{checked} (not checked: {s.get('values_not_range_checked')})")
                             + f" | {s.get('min')} | {s.get('max')} |")
            lines.append("")
        for f in v.get("findings") or []:
            lines.append(f"- [{f['severity']}] {f.get('variable') or f.get('file')}: "
                         f"{f['evidence']}; {f['action']}")
        if v.get("findings"):
            lines.append("")
    return lines
