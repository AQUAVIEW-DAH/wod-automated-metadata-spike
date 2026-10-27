import os
from typing import Dict, Any
import httpx

from wod_etl.config import TEXT_ENCODINGS, USER_AGENT


def fetch_and_peek_file(file_url_or_path: str, num_bytes: int = 8192) -> Dict[str, Any]:
    """Read the first num_bytes of a local file or URL to inspect its header."""
    if os.path.exists(file_url_or_path):
        with open(file_url_or_path, "rb") as f:
            chunk = f.read(num_bytes)
        return _format_peek_response(file_url_or_path, chunk)

    headers = {
        "Range": f"bytes=0-{num_bytes - 1}",
        "User-Agent": USER_AGENT
    }

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(file_url_or_path, headers=headers)
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            chunk = resp.content
            return _format_peek_response(file_url_or_path, chunk)
    except Exception as e:
        return {"source": file_url_or_path, "error": str(e), "preview_text": None}


def _decode_peek(chunk: bytes):
    """Decode a truncated read, so a character cut at the end does not force a latin-1 fallback."""
    for enc in TEXT_ENCODINGS:
        # Report plain 'utf-8' unless a BOM is present, so labels compare across files.
        label = "utf-8" if enc == "utf-8-sig" and not chunk.startswith(b"\xef\xbb\xbf") else enc
        try:
            return chunk.decode(enc), label
        except UnicodeDecodeError as e:
            if e.start >= len(chunk) - 3:
                try:
                    return chunk[:e.start].decode(enc), label
                except UnicodeDecodeError:
                    pass
            continue
    return chunk.decode("ascii", errors="replace"), "ascii (lossy)"


def _format_peek_response(source: str, chunk: bytes) -> Dict[str, Any]:
    text, encoding = _decode_peek(chunk)

    lines = text.splitlines()

    return {
        "source": source,
        "peek_size_bytes": len(chunk),
        "detected_encoding": encoding,
        "preview_text": text,
        "line_count_in_peek": len(lines)
    }
