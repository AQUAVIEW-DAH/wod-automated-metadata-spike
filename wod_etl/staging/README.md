# Phase 0 — Staging

**Build a readable copy of a file that cannot be processed as it stands.**

```python
from wod_etl.staging import (parse_coordinate, parse_timestamp, pressure_to_depth,
                             read_table, clean_sentinels, group_rows, fold_unit_row,
                             combine_levels, combine_on_key, pivot_long, melt_matrix,
                             read_station_list, join_station_list,
                             reconcile_stations, check_track,
                             extract_pdf_rows, write_staged_table, write_staged_cast,
                             write_staged_haul, read_staged_haul)
```

For the shapes these exist to handle -- coordinate forms, non-English headers, sentinel
legends, split-across-files casts, date/time traps -- see
[`knowledge/accession_field_guide.md`](../../knowledge/accession_field_guide.md).

Phases 1–4 assume a file carries its own coordinates and parses as a table. Accessions
routinely break that assumption. Staging closes the gap by writing a **derived** copy into
`data/staging/` that does carry them. The archive copy in `data/raw/` is never edited — it
is the provenance record, and the staged file records where each added value came from.

Skip this phase when a file already has coordinates and a clean single-row header. Most
CTD and float files do. Bottle, nutrient, plankton and underway submissions usually don't.

---

## Does this file need staging?

Peek at it and at its siblings before deciding. Five signals, each with a fix:

| What you see | What it means | Fix |
| --- | --- | --- |
| A `latlon-*.csv`, `*_readme.csv` or station-list sibling | The data file has no positions | Join, then `write_staged_cast` per cast |
| One table, a `Station` column, hundreds of rows | One file holds the whole cruise | `group_rows`, one staged file per cast |
| Row 1 is a title, row 2 is the header | Merged/decorated header | `read_table(header_row=1)` |
| `PrDM`/pressure but no depth | Depth axis must be derived | `pressure_to_depth` |
| `**NS`, `*DL`, `***NA` in cells | Originator missing-value legend | `clean_sentinels` |
| A second header row of units | Units invisible to detection | `fold_unit_row` |
| One file per depth level | Levels of one cast, split up | `combine_levels` |
| One file per variable | Variables of one cast, split up | `combine_on_key` |
| `StNo, parm, depth, value` | Long format: the variable is named in a cell | `pivot_long` |
| Taxa down, stations across | A count matrix | `melt_matrix`, then `write_staged_haul` |
| Positions only in a report table | A PDF holds the station list | `extract_pdf_rows` |

**The coordinates may not be in any data file at all.** Positions and dates are sometimes
stated only in prose in the dataset's readme PDF. Read the readmes before concluding a
dataset cannot be staged.

---

## `read_table(path, header_row=0, encoding=None, drop_blank_rows=True, delimiter=",", comment_prefix=None)`

Returns `(columns, rows, info)`. Handles what stops `load_tabular` before it starts:

- **Encoding** — tries `config.TEXT_ENCODINGS` in order. A cp932 file raises
  `UnicodeDecodeError` on utf-8, which reads as a corrupt file when it is only a Japanese
  spreadsheet export. `info["encoding"]` says which one worked.
- **Blank rows** — files saved with `\r\r\n` interleave an empty row between every real
  one. Undetected, this shifts the header and doubles the row count.
- **A title row above the header** — `header_row` indexes the *non-blank* rows.
- **The delimiter** — `","` by default. Pass **`delimiter=None`** to sniff it from the
  header, which is what a tab- or semicolon-separated file needs.

```json
{"encoding": "utf-8-sig", "delimiter": ",", "rows_in_file": 1019,
 "blank_rows_dropped": 510, "data_rows": 507, "duplicate_columns": []}
```

`comment_prefix="#"` drops `# ...` rows before the header is located -- what a staged table's
`# Source:` lines need.

**Check `blank_rows_dropped` and `duplicate_columns`.** A large blank count confirms the
double-spacing; duplicates mean a merged header that needs decoding by hand.

