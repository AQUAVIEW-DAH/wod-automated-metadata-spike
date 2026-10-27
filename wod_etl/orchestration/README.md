# Orchestration

Runs all four phases over a whole accession and records what was done, what failed and why,
and how to resume.

```python
from wod_etl.orchestration import process_accession
```

---

## Run it yourself: `wod-etl`

Every accession runs in two steps with a person's review between them, in a browser
(`wod-etl web`) or a terminal.

```bash
uv run wod-etl run <input>         # 1. triage every file; write decisions.toml for review
#    ... review data/runs/<acc>/decisions.toml: include or not, as which dataset, why ...
uv run wod-etl confirm <acc>       # 2. write what the reviewed decisions include
uv run wod-etl status <acc>        # where it stopped, what is open, where the files are
```

- `<input>` is a landing-page URL, an accession number, an NCEI data-directory URL, a local
  folder or a `.tar.gz`/`.zip`.
- `run` never processes anything; `confirm` processes exactly what the file includes.
- An input with no accession number (a folder called `survey_2024`) needs `--accession-id`.
- Either command takes `--from <stage> --yes` to redo one of its own stages.
- `python -m wod_etl` is the same command. From Python: `pipeline.run_pipeline(source)` and
  `pipeline.confirm_pipeline(accession)`.

### Stages

| # | Stage | Command | Does | Stops when |
| --- | --- | --- | --- | --- |
| 1 | resolve | run | turns the input into a listing: the accession's ISO record in NCEI's metadata catalogue (then its landing page) names the archive directory; picks the latest version of a versioned accession; extracts a tarball | NCEI does not answer (run again later), or the input is not an accession |
| 2 | fetch | run | mirrors a remote accession into `data/raw/<acc>/`, re-fetching files whose listed date changed; a local folder is used in place | a download fails (run again; finished files are kept) |
| 3 | triage | run | `classify_accession` → `triage.json`: per file, what it holds, include or not, the WOD dataset, why | |
| 4 | decide | run | writes an entry for every undecided file and recipes for files that need reshaping | never: review is the next step |
| 5 | check | confirm | validates the reviewed decisions and builds the plan | a decision is open, or the files changed since `run` (exit 2) |
| 6 | stage | confirm | runs the reshaping recipes into `data/staging/<acc>/pipeline/profiles/` | a recipe cannot run |
| 7 | process | confirm | `process_accession` on every included file, each as the dataset its entry names and validated on its own; then `assemble_profiles` writes WOD files, one per dataset and year, into `data/processed/<acc>/` | a transient failure (exit 1; confirm again to retry). A file that will never read is reported and the rest are written (exit 1) |
| 8 | verify | confirm | `verify_output` over the WOD files written; copy and depth-axis checks judge each source file's casts separately | -- (a high finding gives exit 3) |
| 9 | report | both | `report.md` + `report.json`, always, however the command ended | |

Exit codes: `0` done (run: decisions written; confirm: written and verified, or nothing to
write) · `1` a stage failed, or a file could not be written · `2` decisions needed ·
`3` written, but verification flagged the output · `130` interrupted.

### Resume: what running a command again redoes

Each stage records in `state.json` a fingerprint of its inputs (the input string, the
listing, the files it reads by size and nanosecond mtime, the part of `decisions.toml` it
reads, and the code it runs) and of the files it wrote. Running the command again:

- skips a stage that is done, with an unchanged fingerprint and untouched output;
- reruns a stage whose fingerprint changed, or whose output was deleted or edited by hand,
  clearing only what it owns;
- resumes a failed stage with unchanged inputs: the process stage retries only the failed
  files (its manifests are `<acc>_direct` and `<acc>_staged`);
- after Ctrl-C (exit 130), resets the interrupted stage and continues it.

`confirm` writes only what was reviewed. If the files changed after `run`, or the triage code
proposes something different for a file (action, include, dataset or drop), it refuses and
asks for `run` again, which adds entries for new files and never changes the person's. A code
change that triages the files the same way goes ahead.

A `.lock` prevents two runs of one accession at once; a lock left by a dead process on the
same machine is taken over.

### `decisions.toml`

The person decides one thing per file: include it or not. wod-etl works out the rest (how a
file becomes casts, where a mooring's loggers are, which station list locates a table's
casts) and explains it in the entry's `why`; what it cannot work out is proposed left out,
with the `why` saying what is missing.

