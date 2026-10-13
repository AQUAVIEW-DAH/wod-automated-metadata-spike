---
name: wod-etl
description: Convert ocean profile data from an NCEI accession URL or a local folder into WOD-standardized, CF-compliant NetCDF. Works out each file's format from its bytes and its WOD dataset from its own evidence, then triages the accession, stages what needs it, maps columns and metadata to WOD codes, converts units, and validates. Use when given an accession link, an archive path, or a directory of cast files (CTD, XCTD, XBT, MBT, bottle, buoy, float, glider) to standardize, convert, quality-control or triage.
when_to_use: Requests like process this accession, convert these casts to NetCDF, standardize this cruise data, QC these profiles, triage this accession, or any NCEI accession number, URL or folder of profile files. For a WOD code, flag, unit or threshold question with no files to process, use wod-reference instead.
---

# WOD ETL

Turn raw ocean profile files into standardized NetCDF that passes WOD and CF checks. Run it
unattended: every file's outcome is recorded, failures do not stop the run.

Setup: `uv sync` once, then run everything with `uv run python ...` from the repo root
(Python 3.12+).

## An accession is not a dataset

A real NCEI accession is a submitter's whole cruise: two dozen numbered folders of which
only a few are WOD profile data, plus readmes, cruise reports and imagery. Pointing
`process_accession` at the accession root and reading the pass count is how you ship
plausible, wrong output.

**A citation resolves to its data.** A landing page, an `/archive/accession/<n>` URL or a
bare number is resolved to the data directory -- the latest version when the accession is
versioned (`1.1/`, `2.2/`) -- and a downloaded `.tar.gz`/`.zip` is extracted under
`data/raw/`. `resolution` and `version` in the listing say where it went.

**Work through it in three steps.**

### 1. Triage — decide what is WOD profile data at all

Start here, always, before processing anything:

```python
from wod_etl.discovery import classify_accession

report = classify_accession("data/raw/0287423")
report["files"]          # one verdict per FILE: process/stage/sidecar/exclude/document/inspect
```

It reads every file's head and gives each **file** an `action` with its evidence: the format,
the dataset type, empty columns, why anything was excluded, and what is still worth taking
from it. NCEI archives everything under one `data/0-data`, so act on the per-file verdicts;
the folder verdicts only summarise. **`inspect` means the evidence did not decide.**

It answers the first two triage questions; the third is yours:

1. **Is there a Table 3a measured variable in it?** Plankton biomass, bottom depth, currents,
   nitrite, PAHs, eDNA, imagery and catch data cannot become profiles. `exclusion.route` says
   where each belongs instead. Background:
   [`accession_field_guide.md`](../../../knowledge/accession_field_guide.md) → *What not to
   add*.
2. **Is there any data?** `measurement_density: 0.0` means station and depth are full and
   every measurement is `Not analyzed`/`***NA`. **It reads only the first few KB** — count
   real values over the whole file before excluding anything.
3. **Where do its coordinates live?** In the data file, a station list (flagged `sidecar`),
   or only in prose in a readme -- that last one is yours to find.

**Check `cross_file_shapes`.** When a folder's files are identical columns with one row
each, or disjoint variables sharing a depth column, they are *parts* of one cast; a
`parm,value` table names its variable in a cell. Processed as they stand these give
single-level profiles, half-empty casts, or nothing. Combine with `combine_levels`,
`combine_on_key` or `pivot_long` in staging, confirming against the readme first.

**Excluding a folder is not the same as having nothing to take from it.** The verdict's
`harvest` field names what to keep: bottom depth and navigation from an ADCP section, a
sounding at a cast's position (that *is* secondary header 10), Table 6a collection metadata
from a plankton haul while the accession is still open. Nothing harvests automatically —
matching two files on position is a judgement — so do it in staging and cite the source file.

The verdict is evidence, not authority. Read every folder's readme, and **say what you
decided for every folder**, including the ones you excluded and why.

### 2. Stage what cannot be read as it stands

Files that carry their own coordinates and a clean header go straight to step 3. For the
rest — a sidecar station list, one table holding the whole cruise, a title row above the
header, pressure but no depth, `**NS`/`*DL` sentinels — build a derived copy first:

```python
from wod_etl.staging import (read_table, read_station_list, join_station_list, pivot_long,
                             melt_matrix, group_rows, clean_sentinels, rename_columns,
                             reconcile_stations, check_track, extract_pdf_rows,
                             pressure_to_depth, write_staged_cast)
```

To rename a column — including "call the latitude column Latitude" — use `rename_columns`
on the staged copy. It resolves the column from your name for it and refuses any rename
that would change its WOD code, unit or scale, each of which the name alone carries.

