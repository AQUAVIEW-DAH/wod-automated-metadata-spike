# WOD Agent ETL

Converts oceanographic data from an NCEI accession URL into standardized, CF-compliant
NetCDF profiles following World Ocean Database conventions.

Built to run unattended. An agent is given an accession link plus instructions and calls one
function; discovery, download, schema mapping, unit conversion, QC flagging, writing and
validation all happen without a human in the loop. Every file's outcome is recorded, and a
file that fails does not stop the run.

---

## Setup

```bash
uv sync            # runtime + dev dependencies, into .venv
uv run pytest -q   # 112 tests
```

Run everything with `uv run` from the project root.

| Package | Used for |
| --- | --- |
| `httpx` | HTTP fetching, Range requests, streaming downloads |
| `beautifulsoup4` | parsing Apache/NGINX directory indexes |
| `pandas`, `numpy` | tabular parsing, conversions, QC |
| `netCDF4` | reading and writing `.nc` |
| `pypdf`, `pytest` | dev only — manual extraction and tests |

---

## Quick start

A folder of files that carry their own coordinates goes straight through:

```python
from wod_etl.orchestration import process_accession
from wod_etl.verify import audit_dataset

result = process_accession("data/staging/0287423/2-CTD", dataset_type="CTD",
                           output_dir="data/processed/2-CTD", run_id="ctd")
print(audit_dataset("data/staging/0287423/2-CTD", "CTD", "data/processed/2-CTD")["text"])
```

Look before a large run:

```python
process_accession(src, dataset_type="CTD", dry_run=True)   # what would be processed
process_accession(src, dataset_type="CTD", limit=3)        # prove the shape on 3 files
```

Re-invoking with the same `run_id` resumes: completed files are skipped, interrupted ones
retried. `retry_failed=True` also re-attempts failures.

### An accession is not a dataset

Do **not** point `process_accession` at an accession root and read the pass count. A real
NCEI accession is a submitter's whole cruise — two dozen numbered folders of which only a
few are WOD profile data, plus readmes, cruise reports and imagery. Triage first:

1. **Is there a Table 3a measured variable in it?** Plankton biomass, bottom depth,
   currents, nitrite, PAHs and eDNA cannot become profiles. See
   [standardization_rules.md](knowledge/wod_code_tables/standardization_rules.md) →
   *What the profile path cannot carry*.
2. **Is there any data?** Count real values per column — whole columns are often
   `Not analyzed`.
3. **Where do its coordinates live?** In the data file, in a sibling station list, or only
   in the readme PDF. That decides whether it needs [Phase 0](wod_etl/staging/README.md).

Then give each dataset its own `run_id` and `output_dir`, and run `audit_dataset` on each.

### `status: "complete"` is not a quality result

It means every file was written and passed structural validation. A column in the wrong
unit, a dropped variable and a spurious QC flag all produce a clean run. Always open a
finished file and read the value ranges — that is how unit errors and instrument artifacts
surface, and `audit_dataset` prints them per dataset.

### What comes back

```python
{"status": "complete" | "incomplete" | "aborted" | "error" | "empty" | "dry_run",
 "counts": {"done": 42, "failed": 3, "skipped": 5},
 "failure_rate": 0.06,
 "outputs": ["data/processed/cast_01.nc", ...],
 "failures": [{"key": "cast_07.csv", "stage": "load_tabular", "error": "..."}],
 "manifest_path": "data/staging/manifests/<run_id>.json"}
```

A run aborts on its own if more than half of the attempts fail, on the assumption that
something systematic is wrong rather than the files being individually bad.

---

## The pipeline

```
  [ accession URL or local directory ]
              │
              ▼
  Phase 0  wod_etl/staging/      only when a file cannot be read as it stands:
              │                  sidecar positions, cruise-wide tables, pressure-only
              │                  casts, decorated headers, sentinel legends
              ▼
  wod_etl/orchestration/         process_accession — drives everything, records outcomes
              │
              ├─ Phase 1  wod_etl/discovery/      list → peek → profile → download → split → load
              ├─ Phase 2  wod_etl/mapping/        WOD spec lookup → mapping contract
              ├─ Phase 3  wod_etl/qc_transforms/  unit conversion → QC flags
              └─ Phase 4  wod_etl/writers/        ragged NetCDF
                          wod_etl/verify/         validate structure → audit the values
                          wod_etl/reference/      WOD tables + manual lookup (used by all)
              │
              ▼
  [ standardized .nc files + a run manifest ]
```

Every skill takes and returns JSON-serializable values, holds no hidden state, and returns
errors rather than raising — so an agent can call one, read the result, and decide.

