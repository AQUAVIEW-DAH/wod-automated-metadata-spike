# Phase 0 — Staging

Build a readable copy of a file that cannot be processed as it stands.

```python
from wod_etl.staging import (parse_coordinate, parse_timestamp, pressure_to_depth,
                             read_table, clean_sentinels, group_rows, fold_unit_row,
                             combine_levels, combine_on_key, pivot_long, melt_matrix,
                             read_station_list, join_station_list,
                             reconcile_stations, check_track,
                             extract_pdf_rows, write_staged_table, write_staged_cast,
                             write_staged_haul, read_staged_haul)
```

For the shapes these handle (coordinate forms, non-English headers, sentinel legends, casts
split across files, date/time traps), see
[`knowledge/accession_field_guide.md`](../../knowledge/accession_field_guide.md).

Phases 1–4 assume a file carries its own coordinates and parses as a table. Staging writes a
derived copy into `data/staging/` that does. `data/raw/` is never edited; the staged file
records where each added value came from.

Skip this phase when a file already has coordinates and a clean single-row header (most CTD
and float files). Bottle, nutrient, plankton and underway submissions usually need it.

These are primitives. `wod-etl` stages common shapes itself (long tables, moorings,
plankton) using them in `orchestration/recipes.py`. Anything else needs a staging script
(`kind = "script"` in `decisions.toml`). Either way, staging is logged to
`data/runs/<acc>/run.log`.

---

## Does this file need staging?

Look at the file and its siblings first.

| What you see | What it means | Fix |
| --- | --- | --- |
| A `latlon-*.csv`, `*_readme.csv` or station-list sibling | The data file has no positions | Join, then `write_staged_cast` per cast |
| One table, a `Station` column, hundreds of rows | One file holds the whole cruise | `group_rows`, one staged file per cast |
| Row 1 is a title, row 2 is the header | Decorated header | `read_table(header_row=1)` |
| `PrDM`/pressure but no depth | Depth axis must be derived | `pressure_to_depth` |
| `**NS`, `*DL`, `***NA` in cells | Originator missing-value legend | `clean_sentinels` |
| A second header row of units | Units invisible to detection | `fold_unit_row` |
| One file per depth level | Levels of one cast, split up | `combine_levels` |
| One file per variable | Variables of one cast, split up | `combine_on_key` |
| `StNo, parm, depth, value` | Long format: the variable is named in a cell | `pivot_long` |
| Taxa down, stations across | A count matrix | `melt_matrix`, then `write_staged_haul` |
| Positions only in a report table | A PDF holds the station list | `extract_pdf_rows` |

Positions and dates are sometimes only in a readme PDF. Read the readmes before deciding a
dataset cannot be staged.

---

## `read_table(path, header_row=0, encoding=None, drop_blank_rows=True, delimiter=",", comment_prefix=None)`

Returns `(columns, rows, info)`. Handles what stops `load_tabular`:

- **Encoding**: tries `config.TEXT_ENCODINGS` in order (e.g. cp932 for Japanese exports).
  `info["encoding"]` says which worked.
- **Blank rows**: files saved with `\r\r\n` have an empty row between every real one.
- **Title row above the header**: `header_row` indexes the non-blank rows.
- **Delimiter**: `","` by default. Pass `delimiter=None` to sniff it from the header (for tab-
  or semicolon-separated files). Sniffing never picks whitespace; fixed-width files belong
  to `read_any`.

```json
{"encoding": "utf-8-sig", "delimiter": ",", "rows_in_file": 1019,
 "blank_rows_dropped": 510, "data_rows": 507, "duplicate_columns": []}
```

`comment_prefix="#"` drops `# ...` rows before the header is located (as in a staged table's
`# Source:` lines).

**Check:**

- `blank_rows_dropped`: a large count confirms double-spacing.
- `duplicate_columns`: a merged header that needs decoding by hand.
- `len(columns)`: a tab-separated file read as CSV comes back as one column and is later
  skipped as holding no measurements.

---

## `clean_sentinels(rows, columns, extra=None)`

Replaces the originator's missing-value codes with empty fields and returns
`(rows, counts)` keyed `"<column> :: <code>"`. Report the counts.

