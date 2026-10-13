# The mapping contract, the metadata, and the flags

**Read this when a column behaves oddly, when you are about to add an alias, or when you
need to know where a piece of metadata went.** The SKILL.md body carries the decisions that
are expensive to get wrong; this is the rest of them.

---

## The contract `generate_field_mapping` produces

Everything downstream reads it. Six keys matter:

| Key | What it holds |
| --- | --- |
| `column_mapping` | raw column → WOD code, target unit, conversion required, plus the originator's unit and scale kept verbatim |
| `unmapped_columns` | **silently dropped from the output.** Check before trusting a run |
| `dropped_columns` | the subset refused **on purpose**, each with a reason and a route. Read this before reaching for an alias |
| `missing_required_coordinates` | a file with `latitude`, `longitude` or `depth` here fails validation later; `time` is only a warning |
| `ready_for_transform` | false means do not proceed without looking |
| `warnings` | unresolved dataset, duplicate variables, a variable not stored in this dataset |

Plus the three metadata keys, below.

### Adding an alias, or renaming a column

If a column you expect is in `unmapped_columns` and **not** in `dropped_columns`, you have
two options and they are not interchangeable. Never touch `data/raw/` with either — the raw
file is the provenance record.

| | Alias | Rename |
| --- | --- | --- |
| Where | `VARIABLE_ALIASES` in `wod_etl/mapping/query_wod_spec.py` | `rename_columns` in `wod_etl.staging`, on the staged copy |
| Scope | every accession, forever | this one file |
| Touches data | no | the staged header only |
| Use when | the vendor's name will recur (`T090C`, `PrDM`) | *this* file's header is wrong, missing, or missing its unit |

**Renaming, when you do it:**

```python
from wod_etl.staging import rename_columns
cols, audit = rename_columns(cols, {"latitude": "Latitude"})   # -> LAT becomes Latitude
```

The key can be a human's name for the column — `resolve_column` finds that the latitude
column is spelt `LAT`. It **refuses to guess between two**: `t090C` and `t190C` both resolve
to code 1 (primary and secondary sensor), so "the temperature column" is ambiguous and comes
back as such rather than picking one.

The column name carries three facts and **nothing else supplies any of them** — the WOD
code, the originator's unit, the measurement scale. So these are refused by default, with
the fix printed, and nothing is applied when one fails:

- `Temp (deg F)` → `Temperature` — the unit is gone and the values read as already-Celsius
- `Temperature [ITS-90, deg C]` → `Temperature (deg C)` — the scale is gone
- `T090C` → `Theta` — code 1 becomes no code

Two renames only ever add, and are always allowed: naming `col_0` → `Depth (m)` from a
format description, and folding a unit in (`DO` → `DO (mg/L)`), which is the one rename that
changes behaviour because it makes the conversion detectable.

**Do not canonicalize column names as a habit.** `suggest_column_names` will propose it, but
the output NetCDF variable name comes from the WOD code, not the column name, so
canonicalising changes no output — and on a Sea-Bird cast it would collapse `t090C` and
`t190C` onto one name and drop a sensor. Report `audit["renames"]`: a rename is a claim
about the source.

Instrument exports are the usual cause. Sea-Bird writes `T090C`, `Sal00`, `PrDM`,
`Sbeox0ML/L`, `FlSP` and `DepSM` rather than English names, and the trailing digits are the
sensor index. Those are aliased already; other vendors are not. `DepSM` is the one to watch —
an unmapped depth column yields a file with no depth axis.

Matching normalises first: bracketed unit text is stripped, everything after a comma is
dropped, the rest lowercased to underscore words. Exact alias wins; otherwise the **longest**
whole-word alias wins, which is why `Air_Temperature` reaches code 26 rather than being
captured by `temp`.

### Names that look like variables and are not

`explain_column(name)` is the single registry, and it is what `match_variable` consults to
*refuse* a match — so a refusal and its explanation cannot drift apart.

