# WOD ETL — agent instructions

**This is the canonical instruction file.** `CLAUDE.md`, `GEMINI.md`, `AGENTS.md` and
`.claude/skills/wod-etl/SKILL.md` are thin pointers to this document; keep the substance
here so the copies cannot drift.

Nothing below is specific to one agent runtime. The requirements are a shell, Python 3.12+
and the ability to read files — `uv sync` once, then `uv run python ...` from the repo root.

---

Turn raw ocean profile files into standardized NetCDF that passes WOD and CF checks. Run it
unattended: every file's outcome is recorded, failures do not stop the run.

## An accession is not a dataset

A real NCEI accession is a submitter's whole cruise: two dozen numbered folders of which
only a few are WOD profile data, plus readmes, cruise reports and imagery. Pointing
`process_accession` at the accession root and reading the pass count is how you ship
plausible, wrong output.

**Work through it in three steps.**

### 1. Triage — decide what is WOD profile data at all

List the accession, then for each candidate folder read its readme and peek at the data.
Three questions, in order:

1. **Is there a Table 3a measured variable in it?** If the only measurements are plankton
   biomass, bottom depth, currents, nitrite, PAHs, eDNA, imagery or catch data, it cannot
   become a profile. `knowledge/accession_field_guide.md` → *What not to add* is the
   checklist; `knowledge/wod_code_tables/standardization_rules.md` → *What the profile path
   cannot carry* explains why, including the two that belong in WOD but not through here.
2. **Is there any data?** Columns that are entirely `Not analyzed`/`***NA` mean a file with
   no measurements. Count real values per column before staging anything.
3. **Where do its coordinates live?** In the data file, in a sibling station list, or only
   in prose in the readme PDF. This decides whether it needs Phase 0.

Say what you decided for **every** folder, including the ones you excluded and why.

### 2. Stage what cannot be read as it stands

Files that carry their own coordinates and a clean header go straight to step 3. For the
rest — a sidecar station list, one table holding the whole cruise, a title row above the
header, pressure but no depth, `**NS`/`*DL` sentinels — build a derived copy first:

```python
from wod_etl.staging import (read_table, clean_sentinels, group_rows,
                             parse_coordinate, parse_timestamp,
                             pressure_to_depth, write_staged_cast)
```

Read `wod_etl/staging/README.md` for the API and
[`knowledge/accession_field_guide.md`](../knowledge/accession_field_guide.md) for the
shapes themselves — coordinate forms like `49-46.9858N`, non-English headers, units rows,
sentinel legends, one-cast-across-many-files, and the date/time traps. The archive copy in
`data/raw/` is never edited; the staged copy records where every added value came from.

### 3. Process

```python
from wod_etl.orchestration import process_accession

result = process_accession(
    "data/staging/<accession>/2-CTD",   # or a URL/folder that needs no staging
    dataset_type="CTD",                 # the instrument named in the user's instructions
    output_dir="data/processed/2-CTD",
)
```

Run it with `uv run python ...` from the project root. That call does discovery, download,
profiling, mapping, conversion, QC, writing and validation for every file.

**Before a large run, look first:**

```python
process_accession(src, dataset_type="CTD", dry_run=True)   # what would be processed
process_accession(src, dataset_type="CTD", limit=3)        # prove the shape on 3 files
```

Then open one output and check the values, not just the status. Re-invoking with the same
`run_id` resumes: completed files are skipped, interrupted ones retried.

### Verify before you believe it

`status: "complete"` means every file was written, not that the numbers are right. Three
checks that have each caught real corruption:

- **Open a NetCDF and read the ranges.** A variable in the wrong unit still validates.
- **Check `unmapped_columns` for every dataset**, not once. It is the only place a dropped
  column appears.

`audit_dataset` in `wod_etl.verify` does both at once, per dataset — value ranges, dropped
columns and the QC flag distribution. Run it and report it alongside the counts.
- **Cross-check a claim against an independent source in the accession.** Station-list
  times can be verified against an underway GPS track; positions in a data file against
  the sibling station list. A "Date time (UTC)" column beside a "Time zone" column is
  ambiguous until you test it.

## Reading the result