| Code | Meaning | Consequence |
| --- | --- | --- |
| `**NS` | not sampled | absent |
| `***NA` | not analysed | absent |
| `*DL` | below detection limit | a real measurement under a stated threshold |
| `**ND` | not detected | same |

WOD Table 12 has no flag for below-detection, so `*DL` still becomes missing; report how many.
Do not substitute the detection limit.

The legend is usually in the last rows of the sheet or the readme, and differs between
submitters. Pass other codes via `extra`.

---

## `fold_unit_row(columns, units)`

Merges a second header row of units into the column names.

```python
cols, rows, _ = read_table(path)
cols = fold_unit_row(cols, rows[0])      # the units row
rows = rows[1:]                          # data starts after it
# ["time (UTC)", "latitude (degree N)", "water temperature (degree C)", "chlorophyll (ppb)"]
```

Unit detection reads only the column name, so a units row left in place is ignored and every
value is taken as standard. Columns that already carry a bracketed unit are left alone.

---

## `rename_columns(columns, renames, *, require_unit=True, strict=True)`

Returns `(columns, audit)`. Renames columns in a staged header, never in `data/raw/`.

```python
cols, audit = rename_columns(["LAT", "LON", "Depth (m)"], {"latitude": "Latitude"})
# (["Latitude", "LON", "Depth (m)"], {...})
```

Keys may be an exact column name or a plain name resolved through
[`resolve_column`](../mapping/README.md).

A column name carries the WOD variable (`match_variable`), the unit (`extract_unit`) and the
scale (`extract_scale`). A rename that loses one still validates, so these are refused:

| Rename | What is lost | What happens |
| --- | --- | --- |
| `Temp (deg F)` → `Temperature` | the unit | Fahrenheit values are read as Celsius |
| `Temperature [ITS-90, deg C]` → `Temperature (deg C)` | the scale | variable specific header 3 is lost |
| `T090C` → `Theta` | the code | the column maps to nothing and is dropped |

A refusal prints the fix (e.g. `use 'Temperature (deg F)'`) and applies nothing; the columns
come back unchanged.

Renames that only add information are allowed:

- `col_0` → `Depth (m)`: naming a headerless column.
- `DO` → `DO (mg/L)`: adding a unit, which makes the conversion detectable.

`strict=False` applies a refused rename anyway and sets `applied_with_problems`. A query
that matches no column, or a collision (two columns under one name), is always refused and
listed in `audit["blocked"]`.

Include `audit["renames"]` in your report.

### `suggest_column_names(columns, units=None)`

A proposal, not applied. Per column: the WOD variable, its unit, and a suggested name where
one is safe.

- Sensor pairs (`t090C`, `t190C`) both resolve to code 1, so they are listed under
  `collisions` with no suggestion.
- Renaming does not change output: the NetCDF variable name comes from the WOD code.
  Renaming is only for legibility of the staged file.

---

## `combine_levels(tables)` and `combine_on_key(tables, key, key_aliases=None)`

Combine a cast split across files before staging.

**One file per depth:**

```python
cols, rows, info = combine_levels([read_table(p)[:2] for p in sorted(files)])
rows.sort(key=lambda r: float(r[0]))
```

Check `info["schema_mismatches"]`: a table whose columns differ is dropped.

**One file per variable:** join on depth for a profile, or on a timestamp for an underway
track.

```python
cols, rows, info = combine_on_key([(c1, r1), (c2, r2)], key="Depth (m)",
                                  key_aliases=["DEPTH", "depth_m"])
```

Check `info["per_table"]` (`key_column`, `matched_existing_keys`, `distinct_keys`,
`duplicate_keys_within_table`):

| What you see | What it means |
| --- | --- |
| `matched_existing_keys` near zero | the files do not share a key; the result is disjoint blocks |
| `duplicate_keys_within_table` non-zero | one file repeats a key; the later row overwrites the earlier |
| `colliding_columns` non-empty | a variable appears in more than one table; only the last value survives |

- Judge overlap against `distinct_keys`, not `rows`.
- Keys are compared as raw text (`0`, `0.0` and `00.0` differ). Normalise the key column
  when files format depths differently.

---

## `combine_files(paths)` (`combine.py`)

