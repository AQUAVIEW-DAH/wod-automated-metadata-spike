# Phase 2 — Mapping

Decide what each raw column is in WOD terms, and produce the mapping contract that every later
phase reads.

```python
from wod_etl.mapping import query_wod_spec, generate_field_mapping
```

The WOD tables load from [`knowledge/wod_code_tables/`](../../knowledge/wod_code_tables/),
extracted from `knowledge/wod_readme.pdf` with page citations.

---

## `query_wod_spec(dataset_type, variable_query=None, depth_increment_m=None)`

Resolves the dataset and returns the WOD definition of each variable.

```python
spec = query_wod_spec("XCTD", list(df.columns), depth_increment_m=0.5)
```

```json
{"dataset_type": "XCTD",
 "wod_dataset_code": "CTD",
 "dataset_resolution": {"wod_dataset_code": "CTD", "resolved_by": "depth_increment",
                        "note": "XCTD with depth increment 0.50 m -> CTD"},
 "required_coordinates": ["latitude", "longitude", "depth", "time"],
 "standard_variables": [{"wod_code": 1, "wod_name": "Temperature",
                         "standard_unit": "Degrees Celsius", "unit_abbrev": "°C",
                         "datasets": ["OSD", "CTD", ...], "per_volume": false,
                         "valid_for_dataset": true, "aliases": [...]}],
 "unmatched_variable_queries": ["Cond"],
 "secondary_header_rules": {"1": "Accession Number", ...}}
```

Omit `variable_query` to get all 29 Table 3a variables.

### Dataset resolution

WOD defines eleven datasets: OSD, CTD, MBT, XBT, SUR, APB, MRB, PFL, DRB, UOR, GLD. XCTD is
not one of them. XCTD and CTD are routed by vertical resolution (manual p.19):

| Depth increment | Dataset |
| --- | --- |
| `< 2.0 m` | `CTD` (high-resolution) |
| `>= 2.0 m` | `OSD` (low-resolution) |

So `dataset_type="XCTD"` can correctly resolve to `OSD`.

| `resolved_by` | Meaning |
| --- | --- |
| `exact_match` | The label is already a WOD dataset name. |
| `depth_increment` | Routed by resolution; `note` gives the reasoning. |
| `unresolved` | XCTD/STD with `depth_increment_m=None`. The code is `null`; supply the increment. |
| `unknown` | Not a WOD dataset. `note` lists the valid names. |

`valid_for_dataset` is false for a variable Table 3a does not store in the resolved dataset
(phosphate in CTD, salinity in MBT). `generate_field_mapping` leaves it out and lists it in
`dropped_columns` (`kind = "not_in_dataset"`, with the datasets that do store it). If it
belongs in WOD, write the file as a dataset that stores it.

Header parsing also reads:

- a cast date labelled `Time:` (`Time: 2009. 8.18`) and seconds in a time of day;
- a `Depth:` on the position line as bottom depth (secondary header 10; a negative value is
  a missing-value code);
- a `Source:` line as a source file only when its value is a path;
- ACDD's `creator_name` as principal investigator.

A `key: value` value ends at the next `Key:`, `;` or `|`, not at a comma.

### Helpers (importable from `wod_etl.mapping.query_wod_spec`)

| Function | Use |
| --- | --- |
| `match_variable(name)` | Column name → WOD code, or `None`. Returns `0` for a depth column. |
| `explain_column(name)` | Why a column has no code, for columns that must not be aliased. `None` means "not recognised" (may need an alias). |
| `normalize_name(name)` | `"Temp (deg C)"` → `"temp"`. What matching operates on. |
| `extract_unit(name)` | `"Temp (deg C)"` → `"deg C"`. |
| `extract_scale(name)` | `"t090C: Temperature [ITS-90, deg C]"` → `"ITS-90"`. A scale is not a unit. |
| `resolve_dataset(label, depth_increment_m)` | Dataset routing on its own. |
| `wod_variables()` / `wod_datasets()` / `wod_secondary_headers()` | Raw tables. |

### Names that look like variables and are not

`NOT_STORED` in [`query_wod_spec.py`](query_wod_spec.py) is the single registry of columns
WOD has no code for. `match_variable` uses it to refuse a match and `explain_column` uses it
to say why.

```python
match_variable("Potential Temperature (deg C)")   # -> None
explain_column("Potential Temperature (deg C)")
# {"variable": "potential_temperature", "reason": ..., "route": ..., "do_not_alias": True}
```