```python
{"status": "complete"|"incomplete"|"aborted"|"error"|"empty"|"dry_run",
 "counts": {"done": 42, "failed": 3, "skipped": 5},
 "failure_rate": 0.06,
 "outputs": [...paths to .nc files...],
 "failures": [{"key": "cast_07.csv", "stage": "load_tabular", "error": "..."}],
 "manifest_path": "data/staging/manifests/<run_id>.json"}
```

- `status: "complete"` — every item reached `done` or `skipped`. Report the output count.
- `failures` non-empty — say which files failed and at which stage. Do not claim success.
- `status: "aborted"` — the run stopped because more than half of the attempts failed.
  Something systematic is wrong (wrong URL, wrong `dataset_type`, files not tabular).
  Diagnose one file by hand before rerunning.

Retry just the failures once you have addressed the cause:

```python
process_accession(url, dataset_type="CTD", run_id="<same id>", retry_failed=True)
```

## Deciding `dataset_type`

Pass what the user says: `OSD`, `CTD`, `XCTD`, `XBT`, `MBT`, `SUR`, `APB`, `MRB`, `PFL`,
`DRB`, `UOR`, `GLD`. Default to `OSD` if unstated.

`XCTD` is not a WOD dataset. XCTD and CTD are routed by **resolution, not instrument**
(manual p.19): the pipeline measures each cast's depth increment and sends it to `CTD` when
below 2 m and `OSD` otherwise. So passing `XCTD` is fine — expect some outputs to say
`wod_dataset_type = OSD`, and do not treat that as an error.

## When you need to look inside one file

Use the phase skills directly. Every one takes and returns JSON-serializable values and
returns errors instead of raising.

```python
from wod_etl.staging import read_table, group_rows, write_staged_cast, pressure_to_depth
from wod_etl.discovery import (list_remote_directory, fetch_and_peek_file, profile_raw_data,
                              download_file, load_tabular, split_casts)
from wod_etl.mapping import query_wod_spec, generate_field_mapping
from wod_etl.qc_transforms import convert_units_and_types, apply_qc_auditing
from wod_etl.writers import write_ragged_profiles
from wod_etl.verify import validate_intermediate_netcdf, audit_dataset
```

The order is: list → peek → profile → split casts → load → spec → contract → convert → QC →
write → validate. `process_one_file(url, dataset_type, out_dir, run_id)` is that chain for
a single file if you want the whole thing without the manifest.

### Detailed reference per phase

**Read the relevant one before working inside a phase.** Each documents every function's
inputs, output schema, failure modes and gotchas.

| Phase | Doc | Read it when |
| --- | --- | --- |
| 0 — Staging | `wod_etl/staging/README.md` | coordinates are in a sibling file or a readme, one table holds many casts, the header spans two rows, there is pressure but no depth, cells hold `**NS`/`*DL` |
| 1 — Discovery | `wod_etl/discovery/README.md` | a file won't parse, columns look wrong, casts won't split, a listing is empty |
| 2 — Mapping | `wod_etl/mapping/README.md` | columns are unmapped, a dataset won't resolve, a conversion isn't detected |
| 3 — QC transforms | `wod_etl/qc_transforms/README.md` | interpreting flags, checking a threshold, questioning a conversion |
| 4 — Writers | `wod_etl/writers/README.md` | you need the NetCDF layout, or a write fails |
| Verification | `wod_etl/verify/README.md` | validation fails, or you need to check a finished dataset is *right* rather than merely well-formed |
| Orchestration | `wod_etl/orchestration/README.md` | resuming a run, reading the manifest, tuning failure policy |
| Reference | `wod_etl/reference/README.md` | looking up a WOD code, unit, flag or threshold in code — the loader for `knowledge/` |

## The mapping contract

`generate_field_mapping` produces the artifact everything downstream reads. Inspect it when
a file behaves oddly:

- `column_mapping` — raw column to WOD code, target unit, conversion required
- `unmapped_columns` — **silently dropped from the output.** Check this before trusting a run
- `missing_required_coordinates` — a file with these will fail validation later
- `ready_for_transform` — false means do not proceed without looking
- `warnings` — unresolved dataset, duplicate variables, variables not valid for the dataset

If a column you expect is in `unmapped_columns`, add an alias to `VARIABLE_ALIASES` in
`wod_etl/mapping/query_wod_spec.py`. Do not rename the source data. Instrument exports are
the usual cause — Sea-Bird names a column `T090C`, not `Temperature`.