---

## Skill reference

Each package has its own detailed doc — function-by-function inputs, output schemas, failure
modes and gotchas. The tables below are the index; the docs are the reference.

| Phase | Detailed doc |
| --- | --- |
| 0 — Staging | [wod_etl/staging/README.md](wod_etl/staging/README.md) |
| 1 — Discovery | [wod_etl/discovery/README.md](wod_etl/discovery/README.md) |
| 2 — Mapping | [wod_etl/mapping/README.md](wod_etl/mapping/README.md) |
| 3 — Conversions and QC | [wod_etl/qc_transforms/README.md](wod_etl/qc_transforms/README.md) |
| 4 — Writers | [wod_etl/writers/README.md](wod_etl/writers/README.md) |
| Verification | [wod_etl/verify/README.md](wod_etl/verify/README.md) |
| Orchestration | [wod_etl/orchestration/README.md](wod_etl/orchestration/README.md) |
| Reference | [wod_etl/reference/README.md](wod_etl/reference/README.md) |

### Phase 0 — Staging (`wod_etl/staging/`)

Only needed when a file cannot be read as it stands. Primitives, not a universal converter:
each accession's sidecar is shaped differently, so compose these rather than expecting a
declarative joiner to fit.

| Function | Purpose |
| --- | --- |
| `read_table(path, header_row, encoding)` | Read a delimited originator file through an encoding fallback (cp932 is common), dropping the blank rows a `\r\r\n` file interleaves, with `header_row` indexing the non-blank rows so a title line above the header is handled. |
| `clean_sentinels(rows, columns, extra)` | Replace an originator's missing-value legend (`**NS`, `*DL`, `***NA`, `**ND`) with empty fields and **count every one**. Below-detection markers are real measurements WOD has no flag for. |
| `group_rows(rows, columns, key_columns)` | Split one cruise-wide table into per-cast groups, in file order. |
| `parse_coordinate(value, minutes, hemisphere)` | Signed decimal degrees from decimal, degrees + decimal minutes, `64-05.8454N`, DMS, or three separate columns. A hemisphere letter beats a leading minus. |
| `parse_timestamp(value, time_value)` | Datetime from the usual spreadsheet forms, including single-digit months and a separate time column. |
| `pressure_to_depth(pressure_dbar, latitude_deg)` | UNESCO 1983 (Fofonoff & Millard). CTD exports often carry pressure only; WOD needs a depth axis. |
| `write_staged_cast(path, columns, rows, *, latitude, longitude, date, ...)` | Write the `Key: value` header block Phase 1 reads, then the CSV. `metadata` becomes Table 4a secondary headers; `notes` is where every derived value is recorded. |

### Phase 1 — Discovery (`wod_etl/discovery/`)

| Function | Purpose |
| --- | --- |
| `list_remote_directory(url_or_path, recursive, filter_extension)` | Crawl an HTTP index or local folder. Parses size and last-modified, and refuses to follow links out of the starting directory. |
| `fetch_and_peek_file(path, num_bytes)` | Read only the first N bytes via HTTP Range — identify a file without downloading it. |
| `profile_raw_data(preview_text, path)` | Separate header comments from data, sniff the delimiter, read column names. |
| `download_file(url, dest_dir, subdir, overwrite)` | Stream a full file to `data/raw/`. Writes `.part` then renames, so an interrupted run never leaves a truncated file. Returns sha256. |
| `split_casts(path_or_text, profile)` | Split a file holding several casts. A header block appearing after data marks the next cast. Safe to call on single-cast files. |
| `load_tabular(path, profile, ...)` | Read the complete file into a DataFrame using the profiled structure. Maps `-999`-style sentinels to NaN. |

### Phase 2 — Mapping (`wod_etl/mapping/`)

| Function | Purpose |
| --- | --- |
| `query_wod_spec(dataset_type, variable_query, depth_increment_m)` | WOD standards lookup, loaded from `knowledge/wod_code_tables/`. Resolves the real dataset code. |
| `generate_field_mapping(profile_result, spec_result)` | Build the mapping contract. |
| `match_variable(column_name)` | Column name → WOD variable code. Normalizes away unit suffixes. |
| `resolve_dataset(label, depth_increment_m)` | Route an instrument label to a WOD dataset. |

### Phase 3 — Conversions and QC (`wod_etl/qc_transforms/`)

