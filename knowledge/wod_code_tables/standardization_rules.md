# WOD Standardization Rules

Extracted from the **World Ocean Database 2023 User's Manual** (`knowledge/wod_readme.pdf`,
131 pages). Page numbers below refer to that PDF. The machine-readable tables live beside
this file as `table_*.json`.

---

## Which table is which

The manual's numbering is easy to get backwards. It is:

| Table | Page | Contents |
| --- | --- | --- |
| **Table 2** | 17 | **Dataset names** — the eleven WOD datasets |
| **Table 3a** | 18 | **Measured variables** — codes, standard units, which datasets carry them |
| Table 3b | 19 | Unit conversion constants (atomic weights, molar volume of O₂) |
| Table 4a | 23–24 | Secondary header variables (metadata codes) |
| Table 4b | 24+ | Descriptions of the Table 4a codes |
| Table 11 | 54 | Which variables get range checks, and which appendix holds the numbers |
| Table 12 | 55 | Quality control flag definitions — **five separate scales** |
| Table 13 | 56 | Maximum gradient (MGV) and inversion (MIV) factors |
| Appendix 9 | 81 | Standard vertical levels and depths |
| Appendix 11 | 86+ | The actual acceptable ranges, by depth and by basin |

---

## Datasets (Table 2)

Exactly **eleven**: OSD, CTD, MBT, XBT, SUR, APB, MRB, PFL, DRB, UOR, GLD.

**`XCTD` is not a dataset name.** XCTD data is routed by resolution, and the same rule
governs plain CTD:

- Depth increment **< 2 m** → high-resolution → **CTD**
- Depth increment **≥ 2 m** → low-resolution → **OSD**

So resolution, not instrument, decides the destination dataset. An XCTD cast cannot be
assigned to a dataset without inspecting its depth spacing.

---

## Variables (Table 3a)

29 codes, 1–43, non-contiguous. Standard units are mandatory — data must be converted into
them, not merely labelled.

Common ones: `1` Temperature (°C), `2` Salinity (dimensionless), `3` Oxygen (µmol/kg),
`4` Phosphate (µmol/kg), `6` Silicate (µmol/kg), `8` Nitrate and Nitrate+Nitrite (µmol/kg),
`9` pH, `11` Chlorophyll (µg/l), `17` Alkalinity (mmol/l), `20` pCO₂ (µatm),
`21` Dissolved inorganic carbon (mmol/l), `24` Transmissivity (m⁻¹), `25` Pressure (dbar).

Two things to watch:

- **Nutrients are per-mass (µmol·kg⁻¹), but alkalinity and DIC are per-volume (mmol·l⁻¹).**
  Do not convert every chemical variable to a mass basis.
- **Codes 30, 31, 32 are Latitude, Longitude and Julian year-day *as measured variables*** —
  used only by SUR, APB and UOR, where every observation carries its own position rather
  than inheriting one cast position.

There is no WOD variable code for depth itself. Depth is the vertical coordinate; `25` is
Pressure, a distinct measured variable.

### Secondary headers are a different namespace

Table 4a IDs are unrelated to Table 3a codes. ID `29` is *Probe Type*; variable code `29` is
*Air pressure*. Keep them in separate lookups. Secondary headers appear in WOD CSV output as
`s_<id>_<name>` (e.g. `s_1_accession`, `s_3_platform`).

---

## What the profile path cannot carry

Table 3a is the *whole* list of depth-dependent measured variables. A dataset whose only
measurements fall outside it cannot become a WOD profile, however oceanographic it is.
Decide this before doing any work on a file.

### It belongs in WOD, but not as a profile

| Data | Where it actually lives |
| --- | --- |
| **Plankton biomass and taxonomy** | Biological Header (§5, Tables 6a/6b, Appendix 4) plus Taxa-specific and Biomass Data records (§6, p.32). A measurement carries an **upper and lower depth**, not a level — which is what a net haul actually samples. Codes `14` (all plankton), `-5002` (zooplankton), `-5006` (phytoplankton) are **PI-assignment** codes in that section, not Table 3a variables. |
| **Bottom depth / bathymetry soundings** | Secondary header `10`, per-cast metadata. A sounding track has coordinates and a depth axis but no measured variable, so there is nothing to put on a profile. |

Table 2 listing "Plankton data" under OSD is what makes this confusing: the *dataset* is
right, the *record structure* is different.