**Check `len(columns)` when you did not pass `delimiter=None`.** A tab-separated file read
as CSV comes back as **one** column per row. That does not look like a delimiter problem
three phases later — it looks like a file holding no measurements, and it is skipped with
that reason. Comma remains the default so a file that read correctly before still does,
and sniffing deliberately never considers whitespace: whitespace also separates
fixed-width columns, and calling a fixed-width file whitespace-delimited is what produces
ragged rows and a phantom column. A fixed-width file belongs to `read_any`, not here.

---

## `clean_sentinels(rows, columns, extra=None)`

Replaces the originator's missing-value codes with empty fields and returns
`(rows, counts)` keyed `"<column> :: <code>"`.

**Report the counts.** Two of the codes are not missing data:

| Code | Meaning | Consequence |
| --- | --- | --- |
| `**NS` | not sampled | genuinely absent |
| `***NA` | not analysed | genuinely absent |
| `*DL` | **below detection limit** | a real measurement under a stated threshold |
| `**ND` | **not detected** | same |

WOD Table 12 has no observed-level flag for below-detection, so `*DL` still becomes
missing — but that is a loss worth stating, and it can be large. Do **not** substitute the
detection limit for it: that fabricates a measurement the originator did not report.

The legend defining these codes is usually in the last rows of the sheet, or in the
readme. Read it — the codes are not standardised across submitters. Pass anything unusual
via `extra`.

---

## `fold_unit_row(columns, units)`

Merges a second header row of units into the column names.

```python
cols, rows, _ = read_table(path)
cols = fold_unit_row(cols, rows[0])      # the units row
rows = rows[1:]                          # data starts after it
# ["time (UTC)", "latitude (degree N)", "water temperature (degree C)", "chlorophyll (ppb)"]
```

Unit detection reads the full column name and nothing else, so a units row left in place is
invisible and every value is taken as already-standard. This is what makes `mg/l`, `ml/l` and
`deg F` detectable at all. Columns that already carry a bracketed unit are left alone.

---

## `rename_columns(columns, renames, *, require_unit=True, strict=True)`

Returns `(columns, audit)`. Renames columns in a **staged** header — never in `data/raw/`.

```python
cols, audit = rename_columns(["LAT", "LON", "Depth (m)"], {"latitude": "Latitude"})
# (["Latitude", "LON", "Depth (m)"], {...})
```

Keys may be an exact column name **or a human's name for one**, resolved through
[`resolve_column`](../mapping/README.md), so the caller does not have to know the column is
spelt `LAT`.

A column name here is not a label. It is the only carrier of three separate facts —
`match_variable` reads it for the WOD variable code, `extract_unit` for the originator's
unit, `extract_scale` for the measurement scale — and nothing else supplies any of them. So
a rename has three ways to destroy data, and **every one of them still validates**:

| Rename | What is lost | What happens |
| --- | --- | --- |
| `Temp (deg F)` → `Temperature` | the unit | Fahrenheit values are read as already-Celsius |
| `Temperature [ITS-90, deg C]` → `Temperature (deg C)` | the scale | variable specific second header 3 is gone; a temperature is ambiguous without it |
| `T090C` → `Theta` | the code | code 1 becomes no code; the column is dropped |

Each is refused by default, with the fix printed — the first returns
`use 'Temperature (deg F)'`. Refused means **nothing is applied**: the columns come back
unchanged, so a bad rename cannot reach `write_staged_cast`.

Two renames are safe and need no escape hatch, because they only ever add:

- **`col_0` → `Depth (m)`** — naming a headerless fixed-width column. No code becomes a
  code; that is the point.
- **`DO` → `DO (mg/L)`** — folding in a unit from a units row or a readme. This is the one
  rename that genuinely changes behaviour, because it makes the conversion detectable.

`strict=False` applies a judgement call anyway and sets `applied_with_problems`. It still
refuses the **structural** ones: a query that matches no column, and a collision. A
collision is not a judgement — two columns under one name loses one of them with nothing
said — so `audit["blocked"]` names those regardless.

Put `audit["renames"]` in your report. A renamed column is a claim about the source.

### `suggest_column_names(columns, units=None)`

