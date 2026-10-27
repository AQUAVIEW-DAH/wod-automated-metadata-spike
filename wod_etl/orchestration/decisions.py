"""The decisions file (`decisions.toml`): what each file holds, whether it goes into WOD, and why.

`wod-etl run` writes `[metadata]` and one `[files."<path or pattern>"]` entry per file or group
of alike files; a person reviews it and runs `wod-etl confirm`. Once written it is only appended
to, and an entry for one file overrides a pattern that also matches it.
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
    """The table header for an item id, e.g. `conflicts."date:6"`."""
    return f"{kind}.{toml_key(item_id)}"


# ----------------------------------------------------------------- reading
def load(path) -> Tuple[Dict[str, Any], Optional[str]]:
    """(decisions, error): a missing file is empty, a malformed one an error that stops the run."""
    p = Path(path)
    if not p.exists():
        return {}, None
    try:
        return tomllib.loads(p.read_text(encoding="utf-8")), None
    except tomllib.TOMLDecodeError as e:
        return {}, f"{p.name} is not valid TOML: {e}"


def has_item(decisions: Dict[str, Any], kind: str, item_id: str) -> bool:
    return item_id in (decisions.get(kind) or {})


def is_pattern(key: str) -> bool:
    return any(c in key for c in "*?[")


def matches(pattern: str, path: str) -> bool:
    """Match `path` against `pattern`; a `*` does not cross folders."""
    parts, wanted = path.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(a, b)
                                             for a, b in zip(parts, wanted))


def decision_key(decisions: Dict[str, Any], path: str) -> Optional[str]:
    """The `[files]` key deciding `path`: its own, else the most literal matching pattern."""
    entries = decisions.get("files") or {}
    if isinstance(entries.get(path), dict):
        return path
    patterns = [k for k, v in entries.items()
                if is_pattern(k) and isinstance(v, dict) and matches(k, path)]
    return max(patterns, key=lambda k: len(re.sub(r"[*?]|\[[^]]*\]", "", k))) \
        if patterns else None


def file_decision(decisions: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    """The `[files]` entry that decides `path` (see decision_key)."""
    key = decision_key(decisions, path)
    return (decisions.get("files") or {})[key] if key is not None else None


def section_hash(obj: Any) -> str:
    """A short, order-independent hash of a decisions section, for stage fingerprints."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def values_hash(values: Dict[str, Any]) -> str:
    return section_hash(values)[:10]


# ----------------------------------------------------------------- writing
FILE_HEADER = '''\
# Accession {accession}: which of its files go into WOD, and why. Review it, then run:
#
#     uv run wod-etl confirm {accession}
#
{summary}#
# Each entry decides one file, or every file its * pattern matches (a file's own entry wins):
#   include   true: written to WOD as `dataset`; false: left out
#   dataset   the WOD dataset: {datasets}
#   data/why  what the file holds, and why it was decided so; change what you disagree with
#   drop      variables not to write, e.g. drop = ["salinity"]
#   metadata  header values for its casts, e.g. metadata = {{ cruise = "NABOS-08" }}
# `wod-etl run` never rewrites this file: entries for files it has not seen go at the end.

accession = {accession_toml}
'''

# Section headings, in file order.
SECTIONS = [
    ("metadata", "Header values for every cast: they override what a file states"),
    ("check", "CHECK: what these hold could not be told; decide them"),
    ("in", "IN: written to WOD by `wod-etl confirm`"),
    ("review_out", "OUT: left out by the review"),
    ("later", "OUT: WOD takes these, but wod-etl cannot write them (why says what is missing)"),
    ("not_wod", "OUT: not something WOD stores"),
    ("sidecar", "OUT: read to locate casts, not written themselves"),
    ("docs", "DOCUMENTATION: read, not processed"),
    ("unmatched", "ENTRIES THAT MATCH NO FILE of the accession (kept as written)"),
]


def section_of(f: Dict[str, Any], entry: Optional[Dict[str, Any]] = None) -> str:
    """The SECTIONS key for a file's entry: the review's `include` first, then triage."""
    include = (entry or {}).get("include", f.get("include"))
    action = f.get("action")
    if include is True:
        return "in"
    if action == "document":
        return "docs"
    if action in ("process", "stage"):
        # Left out by wod-etl (it cannot be staged) or by the review.
        return "later" if f.get("include") is False else "review_out"
    if action == "sidecar":
        return "sidecar"
    if action == "exclude":
        return "later" if f.get("belongs_in_wod") is True else "not_wod"
    return "check"


def summary(rows: List[Tuple[str, Dict[str, Any], Optional[str]]]) -> str:
    """Opening comment lines counting files per section. `rows` are (section, triage entry,
    dataset) per file."""
    by: Dict[str, List[Tuple[Dict[str, Any], Optional[str]]]] = {}
    for sec, f, dataset in rows:
        by.setdefault(sec, []).append((f, dataset))
    kinds = lambda fs: "(" + _few(dict.fromkeys(str(f.get("exclusion") or f.get("data") or "")
                                                for f, _ in fs)) + ")"
    detail = {
        "in": lambda fs: ("as " + ", ".join(f"{d} ({n})" for d, n in _counts(
            str(d or "?") for _, d in fs)), ""),
        "check": lambda fs: ("what they hold could not be told: decide them", ""),
        "review_out": lambda fs: ("left out by the review", ""),
        "later": lambda fs: ("WOD takes them, but wod-etl cannot write them", kinds(fs)),
        "not_wod": lambda fs: ("not something WOD stores", kinds(fs)),
        "sidecar": lambda fs: ("read to locate casts, not written", ""),
        "docs": lambda fs: ("documentation: read, not processed", ""),
    }
    label = {"in": "IN", "check": "CHECK", "docs": "DOCS"}
    lines = []
    for sec, _heading in SECTIONS:
        if by.get(sec) and sec in detail:
            n = len(by[sec])
            text, more = detail[sec](by[sec])
            lines.append(f"#   {label.get(sec, 'OUT'):<5} {n:>4} file{'s' if n != 1 else ' '}  "
                         f"{text}")
            if more:
                lines.append(f"#{'':22}{more}")
    return ("# As proposed when this file was written (report.md says what was written):\n"
            + "\n".join(lines) + "\n") if lines else ""


