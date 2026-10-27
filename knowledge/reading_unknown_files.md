# Reading a file nobody documented

Submitters send NetCDF, Sea-Bird `.cnv`, old fixed-column ASCII, Excel workbooks, CSV named
`.dat`, and files with no extension. This procedure is for a file that does not open: one
listed under "Check", or one reported as "does not read as any table".

Answer the questions below for each folder before staging; the answers are the provenance of
every derived value. The Python calls run from `uv run python`.

## 0. Before any file: what is in this accession?

`wod-etl run` has already answered this in `decisions.toml`. From Python:

```python
from wod_etl.discovery import classify_accession
report = classify_accession("data/raw/0287423")
report["process"], report["exclude"], report["inspect"]
```

Every folder gets `process`, `exclude` or `inspect`, with the evidence. `inspect` means the
evidence did not decide; that is where the questions below apply. Read the folder's readme
before opening its data.

## 1. What is this file?

```python
from wod_etl.discovery import sniff_format
fmt = sniff_format("data/raw/0287423/2-CTD/cast_01")
fmt["format_family"], fmt["reader"], fmt["confidence"], fmt["evidence"]
```

Detection uses content, not extension: `.dat` is CSV as often as fixed-width, NetCDF arrives
with no extension, and `.txt` is sometimes a Sea-Bird header.

| It says | It means | Next |
| --- | --- | --- |
| `netcdf*`, `sea_bird_cnv`, `spreadsheet*` | A known container | `read_any` handles it; go to Q3 |
| `delimited_text` with `confidence: high` | An ordinary table | `read_any`; go to Q2 |
| `delimited_text` with `confidence: medium` | Rows disagree on field count | Usually a units row or a trailing comment. Look at the raw lines |
| `fixed_width_text` | Boundaries were inferred, not declared | Q2, and verify the boundaries |
| `ragged_text` | Numeric, but no consistent shape | Several blocks concatenated, or wrapped records. Open it |
| `unstructured_text` | Prose | Not data. Read it for coordinates and sentinel legends, then exclude it |
| `binary_unknown` | Nothing recognises it | See *When the file still will not open* |
| `archive_zip`, `gzip` | A container | Extract into `data/raw/`, list the members, start again |

Then read it:

```python
from wod_etl.discovery import read_any
casts, info = read_any(path, fmt)     # [{"cast_id", "df", "header_block", "source_note"}]
info["warnings"]                       # never skip these
```

Every format returns the same shape. `header_block` is `Key: value` text that the mapper
parses, so a coordinate from a NetCDF attribute reaches the contract by the same route as a
`# Latitude:` comment.

### Warnings to act on

- Fixed-width column boundaries are inferred from where whitespace held across the sampled
  rows. A column that is full in the sample and empty later shifts every field after it. Check
  the boundaries against the accession's format description.
- A headerless fixed-width file gets `col_0…col_N`; name the columns during staging (Q2).
- NetCDF units are folded into the column name (`TEMP (degree_Celsius)`); do not strip them.

## 2. Where does the table start, and what are the columns?

Ask, in this order:

1. Is the first non-comment line a header or the first level? A headerless file read with a
   header loses its shallowest level and labels every column with that level's values. For
   fixed-width files `read_any` checks whether that line is all numeric; for delimited files
   it trusts the profile. Compare `df.head()` with the raw first lines.
2. Is there a units row under the header? `Station,Pressure,Temperature` over
   `,dbar,ITS-90 deg C` is two header rows. Fold them with `fold_unit_row` when staging:
   `Pressure (dbar)`, not `Pressure`.
3. Is there a title row above the header? Common in exported spreadsheets:
   `read_table(header_row=1)`.
4. Are the columns named at all? `col_0 … col_N` cannot be mapped. Name them in the staged
   copy, from the header block or the accession's format description:

   ```python
   from wod_etl.staging import rename_columns, suggest_column_names
   suggest_column_names(cols, units=units_row)   # what each column resolves to
   cols, audit = rename_columns(cols, {"col_0": "Depth (m)", "col_1": "Temp (deg C)"})
   ```

   `rename_columns` refuses any rename that would change the column's WOD code, unit or scale,
   and prints the fix.
5. Did the whole file come back as one column? `read_table` is comma-only by default, so a
   tab- or semicolon-separated file collapses into one column and later shows up as "no
   measurements in this file", not as a delimiter problem. Pass
   `read_table(path, delimiter=None)` to sniff the delimiter.
6. Do comment lines appear below the header? They parse as rows and become phantom levels
   whose depth is the comment text.

## 3. Where are the coordinates and the time?

A profile needs latitude, longitude and depth; without any one of them the cast cannot be
written. A missing time is a warning, not a blocker, but flag a cast with no date.

Look in this order, and record which source you used:

| Where | How it looks | What to do |
| --- | --- | --- |
| Columns in the data | `Lat`, `LATITUDE`, `lat_deg` | Nothing; the mapper finds them |
| The file's own header block | `# Latitude: 34.250`, `* NMEA Latitude = 34 15.00 N` | Nothing; `read_any` normalises both |
| NetCDF attributes or per-profile variables | `LATITUDE(N_PROF)`, `geospatial_lat_min` | Nothing; `read_any` puts them in the header block |
| A sibling station list | `stations.csv` keyed by station number | Join it in during Phase 0 staging |
| Only in a readme or cruise report | Prose, or a PDF | Phase 0 staging. A position stated in a readme is a source: use it and cite it |
| Nowhere | | The cast cannot be staged; a position is never invented |

