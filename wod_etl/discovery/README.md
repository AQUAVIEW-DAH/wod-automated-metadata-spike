# Phase 1 — Discovery

**Find files, work out what's inside them, and get the data into memory.**

Everything here operates on raw bytes and text. Nothing here knows about WOD yet — that
starts in [Phase 2](../mapping/README.md).

**This phase assumes the file carries its own coordinates and parses as a table.** When it
does not — positions in a sibling station list or a readme PDF, one table holding the whole
cruise, a title row above the header, a cp932 encoding, `**NS`/`*DL` sentinels — go through
[Phase 0 — Staging](../staging/README.md) first and point discovery at the staged copy.

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
sniff_format            what IS this file? (bytes, not extension)
        ↓
read_any                → [{cast_id, df, header_block}]   for every format
        ↓
classify_dataset        which WOD dataset is this cast?   → Phase 2
```

`read_any` is the dispatcher. Underneath it, delimited text still goes through
`profile_raw_data` → `split_casts` → `load_tabular`, which remain callable on their own and
are documented below; NetCDF, Sea-Bird `.cnv`, fixed-width ASCII and spreadsheets take their
own readers. Everything comes back in the same shape.

`fetch_and_peek_file` can run on a *remote URL* before downloading, which is the point of
it: decide whether a 4 GB file is worth fetching by reading 8 KB of it.

**Reading a file that does not just open** is a procedure, not an API call:
[`.claude/skills/wod-etl/reading-unknown-files.md`](../../.claude/skills/wod-etl/reading-unknown-files.md)
walks the six questions to answer before staging anything.

---

## `list_remote_directory(url_or_path, recursive=False, filter_extension=None)`

Crawls an open HTTP directory index (Apache/NGINX, as NCEI serves) or a local folder.
Auto-detects which by checking whether the path exists on disk.

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

**What it handles for you**

- Sizes written as `1.0M` / `14K` are converted to bytes; `-` becomes `null`.
- Dates in `12-Apr-2023 14:25` or `2023-04-12 14:25` form become ISO strings.
- Links that leave the starting directory are refused. NCEI writes its parent link as an
  absolute path (`/data/oceans/archive/arc0222`), not `../`, so a naive filter would list
  the parent as a child and `recursive=True` would crawl upward into the whole archive.
  `skipped_navigation_links` counts what was rejected.
- Recursion is depth-capped at 8 and tracks visited URLs, so a symlink loop cannot hang it.

**Failure** — network problems come back as `{"error": "...", "total_items": 0, "items": []}`.
Never raises. Every request goes through `wod_etl.http_util.get_with_retries`: 429 and 5xx
responses and timeouts are retried `HTTP_RETRIES` times with backoff, a 404 is not.

**A citation is resolved to its data directory before anything is listed.** An NCEI
metadata landing page, an `/archive/accession/<n>` URL and a bare accession number hold no
files. `resolve_accession_url(text)` reads the landing page -- whose file listing renders
client-side, but whose static HTML still carries the FTP link
`ftp://ftp-oceans.ncei.noaa.gov/nodc/archive/arc0238/0303535/` -- and returns the HTTPS
directory under `/data/oceans/archive/`. The listing records it under `resolution`. When the
landing page cannot be read, the result is an `error` plus a `hint` naming the page to open.
`accession_number(text)` pulls the number out of any of those forms.

**A versioned accession is narrowed to its latest version.** Newer accessions hold `1.1/`,
`2.2/`... each a full copy of the bundle; a recursive crawl of the root processed every file
once per version. With `recursive=True` only the highest version is listed, and `version`
says which and what else exists. Point at a version's URL to take another.

**A local archive is extracted.** A `.tar`, `.tar.gz`, `.tgz` or `.zip` is unpacked once under
`data/raw/<name>/` and that folder is listed (`extracted_from`). A member that would land
outside the target -- an absolute path, `..`, a link -- is refused and listed in
`refused_members`.

**Gotcha** — `filter_extension` only filters *files*. Directories are always returned so
recursion can work, so a `.csv` filter still shows folders.

---

## `download_file(url_or_path, dest_dir=None, overwrite=False, max_bytes=2GB, subdir=None)`

