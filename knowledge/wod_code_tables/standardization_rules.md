# WOD standardization rules

From the World Ocean Database 2023 User's Manual (`knowledge/wod_readme.pdf`); page numbers
refer to it. The machine-readable tables are the `*.json` files beside this one.

## Which table is which

| Table | Page | Contents |
| --- | --- | --- |
| Table 2 | 17 | The eleven datasets |
| Table 3a | 18 | Measured variables: codes, standard units, datasets |
| Table 3b | 19 | Unit constants (atomic weights, molar volume of O₂) |
| Table 4a/4b | 23–27 | Secondary headers (cast metadata) |
| Table 5a/5b | 28–29 | Variable-specific headers (metadata of one variable) |
| Table 6a/6b | 30–31 | Biological headers (how a plankton sample was taken) |
| Table 7a/7b, 8, 9 | 32–36 | Taxa-Record fields, taxa code ranges, plankton grouping |
| Table 11 | 54 | Variables that get range checks |
| Table 12 | 55 | QC flags: five separate scales |
| Table 13 | 56 | Gradient (MGV) and inversion (MIV) factors |
| Appendix 1 | 68 | Country codes |
| Appendix 9 | 81 | Standard levels |
| Appendix 11 | 86–113 | Acceptable ranges by depth and basin |

Not extracted, because the pipeline does not use them: Tables 1a/1b (product volumes, cast
counts), 4c (names of WMO code tables), 10a–c (WOD's native ASCII layout), Appendix 6 (plankton
grouping list; WOD assigns it), 7 (WMO squares), 9 and 10 (standard levels and interpolation
limits; no interpolation here), 13 (basins for objective analysis).

## Datasets (Table 2)

OSD, CTD, MBT, XBT, SUR, APB, MRB, PFL, DRB, UOR, GLD. XCTD is not a dataset: resolution decides,
as for CTD (p.19).

- Depth increment < 2 m: high resolution, CTD.
- Depth increment ≥ 2 m: low resolution, OSD.

## Variables (Table 3a)

29 codes between 1 and 43. Values are converted to the standard unit, not relabelled.

Common ones: `1` temperature (°C), `2` salinity, `3` oxygen (µmol/kg), `4` phosphate, `6`
silicate, `8` nitrate and nitrate+nitrite (µmol/kg), `9` pH, `11` chlorophyll (µg/l), `17`
alkalinity (mmol/l), `20` pCO₂ (µatm), `21` DIC (mmol/l), `24` transmissivity (m⁻¹), `25`
pressure (dbar).

- Nutrients are per mass (µmol/kg); alkalinity, DIC and chlorophyll are per volume (p.15).
- Codes 30–32 (latitude, longitude, Julian year-day) are measured variables only in SUR, APB
  and UOR, where each observation has its own position.
- Depth is the vertical coordinate, not a variable; code 25 is pressure.
- Secondary-header IDs are a separate namespace: ID 29 is Probe Type, variable 29 is Air
  pressure.

## What a profile cannot carry

Table 3a is the whole list. A file whose only measurements are outside it is not a profile.

| Data | Where it goes |
| --- | --- |
| Plankton biomass and taxa | Taxa-Records with an upper and lower depth (Tables 6a, 7a; [`../plankton_biomass.md`](../plankton_biomass.md)). Codes `14`, `-5002`, `-5006` are PI-assignment codes, not variables (p.30). |
| Bottom depth, soundings | Secondary header 10 (Bottom Depth), cast metadata |
| Nitrite | Excluded from the online database (p.49). Code 8 is nitrate and nitrate+nitrite. |
| Ammonium, CDOM, turbidity, PAHs, microplastics, eDNA, currents | No WOD code |
| Conductivity, sigma-t, sound velocity, potential and conservative temperature | No WOD code. Potential and conservative temperature are derived and are never code 1. |
| Absolute, reference or preformed salinity (TEOS-10) | Not the practical salinity code 2 stores. |
| Size-fractionated chlorophyll | Only total chlorophyll maps to code 11 |

`explain_column()` in `wod_etl.mapping` lists every such column and why.

