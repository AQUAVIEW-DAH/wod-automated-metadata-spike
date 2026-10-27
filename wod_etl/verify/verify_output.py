"""Read the written NetCDF back and check that its values are possible.

Compares every observation with the manual's range for its variable and depth (Appendix 11)
and reports findings with a severity. An unreadable file is a finding, not an exception.

    from wod_etl.verify import verify_output
    report = verify_output("data/processed/2-CTD")
    report["passed"], report["findings"]

Ranges are the union across all ocean basins, so an unflagged value may still be wrong.
"""
import glob
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import re

import numpy as np

# At least this fraction out of range means a unit or conversion error, not bad levels.
SYSTEMATIC_FRACTION = 0.5

# Below this fraction, out-of-range levels are reported at low severity.
NOTABLE_FRACTION = 0.02

# Conversion factors recognisable in a ratio, reported as a suspected cause.
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

# Values below a zero lower bound by at most this fraction of the variable's 99th
# percentile are reported as a sensor zero offset, not as out of range.
NEAR_ZERO_FRACTION = 0.1


# Values range-checked per variable; past this the report says how many were not.
SAMPLE_LEVELS = 5_000_000


def verify_output(processed_dir, sample_levels: int = SAMPLE_LEVELS,
                  paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Check every NetCDF in a directory against the manual's acceptable ranges.

    `paths` checks exactly those files instead. `sample_levels` caps how many observations
    of one variable are range-checked; the cap is reported when it applies.
    """
    paths = (sorted(str(p) for p in paths) if paths is not None
             else sorted(glob.glob(str(Path(processed_dir) / "*.nc"))))
    report: Dict[str, Any] = {
        "processed_dir": str(processed_dir),
        "netcdf_files": len(paths),
        "casts": 0,
        "observations": 0,
        "variables": {},
        "flag_distribution": {},
        "findings": [],
        "checked": ["acceptable_range", "near_zero_offset", "all_fill", "constant_value",
                    "depth_axis", "unique_cast_id", "identical_variables"],
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
    # `passed` means no high finding; a medium finding is a question, not a failure.
    report["passed"] = report["finding_counts"]["high"] == 0
    report["text"] = render(report)
    log = logging.getLogger(__name__)
    for f in report["findings"]:
        log.log(logging.WARNING if f["severity"] == "high" else logging.INFO,
                "verify %s [%s] %s: %s", processed_dir, f["severity"],
                f["variable"] or f["file"] or "-", f["evidence"])
    return report


# ------------------------------------------------------------------ reading
def _read_one(path, per_variable, flags, report, sample_levels, cast_ids=None) -> None:
    import netCDF4 as nc

    with nc.Dataset(path) as ds:
        report["casts"] += len(ds.dimensions.get("casts", ()) or ())
        report["observations"] += len(ds.dimensions.get("z_obs", ()) or ())
        if cast_ids is not None and "cast_id" in ds.variables:
            _register_cast_ids(ds, Path(path).name, cast_ids)

        z = _numeric(ds.variables["z"]) if "z" in ds.variables else None
        # Judge each source on its own, so one source's problem cannot hide among another's.
        measured = _measured(ds)
        sizes = (np.ma.filled(np.ma.asarray(ds.variables["z_row_size"][:]), 0).astype(int)
                 .ravel() if "z_row_size" in ds.variables else None)
        starts = np.concatenate([[0], np.cumsum(sizes)[:-1]]) if sizes is not None else None
        for source, levels in _source_levels(ds):
            where = Path(path).name + (f" ({source})" if source else "")
            if z is not None:
                # Casts of one level each (surface samples at 0 m) share a depth by nature.
                n_casts = (None if sizes is None else len(sizes) if levels is None
                           else int(np.isin(starts, levels).sum()))
                n_levels = z.size if levels is None else len(levels)
                _check_depth_axis(where, z if levels is None else z[levels],
                                  report["findings"], str(getattr(ds, "wod_dataset_type", "")),
                                  one_level_casts=n_casts == n_levels)
            _check_copies(where, measured, report["findings"], levels)

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
            bad = _flagged_bad(ds, name, data.size)
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
            # Keep values with their depths: the acceptable range depends on depth.
            room = sample_levels - len(entry["values"])
            # A level its originator flagged bad is not judged again: it is known bad.
            entry["originator_bad"] = entry.get("originator_bad", 0) + int((finite & bad).sum())
            present = np.flatnonzero(finite & ~bad)
            keep = present[:max(room, 0)]
            entry["values"].extend(data[keep].tolist())
            entry["depths"].extend((z[keep].tolist() if z is not None
                                    and z.size == data.size
                                    else [np.nan] * keep.size))
            # Report whenever the cap applies, so unchecked values are never claimed verified.
            if keep.size < present.size:
                entry["truncated"] = True
                entry["not_checked"] = entry.get("not_checked", 0) + int(present.size - keep.size)


# Fewer shared levels than this and two variables agreeing proves nothing.
MIN_LEVELS_FOR_A_COPY = 10


def _measured(ds) -> Dict[str, np.ndarray]:
    """Every measured variable of a file, as floats with fill as NaN."""
    out = {n: _numeric(v) for n, v in ds.variables.items()
           if getattr(v, "wod_variable_code", None) is not None
           and not n.endswith(("_qc", "_qc_originator"))}
    return {n: a for n, a in out.items() if a is not None}


def _cast_sources(ds) -> Optional[List[str]]:
    """Each cast's source file, from the per-cast variable or the file's attribute."""
    n = len(ds.dimensions.get("casts", ()) or ())
    if "source_file" in ds.variables:
        sources = [str(s) for s in np.asarray(ds.variables["source_file"][:]).ravel()]
        return sources if len(sources) == n else None
    return [str(getattr(ds, "source_file", ""))] * n


def _register_cast_ids(ds, name: str, cast_ids: Dict[str, List[str]]) -> None:
    """Record each cast id of a file. An id the writer marks as shared across source files
    (`repeated_across_source_files`) is recorded again so it is reported as a duplicate."""
    var = ds.variables["cast_id"]
    for cid in np.asarray(var[:]).ravel():
        cast_ids.setdefault(str(cid), []).append(name)
    for cid in str(getattr(var, "repeated_across_source_files", "") or "").split("; "):
        if cid:
            cast_ids.setdefault(cid, []).append(name)


def _source_levels(ds) -> List[tuple]:
    """[(source, level indices)] per source file of the casts, or a single `("", None)`
    (every level) for a file of one source."""
    n = len(ds.dimensions.get("casts", ()) or ())
    if "source_file" not in ds.variables or "z_row_size" not in ds.variables or not n:
        return [("", None)]
    sources = _cast_sources(ds)
    rows = np.ma.filled(np.ma.asarray(ds.variables["z_row_size"][:]), 0).astype(int).ravel()
    if sources is None or rows.size != n:
        return [("", None)]
    level_cast = np.repeat(np.arange(n), rows)
    out = []
    for source in dict.fromkeys(sources):
        casts = np.flatnonzero(np.asarray(sources) == source)
        out.append((source, np.flatnonzero(np.isin(level_cast, casts))))
    return out


def _check_copies(name: str, measured: Dict[str, np.ndarray], findings,
                  levels: Optional[np.ndarray] = None) -> None:
    """Two measured variables equal at every level: one is a copy of the other, which range
    checks cannot catch. `levels` limits the check to one source's levels."""
    names = list(measured)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            x, y = measured[a], measured[b]
            if levels is not None:
                if x.shape != y.shape or levels.size == 0 or levels.max() >= x.size:
                    continue
                x, y = x[levels], y[levels]
            both = np.isfinite(x) & np.isfinite(y)
            if x.shape == y.shape and both.sum() >= MIN_LEVELS_FOR_A_COPY \
                    and np.array_equal(x[both], y[both]) \
                    and np.array_equal(np.isfinite(x), np.isfinite(y)):
                findings.append(_finding(
                    "identical_variables", "high", name, b,
                    f"{b} equals {a} at every one of {int(both.sum())} levels",
                    "One is a copy of the other, not a measurement. Find out which the "
                    "originator meant, and leave the copy out: drop = [\"" + b + "\"] in its "
                    "decisions.toml entry."))


# Flag meanings that say a value is wrong (CF flag_meanings, as the originator stated them).
_BAD_MEANING = re.compile(r"^(?:bad|erroneous|wrong|incorrect|fail(?:ed)?|reject(?:ed)?|"
                          r"invalid)(?:_|$)", re.I)


def _flagged_bad(ds, name: str, size: int) -> np.ndarray:
    """Levels whose originator flag (`<name>_qc_originator`) means bad, by its own scheme."""
    o = ds.variables.get(f"{name}_qc_originator")
    meanings = str(getattr(o, "flag_meanings", "") or "").split() if o is not None else []
    values = np.atleast_1d(getattr(o, "flag_values", [])) if o is not None else []
    bad_values = [float(v) for v, m in zip(values, meanings) if _BAD_MEANING.match(m)]
    flags = _numeric(o) if bad_values else None
    if flags is None or flags.size != size:
        return np.zeros(size, dtype=bool)
    return np.isin(flags, bad_values)


def _numeric(var) -> Optional[np.ndarray]:
    try:
        if var.dtype == str or np.asarray(var[:]).dtype.kind in "USO":
            return None
        # np.ma keeps the mask, so fill values become NaN rather than measurements.
        return np.ma.filled(np.ma.asarray(var[:], dtype=float), np.nan).ravel()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ judging
def _summarize(name, v, findings) -> Dict[str, Any]:
    from wod_etl.qc_transforms.apply_qc_auditing import range_at_depth, ranges_at_depths

    out = {"wod_code": v["wod_code"], "units": v["units"],
           "n": v["n"], "n_finite": v["n_finite"],
           "min": None if v["n_finite"] == 0 else round(v["min"], 6),
           "max": None if v["n_finite"] == 0 else round(v["max"], 6),
           "levels_range_checked": len(v["values"]),
           "sample_truncated": v["truncated"],
           "values_not_range_checked": v.get("not_checked", 0),
           "flagged_bad_by_originator": v.get("originator_bad", 0)}

    if v["n_finite"] == 0:
        findings.append(_finding(
            "all_fill", "medium", None, name, "every value is fill",
            "The source column existed and mapped but held nothing, or the conversion "
            "produced NaN throughout. Correct output if the column really was empty."))
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

    bounds = ranges_at_depths(v["wod_code"], depths)
    if bounds is not None:
        lo, hi = bounds
    else:
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
            "A sensor zero or dark offset, not impossible data. The values are kept as "
            "submitted (QC flags them downstream), not clipped to zero, which would change "
            "the originator's numbers."))
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
            "A few extreme levels, which is normal in real casts and not alarming."))
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
        "merges these files. If it is one cast in two source files, leave one of them out "
        "(include = false); if they are different casts, give each an accession, cruise, "
        "cast or station and date in its staged header (the writer builds the id from "
        "those)."))


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


def _check_depth_axis(filename, z, findings, dataset: str = "",
                      one_level_casts: bool = False) -> None:
    finite = z[np.isfinite(z)]
    if finite.size == 0:
        return
    if (finite < 0).any():
        findings.append(_finding(
            "negative_depth", "high", filename, "z",
            f"{int((finite < 0).sum())} level(s) below zero, minimum {finite.min():g} m",
            "Depth is positive down. A negative depth is a sign convention that was not "
            "applied, or a height above the surface written into the depth axis."))
    surface = dataset == "SUR"           # a track at the surface: every level is 0 m
    if finite.size > 1 and finite.min() == finite.max() and not surface \
            and not one_level_casts:
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
    lines.append("  Note: ranges are the union across all 30 ocean basins, so they are "
                 "more permissive than a basin-aware check. A flagged value is outside "
                 "every basin's bound; an unflagged one may still be wrong.")
    return "\n".join(lines)
