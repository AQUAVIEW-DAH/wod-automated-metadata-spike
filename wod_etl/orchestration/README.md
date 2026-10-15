# Orchestration

**Run all four phases over a whole accession, unattended, and keep a ledger of what
happened.**

```python
from wod_etl.orchestration import process_accession
```

This is the layer that makes the pipeline autonomous rather than a set of parts. It exists
to answer three questions a long run always raises: *what did it actually do*, *what failed
and why*, and *how do I carry on after a crash*.

---

## Run it yourself: `wod-etl`

For a person, with no coding agent: every accession is run in **two steps**, with a review
between them.

```bash
uv run wod-etl run <input>         # 1. triage every file; write decisions.toml for review
#    ... review data/runs/<acc>/decisions.toml: include or not, as which dataset, why ...
uv run wod-etl confirm <acc>       # 2. write what the reviewed decisions include
uv run wod-etl status <acc>        # where it stopped, what is open, where the files are
```

`<input>` is a landing-page URL, an accession number, an NCEI data-directory URL, a local
folder or a `.tar.gz`/`.zip`. `run` never processes anything, and `confirm` processes
exactly what the file includes. `python -m wod_etl` is the same command; from Python,
`pipeline.run_pipeline(source)` and `pipeline.confirm_pipeline(accession)`. An input with no
accession number in it (a folder called `survey_2024`) needs `--accession-id`. Either
command takes `--from <stage> --yes` to redo a stage of its own.

### Stages

| # | Stage | Command | Does | Stops when |
| --- | --- | --- | --- | --- |
| 1 | resolve | run | turns the input into a listing: an accession's ISO record in NCEI's metadata catalogue (then its landing page) names the archive directory; the latest version of a versioned accession; a tarball extracted | NCEI does not answer (run again later), or the input is not an accession |
| 2 | fetch | run | mirrors a remote accession into `data/raw/<acc>/`, re-fetching a file whose listed date changed; a local folder is used in place | a download fails (run again -- finished files are kept) |
| 3 | triage | run | `classify_accession` → `triage.json`: per file what it holds, include or not, the WOD dataset, why | |
| 4 | decide | run | writes an entry for every file no entry decides yet, recipes for files that need reshaping, and says what must be filled in | never: reviewing is the next step |
| 5 | check | confirm | validates the reviewed decisions and builds the plan | **a decision is open, or the files changed since `run` (exit 2)** |
| 6 | stage | confirm | runs the reshaping recipes into `data/staging/<acc>/pipeline/profiles/` | a recipe cannot run |
| 7 | process | confirm | `process_accession` on every included file, each as the dataset its entry names and validated on its own; then `assemble_profiles` writes them as WOD files, one per dataset and year, into `data/processed/<acc>/` | a transient failure (exit 1; confirm again retries it). A file that will never read is reported and the rest are written (exit 1) |
| 8 | verify | confirm | `verify_output` over the WOD files written; the copy and depth-axis checks judge each source file's casts on their own | -- (a high finding gives exit 3) |
| 9 | report | both | `report.md` + `report.json`, **always**, however the command ended | |

Exit codes: `0` done (run: decisions written; confirm: written and verified, or nothing to
write) · `1` a stage failed, or a file could not be written · `2` decisions needed ·
`3` written, but verification flagged the output · `130` interrupted.

### Resume: what running a command again redoes

Each stage records a **fingerprint** of its inputs in `state.json` -- the input string, the
listing, the files it reads (size and nanosecond mtime), the part of `decisions.toml` it
reads, **and the code it runs** -- and of the files it wrote. Running the command again:

- **skips** a stage that is done, whose fingerprint is unchanged and whose output is as it
  left it;
- **reruns from scratch** a stage whose fingerprint changed, clearing only what it owns: an
  edited unit restages and reprocesses the casts, a fixed writer rewrites the output, and an
  edit to another file's entry leaves the profiles alone. Output deleted or edited by hand
  is rebuilt the same way;
- **resumes** a stage that failed with unchanged inputs: the process stage retries only the
  files that failed (its manifests are `<acc>_direct` and `<acc>_staged`, so per-file resume
  works across invocations);
- after Ctrl-C (exit 130) resets the interrupted stage and continues it.

`confirm` writes only what was reviewed: when the files changed after `run`, or the triage
code changed and now proposes something different (another action, include, dataset or
drop for some file), it refuses and asks for `run` again -- which adds entries for new files
and never touches the person's. A code change that triages the files the same goes ahead.
A `.lock` stops two runs of one accession at once; a lock left by a dead process on the
same machine is taken over.

