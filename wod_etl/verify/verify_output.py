"""Read the written NetCDF back and judge it, not just measure it.

`validate_intermediate_netcdf` gates the structure: does this file obey CF and the ragged
array contract. `audit_dataset` reports what a dataset mapped, dropped and flagged. Neither
says whether the NUMBERS are possible -- and the failure that matters most is the one that
passes both: a variable in the wrong unit validates cleanly, reads back cleanly, and is
wrong by a factor of a thousand.

This closes that. It compares every observation against the manual's own acceptable range
for that variable at that depth (Appendix 11), and turns what it finds into FINDINGS with a
severity, so a run can fail its own output instead of waiting to be asked.

    from wod_etl.verify import verify_output
    report = verify_output("data/processed/2-CTD")
    report["passed"], report["findings"]

`process_accession` calls this at the end of every run. Nothing here raises: an unreadable
file becomes a finding.

**A finding is not proof.** The ranges are the union across all 30 ocean basins, so they are
more permissive than a basin-aware check -- which cuts one way only: something flagged here
is outside EVERY basin's bound and needs looking at, while something unflagged may still be
wrong. `high` severity means the whole variable is out of range, which is a unit or a
conversion, not bad data.
"""
import glob
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

# A variable entirely outside its acceptable range is not a data problem. Individual bad
# levels are normal in real casts; ALL of them being bad means the column was converted
# wrongly, or not at all.
SYSTEMATIC_FRACTION = 0.5

# Below this, out-of-range levels are reported as a count without raising severity: a
# handful of extreme values in a real cast is what QC flags exist for.
NOTABLE_FRACTION = 0.02

# Conversion factors whose signature is recognisable in a ratio. Naming the likely cause
# saves the reader deriving it, and getting it wrong costs nothing -- it is phrased as a
# suspicion and the evidence is printed beside it.
_SUSPECT_RATIOS = [
    (1000.0, "values are ~1000x too large: a per-litre column written into a per-kilogram "
             "variable, or mmol read as umol"),
    (0.001, "values are ~1000x too small: the reciprocal of the above"),
    (1.8, "values are ~1.8x too large: Fahrenheit written into a Celsius variable"),
    (0.3048, "values are ~0.3x: metres written where feet were expected, or the reverse"),
    (3.2808, "values are ~3.3x too large: feet written into a metres variable"),
    (1.025, "values are ~1.025x: a seawater-density division that did not happen"),
    (0.9756, "values are ~0.98x: a seawater-density division applied twice"),
]
_RATIO_TOLERANCE = 0.15

# A value just below a lower bound of zero is usually not an impossible value but a sensor
# zero offset: a fluorometer's dark count, a nutrient analyser's blank. Below the bound by no
# more than this fraction of the variable's own 99th percentile, it is reported as an offset
# rather than counted as out of range -- 0303928's fluorometer chlorophyll had 14% of levels
# at -0.02 ug/l and was reported as 'enough levels are impossible to be worth reading'.
NEAR_ZERO_FRACTION = 0.1