A **proposal**, applied by nothing. Per column: the WOD variable it resolves to, its unit,
and a suggested name where one is safe.

It deliberately does not offer a canonical name for everything, for two reasons:

- **Sensor-indexed pairs collide.** `t090C` and `t190C` are the primary and secondary
  temperature sensor and **both resolve to code 1**. There is no single canonical name that
  keeps both, so they appear under `collisions` and get no suggestion. Canonicalising them
  would drop a real measurement.
- **A canonical name changes no output.** The NetCDF variable name comes from the WOD code
  via `wod_name`, not from the column name. Renaming is for the legibility of the staged
  file; it is not a precondition for conversion.

---

## `combine_levels(tables)` and `combine_on_key(tables, key, key_aliases=None)`

One cast is often split across several files. Both shapes produce silently wrong output if
not combined *before* staging.

**One file per depth** — each file is a single level. Left alone you get a pile of
single-level profiles instead of one profile.

```python
cols, rows, info = combine_levels([read_table(p)[:2] for p in sorted(files)])
rows.sort(key=lambda r: float(r[0]))
```

Check `info["schema_mismatches"]` — a table whose columns differ is dropped rather than
misaligned.

**One file per variable** — temperature in one, oxygen in another. Join on depth for a
profile, or on a timestamp for an underway track.

```python
cols, rows, info = combine_on_key([(c1, r1), (c2, r2)], key="Depth (m)",
                                  key_aliases=["DEPTH", "depth_m"])
```

**Check `info["per_table"]`.** It reports, per table, its `key_column`,
`matched_existing_keys`, `distinct_keys` and `duplicate_keys_within_table`. Three failures
live in there, and all three look like success:

| What you see | What it means |
| --- | --- |
| `matched_existing_keys` near zero | the files do not share a key; the join produced two disjoint blocks, not merged levels |
| `duplicate_keys_within_table` non-zero | **one file repeats a key.** The later row overwrites the earlier one and that measurement is gone |
| `colliding_columns` non-empty | a variable appeared in more than one table; only the last value survives |

**Judge overlap against `distinct_keys`, not `rows`.** A file that repeats a key has more
rows than keys, so `matched / rows` reads as healthy overlap on a file that is overwriting
itself. That is why `distinct_keys` is reported separately.

**Keys are compared as raw text**, so `0`, `0.0` and `00.0` are three different keys — which
makes the trap bidirectional. Files that genuinely share every depth can report **0%**
overlap and look unrelated, purely because one wrote `0` and the other `0.0`. Normalise the
key column before joining when the files spell their depths differently.

---

## `read_station_list(source, *, station=None, latitude=None, longitude=None, date=None, time=None, year=None, time_zone=None, header_row=0)` and `join_station_list(cast_keys, stations)`

The positions-in-a-sibling-file pattern as two calls. `read_station_list` detects the station,
latitude, longitude, date, time and year columns from the header (or takes them by name),
parses every coordinate form `parse_coordinate` does -- including `142°18’E` -- and returns
`{station_key: {"station", "latitude", "longitude", "date", "time", "time_known",
"time_zone", "row", "raw", "source"}}` in file order.

```python
stations, info = read_station_list(SRC / "SHU1505_StationInfo.csv")
info["dropped"]      # footer remarks in the station column, by row -- never silent
info["duplicates"]   # a re-occupied station: key '7#2'. Match on station AND date then.
matched, report = join_station_list(cast_keys, stations)
report["casts_without_station"], report["stations_without_cast"]   # report BOTH sides
```

Keys are compared through `normalise_station`: `St-01`, `stn 1`, `Station 1` and `1` meet.
A zone named in a header (`Time (UTC)`) is recorded; nothing is converted. `source` may also
be `(columns, rows)` -- rows from `extract_pdf_rows`, for instance.

---

## `pivot_long(tables, keys, variable, value, *, key_aliases=None, columns=None)`

Long format names the variable in a cell -- `Cruise,StNo,parm,depth,value` -- so the table's
one measurement column is called `value` and maps to nothing. Read as it stands it skipped as
a station list. Pivot it, usually across one file per variable:

