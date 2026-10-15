"""Audit what a processed dataset actually mapped, dropped and flagged.

`process_accession` reports pass/fail. That is not enough to trust a run: a column in the
wrong unit, a dropped variable and a spurious QC flag all produce a clean summary. This
recomputes the mapping contract for every staged file and reads back every written NetCDF,
so the three things the manifest never contains can be reported.

    from wod_etl.verify import audit_dataset
    report = audit_dataset("data/staging/0287423/2-CTD", "CTD", "data/processed/2-CTD")

Print `report["text"]` for a readable summary, or use the structured fields.
"""
import collections
import glob
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from wod_etl.discovery import fetch_and_peek_file, load_tabular, profile_raw_data
from wod_etl.mapping import generate_field_mapping, query_wod_spec


def _depth_increment(df) -> Optional[float]:
    """Median positive level spacing, which is what decides CTD vs OSD routing."""
    import pandas as pd
    col = next((c for c in df.columns
                if str(c).strip().lower().startswith(("depth", "depsm", "depfm", "z "))), None)
    if col is None:
        return None
    vals = pd.to_numeric(df[col], errors="coerce").dropna().to_numpy(dtype=float)
    if vals.size < 2:
        return None
    d = np.diff(np.sort(vals))
    d = d[d > 0]
    return float(np.median(d)) if d.size else None


def contract_for(path: str, dataset_type: str) -> Dict[str, Any]:
    """The mapping contract for one staged file, or `{"error": ...}`."""
    peek = fetch_and_peek_file(str(path))
    if peek.get("error"):
        return {"error": peek["error"]}
    profile = profile_raw_data(peek["preview_text"], str(path))
    df, info = load_tabular(str(path), profile)
    if df is None:
        return {"error": info.get("error", "load_tabular returned no frame")}
    spec = query_wod_spec(dataset_type, list(df.columns),
                          depth_increment_m=_depth_increment(df))
    return generate_field_mapping({**profile, "columns": list(df.columns)}, spec)


def audit_dataset(staged_dir, dataset_type: str, processed_dir) -> Dict[str, Any]:
    """Report mapped columns, dropped columns, value ranges and QC flags for one dataset."""
    import netCDF4 as nc

    staged = sorted(p for p in glob.glob(str(Path(staged_dir) / "*")) if Path(p).is_file())
    mapped: collections.Counter = collections.Counter()
    unmapped: collections.Counter = collections.Counter()
    warnings: collections.Counter = collections.Counter()
    routing: collections.Counter = collections.Counter()
    coord_gaps: collections.Counter = collections.Counter()
    coord_gap_files: Dict[str, List[str]] = collections.defaultdict(list)
    errors: List[Dict[str, str]] = []

    for path in staged:
        c = contract_for(path, dataset_type)
        if c.get("error"):
            errors.append({"file": Path(path).name, "error": c["error"]})
            continue
        routing[c.get("wod_dataset_code")] += 1
        for m in c["column_mapping"]:
            mapped[f"{m['wod_name']} <- {m['raw_column']}"] += 1
        for u in c["unmapped_columns"]:
            unmapped[u] += 1
        for w in c["warnings"]:
            warnings[w] += 1
        for k in c["missing_required_coordinates"]:
            coord_gaps[k] += 1
            coord_gap_files[k].append(Path(path).name)

    ncs = sorted(glob.glob(str(Path(processed_dir) / "*.nc")))
    casts = obs = 0
    flags: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    ranges: Dict[str, Dict[str, float]] = {}
    empty: collections.Counter = collections.Counter()
    text_vars: Dict[str, set] = collections.defaultdict(set)
    sources: set = set()

    for path in ncs:
        with nc.Dataset(path) as ds:
            casts += len(ds.dimensions["casts"])
            obs += len(ds.dimensions["z_obs"])
            # Which files the casts came from: a WOD file holds casts of many sources, so the
            # number of NetCDF files says nothing about which staged files were written.
            if "source_file" in ds.variables:
                sources.update(str(x) for x in ds.variables["source_file"][:])
            elif "source_file" in ds.ncattrs():
                sources.add(str(ds.source_file))
            for name, var in ds.variables.items():
                if name.endswith("_qc"):
                    vals, counts = np.unique(np.asarray(var[:]).astype(int), return_counts=True)
                    for v, n in zip(vals, counts):
                        flags[name][int(v)] += int(n)
                    continue
                if name in ("cast_id", "z_row_size"):
                    continue
                if var.dtype == str or np.asarray(var[:]).dtype.kind in "USO":
                    # Per-cast text metadata -- the secondary_header_* variables the writer
                    # emits whenever a header differs between casts. Coercing them to float
                    # used to abort the whole audit on the normal multi-cast file.
                    text_vars[name].update(
                        str(v) for v in np.asarray(var[:]).ravel() if str(v))
                    continue
                a = np.ma.filled(np.ma.asarray(var[:], dtype=float), np.nan)
                finite = a[np.isfinite(a)]
                if finite.size == 0:
                    empty[name] += 1
                    continue
                r = ranges.setdefault(name, {"min": np.inf, "max": -np.inf,
                                             "units": getattr(var, "units", ""), "n": 0})
                r["min"] = min(r["min"], float(finite.min()))
                r["max"] = max(r["max"], float(finite.max()))
                r["n"] += int(finite.size)

    # By the file's resolved path: a name alone would let another folder's cast.csv answer
    # for this one's.
    written = {os.path.realpath(x) for x in sources if "://" not in x}
    staged_without_casts = [Path(p).name for p in staged if os.path.realpath(p) not in written]

    flag_summary = {}
    for name, counter in flags.items():
        total = sum(counter.values())
        nonzero = {k: v for k, v in sorted(counter.items()) if k != 0}
        flag_summary[name] = {
            "total": total,
            "non_zero_pct": round(100.0 * sum(nonzero.values()) / total, 3) if total else 0.0,
            "non_zero_counts": nonzero,
        }

    report = {
        "staged_dir": str(staged_dir), "processed_dir": str(processed_dir),
        "dataset_type": dataset_type,
        "staged_files": len(staged), "netcdf_files": len(ncs),
        "staged_without_casts": staged_without_casts,
        "casts": casts, "observations": obs,
        "wod_dataset_routing": dict(routing),
        "mapped_columns": dict(mapped),
        "unmapped_columns": dict(unmapped),
        "missing_coordinates": dict(coord_gaps),
        "missing_coordinate_files": {k: v for k, v in coord_gap_files.items()},
        "contract_warnings": dict(warnings),
        "contract_errors": errors,
        "value_ranges": ranges,
        "text_variables": {k: sorted(v) for k, v in sorted(text_vars.items())},
        "all_missing_variables": dict(empty),
        "qc_flags": flag_summary,
    }
    report["text"] = _render(report)
    return report