| Function | Purpose |
| --- | --- |
| `convert_units_and_types(df, contract, seawater_density_kg_m3)` | Apply the contract's conversions; log every constant and its provenance. |
| `apply_qc_auditing(df, contract, high_resolution)` | Depth, range, gradient, inversion and zero-sensitivity checks; append Table 12 flags. |
| `range_at_depth(code, depth_m)` / `gradient_limits(code, depth_m)` | The thresholds used, if you want to check a value by hand. |

### Phase 4 — Writers (`wod_etl/writers/`)

| Function | Purpose |
| --- | --- |
| `write_ragged_profiles(casts, out_path)` | Write N casts into one contiguous ragged array file. |
| `generate_netcdf_ragged(df, contract, out_path)` | Single-cast wrapper over the above. |

### Verification (`wod_etl/verify/`)

| Function | Purpose |
| --- | --- |
| `validate_intermediate_netcdf(path)` | Six structural check groups; returns errors and warnings separately. Errors fail the file. |
| `audit_dataset(staged_dir, dataset_type, processed_dir)` | Read a whole dataset back: mapped and **dropped** columns, per-variable value ranges, QC flag distribution, all-missing variables. The quality check `process_accession` cannot give you. |
| `contract_for(path, dataset_type)` | One file's mapping contract, without auditing the set. |

Structural validation gates the output; the audit is what you report. A wrong unit, a
dropped column and an impossible value all pass the validator.

### Reference (`wod_etl/reference/`)

| Function | Purpose |
| --- | --- |
| `query_wod_knowledge(query, kind, depth_m)` | Look up a variable, dataset, header, flag, threshold or constant — every answer carries its manual page citation. |
| `search_manual(term)` | Keyword search over the manual's prose for anything not in a table. |
| `wod_variables()` / `wod_datasets()` / `qc_flag_scales()` / … | Whole tables, for pipeline code. |

### Orchestration (`wod_etl/orchestration/`)

| Function | Purpose |
| --- | --- |
| `process_accession(url, ...)` | The entry point. Everything, for every file, with bookkeeping. |
| `process_one_file(url, dataset_type, out_dir, run_id)` | All four phases for one file, no manifest. |
| `run_manifest.*` | The ledger: `resume_or_create`, `pending_items`, `mark`, `summarize`, `list_runs`. |

---

## The mapping contract

`generate_field_mapping` produces the artifact every later phase reads. Inspect it first
when a file behaves oddly.

```python
{"column_mapping": [{"raw_column": "Temp (deg F)", "wod_code": 1,
                     "wod_name": "Temperature", "target_unit": "Degrees Celsius",
                     "conversion_needed": "fahrenheit_to_celsius"}],
 "extracted_header_metadata": {"1": {"name": "Accession Number", "value": "0287423"}},
 "parsed_coordinates": {"latitude": 61.93, "longitude": -172.27, "year": 1985, ...},
 "missing_required_coordinates": [],
 "unmapped_columns": ["Cond"],
 "warnings": [],
 "ready_for_transform": true}
```

- **`unmapped_columns` are dropped from the output.** Check this before trusting a run. To
  map one, add an alias to `VARIABLE_ALIASES` in `wod_etl/mapping/query_wod_spec.py`.
- **`missing_required_coordinates`** means the file will fail validation later.
- **`ready_for_transform: false`** means do not proceed without looking.

### Dataset routing

`XCTD` is not a WOD dataset. XCTD and CTD are routed by **resolution, not instrument**
(manual p.19): depth increment below 2 m → `CTD`, otherwise → `OSD`. The pipeline measures
each cast's median depth spacing and decides. Passing `dataset_type="XCTD"` is correct;
expect some outputs to report `wod_dataset_type = OSD`.

---

## Quality flags

Two scales, both from manual Table 12. **The same integer means different things in each** —
that is per the manual, not a bug.

`z_qc` — depth flags:

| | |
| --- | --- |
| 0 | accepted |
| 1 | duplicate or inversion in recorded depth |
| 2 | density inversion |

`<var>_qc` — observed level flags:

| | |
| --- | --- |
| 0 | accepted |
| 1 | range outlier |
| 2 | failed inversion check (MIV) |
| 3 | failed gradient check (MGV) |
| 4 | zero-sensitivity |
| 5 | combined gradient and inversion |
| 6 | range and inversion |
| 7 | range and gradient |
| 9 | range, gradient and inversion |

Nothing is deleted — flagged levels are what the manual excludes at interpolation time. A
file full of non-zero flags is still valid output.

---

## Knowledge base

`knowledge/wod_readme.pdf` is the WOD 2023 User's Manual (131 pages). Its tables are
extracted to JSON with page citations, and the code loads them from there rather than
hardcoding values.

