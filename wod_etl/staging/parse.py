"""Parsing helpers for the forms coordinates, times and depths actually arrive in."""
import math
import re
from typing import Any, Optional

# 62.16 | -170.5 | "62.16 N" | "33.5 S"
_DECIMAL = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([NSEW])?\s*$", re.I)
# "62 9.6 N" | "170 30.01 W"
_DEG_MIN_SPACE = re.compile(r"^\s*([-+]?\d+)[\s:]+(\d+(?:\.\d+)?)\s*([NSEW])?\s*$", re.I)
# "64-05.8454N" | "170-30.01 W" | "62-09.60"
_DEG_MIN_DASH = re.compile(r"^\s*([-+]?\d+)\s*-\s*(\d+(?:\.\d+)?)\s*([NSEW])?\s*$", re.I)
# "62 09 36 N" degrees, minutes, seconds
_DMS = re.compile(r"^\s*([-+]?\d+)[\s:]+(\d+)[\s:]+(\d+(?:\.\d+)?)\s*([NSEW])?\s*$", re.I)


def _signed(magnitude: float, sign_source: Any, hemisphere: Optional[str]) -> float:
    """Apply hemisphere, preferring an explicit letter over a leading minus."""
    hemi = (hemisphere or "").strip().upper()
    if hemi in ("S", "W"):
        return -abs(magnitude)
    if hemi in ("N", "E"):
        return abs(magnitude)
    return -magnitude if str(sign_source).strip().startswith("-") else magnitude


def parse_coordinate(value: Any, minutes: Any = None, hemisphere: Any = None) -> Optional[float]:
    """Return signed decimal degrees, or None if the value cannot be read.

    Accepts every form seen in NCEI submissions:

        parse_coordinate(62.16)                  ->   62.16
        parse_coordinate("170 30.01 W")          -> -170.500167
        parse_coordinate("64-05.8454N")          ->   64.097423
        parse_coordinate("62 09 36 N")           ->   62.16
        parse_coordinate(62, 9.6, "N")           ->   62.16      (separate columns)

    A hemisphere letter always wins over a leading minus sign, because station lists
    routinely write both and the letter is the one the originator intended.
    """
    if value is None:
        return None

    # Three-column form: degrees, decimal minutes, hemisphere.
    if minutes is not None and str(minutes).strip() != "":
        try:
            deg, mins = float(str(value).strip()), float(str(minutes).strip())
        except (TypeError, ValueError):
            return None
        return _signed(abs(deg) + mins / 60.0, value, hemisphere)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _signed(abs(float(value)), value, hemisphere)

    text = str(value).strip()
    if not text:
        return None

    m = _DMS.match(text)
    if m:
        deg, mins, secs, hemi = m.groups()
        return _signed(abs(float(deg)) + float(mins) / 60.0 + float(secs) / 3600.0,
                       deg, hemisphere or hemi)

    for pattern in (_DEG_MIN_DASH, _DEG_MIN_SPACE):
        m = pattern.match(text)
        if m:
            deg, mins, hemi = m.groups()
            return _signed(abs(float(deg)) + float(mins) / 60.0, deg, hemisphere or hemi)

    m = _DECIMAL.match(text)
    if m:
        num, hemi = m.groups()
        return _signed(abs(float(num)), num, hemisphere or hemi)

    return None


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M", "%Y%m%d %H%M%S", "%Y%m%d%H%M%S", "%Y%m%d",
    "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y",
)


def _punctuate_time(t: str) -> str:
    """'2102' -> '21:02', '031903' -> '03:19:03'. Anything else is returned untouched."""
    if t.isdigit():
        if len(t) == 4:
            return f"{t[:2]}:{t[2:]}"
        if len(t) == 6:
            return f"{t[:2]}:{t[2:4]}:{t[4:]}"
    return t


def parse_timestamp(value: Any, time_value: Any = None):
    """Return a datetime, or None. Handles the separate-date-and-time-column form too.

    Single-digit months and days ('2023/6/1') are what spreadsheets emit and are accepted.
    """
    import datetime as dt

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if time_value is not None and str(time_value).strip():
        text = f"{text} {_punctuate_time(str(time_value).strip())}"

    # A bare HHMM or HHMMSS still glued to the date parses under "%H%M%S", where strptime
    # reads 2102 as 21:00:02 rather than failing. Punctuate it so the fields cannot slide.
    m = re.fullmatch(r"(.+?)[ T](\d{4}|\d{6})", text)
    if m:
        text = f"{m.group(1)} {_punctuate_time(m.group(2))}"

    for fmt in _TIME_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def pressure_to_depth(pressure_dbar: float, latitude_deg: float) -> float:
    """Pressure in decibars to depth in metres (UNESCO 1983, Fofonoff & Millard).

    CTD exports often carry pressure and no depth. WOD requires a depth axis, and this is
    the standard conversion. It is latitude-dependent through gravity, so pass the cast's
    latitude rather than assuming one. Record the derivation in the staged header.
    """
    x = math.sin(math.radians(latitude_deg)) ** 2
    gravity = 9.780318 * (1.0 + (5.2788e-3 + 2.36e-5 * x) * x) + 1.092e-6 * pressure_dbar
    numerator = ((((-1.82e-15 * pressure_dbar + 2.279e-10) * pressure_dbar
                   - 2.2512e-5) * pressure_dbar + 9.72659) * pressure_dbar)
    return numerator / gravity
