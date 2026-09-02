"""Stage NCEI accession 0146739 (NIWA New Zealand CTD) into per-cast files.

The submission is one WOD-export CSV per cruise: each cast is a `#---` separated record
holding a metadata block, a VARIABLES/UNITS/Prof-Flag header trio and its own level table.
Phase 1 cannot read that shape, so each cast is written out as its own staged file with the
units folded into the column names and the position/time in the header block.

Two derivations are recorded in every staged header:

  * UTC time.  The source states local time labelled `hours minutes (NZST)`.  NZST is
    UTC+12, so UTC = local - 12 h.  Verified against NCEI's own GMT station listing in
    about/1-email.txt, which converts the same casts with the same offset.
  * Nothing else.  Depth, temperature, salinity and the position are copied verbatim.
"""
import csv
import datetime as dt
import json
from pathlib import Path

from wod_etl.staging import write_staged_cast

RAW = Path("data/raw/0146739/0-data")
OUT = Path("data/staging/0146739/CTD")
REPORT = Path("data/staging/0146739/staging_report.json")

# The source labels every cast "hours minutes (NZST)"; NZST is UTC+12.
NZST_OFFSET_HOURS = 12

COLUMNS = ["Depth (m)", "Temperature (degrees C)", "Salinity (PSS)"]

# Short platform names, so the mined Table 4a header is not truncated at the first ';'
# of the originator's parenthetical description.
PLATFORM_NAME = {
    "8677.": "TANGAROA",
    "5423.": "KAHAROA",
    "10277": "SAN_WAITAKI",
    "10278.": "THOMAS_HARRISON",
    "10914.": "SEAMOUNT_EXPLORER",
}


def split_records(path):
    """Yield the lines of each `#---` delimited cast record in one cruise file."""
    recs, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#---"):
            if cur is not None:
                recs.append(cur)
            cur = []
            continue
        if cur is not None:
            cur.append(line)
    if cur is not None:
        recs.append(cur)
    return recs


def parse_record(lines):
    """Return (metadata, level rows) for one cast record."""
    meta, rows = {}, []
    in_table = False
    for line in lines:
        f = [x.strip() for x in line.split(",")]
        head = f[0]
        if head.startswith(("VARIABLES", "UNITS", "Prof-Flag")):
            in_table = True
            continue
        if head in ("METADATA", "END OF VARIABLES SECTION", ""):
            continue
        if in_table:
            if head.isdigit():
                rows.append(f)
            continue
        key = head if not f[1] else f"{head}|{f[1]}"
        meta[key] = {"value": f[2] if len(f) > 2 else "",
                     "unit": f[3] if len(f) > 3 else "",
                     "desc": f[4] if len(f) > 4 else ""}
    return meta, rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.csv"):
        stale.unlink()

    written, empty, skipped = [], [], []

    for path in sorted(RAW.glob("*.csv")):
        for index, rec in enumerate(split_records(path)):
            meta, rows = parse_record(rec)
            get = lambda k: meta.get(k, {}).get("value", "")
            cruise, station = get("Originators Cruise ID"), get("Originators Station ID")
            ident = f"{cruise}_st{station}"

            if not rows:
                empty.append({"file": path.name, "record": index, "cast": ident,
                              "latitude": get("Latitude"), "longitude": get("Longitude"),
                              "date_local": f"{get('Year')}-{get('Month')}-{get('Day')}",
                              "time_local": get("Time"), "bottom_depth_m": get("Bottom depth")})
                continue

            local = dt.datetime(int(get("Year")), int(get("Month")), int(get("Day")),
                                int(get("Time")[:2]), int(get("Time")[2:]))
            utc = local - dt.timedelta(hours=NZST_OFFSET_HOURS)

            # level, depth, F, O, temperature, F, O, salinity, F, O -> the three values
            levels = [[r[1], r[4], r[7]] for r in rows]

            platform_code = get("Platform")
            probe = get("probe_type")
            metadata = {
                "Accession": "0146739",
                "Platform": PLATFORM_NAME.get(platform_code, platform_code),
                "Institute": "NIWA",
                "Station": station,
                "Cast": ident,
                "Bottom Depth": get("Bottom depth"),
            }
            if probe:
                metadata["Probe Type"] = probe.rstrip(".")

            notes = [
                f"source: {path.name} record {index + 1} of the WOD-export CSV "
                f"(one metadata block and one level table per cast)",
                f"originator cruise {cruise}; platform OCL code {platform_code}; "
                f"institute NODC code 1344; country NZ; database origin WOD code 3 (GODAR)",
                f"time DERIVED: source states {local:%Y-%m-%d %H:%M} labelled NZST; "
                f"UTC = local minus {NZST_OFFSET_HOURS} h, giving {utc:%Y-%m-%d %H:%M}Z. "
                "Offset confirmed against NCEI's own GMT listing in about/1-email.txt",
                "originator QC flags were 0 (accepted) at every level and in every "
                "profile flag, for depth, temperature and salinity; they are not carried "
                "into the staged table because they add nothing",
                "originator marks both temperature and salinity uncalibrated (flag 1)",
            ]
            if not probe:
                notes.append("probe_type is blank in the source for this cast")

            res = write_staged_cast(
                OUT / f"{ident}.csv", COLUMNS, levels,
                latitude=float(get("Latitude")), longitude=float(get("Longitude")),
                date=f"{utc:%Y-%m-%d}", time=f"{utc:%H:%M}",
                metadata=metadata, notes=notes)
            written.append({"cast": ident, "file": path.name, "record": index,
                            "levels": res["data_rows"],
                            "utc": f"{utc:%Y-%m-%dT%H:%M}Z", "local": f"{local:%Y-%m-%dT%H:%M}",
                            "latitude": float(get("Latitude")), "longitude": float(get("Longitude")),
                            "platform": PLATFORM_NAME.get(platform_code, platform_code),
                            "probe_type": probe.rstrip(".") or None})

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"written": written, "empty_records": empty,
                                  "skipped": skipped}, indent=1))
    print(f"staged {len(written)} casts, {sum(w['levels'] for w in written)} levels")
    print(f"skipped {len(empty)} header-only records with no levels")
    for e in empty:
        print(f"   no levels: {e['cast']:>16}  {e['file']}  record {e['record'] + 1}")
    print(f"report: {REPORT}")


if __name__ == "__main__":
    main()
