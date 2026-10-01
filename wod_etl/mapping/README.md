# Phase 2 — Mapping

**Decide what each raw column is in WOD terms, and produce the mapping contract.**

This is where the pipeline stops thinking in "columns of a text file" and starts thinking in
WOD variable codes, standard units and dataset names. Everything downstream reads the
contract this phase produces.

```python
from wod_etl.mapping import query_wod_spec, generate_field_mapping
```

Nothing here is hardcoded from memory: the WOD tables load from
[`knowledge/wod_code_tables/`](../../knowledge/wod_code_tables/), which was extracted from
`knowledge/wod_readme.pdf` with page citations.

---

## `query_wod_spec(dataset_type, variable_query=None, depth_increment_m=None)`

The standards lookup. Resolves the dataset and returns the WOD definition of each variable.

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

### Dataset resolution is not a lookup

`XCTD` **is not a WOD dataset.** The manual defines exactly eleven: OSD, CTD, MBT, XBT, SUR,
APB, MRB, PFL, DRB, UOR, GLD. XCTD and CTD are routed by **resolution, not instrument**
(manual p.19):

| Depth increment | Dataset |
| --- | --- |
| `< 2.0 m` | `CTD` (high-resolution) |
| `>= 2.0 m` | `OSD` (low-resolution) |

So `dataset_type="XCTD"` is a correct thing to pass, and getting `wod_dataset_code: "OSD"`
back is a correct answer, not a bug.

`resolved_by` tells you how the answer was reached:

| Value | Meaning |
| --- | --- |
| `exact_match` | The label is already a WOD dataset name. |
| `depth_increment` | Routed by resolution. Read `note` for the reasoning. |
| `unresolved` | XCTD/STD with `depth_increment_m=None`. **The code is `null`** — supply the increment rather than guessing. |
| `unknown` | Not a WOD dataset at all. `note` lists the valid names. |

`valid_for_dataset` flags a variable that Table 3a does not store in the resolved dataset —
e.g. phosphate in a CTD file. It becomes a contract warning, not an error.

### Helpers (importable from `skills.mapping.query_wod_spec`)

| Function | Use |
| --- | --- |
| `match_variable(name)` | Column name → WOD code, or `None`. Returns `0` for a depth column. |
| `explain_column(name)` | **Why** a column has no code, for the ones that must not be aliased. `None` means "not recognised" — that one may need an alias. |
| `normalize_name(name)` | `"Temp (deg C)"` → `"temp"`. What matching operates on. |
| `extract_unit(name)` | `"Temp (deg C)"` → `"deg C"`. What normalisation throws away, kept. |
| `extract_scale(name)` | `"t090C: Temperature [ITS-90, deg C]"` → `"ITS-90"`. A scale is not a unit. |
| `resolve_dataset(label, depth_increment_m)` | Dataset routing on its own. |
| `wod_variables()` / `wod_datasets()` / `wod_secondary_headers()` | Raw tables. |

### Names that look like variables and are not

`NOT_STORED` in [`query_wod_spec.py`](query_wod_spec.py) is the **single registry** of
columns WOD has no code for. `match_variable` consults it to *refuse* a match, and
`explain_column` reads the same registry to say why — so the refusal and its explanation
cannot drift apart. Nothing else in the project keeps a second copy.

```python
match_variable("Potential Temperature (deg C)")   # -> None
explain_column("Potential Temperature (deg C)")
# {"variable": "potential_temperature", "reason": ..., "route": ..., "do_not_alias": True}
```

**Potential temperature is the one that bites.** It is derived — in-situ temperature
adjusted for adiabatic compression — and it used to be an alias of code 1. With both columns
present the cast claimed Temperature twice; with only theta present the cast silently
reported the wrong quantity. It can be converted back to in-situ if pressure and salinity
are known, but that is a computation done during [staging](../staging/README.md), not a
rename.

The registry is checked **before** the alias fallback, because the fallback is what captured
these: `potential_temperature` contains `temperature`, and the longest-match rule duly
returned code 1. Exact matches are tried in both directions first, so `nitrate_nitrite`
still resolves to code 8 rather than being blocked as nitrite.

### Columns with "depth" in the name that are not the depth axis

`Bottom Depth`, `Water Depth`, `Sounding`, `Station Depth`, `Max Depth`, `Sensor Depth`,
`Depth to bottom` — all matched `depth` inside their own name and were handed to the
vertical axis, which set every level of the cast to the same value. `NOT_THE_DEPTH_AXIS`
blocks them. They are not lost: `map_metadata` claims bottom depth as **secondary header
10**.