| Column | Why there is no code |
| --- | --- |
| **potential temperature** | Derived, not measured. See below — this is the sharpest case |
| conductivity, sigma-t / sigma-theta, sound velocity | not in Table 3a |
| nitrite alone | excluded from the online database (p.49). Code 8 is nitrate **and** nitrate+nitrite, a different measurement |
| ammonium, turbidity, CDOM | not in Table 3a. CDOM in m⁻¹ is *not* code 24 — transmissivity is a different measurement |
| size-fractionated anything | one chlorophyll code exists, so only a total maps |
| bottom depth, water depth, sounding | not a variable at all: **secondary header 10**, claimed by `map_metadata` |

**Potential temperature is not an alias of temperature.** Theta is in-situ temperature
adjusted for adiabatic compression. With both columns present, aliasing it to code 1 makes a
cast claim Temperature twice; with only theta present, the cast silently reports the wrong
quantity. If theta is all you have, convert it to in-situ during staging — it needs pressure
and salinity — and record the derivation.

`Bottom Depth`, `Water Depth`, `Sounding`, `Station Depth` and `Sensor Depth` are not the
depth axis either, however much the name suggests it. Treating one as the vertical coordinate
sets every level of the cast to the same value.

---

## Units and scales

**Unit detection reads the full raw column name and nothing else.** A units row underneath
the header is invisible to it, so fold units into the name when staging (`DO (mg/L)`, not
`DO`).

**Check the conversion direction against Table 3a.** Alkalinity and DIC are stored
per-volume while nutrients are per-mass, so "already µmol/kg" is right for one and a 1000×
error for the other. Two units need nothing and must not be "corrected": `mg m⁻³` for
chlorophyll (identical to µg/l) and `hPa` for air pressure (identical to mbar).

**The unit is kept, not just used.** Whatever the originator wrote reaches the output as
`originator_units` — variable specific second header 8 — because a conversion is only
auditable against the unit the source declared.

**A scale is not a unit.** `[ITS-90, deg C]` and `(PSS-78)` state which standard the numbers
are on; that is header 3, recorded separately. A temperature is ambiguous without it.

---

## Metadata: three keys, three sources

A cast is not only its numbers. The probe, the ship, the bottom depth, the sampling gear, the
wind, the scale, the originator's units and their flags are all things WOD has a field for —
and all things that resolve to no Table 3a code, so anything mapping only measurements drops
every one. `generate_field_mapping` runs `map_metadata` for you.

| Contract key | What it holds |
| --- | --- |
| `secondary_headers` | Table 4a **cast** metadata, from header lines and from metadata columns, each with the URL of its published code list and a `code_lookup` |
| `column_mapping[].variable_specific_headers` | Table 5a **per-variable** metadata: originator's units (8), scale (3), instrument (5), nitrate+nitrite (17) |
| `character_data` | the originator's cruise code and PI (manual p.30) -- **not** secondary header 3 |
| `originator_flags` | the submitter's own flag columns, paired with the variables they flag |
| `provenance_notes`, `source_files` | the staged header's `# Note:` and `# Source:` lines -- they become `comment` and `source_files_raw` |

Metadata arrives four ways, all handled:

1. a `Key: value` header line — `# Probe type: T-7` → header 29; `# Instrument: SBE 911plus`
   → header 5 on **every** measured variable; `# Cruise: SHU1505` → `character_data`
2. a **delimited** header line — `Platform,,5423.,OCL code,KAHAROA` → header 3
3. a **whole column** constant down the cast — a `Bottom Depth (m)` column → header 10
4. part of a measurement column's own name — `[ITS-90, deg C]` → scale *and* originator unit

### The delimited block, and its variable column

WOD's own CSV export writes the block as columns, and originators copy it:

```
Platform     ,,           5423.,OCL code,KAHAROA (R/V; call sign ZM7552),
Instrument   ,Temperature,  464.,WOD code,CTD: SBE 37-IM MicroCAT,
uncalibrated ,Salinity,       1.,,,
```