Read [`wod_etl/staging/README.md`](../../../wod_etl/staging/README.md) for the API and
[`knowledge/accession_field_guide.md`](../../../knowledge/accession_field_guide.md) for the
shapes themselves — coordinate forms like `49-46.9858N`, non-English headers, units rows,
sentinel legends, one-cast-across-many-files, and the date/time traps. The archive copy in
`data/raw/` is never edited; the staged copy records where every added value came from.

### 3. Process

```python
from wod_etl.orchestration import process_accession

result = process_accession(
    "data/staging/<accession>/2-CTD",   # or a URL/folder that needs no staging
    output_dir="data/processed/2-CTD",
)
```

That call does discovery, download, format identification, dataset classification, column
and metadata mapping, unit conversion, writing and validation for every file. It does **not** run WOD QC unless you ask
(`run_qc=True`).

**Do not pass `dataset_type` unless you mean to force it.** It is optional, and omitting it
is the normal case — see *The dataset is decided per file* below.

**Before a large run, look first:**

```python
process_accession(src, dry_run=True)   # what would be processed, and as what
process_accession(src, limit=3)        # prove the shape on 3 files
```

Then open one output and check the values, not just the status. Re-invoking with the same
`run_id` resumes: completed files are skipped, interrupted ones retried.

### Verify before you believe it

`status` says the run finished. **`output_verified` says the numbers are possible** — the
run now checks its own output, comparing every value against the manual's acceptable range
for that variable at that depth. `false` means a high finding: a variable ≥50% out of range
(a unit or a conversion, not bad data), a flat or negative depth axis, or a file that cannot
be read back. **`complete` with `output_verified: false` is a run you do not ship.** Each
finding in `output_verification.findings` carries the worst value, its depth, the bound it
broke, and what to do.

Two gaps it cannot close:

- **It judges what was written, not what should have been.** A dropped column is invisible
  to it — that is `unmapped_columns`, per dataset, every time.
- **Its bounds are the union of all 30 ocean basins**, so an unflagged value may still be
  wrong: nitrate's bound reaches 500 µmol/kg. Open one output and read the ranges.

And before staging: **check every position and date against the accession's other
sources** -- `reconcile_stations` across the station list, trawl log and report tables
(`extract_pdf_rows`), `check_track` along the cruise. A conflict is a question for the
submitter; record your choice in the staged notes.

`audit_dataset` is the wider per-dataset report: mapped and dropped columns across the whole
set, plus ranges and flags.

## Reading the result

`status` is `complete` | `incomplete` | `aborted` | `error` | `empty` | `dry_run`.

- `complete` — every item reached `done` or `skipped`. Report the output count.
- `failures` non-empty — name the files and the stage. Do not claim success.
- `aborted` — over half the *attempts* failed. Something systematic is wrong; diagnose one
  file by hand before rerunning.

**Skipped is not failed.** A file read successfully and found not to be profile data —
documentation, currents, plankton, a station-list sidecar, every measurement column empty —
is skipped with a reason and does not count toward the abort rate. A file that is *not* a
document and still will not read is a **failure**: a silent skip there is how a real cast in
an unrecognised layout disappears from a run.

**Fields you must report, none of them in `counts`:** `output_verified`, `dataset_types`,
`skipped_reasons`, `unstorable_columns_dropped` (rule 2), `outputs_without_time` (no date),
`outputs_without_time_of_day` (a date, no clock time: `GMT_time` is fill) and
`coded_headers_not_resolved` (coded metadata kept as text -- no unique code matched).

Retry the failures once you have addressed the cause:
`process_accession(url, run_id="<same id>", retry_failed=True)`. Full result schema:
[`wod_etl/orchestration/README.md`](../../../wod_etl/orchestration/README.md).

## The dataset is decided per file

**Do not pass `dataset_type`.** An accession is rarely one instrument — one submission holds
shipboard CTD, bottle nutrients, an ADCP section and a plankton haul — and one label applied
to all of them is wrong for most of them. Each file is classified from its own path, header
text, column names and depth spacing:

```python
from wod_etl.discovery import classify_dataset
classify_dataset(path="2-CTD/cast_01.cnv", columns=["prDM", "t090C", "sal00"])
# {"verdict": "profile", "dataset_type": "CTD", "confidence": "high", "evidence": [...]}
```

`verdict` has three values and they mean different things:

- **`profile`** — process it as `dataset_type`.
- **`excluded`** — it cannot become a profile. `exclusion.route` says where it belongs, and
  `exclusion.belongs_in_wod` separates plankton (belongs in WOD, other path) from currents
  and eDNA (not in WOD at all).
- **`unidentified`** — profile-shaped, but nothing names an instrument. **It is not
  guessed.** Read the folder readme and then say what it is.

