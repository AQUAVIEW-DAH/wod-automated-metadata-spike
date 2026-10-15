"""Resolve a coded metadata value against WOD's published code list.

A coded field holds a CODE. `Platform Code` is 4959, not 'Shunyo-Maru'; `Instrument` is 411,
not 'SBE 911plus'. Writing the originator's text into a coded field produces a file that
validates and that no WOD tool can read back -- so this looks the text up, and only a value
that resolves to exactly ONE code is treated as that code.

    from wod_etl.reference.code_lists import lookup_code

    lookup_code("secondary_header", 3, "Shunyo-Maru")
    # {"status": "ambiguous", "candidates": [{"code": "4959", "label": "SHUNYO MARU"},
    #                                        {"code": "10793", "label": "SHUNYO MARU (R/V;..."}]}

The lists are fetched from NCEI on first use and cached under
`config.CODE_LIST_CACHE_DIR`, with the date they were retrieved -- they change between WOD
releases, so every result says which copy it was checked against. A stale cache is refetched
after `config.CODE_LIST_MAX_AGE_DAYS`; if that fails the stale copy is used and says so.
Nothing raises: with no network and no cache the status is `unavailable`.

Statuses, and what a caller does with each:

| status            | meaning                                           | write              |
| ---               | ---                                               | ---                |
| `matched`         | exactly one entry's name equals the value         | the code           |
| `already_a_code`  | the value is a number in the list (or a number)   | the value          |
| `ambiguous`       | several entries share the name                    | text + candidates  |
| `no_match`        | no entry has the name; `possible` lists near ones | text + possible    |
| `unavailable`     | the list could not be read                        | text, unverified   |
| `not_coded`       | the field has no code list                        | the value as-is    |
| `disabled`        | lookups are switched off (`WOD_CODE_LOOKUP=0`)     | text, unverified   |
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
    """The CSV for a field's code list. The manual links some lists only as PDF or HTML;
    NCEI publishes the CSV beside them under the same field name."""
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

    `entries` is a list of `{"code", "label", "names", ...}`: `names` are the normalised
    forms a value is compared against (the label, its synonyms, the part after a
    `Category:` prefix).
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
            if stamp.tzinfo is None:          # a hand-edited '2026-01-01' is naive
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
    """Normalised name -> the entries carrying it, in list order. Built once per list, so a
    lookup is a dict hit rather than a scan of every entry (the platform list has >10,000)."""
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

    Most lists are `code_name,<Title>` then `units,<code system>` then `code,label`. The taxa
    list is `ITIS TSN,PGC code,Rank,Taxonomic Name -[ Author ]-`. A label may hold several
    synonyms separated by runs of spaces (`ug/l    mg/m3    ppb`), and a `Category: name`
    prefix (`Temperature: ITS-90`); each is a name the value may match.
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
            # The head before a description: 'COPEPODITE(S) sum of various ... stages' is
            # named 'COPEPODITE'; 'SHUNYO MARU (R/V; ... renamed ...)A' is 'SHUNYO MARU'.
            head = part.split("(", 1)[0].strip()
            if head and head != part:
                names.add(normalise_label(head))
                if ":" in head:
                    names.add(normalise_label(head.split(":", 1)[1]))
        names.discard("")
        # An entry the list itself retires is no answer: 'KAPITAN DRANITSYN ... DO NOT
        # USE' made the ship's name ambiguous with the code to use.
        if re.search(r"\bdo\s+not\s+use\b", label, re.IGNORECASE):
            names = set()
        entry = {"code": code, "label": re.sub(r"\s+", " ", label), "names": sorted(names)}
        if header is not None:
            for name, value in zip(header[1:-1], r[1:-1]):
                entry[name] = value.strip()
        entries.append(entry)
    return entries


# Lists not downloaded just to confirm a number is in them. The accession list is every NCEI
# accession there is: fetching it to verify one number cost each run a minute and a half of
# timeouts, and NCEI assigned the number in the first place. A cached copy is still used.
_NOT_FETCHED_TO_VERIFY = {("secondary_header", 1)}


def lookup_code(namespace: str, field_id: int, value: Any, *,
                fetch: Optional[bool] = None) -> Dict[str, Any]:
    """Resolve one value against a field's code list. See the module docstring for statuses."""
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
    if not hits:
        # 'larvae' is the list's 'LARVA', 'nauplii' its 'NAUPLIUS'. Plural endings only.
        for alt in _singulars(want):
            hits = list(index.get(alt, []))
            if hits:
                break
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
        # 'CTD' names a kind of instrument, and every entry of that kind starts with it:
        # offering two of them as the choice points at the wrong answer.
        if len(re.sub(r"[^A-Za-z0-9]", "", str(result.get("value") or ""))) <= 4:
            return (f"{result.get('value')!r} is too general to code{where}{when}: name the "
                    f"model (e.g. 'SBE 19plus') in decisions.toml [metadata] Instrument")
        return (f"ambiguous{where}{when}: " + "; ".join(
            f"{c['code']} {c['label']}" for c in result.get("candidates", [])[:6])
            + " -- choose one with the submitter")
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