Coordinates that are not decimal degrees (`49-46.9858N`) are handled by `parse_coordinate` in
`wod_etl.staging`; see [`accession_field_guide.md`](accession_field_guide.md).

Time traps: a `Date time (UTC)` column beside a `Time zone` column is ambiguous until tested.
`08 30` is 08:30, not two fields. A "Julian day" is day-of-year, not the Julian calendar.
Cross-check one timestamp against an independent source in the accession (an underway GPS
track, a station list, a cruise log) before trusting the column.

## 4. What is each column, in WOD terms?

```python
from wod_etl.mapping import query_wod_spec, generate_field_mapping
spec = query_wod_spec("CTD", list(df.columns), depth_increment_m=dz)
contract = generate_field_mapping({"columns": list(df.columns),
                                   "raw_header_block": header}, spec)
contract["unmapped_columns"], contract["warnings"]
```

Each name in `unmapped_columns` is one of three things:

1. A WOD variable under an instrument's name: `T090C` is Temperature. If the name will recur,
   add an alias to `VARIABLE_ALIASES` in `wod_etl/mapping/query_wod_spec.py`; for one file,
   rename the staged copy with `rename_columns`. Never edit `data/raw/`.
2. A variable with no Table 3a code (conductivity, sigma-t, nitrite alone, potential
   temperature…); `classify_dataset` lists these in `unstorable_columns`, and they are not
   written. Mapping it onto an adjacent code is worse: aliasing potential temperature to code
   `1` makes a cast claim Temperature twice.
3. Not a measurement: a station id, a flag column, a comment, an originator's QC code. Leave
   it unmapped, and consider carrying a flag column as secondary header 96 (Originator's
   Flags).

Table 3a, reached through [`README.md`](README.md), decides what WOD stores.

## 5. What are the units?

A wrong unit still validates, so this is the most expensive question to get wrong.

- Unit detection reads only the raw column name. A unit in a separate row, a readme, or a
  NetCDF `units` attribute that `read_any` did not fold in is invisible. Put it in the name
  during staging.
- Check the conversion direction against Table 3a. Alkalinity and DIC are stored per volume;
  nutrients are stored per mass. "Already µmol/kg" is correct for one and a 1000× error for
  the other.
- Depth versus pressure: depth is the axis and has no variable code; pressure is code 25.
  Pressure in dbar becomes depth automatically when the cast has a latitude; otherwise
  stage it with `pressure_to_depth` in `wod_etl.staging` (dbar in, metres out).
- Read the value ranges of one output file before trusting any of this. `audit_dataset` in
  `wod_etl.verify` does it per dataset.

## 6. Is there any data in it?

A file of `Not analyzed` parses cleanly and writes a valid, empty profile.

```python
report["folders"]["4-nutrients"]["files"][0]["empty_columns"]
report["folders"]["4-nutrients"]["files"][0]["measurement_density"]
```

`measurement_density` ignores station, depth and time columns, so a nutrient file whose
nutrient columns are all `Not analyzed` scores `0.0`. `process_accession` skips these with a
stated reason.

The sample is only the first few KB. Count real values over the whole file before excluding
anything.

Sentinels such as `**NS`, `***NA`, `*DL` and `**ND` are counted by `clean_sentinels` (legend in
[`accession_field_guide.md`](accession_field_guide.md)). Below-detection is a real measurement,
but WOD has no flag for it, so it becomes missing.

## When the file still will not open

- `binary_unknown`: read the folder's readme and any format description in the accession, and
  find out what wrote it. Do not guess a layout: a wrong record length still produces numbers
  that look like data.
- A format the accession documents but nothing here reads: write a staging script in
  `scripts/` (gitignored) and name it in `decisions.toml`, with the files it reads:

  ```toml
  [profiles.old_ascii]
  kind = "script"
  script = "scripts/0287423_old_ascii.py"
  files = ["3-old-ascii/drop_001.dat"]
  dataset_type = "MBT"
  ```

  `wod-etl confirm` runs it with `WOD_DATA_ROOT` and `WOD_STAGING_OUT` set. The script writes
  staged CSVs with `wod_etl.staging.write_staged_cast`.
- A layout seen twice needs a reader, not a script: add it to
  `wod_etl/discovery/read_any.py` and a signature to `sniff_format.py`.

## The record to keep

Keep one row per folder in your notes: `report.md` records what was written, this table
records why.

| Folder | Format | Dataset | Coordinates from | Units from | Columns dropped | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `1-CTD` | Sea-Bird `.cnv` | CTD | file header (`NMEA Latitude`) | column names | `Conductivity` (no Table 3a code) | 48 casts written |
| `3-bottle` | CSV + sidecar | OSD | `stations.csv`, joined on station | units row, folded in during staging | none | 12 casts written |
| `5-plankton` | CSV matrix | OSD (Taxa-Records) | station list | readme: number per sample | none | 24 hauls; volume filtered not stated |

Every derived or assumed value (a depth converted from pressure, a depth of 0 applied by
convention, a cast identified by matching two files) goes in the staged header and in the
report. Each is a judgement, not a measurement.
