"""The CF Standard Name Table, loaded from knowledge/cf_standard_names.json."""
import functools
import json
from typing import Any, Dict, Optional

from wod_etl.config import CF_STANDARD_NAMES_JSON


@functools.lru_cache(maxsize=None)
def cf_standard_name_table() -> Dict[str, Any]:
    """The whole table: version, canonical units by name, and aliases."""
    if not CF_STANDARD_NAMES_JSON.exists():
        raise FileNotFoundError(
            f"Missing {CF_STANDARD_NAMES_JSON}. Rebuild it with "
            f"`uv run python -m wod_etl.reference.extract_cf_standard_names`.")
    return json.loads(CF_STANDARD_NAMES_JSON.read_text())


@functools.lru_cache(maxsize=None)
def _by_lower() -> Dict[str, str]:
    """Current names and aliases, lower-cased, to the current name."""
    t = cf_standard_name_table()
    return {**{k.lower(): v for k, v in t["aliases"].items()},
            **{n.lower(): n for n in t["canonical_units"]}}


def resolve_standard_name(name) -> Optional[str]:
    """The current standard name for `name` (an alias becomes its replacement); None when
    the table has no such name. Case is ignored, as originators' files vary."""
    return _by_lower().get(str(name or "").strip().lower())


def canonical_units(name: str) -> Optional[str]:
    """The canonical units of a current standard name."""
    return cf_standard_name_table()["canonical_units"].get(name)


def is_alias(name: str) -> bool:
    """True when `name` is a deprecated alias rather than a current name."""
    return name in cf_standard_name_table()["aliases"]


def standard_name_vocabulary() -> str:
    """The ACDD `standard_name_vocabulary` value: 'CF Standard Name Table v95'."""
    return f"CF Standard Name Table v{cf_standard_name_table()['version']}"
