# Verification

**Checking that what was written is actually right.** Two checks that fail in different
ways, and the second is the one that catches real corruption.

```python
from wod_etl.verify import validate_intermediate_netcdf, audit_dataset
```

`validate_intermediate_netcdf` is **structural** — does this file obey CF and the ragged-array
contract. It gates output: a file that fails it is not output, it is a failure.

`audit_dataset` is **semantic** — does the content make sense. Run it on every dataset and
report what it says.

The gap between them is the whole point. Every silent-corruption bug this project has hit
passed structural validation cleanly:

| What went wrong | Validator | Audit |
| --- | --- | --- |
| `DO [mg/L]` mapped with no conversion (~30× error) | PASS | value range physically impossible |
| DIC in µmol/kg treated as standard (~1000× error) | PASS | same |
| A column silently dropped at mapping | PASS | appears in `unmapped_columns` |
| Depth column unmapped, `z` all NaN | PASS *(until fixed)* | `z` range empty |
| QC misapplied to a non-profile dataset | PASS | 85–100% non-zero flags |

A structural check cannot know what a number means. Run both.

---

## `validate_intermediate_netcdf(nc_file_path)`

Opens the file and runs six check groups.

```json
{"status": "PASS", "passed": true, "error_count": 0, "warning_count": 1,
 "checks_run": ["global_attributes", "dimensions", "cf_variable_names",
                "coordinate_bounds", "ragged_array_structure", "qc_flags"],
 "errors": [], "warnings": ["'lat' is missing for 1 of 3 cast(s)"]}
```

**Errors fail the file. Warnings do not.**

| Check | Errors on |
| --- | --- |
| `global_attributes` | missing `Conventions`, `featureType`, `wod_dataset_type`, `WOD_version`; `featureType` ≠ `profile` |
| `dimensions` | missing `casts` or `z_obs` |
| `cf_variable_names` | any variable or dimension name that is not a CF identifier |
| `coordinate_bounds` | `lat`/`lon` absent, entirely missing, or out of range; `z` absent or lacking `positive="down"`; `time` without `units` |
| `ragged_array_structure` | a measured variable or `z` not on `z_obs`; any stray `*_obs` dimension; a count variable without `sample_dimension`, on the wrong dimension, or whose sum ≠ the observation dimension length |
| `qc_flags` | a QC array whose dims differ from its variable, or values outside 0–9 |

Warnings cover: a partially-missing coordinate, negative depths, `wod_dataset_type` of
`unresolved`, a measured variable with no QC array, a QC array lacking `flag_meanings`.

An unreadable file returns `status: "FAIL"` with `fatal_error` rather than raising.

### Reading a failure

- *"not on the shared observation dimension"* — the writer produced per-variable dimensions.
  Structural; regenerate rather than patch.
- *"not a valid CF identifier"* — a name was written raw instead of through `cf_safe_name`.
- *"entirely missing: no usable position"* — the contract had no coordinates. The fix is
  upstream in Phase 2, not here; check `missing_required_coordinates`.
- *"Row size mismatch"* — counts and observations disagree, meaning casts were assembled
  inconsistently.

---

## `audit_dataset(staged_dir, dataset_type, processed_dir)`

Recomputes the mapping contract for every staged file and reads back every NetCDF, so the
questions the run manifest cannot answer get numbers.

| Not in a run summary | What the audit gives you |
| --- | --- |
| Which columns were dropped | `unmapped_columns`, per file |
| Whether the values are plausible | `value_ranges` — min/max and units per variable |
| The QC flag distribution | `qc_flags` — counts and non-zero percentage |

```python
from wod_etl.verify import audit_dataset

report = audit_dataset("data/staging/0287423/2-CTD", "CTD", "data/processed/2-CTD")
print(report["text"])
```

It returns `unmapped_columns`, `value_ranges` (min/max/units per variable),
`qc_flags` (distribution and non-zero percentage), `all_missing_variables`,
`missing_coordinates`, `contract_warnings` and `wod_dataset_routing` — plus `text`, a
readable rendering of all of it.

Read it for three things: a column in `unmapped_columns` you expected to keep, a range that
is physically wrong (which is how a unit error shows up), and a near-total flag rate — which
means a check is being misapplied, not that the data is bad.

`contract_for(path, dataset_type)` is the single-file version, for when you want one file's
contract without auditing the set.

---

## What neither check can do

Both check the machinery. Neither knows the science:

- A **mis-triaged folder** — currents or bathymetry forced through the profile path — audits
  clean, because every number in it is internally consistent.
- A **wrong time zone** shifts every cast by hours and stays inside every bound.
- A **basin-inappropriate range** passes, because range checks use the union of all 30 ocean
  basins (`range_area.msk` is not in the manual).

For those, cross-check against an independent source inside the accession — an underway GPS
track against station times, a sibling station list against in-file positions. See
[`knowledge/accession_field_guide.md`](../../knowledge/accession_field_guide.md).
