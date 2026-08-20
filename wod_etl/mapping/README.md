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
| `normalize_name(name)` | `"Temp (deg C)"` → `"temp"`. What matching operates on. |
| `resolve_dataset(label, depth_increment_m)` | Dataset routing on its own. |
| `wod_variables()` / `wod_datasets()` / `wod_secondary_headers()` | Raw tables. |

**Depth is not a WOD variable.** There is no code for it — code 25 is *Pressure*, a distinct
measurement. Internally a depth column carries the sentinel code `0`
(`DEPTH_SENTINEL_CODE`), meaning "this is the vertical axis, in metres".

---

## `generate_field_mapping(profile_result, spec_result)`

Builds the contract.

```python
contract = generate_field_mapping({**profile, "columns": list(df.columns)}, spec)
```

```json
{"source": ".../xctd_01.csv",
 "dataset_type": "XCTD", "wod_dataset_code": "CTD",
 "column_mapping": [
   {"raw_column": "Depth (ft)", "wod_code": 0, "wod_name": "Depth",
    "role": "depth_axis", "target_unit": "meters", "conversion_needed": "feet_to_meters"},
   {"raw_column": "Temp (deg F)", "wod_code": 1, "wod_name": "Temperature",
    "role": "measurement", "target_unit": "Degrees Celsius",
    "conversion_needed": "fahrenheit_to_celsius"}],
 "extracted_header_metadata": {"1": {"name": "Accession Number", "value": "0287423"},
                               "3": {"name": "Platform Code", "value": "STOCS85A"}},
 "parsed_coordinates": {"latitude": 61.93, "longitude": -172.27,
                        "year": 1985, "month": 6, "day": 12},
 "missing_required_coordinates": [],
 "unmapped_columns": ["Cond"],
 "warnings": [],
 "ready_for_transform": true}
```

### Read these four fields before trusting a run

| Field | What it means |
| --- | --- |
| `unmapped_columns` | **Silently dropped from the output file.** A column you expected here means a missing alias. |
| `missing_required_coordinates` | The file will fail Phase 4 validation. Lists any of `latitude`, `longitude`, `time`, `depth`. |
| `ready_for_transform` | `false` = non-empty mapping is missing or coordinates are absent. Do not proceed blind. |
| `warnings` | Unresolved dataset, two columns claiming one WOD code, a variable not stored in this dataset. |

### To map a column that landed in `unmapped_columns`

First ask whether WOD stores the variable at all. Some columns *should* stay unmapped, and
aliasing them to an adjacent code is worse than dropping them:

| Column | Why it has no code |
| --- | --- |
| conductivity, sigma-t/sigma-theta, sound velocity | not in Table 3a |
| potential temperature | mapping it to code 1 makes a cast claim Temperature twice |
| nitrite | excluded from the online database (manual p.49); code 8 is nitrate **and** nitrate+nitrite, a different measurement |
| ammonium, turbidity | not in Table 3a |
| CDOM absorption (m⁻¹) | *not* code 24 — transmissivity/beam attenuation is a different measurement |
| size-fractionated chlorophyll | one chlorophyll code exists, so only a total maps |

If it is a real WOD variable, add an alias to `VARIABLE_ALIASES` in
[`query_wod_spec.py`](query_wod_spec.py). **Do not rename the source data** — the raw file
is the provenance record.

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

`extracted_header_metadata` is keyed by **Table 4a secondary header ID**, which is a
*different namespace* from Table 3a variable codes — ID 29 is "Probe Type" while variable
code 29 is "Air pressure". Only genuinely numeric headers (10, 22, 23, 24) are coerced to
float; accession, station and WMO codes stay strings so `0287423` keeps its leading zero.

---

## Next

[Phase 3 — Conversions and QC](../qc_transforms/README.md) consumes the contract.
Coming from a file that would not parse? [Phase 0 — Staging](../staging/README.md).