def verify_output(processed_dir, sample_levels: int = 200_000) -> Dict[str, Any]:
    """Check every NetCDF in a directory against the manual's acceptable ranges.

    `sample_levels` caps how many observations of one variable are range-checked, so a
    multi-million-level dataset stays cheap. The cap is reported when it bites.
    """
    paths = sorted(glob.glob(str(Path(processed_dir) / "*.nc")))
    report: Dict[str, Any] = {
        "processed_dir": str(processed_dir),
        "netcdf_files": len(paths),
        "casts": 0,
        "observations": 0,
        "variables": {},
        "flag_distribution": {},
        "findings": [],
        "checked": ["acceptable_range", "near_zero_offset", "all_fill", "constant_value",
                    "depth_axis", "unique_cast_id"],
    }
    if not paths:
        report["passed"] = True
        report["note"] = "no output to verify"
        return report

    per_variable: Dict[str, Dict[str, Any]] = {}
    flags: Dict[str, Dict[int, int]] = {}
    cast_ids: Dict[str, List[str]] = {}

    for path in paths:
        try:
            _read_one(path, per_variable, flags, report, sample_levels, cast_ids)
        except Exception as e:                       # noqa: BLE001 - a bad file is a finding
            report["findings"].append(_finding(
                "unreadable_output", "high", Path(path).name, None,
                f"{type(e).__name__}: {e}",
                "The pipeline wrote a file it cannot read back. Treat the run as failed for "
                "this file."))

    for name, v in sorted(per_variable.items()):
        report["variables"][name] = _summarize(name, v, report["findings"])
    _check_cast_ids(cast_ids, report["findings"])
    report["flag_distribution"] = {
        name: {"total": sum(c.values()),
               "non_zero_pct": round(100.0 * sum(n for k, n in c.items() if k != 0)
                                     / max(sum(c.values()), 1), 3),
               "counts": {str(k): n for k, n in sorted(c.items())}}
        for name, c in sorted(flags.items())}

    order = {"high": 0, "medium": 1, "low": 2}
    report["findings"].sort(key=lambda f: (order.get(f["severity"], 3), f["variable"] or ""))
    report["finding_counts"] = {
        s: sum(1 for f in report["findings"] if f["severity"] == s)
        for s in ("high", "medium", "low")}
    # `passed` is about the numbers being possible, not about the run finishing. A medium
    # finding is a question; a high finding is a variable that cannot be what it claims.
    report["passed"] = report["finding_counts"]["high"] == 0
    report["text"] = render(report)
    return report


# ------------------------------------------------------------------ reading
def _read_one(path, per_variable, flags, report, sample_levels, cast_ids=None) -> None:
    import netCDF4 as nc

    with nc.Dataset(path) as ds:
        report["casts"] += len(ds.dimensions.get("casts", ()) or ())
        report["observations"] += len(ds.dimensions.get("z_obs", ()) or ())
        if cast_ids is not None and "cast_id" in ds.variables:
            for cid in np.asarray(ds.variables["cast_id"][:]).ravel():
                cast_ids.setdefault(str(cid), []).append(Path(path).name)

        z = _numeric(ds.variables["z"]) if "z" in ds.variables else None
        if z is not None:
            _check_depth_axis(Path(path).name, z, report["findings"])

        for name, var in ds.variables.items():
            if name.endswith("_qc"):
                vals = np.asarray(var[:]).astype(int).ravel()
                counter = flags.setdefault(name, {})
                for value, count in zip(*np.unique(vals, return_counts=True)):
                    counter[int(value)] = counter.get(int(value), 0) + int(count)
                continue
            code = getattr(var, "wod_variable_code", None)
            if code is None or name.endswith("_qc_originator"):
                continue

            data = _numeric(var)
            if data is None:
                continue
            entry = per_variable.setdefault(name, {
                "wod_code": int(code), "units": getattr(var, "units", ""),
                "n": 0, "n_finite": 0, "min": np.inf, "max": -np.inf,
                "values": [], "depths": [], "files": 0, "truncated": False})
            entry["files"] += 1
            entry["n"] += int(data.size)
            finite = np.isfinite(data)
            entry["n_finite"] += int(finite.sum())
            if finite.any():
                entry["min"] = min(entry["min"], float(data[finite].min()))
                entry["max"] = max(entry["max"], float(data[finite].max()))
            # Keep values and their depths together: the acceptable range is depth-dependent,
            # so a value cannot be judged without the level it was measured at.
            room = sample_levels - len(entry["values"])
            if room > 0:
                keep = np.flatnonzero(finite)[:room]
                entry["values"].extend(data[keep].tolist())
                entry["depths"].extend((z[keep].tolist() if z is not None
                                        and z.size == data.size
                                        else [np.nan] * keep.size))
            elif finite.any():
                entry["truncated"] = True


def _numeric(var) -> Optional[np.ndarray]:
    try:
        if var.dtype == str or np.asarray(var[:]).dtype.kind in "USO":
            return None
        return np.ma.filled(np.asarray(var[:], dtype=float), np.nan).ravel()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ judging
