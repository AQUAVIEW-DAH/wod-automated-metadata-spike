"""The decisions file: every judgement a person makes about one accession, in one place.

`wod-etl run` does everything that needs no judgement on its own. The rest -- which units a
long-format table is in, which station list locates its casts, what an unidentified file is,
which of two sources has a station's date right -- it writes into
`data/runs/<accession>/decisions.toml` as proposals, and pauses. The person edits the file and
reruns; the run continues from where it stopped.

The file is the person's, not the script's:
- it is written once, and afterwards only APPENDED to -- new proposals go in a marked block at
  the end, and nothing the person typed (comments included) is ever rewritten;
- every item has a stable id (a file's path, `date:6`, `outlying_step:10->11`), so an item is
  never proposed twice;
- an acknowledged conflict carries the hash of the values it acknowledged: if the values
  change, it is open again.

Sections:

    accession = "0303928"
    [metadata]                 # written into every staged cast and haul
    [profiles.<name>]          # a staging recipe for casts: kind = long | per_file | script
    [taxa.<name>]              # a staging recipe for plankton hauls: kind = matrix | long
    [[cross_check]]            # an extra source to check station positions and dates against
    [track]                    # bounding box and outlier factor for the cruise-track check
    [conflicts."<id>"]         # a disagreement between sources, and how it was settled
    [files."<path>"]           # what to do with one file: action = process | skip | taxa
"""
import fnmatch
import hashlib
import json
import os.path
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wod_etl.reference import wod_datasets

PLACEHOLDER = "?"
UNIT_PLACEHOLDER = "UNIT?"


# ----------------------------------------------------------------- TOML writing
def _toml_string(s: str, literal: bool = False) -> str:
    if literal and "'" not in s and "\n" not in s:
        return f"'{s}'"
    out = (s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
           .replace("\t", "\\t").replace("\r", "\\r"))
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
                 lambda m: f"\\u{ord(m.group(0)):04x}", out)
    return f'"{out}"'


def toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_string(key)


def toml_value(v: Any, literal: bool = False) -> str:
    """One TOML value. Regexes and paths are written as literal strings when `literal`."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        r = repr(v)
        return r if any(c in r for c in ".einf") else f"{r}.0"
    if isinstance(v, str):
        return _toml_string(v, literal)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x, literal) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{toml_key(str(k))} = {toml_value(x, literal)}"
                                for k, x in v.items() if x is not None) + " }"
    raise TypeError(f"cannot write {type(v).__name__} as TOML")


_LITERAL_KEYS = {"row_pattern", "station_pattern", "script"}


def section(header: str, fields: Dict[str, Any], comments: Optional[List[str]] = None,
            commented_fields: Optional[Dict[str, Any]] = None) -> str:
    """A `[header]` table: comments, then `key = value` lines, then commented-out examples."""
    lines = [f"# {c}" if c else "#" for c in (comments or [])]
    lines.append(f"[{header}]")
    for k, v in fields.items():
        if v is None:
            continue
        lines.append(f"{toml_key(k)} = {toml_value(v, literal=k in _LITERAL_KEYS)}")
    for k, v in (commented_fields or {}).items():
        lines.append(f"# {toml_key(k)} = {toml_value(v, literal=k in _LITERAL_KEYS)}")
    return "\n".join(lines) + "\n"


def item_header(kind: str, item_id: str) -> str:
    """`[profiles.ctd]`, `[conflicts."date:6"]` -- the table header for an item id."""
    return f"{kind}.{toml_key(item_id)}"


# ----------------------------------------------------------------- reading
def load(path) -> Tuple[Dict[str, Any], Optional[str]]:
    """(decisions, error). A missing file is empty decisions; a malformed one is an error
    naming the line, because a typo must stop the run rather than be read as 'no decision'."""
    p = Path(path)
    if not p.exists():
        return {}, None
    try:
        return tomllib.loads(p.read_text(encoding="utf-8")), None
    except tomllib.TOMLDecodeError as e:
        return {}, f"{p.name} is not valid TOML: {e}"


def has_item(decisions: Dict[str, Any], kind: str, item_id: str) -> bool:
    return item_id in (decisions.get(kind) or {})


def file_decision(decisions: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    """The `[files]` entry that applies to `path`: its own key, else the most specific
    pattern that matches it (`data/0-data/par/*.txt`). One pattern settles thirty files."""
    entries = decisions.get("files") or {}
    if path in entries:
        return entries[path]
    patterns = [k for k in entries if any(c in k for c in "*?[") and fnmatch.fnmatchcase(path, k)]
    return entries[max(patterns, key=len)] if patterns else None


def section_hash(obj: Any) -> str:
    """A short, order-independent hash of a decisions section, for stage fingerprints."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def values_hash(values: Dict[str, Any]) -> str:
    return section_hash(values)[:10]