## Originator quality flags

Kept verbatim in their own variable, never translated: an originator's `1` may not mean WOD's
`1`. Secondary header 96 names the flag set (p.13, p.58).

## Primary header and character data

- Country (Appendix 1, p.68): two-character code, mostly ISO 3166-1 alpha-2. A ship's country
  is its flag; a float, mooring or glider takes its operator's (p.17). `99` is unknown.
- A cruise identifier is country code + cruise number, unique within the country. No cruise
  information: cruise number 0 (p.17).
- Character data: the originator's cruise and station codes, and the PI as a code from
  `primary_investigator_list` (p.30, Table 10b).
- Code lists online: `country_list`, `primary_investigator_list` and one per coded header
  (`code_table_links.json`).

## Surface data (SUR, p.37)

- One cast per cruise; each observation has its own latitude, longitude and Julian year-day.
- The primary header repeats the first observation's position and time.
- Depth is 0 m by the dataset's definition (p.20).
- Julian year-day 0.00 is 00:00 on 1 January of the first observation's year. The worked
  example on p.37 implies one more; this project uses the definition and says so.
- WOD splits a cruise of more than 24,000 observations into casts (p.17). Here, streams logged
  on one clock are merged into one cast, and each separate track file is its own cast.

## Quality control (pp.51–56)

Every check sets a flag; nothing is deleted.

1. Depth inversion and duplication (p.52): the second of two depths that do not increase is
   flagged 1; if three in a row are shallower than the first, every later depth is flagged.
   A cast recorded deepest first is reversed.
2. Range check: by depth and by basin, up to 30 basins (Appendix 11). Outliers get flag 1.
   Without the WOD basin masks, the union across basins is used, which is permissive.
3. Gradients (Table 13): `(v2 - v1) / (z2 - z1)`. A decrease beyond MGV is flag 3; an
   increase beyond MIV is flag 2. Separate factors above and below 400 m; high-resolution
   instruments use at least 3 m of depth difference; a fall to zero beyond MGV × ZSI is flag 4.
4. Density inversions (p.54) estimate data quality; they do not flag T and S.
5. Interpolation to standard levels (p.56) skips flagged values. Not done here.

Surface tracks get no vertical checks: depth duplication, gradient and inversion describe a
profile. Air pressure, year-day, latitude and longitude get range checks only (p.49).

## Flag scales (Table 12)

Five scales; never mix them in one column. Depth flags: 0 accepted, 1 duplicate or inversion,
2 density inversion. Observed-level flags: 0 accepted, 1 range, 2 inversion, 3 gradient, 4
bullseye or zero gradient, 5–9 combinations. The cast, standard-level and biological scales are
in `table_12_qc_flags.json`.

## Units (Tables 3a, 3b)

- Per-volume to per-mass uses the sample's density, or a constant 1025 kg/m³ when it is not
  reported (p.15). The output records which.
- Oxygen to µmol/l: ml/l × 1000 / 22.3916 (molar volume), mg/l × 1000 / 31.9988 (2 × 15.9994,
  Table 3b); then ÷ density for µmol/kg.
- Alkalinity and DIC stay per volume: µmol/kg for them is a unit to convert, not the standard.
- Equal to the standard, so left alone: chlorophyll in mg/m³, air pressure in hPa,
  oxygen and nutrients already in µmol/kg. Fluorescence in RFU or volts is not a concentration.
- Chlorophyll in mg/l is × 1000. In a molar unit it is not written: Table 3b gives no molar
  mass to convert it to µg/l.
- The originator's unit goes in variable header 8 (`v_8_orig_units`) only when it differs
  from the standard; a depth unit other than metres goes in secondary header 95.
- Unit text is compared after folding typographic forms (`µmol kg−1`, `μmol/kg`).

## XBT (p.61)

Observed-level depths are kept as submitted. Secondary header 33 records the depth-time
equation: 0 the original (Sippican), 1 a corrected one (Hanawa et al. 1995 or later). Header 54
says whether a correction is needed. A probe stated as a model (`T-7`) is category XBT in
header 29 and the model in variable header 5.
