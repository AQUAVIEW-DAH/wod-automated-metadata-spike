"""List an open HTTP directory index (Apache/NGINX) or a local folder.

Parses the size and last-modified text that servers print beside each link, and refuses to
follow links that leave the starting directory -- NCEI's "Parent Directory" link is an
absolute path rather than '../', so a naive filter walks upward into the whole archive.
"""
import os
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set
from urllib.parse import urljoin, urlparse, unquote

import httpx
from bs4 import BeautifulSoup

from wod_etl.config import HTTP_TIMEOUT, USER_AGENT

_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]?)B?$", re.IGNORECASE)
_DATE_FORMATS = [
    "%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y", "%Y-%m-%d",
]
_MAX_RECURSION_DEPTH = 8


def list_remote_directory(
    url_or_path: str,
    recursive: bool = False,
    filter_extension: Optional[str] = None,
    _visited: Optional[Set[str]] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    """List the contents of a local folder or an open HTTP directory index."""
    if os.path.exists(url_or_path):
        return _list_local_dir(url_or_path, recursive, filter_extension)

    visited = _visited if _visited is not None else set()
    base = _normalize(url_or_path)
    if base in visited:
        return {"source": url_or_path, "total_items": 0, "items": [],
                "note": "already visited; cycle avoided"}
    visited.add(base)

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url_or_path, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        return {"source": url_or_path, "error": f"{type(e).__name__}: {e}",
                "total_items": 0, "items": []}

    items: List[Dict[str, Any]] = []
    skipped_nav = 0

    for link in soup.find_all("a"):
        href = link.get("href")
        if not href:
            continue

        full_url = urljoin(url_or_path, href)
        if not _is_descendant(base, full_url):
            skipped_nav += 1
            continue

        name = unquote(href.rstrip("/").split("/")[-1])
        if not name or name.startswith("?"):
            skipped_nav += 1
            continue

        is_dir = href.endswith("/")
        size_bytes, last_modified = _extract_metadata(link)

        if filter_extension and not is_dir and not name.lower().endswith(filter_extension.lower()):
            continue

        items.append({
            "name": name,
            "path": full_url,
            "is_dir": is_dir,
            "size_bytes": size_bytes,
            "last_modified": last_modified,
        })

        if recursive and is_dir and _depth < _MAX_RECURSION_DEPTH:
            sub = list_remote_directory(full_url, True, filter_extension, visited, _depth + 1)
            items.extend(sub.get("items", []))

    result = {
        "source": url_or_path,
        "total_items": len(items),
        "items": items,
        "skipped_navigation_links": skipped_nav,
    }
    if not items:
        result.update(_diagnose_empty(url_or_path))
    return result


# An NCEI accession is usually cited by its metadata landing page or by its bare number, and
# neither is a directory. Crawled, they yield a valid HTTP 200 with zero data files, which
# reads downstream as "empty accession -- check the URL" and sends the reader looking for a
# typo that is not there.
_LANDING_PAGE = re.compile(
    r"landing-page|/metadata/|gov\.noaa\.nodc[:.](\d+)|catalog\.data\.gov", re.IGNORECASE)
_ACCESSION_URL = re.compile(r"/archive/accession/(\d+)", re.IGNORECASE)
_BARE_ACCESSION = re.compile(r"^\s*0*(\d{5,7})\s*$")


def accession_number(text: str):
    """The NCEI accession number in a URL or a bare string, or None."""
    for pattern in (_ACCESSION_URL, re.compile(r"gov\.noaa\.nodc[:.](\d+)", re.IGNORECASE),
                    _BARE_ACCESSION):
        m = pattern.search(str(text))
        if m:
            return m.group(1).lstrip("0") or "0"
    return None


def _diagnose_empty(url_or_path: str) -> Dict[str, Any]:
    """Say WHY a listing came back empty, when the URL explains it.

    A metadata landing page is not a data directory, and no amount of retrying makes it one.
    NCEI renders the file listing client-side, so there is no static directory URL to derive
    -- the reader has to open the accession page and take the data URL off it. Saying that is
    the whole value here; guessing a URL pattern would be worse than saying nothing.
    """
    number = accession_number(url_or_path)
    is_landing = bool(_LANDING_PAGE.search(url_or_path)) or bool(_ACCESSION_URL.search(url_or_path))
    if not (is_landing or _BARE_ACCESSION.match(str(url_or_path))):
        return {"hint": "The page was fetched but held no file links. It may be an index "
                        "that renders its listing with JavaScript, or a page that is not a "
                        "directory at all. Open it and check before retrying."}

    where = (f"https://www.ncei.noaa.gov/archive/accession/download/{number}"
             if number else "the accession's download page")
    return {
        "error": "not an accession data directory: this is an NCEI metadata landing page or "
                 "a bare accession number, so there are no files to list.",
        "accession_number": number,
        "hint": f"Open {where} and take the data directory URL from it, then point "
                f"process_accession at that. NCEI renders the file listing client-side, so "
                f"the directory URL cannot be derived from the accession number -- it lives "
                f"under /data/oceans/archive/arc<NNNN>/{number or '<accession>'}/ and only "
                f"that page says which arc bundle. If the files are already downloaded, "
                f"point at the local folder instead.",
    }


def _normalize(url: str) -> str:
    p = urlparse(url)
    path = p.path if p.path.endswith("/") else p.path + "/"
    return f"{p.scheme}://{p.netloc}{path}"


def _is_descendant(base: str, candidate: str) -> bool:
    """True only if candidate sits at or below base.

    This is what stops the parent-directory link being reported as a child. NCEI writes it
    as an absolute path (/data/oceans/archive/arc0222), so matching on '../' misses it.
    """
    b, c = urlparse(base), urlparse(candidate)
    if (b.scheme, b.netloc) != (c.scheme, c.netloc):
        return False
    bp = b.path if b.path.endswith("/") else b.path + "/"
    cp = c.path
    return cp.startswith(bp) and cp != bp


def _extract_metadata(link) -> tuple:
    """Pull size and last-modified from the text a server prints beside the link."""
    # Table-based indexes: read the sibling cells.
    td = link.find_parent("td")
    if td is not None:
        cells = [c.get_text(" ", strip=True) for c in td.find_next_siblings("td")]
        return _from_fields(cells)

    # <pre>-based indexes: the trailing text node holds "date  size".
    tail = ""
    node = link.next_sibling
    while node is not None and len(tail) < 120:
        text = node if isinstance(node, str) else getattr(node, "get_text", lambda **k: "")()
        if text:
            tail += text
        if "\n" in tail:
            break
        node = node.next_sibling
    tail = tail.split("\n")[0]
    return _from_fields([tail])


def _from_fields(fields: List[str]) -> tuple:
    size, modified = None, None
    joined = " ".join(f for f in fields if f).strip()
    if not joined:
        return None, None

    m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
                  r"|\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)", joined)
    if m:
        modified = _parse_date(m.group(1))
        joined = joined[m.end():]

    for tok in reversed(joined.split()):
        s = _parse_size(tok)
        if s is not None:
            size = s
            break
    return size, modified