Potential temperature is derived, not measured. Mapping it to code 1 would report the wrong
quantity, or claim Temperature twice. If it is all you have, convert it back to in-situ
during [staging](../staging/README.md).

The registry is checked before the alias fallback (whose longest-match rule would otherwise
match `temperature` inside `potential_temperature`). Exact matches are tried first, so
`nitrate_nitrite` still resolves to code 8.

### Fluorescence is decided by its unit

WOD has no fluorescence variable; it has Chlorophyll (code 11, µg/l).
`fluorescence_rule` in [`query_wod_spec.py`](query_wod_spec.py) decides from the unit:

| Column | Result |
| --- | --- |
| `Fluorescence (ug/l)`, `Fluor [ppb]`, `fluorescence (mg/m^3)` | code 11, with a `comment`: fluorometer-derived, not extracted chlorophyll-a |
| `FlSP`, `flECO-AFL` (Sea-Bird calibrated channels), no unit | code 11, same comment |
| `Fluorescence (RFU)`, `chl_fluorescence (V)`, `Fluorometer (volts)` | no code; `explain_column` says it is a raw signal; dropped and reported |
| `fluorescence`, no unit | no code; find the unit and put it in the staged column name |

Stage the column as `Fluorescence (<unit>)`; do not rename it to `Chlorophyll`.

### Columns with "depth" in the name that are not the depth axis

`Bottom Depth`, `Water Depth`, `Sounding`, `Station Depth`, `Max Depth`, `Sensor Depth` and
`Depth to bottom` are blocked from the vertical axis by `NOT_THE_DEPTH_AXIS`. `map_metadata`
claims bottom depth as secondary header 10.

Depth is not a WOD variable (code 25 is Pressure). Internally a depth column carries the
sentinel code `0` (`DEPTH_SENTINEL_CODE`): the vertical axis, in metres.

---

## `resolve_column(query, columns)`

Finds which column a plain name refers to, so the caller need not know it is spelt `LAT`.

```python
resolve_column("latitude", ["LAT", "LON", "Depth (m)"])
# {"column": "LAT", "wod_code": 30, "matched_by": "wod_code",
#  "confidence": "high", "candidates": ["LAT"], "ambiguous": False, "note": None}
```

Four tiers, tried in order; `matched_by` names the one that matched:

| Tier | Test | Catches |
| --- | --- | --- |
| `exact` | string equality | the caller already knew the name |
| `normalized` | `normalize_name` equality | `latitude` → `Latitude (degree N)` |
| `wod_code` | both resolve to the same WOD code | `latitude` → `LAT`, `oxygen` → `Sbeox0ML/L` |
| `substring` | normalised substring, `confidence: "low"` | last resort |

Two or more hits at the winning tier return `column: None`, `ambiguous: True` and the
candidates. For example, Sea-Bird `t090C` and `t190C` (primary and secondary sensors) both
resolve to code 1.

A query that lands on a column WOD does not store is refused with `explain_column`'s reason.

To rename what it finds, use [`rename_columns`](../staging/README.md) in Phase 0.

---

## `map_metadata(header_text, columns, df=None, dataset_code=None)`

Maps everything in a file that is not a measurement value (bottom depth, probe type, ship,
wind, gear, scale, originator units and flags) to its WOD field.

```python
from wod_etl.mapping import map_metadata
meta = map_metadata(header_text, list(df.columns), df)
```

| Where it lives | Example | Becomes |
| --- | --- | --- |
| A `Key: value` header line | `# Probe type: T-7` | secondary header 29 |
| A cast-level Table 5a line | `# Instrument: SBE 911plus` | variable specific header 5 on every measured variable |
| A cruise or PI line | `# Cruise: SHU1505` | `character_data` (manual p.30), not the platform |
| A delimited header line | `Platform,,5423.,OCL code,KAHAROA` | secondary header 3 |
| A whole column, constant down the cast | a `Bottom Depth (m)` column | secondary header 10 |
| Part of a measurement column's name | `[ITS-90, deg C]` | variable specific headers 3 and 8 |

### The delimited header block

WOD's CSV export (and some originators) write the header block as columns:

```
Platform     ,,           5423.,OCL code,KAHAROA (R/V; call sign ZM7552; built 1981),
Bottom depth ,,              36,meters,,
Instrument   ,Temperature,  464.,WOD code,CTD: SBE 37-IM MicroCAT,
uncalibrated ,Salinity,       1.,,,
```

- Column 0 is the key, column 2 the value, and the rest a unit or a code system plus a
  description.
- Column 1 is the variable the key applies to: empty means the cast (Table 4a); a name means
  that one variable (Table 5a).