def _counts(values) -> List[Tuple[str, int]]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return sorted(out.items(), key=lambda kv: -kv[1])


def _few(words, most: int = 3) -> str:
    words = [w for w in words if w]
    return ", ".join(words[:most]) + (f" and {len(words) - most} more" if len(words) > most
                                      else "")


def heading(sec: str) -> str:
    text = dict(SECTIONS)[sec]
    return f"# ==== {text} " + "=" * max(4, 84 - len(text)) + "\n"


def append(path, sections: List[str], accession: str, label: str,
           triage: Optional[Dict[str, Any]] = None) -> int:
    """Create the file, or append `sections` in a marked block; returns how many were added."""
    p = Path(path)
    if not sections and p.exists():
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        rows = [(section_of(f), f, f.get("dataset")) for f in (triage or {}).get("files", [])]
        text = header(accession, rows) + "\n" + "\n".join(sections)
        p.write_text(text, encoding="utf-8")
        return sum(1 for x in sections if not x.startswith("# ===="))
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"\n# ---- added by {label} ----\n\n" + "\n".join(sections))
    return sum(1 for x in sections if not x.startswith("# ===="))


def header(accession: str, rows) -> str:
    return FILE_HEADER.format(accession=accession, accession_toml=toml_value(accession),
                              datasets=", ".join(wod_datasets()), summary=summary(rows))


