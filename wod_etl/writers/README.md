# Phase 4 — Writers

**Write a CF-compliant contiguous ragged array NetCDF file, then prove it is one.**

```python
from wod_etl.writers import write_ragged_profiles, generate_netcdf_ragged
```

A file that fails validation is not output. Report it as failed rather than shipping it.

**Validation proves structure, not correctness.** A file can pass every check here and still
be wrong: a variable in the wrong unit validates cleanly, and so does a cast whose values
are all fill. Two checks exist because both were shipped against real data:

- **An all-fill `z` is a hard error.** A depth axis of nothing but fill satisfies every
  structural rule while making the file useless — no measurement can be placed in the water
  column. It happens when the depth column fails to map and no one reads the values.
- **An all-fill measured variable is a warning.** An entirely unanalysed source column is
  honest output, but it is indistinguishable from a mapping failure unless it is announced.

Neither replaces opening a finished file and reading the ranges.

---

## The ragged array, and why the shape matters

A profile dataset is jagged: cast 1 has 3 levels, cast 2 has 2. Padding that into a 2-D
`(casts × max_levels)` grid wastes space and invents fill values. CF's Discrete Sampling
Geometry instead flattens every observation into **one** dimension and stores a count per
cast:

```
dimensions:  casts = 2 ;  z_obs = 5 ;

z_row_size = [3, 2]        ← cast 0 owns z_obs[0:3], cast 1 owns z_obs[3:5]
z          = [0, 10, 20,  0, 12]
temperature= [8.7, 8.4, 7.9,  8.1, 7.8]
lat        = [62.1, 62.4]   ← one per cast
```

**Every** measured variable and `z` live on `z_obs`. That shared dimension is what makes
depth index-aligned with temperature. Giving each variable its own `Temperature_obs`
dimension breaks the alignment — the file still opens, but nothing can pair a measurement
with its depth. The validator treats that as a hard error.

---

## `write_ragged_profiles(casts, output_nc_path)`

The real writer. Takes a **list** of casts and writes them into one file.

```python
casts = [{"df": df1, "contract": c1, "transform_log": t1},
         {"df": df2, "contract": c2}]
res = write_ragged_profiles(casts, "data/processed/cast_02.nc")
```

```json
{"status": "success", "output_path": "...", "cast_count": 2, "total_observations": 5,
 "row_sizes": [3, 2], "variables_written": ["temperature", "salinity"],
 "variable_detail": [{"nc_name": "temperature", "wod_name": "Temperature",
                      "wod_code": 1, "units": "Degrees Celsius"}]}
```

Casts may have different column sets — the union is written and absent values become fill.

### `generate_netcdf_ragged(df, contract, path, transform_log=None)`

Single-cast wrapper over the above. Use it when you have exactly one cast.

### What ends up in the file

