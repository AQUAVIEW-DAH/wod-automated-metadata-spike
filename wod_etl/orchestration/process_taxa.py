"""Staged plankton hauls to Taxa-Record NetCDF (Biological Header plus Taxa-Records).

`assemble_taxa` writes one file per year, `process_taxa` one file per haul. A failed haul is
listed in `failures` and the rest are still written.
"""
import glob
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl import runlog

from wod_etl.mapping.taxa import map_haul
from wod_etl.orchestration.process_accession import _output_stem
from wod_etl.staging.taxa_haul import is_staged_haul, read_staged_haul
from wod_etl.verify.validate_taxa_netcdf import validate_taxa_netcdf
from wod_etl.writers.generate_netcdf_ragged import cast_year
from wod_etl.writers.generate_netcdf_taxa import write_taxa_netcdf
from wod_etl.writers.wod_files import is_taxa_file_name, taxa_file_name

log = logging.getLogger(__name__)


class _Tally:
    """Report counts gathered from the written hauls."""

    def __init__(self):
        self.unresolved: Dict[str, int] = {}
        self.higher_rank: Dict[str, str] = {}
        self.stages: Dict[str, str] = {}
        self.missing: Dict[str, int] = {}
        self.unmapped: Dict[str, int] = {}
        self.unavailable = False
        self.records = 0

    def add(self, haul: Dict[str, Any], report: Dict[str, Any]) -> None:
        self.records += len(haul["records"])
        for name, t in haul["taxa"].items():
            if t["status"] == "matched_higher_rank":
                self.higher_rank[name] = t["note"]
            elif t["status"] != "matched":
                self.unavailable = self.unavailable or t["status"] in ("unavailable",
                                                                       "disabled")
                label = name or "(blank)"
                self.unresolved[label] = self.unresolved.get(label, 0) + sum(
                    1 for r in haul["records"] if r["taxon"] is t)
        for text, r in haul["life_stages"].items():
            self.stages[text] = r.get("summary") or r.get("status")
        for m in report.get("missing_biological_fields") or []:
            self.missing[m] = self.missing.get(m, 0) + 1
        for c in haul["unmapped_columns"]:
            self.unmapped[c] = self.unmapped.get(c, 0) + 1

    def summary(self) -> Dict[str, Any]:
        return {"taxa_records": self.records,
                "unresolved_taxa": self.unresolved or None,
                "taxa_resolved_at_higher_rank": self.higher_rank or None,
                "life_stage_lookups": self.stages or None,
                "missing_biological_fields": self.missing or None,
                "unmapped_columns": self.unmapped or None,
                "taxa_list_unavailable": self.unavailable}


def _resolution_counts(hauls: List[Dict[str, Any]]) -> Dict[str, int]:
    statuses = [r["taxon"]["status"] for h in hauls for r in h["records"]]
    return {"resolved": statuses.count("matched"),
            "higher_rank": statuses.count("matched_higher_rank"),
            "by_name": sum(1 for s in statuses if s not in ("matched", "matched_higher_rank"))}


def _staged_hauls(source: Path) -> List[str]:
    return ([str(source)] if source.is_file() else
            sorted(p for p in glob.glob(str(source / "**" / "*.csv"), recursive=True)
                   if is_staged_haul(p)))


def _checked_haul(path: str, probe: Path) -> Dict[str, Any]:
    """Map, write and validate one haul on its own: `{"haul", ...}` or `{"stage", "error"}`."""
    try:
        haul = map_haul(read_staged_haul(path))
    except Exception as e:                              # noqa: BLE001 - unattended run
        return {"stage": "map_haul", "error": f"{type(e).__name__}: {e}"}
    if not haul["records"]:
        return {"stage": "map_haul", "error": "no Taxa-Records"}
    w = write_taxa_netcdf([haul], str(probe))
    if w.get("status") != "success":
        return {"stage": "write_taxa_netcdf", "error": w.get("error")}
    report = validate_taxa_netcdf(str(probe))
    if not report["passed"]:
        return {"stage": "validate_taxa_netcdf", "error": report["errors"]}
    return {"haul": haul, "report": report, "records": w["taxa_records"]}


