"""WOD country codes (Appendix 1, p.68): the primary header's two-character country code."""
import functools
import re
from typing import Any, Dict

from wod_etl.reference.load_tables import load_table

# Unambiguous common names the appendix spells differently.
_ALIASES = {
    "UK": "GB", "UNITEDKINGDOM": "GB", "ENGLAND": "GB", "SCOTLAND": "GB", "WALES": "GB",
    "USA": "US", "UNITEDSTATESOFAMERICA": "US",
    "RUSSIA": "RU", "USSR": "SU", "SOUTHKOREA": "KR",
    "WESTGERMANY": "DE", "FEDERALREPUBLICOFGERMANY": "DE", "GERMANDEMOCRATICREPUBLIC": "DU",
    "PEOPLESREPUBLICOFCHINA": "CN",
}


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


@functools.lru_cache(maxsize=None)
def country_codes() -> Dict[str, str]:
    """{code: name} from Appendix 1, plus the codes WOD has added online since."""
    table = load_table("appendix_1_country_codes")
    return {**table["codes"], **(table.get("online_additions") or {}).get("codes", {})}


@functools.lru_cache(maxsize=None)
def _names() -> Dict[str, str]:
    """Normalised name -> code. 'Korea; Republic of' is also 'Republic of Korea'."""
    out = dict(_ALIASES)
    for code, name in country_codes().items():
        out[_key(name)] = code
        if ";" in name:
            head, tail = (part.strip() for part in name.split(";", 1))
            out[_key(head)] = out.get(_key(head), code)
            out[_key(f"{tail} {head}")] = code
    return out


def resolve_country(value: Any) -> Dict[str, Any]:
    """A stated country as its WOD code: `{"status", "code", "label", "value"}`.

    Statuses: `already_a_code` (a listed code), `matched` (a name), `no_match`.
    """
    text = str(value or "").strip()
    codes = country_codes()
    if text.upper() in codes:
        code = text.upper()
        return {"status": "already_a_code", "code": code, "label": codes[code], "value": text}
    code = _names().get(_key(text))
    if code:
        return {"status": "matched", "code": code, "label": codes[code], "value": text}
    return {"status": "no_match", "code": None, "label": None, "value": text}
