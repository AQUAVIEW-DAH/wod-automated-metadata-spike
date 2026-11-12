# WOD ETL

Converts an NCEI accession into CF-compliant NetCDF in World Ocean Database (WOD) form, with a
person reviewing what goes in. It runs in a browser or a terminal; both do the same two steps.

Each file's format is read from its contents and its WOD dataset from what it measures. `run`
proposes what to include and why; `confirm` writes the measurements and metadata to their WOD
fields. Moorings become MRB profiles; plankton tables become Taxa-Records.

## Setup

```bash
uv sync            # Python 3.12+
uv run pytest -q
```

Run everything with `uv run` from the project root.

## Run an accession

### In the browser

```bash
uv run wod-etl web                 # http://127.0.0.1:8000  (--host, --port)
```

1. Run: enter an accession number, NCEI URL, local folder or `.tar.gz`, or several separated
   by commas. Run downloads and triages every file and writes
   `data/runs/<accession>/decisions.toml`. Nothing is written to WOD form yet.
2. Review: each file has an entry with what it holds, `include`, `dataset` and `why`. Edit what
   you disagree with and save. Files that cannot be written are proposed out, with the reason.
3. Confirm: stages, writes and verifies the included files. The output files are listed on the
   right with download links. Click one to see its casts, each cast's levels, a profile plot
   and the file's attributes.

Input files have a preview link too: text, tables, XML and workbooks formatted for reading
("As stored" shows the raw file), a NetCDF file's variables, or the PDF itself.

Both previews have a Statistics tab: count, missing values, quartiles, mean, standard
deviation and a histogram for each variable; the most common values for flags and text; the
first and last value for times.

Several accessions run one after another. Each has a chip showing its state (waiting, running,
review, done, nothing written, failed); click it to open that accession, or step through them
with ‹ › or `[` `]`. × removes a waiting accession from the queue. Confirm ready confirms every
accession that is waiting for review and has nothing left to settle.

Running a step again redoes only what changed. The page is for local use: it has no login and
runs one command at a time, queueing the rest. Save checks the TOML and will not overwrite a
file that changed on disk; Run and Confirm save pending edits first. The page and the terminal
share `data/runs/<accession>/`, so a run started in one can finish in the other.

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

- `data/runs/<accession>/`: `decisions.toml`, `report.md` (start here), `run.log`.
- `data/processed/<accession>/`:
  - profiles, one file per dataset and year, e.g. `wod_ctd_2008_0062621.nc`;
  - plankton, one Taxa-Record file per year, e.g. `wod_osd_2015_0303928_taxa.nc`.

## Rules

- No coordinate, date or platform code is invented. A position from a readme or cruise report
  is used only with a citation.
- Nothing is dropped silently. Every file, column, failure or sentinel value left out is named
  in `decisions.toml` and `report.md`, with the reason.
- The report also names what could not be done, such as unresolved taxa or Biological Header
  fields (gear, mesh, volume filtered) that only the submitter can supply.
- A file that fails structural validation is not written.
- `data/raw/` is never edited; reshaping works on a staged copy.
- Derived or assumed values are recorded in the staged file's notes, which end up in the
  NetCDF `comment`.

## WOD reference

The WOD 2023 manual is [`knowledge/wod_readme.pdf`](knowledge/wod_readme.pdf). Its tables are
extracted to JSON with page citations in `knowledge/wod_code_tables/`, and the code reads them
instead of hardcoding values.

Standard names and their units are checked against the CF Standard Name Table
(`knowledge/cf_standard_names.json`), including deprecated aliases. Each output records the
table version in `standard_name_vocabulary`.

## For developers

```
wod_etl/
  cli.py          the wod-etl command
  web/            the browser page (FastAPI + static files)
  orchestration/  the run/confirm stages, decisions.toml, recipes, report
  staging/        reshaping files that cannot be read as they are
  discovery/      list, fetch, sniff, read, classify
  mapping/        columns and metadata to WOD codes
  qc_transforms/  unit conversion, opt-in WOD QC
  writers/        ragged NetCDF: profiles and Taxa-Records
  verify/         structural and value checks
  reference/      loads knowledge/: WOD tables, CF standard names
knowledge/        the manual and its tables as JSON, the CF Standard Name Table
tests/            unit and end-to-end tests; fixtures/accessions/
data/             working area, not tracked
```

