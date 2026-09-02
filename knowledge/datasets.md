# The eleven WOD datasets

Manual §I.D, pp.17–21. Machine-readable names in
[`wod_code_tables/table_2_datasets.json`](wod_code_tables/table_2_datasets.json).

There are **exactly eleven**. Anything else — XCTD, STD, DBT, rosette, Niskin — is an
*instrument*, not a dataset, and has to be routed to one of these.

| Code | Holds | Typical source |
| --- | --- | --- |
| **OSD** | Bottle (discrete water samples), **low-resolution** CTD/STD/XCTD, some surface-only data, and **plankton** taxonomic and biomass measurements | Rosette/Niskin casts, reversing thermometers, net tows |
| **CTD** | **High-resolution** CTD and STD vs depth, and high-resolution XCTD | Sea-Bird and similar profilers |
| **MBT** | Temperature vs depth in the upper water column | Mechanical, Digital and Micro bathythermographs |
| **XBT** | Temperature vs depth; depth derived from fall-rate, not measured | Expendable bathythermographs |
| **SUR** | Surface-only data by any in situ means, along a horizontal path | Underway thermosalinograph, ship-track sampling |
| **APB** | Temperature (and salinity) from recorders attached to marine mammals | TTDR / CTD-SRDL on elephant seals |
| **MRB** | Temperature and salinity from **moored** buoys | TAO/TRITON, PIRATA, RAMA |
| **PFL** | Temperature, salinity, O₂, nitrate, pH, chlorophyll from **drifting profiling floats** | Argo core and BGC-Argo, Deep Argo |
| **DRB** | Surface **drifting** buoys and drifting floats with subsurface thermistor chains | GTSPP, Arctic buoy programmes |
| **UOR** | CTD probe on a **towed undulating** vehicle | SeaSoar and similar |
| **GLD** | Autonomous gliders on a saw-toothed trajectory | Slocum, Seaglider, Spray |

---

## Choosing between them

### Resolution decides CTD vs OSD — not the instrument

This is the rule people get wrong. Manual p.19:

> "All casts with a depth increment less than two meters are considered high-resolution CTD
> otherwise, the casts are considered as low-resolution CTD data. The low-resolution CTD data
> reside within OSD dataset."

- depth increment **< 2 m** → **CTD**
- depth increment **≥ 2 m** → **OSD**

The same rule governs XCTD, which is why `dataset_type="XCTD"` is a legitimate thing to pass
and getting `OSD` back is a correct answer rather than a bug. `resolve_dataset` will not guess
without an increment — it returns `unresolved` instead, and the pipeline measures the median
positive level spacing per cast so the routing is decided per cast rather than per run.

### The three buoy/float datasets are distinguished by motion

| | Moves vertically? | Drifts horizontally? |
| --- | --- | --- |
| **MRB** moored buoy | no — fixed instruments at set depths | no |
| **PFL** profiling float | yes — profiles up and down | yes |
| **DRB** drifting buoy | no — thermistor chain hangs below | yes |

A "profile" from MRB is a set of concurrent readings from instruments at different depths,
not an instrument descending (p.17).

### Discrete samples are OSD even when collected at the surface

A lab-analysed bottle sample is OSD — that is what OSD *is*. Reserve SUR for continuous
underway sensor streams. This matters beyond taxonomy: Table 3a stores several variables
(δ¹⁸O and the transient tracers) in **OSD only**, so routing discrete surface chemistry to
SUR silently discards them.

### Plankton is OSD, but not through the profile path

Table 2 lists plankton under OSD, which is true of the *dataset* and misleading about the
*record structure* — plankton lives in the Biological Header and Taxa/Biomass records, where
a measurement carries an upper and lower depth. See
[`plankton_biomass.md`](plankton_biomass.md) for the whole picture,
[`cast_structure.md`](cast_structure.md) for where it sits in a cast, and *What the profile
path cannot carry* in
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md).

---

## What counts as a cruise, a cast, a profile, a station

Manual p.17. These definitions decide how many output files you produce.

- **Cruise** — ISO 3166-1 country code **+** a cruise number unique only within that country
  code. Usually the ship's flag; for a float, buoy or glider, the country of the operating
  institute. No country information → code **99**. No identifiable cruise → cruise number
  **0**.
- **Profile** — measurements of one variable at discrete depths as an instrument moves
  vertically. For surface-only data the path is horizontal instead; for moored and drifting
  buoys nothing moves and a profile is a set of concurrent readings.
- **Cast** — one lowering. **Station** — one or more casts at one geographic location.

Two consequences worth remembering:

- **One cruise number covers the whole life** of a profiling float, moored buoy or drifting
  buoy. A glider deployment is one cruise.
- **In SUR a cast and a cruise are the same thing** (bar 27 historical cruises split for
  size). So separate underway instrument streams sampling the same clock belong in **one**
  cast, not one each.
