# WOD Agent ETL

Converts oceanographic data from an NCEI accession URL into standardized, CF-compliant
NetCDF profiles following World Ocean Database conventions.

Built to run unattended. An agent is given an accession link and calls one function;
triage, discovery, download, format identification, dataset classification, schema mapping,
unit conversion, QC flagging, writing and validation all happen without a human in the loop.
Every file's outcome is recorded, and a file that fails does not stop the run.

It works out what each file **is** rather than being told: the format from its bytes (NetCDF,
Sea-Bird `.cnv`, fixed-width ASCII, spreadsheets, delimited text, no extension at all), and
the WOD dataset from its own path, header, columns and depth spacing. Files that cannot
become a WOD profile — currents, plankton, empty columns, documentation — are skipped with a
stated reason, and with a note of the metadata still worth taking from them.

It maps the **metadata**, not only the measurements: the probe, the ship, the bottom depth,
the sampling gear, the measurement scale, the originator's units and the originator's own
quality flags all land in the WOD field that holds them, each with the URL of the code list
its values are coded against.

---

## Setup

```bash
uv sync            # runtime + dev dependencies, into .venv
uv run pytest -q   # the test suite
```

Run everything with `uv run` from the project root.

| Package | Used for |
| --- | --- |
| `httpx` | HTTP fetching, Range requests, streaming downloads |
| `beautifulsoup4` | parsing Apache/NGINX directory indexes |
| `pandas`, `numpy` | tabular parsing, conversions, QC |
| `netCDF4` | reading and writing `.nc` |
| `pypdf` | reading tables out of cruise-report PDFs, and the manual |
| `pytest` | dev only — tests |

---

## Run an accession yourself

No coding agent needed. One command runs every stage for one accession:

```bash
uv run wod-etl run 0303928                        # an accession number
uv run wod-etl run "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.nodc:303535"
uv run wod-etl run data/raw/0303928               # a folder already on disk, or a .tar.gz
uv run wod-etl status 0303928                     # where it stopped, and what is open
```

It resolves and downloads the accession, triages every file, stages what it can, writes the
WOD NetCDF and verifies it. **When a judgement is needed** -- which unit a table is in, what
an unlabelled file is, which of two sources has a station's date right -- it pauses and
writes the questions, pre-filled with its proposals, into
`data/runs/<accession>/decisions.toml`. Answer them and run the same command again: it
continues from where it stopped, and redoes only what your answers changed.

| Exit code | Meaning |
| --- | --- |
| 0 | finished, output verified |
| 1 | a stage failed -- the message says what failed, why, and how to fix it |
| 2 | paused: answer the questions in decisions.toml, then rerun |
| 3 | finished, but verification flagged the output (see report.md) |
| 130 | interrupted -- rerun to continue |