Merges one cast reported in several tables (chemistry in one, chlorophyll of the same
bottles in another) into one cast per station and date. Each table is split by station with
`read_any`; casts with the same station and date and positions within 0.05° are merged on
depth. A depth sampled twice in one table stays two levels; a column in two tables keeps the
first table's value. Returns the casts and `in_one_file_only`.

---

## `read_station_list(source, *, station=None, latitude=None, longitude=None, date=None, time=None, year=None, time_zone=None, header_row=0)` and `join_station_list(cast_keys, stations)`

Positions from a sibling station list. `read_station_list` detects the station, latitude,
longitude, date, time and year columns (or takes them by name), parses every coordinate form
`parse_coordinate` does, and returns `{station_key: {"station", "latitude", "longitude",
"date", "time", "time_known", "time_zone", "row", "raw", "source"}}` in file order.

```python
stations, info = read_station_list(SRC / "SHU1505_StationInfo.csv")
info["dropped"]      # footer remarks in the station column, by row -- never silent
info["duplicates"]   # a re-occupied station: key '7#2'. Match on station AND date then.
matched, report = join_station_list(cast_keys, stations)
report["casts_without_station"], report["stations_without_cast"]   # report BOTH sides
```

Keys are compared through `normalise_station`, so `St-01`, `stn 1`, `Station 1` and `1`
match. A zone named in a header (`Time (UTC)`) is recorded, not converted. `source` may also
be `(columns, rows)`, e.g. from `extract_pdf_rows`.

---

## `pivot_long(tables, keys, variable, value, *, key_aliases=None, columns=None)`

Long format names the variable in a cell (`Cruise,StNo,parm,depth,value`), so the single
`value` column maps to nothing. Pivot it, usually across one file per variable:

```python
tables = [read_table(p)[:2] for p in (temp_csv, sal_csv, fluor_csv)]
cols, rows, info = pivot_long(
    tables, keys=["StNo", "depth"], variable="parm", value="value",
    key_aliases={"depth": ["dep"]},                   # the temperature file says 'dep'
    columns={"Temperature": "Temperature (deg C)", "Salinity": "Salinity (PSU)",
             "fluorescence": "Fluorescence (ug/l)"})  # the unit goes in HERE
for (station,), levels in group_rows(rows, cols, ["StNo"]).items(): ...
```

Numeric keys compare as numbers (`4.969` = `4.9690`). Read `info`: `conflicts` (one key and
variable with two values; the first is kept, all are listed), `levels_missing_variable`, and
`per_table` (key columns each table used).

## `melt_matrix(rows, *, header_rows, label_columns, header_names, fill_down=(), fill_across=True, thousands=",", blank="absent")`

Converts a count sheet (taxa down the rows, samples across the columns, one or more header
rows naming each sample) into one row per non-blank cell: label columns + one column per
header row + `value`. Header values carry across blank cells (merged spreadsheet cells);
`fill_down` names label columns written once per group. A blank cell means "not found in this
sample" by default (`blank="absent"`: no row written, counted in `info["blank_cells"]`);
`blank="zero"` only if the originator says so.

---

## `reconcile_stations(sources, *, position_tolerance_km=5.0)` and `check_track(stations, *, outlier_factor=1.75, bounding_box=None)`

Cross-check positions and dates stated in more than one place (station list, trawl log,
report tables).

```python
report = reconcile_stations({"SHU1505_StationInfo.csv": stations,
                             "report p.4 Table 1": table1,          # extract_pdf_rows
                             "report p.5 Table 2 (trawls)": trawls},
                            position_tolerance_km=10)
report["conflicts"]     # [{"station": "6", "field": "date", "values": {...}}, ...]
check_track(stations)["flags"]   # an outlying hop, time running backwards, impossible speed
```

- Set the tolerance for the sources: two copies of one list should agree to the arc-minute;
  a trawl may be kilometres from its CTD station.
- `check_track` needs stations in occupation order. A hop over `outlier_factor` times the
  median often means a mistyped degree.
- Neither changes data. Settle each conflict from a third source or with the submitter, and
  record the choice in `notes`.

---

## `extract_pdf_rows(path, page, row_pattern, columns)` and `write_staged_table(path, columns, rows, *, source, notes=None)`

Extracts table rows from a PDF report. Describe one row as a regex (one group per column)
against the page text with whitespace collapsed:

