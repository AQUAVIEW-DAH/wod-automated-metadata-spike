# Phase 4 — Writers

Write CF-compliant contiguous ragged array NetCDF files.

```python
from wod_etl.writers import write_ragged_profiles, generate_netcdf_ragged
```

A file that fails validation is not output; it is reported as failed.

Validation checks structure, not correctness: a variable in the wrong unit still validates.
An all-fill `z` is an error (usually an unmapped depth column); an all-fill measured variable
is a warning, since the source column may be unanalysed.

---

## The ragged array

Profiles have different numbers of levels. Instead of padding to a 2-D
`(casts × max_levels)` grid, CF's Discrete Sampling Geometry stores every observation on one
dimension with a count per cast:

```
dimensions:  casts = 2 ;  z_obs = 5 ;

z_row_size = [3, 2]        ← cast 0 owns z_obs[0:3], cast 1 owns z_obs[3:5]
z          = [0, 10, 20,  0, 12]
temperature= [8.7, 8.4, 7.9,  8.1, 7.8]
lat        = [62.1, 62.4]   ← one per cast
```

Every measured variable and `z` share `z_obs` (plain CF), with fill where a cast lacks a
variable. WOD's own files (Appendix 12, p.114) instead give each variable its own `VAR_obs`
dimension and `VAR_row_size` count (0 for a cast without it); converting to that layout drops
each variable's fill-only casts. The validator rejects a file mixing the two.

---

## `write_ragged_profiles(casts, output_nc_path)`

Writes a list of casts into one file.

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

Casts may have different column sets; the union is written and absent values become fill.

### `generate_netcdf_ragged(df, contract, path, transform_log=None)`

Single-cast wrapper over `write_ragged_profiles`.

### One file per WOD dataset and year -- `wod_files`

```python
from wod_etl.writers import group_by_file, wod_file_name, is_wod_file_name
wod_file_name("CTD", 2008, "0062621")      # 'wod_ctd_2008_0062621.nc'
```