Streams a complete file to `data/raw/` (or `dest_dir`). Use `subdir=run_id` to keep runs
separate.

```json
{"status": "downloaded", "source": "https://...", "local_path": "data/raw/<run>/cast_01.csv",
 "size_bytes": 48213, "sha256": "9f2c...", "content_type": "text/csv", "attempts": 1}
```

`status` is one of `downloaded`, `cached` (already present — pass `overwrite=True` to
refetch), `copied` (source was a local path), or `error`.

Writes to `<name>.part` and renames on completion, so an interrupted run never leaves a
truncated file that looks finished. Retries 3 times with backoff. Refuses files above
`max_bytes`, checking both the declared `content-length` and the actual stream.

**Gotcha** — filenames are sanitised to `[A-Za-z0-9._-+]` and truncated to 200 chars, so
`local_path` may not exactly match the remote name. Use the returned path, don't rebuild it.

---

## `sniff_format(path, num_bytes=65536)`

**What the file is, from its bytes.** Extensions lie: `.dat` is CSV as often as it is
fixed-width, NetCDF arrives with no extension, and `.txt` is sometimes a Sea-Bird header.

```json
{"format_family": "netcdf4_hdf5", "reader": "netcdf", "confidence": "high",
 "readable": true, "is_document": false,
 "evidence": ["magic bytes b'\\x89HDF\\r\\n\\x1a\\n'"], "size_bytes": 48213}
```

`reader` is the strategy `read_any` will dispatch to: `netcdf`, `spreadsheet`, `delimited`,
`fixed_width`, `sea_bird_cnv`, or `none`.

| `format_family` | How it is recognised |
| --- | --- |
| `netcdf_classic`, `netcdf_64bit_offset`, `netcdf_64bit_data`, `netcdf4_hdf5` | magic bytes |
| `spreadsheet_xlsx`, `opendocument`, `archive_zip` | ZIP magic, then the member list |
| `pdf`, `gzip`, `bzip2`, `xz`, `sqlite`, `access_db` | magic bytes |
| `sea_bird_cnv` | `*END*` / `# name 0 =` markers |
| `json`, `xml` | parses as JSON, or opens with a tag |
| `delimited_text` | one delimiter splits ≥60% of rows into the same field count |
| `fixed_width_text` | column boundaries hold across every sampled row |
| `ragged_text` | numeric rows that agree on nothing |
| `unstructured_text` | rows exist but <50% of their fields are numeric: prose |
| `binary_unknown` | no configured encoding decodes it |

**Delimiters are counted outside quotes.** A station list whose last column is a quoted
remark (`"CTD, trawl, NORPAC"`) has a different raw comma count on every row; counted as csv
does, it is a table. A `LAT: 21.0` line above a staged table is not a data row, so a staged
cast of three levels still sniffs as delimited.

**Two flags, and they are not the same.** `readable` means there is a table to read.
`is_document` means the file is documentation or a container *by nature* — PDF, archive,
XML metadata. A caller should **skip** an `is_document` file and **fail** on anything else
that will not read: a silent skip is how a real cast in an unrecognised layout disappears
from a run.

**Gotchas**

- `fixed_width_text` `colspecs` are **inferred** from where whitespace held in the sample.
  A column that is full in the first rows and empty later shifts every field after it.
  Verify against the accession's format description.
- `delimited_text` with `confidence: medium` usually means a units row under the header, or
  a trailing comment. That is information — look at it.
- `unstructured_text` is still worth reading during triage. Coordinates and sentinel legends
  live in readmes.

---

## `read_any(path, sniff=None, profile=None, max_casts=None)`

**One call that reads any supported format into the same shape.**

```python
casts, info = read_any("data/raw/0287423/2-CTD/cast_01")
# casts: [{"cast_id": "0", "df": DataFrame, "header_block": "...", "source_note": "..."}]
```

`header_block` is `Key: value` text in the form `generate_field_mapping` already parses, so
a position held in a NetCDF attribute or a `.cnv` `* NMEA Latitude =` line reaches the
mapping contract by the same route as a `# Latitude:` comment.