**[`knowledge/README.md`](knowledge/README.md) is the index.** It routes each kind of
question to the file that answers it, so the manual does not have to be re-read mid-task:

| Tier | For | Cost |
| --- | --- | --- |
| JSON code tables | codes, units, flags, thresholds, ranges | a function call |
| Prose references | datasets, cast structure, QC, scope, submission shapes | one short file |
| `search_manual(term)` | anything else — finds the page from a cached full-text index | free |
| The PDF | only the appendices listed in the index | a read |

| Prose reference | Answers |
| --- | --- |
| [`datasets.md`](knowledge/datasets.md) | The eleven datasets, and how to choose — resolution decides CTD vs OSD, motion distinguishes MRB/PFL/DRB, what a cruise and a cast are |
| [`cast_structure.md`](knowledge/cast_structure.md) | The seven parts of a cast, the four colliding numbering systems, where information belongs when it is not a Table 3a variable |
| [`wod_code_tables/standardization_rules.md`](knowledge/wod_code_tables/standardization_rules.md) | QC procedure and flags, what the profile path cannot carry, the SUR model, unit traps |
| [`accession_field_guide.md`](knowledge/accession_field_guide.md) | How submissions arrive, and the intermediate format contract |

| File | Contents |
| --- | --- |
| `table_2_datasets.json` | The eleven WOD dataset names (p.17) |
| `table_3a_variables.json` | 29 measured variables — code, unit, datasets (p.18) |
| `table_3b_unit_constants.json` | Atomic weights, molar volume of O₂ (p.19) |
| `table_4a_secondary_headers.json` | 67 secondary header codes (p.23) |
| `table_11_range_variables.json` | Which variables get range checks (p.54) |
| `table_12_qc_flags.json` | All five QC flag scales (p.55) |
| `table_13_gradients.json` | MGV / MIV / ZSI factors (p.56) |
| `appendix_11_ranges.json` | Acceptable ranges by depth, 33 levels (pp.87–113) |
| `standardization_rules.md` | The QC procedure, what the profile path cannot carry, the SUR model, unit traps |

**Never hardcode a WOD code, unit or threshold from memory.** Read the JSON, or the PDF page
it cites.

[`accession_field_guide.md`](knowledge/accession_field_guide.md) is ground truth about how
submissions actually *arrive*, as opposed to what WOD requires:

- **What not to add** — currents, imagery, catch data, plankton biomass, bathymetry, and
  anything 100% unanalysed
- **Coordinates** — what `49-46.9858N` means, non-English headers (経度/緯度 and the
  longitude-before-latitude trap), and why a hemisphere letter beats a minus sign
- **Table shapes** — units rows, metadata blocks, footer legends, missing-value legends
- **One cast across many files** — one file per depth, or one file per variable
- **Dates and times** — reaching UTC, and why "Julian year-day" is *not* the Julian calendar
- **The intermediate format contract** — what every output guarantees, so a single routine
  can convert it to WOD binary

---

## Directory layout