```python
tables = [read_table(p)[:2] for p in (temp_csv, sal_csv, fluor_csv)]
cols, rows, info = pivot_long(
    tables, keys=["StNo", "depth"], variable="parm", value="value",
    key_aliases={"depth": ["dep"]},                   # the temperature file says 'dep'
    columns={"Temperature": "Temperature (deg C)", "Salinity": "Salinity (PSU)",
             "fluorescence": "Fluorescence (ug/l)"})  # the unit goes in HERE
for (station,), levels in group_rows(rows, cols, ["StNo"]).items(): ...
```

Numeric keys meet as numbers (`4.969` = `4.9690`). **Read `info`**: `conflicts` (one key and
variable, two values -- the first is kept and every one listed), `levels_missing_variable`,
and `per_table` (which key columns each table used).

## `melt_matrix(rows, *, header_rows, label_columns, header_names, fill_down=(), fill_across=True, thousands=",", blank="absent")`

A count sheet typed as taxa down the rows and samples across the columns, with one or more
header rows naming each sample (station, then mesh). Returns one row per non-blank cell:
label columns + one column per header row + `value`. Header values are carried across blank
cells (a merged spreadsheet cell); `fill_down` names label columns written once per group.
**A blank cell is "not found in this sample" by default** (`blank="absent"`, no row written,
counted in `info["blank_cells"]`) -- `blank="zero"` is a claim the originator must have made.

---

## `reconcile_stations(sources, *, position_tolerance_km=5.0)` and `check_track(stations, *, outlier_factor=1.75, bounding_box=None)`

A position or date taken from one file is a claim. Accessions state each station two or three
times -- station list, trawl log, report tables, a map -- and the copies disagree more often
than anyone checks.

```python
report = reconcile_stations({"SHU1505_StationInfo.csv": stations,
                             "report p.4 Table 1": table1,          # extract_pdf_rows
                             "report p.5 Table 2 (trawls)": trawls},
                            position_tolerance_km=10)
report["conflicts"]     # [{"station": "6", "field": "date", "values": {...}}, ...]
check_track(stations)["flags"]   # an outlying hop, time running backwards, impossible speed
```

Choose the tolerance for what the sources ARE: two transcriptions of one station list should
agree to the arc-minute, while a trawl is towed some kilometres from its CTD station.
`check_track` needs the stations in occupation order; a hop over `outlier_factor` times the
median hop is the signature of a mistyped degree. Neither changes anything: settle each
conflict from a third source or with the submitter, and record the choice in `notes`.

---

## `extract_pdf_rows(path, page, row_pattern, columns)` and `write_staged_table(path, columns, rows, *, source, notes=None)`

Rows of a table that exists only in a cruise report. PDF text loses the layout, so describe
ONE row as a regex, one group per column, against the page text with whitespace collapsed:

```python
cols, rows, info = extract_pdf_rows(
    "0-data/Final.Report.U2015-015.JAPAN.pdf", page=8,
    row_pattern=r"(\d+) (Nov\. \d{2}, \d{4}) (\S+E) (\S+N) (\d+\.\d) (\d+)",
    columns=["Station", "Date", "Long", "Lat", "SST (deg C)", "Tow depth (m)"])
info["source"]   # 'Final.Report.U2015-015.JAPAN.pdf, PDF page 8 (printed page 5)'
```

`page` is the PDF's own index. **Compare `info["matches"]` with the printed table**: a row the
pattern misses is absent, not flagged. `write_staged_table` saves the rows with a `# Source:`
citation so they can be read back with `read_station_list` and reconciled.

---

## `parse_coordinate(value, minutes=None, hemisphere=None)`

Signed decimal degrees from every form NCEI submissions use, or `None`.

```python
parse_coordinate(62.16)             #   62.16
parse_coordinate("170 30.01 W")     # -170.500167
parse_coordinate("64-05.8454N")     #   64.097423
parse_coordinate("62 09 36 N")      #   62.16       degrees, minutes, seconds
parse_coordinate("142°18’E")        #  142.3        degree sign and a CURLY quote
parse_coordinate("N 21°00.5'")      #   21.008333   hemisphere first
parse_coordinate(62, 9.6, "N")      #   62.16       three separate columns
```

