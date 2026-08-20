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

## `process_accession(url_or_path, dataset_type="OSD", ...)`

```python
result = process_accession(
    "https://www.ncei.noaa.gov/data/oceans/archive/arc0222/0287423/",
    dataset_type="XCTD",
)
```

| Parameter | Purpose |
| --- | --- |
| `dataset_type` | Instrument hint from the user's instructions. See [dataset routing](../mapping/README.md#dataset-resolution-is-not-a-lookup). |
| `output_dir` / `raw_dir` | Override `data/processed/` and `data/raw/`. |
| `run_id` | Names the manifest. Reuse it to resume. Auto-generated from the URL + timestamp. |
| `recursive` | Crawl subdirectories (default `True`). |
| `filter_extension` | Only consider files with this extension. |
| `limit` | Process at most N files — use this to prove the shape first. |
| `resume` | Fold into an existing manifest (default `True`). |
| `retry_failed` | Also re-attempt items already marked `failed`. |
| `dry_run` | Report what would be processed; write nothing. |
| `on_error` | `"skip"` (default) or `"stop"`. |

### Recommended sequence

```python
process_accession(url, dataset_type="CTD", dry_run=True)   # 1. what's there
process_accession(url, dataset_type="CTD", limit=3)        # 2. prove it on 3 files
process_accession(url, dataset_type="CTD")                 # 3. the rest
```

Step 2 matters: a wrong `dataset_type` or an unusual file format shows up immediately on 3
files, rather than after 400.

### What comes back

```json
{"status": "complete", "run_id": "0287423_20260817T101500Z",
 "total_items": 50, "counts": {"done": 42, "failed": 3, "skipped": 5},
 "failure_rate": 0.066, "complete": false,
 "outputs": ["data/processed/cast_01.nc", "..."],
 "failures": [{"key": "cast_07.csv", "stage": "load_tabular", "error": "parse failed: ..."}],
 "manifest_path": "data/staging/manifests/0287423_20260817T101500Z.json"}
```

| `status` | Meaning |
| --- | --- |
| `complete` | Every item reached `done` or `skipped`. |
| `incomplete` | Some items are still `failed` — retryable. |
| `aborted` | Stopped early. Read `abort_reason`. |
| `error` | Could not even list the source. Check the URL. |
| `empty` | Listing succeeded but held no files. Check `filter_extension`. |
| `dry_run` | Nothing was written. |

**`complete: false` with `status: "complete"` is not a contradiction** — `status` describes
the run, `complete` describes whether every item is in a terminal state. Failed items are
neither done nor skipped, because they can be retried.

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
`download_file`, `fetch_and_peek_file`, `profile_raw_data`, `split_casts`, `mapping`,
`generate_netcdf_ragged`, `validate_intermediate_netcdf`, or `unexpected`.

**`status: "complete"` is not a quality result.** It says every file was written and passed
structural validation. It does not say the values are right, and this summary cannot tell
you — a column in the wrong unit, a dropped variable and a spurious QC flag all produce a
clean run. Three things the manifest never contains, which you have to gather yourself:

| Not in the summary | How to get it |
| --- | --- |
| Which columns were dropped | recompute the contract per file and collect `unmapped_columns` |
| Whether the values are plausible | open a NetCDF and read each variable's min/max |
| The QC flag distribution | read the `*_qc` arrays across the output set |

[`audit_dataset`](../verify/README.md) does all three. Run it for every dataset and report
it alongside the counts -- the manifest tells you a file was written, not that it is right.
### One accession is many runs

Give each dataset within an accession its own `run_id` and `output_dir`
(`run_id="osd_7-CTD-bottle"`, `output_dir="data/processed/7-CTD-bottle"`). They need
different `dataset_type` values, they fail for different reasons, and a single run over the
whole accession makes the 50%-failure abort meaningless — a folder of imagery would take the
whole run down with it.

---

## `process_one_file(url, dataset_type, out_dir, run_id, raw_root=None)`

All four phases for a single file, without the manifest. Returns
`{"ok": bool, "stage": ..., "error": ...}` and never raises. Use it to debug one file.

The chain it runs:

```
download_file → fetch_and_peek_file → profile_raw_data → split_casts
   → per cast: load_tabular → query_wod_spec → generate_field_mapping
                → convert_units_and_types → apply_qc_auditing
   → write_ragged_profiles → validate_intermediate_netcdf
```

Note the depth increment is measured from the loaded data (median positive spacing of the
depth column) and passed to `query_wod_spec`, which is how CTD/XCTD routing gets decided
per cast rather than guessed per run.

A cast that produces no usable mapping is dropped. If *every* cast drops, the file fails at
stage `mapping` — check `unmapped_columns` and `missing_required_coordinates` on that file
by hand.

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

## Skipped files

Extensions in `NON_DATA_EXTENSIONS` (`.xml`, `.pdf`, `.html`, `.json`, images, archives,
`.sh`) are skipped before any work happens — an accession is mostly metadata alongside its
measurements. Edit the set in [`config.py`](../config.py) if a real data file is being
skipped.

---

## Phases

[Discovery](../discovery/README.md) · [Mapping](../mapping/README.md) ·
[QC transforms](../qc_transforms/README.md) · [Writers](../writers/README.md)
