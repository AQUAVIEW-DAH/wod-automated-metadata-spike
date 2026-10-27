"""Staging recipes that `wod-etl confirm` runs for the included files.

Each recipe reshapes source files into staged casts or hauls. Every value a recipe adds (e.g. a
position from a station list) is recorded in the staged file's notes and the NetCDF `comment`.
"""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from wod_etl import config, runlog
from wod_etl.staging import (group_rows, join_station_list, normalise_station, pivot_long,
                             read_station_list, read_table, write_staged_cast,
                             write_staged_haul)

log = logging.getLogger(__name__)

DEFAULT_DEPTH_COLUMN = "Depth (m)"


def _safe(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "x"


def _station_lookup(root: Path, recipe: Dict[str, Any]):
    stations, info = read_station_list(root / recipe["station_list"],
                                       station=recipe.get("station_column"),
                                       time_zone=recipe.get("time_zone"))
    if info.get("error"):
        raise ValueError(f"station list {recipe['station_list']}: {info['error']}")
    return stations, info


def _apply_override(entry: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]):
    """A station-list entry with its override applied, plus notes describing the change."""
    o = overrides.get(normalise_station(entry["station"])) or {}
    if not o:
        return entry, []
    changed = dict(entry)
    for field in ("latitude", "longitude", "date", "time"):
        if o.get(field) not in (None, ""):
            changed[field] = o[field]
    return changed, list(o.get("notes") or [])


def _pivot(root: Path, recipe: Dict[str, Any]):
    tables = [read_table(root / f)[:2] for f in recipe["files"]]
    return pivot_long(tables, keys=list(recipe["keys"]), variable=recipe["variable"],
                      value=recipe["value"], key_aliases=recipe.get("key_aliases"),
                      columns=recipe.get("columns"))


def check_profile_recipe(recipe: Dict[str, Any], data_root) -> Dict[str, Any]:
    """Dry-run a profile recipe: casts the station list cannot locate, and pivot conflicts."""
    root = Path(data_root)
    if recipe.get("kind") == "mooring":
        return check_mooring_recipe(recipe, root)
    try:
        if recipe.get("kind") == "script" or recipe.get("station_list") in (None, "", "?"):
            return {}
        stations, _ = _station_lookup(root, recipe)
        if recipe.get("kind") == "long":
            if not recipe.get("files") or not recipe.get("keys"):
                return {"error": "files and keys are required"}
            cols, rows, info = _pivot(root, recipe)
            casts = [k[0] for k in group_rows(rows, cols, [recipe["cast_key"]]) if k]
            conflicts = len(info["conflicts"])
        elif recipe.get("kind") == "by_station":
            casts = list(dict.fromkeys(k for f in recipe.get("files") or []
                                       for k, _ in _station_groups(root / f, recipe)))
            conflicts = 0
        else:
            casts = [_cast_from_file(f, recipe) for f in recipe.get("files") or []]
            conflicts = 0
        mapping = recipe.get("station_map") or {}
        _, report = join_station_list([mapping.get(c, c) for c in casts], stations)
        reverse = {mapping.get(c, c): c for c in casts}
        return {"casts": len(casts), "pivot_conflicts": conflicts,
                "casts_without_station": [reverse.get(c, c)
                                          for c in report["casts_without_station"]],
                "stations_without_cast": report["stations_without_cast"]}
    except Exception as e:                              # noqa: BLE001 - reported to the person
        return {"error": f"{type(e).__name__}: {e}"}


def _unit_key(code: int, units: str) -> str:
    """A unit as a comparable word: 'degC', 'degree_C' and 'Celsius' are one unit."""
    u = re.sub(r"[\s_.°-]", "", str(units or "")).lower()
    if code == 1:
        return ("c" if u in ("c", "degc", "degreec", "degreesc", "celsius", "degreecelsius",
                             "degreescelsius", "its90", "degcits90") else u)
    if code in (2, 9):                                        # dimensionless
        return "" if u in ("", "1", "psu", "pss", "pss78", "practicalsalinityunits",
                           "unitless", "dimensionless", "0.001") else u
    return u