Degree, minute and second marks -- `°`, `′`, `’`, `'`, `″`, `"`, `deg`, `min` -- are
separators. A Word- or Japanese-authored list writes `’` (U+2019), not an ASCII apostrophe.

A hemisphere letter beats a leading minus sign, because station lists often carry both.

**Longitude sign is the classic silent error.** A western longitude written as
`170 30.01 W` becomes −170.5; the same row copied into a data file as `170.5` is in the
Pacific's other half. When a dataset has both a station list and in-file coordinates,
check that they agree before trusting either.

---

## `parse_timestamp(value, time_value=None, year=None)`

A datetime from the usual spreadsheet forms, including single-digit months (`2023/6/1`),
a separate time column (`parse_timestamp("20230624", "031903")`), month names
(`"Nov. 09, 2015"`, `"12-Nov-2015"`, `"Sept. 3 2015"`), and a year held in its own column
(`parse_timestamp("Nov. 09", year=2015)`).

---

## `pressure_to_depth(pressure_dbar, latitude_deg)`

UNESCO 1983 (Fofonoff & Millard). CTD exports frequently carry pressure and no depth;
WOD requires a depth axis. Latitude-dependent through gravity, so pass the cast's own.

Keep the pressure column as well — pressure is WOD variable 25 in its own right, and
`Depth` is the vertical axis, not a variable. Record the derivation in `notes`.

---

## `group_rows(rows, columns, key_columns)`

Splits a cruise-wide table into per-cast groups in file order. Group by `["Station"]`, or
`["Station", "Date"]` when a station was occupied more than once.

**Footer legends group as if they were stations.** A legend line often has its text in the
station column, so a blank-key filter will not catch it. Drop a group when its position
does not parse, and log what was dropped.

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

`metadata` keys become `# Key: value` lines, and every one is placed or reported:
Accession, Platform, Station, Cast and Bottom Depth become Table 4a secondary headers;
`Cruise` is the originator's cruise code (character data, not the platform); `Instrument`,
`Method` and the other Table 5a keys apply to **every measured variable**; a key with no WOD
field lands in `unresolved` and in the output's `metadata_not_placed`. Coded values
(platform, instrument) are looked up in WOD's code lists -- see the mapping README.

`notes` reach the NetCDF as `comment` and `sources` as `source_files_raw`. `time_zone`
states the zone of `date`/`time` when it is not UTC: a numeric offset (`UTC+10`) with a time
of day is converted to UTC by the writer; a named zone or a date alone is recorded, not
shifted.

**Two rules for the staged file:**

1. **Column names must carry their units** — `Temp (deg C)`, `DO (mg/L)`, `Depth (m)`.
   Unit detection reads the full raw column name and nothing else. A units row underneath
   the header is invisible to it, so fold those units into the names.

2. **Every derived value goes in `notes`.** A depth computed from pressure, a position
   read out of a readme PDF, a depth of 0 applied by convention, a cast identified by
   matching two files — the staged file is the only place a later reader can learn the
   value was not in the source. Then repeat it in what you report.

---

## `write_staged_haul(path, records, *, latitude, longitude, date, time=None, time_zone=None, upper_depth=None, lower_depth=None, biological_header=None, metadata=None, notes=None, sources=None)`

**Biological data -- plankton counts, biomass -- staged as one haul.** WOD does not store it
as a profile: a net haul samples a depth *range*, and each observation is a taxon with
qualifiers. A haul is the counterpart of a cast: a position and date, a Biological Header
(Table 6a: how it was collected) and Taxa-Records (Table 7a). Background:
[`knowledge/plankton_biomass.md`](../../knowledge/plankton_biomass.md); the rest of the path
is `wod_etl.orchestration.process_taxa`.

