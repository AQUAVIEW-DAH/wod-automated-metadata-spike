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
| `output_dir` / `raw_dir` | Override `data/processed/` and `data/raw/`. |
| `run_id` | Names the manifest. Reuse it to resume. Auto-generated from the URL + timestamp. |
| `recursive` | Crawl subdirectories (default `True`). |
| `filter_extension` | Only consider files with this extension. |
| `limit` | Process at most N files — use this to prove the shape first. |
| `resume` | Fold into an existing manifest (default `True`). |
| `retry_failed` | Also re-attempt items already marked `failed`. |
| `verify` | Read the written NetCDF back at the end and check every value against the manual's acceptable range for that variable at that depth. **On by default** -- the failure that matters most is the one that passes structural validation. Sets `output_verified`. |
| `run_qc` | Whether to run WOD's own observed-level QC. Defaults to `RUN_WOD_QC` in [`config.py`](../config.py), which is **off** — QC is applied downstream, and a Table 12 flag written here is one more thing to reconcile. The ORIGINATOR's flag columns are carried through either way, verbatim and untranslated. |
| `dry_run` | Report what would be processed, and as what; write nothing. |
| `on_error` | `"skip"` (default) or `"stop"`. |

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
 "outputs": ["data/processed/cast_01.nc", "..."],
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

## `process_taxa(source, output_dir)` — biological data

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