def _render(r: Dict[str, Any]) -> str:
    L = [f"### {r['staged_dir']}  ->  {r['processed_dir']}",
         f"  staged {r['staged_files']} files -> {r['netcdf_files']} NetCDF, "
         f"{r['casts']} casts, {r['observations']} levels",
         f"  routing: {r['wod_dataset_routing']}"]
    if r.get("staged_without_casts"):
        shown = ", ".join(r["staged_without_casts"][:5])
        more = len(r["staged_without_casts"]) - 5
        L.append(f"  {len(r['staged_without_casts'])} staged file(s) produced no cast: {shown}"
                 + (f", ... (+{more} more)" if more > 0 else "")
                 + " -- see MISSING COORDINATES and CONTRACT ERRORS below")
    L.append("  mapped:")
    for k, v in sorted(r["mapped_columns"].items()):
        L.append(f"      {v:5d}x  {k}")
    L.append("  UNMAPPED (dropped from output):")
    for k, v in sorted(r["unmapped_columns"].items()):
        L.append(f"      {v:5d}x  {k!r}")
    if not r["unmapped_columns"]:
        L.append("      (none)")
    if r["missing_coordinates"]:
        L.append(f"  MISSING COORDINATES -- these files cannot pass validation and will "
                 f"write no NetCDF. Supply the missing values from a station list or the "
                 f"readme via Phase 0 staging (wod_etl/staging/):")
        for k, n in sorted(r["missing_coordinates"].items()):
            files = r.get("missing_coordinate_files", {}).get(k, [])
            shown = ", ".join(files[:5]) + (f", ... (+{len(files) - 5} more)"
                                            if len(files) > 5 else "")
            L.append(f"      {k:10} missing in {n:4d} file(s): {shown}")
    if r["all_missing_variables"]:
        L.append("  written but entirely missing:")
        for k, v in sorted(r["all_missing_variables"].items()):
            L.append(f"      {v:5d} cast(s)  {k}")
    if r["contract_warnings"]:
        L.append("  contract warnings:")
        for k, v in r["contract_warnings"].items():
            L.append(f"      {v:5d}x  {k}")
    if r["contract_errors"]:
        L.append("  CONTRACT ERRORS:")
        for e in r["contract_errors"][:10]:
            L.append(f"      {e['file']}: {e['error'][:100]}")
    L.append("  value ranges:")
    for name, v in sorted(r["value_ranges"].items()):
        L.append(f"      {name:34} {v['units'][:20]:22} n={v['n']:8d} "
                 f"{v['min']:12.4g} .. {v['max']:12.4g}")
    if r["text_variables"]:
        L.append("  per-cast text metadata:")
        for name, values in r["text_variables"].items():
            shown = ", ".join(values[:6]) + (f", ... ({len(values)} distinct)" if len(values) > 6 else "")
            L.append(f"      {name:34} {shown}")
    L.append("  QC flags (0 = accepted):")
    for name, v in sorted(r["qc_flags"].items()):
        L.append(f"      {name:34} n={v['total']:8d}  non-zero={v['non_zero_pct']:6.2f}%  "
                 f"{v['non_zero_counts'] or ''}")
    return "\n".join(L)