def reformat(path, triage: Dict[str, Any],
             old_triage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Rewrite a decisions file in the current layout, keeping every value the review set.

    Also refreshes `why`s the review left as `old_triage` wrote them, drops entries shadowed by
    per-file entries and renames old [metadata] keys. Comments are lost; the old file is kept
    as `.bak`. Returns `{"written", "dropped", "whys_updated", "error"}`."""
    p = Path(path)
    decisions, err = load(p)
    if err or not decisions:
        return {"written": False, "error": err or f"{p} has no decisions"}
    files = {f["path"]: f for f in triage.get("files", [])}
    old_why = {f["path"]: f.get("why") for f in (old_triage or {}).get("files", [])}
    entries = decisions.get("files") or {}
    if any(not isinstance(e, dict) for e in entries.values()):
        return {"written": False, "error": "an entry under [files] is not a table: fix it first"}
    decided: Dict[str, List[str]] = {}
    for f in files:
        key = decision_key(decisions, f)
        if key is not None:
            decided.setdefault(key, []).append(f)

    expected = {k: v for k, v in decisions.items() if k not in ("files", "metadata")}
    expected["files"] = {}
    rows, chunks, dropped, updated = [], {}, [], 0
    for key, entry in entries.items():
        members = sorted(decided.get(key, []))
        if not members and any(f == key or (is_pattern(key) and matches(key, f))
                               for f in files):
            dropped.append(key)
            continue
        e = dict(entry)
        if members and old_triage and all(old_why.get(m) == e.get("why") for m in members):
            now = {files[m].get("why") for m in members}
            if len(now) == 1 and None not in now and now != {e.get("why")}:
                e["why"], updated = now.pop(), updated + 1
        if isinstance(e.get("metadata"), dict):
            e["metadata"] = {_OLD_METADATA_KEYS.get(k, k): v for k, v in e["metadata"].items()}
        sec = section_of(files[members[0]], e) if members else "unmatched"
        if sec == "docs" and members and all(files[m].get("why") == e.get("why")
                                              for m in members):
            e.pop("why", None)                   # its heading says it
        expected["files"][key] = e
        rows += [(sec, files[m], e.get("dataset")) for m in members]
        order = ["include", "dataset", "data", "why", "drop", "metadata"]
        fields = {k: e[k] for k in order if k in e} | {k: v for k, v in e.items()
                                                        if k not in order}
        chunks.setdefault(sec, []).append(section(item_header("files", key), fields,
                                                  comments=file_list([_name(m)
                                                                      for m in members])))
    meta = {_OLD_METADATA_KEYS.get(k, k): v
            for k, v in (decisions.get("metadata") or {}).items()}
    if "metadata" in decisions:
        expected["metadata"] = meta

    accession = str(decisions.get("accession") or "")
    text = header(accession, rows)
    scalars = {k: v for k, v in decisions.items()
               if k != "accession" and not isinstance(v, (dict, list))}
    text += "".join(f"{toml_key(k)} = {toml_value(v)}\n" for k, v in scalars.items())
    text += "\n" + heading("metadata") + "\n".join(METADATA_HELP + metadata_lines(meta)) + "\n"
    for sec, _ in SECTIONS:
        if chunks.get(sec):
            text += "\n" + heading(sec) + "\n" + "\n".join(chunks[sec])
    for kind, value in decisions.items():
        if kind in ("accession", "files", "metadata") or kind in scalars:
            continue
        if isinstance(value, list):
            text += "".join(f"\n[[{toml_key(kind)}]]\n" + section("x", v).split("\n", 1)[1]
                            for v in value)
        elif kind in ("profiles", "conflicts"):
            text += "".join("\n" + section(item_header(kind, i), v) for i, v in value.items())
        else:
            text += "\n" + section(toml_key(kind), value)
    try:
        if tomllib.loads(text) != expected:
            return {"written": False, "error": "the rewrite would not read back as the same "
                                              "decisions; the file was left as it is"}
    except tomllib.TOMLDecodeError as e:
        return {"written": False, "error": f"the rewrite is not valid TOML ({e}); the file "
                                           f"was left as it is"}
    p.with_name(p.name + ".bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(text, encoding="utf-8")
    return {"written": True, "dropped": dropped, "whys_updated": updated, "error": None}


# ----------------------------------------------------------------- proposals
def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return s or "recipe"


def _depth_like(col: str) -> bool:
    return bool(re.match(r"^(depth|dep|z|pres|press|pressure|prdm|depsm)", col.strip(), re.I))


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def propose(triage: Dict[str, Any], decisions: Dict[str, Any]) -> List[str]:
    """Sections for files no entry decides yet, under headings; [metadata] the first time."""
    files = triage.get("files", [])
    everything = [f["path"] for f in files]
    new = [f for f in files if file_decision(decisions, f["path"]) is None]
    out = [] if "metadata" in decisions else [_metadata_section(triage)]
    by_section: Dict[str, List[Tuple[str, List[Dict[str, Any]]]]] = {}
    for key, members in _groups(new, everything):
        by_section.setdefault(section_of(members[0]), []).append((key, members))
    for sec, _ in SECTIONS:
        if by_section.get(sec):
            out.append(heading(sec))
            out += [_entry(key, members)
                    for key, members in sorted(by_section[sec], key=lambda e: e[0])]
    return out


# [metadata] keys (one word, so unquoted), their placeholder, and the WOD code list of each.
METADATA_FIELDS = [
    ("platform", "<WOD platform code, or the ship's name>", ("secondary_header", 3)),
    ("institution", "<WOD institution code, or its name>", ("secondary_header", 4)),
    ("project", "<WOD project code, or its name>", ("secondary_header", 2)),
    ("cruise", "<the originator's cruise id>", None),
    ("principal_investigator", "<name>", None),
    ("probe_type", "<WOD probe type code, or e.g. CTD>", ("secondary_header", 29)),
    ("instrument", "<WOD instrument code, or the model, e.g. SBE 19plus>",
     ("variable_specific_header", 5)),
]
METADATA_HELP = [
    "# A WOD code is safest (platform = 8553): a name is looked up in WOD's list and kept as",
    "# text when it is not one entry. A value that differs between files goes in their own",
    '# entries instead: metadata = { cruise = "NABOS-08" }.']
# The keys as older decisions files wrote them.
_OLD_METADATA_KEYS = {"Platform": "platform", "Institution": "institution",
                      "Project": "project", "Cruise": "cruise",
                      "Principal investigator": "principal_investigator",
                      "Probe type": "probe_type", "Instrument": "instrument"}


def code_name(key: str, value: Any) -> Optional[str]:
    """The name of a WOD code in a [metadata] key's cached code list, or None."""
    from wod_etl.reference.code_lists import load_code_list

    coded = next((c for k, _, c in METADATA_FIELDS if k == key), None)
    text = str(value).strip()
    if not coded or not re.fullmatch(r"\d+", text):
        return None
    try:
        listing = load_code_list(*coded, fetch=False)
    except Exception:                                # noqa: BLE001 - a name is a nicety
        return None
    return next((e["label"] for e in listing.get("entries") or []
                 if e["code"].lstrip("0") == text.lstrip("0")), None)


def metadata_lines(values: Dict[str, Any], proposed: Optional[Dict[str, List]] = None
                   ) -> List[str]:
    """`[metadata]` lines: set values (codes named), proposals, then unset keys commented out."""
    lines = ["[metadata]"]
    for key, value in values.items():
        name = code_name(key, value)
        lines.append(f"{toml_key(key)} = {toml_value(value)}" + (f"   # {name}" if name else ""))
    for key, options in (proposed or {}).items():
        if key not in values:
            lines.append(f"# {toml_key(key)} = {toml_value(options[0][0])}")
            lines += [f"#   {line}" for line in _wrap(
                "or: " + "; ".join(v for v, _ in options[1:]), 88)]
    for key, placeholder, _ in METADATA_FIELDS:
        if key not in values and key not in (proposed or {}):
            lines.append(f"# {key} = {toml_value(placeholder)}")
    return lines


def _metadata_section(triage: Dict[str, Any]) -> str:
    """The [metadata] proposal from ISO 19115 records: single values set, several listed to
    choose from, the rest commented out."""
    found = iso_metadata(triage)
    lines = [heading("metadata").rstrip("\n")] + METADATA_HELP
    if found:
        sources = ", ".join(sorted({src for v in found.values() for _, src in v}))
        lines += [f"# {line}" for line in _wrap(
            f"Proposed from the accession's ISO 19115 metadata ({sources}); check them.", 92)]
    single = {k: v[0][0] for k, v in found.items() if len(v) == 1}
    several = {k: v for k, v in found.items() if len(v) > 1}
    return "\n".join(lines + metadata_lines(single, several)) + "\n"


_ISO = {"gmd": "http://www.isotc211.org/2005/gmd", "gco": "http://www.isotc211.org/2005/gco",
        "gmi": "http://www.isotc211.org/2005/gmi", "gmx": "http://www.isotc211.org/2005/gmx"}


def iso_metadata(triage: Dict[str, Any]) -> Dict[str, List[Tuple[str, str]]]:
    """{key: [(value, file)]} from the accession's ISO 19115 records: platforms, project and
    instrument-type keywords, and the principal investigator."""
    import xml.etree.ElementTree as ET

    root = Path(triage.get("root") or "")
    found: Dict[str, List[Tuple[str, str]]] = {}

    def add(key, value, src):
        value = " ".join(str(value or "").split())
        if value and value.lower() not in {v.lower() for v, _ in found.get(key, [])}:
            found.setdefault(key, []).append((value, src))

    for f in triage.get("files", []):
        if not re.search(r"-ISO-19115-2\.xml$|^\d{5,7}\.\d+\.\d+\.xml$", _name(f["path"])):
            continue
        try:
            doc = ET.parse(root / f["path"]).getroot()
        except (OSError, ET.ParseError):
            continue
        src = _name(f["path"])
        platforms = []
        for platform in doc.iter(f"{{{_ISO['gmi']}}}MI_Platform"):
            platforms.append(next((e.text for e in platform.iter()
                                   if e.text and e.text.strip()), None))
        for kw in doc.iter(f"{{{_ISO['gmd']}}}MD_Keywords"):
            kind = kw.find("gmd:type/gmd:MD_KeywordTypeCode", _ISO)
            kind = kind.get("codeListValue") if kind is not None else None
            # An instrument keyword names a type (e.g. 'CTD'): WOD's probe type, not a model.
            key = {"project": "project", "instrument": "probe_type"}.get(kind)
            if key:
                for word in kw.findall("gmd:keyword/gco:CharacterString", _ISO) + \
                        kw.findall("gmd:keyword/gmx:Anchor", _ISO):
                    add(key, word.text, src)
        for party in doc.iter(f"{{{_ISO['gmd']}}}CI_ResponsibleParty"):
            role = party.find("gmd:role/gmd:CI_RoleCode", _ISO)
            if role is not None and role.get("codeListValue") == "principalInvestigator":
                name = party.find("gmd:individualName/gco:CharacterString", _ISO)
                add("principal_investigator", name.text if name is not None else None,
                    src)
        # Submitters sometimes put the instrument in the platform field ('CTD').
        instruments = {v.lower() for v, _ in found.get("probe_type", [])}
        for text in platforms:
            value = " ".join(str(text or "").split())
            if value.lower() not in instruments and not _INSTRUMENT_NAME.fullmatch(value):
                add("platform", value, src)
    return found


_INSTRUMENT_NAME = re.compile(r"x?ctd|xbt|mbt|adcp|svp|sv&t|cast|instrument", re.I)


def _entry(key: str, members: List[Dict[str, Any]]) -> str:
    first = members[0]
    data = ("; ".join(dict.fromkeys(m["data"] for m in members))
            if first["action"] == "document" else first["data"])
    why = first["why"]
    names = [_name(m["path"]) for m in members]
    # Documentation needs no `why`: its heading says it.
    if first["action"] == "document" and not first["include"]:
        why = None
    return section(item_header("files", key),
                   {"include": bool(first["include"]),
                    "dataset": first.get("dataset") if first["include"] else None,
                    "data": data, "why": why,
                    "drop": first.get("drop") or None if first["include"] else None},
                   comments=file_list(names) + depth_list(members))


def depth_list(members: List[Dict[str, Any]]) -> List[str]:
    """Comment lines with each mooring instrument's derived depth and how it was found."""
    placed = [m for m in members if (m.get("mooring") or {}).get("depth_short")]
    if not placed:
        return []
    width = max(len(_name(m["path"])) for m in placed)
    return ["Depths, as wod-etl places them:"] + [
        f"  {_name(m['path']):<{width}}  {m['mooring']['depth']:>6.1f} m  "
        f"{m['mooring']['depth_short']}" for m in sorted(placed, key=lambda m: m["mooring"]["depth"])]


def _wrap(text: str, width: int) -> List[str]:
    import textwrap

    return textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)