# ----------------------------------------------------------------- writing
FILE_HEADER = '''# Decisions for accession {accession}.
#
# `wod-etl run` writes proposals here and pauses whenever a judgement is needed. Edit the
# items it asks about, then rerun:   uv run wod-etl run {accession}
#
# Nothing you type is overwritten: new proposals are appended at the end. Delete an item
# only if you want it proposed again. `wod-etl status {accession}` lists what is still open.

accession = {accession_toml}

# Written into the header of every staged cast and haul, e.g.
#   Platform = "Shunyo-Maru"     Cruise = "SHU1505"     Instrument = "SBE 911plus"
[metadata]
'''


def append(path, sections: List[str], accession: str, label: str) -> int:
    """Create the file, or append `sections` in a marked block. Returns how many were added."""
    p = Path(path)
    if not sections and p.exists():
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        text = FILE_HEADER.format(accession=accession, accession_toml=toml_value(accession))
        text += "\n" + "\n".join(sections)
        p.write_text(text, encoding="utf-8")
        return len(sections)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"\n# ---- added by {label} ----\n\n" + "\n".join(sections))
    return len(sections)


# ----------------------------------------------------------------- proposals
def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return s or "recipe"


def _depth_like(col: str) -> bool:
    return bool(re.match(r"^(depth|dep|z|pres|press|pressure|prdm|depsm)", col.strip(), re.I))


