# WOD ETL

Converts an NCEI accession into World Ocean Database–standard, CF-compliant NetCDF, with a
person reviewing what goes in. Run it in a browser or a terminal; both do the same two steps.

It identifies each file's format from its bytes and its WOD dataset from its content,
proposes what to include and why, and writes measurements and metadata to their WOD fields.
Moorings become MRB profiles; plankton tables become Taxa-Records.

## Setup

```bash
uv sync            # Python 3.12+
uv run pytest -q
```

Run everything with `uv run` from the project root.

## Run an accession

### In the browser

```bash
uv run wod-etl web                 # http://127.0.0.1:8000
```

1. **Run**: enter an accession number, NCEI URL, local folder or `.tar.gz`. It downloads and
   triages every file and writes `data/runs/<accession>/decisions.toml`. Nothing is processed.
2. **Review**: each file has an entry with what it holds, `include`, `dataset` and `why`.
   Change what you disagree with and Save. Files that cannot be written are proposed out,
   with the reason.
3. **Confirm**: stages, processes and verifies the included files. The output appears on the
   right, with download links. Click a file to preview it: its casts, each cast's levels
   and fields, a profile plot, and the file's attributes.

Running a step again redoes only what changed. The page is local-only, has no login and runs
one command at a time. See [`wod_etl/web/README.md`](wod_etl/web/README.md).

### In a terminal

```bash
uv run wod-etl run 0303928                                    # or a URL, folder or .tar.gz
uv run wod-etl run data/raw/my_cruise --accession-id my_cruise
uv run wod-etl confirm 0303928
uv run wod-etl status 0303928
```

| Exit code | Meaning |
| --- | --- |
| 0 | done (`run`: decisions ready for review) |
| 1 | a stage or a file failed; the message says how to fix it |
| 2 | decisions needed before `confirm` can write |
| 3 | written, but not verified (see `report.md`) |
| 130 | interrupted; run it again to continue |

### Output

- `data/runs/<accession>/`: `decisions.toml`, `report.md` (read this first), `run.log`.
- `data/processed/<accession>/`:
  - profiles, one file per dataset and year, e.g. `wod_ctd_2008_0062621.nc`;
  - plankton, one Taxa-Record file per year, e.g. `wod_osd_2015_0303928_taxa.nc`.

## Operating principles

1. Never invent a coordinate, date or platform code. A position in a readme or cruise report
   counts as a source; cite it.
2. Never drop data silently. Everything left out (columns, files, failures, sentinel values)
   is named in `decisions.toml` and `report.md`.
3. A file that fails verification is not output.
4. `data/raw/` is never edited. Reshaping happens in a staged copy.
5. Record every derived or assumed value in the staged file's notes. They end up in the
   NetCDF `comment`.
6. Report what failed, what was left out, and on what evidence.
7. Name what could not be done. For plankton, that includes unresolved taxa and Biological
   Header fields nobody stated (gear, mesh, volume filtered…). Only the submitter has them.

## WOD reference

The WOD 2023 manual is [`knowledge/wod_readme.pdf`](knowledge/wod_readme.pdf). Its tables are
extracted to JSON with page citations, and the code reads those rather than hardcoded values.
[`knowledge/README.md`](knowledge/README.md) says which file answers which question, and which
appendices still need the PDF.

## For developers

```
wod_etl/
  cli.py          the wod-etl command
  web/            the browser page (FastAPI + static files)
  orchestration/  the run/confirm stages, decisions.toml, recipes, report
  staging/        reshaping files that cannot be read as they stand
  discovery/      list, fetch, sniff, read, classify
  mapping/        columns and metadata to WOD codes
  qc_transforms/  unit conversion, opt-in WOD QC
  writers/        ragged NetCDF: profiles and Taxa-Records
  verify/         structural and value checks
  reference/      loads knowledge/wod_code_tables/
knowledge/        the manual, its tables as JSON, and prose references
tests/            unit and end-to-end tests; fixtures/accessions/
data/             working area, not tracked
```

