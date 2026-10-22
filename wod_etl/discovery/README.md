# Phase 1 — Discovery

Find files, work out what is in them, and load the data into memory.

Everything here works on raw bytes and text; WOD terms start in
[Phase 2](../mapping/README.md).

This phase assumes a file carries its own coordinates and parses as a table. When it does not
(positions in a station list or readme PDF, one table for the whole cruise, a title row above
the header, a cp932 encoding, `**NS`/`*DL` sentinels), stage it first with
[Phase 0 — Staging](../staging/README.md) and point discovery at the staged copy.

```python
from wod_etl.discovery import (list_remote_directory, resolve_accession_url,
                              classify_accession, classify_file, classify_dataset,
                              fetch_and_peek_file, sniff_format, profile_raw_data,
                              download_file, read_any, split_casts, load_tabular)
```

## Order of operations

```
classify_accession      what is in this accession, and which folders are WOD profile data?
        ↓
list_remote_directory   what files exist?
        ↓
download_file           fetch one to data/raw/
        ↓
sniff_format            what is this file? (bytes, not extension)
        ↓
read_any                → [{cast_id, df, header_block}]   for every format
        ↓
classify_dataset        which WOD dataset is this cast?   → Phase 2
```

`read_any` is the dispatcher. Delimited text goes through `profile_raw_data` →
`split_casts` → `load_tabular` (each callable on its own, documented below); NetCDF, Sea-Bird
`.cnv`, fixed-width ASCII and spreadsheets have their own readers. All return the same shape.

`fetch_and_peek_file` works on a remote URL, so you can inspect 8 KB of a large file before
downloading it.

For a file that does not open cleanly, follow
[`knowledge/reading_unknown_files.md`](../../knowledge/reading_unknown_files.md).

---

## `list_remote_directory(url_or_path, recursive=False, filter_extension=None)`

Lists an HTTP directory index (Apache/NGINX, as NCEI serves) or a local folder, detected by
whether the path exists on disk.

```python
res = list_remote_directory(
    "https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/",
    recursive=True, filter_extension=".csv")
```

```json
{"source": "...", "total_items": 7, "skipped_navigation_links": 3,
 "items": [{"name": "surface_data.csv", "path": "https://.../surface_data.csv",
            "is_dir": false, "size_bytes": 1048576,
            "last_modified": "2023-04-12T14:25:00"}]}
```

