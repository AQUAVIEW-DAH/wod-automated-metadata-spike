"""Atomic file writes: a crash leaves either the old file or the new one."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(path, text: str) -> Path:
    """Write `text` to `path` via a temp file in the same folder, then rename over it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def write_json_atomic(path, obj: Any) -> Path:
    return write_text_atomic(path, json.dumps(obj, indent=2, default=str))
