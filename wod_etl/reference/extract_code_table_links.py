"""Rebuild code_table_links.json from the code-list hyperlinks in the WOD manual PDF.

Maintainer tool, re-run after a new WOD release; the pipeline reads only the JSON.
    uv run python -m wod_etl.reference.extract_code_table_links
"""
import collections
import json
import re
from pathlib import Path
from typing import Optional

from wod_etl.config import CODE_TABLES_DIR, WOD_README_PDF
from wod_etl.reference import (biological_headers, taxa_biomass_variables,
                               variable_specific_headers, wod_secondary_headers)

# The URL names the namespace and code id: .../s_29_probe_type.html is secondary header 29.
PREFIX = {"s": "secondary_header", "v": "variable_specific_header",
          "b": "biological_header", "t": "taxa_variable"}
CODE_URL = re.compile(r"/((?:s|v|b|t))_(\d+)_([a-zA-Z0-9_]+)\.(html|csv|pdf)$")

# Hub links, not tied to one field.
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

CSV_BASE = "https://www.ncei.noaa.gov/data/oceans/woa/WOD/CODES/CSV/"

# Lists the manual cites without a link: the country code (Table 10a field 6) and the PI
# code (Table 10b, type 3).
CITED_LISTS = {
    "primary_header": {"6": {
        "field": "country_list", "name": "Country Code", "html": HUBS["country_codes"],
        "csv": CSV_BASE + "country_list.csv", "manual_pages": [17, 39, 68]}},
    "character_data": {"3": {
        "field": "primary_investigator_list", "name": "Principal Investigator",
        "html": HUBS["principal_investigators"],
        "csv": CSV_BASE + "primary_investigator_list.csv", "manual_pages": [30, 41]}},
}

NAME_OF = {
    "secondary_header": lambda i: wod_secondary_headers().get(i),
    "variable_specific_header": lambda i: (variable_specific_headers().get(i) or {}).get("name"),
    "biological_header": lambda i: (biological_headers().get(i) or {}).get("name"),
    "taxa_variable": lambda i: (taxa_biomass_variables().get(i) or {}).get("name"),
}


def annotations(pdf_path: Path):
    """Every (uri, page) hyperlink annotation in the PDF. Requires `pypdf`."""
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
        return {"error": "pypdf is not installed: `uv sync` installs it."}

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
    links.update(CITED_LISTS)

    return {
        "table": "WOD online code tables, one per coded metadata field",
        "source": "Hyperlink annotations embedded in knowledge/wod_readme.pdf, extracted "
                  "with wod_etl.reference.extract_code_table_links",
        "pages": sorted(pages_seen),
        "note": "The manual defines the metadata fields (Tables 4a, 5a, 6a, 7a) and links "
                "each coded one to its value list online; the links are PDF annotations, so "
                "a text extraction loses them. primary_header and character_data are lists "
                "the text cites without a link. A field absent here takes a plain value.",
        "usage": "code_table_link('secondary_header', 29) -> the probe-type list. "
                 "wod_etl.reference.code_lists fetches the CSV and caches it with the date "
                 "retrieved; lists change between WOD releases.",
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