**Chlorophyll is the exception that proves the rule.** Chlorophyll-a concentration at
discrete depths is Table 3a code `11` and is a genuine profile — a phytoplankton pigment
dataset goes through the normal path, while a zooplankton wet-weight dataset does not.

### It has no WOD home at all

Common submission columns with no Table 3a code — all must be reported as dropped, never
mapped to something adjacent:

- **Nitrite.** Deliberately excluded from the online database because the data were not
  examined for science quality (p.49). Note that code `8` is *Nitrate and Nitrate+Nitrite*,
  which is not the same thing.
- **Ammonium**, **CDOM absorption**, **turbidity**, **PAHs**, **microplastics**, **eDNA**.
  CDOM in m⁻¹ is *not* code `24` (transmissivity / beam attenuation) — different measurement.
- **Currents** (ADCP).
- From CTD exports: **conductivity**, **sigma-t / sigma-theta**, **sound velocity**,
  **potential temperature**. Potential temperature in particular must not be mapped to
  code `1`; a cast that already has in-situ temperature would then claim the code twice.
- **Size-fractionated** anything. There is one chlorophyll code, so only a total maps.

---

## Surface-only data is a different shape (§7a, p.37)

> "Surface-only data are treated differently than profile data. For such data, each cruise
> is presented as a single cast with depth, latitude, longitude, and Julian year-day
> associated with each set of measured values."

So a SUR cast is a **track, not a profile**:

- **One cast per cruise.** Separate underway instrument streams sampling the same clock
  belong in one cast, not one each.
- **Every observation carries its own position and time**, as measured variables `30`, `31`
  and `32` — this is the only place those codes are used.
- **The primary header repeats the first observation's** longitude, latitude and date/time.

Two consequences worth knowing before staging one:

- **Depth.** SUR is surface data by definition (p.20). Where no intake depth is stated,
  0 m is the dataset's own convention rather than an invented measurement — but say so.
- **Julian year-day is specified inconsistently.** P.37 defines year-day 0.00 as 00:00 on
  1 January of the year of the first measurement, then gives a worked example (00:00 on
  31 Dec 1965 → 365.00) implying a value one larger. The two cannot both hold. Pick one,
  record the choice in the output, and flag it for confirmation.

### QC depends on the kind of data, not just the variable

- **Non-profile datasets get no vertical checks.** Depth duplication/inversion, excessive
  gradient and excessive inversion all describe a vertical relationship that a surface
  track does not have. Applied anyway, a track sitting at a constant 0 m reads as a
  duplicate depth at every level, and therefore an inversion in every variable.
- **Air pressure, Julian year-day, latitude and longitude** "were not quality controlled
  beyond basic data range checks" (p.49) — they exist to identify the observation, not to
  be audited as measurements.
- Range checks still apply everywhere.

---

## Quality control procedure

The manual applies these in order (pp. 51–56). Every check writes an integer flag; nothing
is deleted.

### 1. Depth inversion and duplication (p. 52)

A **depth inversion** is an observation shallower than the one directly before it. A **depth
duplicate** repeats the previous depth. The *second* observation is always flagged — the
manual explicitly does not try to evaluate which reading is right.

- Second of two successive depths is shallower → flag that depth **1**
- Three successive depths shallower than the first → flag **every** subsequent depth **1**
  (this signals two profiles concatenated with no separating header)
- Two successive depths equal → flag the second **1**
- Correct depths → flag **0**

Afterwards, casts submitted deepest-first are sorted so the shallowest depth comes first.

### 2. Range check (pp. 52–53)

Observed values are compared against acceptable ranges that vary **by depth and by ocean
basin** — 102 standard levels across 16 basin columns (Appendix 11). Outliers get observed-
level flag **1**.

A single global min/max pair is not what the manual specifies. The widest surface bounds
(e.g. −2.10 to 35.00 °C) will pass a 25 °C reading at 3000 m, which the depth-specific range
would reject.

### 3. Excessive vertical gradient (pp. 53–54, Table 13)

```
gradient = (v2 - v1) / (z2 - z1)
```

where 1 is the current level and 2 the next.

Two **different** thresholds, in opposite directions:

| Direction | Threshold | Meaning | Flag |
| --- | --- | --- | --- |
| Negative gradient (decrease with depth) | **MGV** — Maximum Gradient Value | excessive gradient | **3** |
| Positive gradient (increase with depth) | **MIV** — Maximum Inversion Value | excessive inversion | **2** |