def file_list(names: List[str], most: int = 12) -> List[str]:
    """`3 files: a.nc, b.nc, c.nc` as comment lines that fit, for an entry of several files."""
    import textwrap

    if len(names) < 2:
        return []
    text = (f"{len(names)} files: " + ", ".join(names[:most])
            + (f" ... and {len(names) - most} more" if len(names) > most else ""))
    return textwrap.wrap(text, width=94, subsequent_indent="  ", break_long_words=False,
                         break_on_hyphens=False)


def _groups(files: List[Dict[str, Any]], everything: List[str]):
    """(key, files) per entry: alike files in one folder share an entry under a pattern that
    matches exactly them."""
    buckets: Dict[tuple, List[Dict[str, Any]]] = {}
    for f in files:
        alike = (("document",) if f["action"] == "document" else
                 (bool(f["include"]), f.get("dataset"), f["data"], f["why"],
                  tuple(f.get("drop") or ())))
        buckets.setdefault((_parent(f["path"]),) + alike, []).append(f)
    # A file alone in its bucket gets its own path entry, which beats any pattern.
    own = {members[0]["path"] for members in buckets.values() if len(members) == 1}
    out = []
    for members in buckets.values():
        out += _patterns(sorted(members, key=lambda m: m["path"]), everything, own)
    return out


_SEPARATORS = "_-. "