**When to override, and how.** Pass `dataset_type="XBT"` only to force one type on a whole
run. For a mixed accession where you know some folders, name those and let the rest be
classified:

```python
process_accession(url, dataset_overrides={"3-XBT": "XBT", "5-argo": "PFL"})
process_accession(url, process_unidentified_as="OSD")   # a stated assumption; say so
```

An explicit type overrides an exclusion, deliberately: an operator who says "this folder is
XBT" has looked at it and the classifier has not.

**Report `dataset_types` from the result.** A run you expected to be all CTD coming back
`{"CTD": 3, "OSD": 39}` is not an error — `XCTD` is not a WOD dataset, and XCTD/CTD route by
**resolution, not instrument** (manual p.19): a depth increment below 2 m goes to `CTD`,
coarser goes to `OSD`. But it might also be a mis-routed folder. Check which, and say which.

## Files that are not CSV

Extensions lie. `.dat` is comma-delimited as often as it is fixed-width, NetCDF arrives with
no extension at all, and `.txt` is sometimes a Sea-Bird header. Identification is from the
bytes:

```python
from wod_etl.discovery import sniff_format, read_any

fmt = sniff_format(path)                 # netcdf / sea_bird_cnv / spreadsheet /
                                         # delimited / fixed_width / json / pdf / archive...
casts, info = read_any(path, fmt)        # [{"cast_id", "df", "header_block", "source_note"}]
info["warnings"]                          # never skip these
```

`read_any` returns the same shape for every format, and `header_block` is `Key: value` text
the mapper already parses — so a position held in a NetCDF attribute or a `.cnv`
`* NMEA Latitude =` line reaches the contract by the same route as a `# Latitude:` comment.

**Read `info["warnings"]`.** Inferred fixed-width boundaries, a `col_0…col_N` headerless
file, a `.cnv` whose name count does not match its columns — each is reported there and
nowhere else, and each needs a decision from you.

**For a file that does not just open — an unknown binary, a layout nobody documented —
follow [`reading-unknown-files.md`](reading-unknown-files.md).** Six questions to answer
before staging anything: what the file is, where the table starts, where the coordinates and
time live, what each column is in WOD terms, what the units really are, and whether there is
any data. It ends with the per-folder record to keep.

## When you need to look inside one file

Use the phase skills directly, from `wod_etl.<phase>`. Every one takes and returns
JSON-serializable values and returns errors instead of raising. The table below is the
index; each phase README lists its functions.

The order is: triage → list → download → sniff → read → classify → spec → contract →
convert → (QC, only if asked) → write → validate → verify.
`process_one_file(url, None, out_dir, run_id)` is that chain for a single file, without the
manifest.

### Detailed reference per phase

**Read the relevant one before working inside a phase.** Each documents every function's
inputs, output schema, failure modes and gotchas.

| Phase | Doc | Read it when |
| --- | --- | --- |
| 0 — Staging | [`wod_etl/staging/README.md`](../../../wod_etl/staging/README.md) | coordinates are in a sibling file or a readme, one table holds many casts, the header spans two rows, there is pressure but no depth, cells hold `**NS`/`*DL` |
| 1 — Discovery | [`wod_etl/discovery/README.md`](../../../wod_etl/discovery/README.md) | a file won't parse, columns look wrong, casts won't split, a listing is empty |
| 2 — Mapping | [`wod_etl/mapping/README.md`](../../../wod_etl/mapping/README.md) | columns are unmapped, a dataset won't resolve, a conversion isn't detected |
| 3 — QC transforms | [`wod_etl/qc_transforms/README.md`](../../../wod_etl/qc_transforms/README.md) | interpreting flags, checking a threshold, questioning a conversion |
| 4 — Writers | [`wod_etl/writers/README.md`](../../../wod_etl/writers/README.md) | you need the NetCDF layout, or a write fails |
| Verification | [`wod_etl/verify/README.md`](../../../wod_etl/verify/README.md) | validation fails, or you need to check a finished dataset is *right* rather than merely well-formed |
| Orchestration | [`wod_etl/orchestration/README.md`](../../../wod_etl/orchestration/README.md) | resuming a run, reading the manifest, tuning failure policy, writing plankton (`process_taxa`) |
| Reference | [`wod_etl/reference/README.md`](../../../wod_etl/reference/README.md) | looking up a WOD code, unit, flag or threshold in code — the loader for `knowledge/` |

## Mapping, metadata and flags

Three things here are expensive to get wrong. The rest —- the full contract schema, the
alias procedure, the metadata namespaces, the flag scales -— is in
[`mapping-and-metadata.md`](mapping-and-metadata.md). **Read it before adding an alias or
reporting what a run produced.**