`run` writes the file and then only appends to it, so nothing typed into it is rewritten.
Each entry decides one file, or a group of alike files under a `*` pattern (a `*` stays within
one folder); a file's own entry overrides a pattern that also matches it.

```toml
[files."data/0-data/LeConte_*_CTD_*.nc"]       # 4 files: ...
include = true
dataset = "CTD"
data = "CTD profiles: temperature, salinity"
why = "CTD by its path ('CTD') and levels 1.00 m apart (< 2.0 m); ..."

# 17 files: RBRsolo_SN100020.nc, ...
# Depths, as wod-etl places them:
#   RBRsolo_SN100028.nc    37.7 m  45 m up the line from point 1 (82.7 m by SBE37SM_SN10552.nc)
#   RBRsolo_SN100320.nc    86.4 m  listed with SBE37SM_SN10551.nc, no position of its own
[files."data/0-data/downstream_deep_mooring/RBRsolo_*.nc"]
include = true
dataset = "MRB"
data = "moored instrument record: RBRsolo at one place, 13.6 days: temperature"
why = "a mooring's instrument: ...; at the depth mooring_description.txt gives it -- ..."
```

| Field / section | Holds | Blocks `confirm` until |
| --- | --- | --- |
| `[files."<path or pattern>"]` | `include` (true/false), `dataset` (Table 2 code), `data` (what it holds), `why`; optional `drop = [...]` (variables not to write) and `metadata = { ... }` (header values for its casts). Grouped under CHECK, IN, OUT (by reason) and DOCUMENTATION headings after a summary of counts. A mooring's instrument depths are comments above its entry | every file has an entry; `include` is true or false; `dataset`, `drop` and `metadata`, where given, are well formed |
| `[metadata]` | header values for every cast, overriding what a file states: `platform`, `institution`, `project`, `cruise`, `principal_investigator`, `probe_type`, `instrument` (older spellings such as `Platform` are accepted). Proposed from the accession's ISO 19115 records: a value stated once is set, one stated several ways is listed in a comment, the rest are commented out. A WOD code (`platform = 8553`) is safest and gets its name beside it; a name is looked up in WOD's code list and kept as text unless it matches exactly one entry | -- |
| `[profiles.<name>]` | only `kind = "script"`: a custom staging script for a layout nothing else reads (below). Other kinds are ignored, with a note | it names a script |

Every included file is written as the dataset its entry names (a CTD cast with coarse levels
still goes to OSD, manual p.19); one with no `dataset` is classified when processed. An
included file that cannot be staged (a logger with no depth) is not written, and the report
says so. Plankton is written as OSD Taxa-Records (below).

### What wod-etl works out

`decisions.derive_recipes` builds recipes from what triage found:

| Shape | How it becomes casts |
| --- | --- |
| Long-format tables (`StNo, parm, depth, value`) | One recipe per folder: pivoted into one cast per station, located by the station list beside them (of several, the one naming the most casts; with none, the files are proposed left out). A unit stated in a name is read; a name with none is read in WOD's standard unit. A cast the list does not name is left out; two values at one level keep the first; the list's track is checked (inside NCEI's `_lonlat.txt` box, no outlying hop, time running forward). Each issue is reported and written into the casts' notes |
| Casts with no position of their own, and a station list in their folder or one above | Located by the list: `per_file` when it names each cast's file (a `File name` column: `C145-001` for `C145-001.asc`) or station; `by_station` when one table of many casts has a station column matching the list. Each cast gets the list's position, date and time. A cast the list does not name stays out, with the reason |
| Surface tracks logged on one clock (a thermosalinograph and a weather station) | `tracks`: one SUR cast per stream, the files merged on time (`staging/tracks.py`). A longer stream (a DO logger running all cruise) joins each cast where its stamps fall; the rest of it is a cast of its own |
| Tables of the same casts (bottle chemistry in one, chlorophyll of the same bottles in another) | `combine`: when stations, dates and positions (within 0.05°) coincide, one cast per station and date, levels merged by depth (`staging/combine.py`). The cast's notes record which file each column came from |
| Plankton tables (one row per taxon and depth range) | One recipe per file (`staging/taxa_layout.py`): one haul per station and date, located by the station list beside it or its own position columns; one Taxa-Record per taxon, depth range and measurement, written to `wod_osd_<year>_<acc>_taxa.nc`. A file with no location, depth range, taxon or usable unit is proposed out with the reason. See [`knowledge/plankton_biomass.md`](../../knowledge/plankton_biomass.md) |
| A mooring's instruments (one NetCDF per instrument) | One recipe per mooring, grouped by the mooring id the files name, else by folder and position, and split where records stop overlapping in time. One staged cast per day: each instrument's mean over the UTC day at its depth, within the deployment (manual p.17, daily means p.14). The deployment comes from the instruments' pressure records; with none, it is the record minus its first and last day. Each instrument's depth comes from its pressure (day by day), else its file's stated depth or file name (`T_45m.nc`), else the mooring's description (`staging/mooring_layout.py`). An instrument none of these places is proposed left out. Each depth and how it was found appear in decisions.toml comments, report.md and the casts' notes. See [`staging/moorings.py`](../staging/moorings.py) |

| `[profiles]` kind | Fields |
| --- | --- |
| `script` | `script`: a Python file that writes staged casts into `$WOD_STAGING_OUT` (`$WOD_ACCESSION`, `$WOD_DATA_ROOT` and `$WOD_PROJECT_ROOT` are set); optional `files` it consumes and `dataset_type`. Its hash is fingerprinted. Its output and the logs of its `wod_etl` calls go into `run.log`. |

### When it fails

Every problem states what failed, why, and how to fix it; it is printed at the end of the run,
written to `report.md` and logged. `remedies.py` adds the decisions key to set, or the command
to run, to the library's own explanation (a listing's `hint`, a skip reason, a finding's
`action`). An exception in a stage is logged with its traceback and reported as a bug, not a
data problem.