Imports run one way: `cli` → `web` → `orchestration` → the stage packages → `reference`. The
page runs each command as a `wod-etl` child process rather than importing `cli`.

### Staging scripts

For a layout the built-in readers cannot handle, write a script and name it in
`decisions.toml`:

```toml
[profiles.old_ascii]
kind = "script"
script = "scripts/0287423_old_ascii.py"
files = ["3-old-ascii/drop_001.dat"]
```

`confirm` runs the script with `WOD_DATA_ROOT` and `WOD_STAGING_OUT` set. The script writes
staged CSVs with `wod_etl.staging.write_staged_cast`.

### Updating reference data

- New WOD release: replace the PDF and rebuild the code-list links with
  `uv run python -m wod_etl.reference.extract_code_table_links`.
- New CF Standard Name Table: rebuild `knowledge/cf_standard_names.json` from cfconventions.org
  with `uv run python -m wod_etl.reference.extract_cf_standard_names`.

### Settings

Move the data folders with `WOD_DATA_DIR` (or `WOD_RAW_DIR`, `WOD_PROCESSED_DIR`…). The project
root is found from `knowledge/wod_code_tables/`; override it with `WOD_PROJECT_ROOT`.

## Known limitations

| Area | Limitation |
| --- | --- |
| Range checks | Bounds are the union of all 30 ocean basins, so they are permissive. Basin masks are not in the manual. |
| Standard levels | No interpolation to standard depths; output is at observed levels. |
| Pressure → depth | Automatic for pressure in dbar. Other units need a staging script. |
| Station lists | Joined when a delimited list names each cast's file or station. A list only in a workbook is not read. Positions only in a PDF or readme prose need a staging script. |
| Unlabelled columns | Read for XCTD and XBT files with a `# XCTD <cruise> <date> <time> <lat> <lon>` header line, and for Tsurumi-Seiki XCTD exports. Other files without column names, such as bare depth and temperature pairs, need a staging script. |
| Time zones | Offsets, fixed names (JST, NZST) and US regional zones (ET, PT) are converted to UTC. Ambiguous names (CST, EST, MST, IST, BST) are not. |
| Surface data | Written at 0 m (manual p.20), with a year-day per observation. Streams logged on one clock become one SUR cast; separate tracks stay separate casts. Casts over WOD's 24,000-observation limit are not split. |
| Metadata lookups | Country, platform, institute, project, probe, instrument, units and PI are coded from WOD's lists when exactly one entry matches. Otherwise the originator's text is kept and the report says why. |
| Not stored by WOD | ADCP, images, video, PDFs, and tables with nothing WOD stores are left out. Media files are not downloaded. |
| Plankton | Count matrices and whole-sample biomass need a staging script. Biological Header values cannot be set in `decisions.toml`. |
| Code lists | Fetched from the WOD website and cached for 30 days. Offline, values are kept as text. |
| NCEI down | A run by accession number falls back to the earlier download in `data/raw/<accession>` and says so, but cannot tell whether that download was complete. Each command waits for one timeout per NCEI service, then skips that service. |
| WOD QC | Off by default (`WOD_RUN_QC=1` turns it on). The originator's flags are always kept as given. |
| Below-detection values | Become missing (Table 12 has no flag for them). The count is reported. |
| `seawater_density` | Per-volume values are converted with WOD's constant 1025 kg/m³ (manual p.15), not the sample's own density. Each file that uses it says so. |
| Nutrients in mass units | Phosphate, silicate and nitrate in mg/l are not converted. They are written as given under µmol/kg, with the originator's unit recorded, and range checks do not catch it. Chlorophyll in a molar unit is not written (Table 3b has no molar mass for it). |
| Web page | A running command cannot be stopped (a waiting one can). There is no accession-id field, so a local folder whose path has no accession number needs `--accession-id` in a terminal. |