**Depth is not a WOD variable.** There is no code for it — code 25 is *Pressure*, a distinct
measurement. Internally a depth column carries the sentinel code `0`
(`DEPTH_SENTINEL_CODE`), meaning "this is the vertical axis, in metres".

---

## `resolve_column(query, columns)`

**Which of these columns does a human's name for one of them refer to?** "Rename the
latitude column" does not say the column is spelt `LAT`, and the caller should not have to
know.

```python
resolve_column("latitude", ["LAT", "LON", "Depth (m)"])
# {"column": "LAT", "wod_code": 30, "matched_by": "wod_code",
#  "confidence": "high", "candidates": ["LAT"], "ambiguous": False, "note": None}
```

Four tiers, tried in order, and the one that fired is named in `matched_by`:

| Tier | Test | Catches |
| --- | --- | --- |
| `exact` | string equality | the caller already knew the name |
| `normalized` | `normalize_name` equality | `latitude` → `Latitude (degree N)` |
| `wod_code` | both resolve to the same WOD code | `latitude` → `LAT`, `oxygen` → `Sbeox0ML/L` |
| `substring` | normalised substring, `confidence: "low"` | last resort, flagged as such |

**It refuses to choose between two candidates.** Two or more hits at the winning tier gives
`column: None`, `ambiguous: True` and the candidates listed. That is the real Sea-Bird case:
`t090C` and `t190C` are the primary and secondary temperature sensor and **both resolve to
code 1**, so "the temperature column" genuinely does not identify one of them. Picking one
would drop a real measurement.

A query landing on a column WOD does not store is refused with `explain_column`'s reason, so
asking for "the temperature column" cannot quietly return a *potential* temperature.

Renaming what it finds is Phase 0's job — [`rename_columns`](../staging/README.md).

---

## `map_metadata(header_text, columns, df=None, dataset_code=None)`

**Everything in a file that is not a measurement value.** `generate_field_mapping` decides
what each measurement column is; this decides what everything else is — and without it a
file's bottom depth, probe type, ship, wind speed, sampling gear, measurement scale,
originator units and originator flags all resolve to no Table 3a code and are dropped. WOD
has a field for every one of them.

```python
from wod_etl.mapping import map_metadata
meta = map_metadata(header_text, list(df.columns), df)
```

Metadata arrives four ways and all four are handled:

| Where it lives | Example | Becomes |
| --- | --- | --- |
| A `Key: value` header line | `# Probe type: T-7` | secondary header 29 |
| A **delimited** header line | `Platform,,5423.,OCL code,KAHAROA` | secondary header 3 |
| A whole column, constant down the cast | a `Bottom Depth (m)` column | secondary header 10 |
| Part of a measurement column's own name | `[ITS-90, deg C]` | variable specific headers 3 and 8 |

### The delimited header block

WOD's own CSV export, and several originators copying it, write the block as columns rather
than as `Key: value`:

```
Platform     ,,           5423.,OCL code,KAHAROA (R/V; call sign ZM7552; built 1981),
Bottom depth ,,              36,meters,,
Instrument   ,Temperature,  464.,WOD code,CTD: SBE 37-IM MicroCAT,
uncalibrated ,Salinity,       1.,,,
```

Column 0 is the key, column 2 the value, and the rest a unit **or** a code system plus a
description. **Column 1 is the variable the key applies to** — empty means the key describes
the CAST (Table 4a), named means it describes ONE VARIABLE (Table 5a) and says which, so a
per-variable header can be attached rather than guessed at.

Three things this gets right that a `Key: value` read does not:

- **The value is column 2, not "the next non-empty column".** `Accession Number ,, ,NODC
  code,,` has no accession number in it; scanning forward reports one reading "NODC code".
- **A code system is recorded as one.** `464.` under `WOD code` is already a published code:
  `value_is_already_a_code` says so, which stops a reader treating it as a free-text probe
  name or looking up a code that is the answer already.
- **A collision keeps both.** `Originators Cruise ID` and `Platform` both match header 3;
  the coded value wins and the loser goes to `unresolved` naming the winner and why.

### `unresolved` — nothing goes missing quietly

Every key that looked like metadata and could not be placed is listed, with its value, the
line it came from, and why:

```json
[{"key": "Country", "value": "NZ", "matches": null,
  "note": "no WOD field matched this key. Either it has no WOD home -- report it as not
           carried -- or the pattern is missing from SECONDARY_HEADER_PATTERNS."}]
```

**Read it.** A metadata key that vanishes silently is the same failure as a dropped column,
and it was silent until this existed: on accession 0146739 the old parser reported three
headers, all three wrong, and named none of the fourteen keys it had skipped.

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

