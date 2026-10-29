# Verification

Checks that what was written is right, structurally and in its values.

```python
from wod_etl.verify import validate_intermediate_netcdf, verify_output
```

- `validate_intermediate_netcdf` checks structure: CF and the ragged-array contract. A file
  that fails is not output.
- `verify_output` checks that the values make sense.

Many errors pass structural validation:

| What went wrong | Validator | What catches it |
| --- | --- | --- |
| `DO [mg/L]` mapped with no conversion (~30× error) | PASS | value range physically impossible |
| DIC in µmol/kg treated as standard (~1000× error) | PASS | same |
| A column dropped at mapping | PASS | report.md lists it as not written |
| Depth column unmapped, `z` all NaN | FAIL (all-fill `z` is an error) | `z` range empty |
| QC misapplied to a non-profile dataset | PASS | nothing: WOD QC is off by default |

---

## `validate_intermediate_netcdf(nc_file_path)`

Opens the file and runs six check groups.

```json
{"status": "PASS", "passed": true, "error_count": 0, "warning_count": 1,
 "checks_run": ["global_attributes", "dimensions", "cf_variable_names",
                "coordinate_bounds", "ragged_array_structure", "qc_flags"],
 "errors": [], "warnings": ["'lat' is missing for 1 of 3 cast(s)"]}
```

Errors fail the file; warnings do not.

| Check | Errors on |
| --- | --- |
| `global_attributes` | missing `Conventions`, `featureType`, `wod_dataset_type`, `WOD_version`; `featureType` ≠ `profile` |
| `dimensions` | missing `casts` or `z_obs` |
| `cf_variable_names` | any variable or dimension name that is not a CF identifier |
| `coordinate_bounds` | `lat`/`lon` absent, entirely missing, or out of range; `z` absent, entirely missing, or lacking `positive="down"`; `time` without `units` |
| `ragged_array_structure` | a measured variable or `z` not on `z_obs`; any stray `*_obs` dimension; a count variable without `sample_dimension`, on the wrong dimension, or whose sum ≠ the observation dimension length |
| `qc_flags` | a QC array whose dims differ from its variable, or values outside 0–9 |

Warnings: a partially missing coordinate, negative depths, an all-fill measured variable,
`wod_dataset_type` of `unresolved`, a measured variable with no QC array, a QC array lacking
`flag_meanings`.

An unreadable file returns `status: "FAIL"` with `fatal_error`; it does not raise.

### Reading a failure

- *"not on the shared observation dimension"*: the writer produced per-variable dimensions.
  Regenerate rather than patch.
- *"not a valid CF identifier"*: a name was written without `cf_safe_name`.
- *"entirely missing: no usable position"*: the contract had no coordinates. Fix it in
  mapping; check `missing_required_coordinates`.
- *"Row size mismatch"*: counts and observations disagree; casts were assembled
  inconsistently.

---

## `verify_output(processed_dir, sample_levels=SAMPLE_LEVELS, paths=None)`

Checks the values: reads every written NetCDF back and compares each observation with the
manual's range for that variable and depth (Appendix 11). `process_accession` runs it at the
end of every run.

```python
report = verify_output("data/processed/2-CTD")
report["passed"], report["finding_counts"], report["findings"]
print(report["text"])
```

- `paths` limits the check to those files (what a run wrote) instead of every `*.nc` in the
  folder.
- Every value is range-checked, up to `SAMPLE_LEVELS` (5,000,000) per variable; the report
  says how many were skipped beyond that.
- In a file built from several source files, the copy and depth-axis checks run per source
  (by the per-cast `source_file`), and the finding's `file` names the source.

| Finding | Severity | What it means |
| --- | --- | --- |
| `systematically_out_of_range` | high | ≥50% of a variable's levels are impossible: usually a unit or conversion error |
| `flat_depth_axis` | high | every level at one depth, so not a profile; usually a bottom-depth column on the vertical axis |
| `negative_depth` | high | depth is positive down; a negative value is an unapplied sign convention |
| `identical_variables` | high | two measured variables equal at every level of a source; one is a copy of the other. Leave the copy out with `drop` |
| `unreadable_output` | high | the pipeline wrote a file it cannot read back |
| `all_fill` | medium | the column mapped but held nothing, or the conversion produced NaN throughout |
| `constant_value` | medium | a stuck sensor, an unrecognised fill value, or per-cast metadata mapped as a measurement |
| `duplicate_cast_id` | high | two casts in the run share a `cast_id`. Within one file the writer suffixes repeats (`_02`); a suffixed id whose base belongs to another source file still counts |
| `out_of_range_levels` | medium / low | some extreme levels. Normal at low severity; worth reading above 2% |
| `near_zero_offset` | low | values below a lower bound of zero by at most a tenth of the variable's 99th percentile (a fluorometer dark count, an analyser blank). Kept as submitted, not counted as out of range, never clipped |

Every finding has `evidence` (the worst value, its depth and the bound it broke) and an
`action`.

`passed` is false only on a high finding. A medium finding is a question; a high finding
means the variable cannot be what it claims.

**Caveats**

- Ranges are the union of all 30 ocean basins (basin assignment needs `range_area.msk` from
  the WOD website). A flagged value is outside every basin's bound; an unflagged value may
  still be wrong (nitrate's union bound reaches 500 µmol/kg).
- It cannot see a dropped column; the mapping's `unmapped_columns` and report.md do.

`_suspect_cause` names the conversion a systematic offset resembles (~1000× for a per-litre
column in a per-kilogram variable, ~1.8× for Fahrenheit) when the ratio matches within 15%.
Otherwise it prints the ratio and suggests checking Table 3a.

---

## `validate_taxa_netcdf(path)` — the gate for Taxa-Record files

The structural check for `write_taxa_netcdf` output (plankton), which has no depth axis or
Appendix 11 range.

- **Errors** (the file is not output): a required variable missing, `taxa_row_size` not
  summing to the records, a record with no depth range or an upper depth below its lower, a
  record with no measurement, a negative count, a "per UNIT" field with no originator's unit
  (Table 7a field 20).
- **Warnings**: records whose taxon did not resolve to a WOD variable number, and key
  Biological Header fields not stated (`missing_biological_fields`).

---

## What neither check can do

- A mis-triaged folder (currents or bathymetry forced through the profile path) verifies
  clean, because its numbers are internally consistent.
- A wrong time zone shifts every cast by hours and stays inside every bound.

These need a cross-check against an independent source in the accession, such as a station
list against in-file positions; see
[`knowledge/accession_field_guide.md`](../../knowledge/accession_field_guide.md).