**1. A column with no WOD code is dropped silently.** `unmapped_columns` is where it shows
up; `dropped_columns` is the subset refused *on purpose*, with a reason. Check both before
trusting a run, and report both. Conductivity, sigma-t, sound velocity, nitrite alone,
ammonium, CDOM and turbidity have no Table 3a code — mapping one onto an adjacent code is
worse than dropping it. **Potential temperature is not an alias of temperature**: theta is
derived, so aliasing it makes a cast claim Temperature twice, or report the wrong quantity
outright. Fluorescence maps to Chlorophyll only in a concentration unit; RFU or volts are
dropped. `explain_column(name)` says which and why.

**2. A cast is not only its numbers.** The probe, ship, bottom depth, sampling gear, wind,
measurement scale, originator's units and the originator's own flags all have WOD fields and
all resolve to no Table 3a code — so anything that maps only measurements drops every one of
them. `generate_field_mapping` runs `map_metadata` for you; read `secondary_headers`,
`column_mapping[].variable_specific_headers` and `originator_flags`, and report what they
hold. `Bottom Depth` is secondary header 10, not the depth axis.

**3. WOD QC is off by default — leave it off unless asked.** It is applied downstream.
The originator's own flags are always kept, verbatim, on the submitter's scale, and
**never translated to Table 12**: their `1` may mean *good* where WOD's `1` means a failed
range check. A missing `<var>_qc` is the normal shape; *no* flags at all is worth saying.

## Ground truth

Every WOD code, unit, flag value and threshold is already extracted into `knowledge/`.
**Never hardcode one from memory, and never transcribe one out of a PDF read — load it:**

```python
from wod_etl.reference import (wod_variables, wod_datasets, wod_secondary_headers,
                               variable_specific_headers, biological_headers,
                               taxa_biomass_variables, code_table_link, unit_constants,
                               observed_level_flags, gradient_factors, range_levels)
```

**Five of those tables number from 1 and mean different things.** Code `4` is Phosphate
(3a), Institution Code (4a), Institution (5a), Type of tow (6a), Biomass value (7a). Table
4a describes the **cast**; Table 5a describes **one variable within it** and repeats per
variable.

For anything you must *look up* rather than load, **use the `wod-reference` skill**. It
routes each kind of WOD question to the file that answers it, hands you the online code list
for any coded field, and names the short list that genuinely needs the manual. **Do not
re-read `knowledge/wod_readme.pdf`** — 131 pages, confusable tables, and page-scraped text
that drops characters.

| Question | File |
| --- | --- |
| Which dataset? MRB vs DRB vs PFL? CTD vs OSD? | [`datasets.md`](../../../knowledge/datasets.md) |
| Parts of a cast? Which table is this number from? | [`cast_structure.md`](../../../knowledge/cast_structure.md) |
| How is QC done? What can WOD not store? Which conversion? | [`standardization_rules.md`](../../../knowledge/wod_code_tables/standardization_rules.md) |
| What does `49-46.9858N` mean? How do I stage this? | [`accession_field_guide.md`](../../../knowledge/accession_field_guide.md) |
| The accession holds plankton. Now what? | [`plankton_biomass.md`](../../../knowledge/plankton_biomass.md) |
## Rules

1. **Never invent a coordinate, date or platform code** that no source states. A position
   given in a readme PDF *is* a source — use it and cite it. A dataset with no position
   anywhere cannot be staged; say so rather than filling one in.
2. **Never silently drop data.** Columns in `unmapped_columns` and `dropped_columns`, files
   in `failures` and `skipped_reasons`, and sentinel counts from `clean_sentinels` must all
   appear in what you report — including below-detection values, which are real measurements
   WOD has no flag for. A folder excluded from the profile path still has a `harvest`: say
   what you took from it and what you left.
3. **A file that fails validation is not output.** Report it as failed.
4. **Do not delete anything in `data/raw/`** — it is the provenance record for a run.
5. **Record every derived or assumed value where a later reader will find it**: in the
   staged header, and again in your report. A depth converted from pressure, a depth of 0
   applied by convention, a cast identified by matching two files — each is a judgement,
   not a measurement.
6. **Report honestly.** If 3 of 50 files failed, say so and name the stage. If a dataset
   was excluded, say which and on what evidence. If a spec question was ambiguous, say
   which way you resolved it and flag it for confirmation.
7. **Say what you could not do.** Plankton is not a profile: stage each haul with
   `write_staged_haul`, then write Taxa-Records with `process_taxa`
   ([`plankton_biomass.md`](../../../knowledge/plankton_biomass.md)). Report its
   `unresolved_taxa`, `taxa_resolved_at_higher_rank` and `missing_biological_fields` -- gear,
   tow, mesh, volume filtered, preservative, count method: only the submitter has them, so
   ask while the accession is open. Anything that belongs in WOD and was not written is an
   **unfinished item that needs naming**, not a success.
