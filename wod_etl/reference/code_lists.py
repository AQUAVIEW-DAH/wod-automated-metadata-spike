"""Resolve coded metadata values against WOD's published code lists.

Only text matching exactly one entry becomes a code. Lists are fetched from NCEI, cached with
their retrieval date under `config.CODE_LIST_CACHE_DIR`, and refetched after
`config.CODE_LIST_MAX_AGE_DAYS`. Lookups never raise.
"""
import csv
import datetime as dt
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl import config
from wod_etl.reference.load_tables import code_table_link

CSV_BASE = "https://www.ncei.noaa.gov/data/oceans/woa/WOD/CODES/CSV/"

_MEMO: Dict[str, Dict[str, Any]] = {}


def code_list_url(namespace: str, field_id: int) -> Optional[str]:
    """The CSV URL for a field's code list; NCEI publishes it beside the PDF or HTML."""
    link = code_table_link(namespace, field_id)
    if not link:
        return None
    return link.get("csv") or f"{CSV_BASE}{link['field']}.csv"


def _cache_paths(field: str):
    d = Path(config.CODE_LIST_CACHE_DIR)
    return d / f"{field}.csv", d / f"{field}.json"


def load_code_list(namespace: str, field_id: int, *, fetch: Optional[bool] = None,
                   max_age_days: Optional[float] = None) -> Dict[str, Any]:
    """`{"status", "entries", "url", "retrieved", "field"}` for one field's code list.

    `entries` items are `{"code", "label", "names", ...}`; `names` are the normalised forms
    a value is matched against.
    """
    link = code_table_link(namespace, field_id)
    if not link:
        return {"status": "not_coded", "entries": [], "url": None, "field": None}
    field = link["field"]
    if field in _MEMO:
        return _MEMO[field]
    url = code_list_url(namespace, field_id)
    fetch = config.CODE_LIST_FETCH if fetch is None else fetch
    max_age = config.CODE_LIST_MAX_AGE_DAYS if max_age_days is None else max_age_days
    csv_path, meta_path = _cache_paths(field)

    cached_text, retrieved = None, None
    if csv_path.exists():
        cached_text = csv_path.read_text(encoding="utf-8", errors="replace")
        try:
            retrieved = json.loads(meta_path.read_text()).get("retrieved")
        except (OSError, ValueError, AttributeError):
            retrieved = None
    age_days = None
    if retrieved:
        try:
            stamp = dt.datetime.fromisoformat(str(retrieved))
            if stamp.tzinfo is None:          # naive dates are UTC
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            age_days = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 86400
        except (TypeError, ValueError):
            age_days = None

    status, error = "ok", None
    if fetch and (cached_text is None or age_days is None or age_days > max_age):
        try:
            from wod_etl.http_util import get_with_retries
            text = get_with_retries(url).text
            if not text.lstrip().startswith(("<", "<!")):
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                csv_path.write_text(text, encoding="utf-8")
                retrieved = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                meta_path.write_text(json.dumps({"url": url, "retrieved": retrieved}))
                cached_text = text
            else:
                error = "the server returned a web page, not a CSV"
        except Exception as e:                       # noqa: BLE001 - offline is a status
            error = f"{type(e).__name__}: {e}"
        if error and cached_text is not None:
            status = "stale"
    elif cached_text is not None and age_days is not None and age_days > max_age:
        status = "stale"

    if cached_text is None:
        result = {"status": "unavailable", "entries": [], "url": url, "field": field,
                  "retrieved": None,
                  "error": error or ("not cached, and fetching is off "
                                     "(config.CODE_LIST_FETCH / WOD_CODE_LIST_FETCH)")}
    else:
        entries = parse_code_list(cached_text)
        result = {"status": status, "entries": entries, "url": url,
                  "field": field, "retrieved": retrieved, "error": error,
                  "index": _name_index(entries),
                  "codes": {e["code"].lstrip("0") for e in entries}}
    _MEMO[field] = result
    return result


