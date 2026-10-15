"""Run the staging recipes a person reviewed in decisions.toml.

An agent stages an accession by composing the staging primitives in a script; a person
running `wod-etl` describes the same steps in decisions.toml instead, and `wod-etl confirm`
runs them.
It is deliberately small -- three profile shapes and two taxa shapes, the ones accessions
keep arriving in -- with a `script` kind as the way out for anything else:

    profiles  kind = "long"      long-format files -> pivot_long -> one cast per station,
                                 located by a station list
              kind = "per_file"  one file per cast, located by a station list
              kind = "script"    run scripts/<name>.py, which writes staged casts into
                                 $WOD_STAGING_OUT -- for pressure-to-depth, re-occupied
                                 stations, layouts no recipe describes
    taxa      kind = "matrix"    taxa down, samples across -> melt_matrix -> one haul per
                                 sample
              kind = "long"      one row per taxon per station -> one haul per station

Every value a recipe adds that the source files do not state -- a position from the station
list, a unit named in decisions.toml, a conflict settled by the person -- is written into the
staged file's notes, and from there into the NetCDF's `comment`.
"""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from wod_etl import config, runlog
from wod_etl.staging import (group_rows, join_station_list, melt_matrix, normalise_station,
                             pivot_long, read_station_list, read_table, write_staged_cast,
                             write_staged_haul)

log = logging.getLogger(__name__)

DEFAULT_DEPTH_COLUMN = "Depth (m)"


def _safe(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "x"


def _station_lookup(root: Path, recipe: Dict[str, Any]):
    stations, info = read_station_list(root / recipe["station_list"],
                                       time_zone=recipe.get("time_zone"))
    if info.get("error"):
        raise ValueError(f"station list {recipe['station_list']}: {info['error']}")
    return stations, info


def _apply_override(entry: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]):
    """A station-list entry with any settled conflict applied, and the notes saying so."""
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
    """Run a recipe without writing: which casts the station list does not locate, and how
    many levels carry two values. Read by decisions.validate, so the person sees it in the
    same pause as everything else."""
    root = Path(data_root)
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
    """Header keys for one staged cast or haul. `Cast` is scoped by the recipe: a CTD cast
    and a bottle cast at one station on one day are two casts, and must not share an id."""
    return {"Accession": accession, **plan_meta, "Cast": cast,
            **(recipe.get("metadata") or {}), "Station": station}