def _patterns(members: List[Dict[str, Any]], everything: List[str], own=frozenset()):
    """Patterns for a group: one matching exactly its files if possible, else split by leading
    name token, then extension. If that leaves only single files, use the most specific
    pattern whose other matches all have their own entries (`own`)."""
    out = _exact_patterns(members, everything)
    if len(members) > 1 and all(len(group) == 1 for _, group in out):
        paths = {m["path"] for m in members}
        names = [_name(p) for p in sorted(paths)]
        for candidate in reversed(_candidates(_parent(members[0]["path"]), names)):
            if {p for p in everything if matches(candidate, p)} - set(own) == paths:
                return [(candidate, members)]
    return out


def _exact_patterns(members: List[Dict[str, Any]], everything: List[str]):
    if len(members) == 1:
        return [(members[0]["path"], members)]
    paths = {m["path"] for m in members}
    for candidate in _candidates(_parent(members[0]["path"]), [_name(p) for p in sorted(paths)]):
        if {p for p in everything if matches(candidate, p)} == paths:
            return [(candidate, members)]
    for key in (lambda m: re.split(r"[_\-. ]", _name(m["path"]))[0],
                lambda m: Path(m["path"]).suffix.lower()):
        split: Dict[str, List[Dict[str, Any]]] = {}
        for m in members:
            split.setdefault(key(m), []).append(m)
        if len(split) > 1:
            return [x for part in split.values() for x in _exact_patterns(part, everything)]
    return [(m["path"], [m]) for m in members]


def _candidates(folder: str, names: List[str]) -> List[str]:
    """Patterns from most general to most specific, cut at name separators: `*`, `*.nc`,
    `A_*.nc`, `A_*_B_*.nc`."""
    at = f"{folder}/" if folder else ""
    prefix = os.path.commonprefix(names)
    cut = max(prefix.rfind(c) for c in _SEPARATORS)
    prefix = prefix[:cut + 1] if cut >= 0 else ""
    suffix = os.path.commonprefix([n[::-1] for n in names])[::-1]
    cut = min((suffix.find(c) for c in _SEPARATORS if c in suffix), default=-1)
    suffix = suffix[cut:] if cut >= 0 else ""
    out = [f"{at}*"]
    if suffix:
        out.append(f"{at}*{suffix}")
    if prefix:
        out.append(f"{at}{prefix}*{suffix}")
    # A shared middle part only reads as a name after a shared prefix.
    middles = [n[len(prefix):len(n) - len(suffix)] for n in names]
    shared = _shared_part(middles) if prefix else ""
    if shared:
        out.append(f"{at}{prefix}*{shared}*{suffix}")
    return out


def _shared_part(texts: List[str], shortest: int = 3) -> str:
    """The longest substring all texts contain, trimmed to name separators ('kHz_ADCP_' ->
    '_ADCP_')."""
    base = min(texts, key=len) if texts else ""
    for size in range(len(base), shortest - 1, -1):
        for i in range(len(base) - size + 1):
            part = base[i:i + size]
            if all(part in t for t in texts):
                at = [j for j, c in enumerate(part) if c in _SEPARATORS]
                if len(at) >= 2 and at[-1] - at[0] >= shortest - 1:
                    part = part[at[0]:at[-1] + 1]
                return part
    return ""