| Group | Contents |
| --- | --- |
| Global attributes | `title`, `Conventions`, `featureType=profile`, `WOD_version`, `wod_dataset_type`, `source_file` (the path read), `source_format` (how), `source`; `institution` only when the originator stated one (secondary header 4) |
| Provenance | `comment` (the staged header's `# Note:` lines every cast shares; differing ones go to a `cast_notes` variable), `history`, `source_files_raw` (the `# Source:` lines), `metadata_not_placed` (header keys with no WOD field), `unit_conversions`, and `seawater_density_kg_m3` + `seawater_density_provenance` when a conversion used the assumed density |
| Character data | `originator_cruise_id`, `principal_investigator` (manual p.30) -- global when shared, per cast otherwise |
| Secondary headers | one `secondary_header_<id>_<name>` attribute per Table 4a entry, plus `<same>_code_table` where WOD publishes a code list; a coded value that is not a code goes to `<same>_originator_text` with `<same>_code_lookup` instead (below) |
| Per cast (`casts`) | `lat`, `lon`, `time` (days since 1770-01-01), `date` (YYYYMMDD), `GMT_time` (hours, fill when not stated), `cast_id` (`cf_role=profile_id`), `z_row_size` (`sample_dimension=z_obs`), and any secondary header that differs between casts |
| Per level (`z_obs`) | `z` (`positive=down`, `axis=Z`), `z_qc` when WOD QC ran, one variable per WOD code, and per variable `<var>_qc` and/or `<var>_qc_originator` |

Each measured variable carries `units`, `long_name` (the WOD name), `wod_variable_code`,
`coordinates = "time lat lon z"` and `ancillary_variables` pointing at its flag arrays.

### Metadata on each variable

Table 5a variable specific second headers are written as attributes, so the file states what
the originator declared rather than only what the conversion produced:

| Attribute | From | Why it is there |
| --- | --- | --- |
| `originator_units`, `wod_variable_specific_header_8` | the raw column name; header 8 is a code only when WOD's units list has exactly one match, else `_originator_text` | A conversion is only auditable against the unit the source declared |
| `measurement_scale`, `wod_variable_specific_header_3` | `[ITS-90]`, `(PSS-78)` | A temperature is ambiguous without knowing which standard it is on |
| `originator_column_name` | the raw column name, verbatim | The link back to the source file |
| `comment` | the mapping, e.g. a fluorometer column written as Chlorophyll | What the number actually is |
| `<header>_originator_text`, `<header>_code_lookup` | a coded Table 5a value that did not resolve | The originator's text, kept beside the field rather than in it |
| `wod_variable_specific_header_<id>` | other Table 5a fields, e.g. 17 for nitrate+nitrite | How WOD records the distinction |

Use `setncattr`, not `setattr`, if you add more: netCDF4 reserves Python attribute names on
`Variable` — `scale`, `mask`, `name`, `dtype` and others — and rebinding one raises. That is
why the scale attribute is `measurement_scale`.

### Two kinds of quality flag, and they do not mix

| Variable | Scale | Written when |
| --- | --- | --- |
| `<var>_qc` | WOD Table 12 section 2b, with `flag_values` and `flag_meanings` | WOD QC ran (`run_qc=True`) |
| `<var>_qc_originator` | **the submitter's own**, whatever it is | the source file had a flag column |

The originator array deliberately carries **no** `flag_values`/`flag_meanings` — inventing
them would assert a scale nobody stated. It carries `source_column`, `wod_secondary_header =
96`, and a `code_table` URL for the list of WOD flag sets. An originator's `1` may mean
*good* where WOD's `1` means a failed range check; translating between them is not possible
without knowing the set, which is what header 96 records.

It is stored as float, not int: some submitters use decimals or letters, and coercing would
silently rewrite them.

### Variable names are sanitised

NetCDF and CF require identifiers: a letter first, then letters, digits, underscores. WOD
names are prose, so they are converted and the original kept in `long_name`.

| WOD name | Variable name |
| --- | --- |
| `Temperature` | `temperature` |
| `Dissolved inorganic carbon` | `dissolved_inorganic_carbon` |
| `Nitrate and Nitrate + Nitrite` | `nitrate_and_nitrate_nitrite` |
| `14C` | `v_14c` (leading digit prefixed) |

`cf_safe_name(name)` is exported if you need to predict a name. Collisions get `_2`, `_3`.

### Gotchas

- **Depth is not written as a variable.** A mapping with `wod_code == 0` becomes `z`. It
  will not appear in `variables_written`.
- **No `<var>_qc` is normal.** WOD QC is off by default. The validator distinguishes "carries
  the originator's flags and no WOD flags" from "no quality flags at all"; only the second
  is worth chasing.
- **A secondary header identified but with no value** — header 96 when flag columns exist but
  the flag set is unnamed — becomes a `<key>_status` attribute saying so, not a variable of
  pure fill.
- **The output path is yours to choose.** `process_one_file` uses `<source stem>.nc`; nothing
  enforces that.
- Missing coordinates are written as NaN rather than refused, so the *validator* is what
  catches them. Always validate.
- **A date without a time of day is not midnight.** `time` sits at 00:00 of the date because
  CF needs a value, `GMT_time` is fill, and `time.comment` says how many casts are date-only.
  A numeric `time_zone` (`UTC+10`) with a time of day is converted to UTC; a named zone or a
  date alone is recorded as `time_zone_of_source` and **not** shifted (`time_zone_note`).
- **`cast_id` identifies the cast outside the file**: accession (or platform), originator's
  cruise, cast or station (or the source file's name), and the date with its time when
  known -- `0303928_SHU1505-st01_20151109`. It used to be `<platform>_0000` for every one-cast
  file. Repeats within a file are suffixed; `verify_output` checks uniqueness across a run.
- **A coded field holds a code or nothing.** When a lookup did not find exactly one entry
  (`code_lookup` in the contract), the field is not written: the originator's text goes to
  `<field>_originator_text` and the reason to `<field>_code_lookup`. A contract with no lookup
  information is written as it stands.

---

## `write_taxa_netcdf(hauls, output_path)` — Taxa-Records, not profiles

Plankton counts and biomass are not values at a level, so they get their own layout, written
beside the profile writer and sharing its helpers for time, cast ids, secondary headers,
character data and provenance. `hauls` come from `wod_etl.mapping.map_haul`.

| Dimension | Variables |
| --- | --- |
| `casts` (one per haul) | `lat`, `lon`, `time`, `date`, `GMT_time`, `cast_id`, `taxa_row_size` (CF ragged count, `sample_dimension = "taxa_obs"`), Biological Header fields `b_<id>_<name>`, and secondary headers and character data as for a profile |
| `taxa_obs` (one per Taxa-Record) | `t_1_variable_number` (ITIS TSN, or a negative WOD code; fill if unresolved), `taxon_name_originator` (always), `taxon_resolution`, `t_30_plankton_grouping_code`, `t_2_upper_depth`, `t_3_lower_depth`, coded qualifiers (`t_5_taxon_life_stage` + `_originator_text`), measurements (`t_10_taxon_count`...), `t_20_originator_s_unit` |

A coded Biological Header field is its code only when the list has exactly one match;
otherwise the text goes to `_originator_text` with the reason in `_code_lookup`. The global
`wod_record_model` names the layout; `wod_dataset_type` is `OSD`, where Table 2 puts plankton.
Validate with `wod_etl.verify.validate_taxa_netcdf`, not the profile validator.

---

## Validation lives next door

`validate_intermediate_netcdf` moved to [`wod_etl/verify/`](../verify/README.md), with the
dataset audit it belongs with. Missing coordinates are written as NaN here rather than
refused, so the validator is what catches them -- always run it.

---

## Next

[Orchestration](../orchestration/README.md) runs all four phases over a whole accession.