- The value is always column 2. `Accession Number ,, ,NODC code,,` has no value.
- A value under `WOD code` is already a code; `value_is_already_a_code` says so.
- When two keys match one header (`Originators Cruise ID` and `Platform` for header 3), the
  coded value wins and the other goes to `unresolved`, naming the winner.

### `unresolved`

Every key that looked like metadata but could not be placed is listed with its value, source
line and reason:

```json
[{"key": "Country", "value": "NZ", "matches": null,
  "note": "no WOD field matched this key. Either it has no WOD home -- report it as not
           carried -- or the pattern is missing from SECONDARY_HEADER_PATTERNS."}]
```

Read it. Unmatched `Key: value` lines and a second key for an already-held header both land
here, and reach the NetCDF as `metadata_not_placed`.

A known key followed by a bare space counts only when the value has no separator of its own:
`Cast duration: 30` is header 11, not cast number `duration: 30`.

```json
{"secondary_headers": {
   "29": {"name": "Probe Type", "value": "T-7", "source": "header block",
          "is_coded": true,
          "code_table": {"csv": "https://.../CODES/CSV/s_29_probe_type.csv"}},
   "10": {"name": "Bottom Depth (meters)", "value": 2500.0,
          "source": "column 'Bottom Depth (m)'", "is_coded": false}},
 "variable_specific_headers": {
   "t090C: Temperature [ITS-90, deg C]": {
     "wod_code": 1, "headers": {"8": {"name": "Originator's units", "value": "deg C"},
                                "3": {"name": "Scale", "value": "ITS-90"}}}},
 "metadata_columns": [...], "originator_flags": [...], "unresolved": []}
```

- A metadata column that varies down the cast records no value (a secondary header describes
  the whole cast); the reason is stated.
- A header-block value beats a column value.
- Every coded field carries its published code list, from
  `knowledge/wod_code_tables/code_table_links.json` (see
  [`wod_etl/reference/README.md`](../reference/README.md)). `is_coded: false` means the field
  is a plain value (bottom depth is metres).

**Code lookup.** Each coded value is looked up in its list (`wod_etl.reference.code_lists`);
`code_lookup` records the result:

| `code_lookup.status` | `value` becomes | the writer writes |
| --- | --- | --- |
| `matched` (exactly one entry) | the code; the text moves to `originator_value` | the code, with `_originator_text` and `_code_lookup` beside it |
| `already_a_code` (a number) | unchanged | the code |
| `ambiguous`, `no_match`, `unavailable`, `disabled` | unchanged (the text) | no code; the text as `<field>_originator_text`, the reason as `<field>_code_lookup` |

The run reports every unresolved one under `coded_headers_not_resolved`. Originator's units
(header 8) are coded too: `deg C` has no entry, and `ug/l` has two (036 and 136).

### Originator quality flags

A column named `Temperature_QC`, `QC_Temperature`, `TEMP_QF` or `Salinity quality flag` is
paired with the variable it flags, by name or, failing that, by resolved WOD code.

- Flags are kept verbatim and never translated: an originator's `1` may mean good where WOD
  Table 12's `1` means a failed range check.
- They are written to `<var>_qc_originator`, with no `flag_values`/`flag_meanings` and a
  pointer to the code table of flag sets.
- The flag set used belongs in secondary header 96; when the file does not name it, it is
  recorded as unknown.

Flag columns and metadata columns are consumed, not dropped; neither appears in
`unmapped_columns`.

---

## `generate_field_mapping(profile_result, spec_result, df=None)`

Builds the contract. `df` is optional and used only to read metadata column values.

```python
contract = generate_field_mapping({**profile, "columns": list(df.columns)}, spec, df=df)
```