Three things matter: **knowledge** (what WOD says), **data** (a run's inputs and outputs),
and **skills** (the code). Everything else is scaffolding.

```
.
├── docs/AGENT.md                      ← INSTRUCTIONS. Canonical, runtime-neutral.
├── CLAUDE.md  GEMINI.md  AGENTS.md    Thin pointers to docs/AGENT.md, one per runtime
├── .claude/skills/wod-etl/SKILL.md    Pointer too — keeps /wod-etl working in Claude Code
│
├── wod_etl/                           ← CODE. A normal importable Python package.
│   ├── config.py                      Paths, HTTP settings, WOD constants, failure policy
│   ├── knowledge/                     Table loading + query_wod_knowledge
│   ├── staging/                       Phase 0 (only when a file needs it)
│   ├── discovery/                     Phase 1
│   ├── mapping/                       Phase 2
│   ├── qc_transforms/                 Phase 3
│   ├── writers/                       Phase 4
│   ├── verify/                        Structural validation + the dataset audit
│   ├── reference/                     Loads knowledge/ — codes, units, flags, thresholds
│   └── orchestration/                 The driver and the run ledger
│
├── knowledge/                         ← KNOWLEDGE. Ground truth, not code.
│   ├── README.md                      Index — which tier answers which question
│   ├── wod_readme.pdf                 WOD 2023 User's Manual, 131 pages
│   ├── datasets.md                    The eleven datasets and how to choose
│   ├── cast_structure.md              The seven parts of a cast; the colliding code tables
│   ├── wod_code_tables/               8 JSON tables + standardization_rules.md
│   ├── accession_field_guide.md       How submissions arrive; the intermediate contract
│   └── wod_readme_db/page_text.json   Page cache for search_manual (rebuildable)
│
├── data/                              ← DATA. A run's working area.
│   ├── raw/<run_id>/                  Downloaded files — the provenance record
│   ├── staging/manifests/             One JSON ledger per run
│   └── processed/                     Output .nc files
│
├── tests/                             225 tests
├── README.md
└── pyproject.toml
```

**Why instructions and code are separate** — the package is a normal root-level Python
package, owned by no agent runtime's directory convention, so `uv sync` installs it editable
and `import wod_etl` works from anywhere. The procedure lives once in `docs/AGENT.md`;
`CLAUDE.md`, `GEMINI.md`, `AGENTS.md` and `.claude/skills/wod-etl/SKILL.md` are ~20-line
pointers to it. Each runtime finds the file it looks for, and there is still only one copy
of the instructions to maintain.

Requirements are a shell, Python 3.12+ and file reading — nothing runtime-specific.

Paths are resolved by walking up from the package looking for `knowledge/wod_code_tables/`,
so `data/` and `knowledge/` are found no matter where you run from. Override with
`WOD_PROJECT_ROOT` if you need to point at a different workspace.

---

## Testing

```bash
uv run pytest -q                       # everything
uv run pytest tests/test_mapping.py -v # one module
```

The suite runs a full end-to-end job against a local fixture accession — including a
deliberately broken file and a metadata file — and asserts the run completes, isolates the
failure, and produces validated output. It also asserts that **every JSON table in
`knowledge/wod_code_tables/` is reachable through the loader**, so a table cannot be added
and then quietly go unused. Tests that pin a previously-fixed defect say so in
their docstring, so a regression is recognisable rather than just red.

---

## Known limitations

Everything in the earlier defect list has been fixed and pinned by a test. What remains is
scope not yet built, not broken behaviour:

| Area | Limitation |
| --- | --- |
| Range checks | Bounds are the **union across all 30 ocean basins** at each depth. Assigning a cast to a basin needs `range_area.msk` and `range_basin_list.msk` from the WOD website, which are not in the PDF. Bounds are depth-dependent but more permissive than a basin-aware check — at 3000 m the temperature envelope reaches 34 °C because the manual's Red Sea column allows it. |
| Density checks | Depth flag `2` (density inversion, manual p.54) is defined but never assigned; seawater density is not computed. |
| Standard levels | Interpolation to standard depths (Reiniger-Ross, p.56) is not implemented. This project produces intermediate observed-level files, which is the stage before that. |
| Pressure → depth | `process_accession` flags a pressure-only file in `warnings` rather than converting it, because the conversion needs latitude. Convert it during [staging](wod_etl/staging/README.md) with `pressure_to_depth`, which takes the cast's own latitude. |
| `split_casts` | Heuristic: a header block after data starts a new cast. Files that separate casts some other way (a blank line, a row count, a fixed record length) will not split correctly. |
| Manual search | `search_manual` is substring matching over cached page text, not semantic search. It finds the word you type, not the concept. Build the cache with `uv run python -m wod_etl.reference.index_manual`. |
| Appendices not extracted | Ocean weather stations, WMO squares, taxonomic codes, standard depth levels (Appendix 9) and the Reiniger-Ross table (Appendix 10) are not in JSON. Reach them with `search_manual`, then open the PDF at that page. |
| Instrument artifacts survive QC | The permissive basin-union range lets extreme values through. A real XCTD cast in this project ends with a 12-record linearly extrapolated tail reaching salinity 83.8; the range check caught 8 of 9 out-of-range levels, and the smooth ramp defeats the gradient check entirely. Read the value ranges from `wod_etl.verify.audit_dataset` — a physically impossible max is the signal. |
| Plankton and biomass | Not representable. Table 3a has no plankton variable; WOD stores it in the Biological Header plus Taxa/Biomass records (manual §5–6), where a measurement carries an upper and lower depth. Such a dataset must be reported as needing that path, not forced onto a profile level. |
| Below-detection values | `*DL`/`**ND` become missing, because Table 12 has no observed-level flag for them. Substituting the stated detection limit would fabricate a measurement, so it is not done. The loss can be large and must be reported. |
| Julian year-day | Manual p.37 defines year-day 0.00 as 00:00 on 1 January, then gives a worked example implying one more. The definition is used; the choice is written into the staged header and should be confirmed. |
| `seawater_density` | 1025 kg/m³ is an implementer's assumption, not a WOD constant — Table 3b supplies no density. It is recorded in every output's global attributes whenever a conversion used it. |
