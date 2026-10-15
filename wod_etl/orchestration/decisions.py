"""The decisions file: what each file of an accession holds, whether it goes into WOD, and why.

`wod-etl run` triages the accession and writes `data/runs/<accession>/decisions.toml`: an
entry per file -- alike files share one, keyed by a `*` pattern -- saying what the file holds
(`data`), whether it is written to WOD (`include`), as which WOD dataset (`dataset`), and
why (`why`). A person reviews it, changes what they disagree with, and runs `wod-etl confirm`,
which writes exactly what is included.

It opens with a summary of how many files go where, then [metadata], then the entries under
a heading per outcome (IN; OUT, and why; DOCUMENTATION), `include` first in each.

The file is the person's, not the script's:
- it is written once, and afterwards only APPENDED to -- entries for files no entry covers
  yet go in a marked block at the end, and nothing the person typed (comments included) is
  rewritten; `reformat` re-lays out an older file on request, keeping every value;
- an entry for one file overrides a pattern that also matches it;
- a settled conflict carries the hash of the values it settled: if they change, it is open
  again.

Sections:

    accession = "0303928"
    [files."<path or pattern>"]  # data, include, dataset, why: one per file or group of files
                               # (optional: drop = [...], metadata = {...})
    [profiles.<name>]          # how included files that need it are reshaped into casts
    [metadata]                 # header values written into every cast: platform, institution
    [track]                    # bounding box and outlier factor for the station-track check
    [[cross_check]]            # an extra source to check station positions and dates against
    [conflicts."<id>"]         # a disagreement between sources, and how it was settled
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


def is_pattern(key: str) -> bool:
    return any(c in key for c in "*?[")


def matches(pattern: str, path: str) -> bool:
    """A `*` stays within one folder, as a person reads `data/0-data/*.nc`: it does not reach
    into data/0-data/moorings/."""
    parts, wanted = path.split("/"), pattern.split("/")
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(a, b)
                                             for a, b in zip(parts, wanted))


def decision_key(decisions: Dict[str, Any], path: str) -> Optional[str]:
    """The `[files]` key that decides `path`: its own, else the most specific pattern that
    matches it -- the one with the most literal characters. One pattern settles thirty files;
    an entry for one of them overrides it."""
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
#   data/why  what the file holds, and why it was decided so -- change what you disagree with
#   drop      variables not to write, e.g. drop = ["salinity"]
#   metadata  header values for its casts, e.g. metadata = {{ cruise = "NABOS-08" }}
# `wod-etl run` never rewrites this file: entries for files it has not seen go at the end.

accession = {accession_toml}
'''

# Where an entry goes in the file, in this order: what needs deciding, what is written, what is
# left out and why, and what is only read.
SECTIONS = [
    ("metadata", "Header values for every cast: they override what a file states"),
    ("check", "CHECK: what these hold could not be told -- decide them"),
    ("in", "IN: written to WOD by `wod-etl confirm`"),
    ("review_out", "OUT: left out by the review"),
    ("later", "OUT: WOD takes these, but wod-etl cannot write them yet"),
    ("not_wod", "OUT: not something WOD stores"),
    ("sidecar", "OUT: read to locate casts, not written themselves"),
    ("docs", "DOCUMENTATION: read, not processed"),
    ("unmatched", "ENTRIES THAT MATCH NO FILE of the accession (kept as written)"),
]


def section_of(f: Dict[str, Any], entry: Optional[Dict[str, Any]] = None) -> str:
    """Which SECTIONS heading a file's entry goes under: the entry's `include` (the review)
    first, then what triage found."""
    include = (entry or {}).get("include", f.get("include"))
    action = f.get("action")
    if include is True:
        return "in"
    if action == "document":
        return "docs"
    if action in ("process", "stage"):
        return "review_out"
    if action == "sidecar":
        return "sidecar"
    if action == "exclude":
        return "later" if f.get("belongs_in_wod") is True else "not_wod"
    return "check"


def summary(rows: List[Tuple[str, Dict[str, Any], Optional[str]]]) -> str:
    """The comment lines that open the file: how many files go where. `rows` are
    (section, triage entry, dataset) per file."""
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
        "later": lambda fs: ("WOD takes them, but wod-etl cannot write them yet", kinds(fs)),
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
    """Create the file, or append `sections` in a marked block. Returns how many were added.
    A new file opens with a summary of where the files go, from `triage`'s proposals."""
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
    """Rewrite an existing decisions file in the current layout -- summary, sections,
    [metadata] first, `include` first -- keeping every value the review set.

    Two things change, and only these: a `why` still exactly as `old_triage` wrote it (the
    review did not touch it) becomes what `triage` writes now; and an entry that decides no
    file because every file it matches has an entry of its own is dropped. [metadata] keys take
    their one-word names. Hand-written comments are not kept (the values are). The rewrite is
    parsed back and compared before it replaces the file; the old file is kept as
    `decisions.toml.bak`.

    Returns `{"written", "dropped", "whys_updated", "error"}`."""
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
    """New sections for every file no entry decides yet, and for what those files need: a
    reshaping recipe for long-format files, and the station checks a recipe runs."""
    files = triage.get("files", [])
    everything = [f["path"] for f in files]
    recipes, recipe_of, station_lists = _recipe_proposals(files, decisions)
    new = [f for f in files if file_decision(decisions, f["path"]) is None]
    out = [] if "metadata" in decisions else [_metadata_section(triage)]
    # Under a heading each: what needs deciding, what is written, what is left out and why,
    # and what is only read -- so the outcome reads off the file before any one entry does.
    by_section: Dict[str, List[Tuple[str, List[Dict[str, Any]]]]] = {}
    for key, members in _groups(new, everything):
        by_section.setdefault(section_of(members[0]), []).append((key, members))
    for sec, _ in SECTIONS:
        if by_section.get(sec):
            out.append(heading(sec))
            out += [_entry(key, members, recipe_of)
                    for key, members in sorted(by_section[sec], key=lambda e: e[0])]
    out += recipes
    if recipes or decisions.get("profiles"):
        out += _station_checks(triage, decisions, files, station_lists)
    return out