```json
{"source": ".../xctd_01.csv",
 "dataset_type": "XCTD", "wod_dataset_code": "CTD",
 "column_mapping": [
   {"raw_column": "Depth (ft)", "wod_code": 0, "wod_name": "Depth",
    "role": "depth_axis", "target_unit": "meters", "conversion_needed": "feet_to_meters"},
   {"raw_column": "Temp (deg F)", "wod_code": 1, "wod_name": "Temperature",
    "role": "measurement", "target_unit": "Degrees Celsius",
    "conversion_needed": "fahrenheit_to_celsius",
    "originator_unit": "deg F", "originator_scale": null,
    "variable_specific_headers": {"8": {"name": "Originator's units", "value": "deg F"}},
    "originator_flag_column": "Temp_QC"}],
 "secondary_headers": {"1": {...}, "3": {...}, "10": {...}, "29": {...}},
 "metadata_columns": [{"column": "Bottom Depth (m)", "secondary_header": 10, ...}],
 "originator_flags": [{"flag_column": "Temp_QC", "flags_column": "Temp (deg F)",
                       "flags_wod_code": 1, "scope": "variable"}],
 "extracted_header_metadata": {"1": {"name": "Accession Number", "value": "0287423"}},
 "parsed_coordinates": {"latitude": 61.93, "longitude": -172.27,
                        "year": 1985, "month": 6, "day": 12},
 "missing_required_coordinates": [],
 "unmapped_columns": ["Cond"],
 "dropped_columns": [{"column": "Cond", "variable": "conductivity",
                      "reason": "...", "route": "...", "do_not_alias": true}],
 "warnings": [],
 "ready_for_transform": true}
```

Read `secondary_headers`, which covers the header block and metadata columns with their code
lists and lookups. `extracted_header_metadata` is a narrower legacy field kept for
compatibility.

The contract also carries `character_data` (cruise code, PI), `provenance_notes` (every
`# Note:` line), `source_files` (every `# Source:` line), `unresolved_metadata`, `source`
(the path read) and `source_format` (how it was read). The writer turns notes and sources
into `comment` and `source_files_raw`. A fluorometer column mapped to code 11 carries a
`comment` and a warning.

### Fields to check before trusting a run

| Field | What it means |
| --- | --- |
| `unmapped_columns` | Not written to the output. An expected column here means a missing alias, unless it is also in `dropped_columns` (refused on purpose). |
| `dropped_columns` | Columns WOD has no code for, each with the reason and what to do instead. Do not alias these. |
| `missing_required_coordinates` | Any of `latitude`, `longitude`, `time`, `depth`. The file will fail Phase 4 validation. |
| `ready_for_transform` | `false` when the mapping is empty or coordinates are missing. |
| `warnings` | Unresolved dataset, two columns claiming one WOD code, a variable not stored in this dataset. |

### Mapping a column in `unmapped_columns`

First check whether WOD stores the variable. Check `dropped_columns` before adding an alias;
`explain_column` says why a column was refused. These should stay unmapped:

| Column | Why it has no code |
| --- | --- |
| conductivity, sigma-t/sigma-theta, sound velocity | not in Table 3a |
| potential temperature | derived, not measured; convert to in-situ during staging if it is all you have |
| nitrite | excluded from the online database (manual p.49); code 8 is nitrate and nitrate+nitrite, a different measurement |
| ammonium, turbidity | not in Table 3a |
| CDOM absorption (m⁻¹) | not code 24; transmissivity/beam attenuation is a different measurement |
| size-fractionated chlorophyll | one chlorophyll code exists, so only a total maps |
| bottom depth, water depth, sounding | secondary header 10, claimed by `map_metadata` |

For a real WOD variable, add an alias to `VARIABLE_ALIASES` in
[`query_wod_spec.py`](query_wod_spec.py), or rename the column in the staged copy with
[`rename_columns`](../staging/README.md). Never edit `data/raw/`.

| | Alias | Rename |
| --- | --- | --- |
| Where | `VARIABLE_ALIASES` in `wod_etl/mapping/query_wod_spec.py` | `rename_columns` in `wod_etl.staging`, on the staged copy |
| Scope | every accession | this one file |
| Touches data | no | the staged header only |
| Use when | the vendor's name will recur (`T090C`, `PrDM`) | this file's header is wrong, missing, or missing its unit |

Sea-Bird names (`T090C`, `Sal00`, `PrDM`, `Sbeox0ML/L`, `FlSP`, `DepSM`; trailing digits are
the sensor index) are already aliased; other vendors' are not. An unmapped depth column
(e.g. `DepSM`) yields a file with no depth axis.

Matching normalises first: bracketed unit text is stripped, everything after a comma or
semicolon is dropped, and the rest is lowercased to underscore-separated words, so `Temp (C)`,
`TEMP_C` and `Temperature [deg C]` all reach `temp`/`temperature`. An exact alias wins;
otherwise the longest whole-word alias match wins, so `Air_Temperature` resolves to code 26,
not `temp`.

### Unit conversions are inferred from the column name

`conversion_needed` is detected against the full raw column name:

| Detected in name | `conversion_needed` | Applies to |
| --- | --- | --- |
| `deg F`, `fahrenheit` | `fahrenheit_to_celsius` | codes 1, 26 |
| `ft`, `feet`, `foot` | `feet_to_meters` | depth axis only |
| `fathom`, `fm` | `fathoms_to_meters` | depth axis only |
| `ml/l` | `ml_per_l_to_umol_per_kg` | code 3 (oxygen) only |
| `mg/l` | `mg_per_l_to_umol_per_kg` | code 3 (oxygen) only |
| any molar unit | `molar_rescale` | see below |

**Molar units.** `molar_rescale` carries `conversion_from` and `conversion_to` (`"umol/kg"`,
`"mmol/l"`) in the mapping entry, and Phase 3 computes the factor: an SI-prefix rescale, plus
a density step when the basis (per-mass vs per-volume) changes. The target is the variable's
Table 3a standard unit:

| codes | Table 3a standard | so `µmol/kg` is | and `mmol/l` is |
| --- | --- | --- | --- |
| 3, 4, 6, 8 — oxygen, nutrients | µmol/kg | already standard | × 1000 ÷ density |
| 17, 21 — alkalinity, DIC | mmol/l | × density ÷ 1000 | already standard |
| 34, 38, 39 — He, Ar, Ne | nmol/kg | × 1000 | — |
| 11 — chlorophyll | µg/l (not molar) | left alone | left alone |

Chlorophyll (11) has a per-volume standard with no molar equivalent, so a molar-to-mass
conversion is refused for it.

**Units must be in the column name.** A units row below the header or units stated only in a
readme are not seen; fold them into the name during [staging](../staging/README.md). Two
units need no conversion: `mg m⁻³` for chlorophyll (equals µg/l) and `hPa` for air pressure
(equals mbar).

### Header parsing

`parsed_coordinates` accepts decimal (`LAT: 61.930`), integer (`LAT: 61`), degrees + decimal
minutes (`LAT: 61 55.8 N`) and hemisphere suffixes (`33.5 S` → `-33.5`). Dates are read as
`YYYY-MM-DD`, `YYYYMMDD` or `DD-MM-YYYY`, plus `HH:MM` time, and `TIME_ZONE:` as
`time_zone`. A date with no time of day is a warning: it is written date-only (`GMT_time`
fill), not as midnight. `parse_header_block(text)` is the same reader for a header with no
table.

`secondary_headers` is keyed by Table 4a secondary header ID, a different namespace from
Table 3a variable codes (header 29 is Probe Type; variable 29 is Air pressure). Only numeric
headers (`NUMERIC_SECONDARY_HEADERS` in [`map_metadata.py`](map_metadata.py)) are coerced to
float; accession, station and WMO codes stay strings, so `0287423` keeps its leading zero.

`SECONDARY_HEADER_PATTERNS` covers most Table 4a fields (sea state, cloud cover, Secchi
depth, probe type, recorder, water sampler, WMO id, ARGOS fields…). Add a pattern there
rather than parsing a key by hand.

---

## `map_haul(staged)` and `resolve_taxon(name)` — biological data

`map_haul` maps a haul staged with `wod_etl.staging.write_staged_haul` (as returned by
`read_staged_haul`). It produces the same kind of contract as a cast (secondary headers with
code lookups, character data, coordinates, notes, sources), plus the Biological Header with
each coded field looked up (gear `b_7`, tow type `b_4`…) and one record per row with its
taxon, qualifiers and values. `wod_etl.writers.write_taxa_netcdf` writes it.

`resolve_taxon` looks a name up in WOD's `t_1_taxa_list.csv` (about 5000 taxa, with ITIS
numbers and Plankton Grouping Codes) using the standard code-list lookup:

| Written | Result |
| --- | --- |
| `Acartia danae` | `matched`, 86086, PGC 4212010 |
| `Acartia spp.` | `matched` at genus as written (86084); `note` says so |
| `Thunnus obesus` (not in the list) | `matched_higher_rank`: genus *Thunnus*; the species is lost and `note` says so |
| `Mysis of Decapoda` | `matched_higher_rank`: Decapoda (a later capitalised word is the taxon) |
| `Fish larva` | `ambiguous`: "Fish" names more than one WOD group |
| an unknown name, or the list unavailable | no code; the originator's name is kept |

`resolve_taxa_coded(field_id, value)` does the same for a Table 7a qualifier: life stage
(`adult` → 8, `larvae` → 6 LARVA, `copepodite` → 29), sex. The originator's text is always
kept; a code is written only for exactly one match. ITIS itself is not queried.

---

## Next

[Phase 3 — Conversions and QC](../qc_transforms/README.md) consumes the contract.
For a file that would not parse, see [Phase 0 — Staging](../staging/README.md).