Imports run one way: `cli` → `web` → `orchestration` → phase packages → `reference`. The page
runs each command as a `wod-etl` child process rather than importing `cli`.
Each package's README documents its functions:
[orchestration](wod_etl/orchestration/README.md) · [staging](wod_etl/staging/README.md) ·
[discovery](wod_etl/discovery/README.md) · [mapping](wod_etl/mapping/README.md) ·
[qc_transforms](wod_etl/qc_transforms/README.md) · [writers](wod_etl/writers/README.md) ·
[verify](wod_etl/verify/README.md) · [reference](wod_etl/reference/README.md) ·
[web](wod_etl/web/README.md).

**Custom staging scripts.** For a layout nothing here reads, write a script and name it in
`decisions.toml`:

```toml
[profiles.old_ascii]
kind = "script"
script = "scripts/0287423_old_ascii.py"
files = ["3-old-ascii/drop_001.dat"]
```

`confirm` runs the script with `WOD_DATA_ROOT` and `WOD_STAGING_OUT` set. The script writes
staged CSVs with `wod_etl.staging.write_staged_cast`. See
[`knowledge/reading_unknown_files.md`](knowledge/reading_unknown_files.md).

**New WOD release.** Replace the PDF and rebuild the code-list links:
`uv run python -m wod_etl.reference.extract_code_table_links`.

**Settings.** Move the data folders with `WOD_DATA_DIR` (or `WOD_RAW_DIR`,
`WOD_PROCESSED_DIR`…). The project root is found from `knowledge/wod_code_tables/`; override
it with `WOD_PROJECT_ROOT`.

## Known limitations

| Area | Limitation |
| --- | --- |
| Range checks | Bounds are the union of all 30 ocean basins, so they are permissive. Basin masks are not in the manual. |
| Standard levels | No interpolation to standard depths; output is at observed levels. |
| Pressure → depth | Automatic for pressure in dbar. Other units need a staging script. |
| Station lists | Joined when a delimited list names each cast's file or station; a workbook-only list is not read. Positions only in a PDF or readme prose need a staging script. |
| Unlabelled columns | XCTD and XBT files with a `# XCTD <cruise> <date> <time> <lat> <lon>` header line, and Tsurumi-Seiki XCTD exports, are read. Other files with unnamed columns, such as bare depth and temperature pairs, are left out; use a staging script. |
| Time zones | Offsets, fixed names (JST, NZST) and US regional zones (ET, PT) are converted to UTC. Ambiguous names (CST, EST, MST, IST, BST) are not. |
| Surface data | Written at 0 m (manual p.20), a year-day per observation. Streams logged on one clock are one SUR cast; separate tracks stay separate casts, and casts over WOD's 24,000-observation split are not split. |
| Metadata lookups | Country, platform, institute, project, probe, instrument, units and PI are coded from WOD's lists when one entry matches; otherwise the originator's text is kept and the report says why. |
| Left out on purpose | ADCP, images, video, PDFs, and tables with nothing WOD stores. Media files are not downloaded. |
| Plankton | Count matrices and whole-sample biomass need a staging script. Biological Header values cannot be set in `decisions.toml`. |
| Code lists | Fetched from the WOD website and cached for 30 days. Offline, values are kept as text. |
| NCEI down | A run by accession number uses the files downloaded earlier to `data/raw/<accession>` and says so; it cannot tell whether that download was complete. Each command waits on one timed-out request per NCEI service, then skips it. |
| WOD QC | Off by default (`WOD_RUN_QC=1` turns it on). The originator's flags are always kept verbatim. |
| Below-detection values | Become missing (Table 12 has no flag for them). The count is reported. |
| `seawater_density` | Per-volume values are converted with WOD's constant 1025 kg/m³ (manual p.15), not the sample's own density. Every file that uses it says so. |
| Nutrients in mass units | Phosphate, silicate and nitrate in mg/l are not converted: they are written as given under µmol/kg, with the originator's unit recorded. Range checks do not catch it. Chlorophyll in a molar unit is not written (no molar mass in Table 3b). |
| Web page | No cancel button and no accession-id field. Use `--accession-id` in a terminal. |