def check_mooring_recipe(recipe: Dict[str, Any], root: Path) -> Dict[str, Any]:
    """Check a mooring recipe without its records: instruments readable and placed, one unit
    per variable, and all at one position."""
    from wod_etl.staging import read_instrument

    problems, positions, units = [], [], {}
    depths = recipe.get("depths") or {}
    for f in recipe.get("files") or []:
        info = read_instrument(root / f)
        name = Path(f).name
        if info.get("error"):
            problems.append(f"{name}: {info['error']}")
            continue
        if depths.get(name) == "pressure" and not info.get("pressure"):
            problems.append(f"{name}: depth = \"pressure\", but it records no pressure it "
                            f"can be placed by; give its depth in m")
        for v in info["variables"]:
            key = _unit_key(v["code"], v["units"])
            seen = units.setdefault(v["code"], (key, v["units"] or "no unit", name))
            if seen[0] != key:
                problems.append(f"{name} states {v['column']!r} in '{v['units'] or 'no unit'}' "
                                f"where {seen[2]} states '{seen[1]}': one unit per variable on "
                                f"a mooring; convert one, or stage it with a script recipe")
        lat, lon = info.get("latitude"), info.get("longitude")
        if lat is not None and lon is not None:
            if not (-90 <= lat <= 90 and -180 <= lon <= 360):
                problems.append(f"{name}: its position reads as {lat}, {lon}, not a place on "
                                f"Earth; set position = [lat, lon]")
            else:
                positions.append((lat, lon))
    if positions and max(max(abs(a[0] - b[0]), abs(a[1] - b[1]))
                         for a in positions for b in positions) > 0.01:
        problems.append("its instruments are 0.01 degree or more apart, and one recipe is one "
                        "mooring: split it into one per mooring")
    if not recipe.get("position") and not positions:
        problems.append("no instrument states its position: set position = [lat, lon]")
    return {"instruments": len(recipe.get("files") or []), "mooring_problems": problems}


