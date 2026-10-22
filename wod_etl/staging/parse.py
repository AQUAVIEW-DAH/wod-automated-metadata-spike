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

# Degree, minute and second marks (curly quotes included), folded to spaces before matching.
_ANGLE_MARKS = re.compile(
    "[°º˚∘"           # degree sign, masculine ordinal, ring above, ring op
    "′’‘ʼ´'`"    # prime, right/left single quote, modifier, acute, '
    "″”“\"]"               # double prime, right/left double quote, "
    "|\\bdeg(?:rees?)?\\b|\\bmin\\b|\\bsec\\b", re.I)
# "N 21 00.0": a leading hemisphere, moved to the end before matching.
_LEADING_HEMISPHERE = re.compile(r"^\s*([NSEW])\s*([-+]?\d.*)$", re.I)


def _normalise_coordinate_text(text: str) -> str:
    """Fold degree/minute/second marks to spaces and move a leading hemisphere to the end."""
    folded = _ANGLE_MARKS.sub(" ", text)
    m = _LEADING_HEMISPHERE.match(folded)
    if m:
        folded = f"{m.group(2)} {m.group(1)}"
    return re.sub(r"\s+", " ", folded).strip()


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

    Accepted forms:

        parse_coordinate(62.16)                  ->   62.16
        parse_coordinate("170 30.01 W")          -> -170.500167
        parse_coordinate("64-05.8454N")          ->   64.097423
        parse_coordinate("62 09 36 N")           ->   62.16
        parse_coordinate("142°18’E")             ->  142.3       (degree sign, curly quote)
        parse_coordinate("N 21°00.5'")           ->   21.008333  (hemisphere first)
        parse_coordinate(62, 9.6, "N")           ->   62.16      (separate columns)

    A hemisphere letter wins over a leading minus sign.
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

    # A hemisphere letter written twice, as some NMEA headers do: '37 59.49 NN'.
    text = _normalise_coordinate_text(re.sub(r"([NSEW])\1+\s*$", r"\1", str(value).strip(),
                                             flags=re.I))
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
    # Month names; text is normalised first (see _normalise_month_text).
    "%b %d %Y %H:%M", "%b %d %Y", "%d %b %Y %H:%M", "%d %b %Y",
    "%B %d %Y %H:%M", "%B %d %Y", "%d %B %Y %H:%M", "%d %B %Y",
    "%Y %b %d", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%b-%d",
)

_MONTH_NAME = re.compile(r"[A-Za-z]{3,}")


def _normalise_month_text(text: str) -> str:
    """'Nov. 09, 2015' -> 'nov 09 2015'; 'Sept. 3 2015' -> 'sep 3 2015'. Numeric text untouched."""
    if not _MONTH_NAME.search(text):
        return text
    s = re.sub(r"(?<=[A-Za-z])\.", " ", text)          # 'Nov.' -> 'Nov '
    s = re.sub(r",", " ", s)
    s = re.sub(r"\bsept\b", "sep", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def _punctuate_time(t: str) -> str:
    """'2102' -> '21:02', '031903' -> '03:19:03', '08 30' -> '08:30'. Anything else is
    returned untouched."""
    m = re.fullmatch(r"(\d{1,2})\s+(\d{2})(?:\s+(\d{2}))?", t)
    if m:
        return ":".join(x.zfill(2) for x in m.groups() if x)
    if t.isdigit():
        if len(t) == 4:
            return f"{t[:2]}:{t[2:]}"
        if len(t) == 6:
            return f"{t[:2]}:{t[2:4]}:{t[4:]}"
    return t


def parse_timestamp(value: Any, time_value: Any = None, year: Any = None):
    """Return a datetime, or None. `time_value` is a separate time column.

    Accepts single-digit months and days ('2023/6/1') and month names ('Nov. 09, 2015').
    `year` is appended when the date has no four-digit year: `parse_timestamp("Nov. 09",
    year=2015)`.
    """
    import datetime as dt

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if year is not None and str(year).strip() and not re.search(r"\b\d{4}\b", text):
        text = f"{text} {str(year).strip()}"
    text = _normalise_month_text(text)
    if time_value is not None and str(time_value).strip():
        text = f"{text} {_punctuate_time(str(time_value).strip())}"

    # Punctuate a bare HHMM(SS) so strptime cannot read 2102 as 21:00:02; only when the date
    # part already has its year, so a trailing year is not read as a time.
    m = re.fullmatch(r"(.+?)[ T](\d{4}|\d{6})", text)
    if m and re.search(r"\d{4}", m.group(1)):
        text = f"{m.group(1)} {_punctuate_time(m.group(2))}"

    for fmt in _TIME_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def pressure_to_depth(pressure_dbar: float, latitude_deg: float) -> float:
    """Pressure in decibars to depth in metres (UNESCO 1983, Fofonoff & Millard).

    Depends on latitude through gravity, so pass the cast's latitude.
    """
    x = math.sin(math.radians(latitude_deg)) ** 2
    gravity = 9.780318 * (1.0 + (5.2788e-3 + 2.36e-5 * x) * x) + 1.092e-6 * pressure_dbar
    numerator = ((((-1.82e-15 * pressure_dbar + 2.279e-10) * pressure_dbar
                   - 2.2512e-5) * pressure_dbar + 9.72659) * pressure_dbar)
    return numerator / gravity