**Per format**

| Reader | What it does | Watch for |
| --- | --- | --- |
| `delimited` | `profile_raw_data` → `split_casts` → `load_tabular`, unchanged | one cast per header block |
| `netcdf` | one cast per profile; handles 1-D, `N_PROF × N_LEVELS`, and CF contiguous ragged (`row_size` + `sample_dimension`) | units are folded into the column name |
| `sea_bird_cnv` | names from `# name N =`, data after `*END*` | `=`-style header lines are rewritten to `Key: value` |
| `fixed_width` | `read_fwf` with sniffed `colspecs` | headerless files get `col_0…col_N` and a warning — nothing can map those |
| `spreadsheet` | every non-empty sheet becomes one cast | needs an Excel engine; a workbook has no header block |

**Failure** — `([], {"error": ...})`. Never raises. Always read `info["warnings"]`: a
transposed NetCDF variable, a `.cnv` whose name count does not match its column count, and
inferred fixed-width boundaries are all reported there and nowhere else.

**NetCDF specifics.** Global attributes and per-profile scalars both go into
`header_block`, under their original name *and* — for the ones it recognises — again as
`Latitude:` / `Longitude:` / `Date:` so the mapper can parse them. Nothing is lost and the
provenance stays visible. Files over 20 million cells are refused rather than loaded.

---

## `classify_dataset(path=None, columns=None, header_text="", depth_increment_m=None, ...)`

**Which WOD dataset this is — or whether it is WOD profile data at all.** This is what
replaced `dataset_type` as a required argument.

```json
{"verdict": "profile", "dataset_type": "CTD", "confidence": "high", "score": 9.0,
 "evidence": ["path matches 'CTD': names CTD", "columns match 't090c': Sea-Bird export"],
 "candidates": [...], "is_wod_profile_data": true, "exclusion": null,
 "unstorable_columns": [{"column": "Conductivity (S/m)", "variable": "conductivity", ...}],
 "possible_exclusions": []}
```

`is_wod_profile_data` has **three** values: `True` needs evidence for a profile (a named
instrument or a depth axis), `False` is an exclusion, and `None` means nothing decided it --
such a file used to default to `True`, so a plankton matrix and NCEI's bounding-box file
passed triage as profile data. `long_format` is set for a `parm,value` table, with the
`pivot_long` call to make.

`verdict` is `profile`, `excluded` or `unidentified`. **They are three different outcomes:**

- `profile` — process it as `dataset_type`.
- `excluded` — it cannot become a profile. `exclusion.route` says where it belongs instead,
  and `exclusion.belongs_in_wod` distinguishes plankton (belongs in WOD, other path) from
  currents and eDNA (not in WOD at all).
- `unidentified` — profile-shaped, but nothing names an instrument. **Do not guess**; read
  the folder readme and pass the type explicitly.

**Evidence, weighted.** Path and header text count fully, column names at 0.8, extra context
at 0.6. The raw text, a separator-flattened copy and a CamelCase-split copy are searched,
because `\bglider\b` does not match `glider_slocum` (`_` is a word character) and `\btrawl\b`
cannot match inside `TrawlInfo`.

**Exclusions are scored, not tried in order.** Each category -- plankton, fish catch,
currents, eDNA, bathymetry, contaminants -- sums weighted signals from the path and the
columns, and the strongest wins; `alternatives` lists the rest. A word both biological
categories use (`species`, `taxa`, `genus`, `family`) counts for both, so a file whose only
evidence is a `Species` column comes back `biological sample: plankton or fish catch
(ambiguous)` with `belongs_in_wod: None` -- first-match-wins had labelled trawl catch as
plankton. A file of station, latitude and longitude columns with nothing measured is a
**sidecar** (`exclusion.kind == "sidecar"`): it locates casts; join it with
`read_station_list`. A haul staged by `wod_etl.staging.write_staged_haul` is recognised by its marker line.

**File-level versus column-level.** Currents, plankton, eDNA, bathymetry, catch and
contaminants exclude the **file**. Conductivity, sigma-t, sound velocity, nitrite alone,
ammonium, CDOM, turbidity and potential temperature are **columns WOD does not store**: they
appear in `unstorable_columns`, are dropped, and never exclude the file. A CTD cast that also
logs conductivity is still a CTD cast, and excluding it would throw away its temperature too.