def _summarize(name, v, findings) -> Dict[str, Any]:
    from wod_etl.qc_transforms.apply_qc_auditing import range_at_depth

    out = {"wod_code": v["wod_code"], "units": v["units"],
           "n": v["n"], "n_finite": v["n_finite"],
           "min": None if v["n_finite"] == 0 else round(v["min"], 6),
           "max": None if v["n_finite"] == 0 else round(v["max"], 6),
           "levels_range_checked": len(v["values"]),
           "sample_truncated": v["truncated"]}

    if v["n_finite"] == 0:
        findings.append(_finding(
            "all_fill", "medium", None, name, "every value is fill",
            "The source column existed and mapped but held nothing, or the conversion "
            "produced NaN throughout. Honest output if the column really was empty -- say "
            "which it was."))
        return out

    if v["n_finite"] > 1 and v["min"] == v["max"]:
        findings.append(_finding(
            "constant_value", "medium", None, name,
            f"every one of {v['n_finite']} values is {v['min']}",
            "A stuck sensor, a fill value that was not recognised as one, or a per-cast "
            "metadata column mapped as a measurement."))

    values = np.asarray(v["values"], dtype=float)
    depths = np.asarray(v["depths"], dtype=float)
    if values.size == 0:
        return out

    lo = np.full(values.size, -np.inf)
    hi = np.full(values.size, np.inf)
    have_bounds = False
    # Bounds change with depth but only across 33 listed levels, so resolve once per
    # distinct depth rather than once per observation.
    for depth in np.unique(np.where(np.isfinite(depths), depths, 0.0)):
        bounds = range_at_depth(v["wod_code"], float(depth))
        if bounds is None:
            break
        have_bounds = True
        at = np.isclose(np.where(np.isfinite(depths), depths, 0.0), depth)
        lo[at], hi[at] = bounds
    if not have_bounds:
        out["acceptable_range"] = "not defined for this variable in Appendix 11"
        return out

    outside = (values < lo) | (values > hi)
    near_zero = _near_zero_offset(values, lo, outside)
    if near_zero.any():
        n_nz = int(near_zero.sum())
        findings.append(_finding(
            "near_zero_offset", "low", None, name,
            f"{n_nz} of {values.size} levels ({n_nz / values.size:.1%}) are below the lower "
            f"bound of 0 by at most {abs(values[near_zero].min()):g} {v['units']} (minimum "
            f"{values[near_zero].min():g}), small against the variable's own range",
            "A sensor zero or dark offset, not impossible data. Keep the values as "
            "submitted -- QC flags them downstream -- and say so in the report. Do not clip "
            "them to zero: that changes the originator's numbers."))
        outside = outside & ~near_zero
    n_out = int(outside.sum())
    fraction = n_out / values.size
    out["out_of_range"] = {"levels": n_out, "fraction": round(fraction, 4),
                           "near_zero_offset_levels": int(near_zero.sum()),
                           "bound_at_surface": list(range_at_depth(v["wod_code"], 0.0))}
    if n_out == 0:
        return out

    worst_at = int(np.argmax(np.where(outside, np.abs(values - np.clip(values, lo, hi)), 0)))
    evidence = (f"{n_out} of {values.size} levels ({fraction:.1%}) outside Appendix 11; "
                f"worst {values[worst_at]:g} {v['units']} at "
                f"{depths[worst_at]:g} m where the bound is "
                f"[{lo[worst_at]:g}, {hi[worst_at]:g}]")

    if fraction >= SYSTEMATIC_FRACTION:
        findings.append(_finding(
            "systematically_out_of_range", "high", None, name, evidence,
            "This is a unit or a conversion, not bad data: individual extreme levels happen, "
            "a whole variable being impossible does not. "
            + _suspect_cause(values[~np.isnan(values)], lo, hi)))
    elif fraction >= NOTABLE_FRACTION:
        findings.append(_finding(
            "out_of_range_levels", "medium", None, name, evidence,
            "Enough levels are impossible to be worth reading. Check the extremes against "
            "the source file before accepting them."))
    else:
        findings.append(_finding(
            "out_of_range_levels", "low", None, name, evidence,
            "A few extreme levels, which is normal in real casts. Reportable, not alarming."))
    return out