def _parse_size(token: str) -> Optional[int]:
    token = token.strip()
    if not token or token == "-":
        return None
    m = _SIZE_RE.match(token)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2).upper()]
    return int(value * mult)


def _parse_date(token: str) -> Optional[str]:
    token = token.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).isoformat()
        except ValueError:
            continue
    return None


def _list_local_dir(path: str, recursive: bool, filter_extension: Optional[str]) -> Dict[str, Any]:
    items = []
    for root, dirs, files in os.walk(path):
        for d in sorted(dirs):
            full = os.path.join(root, d)
            items.append({"name": d, "path": full, "is_dir": True, "size_bytes": None,
                          "last_modified": _mtime(full)})
        for f in sorted(files):
            if filter_extension and not f.lower().endswith(filter_extension.lower()):
                continue
            full = os.path.join(root, f)
            items.append({"name": f, "path": full, "is_dir": False,
                          "size_bytes": os.path.getsize(full), "last_modified": _mtime(full)})
        if not recursive:
            break
    return {"source": path, "total_items": len(items), "items": items,
            "skipped_navigation_links": 0}


def _mtime(path: str) -> Optional[str]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    except OSError:
        return None


if __name__ == "__main__":
    import json
    res = list_remote_directory("https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/")
    print(json.dumps({k: v for k, v in res.items() if k != "items"}, indent=2))
    for it in res.get("items", [])[:6]:
        print(" ", json.dumps(it))