**A varying metadata column records no value.** A secondary header describes the whole cast,
so a `Bottom Depth` that changes down an underway track is withheld with the reason stated,
rather than collapsed to one number — that would be a fabrication.

**A header-block value beats a column value.** The first was stated about the cast; the
second is inferred from the data.

**Every coded field carries its published code list.** The manual defines the metadata
*fields* and publishes the *value* code lists online, as a link beside each entry — links
that are PDF annotations and do not survive a text extraction, which is why the "App" column
reads like a dead end. They are extracted into
`knowledge/wod_code_tables/code_table_links.json`; see
[`wod_etl/reference/README.md`](../reference/README.md). `is_coded: false` is a real answer,
not a gap: bottom depth is metres, not a code.

### Originator quality flags

A column named `Temperature_QC`, `QC_Temperature`, `TEMP_QF` or `Salinity quality flag` is
paired with the variable it flags — by name, and failing that by resolved WOD code, because
the flag column says `Temperature` while the measurement column may say
`t090C: Temperature [ITS-90, deg C]`.

**They are kept verbatim and never translated.** A submitter's `1` may mean *good* where WOD
Table 12's `1` means a failed range check, so mapping one onto the other invents meaning.
They are written to their own NetCDF variable, `<var>_qc_originator`, with no
`flag_values`/`flag_meanings` and a pointer to the code table that names flag *sets*. WOD's
own field for "which flag set was used" is secondary header 96; when the file does not name
it, that is recorded as unknown rather than guessed.

Flag columns and metadata columns are **consumed**, not dropped — neither appears in
`unmapped_columns`.

---

## `generate_field_mapping(profile_result, spec_result, df=None)`

Builds the contract. `df` is optional and used only to read a metadata column's *value*.

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

`extracted_header_metadata` is the narrower set the original parser produced and is kept so
anything reading that key still works. **`secondary_headers` is the one to read** — it
covers the header block *and* metadata columns, each with its code list.

### Read these four fields before trusting a run

| Field | What it means |
| --- | --- |
| `unmapped_columns` | **Silently dropped from the output file.** A column you expected here means a missing alias — unless it is also in `dropped_columns`, which means it was refused on purpose. |
| `dropped_columns` | Columns WOD has no code for, each with the reason and what to do instead. These must **not** be aliased. |
| `missing_required_coordinates` | The file will fail Phase 4 validation. Lists any of `latitude`, `longitude`, `time`, `depth`. |
| `ready_for_transform` | `false` = non-empty mapping is missing or coordinates are absent. Do not proceed blind. |
| `warnings` | Unresolved dataset, two columns claiming one WOD code, a variable not stored in this dataset. |

### To map a column that landed in `unmapped_columns`

First ask whether WOD stores the variable at all. **Check `dropped_columns` before reaching
for an alias** — anything listed there was refused deliberately, and `explain_column` says
why. Some columns *should* stay unmapped, and aliasing them to an adjacent code is worse
than dropping them:

| Column | Why it has no code |
| --- | --- |
| conductivity, sigma-t/sigma-theta, sound velocity | not in Table 3a |
| potential temperature | derived, not measured. Mapping it to code 1 makes a cast claim Temperature twice — or report theta as in-situ. Convert it during staging if it is all you have |
| nitrite | excluded from the online database (manual p.49); code 8 is nitrate **and** nitrate+nitrite, a different measurement |
| ammonium, turbidity | not in Table 3a |
| CDOM absorption (m⁻¹) | *not* code 24 — transmissivity/beam attenuation is a different measurement |
| size-fractionated chlorophyll | one chlorophyll code exists, so only a total maps |
| bottom depth, water depth, sounding | not a profile variable at all — it is **secondary header 10**, and `map_metadata` already claims it |

If it is a real WOD variable, add an alias to `VARIABLE_ALIASES` in
[`query_wod_spec.py`](query_wod_spec.py). **Never rename anything in `data/raw/`** — the
raw file is the provenance record.

**An alias is not a rename, and the choice between them matters.** An alias teaches the
whole pipeline that `T090C` means Temperature: it applies to every accession from now on
and touches no data. A rename edits one staged copy of one file, via
[`rename_columns`](../staging/README.md) in Phase 0. Add the alias when the vendor's name
will recur; rename when *this* file's header is wrong, missing, or missing its unit.

Instrument exports are the usual reason a real variable goes unmapped: Sea-Bird writes
`T090C`, `Sal00`, `PrDM`, `Sbeox0ML/L`, `FlSP` and `DepSM` rather than English names, and
the trailing digits are the sensor index. Those are already aliased; other vendors are not.
`DepSM` is the one to watch — an unmapped depth column yields a file with no depth axis.

