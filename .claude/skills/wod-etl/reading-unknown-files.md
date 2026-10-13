# Reading a file nobody documented

One tabular reader does not cover an accession. Submitters write NetCDF, Sea-Bird `.cnv`,
fixed-column ASCII from 1974, an Excel workbook, a CSV named `.dat`, and files with no
extension at all. This is the procedure for the ones that do not just open.

**Work one folder at a time, and answer all seven questions before staging anything.** Each
question names the call that answers it and what a wrong answer costs. Record the answers —
they are the provenance for every value you derive.

---

## 0. Before any file: what is in this accession?

```python
from wod_etl.discovery import classify_accession
report = classify_accession("data/raw/0287423")
report["process"], report["exclude"], report["inspect"]
```

Every folder gets `process`, `exclude` or `inspect`, with the evidence. **`inspect` means the
evidence did not decide** — that folder is where this file's questions apply. Read its readme
before opening its data.

---

## 1. What is this file, actually?

```python
from wod_etl.discovery import sniff_format
fmt = sniff_format("data/raw/0287423/2-CTD/cast_01")
fmt["format_family"], fmt["reader"], fmt["confidence"], fmt["evidence"]
```

Content, not extension. `.dat` is CSV as often as it is fixed-width; NetCDF turns up with no
extension; `.txt` is sometimes a Sea-Bird header.

| It says | It means | Next |
| --- | --- | --- |
| `netcdf*`, `sea_bird_cnv`, `spreadsheet*` | A known container | `read_any` handles it — go to Q3 |
| `delimited_text` with `confidence: high` | An ordinary table | `read_any` — go to Q2 |
| `delimited_text` with `confidence: medium` | Rows disagree on field count | Usually a units row or a trailing comment. Look at the raw lines |
| `fixed_width_text` | Boundaries were **inferred**, not declared | Q2, and verify the boundaries |
| `ragged_text` | Numeric, but no consistent shape | Several blocks concatenated, or wrapped records. Open it |
| `unstructured_text` | Prose | Not data. Read it for coordinates and sentinel legends, then exclude it |
| `binary_unknown` | Nothing recognises it | Q6 |
| `archive_zip`, `gzip` | A container | Extract into `data/raw/`, list the members, start again |

Then read it:

```python
from wod_etl.discovery import read_any
casts, info = read_any(path, fmt)     # [{"cast_id", "df", "header_block", "source_note"}]
info["warnings"]                       # never skip these
```

Same shape for every format. `header_block` is `Key: value` text that the mapper parses, so a
coordinate found in a NetCDF attribute reaches the contract by the same route as a
`# Latitude:` comment.

### Three warnings to act on rather than skim

- **Fixed-width column boundaries are inferred**, not declared. They come from where
  whitespace held across the sampled rows, so a column that is full in the sample and empty
  later shifts every field after it. Check them against the accession's format description.
- **A headerless fixed-width file gets `col_0…col_N`.** Nothing downstream can map those to
  a WOD code. Name them during staging, from the header block or the format description,
  with units folded in.
- **NetCDF units are folded into the column name** (`TEMP (degree_Celsius)`) because unit
  detection reads the raw name and nothing else. Do not strip them.

---

## 2. Where does the table start, and what are the columns?

Ask, in this order:

1. **Is the first non-comment line a header, or the first level?** A headerless file read
   with a header consumes its shallowest level *and* labels every column with that level's
   values. `read_any` decides this for fixed-width by checking whether that line is all
   numeric; for delimited files it trusts the profile. Check `df.head()` against the raw
   first lines — always.
2. **Is there a units row under the header?** `Station,Pressure,Temperature` over
   `,dbar,ITS-90 deg C` is two header rows. **Unit detection reads the full column name and
   nothing else**, so a units row is invisible to it. Fold it in when staging with
   `fold_unit_row`: `Pressure (dbar)`, not `Pressure`.
3. **Is there a title row above the header?** Common in exported spreadsheets —
   `read_table(header_row=1)`.
4. **Are the columns named at all?** `col_0 … col_N` from a fixed-width read means nothing
   downstream can map them. Name them from the header block or the accession's format
   description, in the staged copy:

   ```python
   from wod_etl.staging import rename_columns, suggest_column_names
   suggest_column_names(cols, units=units_row)   # what each column resolves to
   cols, audit = rename_columns(cols, {"col_0": "Depth (m)", "col_1": "Temp (deg C)"})
   ```

   `rename_columns` refuses any rename that would change the column's WOD code, unit or
   scale, and prints the fix. Report `audit["renames"]`.
5. **Did the whole file come back as one column?** `read_table` is comma-only by default,
   so a tab- or semicolon-separated file collapses to a single column — which reads three
   phases later as "no measurements in this file", not as a delimiter problem. Pass
   `read_table(path, delimiter=None)` to sniff it.
6. **Do comment lines appear *below* the header?** They parse as rows and become phantom
   levels whose depth is the comment text.

---

## 3. Where are the coordinates and the time?

A profile needs **latitude, longitude and depth**; without any of the three the cast cannot
be written. Time is a warning, not a blocker — but a cast with no date is worth flagging.

Look in this order, and **record which one you used**:

| Where | How it looks | What to do |
| --- | --- | --- |
| Columns in the data | `Lat`, `LATITUDE`, `lat_deg` | Nothing — the mapper finds them |
| The file's own header block | `# Latitude: 34.250`, `* NMEA Latitude = 34 15.00 N` | Nothing — `read_any` normalises both |
| NetCDF attributes or per-profile variables | `LATITUDE(N_PROF)`, `geospatial_lat_min` | Nothing — `read_any` emits them into the header block |
| A **sibling station list** | `stations.csv` keyed by station number | Phase 0 staging: join it in |
| Only in a **readme or cruise report** | prose, or a PDF | Phase 0 staging. A position stated in a readme **is** a source — use it and cite it |
| Nowhere | — | The cast cannot be staged. Say so; never invent one |

