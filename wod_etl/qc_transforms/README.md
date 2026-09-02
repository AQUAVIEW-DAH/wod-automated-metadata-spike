# Phase 3 — Conversions and QC

**Put values into WOD standard units, then — only if asked — flag every level against the
manual's checks.**

Both functions take `(DataFrame, mapping_contract)` and return `(DataFrame, summary)`. Run
them in this order — QC thresholds assume standard units.

```python
from wod_etl.qc_transforms import convert_units_and_types, apply_qc_auditing

df, transform_log = convert_units_and_types(df, contract)
df, qc_summary    = apply_qc_auditing(df, contract)      # opt-in; see below
```

**Nothing is ever deleted.** Bad values are flagged and kept; the manual excludes flagged
levels later, at interpolation time.

---

## WOD QC is OFF by default

`process_accession` does not call `apply_qc_auditing` unless you ask it to. Quality control
is applied downstream by the data centre, and a Table 12 flag written here is one more thing
to reconcile against the one that matters.

```python
process_accession(url)                 # conversions only, no WOD flags
process_accession(url, run_qc=True)    # conversions + Table 12 flags
WOD_RUN_QC=1 uv run python ...         # same, for a whole session
```

The switch lives in [`config.py`](../config.py) as `RUN_WOD_QC`. Everything below still
works exactly as documented — it is opt-in, not removed.