`records` is `(columns, rows)` or a list of dicts. Recognised columns (a bracketed unit is
ignored for matching; `taxa_record_field(name)` gives a column's field):

| Column | Table 7a field |
| --- | --- |
| `Taxon` / `Species` / `Taxa or species` | resolved later to field 1, the variable number |
| `Upper depth (m)`, `Lower depth (m)` | 2, 3 -- or pass `upper_depth`/`lower_depth` for the whole haul |
| `Life stage` / `Stage`, `Sex` | 5, 6 (coded) |
| `Count` / `Number` / `Abundance` | 10, count per UNIT |
| `Unit` | 20, the originator's UNIT for every "per UNIT" field |
| `Biomass`, `Volume`, `Wet weight`, `Dry weight`, `Carbon`, `Size min/max`, `Length`... | 4, 12, 13, 14, 25, 18/19, 22... |

A column matching none is listed in `unmapped_columns`. `biological_header` is keyed by
Table 6a name or id -- `{"Gear code": "NORPAC Net", "Mesh size": 100, "Type of tow":
"vertical tow"}`; a key that is not a Table 6a field is **not** written as one: it becomes a
note and is listed in `unknown_biological_fields`.

The return carries **`missing_biological_fields`**: of gear, tow type, mesh, volume filtered
*or* tow distance, preservative and count method (`config.KEY_BIOLOGICAL_FIELD_IDS`), those
the haul does not state. Report them. Without volume filtered or tow distance a count stays
per sample; nothing here invents one.

The file is a `# Record type: WOD taxa-records` marker, `# Key: value` lines (`# Bio
<field>:` for Table 6a), `LAT:`/`LON:`/`DATE:`, then a CSV. `classify_dataset` recognises
the marker, so `process_accession` skips a staged haul with the route rather than failing
it. `read_staged_haul(path)` reads one back.

---

## Two worked patterns

Almost every accession that needs staging is one of these shapes.

### A. Positions in a sibling station list

The data files hold only the measurement table; a station list keyed by file name or station
carries the coordinates. Common for CTD, XCTD and XBT submissions.

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

**Reconcile the two file lists before staging.** A name present on one side and absent on the
other is usually the same cast under two spellings, not a missing cast — check gear, column
signature and depth range to confirm, then record the identification in `notes`. Never
silently skip an unmatched file.

**Verify the station list's time convention.** A `Date time (UTC)` column sitting beside a
`Time zone` column is ambiguous. If the accession has an underway GPS track, test the
positions against it: the correct convention puts the ship at the cast position.

### B. One cruise-wide table

A single sheet holds every station, and a cast is a group of rows. Common for bottle,
nutrient and plankton submissions.

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

**Group on station *and* date** when a station was occupied more than once; station alone
merges two casts into one.

**Sort levels by depth.** Bottle sheets are often in bottle-firing order, which is bottom-up,
and an unsorted profile trips the depth-inversion check at every level.

### C. Long format, one file per variable, positions in a station list

Accession 0303928: temperature, salinity and fluorescence each in its own
`Cruise,StNo,ObsItem,parm,depth,value` file covering every station, and the positions in
`SHU1505_StationInfo.csv` as `142°18’E` with the year in its own column. No custom parser:

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

Then cross-check before trusting the station list: `reconcile_stations` against the cruise
report's Table 1 found station 6 dated a day apart, and `check_track` flagged station 11's
hop at twice the median -- the trawl log puts it a degree east.

---

## Conventions you may apply, and what to say about them

Some values must be supplied for a file to be ingestable at all. These are defensible; each
must be written into the staged header rather than applied quietly.

| Situation | Convention | Basis |
| --- | --- | --- |
| Surface underway or surface sample, no intake depth stated | depth = 0 m | SUR is surface data by definition (manual p.20) |
| Pressure but no depth | UNESCO 1983 conversion | standard, latitude-dependent |
| Position only in the readme PDF | use it, cite the file | it is the originator's own statement |

**What is never allowed** is inventing a coordinate, date or platform that no source
states. If a dataset has no position anywhere — not in the data, not in a sibling list,
not in the readme — it cannot be staged. Report it as such.

---

## Next

[Phase 1 — Discovery](../discovery/README.md) reads the staged file exactly as it would
read an original.