Coordinate forms that are not decimal degrees: `49-46.9858N`, `49 46.9858 S`, `178-12.33E`.
`parse_coordinate` in `wod_etl.staging` handles these — see
[`knowledge/accession_field_guide.md`](../../../knowledge/accession_field_guide.md).

**The time traps.** A `Date time (UTC)` column beside a `Time zone` column is ambiguous until
you test it. `08 30` is 08:30, not two fields. A "Julian day" is day-of-year, not the Julian
calendar. Cross-check one timestamp against an independent source in the accession — an
underway GPS track, a station list, a cruise log — before trusting the column.

---

## 4. What is each column, in WOD terms?

```python
from wod_etl.mapping import query_wod_spec, generate_field_mapping
spec = query_wod_spec("CTD", list(df.columns), depth_increment_m=dz)
contract = generate_field_mapping({"columns": list(df.columns),
                                   "raw_header_block": header}, spec)
contract["unmapped_columns"], contract["warnings"]
```

Then, for every name in `unmapped_columns`, decide **which of three things it is**:

1. **A WOD variable under an instrument's name** — `T090C` is Temperature, `SBEOX0ML/L` is
   Oxygen, `PRDM` is Pressure. Add an alias to `VARIABLE_ALIASES` in
   `wod_etl/mapping/query_wod_spec.py` when the name will recur; rename the **staged** copy
   with `rename_columns` when it is this one file. Never edit `data/raw/`.
2. **A variable WOD does not store** — conductivity, sigma-t, sound velocity, nitrite alone,
   ammonium, CDOM, turbidity, potential temperature. There is no Table 3a code.
   `classify_dataset` lists these in `unstorable_columns`. **Drop it and report the drop.**
   Mapping it onto an adjacent code is worse than dropping it: aliasing potential temperature
   to code `1` makes a cast claim Temperature twice.
3. **Not a measurement** — a station id, a flag column, a comment, an originator's QC code.
   Leave it unmapped, and check whether the flag column is worth carrying as
   secondary header 96 (Originator's Flags).

Ask `wod-reference` when you are unsure whether WOD stores something. Do not decide from
memory.

---

## 5. What are the units, really?

The single most expensive question in this pipeline, because a wrong unit still validates.

- **Unit detection reads the raw column name and nothing else.** If the unit is in a separate
  row, a readme, or a NetCDF `units` attribute that `read_any` did not fold in, it is
  invisible. Put it in the name during staging.
- **Check the conversion direction against Table 3a.** Alkalinity and DIC are stored
  *per volume*; nutrients are stored *per mass*. "Already µmol/kg" is correct for one and a
  1000× error for the other.
- **Depth versus pressure.** Depth is the axis and has no variable code. Pressure is code 25.
  A file with pressure and no depth needs a conversion that requires latitude — it is not
  automatic. `pressure_to_depth` in `wod_etl.staging`.
- **Open one output and read the value ranges** before believing any of this.
  `audit_dataset` in `wod_etl.verify` does it per dataset.

---

## 6. Is there any data in it?

A file of `Not analyzed` parses perfectly and writes a valid, empty profile.

```python
report["folders"]["4-nutrients"]["files"][0]["empty_columns"]
report["folders"]["4-nutrients"]["files"][0]["measurement_density"]
```

`measurement_density` ignores station, depth and time columns on purpose — a nutrient file
whose station and depth are full and whose three nutrient columns are entirely `Not analyzed`
scores `0.0`, which is the answer. `process_accession` skips those with a stated reason.

**The sample is only the first few KB.** Count real values over the whole file before
excluding anything.

Sentinels beyond the obvious: `**NS` not sampled, `***NA` not analysed, `*DL` below detection
limit, `**ND` not detected. `clean_sentinels` counts them. **Below-detection is a real
measurement** — WOD has no flag for it, so it becomes missing, and that loss must appear in
your report.

---

## When the file still will not open

- **`binary_unknown`.** Read the folder's readme and any format description in the accession.
  Ask what wrote it. Do not guess a layout: a wrong record length produces numbers, and they
  look like data.
- **A format the accession documents but nothing here reads.** Write a one-off reader in
  `scripts/` — a local scratch directory, gitignored, one file per accession — emit a staged
  CSV into `data/staging/`, and point the pipeline at that. The staged copy is what carries
  the provenance, not the script, so record every derived value in its header;
  `data/raw/` is never edited.
- **A layout you have now seen twice.** That is a reader, not a script. Add it to
  `wod_etl/discovery/read_any.py` and a signature to `sniff_format.py`.

---

## The record to keep

One row per folder, in your report. This is what makes a run checkable rather than merely
finished.

| Folder | Format | Dataset | Coordinates from | Units from | Columns dropped | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `1-CTD` | Sea-Bird `.cnv` | CTD | file header (`NMEA Latitude`) | column names | `Conductivity` (no Table 3a code) | 48 casts written |
| `3-bottle` | CSV + sidecar | OSD | `stations.csv`, joined on station | units row, folded in during staging | none | 12 casts written |
| `5-plankton` | CSV matrix | — (Taxa-Records) | station list | readme: number per sample | none | 24 hauls via `process_taxa`; volume filtered and preservative **not stated** -- asked |

Every derived or assumed value — a depth converted from pressure, a depth of 0 applied by
convention, a cast identified by matching two files — goes in the staged header **and** in
the report. Each is a judgement, not a measurement.