def _near_zero_offset(values, lo, outside) -> np.ndarray:
    """Levels below a lower bound of zero by a small fraction of the variable's scale."""
    below = outside & (values < lo) & (lo == 0)
    if not below.any():
        return below
    positive = values[values > 0]
    if positive.size == 0:
        return np.zeros_like(below)
    scale = float(np.percentile(positive, 99))
    return below & (values >= -NEAR_ZERO_FRACTION * scale)


def _check_cast_ids(cast_ids: Dict[str, List[str]], findings) -> None:
    """A cast id must identify one cast across the whole run, not only within its file."""
    repeated = {cid: files for cid, files in cast_ids.items() if len(files) > 1}
    if not repeated:
        return
    shown = "; ".join(f"{cid} in {', '.join(sorted(set(files))[:3])}"
                      for cid, files in list(repeated.items())[:5])
    findings.append(_finding(
        "duplicate_cast_id", "high", None, "cast_id",
        f"{len(repeated)} cast id(s) occur more than once across {len(cast_ids)} id(s): "
        f"{shown}",
        "cast_id is the CF profile_id: two casts sharing one are one cast to any tool that "
        "merges these files. Give each cast an accession, cruise, cast or station and date "
        "in its staged header -- the writer builds the id from those."))


def _suspect_cause(values, lo, hi) -> str:
    """Name the conversion a systematic offset looks like, when the ratio matches one."""
    band = np.where(np.isfinite(lo) & np.isfinite(hi), (lo + hi) / 2.0, np.nan)
    usable = np.isfinite(band) & (band != 0)
    if not usable.any():
        return "Check the column's unit against Table 3a."
    ratio = float(np.median(values[usable] / band[usable]))
    for factor, explanation in _SUSPECT_RATIOS:
        if abs(ratio - factor) <= _RATIO_TOLERANCE * abs(factor):
            return f"The median is {ratio:.3g}x the middle of the band: {explanation}."
    return (f"The median is {ratio:.3g}x the middle of the band. Check the column's unit "
            f"against Table 3a.")


def _check_depth_axis(filename, z, findings) -> None:
    finite = z[np.isfinite(z)]
    if finite.size == 0:
        return
    if (finite < 0).any():
        findings.append(_finding(
            "negative_depth", "high", filename, "z",
            f"{int((finite < 0).sum())} level(s) below zero, minimum {finite.min():g} m",
            "Depth is positive down. A negative depth is a sign convention that was not "
            "applied, or a height above the surface written into the depth axis."))
    if finite.size > 1 and finite.min() == finite.max():
        findings.append(_finding(
            "flat_depth_axis", "high", filename, "z",
            f"every one of {finite.size} levels is {finite.min():g} m",
            "Every measurement sits at one depth, so this is not a profile. Usually a "
            "bottom-depth or station-depth column mapped as the vertical axis."))


def _finding(kind, severity, file, variable, evidence, action) -> Dict[str, Any]:
    return {"finding": kind, "severity": severity, "file": file, "variable": variable,
            "evidence": evidence, "action": action}


# ---------------------------------------------------------------- rendering
def render(report: Dict[str, Any]) -> str:
    lines = [f"Output verification: {report['processed_dir']}",
             f"  {report['netcdf_files']} file(s), {report['casts']} cast(s), "
             f"{report['observations']} observation(s)"]
    if not report["findings"]:
        lines.append("  no findings: every value is within the manual's acceptable range")
    for f in report["findings"]:
        where = f["variable"] or f["file"] or "-"
        lines.append(f"  [{f['severity'].upper():6}] {where}: {f['evidence']}")
        lines.append(f"           -> {f['action']}")
    for name, v in report["variables"].items():
        if v.get("min") is None:
            continue
        lines.append(f"  {name}: {v['min']} .. {v['max']} {v['units']} "
                     f"({v['n_finite']}/{v['n']} finite)")
    for name, f in report["flag_distribution"].items():
        if f["non_zero_pct"]:
            lines.append(f"  {name}: {f['non_zero_pct']}% non-zero {f['counts']}")
    lines.append("  CAVEAT: ranges are the union across all 30 ocean basins, so they are "
                 "more permissive than a basin-aware check. A flagged value is outside "
                 "every basin's bound; an unflagged one may still be wrong.")
    return "\n".join(lines)