```python
cols, rows, info = extract_pdf_rows(
    "0-data/Final.Report.U2015-015.JAPAN.pdf", page=8,
    row_pattern=r"(\d+) (Nov\. \d{2}, \d{4}) (\S+E) (\S+N) (\d+\.\d) (\d+)",
    columns=["Station", "Date", "Long", "Lat", "SST (deg C)", "Tow depth (m)"])
info["source"]   # 'Final.Report.U2015-015.JAPAN.pdf, PDF page 8 (printed page 5)'
```

`page` is the PDF's own index. Compare `info["matches"]` with the printed table; rows the
pattern misses are not flagged. `write_staged_table` saves rows with a `# Source:` citation so
`read_station_list` can read them back.

---

## `parse_coordinate(value, minutes=None, hemisphere=None)`

Signed decimal degrees from common coordinate forms, or `None`.

```python
parse_coordinate(62.16)             #   62.16
parse_coordinate("170 30.01 W")     # -170.500167
parse_coordinate("64-05.8454N")     #   64.097423
parse_coordinate("62 09 36 N")      #   62.16       degrees, minutes, seconds
parse_coordinate("142°18’E")        #  142.3        degree sign and a CURLY quote
parse_coordinate("N 21°00.5'")      #   21.008333   hemisphere first
parse_coordinate(62, 9.6, "N")      #   62.16       three separate columns
```

- Degree, minute and second marks (`°`, `′`, `’`, `'`, `″`, `"`, `deg`, `min`) are
  separators, including the curly `’` (U+2019).
- A hemisphere letter beats a leading minus sign.
- Check longitude sign: when a dataset has both a station list and in-file coordinates,
  confirm they agree.

---

## `parse_timestamp(value, time_value=None, year=None)`

A datetime from common spreadsheet forms: single-digit months (`2023/6/1`), a separate time
column (`parse_timestamp("20230624", "031903")`), month names (`"Nov. 09, 2015"`,
`"12-Nov-2015"`, `"Sept. 3 2015"`), and a year in its own column
(`parse_timestamp("Nov. 09", year=2015)`).

---

## `pressure_to_depth(pressure_dbar, latitude_deg)`

UNESCO 1983 (Fofonoff & Millard). WOD needs a depth axis; many CTD exports have only
pressure. Pass the cast's own latitude.

Keep the pressure column too (WOD variable 25), and record the derivation in `notes`.

---

## `read_instrument(path)`, `deployment(windows)`, `mooring_days(paths, instruments, window)` and `mooring_profiles(instruments, days, depths, latitude, min_day_fraction=0.8)`

Builds WOD MRB profiles from a mooring. A moored buoy's profile is its instruments'
concurrent readings at their depths (manual p.17), kept as daily averages (p.14). An
accession usually holds one NetCDF file per instrument.

```python
from wod_etl.staging import (read_instrument, wet_window, deployment, mooring_days,
                             mooring_profiles)

instruments = [read_instrument(p) for p in paths]       # variables, pressure, depth, position
found = deployment([wet_window(p, i) for p, i in zip(paths, instruments)])
days = mooring_days(paths, instruments, found["window"])  # daily means while deployed
built = mooring_profiles(instruments, days, {"RBRsolo_SN1.nc": 10.0,
                                             "SBE37_SN2.nc": "pressure"}, latitude=56.84)
built["profiles"]   # [{"day": "2018-09-04", "levels": [{"depth", "means": {code: value},
                    #   "file", "instrument", "samples", "placed_by"}]}]
built["left_out"]   # instrument-days whose variables covered too little of the day
```

- **Depth.** An instrument with pressure is placed by its daily mean pressure (UNESCO 1983
  at the mooring's latitude; kPa, bar etc. converted; absolute pressure minus atmosphere; a
  gauge `sea_pressure` preferred). Otherwise by its file (`instrument_depth`, a depth
  variable, a nominal pressure in dbar), its file name (`depth_from_name`: `T_45m.nc`), or
  the mooring's description (below). Nothing else places it.
- **Deployment only.** `wet_window` finds when a pressure-recording instrument was under
  water (above half its usual submerged pressure; `in_water` also returns that pressure).
  `deployment` spans first in to last out (`how = "apart"` when windows do not overlap;
  `"none"` without pressure, in which case triage drops the first and last day).
  `mooring_days` uses only samples within the given window.
