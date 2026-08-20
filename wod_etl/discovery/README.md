# Phase 1 — Discovery

**Find files, work out what's inside them, and get the data into memory.**

Everything here operates on raw bytes and text. Nothing here knows about WOD yet — that
starts in [Phase 2](../mapping/README.md).

**This phase assumes the file carries its own coordinates and parses as a table.** When it
does not — positions in a sibling station list or a readme PDF, one table holding the whole
cruise, a title row above the header, a cp932 encoding, `**NS`/`*DL` sentinels — go through
[Phase 0 — Staging](../staging/README.md) first and point discovery at the staged copy.

```python
from wod_etl.discovery import (list_remote_directory, fetch_and_peek_file, profile_raw_data,
                              download_file, split_casts, load_tabular)
```

## Order of operations

```
list_remote_directory   what files exist?
        ↓
download_file           fetch one to data/raw/
        ↓
fetch_and_peek_file     read the first 8 KB
        ↓
profile_raw_data        header block? delimiter? column names?
        ↓
split_casts             one cast or many?
        ↓
load_tabular            full file → DataFrame          → Phase 2
```

`fetch_and_peek_file` can run on a *remote URL* before downloading, which is the point of
it: decide whether a 4 GB file is worth fetching by reading 8 KB of it.

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
Never raises. NCEI returns 503 and read timeouts fairly often; retry before concluding the
URL is wrong.

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