# The [metadata] keys a reviewer fills in, one word each so none needs quoting -- the header
# parser reads `principal_investigator` as it reads "Principal investigator" -- and the WOD
# code list each is coded against, to name a code beside it.
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
    """The name a WOD code stands for in the code list of a [metadata] key -- from the cached
    list, never fetched for this -- or None."""
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
    """`[metadata]` and its keys: the values set (a code with its name beside it), the
    proposals offered to choose from, then the keys not set yet, commented out."""
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
    """The [metadata] proposal: what the accession's own ISO 19115 records state, applied
    where they state one value (the submitter said it), listed for the reviewer to choose
    where they state several (two platforms), and the rest offered as commented keys."""
    found = iso_metadata(triage)
    lines = [heading("metadata").rstrip("\n")] + METADATA_HELP
    if found:
        sources = ", ".join(sorted({src for v in found.values() for _, src in v}))
        lines += [f"# {line}" for line in _wrap(
            f"Proposed from the accession's ISO 19115 metadata -- check them ({sources}).", 92)]
    single = {k: v[0][0] for k, v in found.items() if len(v) == 1}
    several = {k: v for k, v in found.items() if len(v) > 1}
    return "\n".join(lines + metadata_lines(single, several)) + "\n"


_ISO = {"gmd": "http://www.isotc211.org/2005/gmd", "gco": "http://www.isotc211.org/2005/gco",
        "gmi": "http://www.isotc211.org/2005/gmi", "gmx": "http://www.isotc211.org/2005/gmx"}


def iso_metadata(triage: Dict[str, Any]) -> Dict[str, List[Tuple[str, str]]]:
    """{key: [(value, file)]} from the ISO 19115 records in the accession: the platforms
    (MI_Platform), the project and instrument-type keywords, and the principal investigator
    -- the [metadata] keys a reviewer would otherwise copy out by hand."""
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
        for platform in doc.iter(f"{{{_ISO['gmi']}}}MI_Platform"):
            text = next((e.text for e in platform.iter() if e.text and e.text.strip()), None)
            add("platform", text, src)
        for kw in doc.iter(f"{{{_ISO['gmd']}}}MD_Keywords"):
            kind = kw.find("gmd:type/gmd:MD_KeywordTypeCode", _ISO)
            kind = kind.get("codeListValue") if kind is not None else None
            # An instrument keyword names a TYPE ('CTD', 'sound velocimeter') from NCEI's
            # instrument-types thesaurus -- WOD's probe type, not the model a file names.
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
    return found


def _entry(key: str, members: List[Dict[str, Any]], recipe_of: Dict[str, str]) -> str:
    first = members[0]
    data = ("; ".join(dict.fromkeys(m["data"] for m in members))
            if first["action"] == "document" else first["data"])
    why = first["why"]
    recipe = recipe_of.get(first["path"])
    if recipe:
        why += f" (see [profiles.{recipe}])"
    names = [_name(m["path"]) for m in members]
    # Documentation needs no reason beyond its heading: it said 'read it, nothing to process'
    # under every readme and photo.
    if first["action"] == "document" and not first["include"]:
        why = None
    return section(item_header("files", key),
                   {"include": bool(first["include"]),
                    "dataset": first.get("dataset") if first["include"] else None,
                    "data": data, "why": why,
                    "drop": first.get("drop") or None if first["include"] else None},
                   comments=file_list(names))


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
    """(key, files) per entry. Alike files in one folder share an entry -- the same
    decision for the same reason; for documentation, any reason -- under a pattern that
    matches exactly them: thirty daily logs are one decision, not thirty."""
    buckets: Dict[tuple, List[Dict[str, Any]]] = {}
    for f in files:
        alike = (("document",) if f["action"] == "document" else
                 (bool(f["include"]), f.get("dataset"), f["data"], f["why"],
                  tuple(f.get("drop") or ())))
        buckets.setdefault((_parent(f["path"]),) + alike, []).append(f)
    # A file alone in its bucket gets an entry under its own path, and a path beats any
    # pattern: a pattern may take such a file in without deciding it.
    own = {members[0]["path"] for members in buckets.values() if len(members) == 1}
    out = []
    for members in buckets.values():
        out += _patterns(sorted(members, key=lambda m: m["path"]), everything, own)
    return out