**The originator's own flags are carried through either way.** If the file has a
`Temperature_QC` column, its values reach the output verbatim as
`temperature_qc_originator`, on the submitter's scale, never translated: an originator's `1`
may mean *good* where WOD's `1` means a failed range check. WOD's field for *which* flag set
was used is secondary header 96. See [Phase 2](../mapping/README.md#originator-quality-flags).

With QC off, `qc_summary` is `{"wod_qc": "not run", ...}` naming the originator flag columns
that were kept. A missing `<var>_qc` in the output is then expected, and the validator says
so rather than warning that flags are absent.

---

## `convert_units_and_types(df, contract, seawater_density_kg_m3=1025.0)`

Applies the conversions named in each column mapping's `conversion_needed`, and coerces
every mapped column with `pd.to_numeric(errors="coerce")` so unparseable text becomes NaN
rather than raising.

| `conversion_needed` | Transform | Constant source |
| --- | --- | --- |
| `feet_to_meters` | × 0.3048 | exact definition |
| `fathoms_to_meters` | × 1.8288 | exact definition |
| `fahrenheit_to_celsius` | (v − 32) × 5/9 | exact definition |
| `molar_rescale` | prefix rescale, then ÷ or × density if the basis changes | Table 3a target unit + assumed density |
| `ml_per_l_to_umol_per_kg` | × 1000/22.3916, then ÷ 1.025 | **Table 3b p.19** molar volume of O₂ |
| `none` | numeric coercion only | — |
| `mg_per_l_to_umol_per_kg` | × 1000 / M(O₂), then ÷ density | Table 3b atomic weights, p.19 |

`molar_rescale` reads `conversion_from` and `conversion_to` from the mapping entry, so one
branch covers every prefix and basis pair: `umol/kg → mmol/l` for DIC, `mmol/l → umol/kg`
for oxygen, `nmol/l → nmol/kg` for the noble gases. Without both endpoints it refuses and
warns rather than guessing a direction.

The returned log records the constant and where it came from, so provenance survives into
the NetCDF global attributes:

```json
{"transformations_applied": [
   {"column": "Oxygen (ml/l)", "wod_code": 3, "conversion": "ml_per_l_to_umol_per_kg",
    "from": "ml/l", "to": "umol/kg", "constant": 22.3916, "constant_units": "l/mol",
    "provenance": "molar volume of O2 at STP, WOD Table 3b p.19 (Garcia and Gordon, 1992); then ..."}],
 "warnings": ["Refused molar-to-mass conversion on 'Chlorophyll (umol/l)' (WOD code 11): ..."],
 "seawater_density_kg_m3": 1025.0,
 "seawater_density_provenance": "assumed constant 1025 kg/m3; not specified by WOD Table 3b; ..."}
```

### The density constant is an assumption, not a standard

Table 3b supplies atomic weights and the molar volume of O₂ — **no seawater density**.
1025 kg/m³ is a conventional stand-in used when in-situ density is unavailable. It is
recorded in the output whenever a conversion used it. Pass a real value via
`seawater_density_kg_m3=` if you have one.

### Per-volume variables are refused

Chlorophyll (11), alkalinity (17) and DIC (21) are per-volume in Table 3a. A molar-to-mass
conversion on them is skipped and reported in `warnings` — converting would move them *off*
the WOD standard.

**Also produced** — an `iso_date` column when the contract carries year/month/day.

---

## Which checks run depends on the kind of data

The manual applies different QC to different datasets, and the difference is not cosmetic.

**Non-profile datasets get no vertical checks.** `apply_qc_auditing` reads
`wod_dataset_code` from the contract; for `SUR` it skips depth duplication/inversion,
excessive gradient and excessive inversion, and reports
`summary["vertical_checks_applied"] = False` with a note saying why.

Surface-only data is "treated differently than profile data" (manual p.37): each
observation is an independent surface sample, so a vertical relationship between successive
rows does not exist. Run the profile checks on one anyway and a track sitting at a constant
0 m reads as a duplicate depth at *every* level — which then marks every variable as an
inversion. On a real 71,898-observation underway cast that flagged 85–100% of every
variable, all of it spurious.

**Four variables get range checks only, in any dataset.** Air pressure (29), latitude (30),
longitude (31) and Julian year-day (32) "were not quality controlled beyond basic data range
checks" (p.49) — they identify the observation rather than being measurements to audit.

**Range checks always run.** They are the one check that means the same thing everywhere.

If you see a QC distribution where most levels are flagged, suspect the check before the
data. Genuine bad data is usually a small percentage; a near-total flag rate almost always
means a check is being applied to something it does not describe.

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

`high_resolution` defaults to true for CTD and XBT datasets and enables the manual's 3.0 m
minimum depth difference when computing gradients, so a tiny Δz cannot inflate a gradient.

### The checks

**1. Depth inversion and duplication** (p.52) — three rules, all flagging the *second*
reading rather than trying to judge which is right:

- second of two successive depths is shallower → flag it
- three successive depths shallower than an earlier one → flag **every** subsequent level
  (the signature of two profiles concatenated with no separating header)
- two successive depths equal → flag the second

**2. Broad range** (p.53, Appendix 11) — bounds vary **by depth**. `range_at_depth(code, z)`
exposes the lookup: nearest listed level, shallower one on a tie, and the deepest entry
covers everything below it.

**3. Excessive gradient and inversion** (p.54, Table 13) — `gradient = (v2 − v1) / (z2 − z1)`,
with **two different thresholds in opposite directions**:

| Direction | Threshold | Flag |
| --- | --- | --- |
| negative (decrease with depth) | **MGV** maximum gradient value | 3 |
| positive (increase with depth) | **MIV** maximum inversion value | 2 |

MGV ≠ MIV — temperature is MGV 0.700 but MIV 0.300. Both tighten below 400 m; deep salinity
drops from 9.000 to 0.050. `gradient_limits(code, z)` returns the applicable set.

Each level is checked against **both** neighbours, so one level can fail a decrease on one
side and an increase on the other — that is what produces flag 5.

**4. Zero-sensitivity** — a value of exactly 0 whose gradient exceeds `MGV × ZSI` is flagged
4, distinguishing a true zero from a zero used as a missing-value marker.

**5. Depth-error propagation** — a level sitting at a flagged depth cannot be trusted
vertically, so it is marked as an inversion failure rather than passing as accepted.

### Two flag scales — do not mix them

Both are from Table 12. The same integer means different things in each; that is the
manual's design.

`<depth>_qc_flag` — **depth scale**:

| | |
| --- | --- |
| 0 | accepted |
| 1 | duplicate or inversion in recorded depth |
| 2 | density inversion *(not currently assigned — see limitations)* |

`<var>_qc_flag` — **observed level scale**:

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

Flag 8 (range + questionable data) is defined by the manual but has no corresponding check
here — questionable-data review is a human curation step.

### Interpreting a result

A file full of non-zero flags is still valid output. Report the distribution from
`flags_assigned`; do not discard levels. If `notes` says no depth column was found, the
inversion, gradient and depth-dependent range checks were all skipped — the file was only
range-checked at the surface bounds.

---

## Limitations

- **Range bounds are the union across all 30 ocean basins** at each depth. Basin assignment
  needs `range_area.msk` from the WOD website, which is not in the PDF. Bounds are
  depth-dependent but permissive: at 3000 m the temperature envelope reaches 34 °C because
  the manual's Red Sea column allows it.
- **Density checks are not implemented.** Depth flag 2 is defined but never assigned;
  seawater density is not computed.
- **Standard-level interpolation** (Reiniger-Ross, p.56) is out of scope — this project
  produces intermediate observed-level files, the stage before that.

---

## Next

[Phase 4 — Writers](../writers/README.md) turns the flagged DataFrame into NetCDF.