def _name_index(entries: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Normalised name -> the entries carrying it, in list order."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        for n in e["names"]:
            index.setdefault(n, []).append(e)
    return index


def clear_cache_memo() -> None:
    """Forget lists loaded in this process (tests, or after editing the cache by hand)."""
    _MEMO.clear()


def normalise_label(text: str) -> str:
    """Upper-case, letters and digits only: 'Shunyo-Maru' and 'SHUNYO MARU' are 'SHUNYOMARU'."""
    s = re.sub(r"\(.*?\)|-\[.*?\]-", " ", str(text))
    s = re.sub(r"\bR\s*/\s*V\b|\bRV\b", " ", s, flags=re.I)
    return re.sub(r"[^A-Z0-9]+", "", s.upper())


def _tokens(text: str) -> List[str]:
    s = re.sub(r"\(.*?\)|-\[.*?\]-", " ", str(text))
    return [t for t in re.split(r"[^A-Z0-9]+", s.upper()) if t]


def parse_code_list(text: str) -> List[Dict[str, Any]]:
    """Parse either published layout into entries.

    Most lists are `code_name,<Title>`, `units,<system>`, then `code,label`; the taxa list is
    `ITIS TSN,PGC code,Rank,Taxonomic Name`. Synonyms separated by runs of spaces and the
    part after a `Category:` prefix are also names.
    """
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    entries = []
    if rows[0][0].strip().lower() == "code_name":
        body = rows[1:]
        if body and body[0][0].strip().lower().startswith("unit"):
            body = body[1:]
        header = None
    else:
        header = [c.strip() for c in rows[0]]
        body = rows[1:]
    for r in body:
        code = r[0].strip()
        if not code:
            continue
        label = ",".join(r[1:]).strip() if header is None else r[-1].strip()
        names = set()
        parts = [label] + re.split(r"\s{2,}", label)
        # 'Fish -> pisces' (a WOD group), each side a name.
        parts += [p for part in list(parts) if "->" in part for p in part.split("->")]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            names.add(normalise_label(part))
            if ":" in part:
                names.add(normalise_label(part.split(":", 1)[1]))
            # The head before a parenthesised description is also a name.
            head = part.split("(", 1)[0].strip()
            if head and head != part:
                names.add(normalise_label(head))
                if ":" in head:
                    names.add(normalise_label(head.split(":", 1)[1]))
        names.discard("")
        # Entries marked "do not use" are never matched.
        if re.search(r"\bdo\s+not\s+use\b", label, re.IGNORECASE):
            names = set()
        entry = {"code": code, "label": re.sub(r"\s+", " ", label), "names": sorted(names),
                 "synonyms": [x.strip() for x in re.split(r"\s{2,}", label) if x.strip()]}
        if header is not None:
            for name, value in zip(header[1:-1], r[1:-1]):
                entry[name] = value.strip()
        entries.append(entry)
    return entries


# Lists too large to download only to verify a number; a cached copy is still used.
_NOT_FETCHED_TO_VERIFY = {("secondary_header", 1)}


def lookup_code(namespace: str, field_id: int, value: Any, *,
                fetch: Optional[bool] = None) -> Dict[str, Any]:
    """Resolve one value against a field's code list.

    Statuses: `matched`, `already_a_code`, `ambiguous` (with `candidates`), `no_match` (with
    `possible`), `unavailable`, `not_coded`, `disabled`.
    """
    link = code_table_link(namespace, field_id)
    if not link:
        return {"status": "not_coded"}
    text = str(value).strip() if value is not None else ""
    base = {"field": link["field"], "value": text}
    if not text:
        return {**base, "status": "no_match", "candidates": [], "possible": []}

    numeric = re.fullmatch(r"\d+(?:\.0*)?", text)
    if not config.CODE_LOOKUP:
        return {**base, "status": "already_a_code" if numeric else "disabled",
                "code": text.rstrip(".").split(".")[0] if numeric else None}

    if numeric and (namespace, field_id) in _NOT_FETCHED_TO_VERIFY:
        fetch = False
    listing = load_code_list(namespace, field_id, fetch=fetch)
    base.update(code_list=listing.get("url"), retrieved=listing.get("retrieved"),
                list_status=listing.get("status"))
    if numeric:
        code = text.rstrip(".").split(".")[0]
        known = code.lstrip("0") in (listing.get("codes") or set())
        return {**base, "status": "already_a_code", "code": code,
                "verified": known if listing["entries"] else None}
    if not listing["entries"]:
        return {**base, "status": "unavailable", "error": listing.get("error")}

    index = listing.get("index") or {}
    want = normalise_label(text)
    hits = list(index.get(want, [])) if want else []
    if not hits and _name_part(text) != text:
        # The name alone, without identifiers after it.
        hits = list(index.get(normalise_label(_name_part(text)), []))
    if not hits:
        # Try singulars ('larvae' -> 'LARVA').
        for alt in _singulars(want):
            hits = list(index.get(alt, []))
            if hits:
                break
    codes = list(dict.fromkeys(e["code"] for e in hits))
    if len(codes) > 1:
        # One name, several entries: an identifier or brand in the value may pick one.
        hits = _sharing_an_identifier(text, hits) or _by_brand(text, hits) or hits
        codes = list(dict.fromkeys(e["code"] for e in hits))
    if len(codes) == 1:
        e = hits[0]
        return {**base, "status": "matched", "code": e["code"], "label": e["label"],
                **({k: v for k, v in e.items() if k not in ("code", "label", "names")})}
    if len(codes) > 1:
        return {**base, "status": "ambiguous",
                "candidates": [{"code": e["code"], "label": e["label"]} for e in hits][:20]}

    toks = set(_tokens(text))
    possible = [e for e in listing["entries"]
                if toks and toks <= set(_tokens(e["label"]))][:10]
    return {**base, "status": "no_match", "candidates": [],
            "possible": [{"code": e["code"], "label": e["label"]} for e in possible]}


_IDENTIFIER_WORDS = {"CALL", "SIGN", "CODE", "ICES", "IMO", "BUILT", "SERVICE", "DATE"}


def _by_brand(text: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Among entries differing only by maker ('XBT: T7 (SIPPICAN)', 'XBT: T7 (UNKNOWN
    BRAND)'), the one the value names, else the unknown-brand one."""
    stated = re.sub(r"[^A-Z0-9]+", " ", str(text).upper()).split()
    maker = lambda e: (re.findall(r"\(([^)]*)\)", e["label"]) or [""])[0].upper()
    named = [e for e in hits if maker(e).split() and maker(e).split()[0] in stated]
    if len({e["code"] for e in named}) == 1:
        return named
    unknown = [e for e in hits if maker(e).startswith("UNKNOWN")]
    return unknown if not named and len(unknown) == 1 else []


def _name_part(text: str) -> str:
    """The name before any identifiers: 'Oshoro Maru (call sign 7JRM)' -> 'Oshoro Maru'."""
    return re.split(r"[,;(\[]|\s[-\u2013]\s", str(text), maxsplit=1)[0].strip()


def _sharing_an_identifier(text: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The one candidate sharing an identifier stated beside the name, else []."""
    raw = lambda t: set(re.split(r"[^A-Z0-9]+", str(t).upper())) - {""}
    name = raw(_name_part(text))
    extra = {t for t in raw(text) - name - _IDENTIFIER_WORDS
             if len(t) >= 4 and any(ch.isdigit() for ch in t)}
    if not extra:
        return []
    # 'IMO8401365' in a label, '8401365' in a value: compare digit runs too.
    digits = lambda ts: ts | {re.sub(r"^[A-Z]+", "", t) for t in ts}
    found = [e for e in hits if digits(raw(e["label"])) & digits(extra)]
    return found if len({e["code"] for e in found}) == 1 else []


# ------------------------------------------------------------- units (v_8)
def unit_key(text: Any) -> str:
    """A unit as a comparable string: 'µmol kg−1' and 'micromol per kilogram' are 'umol/kg'."""
    from wod_etl.mapping.query_wod_spec import canonical_unit_text

    s = canonical_unit_text(str(text)).lower().replace("\u00b5", "u").replace("_", " ")
    s = s.replace("\u2219", ".").strip()
    s = re.sub(r"\bper\s*mill?e\b|\u2030|\bper\s*mil\b", "permille", s)
    for word, short in (("micromol(?:e)?s?", "umol"), ("nanomol(?:e)?s?", "nmol"),
                        ("picomol(?:e)?s?", "pmol"), ("millimol(?:e)?s?", "mmol"),
                        ("(?:micro|u)grams?", "ug"), ("(?:milli|m)grams?", "mg"),
                        ("kilograms?", "kg"), ("lit(?:er|re)s?", "l"), (r"degrees?\s*", "deg")):
        s = re.sub(rf"\b{word}\b", short, s)
    s = re.sub(r"\s*\bper\b\s*", "/", s)
    s = re.sub(r"[\s.*]*([a-z]+)\^?-(\d)", lambda m: f"/{m.group(1)}" +
               ("" if m.group(2) == "1" else m.group(2)), s)
    return re.sub(r"[\s\u00b0]+", "", s)


# A qualifier that limits an originator-unit code to some variables, or marks an alternate.
_UNIT_ONLY_FOR = [(re.compile(r"alkalinity\s+only", re.I), {17}),
                  (re.compile(r"only\s+for\s+cfcs?", re.I), {40, 41, 42})]
_UNIT_ALTERNATE = re.compile(r"\balternate\b|instead\s+of", re.I)


def _unit_synonyms(synonyms: List[str]):
    """(unit keys, qualifier text) of one originator-units entry."""
    keys, notes = set(), []
    for part in synonyms:
        inner = re.findall(r"\((.*?)\)", part)
        base = re.sub(r"\(.*?\)", " ", part).strip()
        if _UNIT_ALTERNATE.search(part) or any(w.search(part) for w, _ in _UNIT_ONLY_FOR):
            notes.append(part)
            base = re.split(r"\s+(?=alternate|\()", base, flags=re.I)[0]
        if base:
            keys.add(unit_key(base))
        keys |= {unit_key(i) for i in inner if re.fullmatch(r"[\w/%.\- ]{1,12}", i)
                 and not re.search(r"only|use|for", i, re.I)}
    return keys - {""}, " ".join(notes)


def lookup_unit(value: Any, wod_code: Optional[int], *,
                fetch: Optional[bool] = None) -> Dict[str, Any]:
    """An originator's unit as a v_8_orig_units code, for the variable it measures.

    Statuses as `lookup_code`, plus `standard_unit` for the variable's own WOD unit.
    """
    text = str(value or "").strip()
    base = {"field": "v_8_orig_units", "value": text}
    if text and is_standard_unit(text, wod_code):
        return {**base, "status": "standard_unit"}
    if not config.CODE_LOOKUP:
        return {**base, "status": "disabled"}
    listing = load_code_list("variable_specific_header", 8, fetch=fetch)
    base.update(code_list=listing.get("url"), retrieved=listing.get("retrieved"))
    if not listing["entries"]:
        return {**base, "status": "unavailable", "error": listing.get("error")}
    want = unit_key(text)
    hits = []
    for e in listing["entries"]:
        synonyms = e.get("synonyms") or [e["label"]]
        keys, notes = _unit_synonyms(synonyms)
        if want not in keys:
            continue
        only = [codes for w, codes in _UNIT_ONLY_FOR if w.search(notes)]
        if only and wod_code not in set().union(*only):
            continue
        rank = (0 if only else 1 if not _UNIT_ALTERNATE.search(notes) else 2,
                0 if unit_key(synonyms[0]) == want else 1)
        hits.append((rank, e))
    if not hits:
        return {**base, "status": "no_match", "candidates": [], "possible": []}
    best = min(r for r, _ in hits)
    top = [e for r, e in hits if r == best]
    if len({e["code"] for e in top}) == 1:
        return {**base, "status": "matched", "code": top[0]["code"], "label": top[0]["label"]}
    return {**base, "status": "ambiguous",
            "candidates": [{"code": e["code"], "label": e["label"]} for e in top][:20]}


# Spellings of the standard units Table 3a abbreviates differently or leaves blank.
_STANDARD_SPELLINGS = {1: {"degc", "c", "celsius", "degcits-90", "its-90", "degcelsius"},
                       2: {"", "psu", "pss", "pss-78", "1", "dimensionless", "unitless", "1e-3"},
                       9: {"", "1", "dimensionless", "unitless"},
                       25: {"dbar", "decibar", "decibars", "db"},
                       26: {"degc", "c", "celsius"}, 29: {"mbar", "hpa", "millibar"}}


def is_standard_unit(text: str, wod_code: Optional[int]) -> bool:
    """Whether `text` is the WOD standard unit of variable `wod_code` (Table 3a)."""
    from wod_etl.reference.load_tables import wod_variables

    var = wod_variables().get(wod_code) or {}
    key = unit_key(text)
    return bool(var) and (key == unit_key(var.get("unit_abbrev", "")) or
                          key in _STANDARD_SPELLINGS.get(wod_code, set()))


_TITLES = {"DR", "PROF", "MR", "MRS", "MS", "SIR", "CAPT"}


def _person(name: str):
    """(surname, first initial) from 'Jane A. Smith', 'Smith, Jane' or WOD's 'SMITH; JANE A.'."""
    text = re.sub(r"^\s*\d+\s+", "", str(name)).upper()
    if ";" in text or "," in text:
        surname, given = re.split(r"[;,]", text, maxsplit=1)
    else:
        parts = text.split()
        surname, given = (parts[-1], " ".join(parts[:-1])) if parts else ("", "")
    given = [t for t in re.findall(r"[A-Z]+", given) if t not in _TITLES]
    return re.sub(r"[^A-Z]", "", surname), (given[0][0] if given else "")


def lookup_investigator(name: Any, *, fetch: Optional[bool] = None) -> Dict[str, Any]:
    """A principal investigator's WOD code (primary_investigator_list, manual p.30).

    Matched on surname and first initial; only a single match becomes a code. Several names
    in one value are not resolved.
    """
    text = str(name or "").strip()
    base = {"field": "primary_investigator_list", "value": text}
    if not text or re.search(r"\band\b|&|/", text, re.IGNORECASE):
        return {**base, "status": "no_match", "candidates": [], "possible": []}
    if not config.CODE_LOOKUP:
        return {**base, "status": "disabled"}
    listing = load_code_list("character_data", 3, fetch=fetch)
    base.update(code_list=listing.get("url"), retrieved=listing.get("retrieved"))
    if not listing["entries"]:
        return {**base, "status": "unavailable", "error": listing.get("error")}
    surname, initial = _person(text)
    hits = [e for e in listing["entries"]
            if _person(e["label"]) == (surname, initial) and surname]
    if len({e["code"] for e in hits}) == 1:
        return {**base, "status": "matched", "code": hits[0]["code"], "label": hits[0]["label"]}
    if hits:
        return {**base, "status": "ambiguous",
                "candidates": [{"code": e["code"], "label": e["label"]} for e in hits][:20]}
    return {**base, "status": "no_match", "candidates": [], "possible": []}


def _singulars(word: str) -> List[str]:
    out = []
    if word.endswith("AE"):
        out.append(word[:-1])                  # LARVAE -> LARVA
    if word.endswith("II"):
        out.append(word[:-2] + "IUS")          # NAUPLII -> NAUPLIUS
    if word.endswith("ES"):
        out.append(word[:-2])
    if word.endswith("S"):
        out.append(word[:-1])
    return [w for w in out if w]


def describe_lookup(result: Dict[str, Any]) -> str:
    """One line for a NetCDF attribute or a report."""
    st = result.get("status")
    where = f" in {result.get('field')}" if result.get("field") else ""
    when = f" (list retrieved {result['retrieved']})" if result.get("retrieved") else ""
    if st == "matched":
        return f"matched {result['code']} {result.get('label', '')}{where}{when}"
    if st == "ambiguous":
        # A short generic name ('CTD') prefixes many entries; ask for the model instead.
        if len(re.sub(r"[^A-Za-z0-9]", "", str(result.get("value") or ""))) <= 4:
            return (f"{result.get('value')!r} is too general to code{where}{when}: name the "
                    f"model (e.g. 'SBE 19plus') in decisions.toml [metadata] Instrument")
        return (f"ambiguous{where}{when}: " + "; ".join(
            f"{c['code']} {c['label']}" for c in result.get("candidates", [])[:6])
            + ". Choose one with the submitter")
    if st == "no_match":
        near = result.get("possible") or []
        return (f"no entry named {result.get('value')!r}{where}{when}"
                + (": possibly " + "; ".join(f"{c['code']} {c['label']}" for c in near[:4])
                   if near else ""))
    if st == "unavailable":
        return f"not looked up: code list unavailable ({result.get('error')})"
    if st == "disabled":
        return "not looked up: code lookups are off (WOD_CODE_LOOKUP=0)"
    return str(st)