But check first that WOD actually stores the variable. Conductivity, sigma-t, sound
velocity, nitrite, ammonium, CDOM and turbidity have no Table 3a code, and mapping one onto
an adjacent code is worse than dropping it. Potential temperature is the sharpest case:
aliasing it to code `1` makes a cast claim Temperature twice.

**Unit detection reads the full raw column name and nothing else.** A units row underneath
the header is invisible to it, so fold units into the name when staging (`DO (mg/L)`, not
`DO`). Check the conversion direction against Table 3a: alkalinity and DIC are stored
per-volume while nutrients are per-mass, so "already µmol/kg" is right for one and a
1000× error for the other.

## Quality flags

Two scales, both from manual Table 12. The same integer means different things in each;
this is per the manual, not a mistake.

- `z_qc` — depth flags: `0` accepted, `1` duplicate/inversion, `2` density inversion
- `<var>_qc` — observed level flags: `0` accepted, `1` range, `2` inversion, `3` gradient,
  `4` zero-sensitivity, `5` gradient+inversion, `6` range+inversion, `7` range+gradient,
  `9` range+gradient+inversion

Flags are recorded, never deleted. A file full of non-zero flags is still valid output;
report the distribution rather than discarding data.

## Ground truth

**Start at [`knowledge/README.md`](../knowledge/README.md).** It routes every question
to the file that answers it, and lists the short set of things that genuinely need the PDF.

Do not re-read `wod_readme.pdf` to resolve a doubt. It is 131 pages, its tables are easy to
confuse, and page-scraped text drops characters. Four tiers, in order:

| Tier | Use it for | How |
| --- | --- | --- |
| **Code tables** | Any code, unit, flag value, threshold, range | `from wod_etl.reference import wod_variables, wod_datasets, ...` — load, never read |
| **Prose references** | Datasets, cast structure, QC procedure, scope, submission shapes | the four `.md` files below |
| **`search_manual(term)`** | Anything else — finds the page from a cached full-text index, at no cost | `from wod_etl.reference import search_manual` |
| **The PDF** | Only the appendices listed in `knowledge/README.md` — country codes, code-value enumerations, taxonomy, WMO squares, standard levels, Reiniger-Ross | open it at the page `search_manual` gives you |

| Question | File |
| --- | --- |
| Which dataset? MRB vs DRB vs PFL? CTD vs OSD? | `knowledge/datasets.md` |
| What are the parts of a cast? Which table is this number from? | `knowledge/cast_structure.md` |
| How is QC done? What can WOD not store? Which conversion applies? | `knowledge/wod_code_tables/standardization_rules.md` |
| What does `49-46.9858N` mean? How do I stage this file? | `knowledge/accession_field_guide.md` |

**Never hardcode a WOD code, unit or threshold from memory**, and never transcribe one out of
a PDF read — load it from the JSON.

If you do have to open the PDF for something these files should have covered, **add it to
them** so the next run does not repeat the read.

Known limitation worth stating in reports: range checks use the union of all 30 ocean
basins at each depth, because assigning a cast to a basin needs `range_area.msk` from the
WOD website, which is not in the PDF. Bounds are depth-dependent but more permissive than
a basin-aware check.

## Rules

1. **Never invent a coordinate, date or platform code** that no source states. A position
   given in a readme PDF *is* a source — use it and cite it. A dataset with no position
   anywhere cannot be staged; say so rather than filling one in.
2. **Never silently drop data.** Columns in `unmapped_columns`, files in `failures`, and
   sentinel counts from `clean_sentinels` must all appear in what you report — including
   below-detection values, which are real measurements WOD has no flag for.
3. **A file that fails validation is not output.** Report it as failed.
4. **Do not delete anything in `data/raw/`** — it is the provenance record for a run.
5. **Record every derived or assumed value where a later reader will find it**: in the
   staged header, and again in your report. A depth converted from pressure, a depth of 0
   applied by convention, a cast identified by matching two files — each is a judgement,
   not a measurement.
6. **Report honestly.** If 3 of 50 files failed, say so and name the stage. If a dataset
   was excluded, say which and on what evidence. If a spec question was ambiguous, say
   which way you resolved it and flag it for confirmation.
7. **Say what you could not do.** A dataset that belongs in WOD but not through this path
   (plankton biomass) is not a success and not a failure — it is an unfinished item that
   needs naming.
