"""Phase 0 staging for NCEI accession 0062621 (NABOS CTD, I/B Kapitan Dranitsyn).

Two originator text files hold a whole cruise each: a four-line file preamble, then per
cast a `Stn:` header line, a `Number of levels:` line, a fixed-width column header and the
levels. Phase 1 cannot read that, so each cast becomes one staged CSV carrying its own
position, date and provenance notes.

The source carries pressure and no depth, so the depth axis is derived per cast at the
cast's own latitude. Density (sigma-t), V0 (raw sensor voltage) and Fluor (uncalibrated
fluorometer output) are carried through unchanged so they surface in `unmapped_columns`
rather than disappearing here.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wod_etl.staging import parse_coordinate, pressure_to_depth, write_staged_cast

RAW = Path("data/raw/0062621/0-data")
STAGE = Path("data/staging/0062621")

FILES = {
    "2008CTD_SBE19plus_reprocessed-1.txt": ("2008-CTD", "NABOS-08"),
    "2009CTD_SBE19plus.txt": ("2009-CTD", "NABOS-09"),
}

# `Stn: KD0309 ;  Lat: 81.671N;  Lon:  31.307E;  Time: 2009. 8.18;  Depth: -999m`
STN_RE = re.compile(
    r"^\s*Stn:\s*(?P<stn>\S+)\s*;\s*Lat:\s*(?P<lat>[0-9.]+[NS])\s*;"
    r"\s*Lon:\s*(?P<lon>[0-9.\s]+[EW])\s*;"
    r"\s*Time:\s*(?P<y>\d{4})\.\s*(?P<m>\d{1,2})\.\s*(?P<d>\d{1,2})\s*;"
    r"\s*Depth:\s*(?P<bot>-?\s*\d+)\s*m\s*$"
)
# `Number of levels: 2201;  Ice:   0%      Comment: CTD`
LEV_RE = re.compile(r"^\s*Number of levels:\s*(?P<n>\d+)\s*;\s*Ice:\s*(?P<ice>\d+)\s*%"
                    r"(?:\s*Comment:\s*(?P<comment>.*?))?\s*$")

SRC_COLS = ["PR", "TEMP", "SALNTY", "Density", "V0", "Fluor"]
# Units folded into the names: unit detection reads the full column name and nothing else.
# The three WOD has no Table 3a code for keep names that say so.
OUT_COLS = ["Depth (m)", "PR (dbar)", "TEMP (degrees C)", "SALNTY",
            "Density (sigma-t kg/m3)", "V0 (volts)", "Fluor (uncalibrated)"]
MISSING = -9.0


def parse_casts(path):
    """Yield (header_dict, levels_line_dict, rows, first_data_lineno) per cast."""
    cast = None
    with open(path, encoding="utf-8", errors="strict") as f:
        for lineno, line in enumerate(f, 1):
            m = STN_RE.match(line)
            if m:
                if cast:
                    yield cast
                cast = {"hdr": m.groupdict(), "lev": None, "rows": [], "lineno": lineno}
                continue
            if cast is None:
                continue                                    # file preamble
            m = LEV_RE.match(line)
            if m:
                cast["lev"] = m.groupdict()
                continue
            s = line.strip()
            if not s or s.startswith("PR"):                  # column header
                continue
            parts = s.split()
            if len(parts) != len(SRC_COLS):
                raise ValueError(f"{path.name}:{lineno}: {len(parts)} fields, expected "
                                 f"{len(SRC_COLS)}: {s!r}")
            cast["rows"].append([float(p) for p in parts])
    if cast:
        yield cast


def main():
    totals = {"casts": 0, "levels": 0, "missing": {c: 0 for c in SRC_COLS},
              "level_count_mismatch": [], "dates": []}
    for fname, (subdir, project) in FILES.items():
        src = RAW / fname
        out_dir = STAGE / subdir
        n_casts = 0
        for cast in parse_casts(src):
            h, lev = cast["hdr"], cast["lev"]
            lat = parse_coordinate(h["lat"])
            lon = parse_coordinate(h["lon"].replace(" ", ""))
            if lat is None or lon is None:
                raise ValueError(f"{src.name}:{cast['lineno']}: unparsable position {h}")
            date = f"{int(h['y']):04d}-{int(h['m']):02d}-{int(h['d']):02d}"
            bottom = int(h["bot"].replace(" ", ""))
            declared = int(lev["n"]) if lev else None
            if declared is not None and declared != len(cast["rows"]):
                totals["level_count_mismatch"].append(
                    (src.name, h["stn"], declared, len(cast["rows"])))

            rows = []
            for r in cast["rows"]:
                vals = ["" if v == MISSING else v for v in r]
                for c, v in zip(SRC_COLS, r):
                    if v == MISSING:
                        totals["missing"][c] += 1
                if vals[0] == "":
                    raise ValueError(f"{src.name} {h['stn']}: missing pressure level")
                rows.append([round(pressure_to_depth(r[0], lat), 3)] + vals)

            notes = [
                "Depth (m) derived from PR (dbar) by UNESCO 1983 (Fofonoff & Millard) at "
                "this cast's latitude; the source file carries pressure and no depth.",
                "Source missing-value code -9 (stated in the source file preamble, "
                "'Missing value: -9') replaced with empty fields.",
                "Originator reports date only; no time of day is given for any cast in "
                "this accession, so TIME is absent rather than assumed.",
                "Density (sigma-t), V0 (raw sensor voltage) and Fluor have no WOD Table 3a "
                "code and are carried through unmapped. Fluor is an uncalibrated linear "
                "rescale of V0 (Fluor = 50.995*V0 - 6.272, r2 = 0.9999 over both files) "
                "and stays non-zero below 1800 m, so it is NOT calibrated chlorophyll "
                "(code 11) and was deliberately not mapped to it.",
                f"Staged from {src.name} line {cast['lineno']} "
                f"(archive copy: data/raw/0062621/0-data/{src.name}).",
            ]
            if bottom == -999:
                notes.append("Originator bottom depth is -999 = not reported; "
                             "no Bottom Depth recorded.")
            meta = {
                "Accession": "0062621",
                "Project": project,
                "Platform": "Kapitan Dranitsyn",
                "Institution": "University of Alaska Fairbanks",
                "Station": h["stn"],
                "Instrument": "SBE19plus CTD",
            }
            if bottom != -999:
                meta["Bottom Depth"] = bottom
            if lev and lev.get("ice") is not None:
                meta["Ice Concentration (%)"] = int(lev["ice"])

            write_staged_cast(out_dir / f"{h['stn']}.csv", columns=OUT_COLS, rows=rows,
                              latitude=lat, longitude=lon, date=date,
                              metadata=meta, notes=notes)
            n_casts += 1
            totals["levels"] += len(rows)
            totals["dates"].append(date)
        totals["casts"] += n_casts
        print(f"{fname}: staged {n_casts} casts -> {out_dir}")

    print(f"\ntotal casts={totals['casts']} levels={totals['levels']}")
    print(f"date range: {min(totals['dates'])} .. {max(totals['dates'])}")
    print("missing (-9) values replaced, by source column:")
    for c in SRC_COLS:
        print(f"  {c:8s} {totals['missing'][c]}")
    if totals["level_count_mismatch"]:
        print("LEVEL COUNT MISMATCHES (declared vs actual):")
        for row in totals["level_count_mismatch"]:
            print("  ", row)
    else:
        print("declared 'Number of levels' matches actual row count for every cast")


if __name__ == "__main__":
    main()