_SEPARATORS = "_-. "


def _patterns(members: List[Dict[str, Any]], everything: List[str], own=frozenset()):
    """One pattern for the group when one matches exactly its files among their folder's;
    else one per leading name token (`RBRsolo_*`, `SBE37SM_*`), then per extension. A group
    that would still fall apart into single files takes the most specific pattern whose only
    other files have entries of their own (`own`): their path decides them, whatever
    pattern also matches."""
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
    """Patterns from the most general to the most specific, each cut at name separators so it
    reads as a person would write it: `*`, `*.nc`, `RBRsolo_*.nc`, `LeConte_*_CTD_*.nc`."""
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
    # A shared part in the middle only reads as a name under a shared start: `*_SN*.nc`
    # matches exactly but says nothing a person can check; `LeConte_*_CTD_*.nc` does.
    middles = [n[len(prefix):len(n) - len(suffix)] for n in names]
    shared = _shared_part(middles) if prefix else ""
    if shared:
        out.append(f"{at}{prefix}*{shared}*{suffix}")
    return out


def _shared_part(texts: List[str], shortest: int = 3) -> str:
    """The longest run of characters every text contains, trimmed to name separators where
    it has them ('kHz_ADCP_' -> '_ADCP_')."""
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


def _recipe_proposals(files: List[Dict[str, Any]], decisions: Dict[str, Any]):
    """([profiles] sections, {file: recipe name}, station lists named) for included
    long-format files that no recipe covers yet: they name the variable in a cell (`StNo,
    parm, depth, value`), so they are pivoted into one cast per station and located by the
    station list."""
    sidecars_by_folder: Dict[str, List[str]] = {}
    for f in files:
        if f["action"] == "sidecar":
            sidecars_by_folder.setdefault(_parent(f["path"]), []).append(f["path"])
    covered = {fp for r in (decisions.get("profiles") or {}).values()
               for fp in (r.get("files") or [])}
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for f in files:
        lf = f.get("long_format")
        entry = file_decision(decisions, f["path"])
        included = entry.get("include") if entry else f.get("include")
        if f["action"] != "stage" or not lf or not included or f["path"] in covered:
            continue
        groups.setdefault((_parent(f["path"]), lf["variable_column"], lf["value_column"]),
                          []).append(f)
    out, recipe_of, station_lists = [], {}, []
    names: set = set(decisions.get("profiles") or {})
    for (folder, var, val), members in groups.items():
        # Named for what the files share ('U2015-015_CTD_Temp', '_Salinity' -> u2015_015_ctd),
        # falling back to the folder.
        prefix = os.path.commonprefix([Path(m["path"]).stem for m in members]).rstrip("_-. ")
        name = base = _slug(prefix if len(prefix) >= 3 else (_name(folder) or "profiles"))
        n = 2
        while name in names:
            name, n = f"{base}_{n}", n + 1
        names.add(name)
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
        station_lists += sidecars[:1] if len(sidecars) == 1 else []
        for m in members:
            recipe_of[m["path"]] = name
        out.append(section(
            item_header("profiles", name),
            {"kind": "long",
             "files": [m["path"] for m in members],
             "keys": canonical,
             "key_aliases": {k: v for k, v in aliases.items() if v} or None,
             "cast_key": cast_key,
             "variable": var, "value": val,
             "columns": {v: f"{v} ({UNIT_PLACEHOLDER})" for v in variables},
             "station_list": sidecars[0] if len(sidecars) == 1 else PLACEHOLDER},
            comments=[f"How {len(members)} long-format file(s) in {folder or '.'}/ become "
                      f"casts: they name the variable in the '{var}' column, so they are "
                      f"pivoted into one cast per '{cast_key}' and located by the station "
                      f"list.",
                      "TO FILL IN before confirming: replace every UNIT? with the unit the "
                      "readme or ISO metadata states (unit detection reads only these names), "
                      "and check station_list.",
                      "Optional: dataset_type = \"CTD\", time_zone = \"UTC+10\", "
                      "notes = [...], on_conflict = \"first\", skip_casts = [...], "
                      "station_map = { \"<cast>\" = \"<station>\" }"]
                     + ([f"More than one station list here: {sidecars}; pick one."]
                        if len(sidecars) > 1 else
                        ["No station list found beside these files; name the file that "
                         "locates the casts."] if not sidecars else [])))
    return out, recipe_of, station_lists


