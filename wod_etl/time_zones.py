"""Source time zones and their offset from UTC.

Only unambiguous zones are converted; ambiguous or vague ones are left alone.
"""
import re
from typing import Any, Optional

import pandas as pd

# Unambiguous abbreviations with a fixed offset. Ambiguous ones (CST, IST, BST, AST,
# EST/EDT, MST/MDT, PST) are left out.
FIXED_ZONES = {"JST": 9, "KST": 9, "HKT": 8, "SGT": 8, "AWST": 8, "ACST": 9.5, "AEST": 10,
               "AEDT": 11, "NZST": 12, "NZDT": 13, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3,
               "WET": 0, "WEST": 1, "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10}
# Regional names whose offset depends on daylight saving, resolved per date.
REGIONAL_ZONES = {"ET": "America/New_York", "EASTERN": "America/New_York",
                  "CT": "America/Chicago", "CENTRAL": "America/Chicago",
                  "MT": "America/Denver", "PT": "America/Los_Angeles",
                  "PACIFIC": "America/Los_Angeles", "AKT": "America/Anchorage",
                  "NZT": "Pacific/Auckland"}


def utc_offset_hours(zone: Any, local=None) -> Optional[float]:
    """Hours to add to UTC to get the zone's local time, or None when unknown.

    Regional and IANA zones need `local` (the local date and time) for daylight saving.
    Ambiguous or vague zones ('CST', 'local') return None.
    """
    z = str(zone or "").strip().upper()
    if z in ("UTC", "GMT", "Z", "UT"):
        return 0.0
    m = re.fullmatch(r"(?:UTC|GMT|UT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", z)
    if m:
        hours = int(m.group(2)) + int(m.group(3) or 0) / 60.0
        return hours if m.group(1) == "+" else -hours
    if z in FIXED_ZONES:
        return float(FIXED_ZONES[z])
    name = REGIONAL_ZONES.get(z) or (str(zone).strip() if "/" in str(zone) else None)
    if name and local is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            offset = ZoneInfo(name).utcoffset(pd.Timestamp(local).to_pydatetime())
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return offset.total_seconds() / 3600.0 if offset is not None else None
    return None


ZONE_NAMES = sorted(list(FIXED_ZONES) + list(REGIONAL_ZONES), key=len, reverse=True)