def run_profile_recipe(name: str, recipe: Dict[str, Any], data_root, out_dir,
                       accession: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Stage one profile recipe into `out_dir/<name>/`. Returns written, skipped,
    and the notes recorded."""
    root, out = Path(data_root), Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    kind = recipe.get("kind")
    if kind == "script":
        return _run_script(name, recipe, root, out, accession)

    stations, sinfo = _station_lookup(root, recipe)
    overrides = {normalise_station(k): v for k, v in (plan.get("station_overrides") or {}).items()}
    mapping = recipe.get("station_map") or {}
    skip = {str(c) for c in recipe.get("skip_casts") or []}
    base_notes = list(recipe.get("notes") or []) + [
        f"Staged by `wod-etl confirm` from decisions.toml [profiles.{name}] (kind = {kind})."]
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
            f"variable '{recipe['variable']}', value '{recipe['value']}'. Column names and "
            f"units as stated in decisions.toml: {recipe.get('columns')}."]
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
                raise ValueError(f"cast {cast!r} has no entry in {recipe['station_list']}")
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
                raise ValueError(f"{f}: cast {cast!r} has no entry in {recipe['station_list']}")
            entry, override_notes = _apply_override(entry, overrides)
            cols, rows, _ = read_table(root / f, header_row=int(recipe.get("header_row", 0)),
                                       delimiter=None)
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
    else:
        raise ValueError(f"unknown profile recipe kind {kind!r}")
    return {"recipe": name, "written": written, "skipped": skipped,
            "dropped_station_list_rows": sinfo.get("dropped")}


def _run_script(name: str, recipe: Dict[str, Any], root: Path, out: Path,
                accession: str) -> Dict[str, Any]:
    script = Path(recipe["script"])
    script = script if script.is_absolute() else Path(config.ROOT) / script
    if not script.exists():
        raise FileNotFoundError(f"script {recipe['script']} does not exist")
    env = {**os.environ, "WOD_STAGING_OUT": str(out), "WOD_ACCESSION": accession,
           "WOD_DATA_ROOT": str(root),
           # Resolved here: a relative WOD_PROJECT_ROOT would resolve again from the
           # child's cwd, which is ROOT, and find a workspace inside this one.
           "WOD_PROJECT_ROOT": str(config.ROOT),
           runlog.TO_STDERR_ENV: "1"}
    # A script may print anything, in any encoding; that must not fail a script that worked.
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
    """What the script printed, and the log lines its wod_etl calls sent to stderr, into
    this run's log -- whether it worked or not."""
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
    """'1' -> '01' when casts run to two digits, so files sort in cast order. The recipe's
    folder already says which recipe they came from."""
    return cast.zfill(width) if cast.isdigit() and width else _safe(cast)


def _num(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("inf")


# --------------------------------------------------------------------- taxa
def run_taxa_recipe(name: str, recipe: Dict[str, Any], data_root, out_dir, accession: str,
                    plan: Dict[str, Any]) -> Dict[str, Any]:
    """Stage one taxa recipe: one haul file per sample into `out_dir/<name>/`. (The pipeline
    leaves plankton out for now; this stays for an agent's staging script.)"""
    root, out = Path(data_root), Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    stations, _ = _station_lookup(root, recipe)
    overrides = {normalise_station(k): v for k, v in (plan.get("station_overrides") or {}).items()}
    missing = {str(v).lower() for v in recipe.get("missing_values") or ["na", ""]}
    unit = recipe.get("unit")
    base_notes = list(recipe.get("notes") or []) + [
        f"Staged from decisions.toml [taxa.{name}] (kind = "
        f"{recipe.get('kind')}).",
        f"Counts are '{unit}' as stated in decisions.toml." if unit else
        "No unit stated for the counts."]
    hauls: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}

    if recipe.get("kind") == "matrix":
        cols, rows, _ = read_table(root / recipe["file"])
        cells_cols, cells, minfo = melt_matrix(
            [cols] + rows, header_rows=int(recipe["header_rows"]),
            label_columns=list(recipe["label_columns"]),
            header_names=list(recipe["header_names"]),
            fill_down=list(recipe.get("fill_down") or []),
            blank=recipe.get("blank", "absent"))
        at = {c: i for i, c in enumerate(cells_cols)}
        for r in cells:
            sample = tuple(r[at[h]] for h in recipe["header_names"])
            hauls.setdefault(sample, []).append({
                "Taxon": r[at[recipe["taxon_column"]]],
                "Life stage": (r[at[recipe["stage_column"]]] if recipe.get("stage_column")
                               else ""),
                "Count": r[at["value"]], "Unit": unit})
        base_notes.append(f"Melted from a count matrix: {minfo['cells_written']} cell(s) "
                          f"written, {minfo['blank_cells']} blank cell(s) read as "
                          f"'{recipe.get('blank', 'absent')}'.")
        station_header = recipe.get("cast_header", recipe["header_names"][0])
        station_pos = list(recipe["header_names"]).index(station_header)
    elif recipe.get("kind") == "long":
        cols, rows, _ = read_table(root / recipe["file"])
        at = {c: i for i, c in enumerate(cols)}
        clean = lambda v: "" if str(v).strip().lower() in missing else str(v).strip()
        for r in rows:
            cell = lambda c: clean(r[at[c]]) if c in at and at[c] < len(r) else ""
            name_parts = [cell(c) for c in recipe.get("taxon_columns") or []]
            taxon = " ".join(p for p in name_parts if p) or cell(
                recipe.get("fallback_taxon_column", ""))
            hauls.setdefault((cell(recipe["cast_key"]),), []).append({
                "Taxon": taxon, "Life stage": cell(recipe.get("stage_column", "")),
                "Count": cell(recipe["count_column"]), "Unit": unit})
        station_pos = 0
    else:
        raise ValueError(f"unknown taxa recipe kind {recipe.get('kind')!r}")

    written, missing_bio = [], {}
    for sample, records in hauls.items():
        station = sample[station_pos]
        entry = stations.get(normalise_station(station))
        if entry is None:
            raise ValueError(f"haul station {station!r} has no entry in "
                             f"{recipe['station_list']}")
        entry, override_notes = _apply_override(entry, overrides)
        others = [v for i, v in enumerate(sample) if i != station_pos]
        bio = dict(recipe.get("biological_header") or {})
        for v in others:
            bio.update((recipe.get("sample_biological") or {}).get(v) or {})
        path = out / (f"{_safe(station)}" + "".join(f"_{_safe(v)}" for v in others) + ".csv")
        res = write_staged_haul(
            path, records, latitude=entry["latitude"], longitude=entry["longitude"],
            date=entry["date"] or "", time=entry.get("time"), time_zone=entry.get("time_zone"),
            upper_depth=recipe.get("upper_depth"), lower_depth=recipe.get("lower_depth"),
            biological_header=bio,
            metadata=_metadata(accession, plan.get("metadata") or {}, recipe,
                               entry["station"], f"{name}_{path.stem}"),
            notes=base_notes + [f"Position and date from {entry['source']}."] + override_notes,
            sources=[str(root / recipe["file"]), str(root / recipe["station_list"])])
        written.append(str(path))
        for m in res.get("missing_biological_fields") or []:
            missing_bio[m] = missing_bio.get(m, 0) + 1
    return {"recipe": name, "written": written, "missing_biological_fields": missing_bio}
