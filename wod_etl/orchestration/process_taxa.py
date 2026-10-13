"""End-to-end driver for biological data: staged hauls in, Taxa-Record NetCDF out.

The counterpart of `process_accession` for plankton counts and biomass, which WOD stores as
a Biological Header and Taxa-Records rather than as a profile. Each phase is the same phase
package a profile goes through:

    staging.read_staged_haul -> mapping.map_haul -> writers.write_taxa_netcdf
                             -> verify.validate_taxa_netcdf

    from wod_etl.orchestration import process_taxa
    result = process_taxa("data/staging/0303928/plankton",
                          output_dir="data/processed/0303928/plankton")
    result["unresolved_taxa"], result["missing_biological_fields"]

One output file per staged haul file, like the profile path. Nothing raises: a haul that
fails is listed under `failures` with the stage, and the rest continue.
"""
import glob
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from wod_etl import runlog

from wod_etl.mapping.taxa import map_haul
from wod_etl.orchestration.process_accession import _output_stem
from wod_etl.staging.taxa_haul import is_staged_haul, read_staged_haul
from wod_etl.verify.validate_taxa_netcdf import validate_taxa_netcdf
from wod_etl.writers.generate_netcdf_taxa import write_taxa_netcdf

log = logging.getLogger(__name__)


def process_taxa(source, output_dir, accession: Optional[str] = None) -> Dict[str, Any]:
    """Every staged haul under `source` (a file or a folder) to Taxa-Record NetCDF.

    Logged to the accession's run log (`accession`, or worked out from `source`).
    """
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
    files = ([str(src)] if src.is_file() else
             sorted(p for p in glob.glob(str(src / "**" / "*.csv"), recursive=True)
                    if is_staged_haul(p)))
    out_dir = Path(output_dir)
    outputs, failures = [], []
    unresolved: Dict[str, int] = {}
    higher_rank: Dict[str, str] = {}
    stages: Dict[str, str] = {}
    missing: Dict[str, int] = {}
    unmapped: Dict[str, int] = {}
    n_records = 0
    for f in files:
        try:
            haul = map_haul(read_staged_haul(f))
        except Exception as e:                          # noqa: BLE001 - unattended run
            failures.append({"file": f, "stage": "map_haul", "error": f"{type(e).__name__}: {e}"})
            continue
        if not haul["records"]:
            failures.append({"file": f, "stage": "map_haul", "error": "no Taxa-Records"})
            continue
        # Named from the path under `source`, as the profile path names its outputs: the
        # glob is recursive, and two stations' `haul.csv` must not write one `haul.nc`.
        key = Path(f).name if src.is_file() else Path(f).relative_to(src).as_posix()
        out = out_dir / f"{_output_stem(key)}.nc"
        w = write_taxa_netcdf([haul], str(out))
        if w.get("status") != "success":
            failures.append({"file": f, "stage": "write_taxa_netcdf", "error": w.get("error")})
            continue
        report = validate_taxa_netcdf(str(out))
        if not report["passed"]:
            failures.append({"file": f, "stage": "validate_taxa_netcdf",
                             "error": report["errors"]})
            continue
        outputs.append(str(out))
        n_records += w["taxa_records"]
        runlog.event(log, logging.INFO, f"wrote {out}: {w['taxa_records']} Taxa-Record(s)",
                     kind="haul", file=f, output=str(out), records=w["taxa_records"])
        for name, t in haul["taxa"].items():
            if t["status"] == "matched_higher_rank":
                higher_rank[name] = t["note"]
            elif t["status"] not in ("matched",):
                unresolved[name or "(blank)"] = unresolved.get(name or "(blank)", 0) + sum(
                    1 for r in haul["records"] if r["taxon"] is t)
        for text, r in haul["life_stages"].items():
            stages[text] = r.get("summary") or r.get("status")
        for m in report.get("missing_biological_fields") or []:
            missing[m] = missing.get(m, 0) + 1
        for c in haul["unmapped_columns"]:
            unmapped[c] = unmapped.get(c, 0) + 1

    for fail in failures:
        runlog.event(log, logging.WARNING,
                     f"FAILED haul {fail['file']} at {fail['stage']}: {fail['error']}",
                     kind="haul", status="failed", **fail)
    return {
        "status": ("empty" if not files else "complete" if not failures else "incomplete"),
        "staged_files": len(files), "outputs": outputs, "failures": failures,
        "taxa_records": n_records,
        "unresolved_taxa": unresolved or None,
        "taxa_resolved_at_higher_rank": higher_rank or None,
        "life_stage_lookups": stages or None,
        "missing_biological_fields": missing or None,
        "unmapped_columns": unmapped or None,
        "note": ("Report all four lists: unresolved taxa keep only the originator's name; a "
                 "higher-rank resolution loses the species; missing Biological Header fields "
                 "leave counts per sample; unmapped columns were not written."),
    }