def propose(triage: Dict[str, Any], decisions: Dict[str, Any]) -> List[str]:
    """New sections for everything triage found that the decisions file does not cover yet."""
    out: List[str] = []
    files = {f["path"]: f for f in triage.get("files", [])}
    sidecars_by_folder: Dict[str, List[str]] = {}
    for f in files.values():
        if f["action"] == "sidecar":
            sidecars_by_folder.setdefault(str(Path(f["path"]).parent), []).append(f["path"])
    covered = {fp for r in (decisions.get("profiles") or {}).values()
               for fp in (r.get("files") or [])}
    covered |= {r.get("file") for r in (decisions.get("taxa") or {}).values()}

    # Long-format files, grouped by folder and by their variable/value columns.
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for f in files.values():
        lf = f.get("long_format")
        if f["action"] != "stage" or not lf or f["path"] in covered:
            continue
        key = (str(Path(f["path"]).parent), lf["variable_column"], lf["value_column"])
        groups.setdefault(key, []).append(f)
    for (folder, var, val), members in groups.items():
        # Named for what the files share ('U2015-015_CTD_Temp', '_Salinity' -> u2015_015_ctd),
        # falling back to the folder.
        prefix = os.path.commonprefix([Path(m["path"]).stem for m in members]).rstrip("_-. ")
        name = _slug(prefix if len(prefix) >= 3 else
                     (Path(folder).name if folder not in (".", "") else "profiles"))
        n = 2
        while has_item(decisions, "profiles", name):
            name, n = f"{name}_{n}", n + 1
        key_sets = [m["long_format"]["key_columns"] for m in members]
        canonical = max(key_sets, key=lambda k: key_sets.count(k))
        aliases: Dict[str, List[str]] = {}
        for ks in key_sets:
            for k, c in zip(ks, canonical):
                if k != c and k not in aliases.setdefault(c, []):
                    aliases[c].append(k)
        cast_key = next((k for k in canonical if not _depth_like(k)), canonical[0])
        variables: List[str] = []
        for m in members:
            for v in m["long_format"].get("variables_sampled") or []:
                if v not in variables:
                    variables.append(v)
        sidecars = sidecars_by_folder.get(folder, [])
        out.append(section(
            item_header("profiles", name),
            {"kind": "long", "confirmed": False,
             "files": [m["path"] for m in members],
             "keys": canonical,
             "key_aliases": {k: v for k, v in aliases.items() if v} or None,
             "cast_key": cast_key,
             "variable": var, "value": val,
             "columns": {v: f"{v} ({UNIT_PLACEHOLDER})" for v in variables},
             "station_list": sidecars[0] if len(sidecars) == 1 else PLACEHOLDER},
            comments=[f"PROPOSED: {len(members)} long-format file(s) in {folder}/ name the "
                      f"variable in the '{var}' column. They are pivoted into one cast per "
                      f"'{cast_key}' and located by the station list.",
                      "TO DO: replace every UNIT? with the unit the readme or ISO metadata "
                      "states (unit detection reads only these names), check station_list, "
                      "then set confirmed = true.",
                      "Optional: dataset_type = \"CTD\", time_zone = \"UTC+10\", "
                      "notes = [...], on_conflict = \"first\", skip_casts = [...], "
                      "station_map = { \"<cast>\" = \"<station>\" }"]
                     + ([f"More than one station list here: {sidecars}; pick one."]
                        if len(sidecars) > 1 else
                        ["No station list found beside these files; name the file that "
                         "locates the casts."] if not sidecars else [])))

    # Files nothing identifies: the person says what they are. Files that look alike -- one
    # folder, one extension, one reason -- are ONE question, asked with a pattern: thirty
    # daily .DAT files are not thirty decisions.
    undecided: Dict[Tuple[str, str, str], List[str]] = {}
    for f in files.values():
        if f["action"] == "inspect" and file_decision(decisions, f["path"]) is None \
                and f["path"] not in covered:
            p = Path(f["path"])
            undecided.setdefault((str(p.parent), p.suffix, f["reason"]), []).append(f["path"])
    for (folder, suffix, reason), paths in undecided.items():
        key = paths[0] if len(paths) == 1 else (f"{folder}/*{suffix}" if folder != "." else
                                                f"*{suffix}")
        out.append(section(
            item_header("files", key), {"action": ""},
            comments=[f"OPEN: {reason}"]
                     + ([f"Applies to {len(paths)} files, e.g. {Path(paths[0]).name} ... "
                         f"{Path(paths[-1]).name}. Add a [files.\"<path>\"] item for any "
                         f"that differ."] if len(paths) > 1 else [])
                     + ["Set action = \"skip\", or action = \"process\" with process_as = "
                        "one of " + ", ".join(sorted(wod_datasets())) + "."]))

    # Plankton and the ambiguous biological sheets: never blocking.
    for f in files.values():
        if f["path"] in covered or f["action"] != "exclude":
            continue
        if f.get("belongs_in_wod") is True and not any(
                (r or {}).get("file") == f["path"] for r in (decisions.get("taxa") or {}).values()):
            name = _slug(Path(f["path"]).stem)
            if has_item(decisions, "taxa", name):
                continue
            cols = f.get("columns") or []
            layout = "long" if any(re.search(r"species|taxon|genus", str(c), re.I)
                                   for c in cols) else "matrix"
            out.append(section(
                item_header("taxa", name),
                {"kind": layout, "confirmed": False, "file": f["path"],
                 "station_list": PLACEHOLDER, "unit": PLACEHOLDER,
                 "upper_depth": PLACEHOLDER, "lower_depth": PLACEHOLDER,
                 "biological_header": {"Gear code": PLACEHOLDER, "Type of tow": PLACEHOLDER}},
                comments=[f"PENDING (does not block profiles): {f['path']} is "
                          f"{f['exclusion']}. It belongs in WOD as Taxa-Records.",
                          "To write it, fill in the layout below and set confirmed = true. "
                          "See wod_etl/staging/README.md (write_staged_haul) for fields."]
                         + (["matrix: header_rows, label_columns, header_names, taxon_column, "
                             "stage_column, cast_header, sample_biological = { \"<sample "
                             "header>\" = { \"Mesh size\" = 100 } }"] if layout == "matrix" else
                            ["long: cast_key, taxon_columns = [\"genus\", \"species\"], "
                             "fallback_taxon_column, stage_column, count_column"])))
        elif f.get("belongs_in_wod") is None and not has_item(decisions, "files", f["path"]):
            out.append(section(
                item_header("files", f["path"]), {"action": ""},
                comments=[f"PENDING (does not block): {f['exclusion']}.",
                          "Set action = \"taxa\" and add a [taxa.<name>] recipe if it is "
                          "plankton, or action = \"skip\" if it is fish catch."]))

    # The accession's own bounding box, for the track check.
    if "track" not in decisions:
        box = _bounding_box(triage)
        out.append(section(
            "track", {"bounding_box": box, "outlier_factor": 1.75},
            comments=["The cruise-track check flags a station outside the box, a hop between "
                      "stations over outlier_factor x the median hop, and time running "
                      "backwards. [lon_min, lon_max, lat_min, lat_max]"
                      + (" (from NCEI's _lonlat.txt)" if box else "")],
            commented_fields={} if box else {"bounding_box": [0.0, 0.0, 0.0, 0.0]}))

    if not decisions.get("cross_check"):
        example_sources = [f["path"] for f in files.values()
                           if f["action"] == "exclude" and f.get("columns")
                           and any(re.match(r"^lat", str(c), re.I) for c in f["columns"])]
        pdfs = [f["path"] for f in files.values() if f["format"] == "pdf"]
        if example_sources or pdfs:
            lines = ["# Other sources to check station positions and dates against. Uncomment "
                     "and adjust; each is compared with the station list of every recipe.",
                     "# Candidates found: " + ", ".join(example_sources + pdfs)]
            if example_sources:
                lines += ["# [[cross_check]]", f"# name = {toml_value(example_sources[0])}",
                          f"# file = {toml_value(example_sources[0])}",
                          "# tolerance_km = 10", "# compare = [\"position\", \"date\"]"]
            if pdfs:
                lines += ["# [[cross_check]]", "# name = \"report Table 1\"",
                          f"# file = {toml_value(pdfs[0])}", "# page = 7",
                          "# row_pattern = '(\\d+) (Nov\\. \\d{2}, \\d{4}) (\\S+E) (\\S+N)'",
                          "# columns = [\"Station\", \"Date\", \"Long\", \"Lat\"]",
                          "# tolerance_km = 2"]
            out.append("\n".join(lines) + "\n")
    return out


