import os
from typing import Dict, Any
import httpx

from wod_etl.config import TEXT_ENCODINGS


def fetch_and_peek_file(file_url_or_path: str, num_bytes: int = 8192) -> Dict[str, Any]:
    """
    Fetches the first N bytes of a remote or local file to inspect header contents.
    """
    if os.path.exists(file_url_or_path):
        with open(file_url_or_path, "rb") as f:
            chunk = f.read(num_bytes)
        return _format_peek_response(file_url_or_path, chunk)

    # Remote HTTP file with Range request
    headers = {
        "Range": f"bytes=0-{num_bytes - 1}",
        "User-Agent": "WOD-ETL-Agent/1.0"
    }

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(file_url_or_path, headers=headers)
            # 206 Partial Content or 200 OK
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            chunk = resp.content
            return _format_peek_response(file_url_or_path, chunk)
    except Exception as e:
        return {"source": file_url_or_path, "error": str(e), "preview_text": None}


def _decode_peek(chunk: bytes):
    """Decode a truncated read, without letting the truncation change the verdict.

    A peek cuts the file at a fixed byte count, which lands mid-character often enough to
    matter. Treated as a decode failure, the whole file is then read as latin-1 and every
    'umol/l' becomes 'Âµmol/l' -- mojibake that silently defeats unit detection. So a
    failure in the last three bytes is truncation, not evidence about the encoding.
    """
    for enc in TEXT_ENCODINGS:
        # utf-8-sig differs from utf-8 only by stripping a BOM; report the plain name
        # unless a BOM was really there, so the label stays comparable across files.
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


if __name__ == "__main__":
    import json
    res = fetch_and_peek_file("https://www.ncei.noaa.gov/data/oceans/woa/WOD/DOC/wodreadme.pdf", num_bytes=500)
    print(json.dumps(res, indent=2))