def _run_mooring(name: str, recipe: Dict[str, Any], root: Path, out: Path, accession: str,
                 plan: Dict[str, Any]) -> Dict[str, Any]:
    """Stage one cast per day: each instrument's daily mean at its depth (manual pp.14, 17)."""
    import numpy as np

    from wod_etl.staging import mooring_days, mooring_profiles, read_instrument
    from wod_etl.staging.moorings import MIN_DAY_FRACTION, epoch

    problems = check_mooring_recipe(recipe, root)["mooring_problems"]
    if problems:
        raise ValueError("; ".join(problems))
    files = list(recipe.get("files") or [])
    paths = [root / f for f in files]
    instruments = [read_instrument(p) for p in paths]
    if recipe.get("position"):
        lat, lon = (float(x) for x in recipe["position"])
        where = "decisions.toml (position)"
    else:
        lat = float(np.median([i["latitude"] for i in instruments
                               if i.get("latitude") is not None]))
        lon = float(np.median([i["longitude"] for i in instruments
                               if i.get("longitude") is not None]))
        where = "the instruments' files"
    deployed = recipe.get("deployed")
    window = None if deployed == "record" else [epoch(x) for x in deployed]
    days = mooring_days(paths, instruments, window)
    fraction = float(recipe.get("min_day_fraction", MIN_DAY_FRACTION))
    built = mooring_profiles(instruments, days, recipe.get("depths") or {}, lat, fraction,
                             placed=recipe.get("placed"))
    if not built["profiles"]:
        raise ValueError(f"no day of the deployment has an instrument recording for "
                         f"{fraction:.0%} of its hours: there is no daily mean to write")
    # One column per variable, named by its first instrument; units agree, pressure is in dbar.
    columns: Dict[int, str] = {}
    for info in instruments:
        for v in info["variables"]:
            columns.setdefault(v["code"], v["column"])
    codes = sorted(columns)
    header = [recipe.get("depth_column", DEFAULT_DEPTH_COLUMN)] + [columns[c] for c in codes]
    not_stored = sorted({x for i in instruments for x in i.get("not_stored") or []})
    notes = list(recipe.get("notes") or []) + [
        f"Staged by `wod-etl confirm`: mooring {name}, {len(files)} instrument(s).",
        "A moored buoy's profile is its instruments' concurrent readings at their depths "
        "(WOD manual p.17), kept as daily means (p.14): each level is one instrument's mean "
        "over the UTC day, from a day it recorded for at least "
        f"{fraction:.0%} of the hours.",
        f"Position from {where}.",
        (f"Deployed {built['window'][0]} to {built['window'][1]}, "
         + ("from the pressure its instruments record" if recipe.get("deployment") != "record"
            else "its record without its first and last day: none of its instruments records "
                 "pressure to tell when it went in, and a logger may record air on deck")
         + ": samples outside are not used." if built["window"] else
         "Deployed: the whole record. None of its instruments records pressure to tell "
         "when it was in the water, and the review included it as it stands.")]
    notes += [f"{n}: depth {d}." for n, d in sorted((recipe.get("placed") or {}).items())
              if d and recipe["depths"].get(n) != "pressure"]
    notes += [n for i in instruments for n in i.get("notes") or []]
    if not_stored:
        notes.append("Not written (not stored in MRB, Table 3a): " + "; ".join(not_stored)
                     + ".")
    metadata = dict(recipe.get("metadata") or {})
    if not any(k.lower() == "cruise" for k in metadata):
        metadata["cruise"] = name
    mooring = str(next(v for k, v in metadata.items() if k.lower() == "cruise"))
    written, skipped = [], []
    for profile in built["profiles"]:
        rows = [[f"{lv['depth']:.2f}"] + [_fmt(lv["means"].get(c)) for c in codes]
                for lv in profile["levels"]]
        levels = "; ".join(f"{lv['file']} at {lv['depth']:.1f} m ({lv['placed_by']}, "
                           f"{lv['samples']} samples)" for lv in profile["levels"])
        path = out / f"{profile['day']}.csv"
        write_staged_cast(
            path, header, rows, latitude=lat, longitude=lon, date=profile["day"],
            metadata=_metadata(accession, plan.get("metadata") or {},
                               {**recipe, "metadata": metadata}, mooring, mooring),
            notes=notes + [f"Daily means of {len(profile['levels'])} instrument(s): {levels}."],
            sources=[str(p) for p in paths])
        written.append(str(path))
    for gap in built["left_out"]:
        skipped.append({"cast": f"{gap['file']} {gap['day']}",
                        "reason": f"{gap['coverage']:.0%} of the day's hours recorded: not a "
                                  f"daily mean"})
    runlog.event(log, logging.INFO,
                 f"mooring {name}: {len(written)} daily profile(s) from {len(files)} "
                 f"instrument(s); {len(skipped)} instrument-day(s) left out",
                 kind="recipe", recipe=f"profiles.{name}", written=len(written),
                 left_out=len(skipped))
    return {"recipe": name, "written": written, "skipped": skipped}


def _fmt(value) -> str:
    return "" if value is None else f"{value:.6g}"


def _cast_from_file(path: str, recipe: Dict[str, Any]) -> str:
    stem = Path(path).stem
    pattern = recipe.get("station_pattern")
    if pattern:
        m = re.search(pattern, stem)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return stem


def _metadata(accession: str, plan_meta: Dict[str, Any], recipe: Dict[str, Any],
              station: str, cast: str) -> Dict[str, Any]:
    """Header keys for one staged cast or haul; `Cast` is recipe-scoped so ids do not clash."""
    return {"Accession": accession, **plan_meta, "Cast": cast,
            **(recipe.get("metadata") or {}), "Station": station}


