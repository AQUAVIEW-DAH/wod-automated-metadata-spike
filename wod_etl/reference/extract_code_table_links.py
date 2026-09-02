"""Rebuild knowledge/wod_code_tables/code_table_links.json from the manual's own hyperlinks.

The WOD manual defines the metadata FIELDS in Tables 4a, 5a, 6a and 7a but publishes the
VALUE code lists online, as a hyperlink beside each entry. Those links are PDF annotations,
so they survive neither a text extraction nor a copy-paste -- which is why the "App" column
looks like a dead end when you read the extracted text.

    uv run python -m wod_etl.reference.extract_code_table_links

Re-run it after replacing wod_readme.pdf with a newer WOD release. Nothing fetches the
linked pages: they are external, they change between releases, and a cached copy would go
stale without saying so.

Sits beside index_manual.py for the same reason: both rebuild a file in knowledge/ from the
PDF, both need the dev-only `pypdf`, and neither is imported by anything at runtime.
"""
import collections
import json
import re
from pathlib import Path
from typing import Optional

from wod_etl.config import CODE_TABLES_DIR, WOD_README_PDF
from wod_etl.reference import (biological_headers, taxa_biomass_variables,
                               variable_specific_headers, wod_secondary_headers)

# The URL path encodes both the namespace and the code id, e.g.
# .../CODES/s_29_probe_type.html -> secondary header 29.
PREFIX = {"s": "secondary_header", "v": "variable_specific_header",
          "b": "biological_header", "t": "taxa_variable"}
CODE_URL = re.compile(r"/((?:s|v|b|t))_(\d+)_([a-zA-Z0-9_]+)\.(html|csv|pdf)$")

# Links that are not per-code but are needed often enough to name.
HUBS = {
    "all_wod_code_tables": "https://www.ncei.noaa.gov/access/world-ocean-database/wod-codes.html",
    "secondary_headers": "https://www.ncei.noaa.gov/access/world-ocean-database/wod-codes.html#second",
    "variable_specific_headers": "https://www.ncei.noaa.gov/access/world-ocean-database/wod-codes.html#var_sp",
    "biological_headers": "https://www.ncei.noaa.gov/access/world-ocean-database/wod-codes.html#bio",
    "taxa_variables": "https://www.ncei.noaa.gov/access/world-ocean-database/wod-codes.html#taxa",
    "country_codes": "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/country-list.html",
    "principal_investigators": "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/primary-investigator-list.html",
    "originator_flag_sets": "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/s_96_origflagset.html",
    "depth_dependent_variables": "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/depth-dependent-variables.html",
    "quality_flag_definitions": "http://data.nodc.noaa.gov/woa/WOD/CODES/PDF/DefinitionQualityFlags.pdf",
    "taxa_list_itis": "https://www.ncei.noaa.gov/access/world-ocean-database/CODES/t_1_taxa_list.html",
    "range_area_mask": "https://www.ncei.noaa.gov/data/oceans/woa/WOD/MASKS/range_area.msk",
    "range_basin_list": "https://www.ncei.noaa.gov/data/oceans/woa/WOD/MASKS/range_basin_list.msk",
}

NAME_OF = {
    "secondary_header": lambda i: wod_secondary_headers().get(i),
    "variable_specific_header": lambda i: (variable_specific_headers().get(i) or {}).get("name"),
    "biological_header": lambda i: (biological_headers().get(i) or {}).get("name"),
    "taxa_variable": lambda i: (taxa_biomass_variables().get(i) or {}).get("name"),
}


def annotations(pdf_path: Path):
    """Every (uri, page) hyperlink annotation in the PDF. Requires the dev dep `pypdf`."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    for page_no, page in enumerate(reader.pages, start=1):
        for annot in (page.get("/Annots") or []):
            try:
                action = annot.get_object().get("/A")
                uri = action.get("/URI") if action else None
            except Exception:                       # noqa: BLE001 - malformed annotation
                uri = None
            if uri:
                yield str(uri), page_no


def build(pdf_path: Path) -> dict:
    """The code_table_links.json payload, or an {"error": ...} dict if it cannot be built."""
    if not Path(pdf_path).exists():
        return {"error": f"manual not found at {pdf_path}"}
    try:
        import pypdf                                        # noqa: F401
    except ImportError:
        return {"error": "pypdf is not installed. It is a dev dependency: `uv sync`."}

    found = collections.defaultdict(dict)
    pages_seen = set()
    for uri, page_no in annotations(pdf_path):
        m = CODE_URL.search(uri)
        if not m:
            continue
        kind, num, slug, ext = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        namespace = PREFIX[kind]
        entry = found[namespace].setdefault(
            num, {"field": f"{kind}_{num}_{slug}", "name": NAME_OF[namespace](num),
                  "manual_pages": set()})
        entry[ext] = uri
        entry["manual_pages"].add(page_no)
        pages_seen.add(page_no)

    links = {}
    for namespace in PREFIX.values():
        links[namespace] = {
            str(num): {**{k: v for k, v in entry.items() if k != "manual_pages"},
                       "manual_pages": sorted(entry["manual_pages"])}
            for num, entry in sorted(found[namespace].items())
        }

    return {
        "table": "WOD online code tables, one per coded metadata field",
        "source": "Hyperlink annotations embedded in knowledge/wod_readme.pdf, extracted "
                  "with wod_etl.reference.extract_code_table_links",
        "pages": sorted(pages_seen),
        "note": "The manual defines the metadata FIELDS (Tables 4a, 5a, 6a, 7a) but "
                "publishes the VALUE code lists online, as a link beside each entry. These "
                "are those links, taken from the PDF's own annotations -- they do not "
                "survive a text extraction, which is why the 'App' column looks like a dead "
                "end. A field absent here has no published code list: its value is a plain "
                "measurement or free text, not a code.",
        "usage": "code_table_link('secondary_header', 29) -> the probe-type code list. Fetch "
                 "the CSV rather than the HTML when you need the values: same list, "
                 "parseable. Nothing here fetches them automatically -- they are external, "
                 "they change between WOD releases, and a cached copy would go stale "
                 "silently.",
        "hubs": HUBS,
        "links": links,
    }


def write(pdf_path: Optional[Path] = None) -> dict:
    """Build and write code_table_links.json. Returns a status dict; never raises."""
    data = build(pdf_path or WOD_README_PDF)
    if "error" in data:
        return {"status": "error", **data}
    out = CODE_TABLES_DIR / "code_table_links.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    return {"status": "written", "path": str(out), "pages_scanned": len(data["pages"]),
            "counts": {k: len(v) for k, v in data["links"].items()}}


if __name__ == "__main__":
    import sys

    result = write()
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["status"] == "error" else 0)
