"""Download a complete file to data/raw/.

Phase 1's fetch_and_peek_file only reads the first few kilobytes, which is enough to decide
what a file is but not enough to process it. This is the skill that actually retrieves it.

Downloads stream to a .part file and are renamed on completion, so an interrupted run never
leaves a truncated file that looks finished.
"""
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import unquote, urlparse

import httpx

from wod_etl.config import (
    DOWNLOAD_CHUNK_BYTES,
    HTTP_BACKOFF_SECONDS,
    HTTP_RETRIES,
    HTTP_TIMEOUT,
    MAX_DOWNLOAD_BYTES,
    RAW_DIR,
    USER_AGENT,
)


def download_file(
    url_or_path: str,
    dest_dir: Optional[str] = None,
    overwrite: bool = False,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    subdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a file to local disk and report where it landed.

    A local path is copied rather than downloaded, so callers can treat local and remote
    sources identically. Returns a dict with `status` in {"downloaded", "cached", "copied",
    "error"} -- errors are returned, never raised, so an unattended run can continue.
    """
    dest_root = Path(dest_dir) if dest_dir else RAW_DIR
    if subdir:
        dest_root = dest_root / _safe_subpath(subdir)
    dest_root.mkdir(parents=True, exist_ok=True)

    name = _filename_for(url_or_path)
    dest = dest_root / name

    if os.path.exists(url_or_path):
        return _copy_local(url_or_path, dest, overwrite)

    if dest.exists() and not overwrite:
        return {
            "status": "cached", "source": url_or_path, "local_path": str(dest),
            "size_bytes": dest.stat().st_size, "sha256": _sha256(dest),
            "note": "file already present; pass overwrite=True to refetch",
        }

    tmp = dest.with_suffix(dest.suffix + ".part")
    last_error = None

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            written = 0
            digest = hashlib.sha256()
            with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url_or_path, headers={"User-Agent": USER_AGENT}) as resp:
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type")
                    declared = resp.headers.get("content-length")
                    if declared and int(declared) > max_bytes:
                        return {"status": "error", "source": url_or_path,
                                "error": f"file is {int(declared)} bytes, over the "
                                         f"{max_bytes} byte limit",
                                "local_path": None}
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                            written += len(chunk)
                            if written > max_bytes:
                                fh.close()
                                tmp.unlink(missing_ok=True)
                                return {"status": "error", "source": url_or_path,
                                        "error": f"exceeded {max_bytes} byte limit mid-stream",
                                        "local_path": None}
                            digest.update(chunk)
                            fh.write(chunk)

            tmp.replace(dest)
            return {
                "status": "downloaded", "source": url_or_path, "local_path": str(dest),
                "size_bytes": written, "sha256": digest.hexdigest(),
                "content_type": content_type, "attempts": attempt,
            }

        except Exception as e:                      # noqa: BLE001 - reported, not raised
            last_error = f"{type(e).__name__}: {e}"
            tmp.unlink(missing_ok=True)
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF_SECONDS * attempt)

    return {"status": "error", "source": url_or_path, "error": last_error,
            "attempts": HTTP_RETRIES, "local_path": None}


def _copy_local(src: str, dest: Path, overwrite: bool) -> Dict[str, Any]:
    src_p = Path(src)
    if src_p.resolve() == dest.resolve():
        return {"status": "cached", "source": src, "local_path": str(src_p),
                "size_bytes": src_p.stat().st_size, "sha256": _sha256(src_p),
                "note": "source is already inside the raw directory"}
    if dest.exists() and not overwrite:
        return {"status": "cached", "source": src, "local_path": str(dest),
                "size_bytes": dest.stat().st_size, "sha256": _sha256(dest)}
    shutil.copy2(src_p, dest)
    return {"status": "copied", "source": src, "local_path": str(dest),
            "size_bytes": dest.stat().st_size, "sha256": _sha256(dest)}


def _filename_for(url_or_path: str) -> str:
    base = unquote(urlparse(url_or_path).path.rstrip("/").split("/")[-1])
    return _safe_component(base) or "download"


def _safe_component(name: str) -> str:
    """Strip anything that could escape the destination directory."""
    name = name.replace("\\", "/").split("/")[-1]
    return "".join(c for c in name if c.isalnum() or c in "._-+")[:200]


def _safe_subpath(name: str) -> Path:
    """Sanitise a MULTI-segment subdirectory, keeping the nesting.

    `_safe_component` keeps only the last segment, which is right for a filename but drops
    the run id from 'run_07/stationA' -- so a raw copy landed outside its own run. Each
    segment is sanitised separately instead; '.' and '..' cannot survive that, so the
    traversal guard is unchanged.
    """
    parts = [_safe_component(part) for part in name.replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..")]
    return Path(*parts) if parts else Path()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(DOWNLOAD_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    import json
    res = download_file("https://www.ncei.noaa.gov/data/oceans/woa/WOD/DOC/wodreadme.pdf",
                        max_bytes=5 * 1024 * 1024)
    print(json.dumps(res, indent=2))