**Prose is not evidence about content.** A readme listing an accession's folders mentions
plankton and ADCP; matches found only in header or context text land in
`possible_exclusions` for you to check, never in `exclusion`.

---

## `classify_accession(root, max_files=None, peek_bytes=8192)` and `classify_file(path, rel=None)`

**Triage the whole accession before processing any of it.** Reads each file's head, so it is
cheap enough to run first. **The per-file verdict is the answer**: NCEI archives a
submission as one `data/0-data` folder holding casts, station lists, plankton sheets and
reports together, so a folder verdict describes a mixture.

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
| `sidecar` | a station list -- join it into the casts it locates |
| `exclude` | not a profile; `exclusion` and `belongs_in_wod` say why and where |
| `document` | documentation, metadata, or an NCEI bundle file |
| `inspect` | the evidence did not decide |

**NCEI's own bundle files are recognised by name** -- `NCEI-Readme.txt`, `journal.txt`,
`<accession>.md5`, `*-ISO-19115-2.xml`, `*_preview.jpg`, the confirmation e-mails and
`<accession>_lonlat.txt`, which is two numeric columns that sniff as a table. `ncei_bundle`
says what each is for. `process_accession` skips them before downloading.

`classify_file` is the same triage for one local file; the dry run uses it. Per file the
result also carries the dataset type, `empty_columns` and `measurement_density`.

**`measurement_density` ignores station, depth, time and flag columns on purpose.** A
nutrient file whose station and depth are full and whose three nutrient columns are entirely
`Not analyzed` scores `0.0`, which is the answer; an overall density would score `0.4` and
look like data.

### `cross_file_shapes` — one cast split across many files

A folder's files are usually one cast each. Two shapes break that, and neither is visible in
any single file, because the evidence is the *relationship* between them:

| `shape` | What it is | What to do |
| --- | --- | --- |
| `one file per depth level` | identical columns, one or two rows each | `combine_levels`, sort by depth, stage one file |
| `one file per variable` | disjoint measurement columns sharing a depth or time column | `combine_on_key(key=...)` — and check `info["per_table"]` |
| `long format` / `long format, one file per variable` | the variable named in a cell (`parm,value`); `variables_sampled` lists what each file holds | `pivot_long` with every file together, then `group_rows` per cast |

Processed as they stand, the first produces a pile of single-level profiles and the second a
set of casts each missing most of its variables. Both look like successful runs.

The check is a comparison of the heads already read, so it costs nothing extra. It is a
flag, not a verdict — **confirm against the folder readme before combining**, and for the
join, a near-zero key overlap means the files do not actually share a key.

**Limitation** — it reads the first few KB. A folder that looks empty may have data further
in. Count real values over the whole file before excluding anything, and read every folder's
readme before acting on a verdict. `inspect` means the evidence did not decide.

---

## `fetch_and_peek_file(file_url_or_path, num_bytes=8192)`

Reads only the first N bytes, via an HTTP `Range` request when remote. This is how you
identify an extension-less ASCII file without downloading it.

```json
{"source": "...", "peek_size_bytes": 8192, "detected_encoding": "utf-8",
 "preview_text": "# CRUISE: STOCS85A\n...", "line_count_in_peek": 42}
```

Decoding falls back UTF-8 → Latin-1 → lossy ASCII, so `preview_text` is always a string.

**Gotcha** — a server that ignores `Range` returns the whole file. `peek_size_bytes` tells
you what actually arrived; check it before assuming you read only a prefix.

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

A line counts as header if it starts with `#`, `%`, `*`, `//`, or matches `Key: value` — so
`LAT: 61.930 LON: -172.270` is captured as metadata, not data. That header block is what
Phase 2 mines for coordinates.

**`format_family` decides what happens next:**

| Value | Meaning |
| --- | --- |
| `tabular_ascii` | Normal path. Proceed. |
| `custom_ascii_or_fixed_width` | Pandas could not parse it. Inspect `raw_first_data_line` and pass an explicit `delimiter` to `load_tabular`. |
| `unstructured_text_or_metadata` | No data rows — documentation, not measurements. Skip the file. |