- **Grouping.** `group_moorings(entries)` groups triage entries by the mooring id the files
  name, else by folder and position, splits groups whose records stop overlapping in time,
  and labels each uniquely.
- **Part days are left out.** A variable gets a daily mean only if it has values in at least
  `min_day_fraction` of the day's hours (instruments sampling less than hourly count their
  own steps). One variable's gap does not cost another its day.
- **Originator flags.** Values flagged bad, fail or missing (`flag_meanings`, via
  `ancillary_variables` or a `<name>_qc` variable) are not averaged. Flags without stated
  meanings are not applied, and the instrument's `notes` say so.
- **Variables.** Only Table 3a's MRB variables are kept (temperature, salinity, pressure,
  oxygen, chlorophyll…), matched by CF standard name first. Instrument temperature, air
  values, potential temperature, saturation and duplicate records of a variable are listed
  in `not_stored`. Non-standard calendars and files with several depths along one dimension
  are refused with the reason. Records are read in chunks, so large loggers are fast.

`wod-etl` runs this from the mooring recipe triage works out (`placed` notes, per file, where
a depth came from) and writes the staged casts as MRB.

### `read_layout(path)`, `find_layout(candidates, members)` and `place_instruments(layout, members)`

Reads a mooring's description (`staging/mooring_layout.py`): a table (org/markdown pipes or
delimited) listing instruments by serial number with a depth, or a position along the line
from a reference point. `serial_key` normalises serials (`07819`, `SN07819` and
`37SM56061-7819` match).

```python
layout = read_layout("data/0-data/deep/mooring_description.txt")
placed = place_instruments(layout, [
    {"name": "SBE37SM_SN10552.nc", "serial": "10552", "depth": 34.7, "by_pressure": True},
    {"name": "RBRsolo_SN100028.nc", "serial": "100028", "depth": None}])
placed["placed"]["RBRsolo_SN100028.nc"]
# {"depth": 37.7, "how": "position", "short": "45 m up the line from point 1 (...)",
#  "detail": "45 m up the line from reference point 1 (Bottom of 50m line) in ..."}
```

- A stated depth is used as is.
- A position becomes a depth only when an instrument positioned from the same reference
  point records pressure; that fixes the point. Direction up or down the line comes from the
  header ("height above bottom"), the point's description ("bottom of 50m line"), or
  instruments with known depth. A pressure instrument between two positions bounds the
  point and may shift it by a few metres (`_tolerance`). A blank reference cell takes the
  one named in its block, else the one above.
- A logger listed beside one pressure instrument, with no position of its own, is at that
  instrument's depth.
- Nothing else is guessed. No pressure instrument on the line, records disagreeing with the
  table, out-of-order placements or a duplicated serial give `unplaced` with the reason, and
  triage proposes the file left out.

## `group_rows(rows, columns, key_columns)`

Splits a cruise-wide table into per-cast groups in file order. Group by `["Station"]`, or
`["Station", "Date"]` when a station was occupied more than once.

Footer legends often have text in the station column and form their own group. Drop groups
whose position does not parse, and log what was dropped.

---

## `write_staged_cast(path, columns, rows, *, latitude, longitude, date, time=None, metadata=None, notes=None, sources=None, time_zone=None)`

Writes the header block Phase 1 reads, then the CSV table.

```python
write_staged_cast(
    out / "C145-001.csv",
    columns=["Depth (m)", "T090C", "Sal00"], rows=levels,
    latitude=62.16, longitude=-170.500167, date="2023-06-20", time="14:55",
    metadata={"Accession": "0287423", "Platform": "Oshoro maru",
              "Station": "a01", "Bottom Depth": 47},
    notes=["Depth computed from PrDM by UNESCO 1983 at the cast latitude"],
    sources=["data/raw/0287423/2-CTD/C145-001.asc", "data/raw/0287423/2-CTD/CTD_readme.csv"])
```

`metadata` keys become `# Key: value` lines:

