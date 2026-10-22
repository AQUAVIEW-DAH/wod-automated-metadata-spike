# Phase 3 — Conversions and QC

Convert values to WOD standard units, then, only if asked, flag every level against the
manual's checks.

Both functions take `(DataFrame, mapping_contract)` and return `(DataFrame, summary)`. Run
them in this order; QC thresholds assume standard units.

```python
from wod_etl.qc_transforms import convert_units_and_types, apply_qc_auditing

df, transform_log = convert_units_and_types(df, contract)
df, qc_summary    = apply_qc_auditing(df, contract)      # opt-in; see below
```

Nothing is deleted. Bad values are flagged and kept.

---

## WOD QC is off by default

`process_accession` calls `apply_qc_auditing` only when asked. QC is applied downstream by
the data centre.

```python
process_accession(url)                 # conversions only, no WOD flags
process_accession(url, run_qc=True)    # conversions + Table 12 flags
WOD_RUN_QC=1 uv run python ...         # same, for a whole session
```

The switch is `RUN_WOD_QC` in [`config.py`](../config.py).

The originator's own flags are always kept. A `Temperature_QC` column reaches the output
verbatim as `temperature_qc_originator`, on the submitter's scale (an originator's `1` may
mean good where WOD's `1` means a failed range check). The flag set used is secondary
header 96. See [Phase 2](../mapping/README.md#originator-quality-flags).

With QC off, `qc_summary` is `{"wod_qc": "not run", ...}` and names the originator flag
columns kept. The validator then expects no `<var>_qc` and does not warn.

---

## `convert_units_and_types(df, contract, seawater_density_kg_m3=1025.0)`

Applies each column mapping's `conversion_needed`, and coerces every mapped column with
`pd.to_numeric(errors="coerce")` (unparseable text becomes NaN).

| `conversion_needed` | Transform | Constant source |
| --- | --- | --- |
| `feet_to_meters` | × 0.3048 | exact definition |
| `fathoms_to_meters` | × 1.8288 | exact definition |
| `fahrenheit_to_celsius` | (v − 32) × 5/9 | exact definition |
| `molar_rescale` | prefix rescale, then ÷ or × density if the basis changes | Table 3a target unit + assumed density |
| `ml_per_l_to_umol_per_kg` | × 1000/22.3916, then ÷ density | Table 3b p.19, molar volume of O₂ |
| `mg_per_l_to_umol_per_kg` | × 1000 / M(O₂), then ÷ density | Table 3b p.19, atomic weights |
| `none` | numeric coercion only | — |

`molar_rescale` reads `conversion_from` and `conversion_to` from the mapping entry, covering
every prefix and basis pair (`umol/kg → mmol/l` for DIC, `mmol/l → umol/kg` for oxygen,
`nmol/l → nmol/kg` for noble gases). Without both it refuses and warns.

The log records each constant and its source, which is written to the NetCDF global
attributes:

```json
{"transformations_applied": [
   {"column": "Oxygen (ml/l)", "wod_code": 3, "conversion": "ml_per_l_to_umol_per_kg",
    "from": "ml/l", "to": "umol/kg", "constant": 22.3916, "constant_units": "l/mol",
    "provenance": "molar volume of O2 at STP, WOD Table 3b p.19 (Garcia and Gordon, 1992); then ..."}],
 "warnings": ["Refused molar-to-mass conversion on 'Chlorophyll (umol/l)' (WOD code 11): ..."],
 "seawater_density_kg_m3": 1025.0,
 "seawater_density_provenance": "assumed constant 1025 kg/m3; not specified by WOD Table 3b; ..."}
```

- **Density is an assumption.** Table 3b gives no seawater density. 1025 kg/m³ is a
  conventional stand-in, recorded in the output whenever used. Pass
  `seawater_density_kg_m3=` if you have a real value.
- **Per-volume variables.** Chlorophyll (11), alkalinity (17) and DIC (21) are per-volume in
  Table 3a. A molar-to-mass conversion on them is skipped and reported in `warnings`.
- An `iso_date` column is added when the contract has year/month/day.

---

## Which checks run depends on the kind of data

- **Non-profile datasets get no vertical checks.** For `SUR`, `apply_qc_auditing` skips
  depth duplication/inversion, excessive gradient and excessive inversion, and sets
  `summary["vertical_checks_applied"] = False` with a note. Surface data is "treated
  differently than profile data" (manual p.37); applied to a track at constant 0 m, the
  profile checks would flag nearly every level.
- **Range checks only** for air pressure (29), latitude (30), longitude (31) and Julian
  year-day (32), in any dataset: they "were not quality controlled beyond basic data range
  checks" (p.49).
- **Range checks always run.**

If most levels are flagged, suspect the check before the data.

---

## `apply_qc_auditing(df, contract, high_resolution=None)`

Runs the manual's observed-level checks (pp. 52–56) and appends flag columns.

```python
df, summary = apply_qc_auditing(df, contract)
# adds: <depth>_qc_flag  and one <var>_qc_flag per measured column
```

```json
{"total_levels": 6, "high_resolution": false, "min_gradient_depth_diff_m": 0.0,
 "depth_flags": {"0": 5, "1": 1},
 "flags_assigned": {"Temp_C": {"0": 1, "3": 2, "5": 1, "7": 1, "9": 1}},
 "checks_applied": ["depth_inversion_duplication", "broad_range_by_depth",
                    "excessive_gradient_MGV", "excessive_inversion_MIV", "zero_sensitivity"],
 "notes": ["Range bounds are the union across all 30 ocean basins at each depth. ..."]}
```

`high_resolution` defaults to true for CTD and XBT and applies the manual's 3.0 m minimum
depth difference when computing gradients.

### The checks

**1. Depth inversion and duplication** (p.52). Flags the second reading:

- the second of two successive depths is shallower → flag it;
- three successive depths shallower than an earlier one → flag every subsequent level (two
  concatenated profiles);
- two successive depths equal → flag the second.

**2. Broad range** (p.53, Appendix 11). Bounds vary by depth. `range_at_depth(code, z)` uses
the nearest listed level (the shallower on a tie); the deepest entry covers everything below.

**3. Excessive gradient and inversion** (p.54, Table 13). `gradient = (v2 − v1) / (z2 − z1)`,
with different thresholds per direction:

| Direction | Threshold | Flag |
| --- | --- | --- |
| negative (decrease with depth) | MGV, maximum gradient value | 3 |
| positive (increase with depth) | MIV, maximum inversion value | 2 |

For example, temperature MGV is 0.700 and MIV 0.300. Both tighten below 400 m (deep salinity
drops from 9.000 to 0.050). `gradient_limits(code, z)` returns the applicable set. Each
level is checked against both neighbours, so one level can fail both (flag 5).

**4. Zero-sensitivity.** A value of exactly 0 whose gradient exceeds `MGV × ZSI` is flagged 4
(likely a zero used as missing value).

**5. Depth-error propagation.** A level at a flagged depth is marked as an inversion failure.

### Two flag scales

Both are from Table 12; the same integer means different things in each.

`<depth>_qc_flag` (depth scale):

| | |
| --- | --- |
| 0 | accepted |
| 1 | duplicate or inversion in recorded depth |
| 2 | density inversion *(not currently assigned; see limitations)* |

`<var>_qc_flag` (observed level scale):

| | |
| --- | --- |
| 0 | accepted |
| 1 | range outlier |
| 2 | failed inversion check (MIV) |
| 3 | failed gradient check (MGV) |
| 4 | zero-sensitivity |
| 5 | combined gradient and inversion |
| 6 | range and inversion |
| 7 | range and gradient |
| 9 | range, gradient and inversion |

Flag 8 (range + questionable data) is defined by the manual but not assigned here;
questionable-data review is a manual curation step.

### Interpreting a result

Non-zero flags do not make output invalid. Report the distribution from `flags_assigned`;
do not discard levels. If `notes` says no depth column was found, only surface range bounds
were checked.

---

## Limitations

- **Range bounds are the union of all 30 ocean basins** at each depth. Basin assignment needs
  `range_area.msk` from the WOD website (not in the PDF), so bounds are permissive (e.g. at
  3000 m the temperature envelope reaches 34 °C because of the Red Sea).
- **Density checks are not implemented.** Depth flag 2 is never assigned.
- **Standard-level interpolation** (Reiniger-Ross, p.56) is out of scope; output is at
  observed levels.

---

## Next

[Phase 4 — Writers](../writers/README.md) turns the flagged DataFrame into NetCDF.