WOD distributes one NetCDF file per dataset and year (`wod_ctd_2008.nc`; manual pp.44, 46;
names from NCEI's listing). Output is split the same way with the accession appended, so it
cannot be mistaken for WOD's own files. The year is the cast's UTC date (`cast_year`); a cast
with no date goes to `wod_<dataset>_undated_<acc>.nc`.

`group_by_file(sources, accession)` turns `[{"key", "casts"}]` (each source file's casts, as
`write_ragged_profiles` takes them) into one entry per output file, keeping each source's
casts together and in order, and ordering sources by their first cast's time.

`process_accession` writes each source file on its own first (so a failure belongs to that
file), then assembles with `assemble_profiles`. Assembly replaces or removes only files whose
names match `is_wod_file_name(name, accession)` and whose every source is one the run covers.
Input that is not an accession is named for its folder (`folder_scope`).

**Several sources in one file.** Values all casts share are file attributes; values that
differ are written per cast: `source_file`, notes, headers, variable metadata (listed in the
variable's `per_cast_attributes`), and originator flag schemes
(`<var>_qc_originator_flag_values`/`_flag_meanings`).

### What ends up in the file

| Group | Contents |
| --- | --- |
| Global attributes | `title`, `Conventions`, `featureType=profile`, `WOD_version`, `wod_dataset_type` (`mixed`, with a validator warning, when casts are of several datasets), `source_file` (the file as the accession names it, `data/0-data/x.csv`; a staged cast is `staged/<recipe>/<cast>.csv`), `source_format` (how), `source`; `institution` only when the originator stated one (secondary header 4). `source_file`, `source_format` and `source_files_raw` are per-cast variables when casts came from different files |
| Provenance | `comment` (the staged `# Note:` lines all casts share; differing ones go to a `cast_notes` variable), `history`, `source_files_raw` (the `# Source:` lines: the accession's files a staged cast came from), `metadata_not_placed` (shared header keys with no WOD field; a cast's own go to `cast_metadata_not_placed`), `unit_conversions` (each once), and `seawater_density_kg_m3` + `seawater_density_provenance` when a per-volume value was converted with a density |
| Primary header | `country` (Appendix 1 code) and `country_name`, when the source states a country |
| Character data | `originator_cruise_id`, `principal_investigator`, `principal_investigator_code` (manual p.30); global when shared, per cast otherwise |
| Secondary headers | one `secondary_header_<id>_<name>` attribute per Table 4a entry, plus `<same>_code_table` where WOD publishes a code list; a coded value that is not a code goes to `<same>_originator_text` with `<same>_code_lookup` instead (below) |
| Per cast (`casts`) | `lat`, `lon`, `time` (days since 1770-01-01), `date` (YYYYMMDD), `GMT_time` (hours, fill when not stated), `cast_id` (`cf_role=profile_id`), `z_row_size` (`sample_dimension=z_obs`), `dataset` (the WOD dataset), `wod_number_of_variables` and `wod_variable_codes` (Table 10a fields 18 and 20: the Table 3a codes a cast has values for, as a ragged list on `wod_variables`), `wod_unique_cast` and `wod_cruise_number` (fields 5 and 8, fill: NCEI assigns them), and any secondary header that differs between casts |
| Per level (`z_obs`) | `z` (`positive=down`, `axis=Z`), `z_qc` when WOD QC ran, one variable per WOD code, and per variable `<var>_qc` and/or `<var>_qc_originator` |

Each measured variable carries:

- `units`: the UDUNITS string of its Table 3a standard unit (`degree_C`);
- `wod_units`: Table 3a's wording (`Degrees Celsius`);
- `standard_name` where CF has one for the quantity as WOD stores it (none for pH, whose
  scale WOD does not state);
- `long_name` (the WOD name), `wod_variable_code`, `coordinates = "time lat lon z"`, and
  `ancillary_variables` pointing at its flag arrays.

Missing values are written as `_FillValue`, not NaN. Cast times keep their seconds.

### Metadata on each variable

Table 5a variable specific headers are written as attributes, recording what the originator
declared:

| Attribute | From | Why it is there |
| --- | --- | --- |
| `originator_units`, `wod_variable_specific_header_8` | the raw column name; header 8 is a code only when WOD's units list has exactly one match, else `_originator_text` | A conversion is auditable only against the source unit |
| `measurement_scale`, `wod_variable_specific_header_3` | `[ITS-90]`, `(PSS-78)` | A temperature is ambiguous without its scale |
| `originator_column_name` | the raw column name, verbatim | The link back to the source file |
| `comment` | the mapping, e.g. a fluorometer column written as Chlorophyll | What the number is |
| `<header>_originator_text`, `<header>_code_lookup` | a coded Table 5a value that did not resolve | The originator's text, kept beside the field |
| `wod_variable_specific_header_<id>` | other Table 5a fields, e.g. 17 for nitrate+nitrite | How WOD records the distinction |

Attributes are added with `setncattr`, not `setattr`: netCDF4 reserves names such as `scale`
and `name` on `Variable` (hence `measurement_scale`).

### Two kinds of quality flag

| Variable | Scale | Written when |
| --- | --- | --- |
| `<var>_qc` | WOD Table 12 section 2b, with `flag_values` and `flag_meanings` | WOD QC ran (`run_qc=True`) |
| `<var>_qc_originator` | the submitter's own | the source file had a flag column |

The originator array has `flag_values`/`flag_meanings` only when the source stated them. It
carries `source_column`, `wod_secondary_header = 96`, and a `code_table` URL for the list of
WOD flag sets. It is stored as float, so decimal flags are not rewritten.

### Variable names are sanitised

NetCDF/CF identifiers must start with a letter, then letters, digits or underscores. WOD
names are converted, and the original kept in `long_name`.

| WOD name | Variable name |
| --- | --- |
| `Temperature` | `temperature` |
| `Dissolved inorganic carbon` | `dissolved_inorganic_carbon` |
| `Nitrate and Nitrate + Nitrite` | `nitrate_and_nitrate_nitrite` |
| `14C` | `v_14c` (leading digit prefixed) |

`cf_safe_name(name)` is exported to predict a name. Collisions get `_2`, `_3`.

### Gotchas

- Depth is written as `z`, not as a variable (`wod_code == 0`); it is not in
  `variables_written`.
- No `<var>_qc` is normal: WOD QC is off by default. The validator distinguishes "originator
  flags only" from "no flags at all".
- A secondary header identified without a value (e.g. header 96 when flag columns exist but
  the set is unnamed) becomes a `<key>_status` attribute.
- The output path is yours to choose; `process_one_file` uses `<source stem>.nc`.
- Missing coordinates are written as NaN, not refused; the validator reports them.
- A date without a time of day is not midnight: `time` is 00:00 of the date (CF needs a
  value), `GMT_time` is fill, and `time.comment` says how many casts are date-only. A
  numeric `time_zone` (`UTC+10`) with a time of day is converted to UTC; a named zone or a
  date alone is recorded as `time_zone_of_source`, not shifted (`time_zone_note`).
- `cast_id` identifies the cast outside the file: accession (or platform), originator's
  cruise, cast or station (or source file name), and date with time when known, e.g.
  `0303928_SHU1505-st01_20151109`. Repeats within a file are suffixed; `verify_output`
  checks uniqueness across a run.
- A coded field holds a code or nothing. When a lookup did not find exactly one entry, the
  field is not written: the text goes to `<field>_originator_text` and the reason to
  `<field>_code_lookup`. A contract with no lookup information is written as it stands.

---

## `write_taxa_netcdf(hauls, output_path)` — Taxa-Records, not profiles

Plankton counts and biomass have their own layout, sharing the profile writer's helpers.
`hauls` come from `wod_etl.mapping.map_haul`.

| Dimension | Variables |
| --- | --- |
| `casts` (one per haul) | `lat`, `lon`, `time`, `date`, `GMT_time`, `cast_id`, `wod_unique_cast` and `wod_cruise_number` (fill, as for a profile), `taxa_row_size` (CF ragged count, `sample_dimension = "taxa_obs"`), Biological Header fields `b_<id>_<name>`, and secondary headers and character data as for a profile |
| `taxa_obs` (one per Taxa-Record) | `t_1_variable_number` (ITIS TSN, or a negative WOD code; fill if unresolved), `taxon_name_originator` (always), `taxon_resolution`, `t_30_plankton_grouping_code`, `t_2_upper_depth`, `t_3_lower_depth`, coded qualifiers (`t_5_taxon_life_stage` + `_originator_text`), measurements (`t_10_taxon_count`...), `t_20_originator_s_unit` |

Coded Biological Header fields follow the coded-field rule above. The global
`wod_record_model` names the layout; `wod_dataset_type` is `OSD` (Table 2 puts plankton
there). These files are validated by `wod_etl.verify.validate_taxa_netcdf`.

---

## Validation

`validate_intermediate_netcdf` is in [`wod_etl/verify/`](../verify/README.md), with the
dataset audit.

---

## Next

[Orchestration](../orchestration/README.md) runs all four phases over a whole accession.