- Sizes like `1.0M` / `14K` become bytes; `-` becomes `null`.
- Dates like `12-Apr-2023 14:25` or `2023-04-12 14:25` become ISO strings.
- Links that leave the starting directory (including NCEI's absolute parent link) are
  refused and counted in `skipped_navigation_links`.
- Recursion is capped at depth 8 and tracks visited URLs, so symlink loops cannot hang it.
- Network errors return `{"error": "...", "total_items": 0, "items": []}`; it never raises.
  Requests go through `wod_etl.http_util.get_with_retries`: 429, 5xx and timeouts are
  retried `HTTP_RETRIES` times with backoff; 404 is not.

**Input resolution.** A landing page, an `/archive/accession/<n>` URL or a bare accession
number holds no files. `resolve_accession_url(text)` reads the landing page's FTP link
(e.g. `ftp://ftp-oceans.ncei.noaa.gov/nodc/archive/arc0238/0303535/`) and returns the HTTPS
directory under `/data/oceans/archive/`, recorded under `resolution`. If the page cannot be
read, the result has an `error` and a `hint`. `accession_number(text)` extracts the number
from any of these forms.

**Versioned accessions.** When an accession holds `1.1/`, `2.2/`… (each a full copy), a
recursive listing covers only the highest version; `version` says which and what else
exists. Point at a version's URL to use another.

**Local archives.** A `.tar`, `.tar.gz`, `.tgz` or `.zip` is extracted once under
`data/raw/<name>/` and that folder is listed (`extracted_from`). Members that would land
outside the target (absolute paths, `..`, links) are refused and listed in
`refused_members`.

**Gotcha:** `filter_extension` filters files only. Directories are always returned so
recursion works.

---

## `download_file(url_or_path, dest_dir=None, overwrite=False, max_bytes=2GB, subdir=None)`

Streams a file to `data/raw/` (or `dest_dir`). Use `subdir=run_id` to keep runs separate.

```json
{"status": "downloaded", "source": "https://...", "local_path": "data/raw/<run>/cast_01.csv",
 "size_bytes": 48213, "sha256": "9f2c...", "content_type": "text/csv", "attempts": 1}
```

`status` is `downloaded`, `cached` (already present; `overwrite=True` refetches), `copied`
(local source) or `error`.

- Writes to `<name>.part` and renames when complete, so an interrupted download never looks
  finished.
- Retries 3 times with backoff.
- Refuses files over `max_bytes`, checking both `content-length` and the actual stream.

**Gotcha:** filenames are sanitised to `[A-Za-z0-9._-+]` and truncated to 200 characters.
Use the returned `local_path`; do not rebuild it.

---

## `sniff_format(path, num_bytes=65536)`

Identifies a file from its bytes, not its extension (`.dat` may be CSV, NetCDF may have no
extension, `.txt` may be a Sea-Bird header).

```json
{"format_family": "netcdf4_hdf5", "reader": "netcdf", "confidence": "high",
 "readable": true, "is_document": false,
 "evidence": ["magic bytes b'\\x89HDF\\r\\n\\x1a\\n'"], "size_bytes": 48213}
```

`reader` is what `read_any` dispatches to: `netcdf`, `spreadsheet`, `delimited`,
`fixed_width`, `sea_bird_cnv`, `wod_csv`, `probe_dat`, `tsk_xctd`, or `none`.

| `format_family` | How it is recognised |
| --- | --- |
| `netcdf_classic`, `netcdf_64bit_offset`, `netcdf_64bit_data`, `netcdf4_hdf5` | magic bytes |
| `spreadsheet_xlsx`, `word_document`, `presentation`, `opendocument`, `archive_zip` | ZIP magic, then the member list |
| `pdf`, `gzip`, `bzip2`, `xz`, `sqlite`, `access_db` | magic bytes |
| `image` (JPEG, PNG, GIF, TIFF) | magic bytes; a document, never a table |
| `wod_csv` | WOD's own CSV export: `CAST`, `VARIABLES ,` and `UNITS` rows |
| `tsk_xctd` | a Tsurumi-Seiki header record: serial, date, time, `DD-MM.MMN`, probe `CT1`/`CT4` |
| `probe_dat` | a first line `# XCTD|XBT cruise yyyymmdd hhmm lat lon n` |
| `sea_bird_cnv` | `*END*` / `# name 0 =` markers |
| `json`, `xml` | parses as JSON, or opens with a tag |
| `delimited_text` | one delimiter splits ≥60% of rows into the same field count |
| `fixed_width_text` | column boundaries hold across every sampled row |
| `ragged_text` | numeric rows that agree on nothing |
| `unstructured_text` | rows exist but <50% of their fields are numeric: prose |
| `binary_unknown` | no configured encoding decodes it |

Delimiters are counted outside quotes, so a quoted remark column (`"CTD, trawl, NORPAC"`)
does not break detection. `Key: value` lines above a table are not counted as data rows.

**`readable` vs `is_document`.** `readable` means there is a table to read. `is_document`
means the file is documentation or a container (PDF, archive, XML metadata). Skip an
`is_document` file; treat any other unreadable file as a failure, not a skip.

**Gotchas**

- `fixed_width_text` `colspecs` are inferred from the sample. A column that is empty later
  in the file shifts every field after it; check against the format description.
- `delimited_text` with `confidence: medium` usually means a units row under the header or a
  trailing comment.
- `unstructured_text` files (readmes) often hold coordinates and sentinel legends; read them
  during triage.

---

## `read_any(path, sniff=None, profile=None, max_casts=None)`

Reads any supported format into the same shape.

```python
casts, info = read_any("data/raw/0287423/2-CTD/cast_01")
# casts: [{"cast_id": "0", "df": DataFrame, "header_block": "...", "source_note": "..."}]
```

`header_block` is `Key: value` text that `generate_field_mapping` parses, so a position from
a NetCDF attribute or a `.cnv` `* NMEA Latitude =` line reaches mapping the same way as a
`# Latitude:` comment.

| Reader | What it does | Watch for |
| --- | --- | --- |
| `delimited` | `profile_raw_data` → `split_casts` → `load_tabular` | one cast per header block; a cruise-wide table splits by station |
| `netcdf` | one cast per profile; handles 1-D, `N_PROF × N_LEVELS`, CF contiguous ragged (`row_size` + `sample_dimension`), and flattened ERDDAP-style tables (one `row` dimension, profile variables in `cdm_profile_variables`; upcasts dropped when downcasts exist) | units are folded into the column name; an opaque name (a BODC code, `TEMPS901`) is replaced by its `long_name`; a flag named in `ancillary_variables` becomes `<variable>_QC` |
| `sea_bird_cnv` | names from `# name N =`, or the row of names after `*END*` (ASCII export); data after it | `=`-style header lines become `Key: value`; date from `NMEA UTC`, `System UTC`, `start_time` or upload time (UTC only for the first two) |
| `fixed_width` | `read_fwf` with sniffed `colspecs` | headerless files get `col_0…col_N` and a warning; these cannot be mapped |
| `spreadsheet` | one cast per non-empty sheet; an RBR Ruskin workbook gives one cast per downcast | needs an Excel engine; upcasts are not written |
| `wod_csv`, `probe_dat`, `tsk_xctd` | `known_formats.py` | WOD's own flags (F) are dropped, the originator's (O) kept |

On failure it returns `([], {"error": ...})` and never raises. Always read
`info["warnings"]`: transposed NetCDF variables, `.cnv` name/column count mismatches and
inferred fixed-width boundaries are reported only there.

**NetCDF.** Global attributes and per-profile scalars go into `header_block` under their
original names, and recognised ones are repeated as `Latitude:` / `Longitude:` / `Date:` for
the mapper. Files over 100 million cells are refused.

**Table layouts.** `profile_raw_data` finds the header row below a title, `Key,value` rows or
a blank-line-separated block, and folds a units row into the names (`Temperature (degC)`). A
table holding many stations (station, date, latitude and longitude columns) is split into one
cast per station; without a depth axis it splits only on a sample column, so an underway
track stays one cast. Japanese column names are matched through `wod_etl/terms.py`.

---

## `classify_dataset(path=None, columns=None, header_text="", depth_increment_m=None, ...)`

Decides which WOD dataset a file is, or whether it is WOD profile data at all.

```json
{"verdict": "profile", "dataset_type": "CTD", "confidence": "high", "score": 9.0,
 "evidence": ["path matches 'CTD': names CTD", "columns match 't090c': Sea-Bird export"],
 "candidates": [...], "is_wod_profile_data": true, "exclusion": null,
 "unstorable_columns": [{"column": "Conductivity (S/m)", "variable": "conductivity", ...}],
 "possible_exclusions": []}
```

`verdict` is one of:

- `profile`: process it as `dataset_type`.
- `excluded`: it cannot become a profile. `exclusion.route` says where it belongs;
  `exclusion.belongs_in_wod` separates plankton (in WOD, another path) from currents and
  eDNA (not in WOD).
- `unidentified`: profile-shaped, but nothing names an instrument. Do not guess; read the
  folder readme and pass the type explicitly.

`is_wod_profile_data` is `True` (evidence of a profile: a named instrument or a depth axis),
`False` (excluded) or `None` (undecided). `long_format` is set for a `parm,value` table, with
the `pivot_long` call to make.

**Evidence weighting.** Path and header text count fully, column names 0.8, extra context
0.6. Matching runs on the raw text, a separator-flattened copy and a CamelCase-split copy, so
`glider_slocum` and `TrawlInfo` match `glider` and `trawl`.

**Exclusions are scored.** Each category (plankton, fish catch, currents, eDNA, bathymetry,
contaminants) sums weighted signals from path and columns; the strongest wins and
`alternatives` lists the rest. Words shared by both biological categories (`species`, `taxa`,
`genus`, `family`) count for both, so a file whose only evidence is a `Species` column comes
back as `biological sample: plankton or fish catch (ambiguous)` with `belongs_in_wod: None`.
A file with only station, latitude and longitude columns is a sidecar
(`exclusion.kind == "sidecar"`); join it with `read_station_list`. A haul staged by
`wod_etl.staging.write_staged_haul` is recognised by its marker line.

**File vs column exclusions.** Currents, plankton, eDNA, bathymetry, catch and contaminants
exclude the file. Conductivity, sigma-t, sound velocity, nitrite alone, ammonium, CDOM,
turbidity and potential temperature are columns WOD does not store: they are listed in
`unstorable_columns`, dropped, and never exclude the file.

**Prose is not content.** Matches found only in header or context text (e.g. a readme
listing other folders) go to `possible_exclusions` for review, never to `exclusion`.

---

## `classify_accession(root, max_files=None, peek_bytes=8192)` and `classify_file(path, rel=None)`

Triages the whole accession before processing. It reads each file's head, so it is cheap to
run first. The per-file verdict is what counts: one NCEI folder often mixes casts, station
lists, plankton sheets and reports.

```json
{"file_count": 23, "folder_count": 3,
 "files": [{"path": "data/0-data/SHU1505_StationInfo.csv", "action": "sidecar", ...},
           {"path": "data/0-data/U2015-015_CTD_Temp.csv", "action": "stage",
            "long_format": {"variable_column": "parm", ...}, ...}],
 "process_files": [], "stage_files": [...], "sidecar_files": [...], "exclude_files": [...],
 "document_files": [...], "inspect_files": [],
 "folders": {"data/0-data": {"verdict": {"action": "stage", "files_by_action": {...}}, ...}}}
```

| `action` | means |
| --- | --- |
| `process` | profile data with a dataset type |
| `stage` | profile data that must be reshaped first (long format) |
| `sidecar` | a station list; join it into the casts it locates |
| `exclude` | not a profile; `exclusion` and `belongs_in_wod` say why and where |
| `document` | documentation, metadata, or an NCEI bundle file |
| `inspect` | the evidence did not decide |

Each file also carries the fields shown in `decisions.toml`: `data` (what it holds),
`include`, `dataset` (the WOD dataset it would be written as, Table 2; XCTD is written as
CTD), `why` (the evidence, citing Table 3a where relevant) and, when one variable is a copy
of another, `drop`. `data` lists only variables the proposed dataset stores (Table 3a: MBT
stores temperature only); `why` says what is not written.

**NCEI bundle files** are recognised by name: `NCEI-Readme.txt`, `journal.txt`,
`<accession>.md5`, `*-ISO-19115-2.xml`, `*_preview.jpg`, `<accession>_map.jpg`, confirmation
e-mails, the Send2NCEI report `<ID>-report.txt`, and `<accession>_lonlat.txt`. `ncei_bundle`
says what each is for; `process_accession` skips them before downloading.

**Documentation** is recognised by content (prose at both head and tail, so a data file with
a long header is not misread) or by name (`readme`, `*description*`, `notes`).

**NetCDF files are opened**, not peeked. Global attributes are read as header text
(`MOORING`, `INST_TYPE`, `platform`) and variables as columns (long name and unit).

**Moorings.** A file whose every variable is one value per time step, at a fixed position,
over a day or more, is one instrument's record from a mooring: `stage`, dataset `MRB`, with
a `time_series` summary (platform, instrument, samples, days) and a `mooring` summary
(variables MRB stores, serial number, pressure record and usual submerged pressure, stated
depth, position). A moored buoy's profile is the concurrent readings of all its instruments
at one time (manual p.17), kept as daily means (p.14), so a mooring's instruments are triaged
together after every file is read:

- grouped into moorings;
- given the deployment their pressure records show (with none, the record minus its first
  and last day);
- each placed at a depth from its pressure, the depth its file or file name states, or the
  mooring's description (`staging/mooring_layout.py`).

The `mooring` summary records `group`, `deployed`, `depth`, `placed_by` and `depth_note`. An
instrument nothing places is proposed left out, with the reason; `wod-etl` stages the rest
into MRB profiles (see *What wod-etl works out* in the
[orchestration README](../orchestration/README.md)). A record that cannot be a level
(nothing MRB stores, or several depths in one file) is excluded as a `moored instrument
time series`.

**Long-format files** get the `station_list` beside them that names their casts (of several,
the one naming most); with none, they are proposed left out.

**Underway records.** The same time-series shape on a moving platform with no depth axis
(keel CTD, thermosalinograph) is excluded as `underway record along a track` (SUR in WOD;
not split out yet). A moving record with a depth or pressure variable (a glider) is
classified normally.

**Other rules**

- The instrument a NetCDF file names (global `instrument`, `INST_TYPE`, `instrumentType`,
  RBR's `model`, or an `instrument` variable's `make_model`) counts like the path.
  `instrumentType: adcp` with velocity variables is a current record; an ADCP's scalar record
  without velocity stays a moored temperature record.
- An SV&T probe (sound velocity and temperature, no conductivity) is a digital BT: MBT.
  Plain `SVP` is not a drifter.
- The depth increment decides CTD by WOD's rule (< 2 m, manual p.19).
- A measured variable equal to another at every level (a salinity filled with temperature)
  is proposed as `drop`.
- Seabed sediment (grain size, d50) is excluded: WOD holds the water column.

`classify_file` is the same triage for one local file; the dry run uses it. Its result also
carries the dataset type, `empty_columns` and `measurement_density`.

`measurement_density` ignores station, depth, time and flag columns, so a nutrient file whose
nutrient columns are all `Not analyzed` scores `0.0`.

### `cross_file_shapes` — one cast split across many files

Two shapes are visible only by comparing files in a folder:

| `shape` | What it is | What to do |
| --- | --- | --- |
| `one file per depth level` | identical columns, one or two rows each | `combine_levels`, sort by depth, stage one file |
| `one file per variable` | disjoint measurement columns sharing a depth or time column | `combine_on_key(key=...)` and check `info["per_table"]` |
| `long format` / `long format, one file per variable` | the variable named in a cell (`parm,value`); `variables_sampled` lists what each file holds | `pivot_long` with every file together, then `group_rows` per cast |

Processed as they stand, the first gives many single-level profiles and the second casts
missing most of their variables, and both look like successful runs.

This is a flag, not a verdict. Confirm against the folder readme before combining; a
near-zero key overlap means the files do not share a key.

**Limitation:** triage reads only the first few KB. Count real values over the whole file
before excluding it, and read each folder's readme before acting on a verdict.

---

## `fetch_and_peek_file(file_url_or_path, num_bytes=8192)`

Reads the first N bytes, using an HTTP `Range` request when remote.

```json
{"source": "...", "peek_size_bytes": 8192, "detected_encoding": "utf-8",
 "preview_text": "# CRUISE: STOCS85A\n...", "line_count_in_peek": 42}
```

Decoding falls back UTF-8 → Latin-1 → lossy ASCII, so `preview_text` is always a string.

**Gotcha:** a server that ignores `Range` returns the whole file. Check `peek_size_bytes`.

---

## `profile_raw_data(preview_text, file_url_or_path="")`

Works out the file's structure from the peeked text.

```json
{"format_family": "tabular_ascii", "detected_delimiter": "','",
 "header_comment_lines": 3,
 "raw_header_block": "# CRUISE: STOCS85A\n# DATE: 1985-06-12\nLAT: 61.930 LON: -172.270",
 "columns": ["Depth (ft)", "Temp (deg F)", "Salinity (PSU)"],
 "inferred_dtypes": {...},
 "detected_scientific_variables": ["temperature", "depth", "salinity"]}
```

A line is header if it starts with `#`, `%`, `*`, `//`, or matches `Key: value`, so
`LAT: 61.930 LON: -172.270` is captured as metadata. Phase 2 reads coordinates from this
header block.

| `format_family` | Meaning |
| --- | --- |
| `tabular_ascii` | Normal path. Proceed. |
| `custom_ascii_or_fixed_width` | Pandas could not parse it. Inspect `raw_first_data_line` and pass an explicit `delimiter` to `load_tabular`. |
| `unstructured_text_or_metadata` | No data rows; documentation. Skip the file. |

**Gotchas**

- `detected_delimiter` is a `repr` (`"','"`). `load_tabular` unwraps it; strip the quotes if
  you use it yourself.
- `inferred_dtypes` keys keep pandas' leading spaces (`" Temp (deg F)"`) while `columns` is
  stripped. Do not join them by key. Nothing downstream uses `inferred_dtypes`.
- `detected_scientific_variables` is a hint. Phase 2's `match_variable` decides what a
  column is.

---

## `split_casts(file_path_or_text, profile_result=None, min_data_rows=1)`

Splits a file that concatenates several casts. Safe to call on any file; a normal file
returns one cast.

```json
{"cast_count": 2, "multi_cast": true,
 "casts": [{"index": 0, "header_block": "# CAST: 1\nLAT: 62.100 LON: -172.500",
            "column_header_line": "Depth, Temp_C",
            "data_text": "Depth, Temp_C\n0.0, 8.70\n10.0, 8.40",
            "data_row_count": 2, "start_line": 0, "end_line": 5}]}
```

A new cast starts when a header/comment line appears after data rows. Each cast keeps its
own `header_block` (its position and time). `data_text` includes the column header row and
parses directly as CSV; `data_row_count` excludes it.

**Limitation:** heuristic. Casts separated another way (blank line, record count,
fixed-width record boundaries) will not split. A table with a `Station` column and no
repeated header returns one cast for the whole cruise; split it with
[`group_rows`](../staging/README.md) during staging.

`cast_as_profile_input(cast)` rebuilds one cast as text for `profile_raw_data`.

---

## `load_tabular(file_path, profile_result=None, delimiter=None, header_lines=None, ...)`

Reads the complete file into a DataFrame (`profile_raw_data` only saw 8 KB).

```python
df, info = load_tabular(local_path, profile)
if df is None:
    ...  # info["error"] says why
```

```json
{"delimiter": ",", "header_lines_skipped": 3, "encoding": "utf-8", "rows": 2,
 "columns": ["Depth (ft)", "Temp (deg F)", "Salinity (PSU)"],
 "dtypes": {...}, "all_na_columns": [], "warnings": []}
```

- Recounts the header block against the full file. Override with `header_lines=`.
- Maps unambiguous missing-value sentinels to NaN: `-999`, `-9999`, `-99999`, `-999.9`,
  `-1e34`, `NA`, `null`, `-` and similar. Pass `na_values=` to replace the list. `-9`, `-99`,
  `9999` and `99999` are not in the list because they can be real values.
- Drops whole-line `#` and `//` comments below the header row and reports the count in
  `warnings`. `*` and `%` lines are kept, since they start sentinel cells like `**NS`.
- Drops all-empty rows and trailing unnamed columns from lines ending in the delimiter.
- Warns when the full file's columns differ from the preview's.
- On failure returns `(None, {"error": ...})`; never raises.

**Check `all_na_columns`** before mapping: an all-missing column maps to a WOD code and
writes a variable full of fill values.

**Originator sentinels** (`**NS`, `*DL`, `***NA`, `**ND`) are not known here; they are
defined per submission. They survive as strings, become NaN in `convert_units_and_types`,
and are counted in its warnings. To handle and report them, clean them during staging with
[`clean_sentinels`](../staging/README.md). Below-detection markers are real measurements WOD
has no flag for, so report the count.

---

## Next

[Phase 2 — Mapping](../mapping/README.md) turns `profile_result` and the loaded columns
into the mapping contract.
