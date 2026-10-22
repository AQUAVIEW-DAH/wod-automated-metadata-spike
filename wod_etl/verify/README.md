# Verification

Checks that what was written is right, structurally and in its values.

```python
from wod_etl.verify import validate_intermediate_netcdf, audit_dataset
```

- `validate_intermediate_netcdf` is structural: does the file obey CF and the ragged-array
  contract? A file that fails is not output.
- `verify_output` and `audit_dataset` are semantic: do the values make sense?

Many real errors pass structural validation:

| What went wrong | Validator | Audit |
| --- | --- | --- |
| `DO [mg/L]` mapped with no conversion (~30× error) | PASS | value range physically impossible |
| DIC in µmol/kg treated as standard (~1000× error) | PASS | same |
| A column dropped at mapping | PASS | appears in `unmapped_columns` |
| Depth column unmapped, `z` all NaN | FAIL (all-fill `z` is an error) | `z` range empty |
| QC misapplied to a non-profile dataset | PASS | most flags non-zero |

Run both.

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
  Phase 2; check `missing_required_coordinates`.
- *"Row size mismatch"*: counts and observations disagree; casts were assembled
  inconsistently.

---

## `verify_output(processed_dir, sample_levels=SAMPLE_LEVELS, paths=None)`

Checks the values. `process_accession` runs it at the end of every run.

It reads every written NetCDF back and compares each observation against the manual's
acceptable range for that variable at that depth (Appendix 11), and reports findings.

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
- It cannot see a dropped column. Use `unmapped_columns` and `audit_dataset` for that.

`_suspect_cause` names the conversion a systematic offset resembles (~1000× for a per-litre
column in a per-kilogram variable, ~1.8× for Fahrenheit) when the ratio matches within 15%.
Otherwise it prints the ratio and suggests checking Table 3a.

---

## `audit_dataset(staged_dir, dataset_type, processed_dir)`

Recomputes the mapping contract for every staged file and reads back every NetCDF, to answer
what the run manifest cannot.

| Not in a run summary | What the audit gives you |
| --- | --- |
| Which columns were dropped | `unmapped_columns`, per file |
| Whether the values are plausible | `value_ranges`: min/max and units per variable |
| The QC flag distribution | `qc_flags`: counts and non-zero percentage |

```python
from wod_etl.verify import audit_dataset

report = audit_dataset("data/staging/0287423/2-CTD", "CTD", "data/processed/2-CTD")
print(report["text"])
```

It also returns `all_missing_variables`, `missing_coordinates`, `contract_warnings`,
`wod_dataset_routing`, and `text`, a readable summary.

Look for an expected column in `unmapped_columns`, a physically wrong range (a unit error),
and a near-total flag rate (a misapplied check).

`contract_for(path, dataset_type)` returns one file's contract without auditing the set.

---

## `validate_taxa_netcdf(path)` — the gate for Taxa-Record files

The structural check for `write_taxa_netcdf` output (plankton), which has no depth axis or
Appendix 11 range.

- **Errors** (the file is not output): a required variable missing, `taxa_row_size` not
  summing to the records, a record with no depth range or an upper depth below its lower, a
  record with no measurement, a negative count, a "per UNIT" field with no originator's unit
  (Table 7a field 20).
- **Warnings** (report them): records whose taxon did not resolve to a WOD variable number,
  and key Biological Header fields not stated (`missing_biological_fields`).

---

## What neither check can do

- A mis-triaged folder (currents or bathymetry forced through the profile path) audits
  clean, because its numbers are internally consistent.
- A wrong time zone shifts every cast by hours and stays inside every bound.
- A basin-inappropriate value passes, because range checks use the union of all 30 basins.

For these, cross-check against an independent source in the accession (an underway GPS
track against station times, a station list against in-file positions). See
[`knowledge/accession_field_guide.md`](../../knowledge/accession_field_guide.md).