def _station_checks(triage: Dict[str, Any], decisions: Dict[str, Any],
                    files: List[Dict[str, Any]], proposed_lists: List[str]) -> List[str]:
    """The sections a reshaping recipe's station list is checked with: header values for the
    staged casts, the accession's bounding box, and other sources of station positions."""
    station_lists = set(proposed_lists) | {r.get("station_list") for r in
                                           (decisions.get("profiles") or {}).values()}
    out = []
    if "track" not in decisions:
        box = _bounding_box(triage)
        out.append(section(
            "track", {"bounding_box": box, "outlier_factor": 1.75},
            comments=["The station-track check flags a station outside the box, a hop between "
                      "stations over outlier_factor x the median hop, and time running "
                      "backwards. [lon_min, lon_max, lat_min, lat_max]"
                      + (" (from NCEI's _lonlat.txt)" if box else "")],
            commented_fields={} if box else {"bounding_box": [0.0, 0.0, 0.0, 0.0]}))
    if not decisions.get("cross_check"):
        # Only what read_station_list can read -- a table, or a PDF table with a row pattern --
        # and never the station lists being checked.
        tables = [f["path"] for f in files
                  if f["action"] in ("exclude", "sidecar") and f.get("columns")
                  and f.get("reader") in ("delimited", "fixed_width", "spreadsheet")
                  and f["path"] not in station_lists
                  and any(re.match(r"^lat", str(c), re.I) for c in f["columns"])]
        pdfs = [f["path"] for f in files if f["format"] == "pdf"]
        if tables or pdfs:
            lines = ["# Other sources to check station positions and dates against. Uncomment "
                     "and adjust; each is compared with the station list of every recipe.",
                     "# Candidates found: " + ", ".join(tables + pdfs)]
            if tables:
                lines += ["# [[cross_check]]", f"# name = {toml_value(tables[0])}",
                          f"# file = {toml_value(tables[0])}",
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
    """(open items, plan). The plan is only usable when no open item is blocking: every file
    decided, every included one with a WOD dataset, every recipe it needs complete, every
    station conflict settled."""
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
            items.append(open_item(hdr, f"{key}: decides no file -- every file it matches has "
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
                items.append(open_item(hdr, f"{key}: is included, but nothing says which WOD "
                                            f"dataset it is",
                                       "set dataset = one of " + ", ".join(sorted(datasets))))

    for name, r in (decisions.get("profiles") or {}).items():
        if name not in plan["profiles"]:
            continue                     # every file it reshapes is left out
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
    return items, plan


def build_plan(decisions: Dict[str, Any], triage: Dict[str, Any],
               conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What `wod-etl confirm` will do, resolved from triage and the decisions: the files it
    processes as they stand, the recipes it stages, and the files it leaves out and why.

    Every included file is written as the `dataset` its entry names -- the reviewed decision
    is the contract, and processing must not classify again from less evidence (it does not
    see a NetCDF file's instrument attributes). CTD still routes cast by cast to OSD when a
    cast's levels are coarse (manual p.19): that is the dataset's own rule."""
    files = {f["path"]: f for f in triage.get("files", [])}
    keys = {p: decision_key(decisions, p) for p in files}
    decided = {p: (decisions.get("files") or {})[k] if k is not None else {}
               for p, k in keys.items()}
    included = {p for p, e in decided.items() if e.get("include") is True}
    # The decisions as the person reads them, one row per entry, for the run summary.
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
        if r.get("kind") == "script":
            profiles[name] = dict(r)
        elif [f for f in r.get("files") or [] if f in included]:
            profiles[name] = {**r, "files": [f for f in r["files"] if f in included]}
    consumed = set()
    for r in profiles.values():
        consumed |= set(r.get("files") or [])
        if r.get("station_list") and r["station_list"] != PLACEHOLDER:
            consumed.add(r["station_list"])
    direct = sorted(p for p in included if p not in consumed)
    overrides = {p: decided[p]["dataset"] for p in direct if decided[p].get("dataset")}
    # What else the review says about how an included file is written: variables to leave
    # out, and header values for its casts on top of the accession's [metadata].
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
    return {"direct": direct, "dataset_overrides": overrides, "file_options": options,
            "left_out": left_out,
            "entries": sorted(entries.values(), key=lambda e: (not e["include"],
                                                                e["documentation"], e["key"])),
            "undecided": sorted(p for p in files if not decided[p] and p not in consumed),
            "profiles": profiles, "consumed": sorted(consumed),
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