def derive_recipes(triage: Dict[str, Any], included: set, taken: set, names: set
                   ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """(recipes, not_written) for included files that need reshaping and are not `taken`.

    Files that cannot be staged go to `not_written` with a reason."""
    from wod_etl.staging.moorings import iso

    files = [f for f in triage.get("files", [])
             if f["path"] in included and f["path"] not in taken and f["action"] == "stage"
             and not f.get("taxa")]
    recipes: Dict[str, Dict[str, Any]] = {}
    not_written: List[Dict[str, Any]] = []
    unique = lambda label: _unique_name(label, names)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        if f.get("mooring"):
            groups.setdefault(f["mooring"].get("group") or "mooring", []).append(f)
    for label, group in groups.items():
        info = group[0]["mooring"]
        if info.get("deployment") == "apart":
            not_written += [{"key": f["path"], "why": "its mooring's pressure records do not "
                                                       "overlap in time"} for f in group]
            continue
        # Every file was read for a position, and none gave one.
        if all("latitude" in f["mooring"] for f in group) and not any(
                f["mooring"].get("latitude") is not None
                and f["mooring"].get("longitude") is not None for f in group):
            not_written += [{"key": f["path"], "why": "nothing states where its mooring was: no "
                                                       "instrument file gives a position"}
                            for f in group]
            continue
        placed = [f for f in group if f["mooring"].get("placed_by")]
        not_written += [{"key": f["path"], "why": "its depth is not known: it records no "
                                                   "pressure, and nothing states where it was"}
                        for f in group if not f["mooring"].get("placed_by")]
        if not placed:
            continue
        window = info.get("deployed")
        recipes[unique(label)] = {
            "kind": "mooring", "dataset_type": "MRB", "files": [f["path"] for f in placed],
            "depths": {_name(f["path"]): ("pressure" if f["mooring"]["placed_by"] == "pressure"
                                          else f["mooring"]["depth"]) for f in placed},
            "placed": {_name(f["path"]): f["mooring"].get("depth_note") for f in placed},
            "deployed": [iso(w) for w in window] if window else "record",
            "deployment": info.get("deployment"), "metadata": {"cruise": label}}

    tables: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for f in files:
        lf = f.get("long_format")
        if lf and not f.get("mooring"):
            tables.setdefault((_parent(f["path"]), lf["variable_column"], lf["value_column"]),
                              []).append(f)
    for (folder, var, val), members in tables.items():
        station_list = members[0]["long_format"].get("station_list")
        if not station_list:
            not_written += [{"key": m["path"], "why": "its casts cannot be located: no station "
                                                      "list beside it names them"}
                            for m in members]
            continue
        key_sets = [m["long_format"]["key_columns"] for m in members]
        canonical = max(key_sets, key=lambda k: key_sets.count(k))
        aliases: Dict[str, List[str]] = {}
        for ks in key_sets:
            for k, c in zip(ks, canonical):
                if k != c and k not in aliases.setdefault(c, []):
                    aliases[c].append(k)
        prefix = os.path.commonprefix([Path(m["path"]).stem for m in members]).rstrip("_-. ")
        datasets = {m.get("dataset") for m in members} - {None}
        recipes[unique(prefix if len(prefix) >= 3 else (_name(folder) or "profiles"))] = {
            "kind": "long", "files": [m["path"] for m in members], "keys": canonical,
            "key_aliases": {k: v for k, v in aliases.items() if v} or None,
            "cast_key": next((k for k in canonical if not _depth_like(k)), canonical[0]),
            "variable": var, "value": val, "station_list": station_list,
            "dataset_type": datasets.pop() if len(datasets) == 1 else None}

    # Casts located by a station list: one recipe per folder, list and way of matching.
    located: Dict[Tuple, List[Dict[str, Any]]] = {}
    for f in files:
        lb = f.get("located_by")
        if lb and not f.get("mooring") and not f.get("long_format"):
            located.setdefault((_parent(f["path"]), lb["kind"], lb["station_list"], lb["key"],
                                lb.get("station_column"), f.get("dataset")), []).append(f)
    # Surface tracks on one clock: one recipe for all of them.
    streams = [f for f in files if f.get("combine_tracks")]
    if streams:
        recipes[unique("surface_tracks")] = {
            "kind": "tracks", "files": [f["path"] for f in streams], "dataset_type": "SUR"}
    # Tables holding the same casts: one combine recipe per set.
    combined: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for f in files:
        if f.get("combine"):
            combined.setdefault(tuple(f["combine"]["files"]), []).append(f)
    for together, members in combined.items():
        recipes[unique(os.path.commonprefix([_name(p) for p in together]).rstrip("_-. ")
                       or "combined")] = {
            "kind": "combine", "files": [m["path"] for m in members],
            "dataset_type": members[0].get("dataset")}
    for (folder, kind, station_list, key, station_column, dataset), members in located.items():
        recipes[unique(_name(folder) or "casts")] = {
            "kind": kind, "files": [m["path"] for m in members], "station_list": station_list,
            "station_column": key, "cast_column": station_column, "dataset_type": dataset}
    return recipes, not_written


def _unique_name(label: str, names: set) -> str:
    name = base = _slug(label)
    n = 2
    while name in names:
        name, n = f"{base}_{n}", n + 1
    names.add(name)
    return name


def derive_taxa_recipes(triage: Dict[str, Any], included: set, taken: set, names: set
                        ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """(recipes, not_written): one taxa recipe per included plankton table found by triage."""
    recipes: Dict[str, Dict[str, Any]] = {}
    not_written: List[Dict[str, Any]] = []
    for f in triage.get("files", []):
        t = f.get("taxa")
        if not t or f["path"] not in included or f["path"] in taken:
            continue
        if t.get("missing"):
            not_written.append({"key": f["path"], "why": "; ".join(t["missing"])})
            continue
        recipes[_unique_name(Path(f["path"]).stem, names)] = {
            "kind": "long", "files": [f["path"]], "dataset_type": "OSD",
            "columns": t["columns"], "measurements": t["measurements"],
            "station_list": t.get("station_list")}
    return recipes, not_written


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
    """An item for the person; `key` is the decisions.toml section that settles it (default:
    the item id; "" for none)."""
    return {"id": item_id, "what": what, "how_to_fix": how, "blocking": blocking,
            "decisions_key": item_id if key is None else (key or None)}


def validate(decisions: Dict[str, Any], triage: Dict[str, Any],
             recipe_checks: Dict[str, Dict[str, Any]],
             conflicts: List[Dict[str, Any]],
             taxa_checks: Optional[Dict[str, Dict[str, Any]]] = None
             ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(open items, plan). The plan is usable only when no open item is blocking.

    What wod-etl settles itself is reported as non-blocking."""
    items: List[Dict[str, Any]] = []
    datasets = set(wod_datasets())
    files = {f["path"]: f for f in triage.get("files", [])}
    plan = build_plan(decisions, triage, conflicts)
    consumed = set(plan["consumed"])

    if plan["undecided"]:
        first = plan["undecided"][0]
        items.append(open_item("files", f"{len(plan['undecided'])} file(s) have no entry, "
                                        f"e.g. {first}",
                               "run `wod-etl run` again: it adds an entry for every file "
                               "without one", key=None))
    for key, entry in (decisions.get("files") or {}).items():
        hdr = item_header("files", key)
        if not isinstance(entry, dict):
            items.append(open_item(hdr, f"{key}: is a value, not an entry",
                                   "write it as [files.\"<path>\"] with include = true or "
                                   "false"))
            continue
        include = entry.get("include")
        if not isinstance(include, bool):
            items.append(open_item(hdr, f"{key}: include = {include!r}" if "include" in entry
                                   else f"{key}: has no include",
                                   "set include = true or include = false"))
            continue
        decided = [p for p in files if file_decision(decisions, p) is entry]
        if not decided:
            matched = [p for p in files if p == key or (is_pattern(key) and matches(key, p))]
            items.append(open_item(hdr, f"{key}: decides no file, as every file it matches has "
                                        f"an entry of its own" if matched else
                                        f"{key}: matches no file of the accession",
                                   "nothing to do; remove it if it is no longer wanted"
                                   if matched else "fix the path, or remove it",
                                   blocking=False))
        if not include:
            continue
        drop = entry.get("drop")
        if drop is not None and not (isinstance(drop, list)
                                     and all(isinstance(d, str) for d in drop)):
            items.append(open_item(hdr, f"{key}: drop = {drop!r}",
                                   "write drop as a list of variable names, e.g. "
                                   "drop = [\"salinity\"]"))
        if entry.get("metadata") is not None and not isinstance(entry["metadata"], dict):
            items.append(open_item(hdr, f"{key}: metadata = {entry['metadata']!r}",
                                   "write metadata as a table, e.g. metadata = "
                                   "{ Cruise = \"NABOS-08\" }"))
        dataset = entry.get("dataset")
        if dataset not in (None, "") and dataset not in datasets:
            items.append(open_item(hdr, f"{key}: dataset = {dataset!r} is not a WOD dataset",
                                   "use one of " + ", ".join(sorted(datasets))))
        elif not dataset:
            unnamed = [p for p in decided if p not in consumed and not files[p].get("dataset")]
            if unnamed:
                items.append(open_item(hdr, f"{key}: is included, and nothing names its WOD "
                                            f"dataset: it is classified from what it holds "
                                            f"when it is processed",
                                       "nothing to do, or set dataset = one of "
                                       + ", ".join(sorted(datasets)),
                                       blocking=False))
    for w in plan["not_written"]:
        key = decision_key(decisions, w["key"])
        items.append(open_item(item_header("files", w["key"]),
                               f"{w['key']}: included, but it cannot be written: {w['why']}",
                               "nothing to do: it is left out (or set include = false)",
                               blocking=False, key=item_header("files", key) if key else ""))

    for name, r in (decisions.get("profiles") or {}).items():
        hdr = item_header("profiles", name)
        if not isinstance(r, dict) or r.get("kind") != "script":
            items.append(open_item(hdr, f"[profiles.{name}] is not read: wod-etl works "
                                        f"out how files become casts from the files",
                                   "nothing to do; delete the section", blocking=False))
            continue
        if not r.get("script"):
            items.append(open_item(hdr, f"profiles '{name}' (kind = \"script\") names no "
                                        f"script", "set script = \"scripts/<name>.py\", or "
                                                   "delete the section"))
        if r.get("dataset_type") and r["dataset_type"] not in datasets:
            items.append(open_item(hdr, f"profiles '{name}': dataset_type "
                                        f"{r['dataset_type']!r} is not a WOD dataset",
                                   "use one of " + ", ".join(sorted(datasets))))

    for name, r in plan["profiles"].items():
        check = recipe_checks.get(name) or {}
        hdr = item_header("profiles", name)
        files_of = ", ".join(_name(f) for f in (r.get("files") or [])[:3])
        leave_out = (f"set include = false on the file(s) at fault in decisions.toml "
                     f"({files_of}{', ...' if len(r.get('files') or []) > 3 else ''})")
        if check.get("error"):
            items.append(open_item(hdr, f"{name}: its files cannot be read: {check['error']}",
                                   leave_out, key=""))
        for problem in check.get("mooring_problems") or []:
            items.append(open_item(hdr, f"mooring {name}: {problem}", leave_out, key=""))
        unmatched = check.get("casts_without_station") or []
        if unmatched:
            items.append(open_item(hdr, f"{name}: cast(s) {unmatched[:8]} have no entry in "
                                        f"{r.get('station_list')}: they are left out",
                                   "nothing to do", blocking=False, key=""))
        if check.get("pivot_conflicts"):
            items.append(open_item(hdr, f"{name}: {check['pivot_conflicts']} level(s) carry "
                                        f"two values for one variable: the first is kept, and "
                                        f"the casts' notes say so",
                                   "nothing to do", blocking=False, key=""))
    for name, r in plan["taxa"].items():
        check = (taxa_checks or {}).get(name) or {}
        source = r["files"][0]
        hdr = item_header("taxa", name)
        if check.get("error"):
            items.append(open_item(hdr, f"{source}: its hauls cannot be read: "
                                        f"{check['error']}",
                                   f"set include = false on {source} in decisions.toml",
                                   key=""))
        unmatched = check.get("casts_without_station") or []
        if unmatched:
            items.append(open_item(hdr, f"{source}: haul(s) {unmatched[:8]} cannot be located, "
                                        f"so they are left out",
                                   "nothing to do", blocking=False, key=""))
        empty = check.get("hauls_without_measurement") or []
        if empty:
            items.append(open_item(hdr, f"{source}: haul(s) {empty[:8]} have no measurement in "
                                        f"any row, so they are left out",
                                   "nothing to do", blocking=False, key=""))
        for column, n in (check.get("negative_values") or {}).items():
            items.append(open_item(hdr, f"{source}: {n} negative value(s) in {column!r} are "
                                        f"read as missing-value codes and not written",
                                   "nothing to do", blocking=False, key=""))
        if check.get("missing_biological_fields"):
            items.append(open_item(hdr, f"{source}: not stated: "
                                        + ", ".join(check["missing_biological_fields"]),
                                   "nothing to do; ask the submitter", blocking=False,
                                   key=""))
    for c in conflicts:
        items.append(open_item(item_header("conflicts", c["id"]),
                               f"{c['what']}; the station list is kept",
                               "nothing to do; the casts' notes record it", blocking=False,
                               key=""))
    return items, plan


def build_plan(decisions: Dict[str, Any], triage: Dict[str, Any],
               conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What `wod-etl confirm` will do: files processed as they stand, recipes to stage, and
    files left out and why.

    Included files are written as the `dataset` their entry names, not reclassified; CTD casts
    with coarse levels still route to OSD (manual p.19)."""
    files = {f["path"]: f for f in triage.get("files", [])}
    keys = {p: decision_key(decisions, p) for p in files}
    decided = {p: (decisions.get("files") or {})[k] if k is not None else {}
               for p, k in keys.items()}
    included = {p for p, e in decided.items() if e.get("include") is True}
    # One row per entry, for the run summary.
    entries: Dict[str, Dict[str, Any]] = {}
    for p in sorted(files):
        if keys[p] is not None:
            e = entries.setdefault(keys[p], {
                "key": keys[p], "files": 0, "include": decided[p].get("include"),
                "dataset": decided[p].get("dataset"), "data": decided[p].get("data"),
                "why": decided[p].get("why"),
                "documentation": files[p]["action"] == "document"})
            e["files"] += 1
            e["documentation"] = e["documentation"] and files[p]["action"] == "document"
    profiles = {}
    for name, r in (decisions.get("profiles") or {}).items():
        if isinstance(r, dict) and r.get("kind") == "script":
            profiles[name] = dict(r)
    taken = {f for r in profiles.values() for f in r.get("files") or []}
    names = set(profiles)
    derived, not_written = derive_recipes(triage, included, taken, names)
    profiles.update(derived)
    taxa, taxa_not_written = derive_taxa_recipes(triage, included, taken, names)
    not_written += taxa_not_written
    consumed = {w["key"] for w in not_written}
    for r in list(profiles.values()) + list(taxa.values()):
        consumed |= set(r.get("files") or [])
        if r.get("station_list") and r["station_list"] != PLACEHOLDER:
            consumed.add(r["station_list"])
    direct = sorted(p for p in included if p not in consumed)
    overrides = {p: decided[p]["dataset"] for p in direct if decided[p].get("dataset")}
    # Per-file options: variables to drop, and header values on top of [metadata].
    shared = decisions.get("metadata") if isinstance(decisions.get("metadata"), dict) else {}
    options = {}
    for p in direct:
        e = decided[p]
        drop = [d for d in e.get("drop") or [] if isinstance(d, str)]
        meta = {**shared, **(e["metadata"] if isinstance(e.get("metadata"), dict) else {})}
        if drop or meta:
            options[p] = {"drop": drop, "metadata": meta}
    left_out = [{"key": p, "data": decided[p].get("data"), "why": decided[p].get("why")}
                for p in sorted(files) if decided[p].get("include") is False and p not in consumed]
    left_out += [{"key": w["key"], "data": decided[w["key"]].get("data"), "why": w["why"]}
                 for w in not_written]
    # Conflicts with a `resolution` in decisions.toml are applied as settled.
    overrides_by_station = {}
    for c in conflicts:
        entry = (decisions.get("conflicts") or {}).get(c["id"]) or {}
        res = entry.get("resolution")
        station = c.get("station")
        if not res or not station:
            continue
        o = overrides_by_station.setdefault(station, {"notes": []})
        o["notes"].append(f"{c['id']}: {c['what']}; settled as {res!r}"
                          + (f" ({entry['note']})" if entry.get("note") else ""))
        if isinstance(res, dict):
            o.update({k: v for k, v in res.items() if k in ("latitude", "longitude", "date",
                                                             "time")})
        elif isinstance(res, str) and res in c.get("values", {}) and c.get("field"):
            picked = c["values"][res]
            if c["field"] == "date":
                o["date"] = picked
            elif c["field"] == "position" and isinstance(picked, list) and len(picked) == 2:
                o["latitude"], o["longitude"] = picked
    for c in conflicts:
        if c.get("station") and not ((decisions.get("conflicts") or {}).get(c["id"]) or
                                     {}).get("resolution"):
            overrides_by_station.setdefault(c["station"], {"notes": []})["notes"].append(
                f"{c['what']}; the station list was kept")
    return {"direct": direct, "dataset_overrides": overrides, "file_options": options,
            "left_out": left_out, "not_written": not_written,
            "entries": sorted(entries.values(), key=lambda e: (not e["include"],
                                                                e["documentation"], e["key"])),
            "undecided": sorted(p for p in files if not decided[p] and p not in consumed),
            "profiles": profiles, "taxa": taxa, "consumed": sorted(consumed),
            "metadata": decisions.get("metadata") or {},
            "station_overrides": {k: v for k, v in overrides_by_station.items()
                                  if v.get("notes")}}


# ----------------------------------------------------------------- cross-checks
def crosscheck(decisions: Dict[str, Any], data_root, recipes: Dict[str, Dict[str, Any]],
               triage: Optional[Dict[str, Any]] = None
               ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(conflicts, warnings) from checking each recipe's station list.

    Checks the track (inside the bounding box, no outlier hops, time running forward) and any
    `[[cross_check]]` source; the station list is kept. Each conflict has `id`, `station`,
    `field`, `values` and `values_hash`. Unreadable sources are warnings.
    """
    from wod_etl.staging import (check_track, extract_pdf_rows, read_station_list,
                                 reconcile_stations)

    root = Path(data_root)
    conflicts: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    track = decisions.get("track") if isinstance(decisions.get("track"), dict) else {}
    box = track.get("bounding_box") or (_bounding_box(triage) if triage else None)
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

    lists = sorted({r["station_list"] for r in recipes.values()
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
                    "values": values, "values_hash": values_hash(values)}
    return list(conflicts.values()), warnings