MGV ≠ MIV. Temperature is the clearest case: MGV 0.700 but MIV 0.300 — a symmetric
threshold is wrong in both directions.

Additional rules:

- **Separate values above and below 400 m.** Oxygen is 43.570 shallow, 32.677 deep;
  salinity drops from 9.000 to 0.050.
- **High-resolution instruments (HCTD, XBT) use a minimum depth difference of 3.0 m** when
  computing the gradient, to stop tiny Δz inflating the result.
- **Zero-sensitivity check (ZSI).** Distinguishes a true zero from a zero used as a
  missing-value marker: if a gradient decreasing to zero exceeds `MGV × ZSI`, flag **4**.
  Mostly relevant to historical nutrient data.

### 4. Density check (p. 54)

Density inversions are located on observed level data but are *not* used to flag temperature
and salinity for subsequent QC — they estimate data quality before interpolation. Depth flag
**2** records a density inversion.

### 5. Interpolation to standard levels (p. 56)

Uses a modified Reiniger and Ross (1968) scheme over the two nearest shallower and two
nearest deeper observations. **Anything flagged as a range outlier, excessive gradient,
inversion, or depth error is excluded from interpolation.** This is what the flags are
ultimately for.

---

## Flag scales (Table 12)

Five distinct scales. The same integer means different things in each — never mix them in
one column.

**Depth flags** (individual depth readings):

| | |
| --- | --- |
| 0 | accepted value |
| 1 | duplicates or inversions in recorded depth |
| 2 | density inversion |

**Observed level flags** (individual variable observations):

| | |
| --- | --- |
| 0 | accepted value |
| 1 | range outlier |
| 2 | failed inversion check |
| 3 | failed gradient check |
| 4 | "bullseye" flag and zero gradient check |
| 5 | combined gradient and inversion checks |
| 6 | failed range and inversion checks |
| 7 | failed range and gradient checks |
| 8 | failed range and questionable data checks |
| 9 | failed range and combined gradient and inversion checks |

The other three scales — entire cast, standard level, and biological (CBV, taxa code 27) —
are in `table_12_qc_flags.json`.

Note that flag `1` legitimately means *inversion* on the depth scale and *range outlier* on
the observed-level scale. Both are correct in their own column.

---

## Unit conversion (Table 3b)

The manual supplies atomic weights and the molar volume of O₂ at STP (22.3916 l/mol), used
for converting oxygen between volumetric and molar units.

It gives **no seawater density constant**. Converting µmol/l to µmol/kg requires in-situ
density; 1025 kg/m³ is a common stand-in when density is unavailable, but it is an
assumption introduced by the implementer, not a WOD-specified value. Record it in the output
metadata when used.

### Traps

Standard units are mandatory, so a unit that is merely *plausible* is a silent error. Four
that bite:

| Source unit | Variable | What must happen |
| --- | --- | --- |
| `mg/l` | Oxygen (3) | ÷ molar mass of O₂ (2 × 15.9994 g/mol), then ÷ density. Roughly a 30× error if missed. |
| `ml/l` | Oxygen (3) | × 1000 / 22.3916, then ÷ density. |
| `µmol/kg` | **Alkalinity (17), DIC (21)** | Still needs converting — their standard is mmol/l, **not** µmol/kg. A ~1000× labelling error if treated as already-standard. |
| `µmol/l` | Nutrients (3, 4, 6, 8) | ÷ density to reach µmol/kg. |

Units that need nothing, and must not be "corrected":

- `mg m⁻³` for chlorophyll — identical to the standard µg/l.
- `hPa` for air pressure — identical to the standard mbar.
- `ppb` from a chlorophyll fluorometer — conventionally read as µg/l; say so rather than
  scaling it.
- `µmol/kg` for oxygen and nutrients — already standard.

The rule underneath: **check the direction against Table 3a before converting.** Per-mass
and per-volume both appear in the standard units, and which one applies is per-variable.

---

## Cruise and position conventions (p. 17)

- A cruise identifier is **ISO 3166-1 country code + cruise number**. The cruise number is
  unique only within a country code.
- Country code is the flag of the ship; for floats, buoys and gliders it is the country of
  the operating institute.
- **No country information → code 99. No identifiable cruise → cruise number 0.**
- For profiling floats, moored buoys and drifting buoys, one cruise number covers the entire
  life of the platform. A glider deployment is one cruise.
- In SUR, a cast and a cruise are the same thing (except 27 cruises split in two for size).