### The accession run log

`data/runs/<acc>/run.log` (text) and `events.jsonl` (the same events as JSON) record every
run against the accession: each file's triage verdict, each decision applied, each file and
cast written or not, and why. `process_accession`, `process_taxa`, `classify_accession` and
the staged writers log here too when they can tell the accession (`accession=`, or from the
path). Each run opens with a banner (invocation id, actor, arguments, git commit,
configuration) and closes with how it ended. To log a hand-written staging script:

```python
from wod_etl.runlog import accession_log
with accession_log("0303928"):
    ...
```

### Files

```
data/runs/<acc>/        decisions.toml   the decisions, for review (written by run)
                        report.md        the run summary (+ report.json), every command
                        run.log          the detailed log (+ events.jsonl, one JSON event
                                         per line): every stage, file, cast and decision
                        state.json  triage.json  listing.json
                        work/            raw copies per run, and per_file/: each source
                                         file's own result, assembled into the output
data/raw/<acc>/         the mirror of a remote accession
data/staging/<acc>/pipeline/profiles/      staged by recipes (cleared on rerun)
data/processed/<acc>/   the output: WOD-standard CF NetCDF, wod_<dataset>_<year>_<acc>.nc
```

**Output layout.** WOD distributes one file per dataset and year (`wod_ctd_2008.nc` holds every
CTD cast of 2008; manual pp.44 and 46). The output uses the same split with the accession
appended: `wod_ctd_2008_0062621.nc`. A file can hold casts from several source files: shared
values are file attributes, differing ones (`source_file`, notes, a column's name or unit, a
header) per-cast variables. Every cast carries its `dataset`.

In `data/processed/<acc>/` the pipeline writes and removes only files with names of that
form. Folders staged by hand under `data/staging/<acc>/` are left alone.

### In a browser: `wod-etl web`

The same two commands, in a browser: [`wod_etl/web/README.md`](../web/README.md).

---

## `process_accession(url_or_path, dataset_type=None, ...)`

```python
result = process_accession(
    "https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/")
```