- Accession, Platform, Station, Cast and Bottom Depth become Table 4a secondary headers.
- `Cruise` is the originator's cruise code (character data, not the platform).
- `Instrument`, `Method` and other Table 5a keys apply to every measured variable.
- A key with no WOD field goes to `unresolved` and the output's `metadata_not_placed`.
- Coded values (platform, instrument) are looked up in WOD's code lists (see the
  [mapping README](../mapping/README.md)).

`notes` become the NetCDF `comment`; `sources` become `source_files_raw`. `time_zone` gives
the zone of `date`/`time` when not UTC: a numeric offset (`UTC+10`) with a time of day is
converted to UTC by the writer; a named zone or a date alone is recorded, not shifted.

**Rules for the staged file:**

1. Column names carry their units: `Temp (deg C)`, `DO (mg/L)`, `Depth (m)`.
2. Every derived value goes in `notes` (depth from pressure, a position from a readme PDF,
   a depth of 0 by convention, a cast matched across two files), and in your report.

---

## `write_staged_haul(path, records, *, latitude, longitude, date, time=None, time_zone=None, upper_depth=None, lower_depth=None, biological_header=None, metadata=None, notes=None, sources=None)`

Stages biological data (plankton counts, biomass) as one haul. A net haul samples a depth
range and each observation is a taxon with qualifiers, so WOD stores it as a Biological
Header (Table 6a: how it was collected) and Taxa-Records (Table 7a), not as a profile. See
[`knowledge/plankton_biomass.md`](../../knowledge/plankton_biomass.md); the rest of the path
is `wod_etl.orchestration.process_taxa`.

