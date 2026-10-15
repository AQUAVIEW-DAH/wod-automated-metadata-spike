"""List an open HTTP directory index (Apache/NGINX) or a local folder.

Parses the size and last-modified text that servers print beside each link, and refuses to
follow links that leave the starting directory -- NCEI's "Parent Directory" link is an
absolute path rather than '../', so a naive filter walks upward into the whole archive.
"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

from wod_etl.config import HTTP_RETRIES, RAW_DIR
from wod_etl.http_util import get_with_retries

log = logging.getLogger(__name__)

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
    """List the contents of a local folder, a local archive, or an open HTTP directory index.

    Three inputs are resolved before anything is listed:

    * an NCEI landing page, `/archive/accession/<n>` URL or bare accession number is turned
      into its data directory (see `resolve_accession_url`) and that is listed, with the
      resolution recorded under `resolved_from`;
    * a versioned accession directory (`1.1/`, `2.2/`, each a full copy of the bundle) is
      narrowed to the latest version, so a recursive crawl does not process every file once
      per version;
    * a local `.tar`, `.tar.gz`, `.tgz` or `.zip` is extracted under `data/raw/` and the
      extracted folder is listed.
    """
    if os.path.isfile(url_or_path) and _is_archive(url_or_path):
        return _list_archive(url_or_path, recursive, filter_extension)
    if os.path.exists(url_or_path):
        return _list_local_dir(url_or_path, recursive, filter_extension)

    if _depth == 0 and _is_accession_reference(url_or_path):
        resolved = resolve_accession_url(url_or_path)
        if resolved.get("error"):
            return {"source": url_or_path, "total_items": 0, "items": [],
                    "error": resolved["error"], "hint": resolved.get("hint"),
                    "accession_number": resolved.get("accession_number")}
        log.info("resolved %s to the data directory %s (bundle %s)", url_or_path,
                 resolved["url"], resolved.get("arc"))
        listing = list_remote_directory(resolved["url"], recursive, filter_extension)
        listing["resolved_from"] = url_or_path
        listing["resolution"] = resolved
        return listing

    visited = _visited if _visited is not None else set()
    base = _normalize(url_or_path)
    if base in visited:
        return {"source": url_or_path, "total_items": 0, "items": [],
                "note": "already visited; cycle avoided"}
    visited.add(base)

    try:
        soup = BeautifulSoup(get_with_retries(url_or_path).text, "html.parser")
    except Exception as e:
        return {"source": url_or_path, "error": f"{type(e).__name__}: {e}",
                "total_items": 0, "items": [],
                "hint": "NCEI answers 503 and times out often; this was retried "
                        f"{HTTP_RETRIES} time(s) with backoff. Try again later, or point at "
                        "a downloaded copy."}

    if _depth == 0 and recursive:
        latest = _latest_version(soup, url_or_path)
        if latest:
            log.info("versioned accession: listing version %s of %s", latest["chosen"],
                     latest["available"])
            listing = list_remote_directory(latest["url"], True, filter_extension, visited, 1)
            listing["source"] = latest["url"]
            listing["version"] = latest
            return listing

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


# A folder named as NCEI writes an accession -- seven digits, zero-padded (0062621) -- or an
# archive of one. A bare 5-6 digit folder is too often a date (201511, 151109) to be read as
# an accession, and output named for the wrong accession is worse than one named for none.
_ACCESSION_COMPONENT = re.compile(
    r"^(?:0(\d{6})|0*(\d{5,7})\.(?:tar\.gz|tgz|tar\.bz2|tar|zip))$", re.IGNORECASE)


def accession_for(text_or_path) -> Optional[str]:
    """The accession id as NCEI writes it -- seven digits, zero-padded -- or None.

    Works on everything an accession is cited as or stored under: a landing page, an
    `/archive/accession/<n>` or `/archive/arc0238/<n>/` URL, a bare number, a tarball name,
    and a local path such as `data/staging/0303928/CTD_20261012` -- a folder named as NCEI
    writes an accession, seven digits zero-padded; `201511` in a path is a date, not one. `accession_number` returns
    the number with its leading zeros stripped (`303928`) and finds nothing in a path; this is
    the one to key files and folders by.
    """
    text = str(text_or_path or "").strip()
    if not text:
        return None
    number = accession_number(text)
    if number is None:
        m = _ARCHIVE_PATH.search(text)
        number = m.group(2) if m else None
    if number is None:
        parts = [p for p in re.split(r"[\\/]+", text) if p]
        for part in reversed(parts):
            m = _ACCESSION_COMPONENT.match(part)
            if m:
                number = m.group(1) or m.group(2)
                break
    return number.lstrip("0").zfill(7) if number else None


def _is_accession_reference(text: str) -> bool:
    return bool(_LANDING_PAGE.search(str(text)) or _ACCESSION_URL.search(str(text))
                or _BARE_ACCESSION.match(str(text)))


# The landing page is rendered client-side, but its static HTML still carries the FTP link
# (and a JSON-LD copy of it): ftp://ftp-oceans.ncei.noaa.gov/nodc/archive/arc0238/0303535/.
# The HTTPS directory is the same path under /data/oceans/.
_ARCHIVE_PATH = re.compile(r"/archive/(arc\d{4})/(\d{5,7})/?", re.IGNORECASE)
LANDING_PAGE_URL = ("https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso"
                    "?id=gov.noaa.nodc:{number}")
# The same ISO record as plain XML from NCEI's metadata catalogue. It answers in under a second
# where the landing page, rendered on request, can take a minute or time out altogether -- and
# it carries the same archive path.
ISO_RECORD_URL = ("https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/"
                  "gov.noaa.nodc:{number}/xml")
DATA_DIR_URL = "https://www.ncei.noaa.gov/data/oceans/archive/{arc}/{number}/"


def resolve_accession_url(text: str) -> Dict[str, Any]:
    """The HTTPS data directory for an accession, from its landing page, URL or number.

    `{"url", "accession_number", "arc", "landing_page", "resolved_from"}` on success. NCEI
    renders the file listing with JavaScript, so the directory cannot be computed from the
    number -- but the accession's ISO record names its archive bundle (`arc0238`), the
    missing piece. It is read from NCEI's metadata catalogue first, and from the landing
    page's static HTML (which carries the FTP download link) when the catalogue does not
    answer. Fetched with retries; on failure the result carries `error` and a `hint` naming
    the page to open by hand.
    """
    number = accession_number(text)
    if number is None:
        return {"error": f"no accession number in {text!r}", "accession_number": None}
    padded = number.zfill(7)
    page = (text if str(text).startswith("http") and _LANDING_PAGE.search(str(text))
            else LANDING_PAGE_URL.format(number=padded))
    tried = []
    for source in (ISO_RECORD_URL.format(number=padded), page):
        try:
            body = get_with_retries(source).text
        except Exception as e:                          # noqa: BLE001 - reported, not raised
            tried.append(f"{source} ({type(e).__name__}: {e})")
            continue
        for m in _ARCHIVE_PATH.finditer(body):
            if m.group(2).lstrip("0") == number:
                arc = m.group(1).lower()
                return {"url": DATA_DIR_URL.format(arc=arc, number=padded),
                        "accession_number": number, "arc": arc, "landing_page": page,
                        "resolved_from": source}
        tried.append(f"{source} (names no archive directory for accession {number})")
    out = _diagnose_empty(text)
    out["error"] = (f"could not find the data directory for accession {number}: "
                    + "; ".join(tried) + ". It may not be public yet, or NCEI did not answer")
    return out


_VERSION_DIR = re.compile(r"^(\d+)\.(\d+)/?$")


def _latest_version(soup, url: str) -> Optional[Dict[str, Any]]:
    """The highest `<major>.<minor>/` subdirectory, when the page lists versions of a bundle."""
    versions = []
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        m = _VERSION_DIR.match(href)
        if m and _is_descendant(_normalize(url), urljoin(url, href)):
            versions.append(((int(m.group(1)), int(m.group(2))), href.rstrip("/")))
    if not versions:
        return None
    versions.sort()
    chosen = versions[-1][1]
    return {"url": urljoin(_normalize(url), chosen + "/"), "chosen": chosen,
            "available": [v for _, v in versions],
            "note": (f"Versioned accession: {len(versions)} versions, each a full copy of "
                     f"the bundle. Listing only the latest, {chosen}; point at another "
                     f"version's URL to process it instead." if len(versions) > 1 else None)}


_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".zip")


def _is_archive(path: str) -> bool:
    import tarfile
    import zipfile

    if not str(path).lower().endswith(_ARCHIVE_SUFFIXES):
        return False
    try:
        return tarfile.is_tarfile(path) or zipfile.is_zipfile(path)
    except OSError:
        return False


def _list_archive(path: str, recursive: bool, filter_extension: Optional[str]) -> Dict[str, Any]:
    """Extract a downloaded accession archive under data/raw/ and list what it holds.

    Extracted once: a second call lists the existing folder. Members that would land outside
    the target -- an absolute path or `..` -- are refused, not extracted.
    """
    import shutil
    import tarfile
    import zipfile
    import zlib

    name = os.path.basename(path)
    stem = re.sub(r"(\.tar)?\.(gz|tgz|bz2|zip|tar)$", "", name, flags=re.IGNORECASE)
    target = RAW_DIR / stem
    refused: List[str] = []
    if not (target.exists() and any(target.iterdir())):
        target.mkdir(parents=True, exist_ok=True)
        root = os.path.realpath(target)
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    for member in z.namelist():
                        dest = os.path.realpath(os.path.join(root, member))
                        if not dest.startswith(root + os.sep) and dest != root:
                            refused.append(member)
                            continue
                        z.extract(member, root)
            else:
                with tarfile.open(path) as t:
                    safe = []
                    for member in t.getmembers():
                        dest = os.path.realpath(os.path.join(root, member.name))
                        if (not dest.startswith(root + os.sep) or member.issym()
                                or member.islnk()):
                            refused.append(member.name)
                            continue
                        safe.append(member)
                    t.extractall(root, members=safe, filter="data")
        # A truncated download -- what a flaky NCEI transfer leaves -- raises EOFError or
        # zlib.error from the decompressor, not a TarError. Whatever was extracted is removed:
        # a non-empty target is read as "already extracted" on the next call, and would be
        # listed as the whole accession.
        except (OSError, EOFError, ValueError, zlib.error, tarfile.TarError,
                zipfile.BadZipFile) as e:
            shutil.rmtree(target, ignore_errors=True)
            return {"source": path, "error": f"could not extract {name}: "
                                              f"{type(e).__name__}: {e}",
                    "total_items": 0, "items": []}
    listing = _list_local_dir(str(target), recursive, filter_extension)
    listing["extracted_from"] = path
    if refused:
        listing["refused_members"] = refused
    return listing


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