def run_profile_recipe(name: str, recipe: Dict[str, Any], data_root, out_dir,
                       accession: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Stage one profile recipe into `out_dir/<name>/`; returns files written and casts skipped."""
    root, out = Path(data_root), Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    kind = recipe.get("kind")
    if kind == "script":
        return _run_script(name, recipe, root, out, accession)
    if kind == "mooring":
        return _run_mooring(name, recipe, root, out, accession, plan)
    if kind == "combine":
        return _run_combine(name, recipe, root, out, accession, plan)
    if kind == "tracks":
        return _run_tracks(name, recipe, root, out, accession, plan)

    stations, sinfo = _station_lookup(root, recipe)
    overrides = {normalise_station(k): v for k, v in (plan.get("station_overrides") or {}).items()}
    mapping = recipe.get("station_map") or {}
    skip = {str(c) for c in recipe.get("skip_casts") or []}
    base_notes = list(recipe.get("notes") or []) + [
        f"Staged by `wod-etl confirm`: recipe {name} (kind = {kind})."]
    sources = [f"{root / f}" for f in recipe.get("files") or []] + [
        str(root / recipe["station_list"])]
    written, skipped = [], []

    if kind == "long":
        cols, rows, info = _pivot(root, recipe)
        cast_key = recipe["cast_key"]
        depth_keys = [k for k in recipe["keys"] if k != cast_key]
        out_cols = ([recipe.get("depth_column", DEFAULT_DEPTH_COLUMN)] + depth_keys[1:]
                    + cols[len(recipe["keys"]):])
        notes = base_notes + [
            f"Pivoted from {len(recipe['files'])} long-format file(s) on {recipe['keys']}: "
            f"variable '{recipe['variable']}', value '{recipe['value']}'. "
            + (f"Column names and units: {recipe['columns']}." if recipe.get("columns") else
               "Columns named as the files name the variables: a unit a name states is "
               "read, and a name with none is read in WOD's standard unit.")]
        if info["conflicts"]:
            notes.append(f"{len(info['conflicts'])} level(s) had two values for one variable; "
                         f"the first was kept (on_conflict = \"first\").")
        idx = {c: i for i, c in enumerate(cols)}
        groups = group_rows(rows, cols, [cast_key])
        width = max((len(k[0]) for k in groups if k and k[0].isdigit()), default=0)
        for key, levels in groups.items():
            if not key:
                continue
            cast = key[0]
            if cast in skip:
                skipped.append({"cast": cast, "reason": "skip_casts in decisions.toml"})
                continue
            entry = stations.get(normalise_station(mapping.get(cast, cast)))
            if entry is None:
                skipped.append({"cast": cast, "reason": f"no entry in "
                                                        f"{recipe['station_list']}: its "
                                                        f"position and date are not known"})
                continue
            entry, override_notes = _apply_override(entry, overrides)
            body = [[r[idx[k]] for k in depth_keys] + r[len(recipe["keys"]):] for r in levels]
            body.sort(key=lambda r: _num(r[0]))
            path = out / f"{_cast_file(cast, width)}.csv"
            write_staged_cast(
                path, out_cols, body, latitude=entry["latitude"], longitude=entry["longitude"],
                date=entry["date"] or "", time=entry.get("time"), time_zone=entry.get("time_zone"),
                metadata=_metadata(accession, plan.get("metadata") or {}, recipe,
                                   entry["station"], f"{name}_{_cast_file(cast, width)}"),
                notes=notes + [f"Position and date from {entry['source']}."] + override_notes,
                sources=sources)
            written.append(str(path))
    elif kind == "per_file":
        renames = recipe.get("columns") or {}
        for f in recipe.get("files") or []:
            cast = _cast_from_file(f, recipe)
            if cast in skip:
                skipped.append({"cast": cast, "reason": "skip_casts in decisions.toml"})
                continue
            entry = stations.get(normalise_station(mapping.get(cast, cast)))
            if entry is None:
                skipped.append({"cast": cast, "reason": f"{f}: no entry in "
                                                        f"{recipe['station_list']}: its "
                                                        f"position and date are not known"})
                continue
            entry, override_notes = _apply_override(entry, overrides)
            cols, rows = _table(root / f)
            cols = [renames.get(c, c) for c in cols]
            path = out / f"{_safe(cast)}.csv"
            write_staged_cast(
                path, cols, rows, latitude=entry["latitude"], longitude=entry["longitude"],
                date=entry["date"] or "", time=entry.get("time"), time_zone=entry.get("time_zone"),
                metadata=_metadata(accession, plan.get("metadata") or {}, recipe,
                                   entry["station"], f"{name}_{_safe(cast)}"),
                notes=base_notes + [f"Position and date from {entry['source']}."]
                      + override_notes,
                sources=[str(root / f), str(root / recipe["station_list"])])
            written.append(str(path))
    elif kind == "by_station":
        for f in recipe.get("files") or []:
            for cast, df in _station_groups(root / f, recipe):
                entry = stations.get(normalise_station(mapping.get(cast, cast)))
                if cast in skip or entry is None:
                    skipped.append({"cast": cast, "reason": "skip_casts in decisions.toml"
                                    if cast in skip else f"{f}: no entry in "
                                    f"{recipe['station_list']}: its position and date are "
                                    f"not known"})
                    continue
                entry, override_notes = _apply_override(entry, overrides)
                cols, rows = _rows(df.drop(columns=[recipe["cast_column"]]))
                path = out / f"{_safe(Path(f).stem)}_{_safe(cast)}.csv"
                write_staged_cast(
                    path, cols, rows, latitude=entry["latitude"], longitude=entry["longitude"],
                    date=entry["date"] or "", time=entry.get("time"),
                    time_zone=entry.get("time_zone"),
                    metadata=_metadata(accession, plan.get("metadata") or {}, recipe,
                                       entry["station"], f"{name}_{_safe(cast)}"),
                    notes=base_notes + [f"Position and date from {entry['source']}."]
                          + override_notes,
                    sources=[str(root / f), str(root / recipe["station_list"])])
                written.append(str(path))
    else:
        raise ValueError(f"unknown profile recipe kind {kind!r}")
    return {"recipe": name, "written": written, "skipped": skipped,
            "dropped_station_list_rows": sinfo.get("dropped")}


def _run_combine(name: str, recipe: Dict[str, Any], root: Path, out: Path, accession: str,
                 plan: Dict[str, Any]) -> Dict[str, Any]:
    from wod_etl.staging.combine import combine_files

    files = recipe.get("files") or []
    casts, info = combine_files([root / f for f in files])
    written = []
    for c in casts:
        cols, rows = _rows(c["df"])
        names = {str(root / f): f for f in files}
        held = "; ".join(f"{names[p]}: {', '.join(cs)}" for p, cs in c["columns_from"].items())
        notes = [f"Staged by `wod-etl confirm`: recipe {name} (kind = combine).",
                 f"Station {c['station']} on {c['date']}, combined from {len(c['sources'])} "
                 f"file(s) reporting the same cast, levels merged by depth: {held}."]
        path = out / f"{_safe(c['station'])}_{c['date']}.csv"
        write_staged_cast(
            path, cols, rows, latitude=c["latitude"], longitude=c["longitude"], date=c["date"],
            time=c.get("time"),
            metadata=_metadata(accession, plan.get("metadata") or {}, recipe, c["station"],
                               f"{name}_{_safe(c['station'])}_{c['date']}"),
            notes=notes, sources=c["sources"])
        written.append(str(path))
    return {"recipe": name, "written": written, "skipped": [],
            "in_one_file_only": info["in_one_file_only"]}


def _run_tracks(name: str, recipe: Dict[str, Any], root: Path, out: Path, accession: str,
                plan: Dict[str, Any]) -> Dict[str, Any]:
    """One staged SUR track per cast: files on one clock merged on time."""
    import pandas as pd

    from wod_etl.staging.tracks import combine_tracks

    files = recipe.get("files") or []
    names = {str(root / f): f for f in files}
    written, skipped = [], []
    for i, track in enumerate(combine_tracks([root / f for f in files]), start=1):
        df = track["df"]
        located = df.dropna(subset=[c for c in ("Latitude", "Longitude") if c in df])
        if not {"Latitude", "Longitude"} <= set(df.columns) or located.empty:
            skipped.append({"cast": f"track {i}", "reason": "no position in any of its files"})
            continue
        first = located.iloc[0]
        stamp = pd.Timestamp(first.iloc[0])
        cols, rows = _rows(df)
        sources = [names.get(s, s) for s in track["sources"]]
        path = out / f"track_{i:02d}.csv"
        write_staged_cast(
            path, cols, rows, latitude=float(first["Latitude"]),
            longitude=float(first["Longitude"]), date=f"{stamp:%Y-%m-%d}",
            time=f"{stamp:%H:%M:%S}", time_zone="UTC" if track["utc"] else None,
            metadata=_metadata(accession, plan.get("metadata") or {}, recipe, "",
                               f"{name}_{i:02d}"),
            notes=[f"Staged by `wod-etl confirm`: recipe {name} (kind = tracks).",
                   f"One SUR cast from {len(sources)} file(s) on one clock, merged on time: "
                   f"{', '.join(sources)}."],
            sources=[str(root / f) for f in sources])
        written.append(str(path))
    return {"recipe": name, "written": written, "skipped": skipped}


def _table(path: Path):
    """(columns, rows) of a cast file, read with `read_any`."""
    from wod_etl.discovery import read_any, sniff_format

    casts, info = read_any(str(path), sniff_format(str(path)))
    if not casts:
        raise ValueError(f"{path.name}: {info.get('error') or 'no table read'}")
    import pandas as pd

    return _rows(pd.concat([c["df"] for c in casts], ignore_index=True))


def _rows(df):
    return [str(c) for c in df.columns], df.astype(object).where(df.notna(), "").values.tolist()


def _station_groups(path: Path, recipe: Dict[str, Any]):
    """[(station, rows of that station)] of one table of many casts, in file order."""
    from wod_etl.discovery import read_any, sniff_format

    casts, info = read_any(str(path), sniff_format(str(path)))
    col = recipe["cast_column"]
    out = []
    for c in casts:
        if col not in c["df"].columns:
            continue
        df = c["df"]
        key = df[col].astype(str).str.strip()
        for station in dict.fromkeys(k for k in key if k):
            out.append((station, df[key == station]))
    return out


def _run_script(name: str, recipe: Dict[str, Any], root: Path, out: Path,
                accession: str) -> Dict[str, Any]:
    script = Path(recipe["script"])
    script = script if script.is_absolute() else Path(config.ROOT) / script
    if not script.exists():
        raise FileNotFoundError(f"script {recipe['script']} does not exist")
    env = {**os.environ, "WOD_STAGING_OUT": str(out), "WOD_ACCESSION": accession,
           "WOD_DATA_ROOT": str(root),
           # Absolute, so the child (cwd ROOT) does not resolve it a second time.
           "WOD_PROJECT_ROOT": str(config.ROOT),
           runlog.TO_STDERR_ENV: "1"}
    # Script output may be in any encoding; never fail on it.
    proc = subprocess.run([sys.executable, str(script)], cwd=str(config.ROOT), env=env,
                          capture_output=True, encoding="utf-8", errors="replace")
    _log_output(recipe["script"], proc)
    if proc.returncode != 0:
        tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-15:]
        raise RuntimeError(f"script {recipe['script']} exited {proc.returncode}:\n"
                           + "\n".join(tail) + "\n(all of its output is in run.log)")
    written = sorted(str(p) for p in out.rglob("*.csv"))
    if not written:
        raise RuntimeError(f"script {recipe['script']} wrote nothing into $WOD_STAGING_OUT "
                           f"({out})")
    return {"recipe": name, "written": written, "skipped": [], "script_output":
            (proc.stdout or "").strip().splitlines()[-20:]}


MAX_LOGGED_LINES = 200


def _log_output(script: str, proc: subprocess.CompletedProcess) -> None:
    """Copy a script's stdout and stderr into this run's log."""
    base = logging.INFO if proc.returncode == 0 else logging.WARNING
    runlog.event(log, base, f"script {script} exited {proc.returncode}", kind="script",
                 script=script, exit_code=proc.returncode)
    for stream, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        lines = (text or "").rstrip().splitlines()
        if len(lines) > MAX_LOGGED_LINES:
            log.log(base, "script %s %s: %d earlier line(s) not kept", script, stream,
                    len(lines) - MAX_LOGGED_LINES)
        for line in lines[-MAX_LOGGED_LINES:]:
            level = next((getattr(logging, w) for w in ("ERROR", "WARNING")
                          if line.startswith(w + " ")), base)
            log.log(level, "script %s %s: %s", script, stream, line)


def _cast_file(cast: str, width: int) -> str:
    """'1' -> '01' when casts run to two digits, so files sort in cast order."""
    return cast.zfill(width) if cast.isdigit() and width else _safe(cast)


def _num(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("inf")


# --------------------------------------------------------------------- taxa
def _taxa_hauls(recipe: Dict[str, Any], root: Path) -> Dict[str, Any]:
    """Group a plankton table's rows into hauls, one per station and date.

    Returns `{"hauls", "skipped", "negative", "records"}`.
    """
    from wod_etl.staging.taxa_layout import is_missing

    path = recipe["files"][0]
    from wod_etl.staging.station_list import _header_row_below_title

    cols, rows, _ = read_table(root / path, delimiter=None,
                               header_row=_header_row_below_title(root / path, ","))
    at = {c: i for i, c in enumerate(cols)}
    roles = recipe["columns"]

    def cell(row, role):
        c = roles.get(role)
        v = row[at[c]].strip() if c in at and at[c] < len(row) else ""
        return "" if is_missing(v) else v

    stations, located_by = {}, None
    if recipe.get("station_list"):
        stations, _ = _station_lookup(root, recipe)
        located_by = recipe["station_list"]
    hauls: Dict[Tuple[str, str], Dict[str, Any]] = {}
    negatives: Dict[str, int] = {}
    for row in rows:
        station, date = cell(row, "station"), cell(row, "date")
        # Normalised like the station list: '014' and '14' are one haul.
        key = (normalise_station(station) if station else "", date)
        haul = hauls.setdefault(key, {"station": station, "date": date, "row": row,
                                      "records": []})
        upper = "0" if roles.get("upper_zero") else cell(row, "upper")
        base = {"Taxon": cell(row, "taxon"), "Upper depth (m)": upper,
                "Lower depth (m)": cell(row, "lower")}
        if roles.get("stage"):
            base["Life stage"] = cell(row, "stage")
        if roles.get("sex"):
            base["Sex"] = cell(row, "sex")
        for m in recipe["measurements"]:
            i = at[m["column"]]
            value = row[i].strip() if i < len(row) else ""
            if is_missing(value):
                continue
            # A negative value is a missing-value code (-999), not a Table 7a smaller-unit sign.
            if _negative(value):
                negatives[m["column"]] = negatives.get(m["column"], 0) + 1
                continue
            if m["negate"]:
                value = f"-{value}"
            if m.get("biomass"):
                haul["records"].append({**base, "Taxon": m["taxon"], "Biomass": value,
                                        "Unit": m["per_unit"]})
                continue
            haul["records"].append({**base, m["column"]: value, "Unit": m["per_unit"]})

    located, skipped = [], []
    for haul in hauls.values():
        station, date = haul["station"], haul["date"]
        label = station or date or "(no station)"
        if not haul["records"]:
            skipped.append({"cast": label, "reason": "no measurement in any of its rows"})
            continue
        if located_by:
            entry = stations.get(normalise_station(station))
            if entry is None:
                skipped.append({"cast": label, "unlocated": True,
                                "reason": f"no entry in {located_by}: its position and date "
                                          f"are not known"})
                continue
            where = {"latitude": entry["latitude"], "longitude": entry["longitude"],
                     "date": date or entry.get("date") or "", "time": entry.get("time"),
                     "time_zone": entry.get("time_zone"), "station": entry["station"],
                     "source": entry["source"]}
        else:
            from wod_etl.staging.parse import parse_coordinate

            lat = parse_coordinate(cell(haul["row"], "latitude"))
            lon = parse_coordinate(cell(haul["row"], "longitude"))
            if lat is None or lon is None or not date:
                skipped.append({"cast": label, "unlocated": True,
                                "reason": "its row states no position or date"})
                continue
            where = {"latitude": lat, "longitude": lon, "date": date,
                     "time": cell(haul["row"], "time") or None, "time_zone": None,
                     "station": station, "source": f"its own columns in {path}"}
        located.append({**haul, "where": where})
    return {"hauls": located, "skipped": skipped, "negative": negatives,
            "records": sum(len(h["records"]) for h in located)}


def _negative(value: str) -> bool:
    try:
        return float(value) < 0
    except ValueError:
        return False


def check_taxa_recipe(recipe: Dict[str, Any], data_root) -> Dict[str, Any]:
    """Dry run of a taxa recipe, for the review: hauls, records, hauls it cannot locate."""
    try:
        built = _taxa_hauls(recipe, Path(data_root))
    except Exception as e:                              # noqa: BLE001 - reported to the person
        return {"error": f"{type(e).__name__}: {e}"}
    from wod_etl.reference import missing_biological_fields

    return {"hauls": len(built["hauls"]), "records": built["records"],
            "casts_without_station": [s["cast"] for s in built["skipped"]
                                      if s.get("unlocated")],
            "hauls_without_measurement": [s["cast"] for s in built["skipped"]
                                          if not s.get("unlocated")],
            "negative_values": built["negative"] or None,
            "missing_biological_fields": missing_biological_fields(
                set(recipe.get("biological_header") or {}))}


def run_taxa_recipe(name: str, recipe: Dict[str, Any], data_root, out_dir, accession: str,
                    plan: Dict[str, Any]) -> Dict[str, Any]:
    """Stage one taxa recipe: one haul file per station and date into `out_dir/<name>/`."""
    root, out = Path(data_root), Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    built = _taxa_hauls(recipe, root)
    overrides = {normalise_station(k): v for k, v in (plan.get("station_overrides") or {}).items()}
    source = recipe["files"][0]
    notes = list(recipe.get("notes") or []) + [
        f"Staged by `wod-etl confirm`: taxa recipe {name}, from {source}.",
        "One Taxa-Record per taxon, depth range and measurement (Table 7a)."]
    for m in recipe["measurements"]:
        if m["negate"]:
            notes.append(f"{m['column']!r} is in {m['unit']}: written negative (Table 7a sign "
                         f"rule).")
    if recipe["columns"].get("upper_zero"):
        notes.append(f"A vertical net tow: each record's depth range is 0 m to the stated "
                     f"{recipe['columns']['lower']!r}.")
    for m in recipe["measurements"]:
        if m.get("biomass"):
            notes.append(f"{m['column']!r} is whole-sample biomass: WOD variable number "
                         f"{m['biomass']} ({m['taxon']}).")
    if any(m["per_unit"] == "sample" for m in recipe["measurements"]):
        notes.append("UNIT is 'sample': no volume filtered or tow distance is stated.")
    for column, n in built["negative"].items():
        notes.append(f"{n} negative value(s) in {column!r} not written: a measurement cannot "
                     f"be negative, so each is read as a missing-value code (e.g. -999).")
    written, missing_bio, stems = [], {}, set()
    for haul in built["hauls"]:
        where = haul["where"]
        entry, override_notes = _apply_override(where, overrides) if where.get("station") \
            else (where, [])
        stem = base = _safe("_".join(p for p in (where.get("station"), where.get("date")) if p))
        n = 2
        while stem in stems:                # two hauls must not write one file
            stem, n = f"{base}_{n}", n + 1
        stems.add(stem)
        path = out / f"{stem}.csv"
        res = write_staged_haul(
            path, haul["records"], latitude=entry["latitude"], longitude=entry["longitude"],
            date=entry["date"] or "", time=entry.get("time"), time_zone=entry.get("time_zone"),
            biological_header=dict(recipe.get("biological_header") or {}),
            metadata=_metadata(accession, plan.get("metadata") or {}, recipe,
                               where.get("station") or "", f"{name}_{stem}"),
            notes=notes + [f"Position and date from {where['source']}."] + override_notes,
            sources=[str(root / source)] + ([str(root / recipe["station_list"])]
                                            if recipe.get("station_list") else []))
        written.append(str(path))
        for m in res.get("missing_biological_fields") or []:
            missing_bio[m] = missing_bio.get(m, 0) + 1
    runlog.event(log, logging.INFO,
                 f"taxa {name}: {len(written)} haul(s), {built['records']} Taxa-Record(s); "
                 f"{len(built['skipped'])} haul(s) left out",
                 kind="recipe", recipe=f"taxa.{name}", written=len(written),
                 left_out=len(built["skipped"]))
    return {"recipe": name, "kind": "taxa", "written": written, "skipped": built["skipped"],
            "missing_biological_fields": missing_bio}
