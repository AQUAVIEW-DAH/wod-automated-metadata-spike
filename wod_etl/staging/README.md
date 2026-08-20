# Phase 0 — Staging

**Build a readable copy of a file that cannot be processed as it stands.**

```python
from wod_etl.staging import (parse_coordinate, parse_timestamp, pressure_to_depth,
                             read_table, clean_sentinels, group_rows, fold_unit_row,
                             combine_levels, combine_on_key, write_staged_cast)
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

**The coordinates may not be in any data file at all.** Positions and dates are sometimes
stated only in prose in the dataset's readme PDF. Read the readmes before concluding a
dataset cannot be staged.

---

## `read_table(path, header_row=0, encoding=None, drop_blank_rows=True)`

Returns `(columns, rows, info)`. Handles what stops `load_tabular` before it starts:

- **Encoding** — tries `config.TEXT_ENCODINGS` in order. A cp932 file raises
  `UnicodeDecodeError` on utf-8, which reads as a corrupt file when it is only a Japanese
  spreadsheet export. `info["encoding"]` says which one worked.
- **Blank rows** — files saved with `\r\r\n` interleave an empty row between every real
  one. Undetected, this shifts the header and doubles the row count.
- **A title row above the header** — `header_row` indexes the *non-blank* rows.

```json
{"encoding": "utf-8-sig", "rows_in_file": 1019, "blank_rows_dropped": 510,
 "data_rows": 507, "duplicate_columns": []}
```

**Check `blank_rows_dropped` and `duplicate_columns`.** A large blank count confirms the
double-spacing; duplicates mean a merged header that needs decoding by hand.

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

**Check `info["per_table"]`.** A `matched_existing_keys` near zero means the files do not
share a key and the join produced two disjoint blocks rather than merged levels — which looks
like success and is not. `colliding_columns` names any variable that appeared in more than one
table; only the last value survives, so resolve it deliberately.

---

## `parse_coordinate(value, minutes=None, hemisphere=None)`

Signed decimal degrees from every form NCEI submissions use, or `None`.

```python
parse_coordinate(62.16)             #   62.16
parse_coordinate("170 30.01 W")     # -170.500167
parse_coordinate("64-05.8454N")     #   64.097423
parse_coordinate("62 09 36 N")      #   62.16       degrees, minutes, seconds
parse_coordinate(62, 9.6, "N")      #   62.16       three separate columns
```

A hemisphere letter beats a leading minus sign, because station lists often carry both.

**Longitude sign is the classic silent error.** A western longitude written as
`170 30.01 W` becomes −170.5; the same row copied into a data file as `170.5` is in the
Pacific's other half. When a dataset has both a station list and in-file coordinates,
check that they agree before trusting either.

---

## `parse_timestamp(value, time_value=None)`

A datetime from the usual spreadsheet forms, including single-digit months (`2023/6/1`)
and a separate time column (`parse_timestamp("20230624", "031903")`).

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

## `write_staged_cast(path, columns, rows, *, latitude, longitude, date, time=None, metadata=None, notes=None)`

Writes the header block Phase 1 reads, then the CSV table.

```python
write_staged_cast(
    out / "C145-001.csv",
    columns=["Depth (m)", "T090C", "Sal00"], rows=levels,
    latitude=62.16, longitude=-170.500167, date="2023-06-20", time="14:55",
    metadata={"Accession": "0287423", "Platform": "Oshoro maru",
              "Station": "a01", "Bottom Depth": 47},
    notes=["Depth computed from PrDM by UNESCO 1983 at the cast latitude"])
```

`metadata` keys become `# Key: value` lines, which `generate_field_mapping` mines for
Table 4a secondary headers — Accession, Platform, Station, Cast, Bottom Depth and
Instrument are all recognised.

**Two rules for the staged file:**

1. **Column names must carry their units** — `Temp (deg C)`, `DO (mg/L)`, `Depth (m)`.
   Unit detection reads the full raw column name and nothing else. A units row underneath
   the header is invisible to it, so fold those units into the names.

2. **Every derived value goes in `notes`.** A depth computed from pressure, a position
   read out of a readme PDF, a depth of 0 applied by convention, a cast identified by
   matching two files — the staged file is the only place a later reader can learn the
   value was not in the source. Then repeat it in what you report.

---

## Two worked patterns

Almost every accession that needs staging is one of these two shapes.

### A. Positions in a sibling station list

The data files hold only the measurement table; a station list keyed by file name or station
carries the coordinates. Common for CTD, XCTD and XBT submissions.

```python
cols, rows, info = read_table(SRC / "CTD_readme.csv")        # often cp932
stations = {}
for r in rows:
    lat, lon = parse_coordinate(r[3], r[4], r[5]), parse_coordinate(r[6], r[7], r[8])
    stamp = parse_timestamp(r[10])
    if lat is None or lon is None or stamp is None:
        continue                                              # header or footer row
    stations[r[2].strip()] = {"station": r[1], "lat": lat, "lon": lon, "time": stamp}

for cast_file in sorted(SRC.glob("*.asc")):
    meta = stations.get(cast_file.stem)
    if meta is None:
        print(f"!! {cast_file.name}: no station-list entry -- not staged")   # never silent
        continue
    cols, levels = read_instrument_export(cast_file)
    write_staged_cast(OUT / f"{cast_file.stem}.csv", cols, levels,
                      latitude=meta["lat"], longitude=meta["lon"],
                      date=f"{meta['time']:%Y-%m-%d}", time=f"{meta['time']:%H:%M}",
                      metadata={"Accession": ACC, "Platform": SHIP,
                                "Station": meta["station"], "Cast": cast_file.stem},
                      notes=[f"source: {cast_file.name} + station list"])
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