`dataset_type` is normally omitted: an accession often mixes CTD, bottle data, ADCP and
plankton, so each file is classified from its own path, header, columns and depth spacing by
[`classify_dataset`](../discovery/README.md#classify_datasetpathnone-columnsnone-header_text-depth_increment_mnone-).

| Parameter | Purpose |
| --- | --- |
| `dataset_type` | `None` (default) classifies per file. A code forces one dataset for the whole run and also overrides exclusions. Use it only when the accession is one instrument. |
| `dataset_overrides` | `{path_prefix: dataset_type}`, e.g. `{"3-XBT": "XBT"}`: the dataset for those folders; everything else is still classified. |
| `process_unidentified_as` | For files that are profile-shaped but name no instrument. `None` (default) skips them with a reason; a code processes them as that dataset. |
| `output_dir` / `raw_dir` | Override `data/processed/` and `data/raw/`. Output is one file per WOD dataset and year, `wod_<dataset>_<year>_<acc>.nc`; input that is not an accession is named for its folder (`wod_ctd_2015_my-cruise.nc`). The WOD files hold only the casts of files this call covers (every file listed and included); an existing file of the same name holding another run's sources is left alone and reported. A source removed from the folder is dropped from the files. |
| `per_file_dir` | Where each source file's own result (its validated NetCDF and casts) goes before assembly: `<per_file_dir>/<run_id>/`, default `<output_dir>/per_file/<run_id>/`. Listed in `per_file_outputs` and `per_file_casts`. |
| `assemble` | Write the WOD files at the end (default `True`). `assemble_profiles(per_file_casts, output_dir, scope, owns=None)` does it separately, e.g. for two runs together. `owns(source)` says which sources' casts it may replace (default: every file of that scope in `output_dir`); an existing file holding any other source is left alone and reported. |
| `run_id` | Names the manifest. Reuse it to resume. Defaults to the URL + timestamp. |
| `recursive` | Crawl subdirectories (default `True`). |
| `filter_extension` | Only consider files with this extension. |
| `limit` | Process at most N files, to test the setup first. A limited run is not assembled; check its `per_file_outputs`. |
| `resume` | Fold into an existing manifest (default `True`). |
| `retry_failed` | Also retry items already marked `failed`. |
| `verify` | Read the written NetCDF back and check every value against the manual's range for its variable and depth (default on). Sets `output_verified`. |
| `run_qc` | Run WOD's observed-level QC. Defaults to `RUN_WOD_QC` in [`config.py`](../config.py), which is off. Originator flag columns are kept either way, verbatim. |
| `dry_run` | Report what would be processed, and as what; write nothing. |
| `on_error` | `"skip"` (default) or `"stop"`. |
| `include` | Process only these keys (paths relative to the listed root); `include_missing` lists any not found. |
| `file_options` | `{key: {"drop": [variable, ...], "metadata": {Key: value}}}`: variables not to write and header values for a file's casts, as set in `decisions.toml`. |
| `accession` | Which accession's run log to write to (`data/runs/<acc>/`). Worked out from the source when omitted; with none, nothing is logged. |

The result also carries `listing` (how the input became a listing: `resolution`, `version`,
`extracted_from`). Each failed file's `traceback` is kept in the manifest.

### Recommended sequence

```python
from wod_etl.discovery import classify_accession
classify_accession("data/raw/0287423")        # 0. what is in here
process_accession(url, dry_run=True)          # 1. what would run, and as what
process_accession(url, limit=3)               # 2. prove the shape on 3 files
process_accession(url)                        # 3. the rest
```

- Step 0 is triage; most folders in an accession are not WOD profile data.
- Step 1 triages a local file from its head (format, header, columns) as `classify_file`
  does, and returns `would_process`, `would_skip`, `needs_staging` (long-format files) and
  `undecided`. A remote file is judged by its path only; each item's `decided_by` says which.
- Step 2 shows a wrong routing or format on 3 files rather than 400.

### What comes back

```json
{"status": "incomplete", "run_id": "0287423_20260817T101500Z",
 "total_items": 50, "counts": {"done": 42, "failed": 3, "skipped": 5},
 "failure_rate": 0.066, "complete": false,
 "outputs": ["data/processed/2-CTD/wod_ctd_2015_0287423.nc", "..."],
 "per_file_outputs": ["data/processed/2-CTD/per_file/ctd/cast_01.nc", "..."],
 "failures": [{"key": "cast_07.csv", "stage": "read_any", "error": "parse failed: ..."}],
 "dataset_types": {"CTD": 31, "OSD": 11},
 "skipped_reasons": {"not WOD profile data: current velocity": 4,
                     "no measurements: every mapped measurement column is empty (...)": 1},
 "unstorable_columns_dropped": {"Conductivity (S/m) (conductivity)": 31},
 "outputs_without_time": null,
 "outputs_without_time_of_day": ["SHU1505_st01.csv", "..."],
 "coded_headers_not_resolved": {"secondary header 3 = 'Shunyo-Maru'": "ambiguous ..."},
 "output_verified": false,
 "output_verification": {"passed": false, "finding_counts": {"high": 1, ...},
                         "findings": [{"finding": "systematically_out_of_range",
                                       "severity": "high", "variable": "temperature",
                                       "evidence": "...", "action": "..."}],
                         "value_ranges": {...}, "flag_distribution": {...}, "text": "..."},
 "manifest_path": "data/staging/manifests/0287423_20260817T101500Z.json"}
```

| `status` | Meaning |
| --- | --- |
| `complete` | Every item reached `done` or `skipped`. |
| `incomplete` | Some items are still `failed` and can be retried. |
| `aborted` | Stopped early. Read `abort_reason`. |
| `error` | Could not list the source, after retries and after resolving a landing page or accession number. Read `error` and `hint`. |
| `empty` | Listing succeeded but held no files. Check `filter_extension`. |
| `dry_run` | Nothing was written. |

`complete` says whether every item is `done` or `skipped`; `status` is also `incomplete` when a
WOD file could not be assembled.

**Run-report fields.** None of these is in `counts`:

| Field | Why it matters |
| --- | --- |
| `dataset_types` | Casts per WOD dataset. Many OSD casts in an expected-CTD run come from the depth-increment rule (manual p.19) or a mis-routed folder. |
| `skipped_reasons` | Files deliberately not processed, by reason: excluded content and empty files. |
| `unstorable_columns_dropped` | Columns not written (no WOD code, or not stored in the dataset), with the reason. Metadata and originator flag columns are consumed, not dropped, so they are not here. |
| `outputs_without_time` | Casts written with no date; validation only warns. |
| `outputs_without_time_of_day` | Casts with a date and no clock time, written date-only (`GMT_time` fill), so `time` reads 00:00. |
| `coded_headers_not_resolved` | Coded metadata kept as text because no unique code matched. Each needs a person's choice. |
| `output_verified` | Whether the values are plausible, separate from `status`. A run can be `complete` and unverified (e.g. a wrong unit). `None` means nothing was written, or `verify=False`. |

### Skipped is not failed

| Outcome | When | Counts toward abort? |
| --- | --- | --- |
| skipped | Read and found not to be WOD profile data: a documentation extension, a PDF/archive/XML container, excluded content (currents, plankton, eDNA), a station-list sidecar with no measured variable, or every measurement column empty. | No |
| failed | Something that should have worked did not: a download error, a file in no known format, no column mapping, missing latitude/longitude/depth, a write or validation error. | Yes |

This keeps an accession of mostly non-profile data from tripping the abort. A file that is
not a document and still will not read is a failure, never a skip.

### Failure policy

Per-file failures are recorded and the run continues. Two things stop it:

- `on_error="stop"`: abort on the first failure.
- Automatic abort above a 50% failure rate, once at least 10 files have been attempted. This
  usually means something systematic (wrong URL, wrong dataset type, non-tabular files).

Both thresholds are in [`config.py`](../config.py).

### Reporting

Each failure's `stage` names the function that returned the error: `download_file`,
`sniff_format`, `read_any`, `mapping`, `generate_netcdf_ragged`, `validate_intermediate_netcdf`,
or `unexpected`.

`status: "complete"` is not a quality result: every file was written and passed structural
validation, which says nothing of the values. `output_verified` judges them, and
`output_verification` holds each variable's range and the QC flag distribution.
[`audit_dataset`](../verify/README.md) also lists a dataset's mapped and dropped columns.

### One accession, several runs

Give each dataset in an accession its own `run_id` and `output_dir`
(`run_id="osd_7-CTD-bottle"`): datasets fail for different reasons and are audited separately,
and a combined run makes the 50% abort hard to read. Plan the runs from `classify_accession`'s
`process` list.

---

## `process_one_file(url, dataset_type=None, out_dir, run_id, raw_root=None, ...)`

Every phase for one file, without the manifest; never raises. Returns
`{"ok": bool, "stage": ..., "error": ...}` or `{"skip": True, "reason": ...}`.

```
download_file → sniff_format → read_any
   → per cast: classify_dataset → query_wod_spec → generate_field_mapping (+ map_metadata)
                → convert_units_and_types → apply_qc_auditing (only if run_qc)
   → write_ragged_profiles → validate_intermediate_netcdf
```

The depth increment (median positive spacing of the depth column) is measured per cast and
passed to `classify_dataset` and `query_wod_spec`, so CTD/OSD routing is decided per cast: a
folder labelled CTD can come back as OSD.

A cast is dropped, with the reason in `skipped_casts`, when it is excluded by
classification, holds no measured variable, has every measurement column empty, maps no
column to a WOD code, has pressure but no depth, or lacks latitude/longitude. If every cast
drops, the file is skipped or failed depending on the reason (see *Skipped is not failed*).

---

## `assemble_taxa` and `process_taxa` — biological data

Plankton is not a profile, so `process_accession` skips it. `wod-etl confirm` calls
`assemble_taxa(staged_dir, processed_dir, accession)`, which checks each staged haul, writes
one Taxa-Record file per year and removes stale ones.

For hauls staged by hand (for example, from a count matrix):

```python
from wod_etl.orchestration import process_taxa
result = process_taxa("data/staging/0303928/plankton", "data/processed/0303928/plankton")
```

Each staged haul goes through `staging.read_staged_haul` → `mapping.map_haul` →
`writers.write_taxa_netcdf` → `verify.validate_taxa_netcdf`, one NetCDF per haul. Nothing
raises; a failed haul is in `failures` with its stage.

These lists are not in any count:

| Field | Meaning |
| --- | --- |
| `unresolved_taxa` | name → records with no WOD variable number; they keep only the originator's name |
| `taxa_resolved_at_higher_rank` | names resolved one rank up; the species (or more) is lost |
| `missing_biological_fields` | key Table 6a field → hauls lacking it. Only the submitter has these |
| `unmapped_columns` | staged columns that are not Table 7a fields, not written |

A biomass record's Table 8 code (which carries its unit, Appendix 5.8) is not chosen
automatically: stage it with that code as the `Taxon`.

---

## The manifest (`run_manifest.py`)

One JSON file per run, at `data/staging/manifests/<run_id>.json`, written atomically after
every status change so a kill leaves a readable ledger.

### Statuses

| Status | Meaning |
| --- | --- |
| `pending` | Discovered, not yet attempted. |
| `running` | Attempt in progress. A leftover `running` entry means the run was killed. |
| `done` | Completed and validated. |
| `failed` | Attempted and failed; `stage` and `error` say where. |
| `skipped` | Deliberately not processed; `reason` says why. |

`done` and `skipped` are terminal. On resume, leftover `running` entries are reset to
`pending`.

### API

| Function | Use |
| --- | --- |
| `new_run_id(source, stamp=None)` | Filesystem-safe id from the source + timestamp. |
| `resume_or_create(run_id, source, items, config)` | Load an existing ledger and fold in newly discovered items. |
| `pending_items(manifest, retry_failed=False)` | What's left to do. |
| `mark(manifest, key, status, **fields)` | Record a transition; persists immediately. |
| `summarize(manifest)` | Counts, failure rate, outputs, failures. |
| `list_runs()` | All known runs, newest first. |
| `load_manifest(run_id)` | Read a ledger back. |

Each item keeps a timestamped `history` of every transition.

### Finding and resuming a run

```python
from wod_etl.orchestration import list_runs, load_manifest, summarize

for r in list_runs():
    print(r["run_id"], r["counts"], "complete" if r["complete"] else "INCOMPLETE")

summary = summarize(load_manifest("0287423_20260817T101500Z"))
```

Then resume with the same `run_id`:

```python
process_accession(url, dataset_type="CTD", run_id="0287423_20260817T101500Z")
```

---

## The pre-filter

Two things are decided from a file's path before download:

1. An extension in `NON_DATA_EXTENSIONS` (`.xml`, `.pdf`, `.html`, `.json`, images,
   archives, `.sh`; set in [`config.py`](../config.py)) marks documentation. NCEI bundle
   files (`NCEI-Readme.txt`, `journal.txt`, `<accession>_lonlat.txt`) are skipped by name.
2. A path that names content WOD cannot store (`4-adcp/`, `plankton/`). Only path matches
   pre-filter; matches in column names or prose wait until the file is read.

**Excluded is not empty.** An excluded folder can still hold metadata for casts: ADCP
bottom-tracking gives bottom depth and navigation; a multibeam sounding at a cast's position
is that cast's secondary header 10. `classify_dataset`'s exclusion has a `harvest` field
saying what to take, and `classify_accession` collects it per folder. Harvesting is left to
staging, citing the source, since matching files on position and time is a judgement.

Check 2 is skipped when `dataset_type` or a matching `dataset_overrides` entry is set.

---

## Phases

[Discovery](../discovery/README.md) · [Mapping](../mapping/README.md) ·
[QC transforms](../qc_transforms/README.md) · [Writers](../writers/README.md)