### `decisions.toml`

Written by `run`, then **only appended to** -- nothing typed into it is ever rewritten. An
entry per file, alike files sharing one under a `*` pattern; an entry for one file overrides
a pattern that also matches it. A `*` stays within one folder.

```toml
[files."data/0-data/LeConte_*_CTD_*.nc"]       # 4 files: ...
data = "CTD profiles: temperature, salinity"
include = true
dataset = "CTD"
why = "CTD by its path ('CTD') and depth increment 1.00 m is high-resolution (< 2.0 m); ..."

[files."data/0-data/LeConte_Sept2018_CTD_ship_data.nc"]
data = "CTD profiles: temperature"
include = true
dataset = "CTD"
drop = ["salinity"]
why = "...; its salinity equals its temperature at every level -- a copy, not a measurement"

[files."data/0-data/downstream_deep_mooring/RBRsolo_*.nc"]   # 17 files
data = "moored instrument record: RBRsolo at one place, 13.6 days"
include = false
why = "a mooring's record belongs in WOD as MRB ...; wod-etl cannot build MRB profiles yet"
```

| Field / section | Holds | Blocks `confirm` until |
| --- | --- | --- |
| `[files."<path or pattern>"]` | `include` (true/false), `dataset` (Table 2 code, for an included file), `data` (what it holds), `why`; optional `drop = [...]` (variables not to write) and `metadata = { ... }` (header values for its casts). Written under a heading per outcome -- IN, OUT (WOD takes it but wod-etl cannot write it yet / WOD does not store it / left out by the review), DOCUMENTATION -- after a summary of how many files go where | every file has an entry; `include` is true or false; an included file has a WOD dataset |
| `[metadata]` | header values for every cast, **overriding what a file states**, first in the file: `platform`, `institution`, `project`, `cruise`, `principal_investigator`, `probe_type`, `instrument` (the older `Platform`, `"Principal investigator"`... are read the same). Proposed from the accession's own ISO 19115 records -- a value they state once is set, one they state several ways is listed in a comment to choose from -- and the rest offered commented out, for the reviewer to fill. A WOD code (`platform = 8553`) is safest, and its name is written beside it; a name is looked up in WOD's code list and kept as text unless it names exactly one entry. A value that differs between files goes in their own entries' `metadata` | -- |
| `[profiles.<name>]` | a recipe that reshapes included files into casts (below) | no `UNIT?` left, a station list that locates every cast |
| `[[cross_check]]` | another source of positions/dates: `name`, `file`, `tolerance_km`, `compare`; for a PDF table also `page`, `row_pattern`, `columns` | -- |
| `[track]` | `bounding_box` (proposed from NCEI's `_lonlat.txt`), `outlier_factor` | -- |
| `[conflicts."<id>"]` | a disagreement: `values`, `values_hash`, `resolution`, `note` | `resolution` is `"keep"`, `"ack"`, a source's name, or `{ date = ..., latitude = ..., longitude = ... }` |

`[profiles]`, `[track]` and `[[cross_check]]` appear only when some file needs reshaping. The
`dataset` of an included file is the contract: every file is written as its entry says
(CTD still goes to OSD cast by cast when a cast's levels are coarse, manual p.19). A conflict
whose values change after it was settled is open again (`values_hash`). Every resolution is
written into the staged notes, so it reaches the NetCDF's `comment`. Plankton is left out
for now ("not handled for now"); the taxa writer remains a library function.

**Profile recipes** (`kind`):

| kind | Fields |
| --- | --- |
| `long` | `files`, `keys` (cast key + depth key), `key_aliases`, `cast_key`, `variable`, `value`, `columns` (variable → staged name **with its unit**), `station_list`; optional `depth_column`, `time_zone`, `on_conflict = "first"`, `station_map`, `skip_casts`, `dataset_type`, `metadata`, `notes` |
| `per_file` | `files` (one cast each), `station_pattern` (regex on the file name → station), `station_list`, `header_row`, `columns` (renames); optional as above |
| `script` | `script`: a Python file that writes staged casts into `$WOD_STAGING_OUT` (`$WOD_ACCESSION`, `$WOD_DATA_ROOT` and `$WOD_PROJECT_ROOT` are set). For what no recipe describes: re-occupied stations, odd layouts. Its hash is fingerprinted. What it prints, and what its `wod_etl` calls log, goes into the run's `run.log`. |

A recipe's files must be included (`include = true`); a recipe whose files are all left out
is ignored.

### When it fails

Every problem carries **what** failed, **why**, and **how to fix it** -- printed at the end
of the run, written to `report.md`, and logged. `remedies.py` adds the decisions key to set
or the command to run to the explanations the library already gives (a listing's `hint`, a
file's skip reason, a finding's `action`). A bug (an exception in a stage) is logged with its
traceback and reported as a bug, not a data problem.

### The accession run log

`data/runs/<acc>/run.log` (text) and `events.jsonl` (the same events as JSON) record every
run against the accession -- `wod-etl run` and `confirm` (each file's triage verdict, every
decision applied, every file and cast written or not and why), **and any agent or script
calling the library**:
`process_accession`, `process_taxa`, `classify_accession` and the staged writers open the log
themselves when they can tell the accession (`accession=`, or from the path). Each run opens
with a banner (invocation id, actor, arguments, git commit, configuration) and closes with
how it ended. Wrap a hand-written staging script to log it too:

```python
from wod_etl.runlog import accession_log
with accession_log("0303928", actor="agent"):
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

**The output is WOD's own layout.** WOD distributes one file per dataset and year --
`wod_ctd_2008.nc` is every CTD cast of 2008 (www.ncei.noaa.gov/data/oceans/ncei/wod/2008/;
the manual describes the split on pp.44 and 46 but not the names). An accession's casts are
split the same way and the accession is added, so the file cannot pass for WOD's own:
`wod_ctd_2008_0062621.nc`. A file holds casts from several source files: what they share is a
file attribute, and what differs -- `source_file`, notes, a column's name or unit, a header --
is a per-cast variable. Every cast also carries its `dataset`, as WOD's files do.

The pipeline never touches anything else: in `data/processed/<acc>/` it writes and removes only
names of that form (and, once, the `pipeline/` folder the output went to before it was WOD
files), and folders an agent staged by hand under `data/staging/<acc>/` are left alone.

---

## `process_accession(url_or_path, dataset_type=None, ...)`

```python
result = process_accession(
    "https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/")
```

**`dataset_type` is optional and normally omitted.** An accession is rarely one instrument:
one submission routinely holds shipboard CTD, bottle nutrients, an ADCP section and a
plankton haul, and one label applied to all of them is wrong for most of them. Each file is
classified from its own path, header, columns and depth spacing —
[`classify_dataset`](../discovery/README.md#classify_datasetpathnone-columnsnone-header_text-depth_increment_mnone-).

| Parameter | Purpose |
| --- | --- |
| `dataset_type` | **`None` (default) = classify per file.** Pass a code only to FORCE one for the whole run — right when the accession really is one instrument, wrong the moment it is not. An explicit type also overrides an exclusion, on the reasoning that an operator who says "this folder is XBT" has looked at it and the classifier has not. |
| `dataset_overrides` | `{path_prefix: dataset_type}`, e.g. `{"3-XBT": "XBT", "5-argo": "PFL"}`. The answer to "the accession is mixed and I know what two of the folders are" — everything else is still classified. |
| `process_unidentified_as` | What to do with a file that is profile-shaped but names no instrument. `None` (default) skips it and says so; set a code to process those under a stated assumption. |
| `output_dir` / `raw_dir` | Override `data/processed/` and `data/raw/`. The output is one file per WOD dataset and year, `wod_<dataset>_<year>_<acc>.nc`; input that is no accession (no folder named as NCEI writes one, seven digits zero-padded) is named for its folder instead (`wod_ctd_2015_my-cruise.nc`), never with WOD's own bare names. The WOD files hold the casts of the files this call answers for -- every file listed and included -- and no others. A file of the same name holding another run's sources (another folder, another `include` batch) is left as it is and reported: write each set to its own `output_dir`, or process them in one call. A source that is gone from the folder (renamed, deleted) is dropped from the files. |
| `per_file_dir` | Where each source file's own result goes -- its NetCDF, validated on its own, and its casts -- before they are assembled into the WOD files: `<per_file_dir>/<run_id>/`, `<output_dir>/per_file/<run_id>/` by default. The result lists them in `per_file_outputs` and `per_file_casts`. |
| `assemble` | Write the WOD files at the end (default `True`). `assemble_profiles(per_file_casts, output_dir, scope, owns=None)` does it on its own: the accession pipeline processes two sets of files, then assembles both runs' `per_file_casts` together. `owns(source)` says which sources' casts the call may replace; an existing file of the scope holding any other source is left as it is and reported. Without `owns` the caller owns every file of that scope in `output_dir` -- right for the pipeline's own folder, wrong for a shared one. |
| `run_id` | Names the manifest. Reuse it to resume. Auto-generated from the URL + timestamp. |
| `recursive` | Crawl subdirectories (default `True`). |
| `filter_extension` | Only consider files with this extension. |
| `limit` | Process at most N files — use this to prove the shape first. A limited run is not assembled: look at its `per_file_outputs`, and an earlier full run's WOD files are left alone. |
| `resume` | Fold into an existing manifest (default `True`). |
| `retry_failed` | Also re-attempt items already marked `failed`. |
| `verify` | Read the written NetCDF back at the end and check every value against the manual's acceptable range for that variable at that depth. **On by default** -- the failure that matters most is the one that passes structural validation. Sets `output_verified`. |
| `run_qc` | Whether to run WOD's own observed-level QC. Defaults to `RUN_WOD_QC` in [`config.py`](../config.py), which is **off** — QC is applied downstream, and a Table 12 flag written here is one more thing to reconcile. The ORIGINATOR's flag columns are carried through either way, verbatim and untranslated. |
| `dry_run` | Report what would be processed, and as what; write nothing. |
| `on_error` | `"skip"` (default) or `"stop"`. |
| `include` | Process only these keys (paths relative to the listed root); `include_missing` names any not found. `wod-etl` uses it to process the files that need no staging in place. |
| `accession` | Which accession's run log to write to (`data/runs/<acc>/`). Worked out from the source when omitted; with none, nothing is logged. |

The result also carries `listing` -- how the input became a listing (`resolution`, `version`,
`extracted_from`) -- and each failed file's `traceback` is kept in the manifest.

### Recommended sequence

```python
from wod_etl.discovery import classify_accession
classify_accession("data/raw/0287423")        # 0. what is even in here
process_accession(url, dry_run=True)          # 1. what would run, and as what
process_accession(url, limit=3)               # 2. prove the shape on 3 files
process_accession(url)                        # 3. the rest
```

Step 0 is the triage step and it is not optional — most folders in a real accession are not
WOD profile data. Step 1 triages a **local** file from its head -- format, header, columns --
exactly as `classify_file` does, and returns `would_process`, `would_skip`, `needs_staging`
(long-format files, which fail in a real run) and `undecided`. A **remote** file is judged by
its path only, because nothing is downloaded yet, and each item's `decided_by` says which. Step 2 matters because a wrong routing or an unusual file format
shows up immediately on 3 files rather than after 400.

### What comes back

```json
{"status": "complete", "run_id": "0287423_20260817T101500Z",
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
| `incomplete` | Some items are still `failed` — retryable. |
| `aborted` | Stopped early. Read `abort_reason`. |
| `error` | Could not even list the source -- after retries, and after resolving a landing page or accession number to its data directory. Read `error` and `hint`. |
| `empty` | Listing succeeded but held no files. Check `filter_extension`. |
| `dry_run` | Nothing was written. |

**`complete: false` with `status: "complete"` is not a contradiction** — `status` describes
the run, `complete` describes whether every item is in a terminal state. Failed items are
neither done nor skipped, because they can be retried.

**The four run-report fields are the ones to report, and none of them is in `counts`:**

| Field | Why it matters |
| --- | --- |
| `dataset_types` | What the run actually decided, per cast. A run you expected to be all CTD coming back `{"CTD": 3, "OSD": 39}` is the depth-increment rule working (manual p.19) — or a mis-routed folder. Check which. |
| `skipped_reasons` | Every file that was read and deliberately not processed, with the reason. This is where excluded content and empty files appear. |
| `unstorable_columns_dropped` | Columns WOD has no code for, dropped by design, each with the reason. Rule 2: never silently drop data. Metadata columns and originator flag columns are **not** here — they are consumed, not dropped. |
| `outputs_without_time` | Casts written with no date. Validation only warns; you should not. |
| `outputs_without_time_of_day` | Casts with a date and no clock time. Written date-only (`GMT_time` fill) -- a reader who looks only at `time` sees 00:00. Say so, and say what zone the date is in if the source says. |
| `coded_headers_not_resolved` | Coded metadata kept as the originator's text because no unique code matched: `{"secondary header 3 = 'Shunyo-Maru'": "ambiguous in s_3_platform: 4959 ...; 10793 ..."}`. Each needs a human choice. |
| `output_verified` | **A separate verdict from `status`, and the one that matters.** `status` says the run finished; this says the numbers are possible. A run can be `complete` and unverified -- that is the classic wrong-unit failure, and it is now caught rather than shipped. `None` means nothing was written, or `verify=False`. |

### Skipped is not failed

The distinction is deliberate and load-bearing.

| Outcome | When | Counts toward abort? |
| --- | --- | --- |
| **skipped** | Read successfully and found not to be WOD profile data: a documentation extension, a `pdf`/archive/XML container, an excluded content type (currents, plankton, eDNA), a station-list sidecar with no measured variable, or every measurement column empty. | No |
| **failed** | Something that should have worked did not: a download error, a file that parses as no known format, no column mapping, a missing latitude/longitude/depth, a write or validation error. | Yes |

An accession that is mostly non-profile data would otherwise trip the 50% abort on a run
working exactly as intended. Conversely, a file that is *not* a document and still will not
read is a **failure**, never a skip — a silent skip there is how a real cast in an
unrecognised layout disappears from a run.

**A missing time does not block.** The validator treats it as a warning, and historical
casts with a date but no clock time are common, so those casts are written and listed in
`outputs_without_time`.

### Failure policy

Per-file failures are recorded and the run continues. Two things stop it:

- `on_error="stop"` — abort on the first failure.
- **Automatic abort** above a 50% failure rate, once at least 10 files have been attempted.
  A majority failing means something systematic (wrong URL, wrong dataset type, files not
  tabular), not individually bad data. Diagnose one file by hand before rerunning.

Both thresholds live in [`config.py`](../config.py).

### Reporting honestly

`failures` is not decoration. If 3 of 50 files failed, say so and name the stage. The
`stage` value is the skill that returned the error, so it points straight at the phase:
`download_file`, `sniff_format`, `read_any`, `mapping`, `generate_netcdf_ragged`,
`validate_intermediate_netcdf`, or `unexpected`.

**`status: "complete"` is not a quality result.** It says every file was written and passed
structural validation. It does not say the values are right, and this summary cannot tell
you — a column in the wrong unit, a dropped variable and a spurious QC flag all produce a
clean run. Three things the manifest never contains, which you have to gather yourself:

| Not in the summary | How to get it |
| --- | --- |
| Which columns were dropped | `unstorable_columns_dropped` covers the ones WOD cannot store; for the rest, recompute the contract per file and collect `unmapped_columns` |
| Whether the values are plausible | open a NetCDF and read each variable's min/max |
| The QC flag distribution | read the `*_qc` arrays across the output set |

[`audit_dataset`](../verify/README.md) does all three. Run it for every dataset and report
it alongside the counts -- the manifest tells you a file was written, not that it is right.
### One accession is still many runs

Per-file classification removes the need to *name* a dataset per folder. It does not remove
the reason to run them separately: give each dataset within an accession its own `run_id`
and `output_dir` (`run_id="osd_7-CTD-bottle"`,
`output_dir="data/processed/7-CTD-bottle"`). They fail for different reasons, they are
audited separately, and one run over the whole accession makes the 50%-failure abort hard to
read.

Use `classify_accession`'s `process` list to decide what those runs are.

---

## `process_one_file(url, dataset_type=None, out_dir, run_id, raw_root=None, ...)`

Every phase for a single file, without the manifest. Returns
`{"ok": bool, "stage": ..., "error": ...}` — or `{"skip": True, "reason": ...}` — and never
raises. Use it to debug one file.

The chain it runs:

```
download_file → sniff_format → read_any
   → per cast: classify_dataset → query_wod_spec → generate_field_mapping (+ map_metadata)
                → convert_units_and_types → apply_qc_auditing (only if run_qc)
   → write_ragged_profiles → validate_intermediate_netcdf
```

The depth increment is measured from the loaded data (median positive spacing of the depth
column) and feeds both `classify_dataset` and `query_wod_spec`. That is how CTD/XCTD routing
is decided per cast rather than guessed per run — and why a folder labelled CTD can
legitimately come back as OSD.

A cast is dropped, with a reason recorded in `skipped_casts`, when it is excluded by
classification, holds no measured variable, has every measurement column empty, maps no
column to a WOD code, has pressure but no depth, or is missing latitude/longitude. If *every* cast drops, the file
is skipped or failed depending on which of those it was — see *Skipped is not failed* above.

---

## `process_taxa(source, output_dir, accession=None)` — biological data

Plankton counts and biomass are not profiles, and `process_accession` skips them with a
route here. Stage each haul with `wod_etl.staging.write_staged_haul` (reshape a count matrix
with `melt_matrix` first), then:

```python
from wod_etl.orchestration import process_taxa
result = process_taxa("data/staging/0303928/plankton", "data/processed/0303928/plankton")
```

Each staged haul goes through the same phase packages a cast does --
`staging.read_staged_haul` -> `mapping.map_haul` -> `writers.write_taxa_netcdf` ->
`verify.validate_taxa_netcdf` -- one NetCDF per haul. Nothing raises: a haul that fails is in
`failures` with its stage.

**Report four lists, none of them in a count:**

| Field | Meaning |
| --- | --- |
| `unresolved_taxa` | name -> records with no WOD variable number; they keep only the originator's name |
| `taxa_resolved_at_higher_rank` | names resolved one rank up -- the species (or more) is lost |
| `missing_biological_fields` | key Table 6a field -> hauls lacking it. Only the submitter has these: ask while the accession is open |
| `unmapped_columns` | staged columns that are not Table 7a fields, not written |

**Still not done:** a biomass record's Table 8 code (which carries its unit, Appendix 5.8) is
not chosen automatically -- stage it with that code as the `Taxon`.

---

## The manifest (`run_manifest.py`)

One JSON file per run, at `data/staging/manifests/<run_id>.json`. Written **atomically**
after every status change, so a kill at any moment leaves a readable ledger.

### Statuses

| Status | Meaning |
| --- | --- |
| `pending` | Discovered, not yet attempted. |
| `running` | Attempt in progress. A leftover `running` entry means the run was killed. |
| `done` | Completed and validated. |
| `failed` | Attempted and failed; `stage` and `error` say where. |
| `skipped` | Deliberately not processed; `reason` says why. |

`done` and `skipped` are terminal. On resume, leftover `running` entries are reset to
`pending` so an interrupted item is retried rather than stranded.

### API

| Function | Use |
| --- | --- |
| `new_run_id(source, stamp=None)` | Filesystem-safe id from the source + timestamp. |
| `resume_or_create(run_id, source, items, config)` | Load an existing ledger and fold in newly discovered items. |
| `pending_items(manifest, retry_failed=False)` | What's left to do. |
| `mark(manifest, key, status, **fields)` | Record a transition; persists immediately. |
| `summarize(manifest)` | Counts, failure rate, outputs, failures. |
| `list_runs()` | All known runs, newest first — how to find one to resume. |
| `load_manifest(run_id)` | Read a ledger back. |

Each item keeps a `history` list of every transition with timestamps, so a run is auditable
after the fact.

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

Two things are decided from a file's path before it is downloaded, because both are certain
from a name alone:

1. An extension in `NON_DATA_EXTENSIONS` (`.xml`, `.pdf`, `.html`, `.json`, images,
   archives, `.sh`) — documentation, not measurements. Edit the set in
   [`config.py`](../config.py) if a real data file is being skipped. NCEI's own bundle files
   are skipped by name too: `NCEI-Readme.txt` and `journal.txt` would otherwise FAIL as
   unreadable prose, and `<accession>_lonlat.txt` would be processed as a two-column table.
2. A **path** that names content WOD cannot store — `4-adcp/`, `plankton/`. Only a path
   match pre-filters; a match in a column name or in prose waits until the bytes are in
   hand, so a readme that lists an accession's other folders cannot exclude the cast files
   sitting beside it.

**Excluded is not empty.** A folder that cannot become a profile often still holds metadata
for casts that can: an ADCP section carries bottom depth from bottom-tracking and navigation
that can supply or verify a cast's position; a multibeam sounding at a cast's position *is*
that cast's secondary header 10. `classify_dataset`'s exclusion carries a `harvest` field
saying what to take, and `classify_accession` collects it per folder. The pipeline does not
harvest automatically — matching two files on position and time is a judgement — so it is
named for you to do during staging, and cited when you do.

Both are bypassed when `dataset_type` or a matching `dataset_overrides` entry is set: an
operator who named the folder has looked at it.

---

## Phases

[Discovery](../discovery/README.md) · [Mapping](../mapping/README.md) ·
[QC transforms](../qc_transforms/README.md) · [Writers](../writers/README.md)