**Gotchas**

- `detected_delimiter` is a `repr`, so a comma appears as `"','"`. `load_tabular` unwraps
  this for you; if you use it yourself, strip the quotes.
- `inferred_dtypes` keys keep pandas' leading spaces (`" Temp (deg F)"`) while `columns` is
  stripped. Do not join the two by key. Nothing downstream uses `inferred_dtypes`.
- `detected_scientific_variables` is a rough hint only. Phase 2's `match_variable` is the
  authority on what a column is.

---

## `split_casts(file_path_or_text, profile_result=None, min_data_rows=1)`

Splits a file that concatenates several casts. Safe to call unconditionally — a normal file
returns one cast.

```json
{"cast_count": 2, "multi_cast": true,
 "casts": [{"index": 0, "header_block": "# CAST: 1\nLAT: 62.100 LON: -172.500",
            "column_header_line": "Depth, Temp_C",
            "data_text": "Depth, Temp_C\n0.0, 8.70\n10.0, 8.40",
            "data_row_count": 2, "start_line": 0, "end_line": 5}]}
```

**The signal** is a header/comment line appearing *after* data rows have started. Each cast
carries its own `header_block`, which is what gives it its own position and time.

`data_text` includes the column header row, so it parses directly as CSV.
`data_row_count` excludes it.

**Limitation** — heuristic. Files that separate casts some other way (a blank line, a
leading record count, fixed-width record boundaries) will not split. If a file should have
many casts and returns one, look at it by hand before trusting the output.

A table with a `Station` column and no repeated header block is the common case this cannot
handle: there are no per-cast headers to detect, so it returns one cast covering the whole
cruise. Split it with [`group_rows`](../staging/README.md) during staging instead.

Use `cast_as_profile_input(cast)` to rebuild one cast as text for `profile_raw_data`.

---

## `load_tabular(file_path, profile_result=None, delimiter=None, header_lines=None, ...)`

Reads the **complete** file into a DataFrame. This is the bridge into Phase 3 — `profile_raw_data`
only ever saw 8 KB.

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

**What it handles**

- Recounts the header block against the *full* file rather than trusting the truncated
  preview. Override with `header_lines=` when you know better.
- Maps the *unambiguous* missing-value sentinels to NaN: `-999`, `-9999`, `-99999`,
  `-999.9`, `-1e34`, `NA`, `null`, `-`, and friends. Pass `na_values=` to replace the list.
  `-9`, `-99`, `9999` and `99999` are deliberately **not** in the list: pandas expands them
  to their float forms, so a real air temperature of −9.0, a longitude of −99.0 or a
  sounding of 9999 m silently became NaN. A sentinel left in the data shows up as an absurd
  value range in `audit_dataset`; a real value turned to NaN shows up as nothing at all.
- Drops whole-line `#` and `//` comments that sit *below* the header row, and says how many
  in `warnings`. `*` and `%` are left alone — they begin real sentinel cells like `**NS`.
- Drops all-empty rows and trailing unnamed columns left by lines ending in the delimiter.
- Warns when the full file's columns differ from what the preview showed.

**Failure** — returns `(None, {"error": ...})`. Never raises.

**Check `all_na_columns`** before mapping: a column that is entirely missing will map to a
WOD code and then write a variable full of fill values.

**`load_tabular` knows numeric sentinels only** (`-999`, `NA`, `null` and friends). It does
not know an originator's asterisk legend — `**NS`, `*DL`, `***NA`, `**ND` — which is defined
per submission, usually in the last rows of the sheet or in the readme. Those cells survive
as strings, become NaN at `convert_units_and_types`, and are counted in its warnings; to
handle and *report* them properly, clean them during staging with
[`clean_sentinels`](../staging/README.md). Below-detection markers matter most: they are real
measurements WOD has no flag for, so the loss should be quantified, not absorbed.

---

## Next

[Phase 2 — Mapping](../mapping/README.md) turns `profile_result` plus the loaded columns
into the mapping contract.