Everything about an accession lives in `data/runs/<accession>/`: `run.log` (every run,
person or agent, appended), `report.md` (the summary to read first), `decisions.toml` and
`state.json`. Outputs go to `data/processed/<accession>/pipeline/`. The full reference is
[*Run it yourself* in the orchestration README](wod_etl/orchestration/README.md#run-it-yourself-wod-etl).

---

## Quick start (Python, or a coding agent)

A folder of files that carry their own coordinates goes straight through:

```python
from wod_etl.discovery import classify_accession
from wod_etl.orchestration import process_accession
from wod_etl.verify import audit_dataset

classify_accession("data/raw/0287423")       # what is in here, folder by folder, and why

result = process_accession("data/staging/0287423/2-CTD",
                           output_dir="data/processed/2-CTD", run_id="ctd")
print(audit_dataset("data/staging/0287423/2-CTD", "CTD", "data/processed/2-CTD")["text"])
```

**`dataset_type` is optional and normally omitted** — each file is classified on its own
evidence. Pass it only to force one type on a whole run, or use
`dataset_overrides={"3-XBT": "XBT"}` to name the folders you know and let the rest be
classified.

Look before a large run:

```python
process_accession(src, dry_run=True)   # what would be processed, and as what
process_accession(src, limit=3)        # prove the shape on 3 files
```

Re-invoking with the same `run_id` resumes: completed files are skipped, interrupted ones
retried. `retry_failed=True` also re-attempts failures.

### Before you run it

Three things that decide whether the output is trustworthy — all covered in the skill,
[`.claude/skills/wod-etl/SKILL.md`](.claude/skills/wod-etl/SKILL.md):

- **An accession is not a dataset.** Triage every folder first with `classify_accession`;
  most folders in a real accession are not WOD profile data. Give each dataset its own
  `run_id` and `output_dir`.
- **`status: "complete"` is not a quality result.** It means every file was written and
  passed structural validation. A column in the wrong unit, a dropped variable and a
  spurious QC flag all produce a clean run. Run `audit_dataset` and read the value ranges.
- **`unmapped_columns` are dropped silently.** The run report names the ones WOD cannot
  store, in `unstorable_columns_dropped`; checking `unmapped_columns` is the only way the
  rest surface. Metadata columns and originator flag columns are *consumed*, not dropped,
  so they appear in neither.

The skill also documents the full result dict, the failure and resume semantics, the mapping
contract, the QC flag scales, and how the dataset is decided per file.
[`.claude/skills/wod-etl/reading-unknown-files.md`](.claude/skills/wod-etl/reading-unknown-files.md)
is the procedure for a file that does not just open.

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
              ├─ Phase 1  wod_etl/discovery/      triage → list → download → sniff → read → classify
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
| `classify_accession(root)` | Triage the whole accession: a `process`/`exclude`/`inspect` verdict per folder, with the format, dataset type, empty columns and evidence per file. Run this first. |
| `classify_dataset(path, columns, header_text, depth_increment_m)` | Which WOD dataset a file is — or whether it is WOD profile data at all. Returns the evidence, not just the answer. |
| `sniff_format(path)` | What a file actually is, from its bytes: NetCDF, Sea-Bird `.cnv`, spreadsheet, delimited, fixed-width, JSON, PDF, archive, unknown binary. Extensions are not consulted. |
| `read_any(path, sniff)` | Read any supported format into the same shape: one entry per cast, each with a DataFrame and a `Key: value` header block. |
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
| `generate_field_mapping(profile_result, spec_result, df)` | Build the mapping contract, measurements and metadata together. |
| `map_metadata(header_text, columns, df)` | Everything that is not a measurement: Table 4a cast metadata from header lines and from columns, Table 5a per-variable metadata, and the originator's quality flags. Each coded field carries its published code list. |
| `match_variable(column_name)` | Column name → WOD variable code. Normalizes away unit suffixes — and refuses the names that look like variables and are not. |
| `explain_column(column_name)` | Why a column has no code, for the ones that must **not** be aliased: potential temperature, conductivity, sigma-t, sound velocity, nitrite, ammonium, turbidity. The single registry. |
| `extract_unit(name)` / `extract_scale(name)` | What normalisation throws away, kept: `"deg C"` and `"ITS-90"` out of `t090C: Temperature [ITS-90, deg C]`. |
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
| `variable_specific_headers()` / `biological_headers()` / `taxa_biomass_variables()` | Tables 5a, 6a and 7a — per-variable metadata, plankton collection metadata, Taxa-Record fields. |
| `classify_taxa_code(code)` / `plankton_grouping_code()` | What a Taxa-Record variable number means (Table 8), and the PGC (Table 9). |
| `code_table_link(namespace, code)` / `code_table_hub(name)` | The online code list for a coded metadata field. WOD publishes these as links beside each manual entry — PDF annotations that do not survive a text extraction, so they are extracted here. |

### Orchestration (`wod_etl/orchestration/`)

| Function | Purpose |
| --- | --- |
| `process_accession(url, ...)` | The entry point. Everything, for every file, with bookkeeping. |
| `process_one_file(url, dataset_type, out_dir, run_id)` | Every phase for one file, no manifest. `dataset_type=None` classifies it. |
| `run_manifest.*` | The ledger: `resume_or_create`, `pending_items`, `mark`, `summarize`, `list_runs`. |

---

## WOD ground truth

`knowledge/wod_readme.pdf` is the WOD 2023 User's Manual (131 pages). Its tables are
extracted to JSON with page citations, and the code loads them from there rather than
hardcoding values — `wod_etl/reference/` is the loader.

**[`.claude/skills/wod-reference/SKILL.md`](.claude/skills/wod-reference/SKILL.md) is the
index.** It routes each kind of WOD question — a variable code, a flag value, a unit, a
threshold, which dataset applies, where the manual covers something — to the table or prose
file that answers it, so the manual does not have to be re-read mid-task. In Claude Code it
loads itself as the `wod-reference` skill.

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
| [`accession_field_guide.md`](knowledge/accession_field_guide.md) | How submissions arrive, what `49-46.9858N` means, the date/time traps, and the intermediate format contract |
| [`plankton_biomass.md`](knowledge/plankton_biomass.md) | Why plankton belongs in WOD but not in a profile, the Taxa-Record model, how `process_taxa` writes it, what to report |

**Never hardcode a WOD code, unit or threshold from memory.** Read the JSON, or the PDF page
it cites.

---

## Directory layout

Three things matter: **knowledge** (what WOD says), **data** (a run's inputs and outputs),
and **skills** (the code). Everything else is scaffolding.

```
.
├── .claude/skills/                    ← INSTRUCTIONS. Agent Skills, canonical.
│   ├── wod-etl/SKILL.md               The procedure: triage, stage, process, verify
│   ├── wod-etl/reading-unknown-files.md   Six questions for a file that will not just open
│   ├── wod-etl/mapping-and-metadata.md    The contract, the metadata namespaces, the flags
│   └── wod-reference/SKILL.md         WOD lookup: which tier answers which question
├── CLAUDE.md  GEMINI.md  AGENTS.md    Thin pointers to wod-etl/SKILL.md, one per runtime
│
├── wod_etl/                           ← CODE. A normal importable Python package.
│   ├── config.py                      Paths, HTTP settings, WOD constants, failure policy
│   ├── staging/                       Phase 0 (only when a file needs it)
│   ├── discovery/                     Phase 1
│   ├── mapping/                       Phase 2
│   ├── qc_transforms/                 Phase 3
│   ├── writers/                       Phase 4
│   ├── verify/                        Structural validation + the dataset audit
│   ├── reference/                     Loads knowledge/ — codes, units, flags, thresholds
│   └── orchestration/                 The drivers (profiles, and Taxa-Records) and the run ledger
│
├── knowledge/                         ← KNOWLEDGE. Ground truth, not code.
│   ├── wod_readme.pdf                 WOD 2023 User's Manual, 131 pages
│   ├── datasets.md                    The eleven datasets and how to choose
│   ├── cast_structure.md              The seven parts of a cast; the colliding code tables
│   ├── wod_code_tables/               12 JSON tables + standardization_rules.md
│   ├── accession_field_guide.md       How submissions arrive; the intermediate contract
│   ├── plankton_biomass.md            Plankton: why it is not a profile; the Taxa-Record path
│   └── wod_readme_db/page_text.json   Page cache for search_manual (rebuildable)
│
├── data/                              ← DATA. A run's working area.
│   ├── runs/<accession>/              run.log, report.md, decisions.toml, state.json
│   ├── raw/<run_id>/                  Downloaded files — the provenance record
│   ├── staging/manifests/             One JSON ledger per run
│   └── processed/                     Output .nc files
│
├── tests/                             Unit suites + the layout guard
├── README.md
└── pyproject.toml

not tracked:
  scripts/                             One-off per-accession staging scripts. Local working
                                       files -- the staged copy in data/staging/ carries the
                                       provenance, not the script. The reusable extractor
                                       lives in wod_etl/reference/ instead.
```

**Why instructions and code are separate** — the package is a normal root-level Python
package, owned by no agent runtime's directory convention, so `uv sync` installs it editable
and `import wod_etl` works from anywhere.

The instructions follow [Anthropic's Agent Skills
format](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview): the
procedure lives once in `.claude/skills/wod-etl/SKILL.md`, which loads on demand and links
one level deep to the per-phase docs and the knowledge base — no intermediate hops. WOD
lookup is a second skill because it has a different trigger: a question about a code or a
flag is not a request to process files. `CLAUDE.md`, `GEMINI.md` and `AGENTS.md` are
~20-line pointers so a runtime that does not read `.claude/skills/` still finds the
procedure, and there is still only one copy to maintain.

The files are plain markdown, so they work in any runtime; nothing requires Claude Code.

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

`tests/test_project_layout.py` guards the instruction layout itself: that each `SKILL.md`
carries the procedure rather than a pointer to it, that its frontmatter stays within the
Agent Skills limits, that every link in it resolves, and that no retired index reappears as
a middle hop.

### Skill evaluations

The skills are evaluated separately from the unit tests, because "does Claude do the right
thing with this instruction" is not a pytest question:

```
.claude/skills/wod-etl/evals/evals.json        10 cases + two fixture accessions
.claude/skills/wod-reference/evals/evals.json   9 cases
```

Each case is a prompt plus the behaviour expected of it — triaging a mixed accession,
staging sidecar coordinates, reporting a dropped column rather than aliasing it, routing a
plankton folder to the path it actually belongs on. Two fixture accessions:
`evals/fixtures/mini-accession/` is five folders — two CTD, one of currents, one entirely
unanalysed, and a readme; `evals/fixtures/mixed-formats/` is four formats that all lie about
themselves — NetCDF with no extension, a Sea-Bird `.cnv` named `.txt`, headerless
fixed-width ASCII, and a plankton table. Run them with the
[`skill-creator` plugin](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator),
which spawns a clean subagent per case and benchmarks with-skill against without-skill.

---

## Known limitations

Everything in the earlier defect list has been fixed and pinned by a test. What remains is
scope not yet built, not broken behaviour:

| Area | Limitation |
| --- | --- |
| Range checks | Bounds are the **union across all 30 ocean basins** at each depth. Assigning a cast to a basin needs `range_area.msk` and `range_basin_list.msk` from the WOD website, which are not in the PDF. Bounds are depth-dependent but more permissive than a basin-aware check — at 3000 m the temperature envelope reaches 34 °C because the manual's Red Sea column allows it. |
| Density checks | Depth flag `2` (density inversion, manual p.54) is defined but never assigned; seawater density is not computed. |
| Standard levels | Interpolation to standard depths (Reiniger-Ross, p.56) is not implemented. This project produces intermediate observed-level files, which is the stage before that. |
| Pressure → depth | `process_accession` fails a pressure-only cast at the `mapping` stage, naming `pressure_to_depth`, rather than converting it — the conversion needs latitude. Convert it during [staging](wod_etl/staging/README.md), and record that depth was derived. |
| `split_casts` | Heuristic: a header block after data starts a new cast. Files that separate casts some other way (a blank line, a row count, a fixed record length) will not split correctly. |
| Manual search | `search_manual` is substring matching over cached page text, not semantic search. It finds the word you type, not the concept. Build the cache with `uv run python -m wod_etl.reference.index_manual`. |
| Appendices not extracted | Ocean weather stations, WMO squares, standard depth levels (Appendix 9) and the Reiniger-Ross table (Appendix 10) are not in JSON. Reach them with `search_manual`, then open the PDF at that page. The **plankton code lists** (Appendices 4.1–4.9, 5.1–5.11, 6) and the ITIS taxonomic list are not extractable at all — the manual defers them to the WOD website. |
| WOD QC is off by default | Quality control is applied downstream, so `apply_qc_auditing` is opt-in (`run_qc=True`, or `WOD_RUN_QC=1`). The **originator's** flag columns are always carried through, verbatim and untranslated, as `<var>_qc_originator` — a submitter's `1` may mean *good* where WOD's `1` means a failed range check, and WOD's field for which set was used is secondary header 96. |
| Metadata values are not resolved to their code lists | The link is provided, per field, from the manual's own hyperlinks; nothing fetches it. They are external, they change between WOD releases, and a cached copy would go stale silently. |
| Instrument artifacts survive QC (when it is on) | The permissive basin-union range lets extreme values through. A real XCTD cast in this project ends with a 12-record linearly extrapolated tail reaching salinity 83.8; the range check caught 8 of 9 out-of-range levels, and the smooth ramp defeats the gradient check entirely. Read the value ranges from `wod_etl.verify.audit_dataset` — a physically impossible max is the signal. |
| Plankton and biomass | **Not writable, but now describable.** Table 3a has no plankton variable; WOD stores it in the Biological Header (Table 6a) plus Taxa-Records (Table 7a), where a measurement carries an upper and lower depth. Those tables, the Table 8 code ranges and the Table 9 PGC are extracted, and `classify_dataset` excludes such folders with `belongs_in_wod: True` — so they reach `skipped_reasons` rather than vanishing. Writing them needs a record model that is not a profile, plus the website code lists. [`knowledge/plankton_biomass.md`](knowledge/plankton_biomass.md). |
| Fixed-width column boundaries | Inferred from where whitespace held across the sampled rows, not declared. A column full in the sample and empty later shifts every field after it. `read_any` warns; verify against the accession's format description. |
| Unidentified files | A profile-shaped file that names no instrument is **skipped, not guessed**. Read the folder readme and pass `dataset_type`, a `dataset_overrides` entry, or `process_unidentified_as` — the last records that it was an assumption. |
| Below-detection values | `*DL`/`**ND` become missing, because Table 12 has no observed-level flag for them. Substituting the stated detection limit would fabricate a measurement, so it is not done. The loss can be large and must be reported. |
| Julian year-day | Manual p.37 defines year-day 0.00 as 00:00 on 1 January, then gives a worked example implying one more. The definition is used; the choice is written into the staged header and should be confirmed. |
| `seawater_density` | 1025 kg/m³ is an implementer's assumption, not a WOD constant — Table 3b supplies no density. It is recorded in every output's global attributes whenever a conversion used it. |
