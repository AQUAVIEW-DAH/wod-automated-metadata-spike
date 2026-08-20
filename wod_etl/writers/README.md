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
| Global attributes | `title`, `Conventions`, `featureType=profile`, `WOD_version`, `wod_dataset_type`, `source_file`, `institution`, `references` |
| Provenance | `unit_conversions`, and `seawater_density_kg_m3` + `seawater_density_provenance` when a conversion used the assumed density |
| Secondary headers | one `secondary_header_<id>_<name>` attribute per contract entry |
| Per cast (`casts`) | `lat`, `lon`, `time` (days since 1770-01-01), `cast_id` (`cf_role=profile_id`), `z_row_size` (`sample_dimension=z_obs`) |
| Per level (`z_obs`) | `z` (`positive=down`, `axis=Z`), `z_qc`, one variable per WOD code, one `<var>_qc` each |

Each measured variable carries `units`, `long_name` (the WOD name), `wod_variable_code`,
`coordinates = "time lat lon z"` and `ancillary_variables` pointing at its QC array. QC
arrays carry `flag_values` and `flag_meanings`.

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
- **The output path is yours to choose.** `process_one_file` uses `<source stem>.nc`; nothing
  enforces that.
- Missing coordinates are written as NaN rather than refused, so the *validator* is what
  catches them. Always validate.

---

## Validation lives next door

`validate_intermediate_netcdf` moved to [`wod_etl/verify/`](../verify/README.md), with the
dataset audit it belongs with. Missing coordinates are written as NaN here rather than
refused, so the validator is what catches them -- always run it.

---

## Next

[Orchestration](../orchestration/README.md) runs all four phases over a whole accession.