Matching normalizes first: bracketed unit text is stripped, everything after a comma or
semicolon is dropped, and the rest is lowercased to underscore-separated words. So
`Temp (C)`, `TEMP_C` and `Temperature [deg C]` all reach `temp`/`temperature`. Exact alias
wins; otherwise the longest whole-word alias match wins, which is why `Air_Temperature`
resolves to code 26 rather than being captured by `temp`.

### Unit conversions are inferred from the column name

The unit text lives in the part of the name that normalization discards, so
`conversion_needed` is detected against the **full** raw name:

| Detected in name | `conversion_needed` | Applies to |
| --- | --- | --- |
| `deg F`, `fahrenheit` | `fahrenheit_to_celsius` | codes 1, 26 |
| `ft`, `feet`, `foot` | `feet_to_meters` | depth axis only |
| `fathom`, `fm` | `fathoms_to_meters` | depth axis only |
| `ml/l` | `ml_per_l_to_umol_per_kg` | code 3 (oxygen) only |
| `mg/l` | `mg_per_l_to_umol_per_kg` | code 3 (oxygen) only |
| any molar unit | `molar_rescale` | see below |

**Molar units are one parameterised conversion, not a label per pair.** `molar_rescale`
carries `conversion_from` and `conversion_to` (`"umol/kg"`, `"mmol/l"`) in the same mapping
entry, and Phase 3 computes the factor: a decimal rescale between SI prefixes, plus a
density step only when the basis changes. The target is read from the variable's Table 3a
standard unit, never assumed:

| codes | Table 3a standard | so `µmol/kg` is | and `mmol/l` is |
| --- | --- | --- | --- |
| 3, 4, 6, 8 — oxygen, nutrients | µmol/kg | already standard | × 1000 ÷ density |
| 17, 21 — alkalinity, DIC | **mmol/l** | × density ÷ 1000 | already standard |
| 34, 38, 39 — He, Ar, Ne | **nmol/kg** | × 1000 | — |
| 11 — chlorophyll | µg/l (not molar) | left alone | left alone |

The SI prefix is the whole point: a single `[µumn]mol/l` pattern matched `mmol/l` too, so
alkalinity and DIC — whose standard unit *is* mmol/l — were read as micromolar and divided
by 1000.

**The direction is per-variable, and both directions occur.** Table 3a keeps nutrients and
oxygen per-**mass** (µmol/kg) but alkalinity (17) and DIC (21) per-**volume** (mmol/l):

- A `µmol/kg` source is already standard for oxygen and nutrients, and still needs
  converting for alkalinity and DIC. Treating the latter as standard mislabels it by ~1000×.
- Chlorophyll (11) has a per-volume standard (µg/l) with no molar equivalent, so a
  molar-to-mass conversion is refused for it outright.

**Units must be in the column name.** Detection reads the full raw name and nothing else. A
units row beneath the header, or units documented only in a readme, are invisible — fold
them into the name during [staging](../staging/README.md). Two units that need nothing and
must not be "corrected": `mg m⁻³` for chlorophyll (identical to µg/l) and `hPa` for air
pressure (identical to mbar).

### Header parsing

`parsed_coordinates` accepts decimal (`LAT: 61.930`), integer (`LAT: 61`), degrees + decimal
minutes (`LAT: 61 55.8 N`), and hemisphere suffixes (`33.5 S` → `-33.5`). Dates are read as
`YYYY-MM-DD`, `YYYYMMDD` or `DD-MM-YYYY`, plus `HH:MM` time.

`secondary_headers` is keyed by **Table 4a secondary header ID**, which is a *different
namespace* from Table 3a variable codes — ID 29 is "Probe Type" while variable code 29 is
"Air pressure". Only genuinely numeric headers are coerced to float
(`NUMERIC_SECONDARY_HEADERS` in [`map_metadata.py`](map_metadata.py)); accession, station and
WMO codes stay strings so `0287423` keeps its leading zero.

`SECONDARY_HEADER_PATTERNS` covers the Table 4a fields that turn up in real submissions —
around fifty of the sixty-seven, including sea state, cloud cover, Secchi depth, probe type,
recorder, digitisation method, water sampler, WMO id and the ARGOS fields. Add a pattern
there rather than parsing a key by hand.

---

## Next

[Phase 3 — Conversions and QC](../qc_transforms/README.md) consumes the contract.
Coming from a file that would not parse? [Phase 0 — Staging](../staging/README.md).
