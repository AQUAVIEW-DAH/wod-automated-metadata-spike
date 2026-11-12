"""Rebuild cf_standard_names.json from the CF Standard Name Table XML.

Maintainer tool, re-run when a new table is published; the pipeline reads only the JSON.
    uv run python -m wod_etl.reference.extract_cf_standard_names [xml path or URL]
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from wod_etl.config import CF_STANDARD_NAMES_JSON, HTTP_TIMEOUT, USER_AGENT

TABLE_URL = ("https://cfconventions.org/Data/cf-standard-names/current/src/"
             "cf-standard-name-table.xml")


def _xml(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        import httpx
        r = httpx.get(source, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        return r.content
    return Path(source).read_bytes()


def build(source: str = TABLE_URL) -> dict:
    """Names with their canonical units, and each alias resolved to a current name."""
    root = ET.fromstring(_xml(source))
    units = {e.get("id"): (e.findtext("canonical_units") or "").strip()
             for e in root.iter("entry")}
    aliases = {}
    for a in root.iter("alias"):
        targets = [t.text.strip() for t in a.findall("entry_id") if t.text]
        # An alias split into several names has no single replacement.
        if len(targets) == 1:
            aliases[a.get("id")] = targets[0]
    # Follow alias chains to a name that is still in the table.
    for old, new in list(aliases.items()):
        seen = {old}
        while new in aliases and new not in units and new not in seen:
            seen.add(new)
            new = aliases[new]
        if new in units:
            aliases[old] = new
        else:
            del aliases[old]
    return {
        "table": "CF Standard Name Table",
        "version": int(root.findtext("version_number")),
        "last_modified": root.findtext("last_modified"),
        "source": source if source.startswith("http") else TABLE_URL,
        "note": "canonical_units: every current standard name and its canonical units. "
                "aliases: deprecated names and the current name each was replaced by.",
        "canonical_units": dict(sorted(units.items())),
        "aliases": dict(sorted(aliases.items())),
    }


def write(source: Optional[str] = None) -> dict:
    """Build and write cf_standard_names.json. Returns a status dict; never raises."""
    try:
        data = build(source or TABLE_URL)
    except Exception as e:                              # noqa: BLE001 - reported to the caller
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    CF_STANDARD_NAMES_JSON.write_text(json.dumps(data, indent=1) + "\n")
    return {"status": "written", "path": str(CF_STANDARD_NAMES_JSON),
            "version": data["version"], "names": len(data["canonical_units"]),
            "aliases": len(data["aliases"])}


if __name__ == "__main__":
    import sys

    result = write(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["status"] == "error" else 0)
