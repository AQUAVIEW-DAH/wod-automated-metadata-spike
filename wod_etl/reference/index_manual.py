"""Extract the manual's page text once so `search_manual` can grep the prose.

Only ~9 tables of the 131-page manual are transcribed to JSON. Everything else -- dataset
descriptions, the ASCII format specs, WMO and taxonomic codes, the interpolation method --
is only reachable as text. This builds a plain page cache: no embeddings, no vector store,
no extra runtime dependency.

    uv run python -m wod_etl.reference.index_manual        # build
    uv run python -m wod_etl.reference.index_manual --stat # report
"""
import json
import sys
from typing import List, Optional

from wod_etl.config import KNOWLEDGE_DIR, WOD_README_PDF

CACHE_PATH = KNOWLEDGE_DIR / "wod_readme_db" / "page_text.json"


def build_page_cache(force: bool = False) -> dict:
    """Extract every page's text to a JSON cache. Requires the dev dependency `pypdf`."""
    if CACHE_PATH.exists() and not force:
        pages = load_page_cache() or []
        return {"status": "cached", "path": str(CACHE_PATH), "pages": len(pages)}

    if not WOD_README_PDF.exists():
        return {"status": "error", "error": f"manual not found at {WOD_README_PDF}"}

    try:
        from pypdf import PdfReader
    except ImportError:
        return {"status": "error",
                "error": "pypdf is not installed. It is a dev dependency: `uv sync`."}

    reader = PdfReader(str(WOD_README_PDF))
    pages: List[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:                        # noqa: BLE001 - one bad page is not fatal
            pages.append(f"<<extraction failed: {type(e).__name__}: {e}>>")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(pages))
    return {"status": "built", "path": str(CACHE_PATH), "pages": len(pages),
            "characters": sum(len(p) for p in pages)}


def load_page_cache() -> Optional[List[str]]:
    """Return the cached page text, or None if it has not been built."""
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    if "--stat" in sys.argv:
        pages = load_page_cache()
        print(json.dumps({"built": pages is not None, "pages": len(pages) if pages else 0,
                          "path": str(CACHE_PATH)}, indent=2))
    else:
        print(json.dumps(build_page_cache(force="--force" in sys.argv), indent=2))