def _bounding_box(triage: Dict[str, Any]) -> Optional[List[float]]:
    lonlat = next((f for f in triage.get("files", [])
                   if re.search(r"_lonlat\.txt$", f["path"])), None)
    if lonlat is None or not triage.get("root"):
        return None
    try:
        pts = []
        for line in (Path(triage["root"]) / lonlat["path"]).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
    except (OSError, ValueError):
        return None
    if not pts:
        return None
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    return [min(lons), max(lons), min(lats), max(lats)]


# ----------------------------------------------------------------- validation
def open_item(item_id: str, what: str, how: str, blocking: bool = True,
              key: Optional[str] = None) -> Dict[str, Any]:
    return {"id": item_id, "what": what, "how_to_fix": how, "blocking": blocking,
            "decisions_key": key or item_id}


def validate(decisions: Dict[str, Any], triage: Dict[str, Any],
             recipe_checks: Dict[str, Dict[str, Any]],
             conflicts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(open items, plan). The plan is only usable when no open item is blocking."""
    items: List[Dict[str, Any]] = []
    datasets = set(wod_datasets())
    files = {f["path"]: f for f in triage.get("files", [])}
    # A file a recipe stages is settled by the recipe -- confirmed or not; an unconfirmed
    # recipe is its own open item.
    in_recipe = {fp for r in (decisions.get("profiles") or {}).values()
                 for fp in (r.get("files") or [])}
    in_recipe |= {r.get("file") for r in (decisions.get("taxa") or {}).values()}

    for name, r in (decisions.get("profiles") or {}).items():
        hdr = item_header("profiles", name)
        if r.get("kind") not in ("long", "per_file", "script"):
            items.append(open_item(hdr, f"profiles '{name}' has kind = {r.get('kind')!r}",
                                   "set kind = \"long\", \"per_file\" or \"script\""))
            continue
        placeholders = [k for k, v in (r.get("columns") or {}).items()
                        if PLACEHOLDER in str(v)]
        if placeholders:
            items.append(open_item(hdr, f"profiles '{name}': the unit of "
                                        f"{', '.join(placeholders)} is not stated",
                                   "replace UNIT? in `columns` with the unit, e.g. "
                                   "\"Temperature (deg C)\""))
        if r.get("kind") != "script" and str(r.get("station_list", "")).strip() in ("", PLACEHOLDER):
            items.append(open_item(hdr, f"profiles '{name}' names no station list",
                                   "set station_list to the file that locates the casts"))
        if r.get("dataset_type") and r["dataset_type"] not in datasets:
            items.append(open_item(hdr, f"profiles '{name}': dataset_type "
                                        f"{r['dataset_type']!r} is not a WOD dataset",
                                   "use one of " + ", ".join(sorted(datasets))))
        check = recipe_checks.get(name) or {}
        if check.get("error"):
            items.append(open_item(hdr, f"profiles '{name}' cannot be read: {check['error']}",
                                   "check files, keys, variable and value against the files"))
        unmatched = [c for c in check.get("casts_without_station") or []
                     if c not in (r.get("skip_casts") or [])
                     and c not in (r.get("station_map") or {})]
        if unmatched:
            items.append(open_item(hdr, f"profiles '{name}': cast(s) {unmatched} have no "
                                        f"entry in {r.get('station_list')}",
                                   "map each to a station with station_map = { \"<cast>\" = "
                                   "\"<station>\" }, or list it in skip_casts"))
        if check.get("pivot_conflicts") and r.get("on_conflict") != "first":
            items.append(open_item(hdr, f"profiles '{name}': {check['pivot_conflicts']} "
                                        f"level(s) carry two different values for one "
                                        f"variable",
                                   "find out which is right; on_conflict = \"first\" keeps "
                                   "the first and records the rest in the staged notes"))
        if not r.get("confirmed"):
            items.append(open_item(hdr, f"profiles '{name}' is not confirmed",
                                   "review it, then set confirmed = true"))

    for path, entry in (decisions.get("files") or {}).items():
        action = str(entry.get("action", "")).strip()
        is_pattern = any(c in path for c in "*?[")
        matched = ([g for p, g in files.items() if fnmatch.fnmatchcase(p, path)] if is_pattern
                   else [files[path]] if path in files else [])
        f = matched[0] if len(matched) == 1 else None
        hdr = item_header("files", path)
        if matched and all(m["path"] in in_recipe for m in matched):
            continue                    # every file it asks about is now a recipe's
        required = any(m["action"] == "inspect" and m["path"] not in in_recipe
                       for m in matched)
        if not action:
            if required:
                what = (f"{path}: nothing identifies it ({matched[0]['reason']})"
                        if not is_pattern else
                        f"{path} ({len(matched)} files): nothing identifies them "
                        f"({matched[0]['reason']})")
                items.append(open_item(hdr, what, "set action = \"skip\", or \"process\" "
                                                  "with process_as"))
            else:
                items.append(open_item(hdr, f"{path}: unanswered question", "set action",
                                       blocking=False))
        elif action not in ("skip", "process", "taxa", "exclude"):
            items.append(open_item(hdr, f"{path}: action = {action!r} is not understood",
                                   "use \"skip\", \"process\", \"taxa\" or \"exclude\""))
        elif action == "process":
            pa = entry.get("process_as")
            if pa and pa not in datasets:
                items.append(open_item(hdr, f"{path}: process_as = {pa!r} is not a WOD "
                                            f"dataset",
                                       "set process_as to one of " + ", ".join(sorted(datasets))))
            elif not pa and not all(m.get("dataset_type") for m in matched or [{}]):
                items.append(open_item(hdr, f"{path}: nothing names its instrument",
                                       "set process_as to one of " + ", ".join(sorted(datasets))))
    for f in files.values():
        if f["action"] == "inspect" and file_decision(decisions, f["path"]) is None \
                and f["path"] not in in_recipe:
            items.append(open_item(item_header("files", f["path"]),
                                   f"{f['path']}: nothing identifies it", "run again to get "
                                   "the proposal, then set action"))

    for c in conflicts:
        entry = (decisions.get("conflicts") or {}).get(c["id"])
        hdr = item_header("conflicts", c["id"])
        if entry is None:
            items.append(open_item(hdr, c["what"], "set resolution in the proposal"))
            continue
        resolution = entry.get("resolution")
        if resolution in (None, "", PLACEHOLDER):
            items.append(open_item(hdr, c["what"], c["how_to_fix"]))
        elif entry.get("values_hash") and entry["values_hash"] != c["values_hash"]:
            items.append(open_item(hdr, f"{c['what']} -- the values changed since it was "
                                        f"settled", f"review it and set values_hash = "
                                                    f"\"{c['values_hash']}\""))
        elif isinstance(resolution, str) and resolution not in ("keep", "ack") and \
                resolution not in c["values"]:
            items.append(open_item(hdr, f"{c['id']}: resolution {resolution!r} is neither "
                                        f"keep, ack, a source name, nor a table of values",
                                   c["how_to_fix"]))

    for name, r in (decisions.get("taxa") or {}).items():
        if not r.get("confirmed"):
            items.append(open_item(item_header("taxa", name),
                                   f"taxa '{name}' ({r.get('file')}) is not confirmed: not "
                                   f"written", "fill it in and set confirmed = true",
                                   blocking=False))
    return items, build_plan(decisions, triage, conflicts)


def build_plan(decisions: Dict[str, Any], triage: Dict[str, Any],
               conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the stage, process and taxa steps will do, resolved from triage and decisions."""
    files = {f["path"]: f for f in triage.get("files", [])}
    profiles = {n: r for n, r in (decisions.get("profiles") or {}).items() if r.get("confirmed")}
    taxa = {n: r for n, r in (decisions.get("taxa") or {}).items() if r.get("confirmed")}
    consumed = set()
    for r in list(profiles.values()) + list(taxa.values()):
        consumed |= set(r.get("files") or []) | ({r["file"]} if r.get("file") else set())
        if r.get("station_list") and r["station_list"] != PLACEHOLDER:
            consumed.add(r["station_list"])
    direct, overrides, skipped = [], {}, []
    for path, f in files.items():
        entry = file_decision(decisions, path) or {}
        act = str(entry.get("action", "")).strip() or None
        if path in consumed:
            continue
        if act in ("skip", "exclude", "taxa"):
            skipped.append({"key": path, "reason": f"decisions.toml: action = {act!r}"})
            continue
        if f["action"] == "process" or act == "process":
            direct.append(path)
            if entry.get("process_as"):
                overrides[path] = entry["process_as"]
    overrides_by_station = {}
    for c in conflicts:
        entry = (decisions.get("conflicts") or {}).get(c["id"]) or {}
        res = entry.get("resolution")
        station = c.get("station")
        note = f"{c['id']}: {c['what']} -- settled as {res!r}" + (
            f" ({entry['note']})" if entry.get("note") else "")
        o = overrides_by_station.setdefault(station, {"notes": []}) if station else None
        if o is None:
            continue
        o["notes"].append(note)
        if isinstance(res, dict):
            o.update({k: v for k, v in res.items() if k in ("latitude", "longitude", "date",
                                                             "time")})
        elif isinstance(res, str) and res in c.get("values", {}) and c.get("field"):
            picked = c["values"][res]
            if c["field"] == "date":
                o["date"] = picked
            elif c["field"] == "position" and isinstance(picked, list) and len(picked) == 2:
                o["latitude"], o["longitude"] = picked
    return {"direct": direct, "dataset_overrides": overrides, "skipped": skipped,
            "profiles": profiles, "taxa": taxa, "consumed": sorted(consumed),
            "metadata": decisions.get("metadata") or {},
            "station_overrides": {k: v for k, v in overrides_by_station.items()
                                  if v.get("notes")}}


# ----------------------------------------------------------------- cross-checks
def crosscheck(decisions: Dict[str, Any], data_root) -> Tuple[List[Dict[str, Any]],
                                                             List[str]]:
    """(conflicts, warnings): every station list a profile recipe uses, checked along its
    track and against each `[[cross_check]]` source.

    Each conflict has a stable `id`, the `station`, the `field`, what each source says
    (`values`), its `values_hash`, and a `how_to_fix`. Unreadable sources are warnings: a
    missing check is reported, never a reason to stop.
    """
    from wod_etl.staging import (check_track, extract_pdf_rows, read_station_list,
                                 reconcile_stations)

    root = Path(data_root)
    conflicts: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    track = decisions.get("track") or {}
    box = track.get("bounding_box")
    box = tuple(box) if isinstance(box, list) and len(box) == 4 and any(box) else None

    def load_source(entry) -> Optional[Dict[str, Any]]:
        path = root / entry["file"]
        if entry.get("page"):
            cols, rows, info = extract_pdf_rows(path, int(entry["page"]),
                                                entry.get("row_pattern", ""),
                                                list(entry.get("columns") or []))
            if info.get("error") or not rows:
                warnings.append(f"cross_check {entry.get('name')!r}: "
                                f"{info.get('error') or 'the row pattern matched nothing'}")
                return None
            stations, _ = read_station_list((cols, rows))
        else:
            stations, info = read_station_list(path)
            if info.get("error"):
                warnings.append(f"cross_check {entry.get('name')!r}: {info['error']}")
                return None
        return stations

    lists = sorted({r["station_list"] for r in (decisions.get("profiles") or {}).values()
                    if r.get("station_list") not in (None, "", PLACEHOLDER)})
    for sl in lists:
        stations, info = read_station_list(root / sl)
        if info.get("error"):
            warnings.append(f"station list {sl}: {info['error']}")
            continue
        for flag in check_track(stations, outlier_factor=float(track.get("outlier_factor", 1.75)),
                                bounding_box=box)["flags"]:
            station = flag.get("to") or flag.get("station")
            cid = (f"{flag['check']}:{flag['from']}->{flag['to']}" if flag.get("from")
                   else f"{flag['check']}:{station}")
            values = {"evidence": flag["evidence"]}
            conflicts[cid] = {
                "id": cid, "station": station, "field": "position", "source": sl,
                "what": f"{flag['check'].replace('_', ' ')} at station {station}: "
                        f"{flag['evidence']}",
                "how_to_fix": "if the station list is right, set resolution = \"ack\"; "
                              "otherwise give the corrected values, e.g. resolution = "
                              "{ longitude = 143.2 }, and say where they come from in note",
                "values": values, "values_hash": values_hash(values)}
        for entry in decisions.get("cross_check") or []:
            if not entry.get("file") or not entry.get("name"):
                warnings.append("a [[cross_check]] entry needs name and file")
                continue
            other = load_source(entry)
            if other is None:
                continue
            report = reconcile_stations({sl: stations, entry["name"]: other},
                                        position_tolerance_km=float(entry.get("tolerance_km", 5)),
                                        compare=tuple(entry.get("compare") or
                                                      ("position", "date")))
            for c in report["conflicts"]:
                cid = f"{c['field']}:{c['station']}"
                values = {src: (list(v) if isinstance(v, tuple) else v)
                          for src, v in c["values"].items()}
                conflicts[cid] = {
                    "id": cid, "station": c["station"], "field": c["field"],
                    "source": entry["name"],
                    "what": f"station {c['station']} {c['field']}: "
                            + "; ".join(f"{s} says {v}" for s, v in values.items())
                            + (f" ({c['spread_km']} km apart)" if c.get("spread_km") else ""),
                    "how_to_fix": "set resolution = \"keep\" to keep the station list, the "
                                  "name of the source to use instead, or explicit values, "
                                  "e.g. { date = \"2015-11-12\" }; say why in note",
                    "values": values, "values_hash": values_hash(values)}
    return list(conflicts.values()), warnings


def conflict_sections(conflicts: List[Dict[str, Any]], decisions: Dict[str, Any]) -> List[str]:
    """Proposals for the conflicts the decisions file does not list yet."""
    out = []
    for c in conflicts:
        if has_item(decisions, "conflicts", c["id"]):
            continue
        out.append(section(item_header("conflicts", c["id"]),
                           {"values": c["values"], "values_hash": c["values_hash"],
                            "resolution": "", "note": ""},
                           comments=[f"OPEN: {c['what']}", f"How: {c['how_to_fix']}"]))
    return out