Column 0 key, column 2 value, then a unit **or** a code system, then a description.
**Column 1 is the variable** — empty means the key describes the cast (Table 4a), named
means it describes one variable (Table 5a) *and says which*, so it is attached rather than
guessed.

`value_is_already_a_code` marks a value that is already a published code (`464.` under
`WOD code`). Do not look it up and do not treat it as a name — it is the answer.

### Three judgements the tool cannot make for you

- **A varying metadata column records no value.** A secondary header describes the whole
  cast, so a bottom depth changing down an underway track is withheld with the reason
  stated. If the variation is *between* casts, split the file per cast during staging.
- **`is_coded: false` is an answer, not a gap.** Bottom depth is metres, not a code.
- **A coded field holds a code, not a name.** Every coded value is looked up in WOD's
  published list (fetched once, cached with its retrieval date). Exactly one entry named that
  -> the code is written, the text kept beside it. Anything else -- two vessels called SHUNYO
  MARU, no entry for `deg C`, the list unreachable -- leaves the field unwritten and puts the
  text in `<field>_originator_text` with the reason. **Each of those needs a human choice**:
  report `coded_headers_not_resolved`, and resolve it in the staged header with the code.

Metadata columns and flag columns are **consumed**, not dropped. Neither appears in
`unmapped_columns`.

### `unresolved` — read it

Every key that looked like metadata and could not be placed, with its value, the line it came
from, and why: no WOD field matched, or it is a Table 5a header with no variable named, or it
lost a collision to another key claiming the same header, or its value column was empty.
`Key: value` lines are covered too, and every unplaced key reaches the NetCDF as
`metadata_not_placed`.

**A metadata key that vanishes silently is the same failure as a dropped column.** On
accession 0146739 the parser once reported three headers, all three wrong, and named none of
the fourteen keys it had skipped — which is exactly what `unresolved` exists to prevent.

---

## Quality flags

**WOD QC is off by default. Do not turn it on unless asked.** It is applied downstream, and a
Table 12 flag written here is one more thing to reconcile against the one that counts.
`process_accession(url, run_qc=True)` enables it.

**The originator's own flags are always kept.** A `Temperature_QC` column reaches the output
verbatim as `temperature_qc_originator`, paired with the variable it flags, on the
submitter's scale — **never translated to Table 12**. An originator's `1` may mean *good*
where WOD's `1` means a failed range check, so mapping one onto the other invents meaning.

WOD's field for *which* flag set was used is **secondary header 96**. When the file does not
name the set, that is recorded as unknown rather than guessed — say so in your report, and
ask the submitter if it matters.

So a missing `<var>_qc` is expected. `<var>_qc_originator` present and `<var>_qc` absent is
the normal shape; **neither** present means the file carried no quality information at all,
which is worth saying.

### If you do run WOD QC

Two scales, both Table 12. The same integer means different things in each — per the manual,
not a mistake:

- `z_qc` — depth: `0` accepted, `1` duplicate/inversion, `2` density inversion
- `<var>_qc` — observed level: `0` accepted, `1` range, `2` inversion, `3` gradient,
  `4` zero-sensitivity, `5` gradient+inversion, `6` range+inversion, `7` range+gradient,
  `9` range+gradient+inversion

Flags are recorded, never deleted. A file full of non-zero flags is still valid output;
report the distribution rather than discarding data.

**Known limitation, worth stating in any report that includes flags:** range checks use the
union of all 30 ocean basins at each depth, because assigning a cast to a basin needs
`range_area.msk` from the WOD website, which is not in the PDF. Bounds are depth-dependent
but more permissive than a basin-aware check.

---

Full API detail: [`wod_etl/mapping/README.md`](../../../wod_etl/mapping/README.md) and
[`wod_etl/qc_transforms/README.md`](../../../wod_etl/qc_transforms/README.md).