def assemble_taxa(staged_dir, processed_dir, accession: str) -> Dict[str, Any]:
    """Write the staged hauls under `staged_dir` as one Taxa-Record file per year.

    Each haul is checked alone first so a bad one does not block the rest. Stale Taxa-Record
    files of this accession are removed. Returns `{"files", "failures", "removed", "taxa"}`.
    """
    processed = Path(processed_dir)
    processed.mkdir(parents=True, exist_ok=True)
    src = Path(staged_dir) if staged_dir else None
    files = _staged_hauls(src) if src is not None and src.exists() else []
    tally, failures = _Tally(), []
    by_year: Dict[Optional[int], List[Dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            key = Path(f).relative_to(src).as_posix()
            res = _checked_haul(f, Path(tmp) / "haul.nc")
            if "haul" not in res:
                failures.append({"file": key, **res})
                continue
            tally.add(res["haul"], res["report"])
            year = cast_year({"contract": res["haul"]["contract"]})
            by_year.setdefault(year, []).append({"key": key, "haul": res["haul"]})

    written: List[Dict[str, Any]] = []
    for year, members in sorted(by_year.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        name = taxa_file_name(year, accession)
        part = processed / f"{name}.part"
        hauls = [m["haul"] for m in members]
        w = write_taxa_netcdf(hauls, str(part))
        report = (validate_taxa_netcdf(str(part)) if w.get("status") == "success"
                  else {"passed": False, "errors": [w.get("error")]})
        if not report["passed"]:
            part.unlink(missing_ok=True)
            failures.append({"file": name, "stage": "write_taxa_netcdf",
                             "error": report["errors"]})
            continue
        os.replace(part, processed / name)
        sources = sorted({Path(s).name for h in hauls
                          for s in (h["contract"].get("source_files") or [])[:1]})
        written.append({"name": name, "year": year, "hauls": len(hauls),
                        "records": w["taxa_records"], "sources": sources,
                        "staged": [m["key"] for m in members], **_resolution_counts(hauls)})
        runlog.event(log, logging.INFO, f"wrote {name}: {len(hauls)} haul(s), "
                                        f"{w['taxa_records']} Taxa-Record(s)",
                     kind="taxa_file", name=name, hauls=len(hauls),
                     records=w["taxa_records"])

    names = {f["name"] for f in written}
    # A leftover `.part` is an unfinished write.
    for p in processed.glob("*.part"):
        if p.is_file() and is_taxa_file_name(p.name[:-len(".part")], accession):
            p.unlink()
    removed = sorted(p.name for p in processed.iterdir()
                     if p.is_file() and is_taxa_file_name(p.name, accession)
                     and p.name not in names)
    for n in removed:
        (processed / n).unlink()
    for fail in failures:
        runlog.event(log, logging.WARNING,
                     f"FAILED haul {fail['file']} at {fail['stage']}: {fail['error']}",
                     kind="haul", status="failed", **fail)
    return {"files": written, "failures": failures, "removed": removed,
            "taxa": tally.summary()}


def process_taxa(source, output_dir, accession: Optional[str] = None) -> Dict[str, Any]:
    """One Taxa-Record file per staged haul under `source` (a file or a folder)."""
    owned = runlog.active() is None
    with runlog.accession_log_for(source, accession, actor="library",
                                  arguments={"call": "process_taxa", "source": str(source),
                                             "output_dir": str(output_dir)}) as ctx:
        result = _process_taxa(source, output_dir)
        runlog.event(log, logging.WARNING if result["failures"] else logging.INFO,
                     f"process_taxa ended: {result['status']}, {len(result['outputs'])} "
                     f"haul(s) written, {len(result['failures'])} failed, "
                     f"{result['taxa_records']} Taxa-Record(s)",
                     kind="taxa_end", **{k: v for k, v in result.items() if k != "note"})
        if owned and ctx is not None:
            ctx["status"] = result["status"]
        return result


def _process_taxa(source, output_dir) -> Dict[str, Any]:
    src = Path(source)
    files = _staged_hauls(src)
    out_dir = Path(output_dir)
    outputs, failures = [], []
    tally = _Tally()
    for f in files:
        # Relative path, so same-named files in different folders do not collide.
        key = Path(f).name if src.is_file() else Path(f).relative_to(src).as_posix()
        out = out_dir / f"{_output_stem(key)}.nc"
        res = _checked_haul(f, out)
        if "haul" not in res:
            failures.append({"file": f, **res})
            continue
        outputs.append(str(out))
        tally.add(res["haul"], res["report"])
        runlog.event(log, logging.INFO, f"wrote {out}: {res['records']} Taxa-Record(s)",
                     kind="haul", file=f, output=str(out), records=res["records"])

    for fail in failures:
        runlog.event(log, logging.WARNING,
                     f"FAILED haul {fail['file']} at {fail['stage']}: {fail['error']}",
                     kind="haul", status="failed", **fail)
    summary = tally.summary()
    summary.pop("taxa_list_unavailable")
    return {
        "status": ("empty" if not files else "complete" if not failures else "incomplete"),
        "staged_files": len(files), "outputs": outputs, "failures": failures, **summary,
        "note": "Lists: unresolved taxa, taxa resolved at a higher rank, missing biological "
                "fields, unmapped columns.",
    }