`records` is `(columns, rows)` or a list of dicts. Recognised columns (bracketed units are
ignored for matching; `taxa_record_field(name)` gives a column's field):

| Column | Table 7a field |
| --- | --- |
| `Taxon` / `Species` / `Taxa or species` | resolved later to field 1, the variable number |
| `Upper depth (m)`, `Lower depth (m)` | 2, 3, or pass `upper_depth`/`lower_depth` for the whole haul |
| `Life stage` / `Stage`, `Sex` | 5, 6 (coded) |
| `Count` / `Number` / `Abundance` | 10, count per UNIT |
| `Unit` | 20, the originator's UNIT for every "per UNIT" field |
| `Biomass`, `Volume`, `Wet weight`, `Dry weight`, `Carbon`, `Size min/max`, `Length`... | 4, 12, 13, 14, 25, 18/19, 22... |

Unmatched columns are listed in `unmapped_columns`. `biological_header` is keyed by Table 6a
name or id (`{"Gear code": "NORPAC Net", "Mesh size": 100, "Type of tow": "vertical tow"}`);
a key that is not a Table 6a field becomes a note and is listed in
`unknown_biological_fields`.

The return includes `missing_biological_fields`: which of gear, tow type, mesh, volume
filtered or tow distance, preservative and count method (`config.KEY_BIOLOGICAL_FIELD_IDS`)
the haul does not state. Report them. Without volume filtered or tow distance, a count stays
per sample.

The file is a `# Record type: WOD taxa-records` marker, `# Key: value` lines (`# Bio
<field>:` for Table 6a), `LAT:`/`LON:`/`DATE:`, then a CSV. `classify_dataset` recognises
the marker, so `process_accession` skips a staged haul with the route instead of failing it.
`read_staged_haul(path)` reads one back.

---

## Worked patterns

### A. Positions in a sibling station list

The data files hold only measurements; a station list keyed by file name or station holds
the coordinates. Common for CTD, XCTD and XBT.

```python
stations, info = read_station_list(SRC / "CTD_readme.csv")        # cp932 is handled
casts = {p.stem: p for p in sorted(SRC.glob("*.asc"))}
matched, report = join_station_list(casts, stations)
for name in report["casts_without_station"]:
    print(f"!! {name}: no station-list entry -- not staged")      # never silent
for name, s in matched.items():
    cols, levels = read_instrument_export(casts[name])
    write_staged_cast(OUT / f"{name}.csv", cols, levels,
                      latitude=s["latitude"], longitude=s["longitude"],
                      date=s["date"], time=s["time"], time_zone=s["time_zone"],
                      metadata={"Accession": ACC, "Platform": SHIP,
                                "Station": s["station"], "Cast": name},
                      sources=[f"data/raw/{ACC}/{casts[name].name}", s["source"]],
                      notes=[f"position and date from {s['source']}"])
```

- Reconcile both file lists first. A name on one side only is often the same cast spelt
  differently; confirm by gear, columns and depth range, and record the match in `notes`.
- Check the station list's time convention. A `Date time (UTC)` column next to a
  `Time zone` column is ambiguous; if there is an underway GPS track, the right convention
  puts the ship at the cast position.

### B. One cruise-wide table

A single sheet holds every station; a cast is a group of rows. Common for bottle, nutrient
and plankton data.

```python
cols, rows, info = read_table(path, header_row=1)             # row 0 is a title
rows, sentinels = clean_sentinels(rows, cols)                 # report these counts

idx = {c: cols.index(c) for c in cols if c}
get = lambda r, c: (r[idx[c]].strip() if c in idx and idx[c] < len(r) else "")

for key, group in group_rows(rows, cols, ["Station", "Date"]).items():
    lat, lon = parse_coordinate(get(group[0], "Latitude")), parse_coordinate(get(group[0], "Longitude"))
    stamp = parse_timestamp(key[1])
    if lat is None or lon is None or stamp is None:
        print(f"   dropped group {key[0][:40]!r} -- no usable position/date")
        continue                                              # the footer legend lands here
    levels = [[get(r, c) for c in table_cols] for r in group]
    levels.sort(key=lambda x: float(x[0]) if x[0] else 1e9)   # order by depth
    write_staged_cast(OUT / f"{key[0]}_{stamp:%Y-%m-%d}.csv", out_cols, levels,
                      latitude=lat, longitude=lon, date=f"{stamp:%Y-%m-%d}",
                      metadata={"Accession": ACC, "Station": key[0]})
```

- Group on station and date when a station was occupied more than once.
- Sort levels by depth. Bottle sheets are often bottom-up, which trips the depth-inversion
  check.

### C. Long format, one file per variable, positions in a station list

Temperature, salinity and fluorescence each in a `Cruise,StNo,ObsItem,parm,depth,value`
file covering every station; positions in a station list as `142°18’E` with the year in its
own column.

```python
stations, _ = read_station_list(SRC / "SHU1505_StationInfo.csv")
tables = [read_table(SRC / f)[:2] for f in ("U2015-015_CTD_Temp.csv",
                                             "U2015-015_CTD_Salinity.csv",
                                             "U2015-015_CTD_fluor.csv")]
cols, rows, info = pivot_long(tables, keys=["StNo", "depth"], variable="parm",
                              value="value", key_aliases={"depth": ["dep"]},
                              columns={"Temperature": "Temperature (deg C)",
                                       "Salinity": "Salinity (PSU)",
                                       "fluorescence": "Fluorescence (ug/l)"})
groups = group_rows(rows, cols, ["StNo"])
matched, report = join_station_list([k[0] for k in groups], stations)
for (st,), levels in groups.items():
    s = matched[st]
    levels = sorted(([r[1]] + r[2:] for r in levels), key=lambda r: float(r[0]))
    write_staged_cast(OUT / f"SHU1505_st{int(st):02d}.csv",
                      ["Depth (m)"] + cols[2:], levels,
                      latitude=s["latitude"], longitude=s["longitude"], date=s["date"],
                      metadata={"Accession": "0303928", "Cruise": "SHU1505",
                                "Platform": "Shunyo-Maru", "Station": st,
                                "Cast": f"SHU1505_st{int(st):02d}"},
                      sources=[...], notes=[...])
```

Then cross-check the station list with `reconcile_stations` (against a report table) and
`check_track` before trusting it.

---

## Conventions you may apply, and what to say about them

Some values must be supplied for a file to be ingested. Each must be written into the staged
header, not applied silently.

| Situation | Convention | Basis |
| --- | --- | --- |
| Surface underway or surface sample, no intake depth stated | depth = 0 m | SUR is surface data by definition (manual p.20) |
| Pressure but no depth | UNESCO 1983 conversion | standard, latitude-dependent |
| Position only in the readme PDF | use it, cite the file | it is the originator's own statement |

Never invent a coordinate, date or platform that no source states. A dataset with no
position anywhere (data, sibling list or readme) cannot be staged; report it as such.

---

## Next

[Phase 1 — Discovery](../discovery/README.md) reads the staged file as it would an
original.
