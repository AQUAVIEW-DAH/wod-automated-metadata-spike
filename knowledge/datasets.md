# The eleven WOD datasets

Manual §I.D, pp.17–21. Machine-readable names are in
[`wod_code_tables/table_2_datasets.json`](wod_code_tables/table_2_datasets.json).

XCTD, STD, DBT, rosette and Niskin are instruments, not datasets, and are routed to one of these.

| Code | Holds | Typical source |
| --- | --- | --- |
| OSD | Bottle (discrete water samples), low-resolution CTD/STD/XCTD, some surface-only data, and plankton taxonomic and biomass measurements | Rosette/Niskin casts, reversing thermometers, net tows |
| CTD | High-resolution CTD and STD vs depth, and high-resolution XCTD | Sea-Bird and similar profilers |
| MBT | Temperature vs depth in the upper water column | Mechanical, Digital and Micro bathythermographs |
| XBT | Temperature vs depth; depth derived from fall-rate, not measured | Expendable bathythermographs |
| SUR | Surface-only data by any in situ means, along a horizontal path | Underway thermosalinograph, ship-track sampling |
| APB | Temperature (and salinity) from recorders attached to marine mammals | TTDR / CTD-SRDL on elephant seals |
| MRB | Temperature and salinity from moored buoys | TAO/TRITON, PIRATA, RAMA |
| PFL | Temperature, salinity, O₂, nitrate, pH, chlorophyll from drifting profiling floats | Argo core and BGC-Argo, Deep Argo |
| DRB | Surface drifting buoys and drifting floats with subsurface thermistor chains | GTSPP, Arctic buoy programmes |
| UOR | CTD probe on a towed undulating vehicle | SeaSoar and similar |
| GLD | Autonomous gliders on a saw-toothed trajectory | Slocum, Seaglider, Spray |

## Choosing between them

### Resolution decides CTD vs OSD, not the instrument

Manual p.19:

> "All casts with a depth increment less than two meters are considered high-resolution CTD
> otherwise, the casts are considered as low-resolution CTD data. The low-resolution CTD data
> reside within OSD dataset."

- depth increment < 2 m → CTD
- depth increment ≥ 2 m → OSD

The same rule applies to XCTD, so XCTD can resolve to `OSD`. Without an increment,
`resolve_dataset` returns `unresolved` rather than guessing. The pipeline measures each cast's
median positive level spacing, so routing is per cast, not per run.

### The three buoy/float datasets are distinguished by motion

| | Moves vertically? | Drifts horizontally? |
| --- | --- | --- |
| MRB moored buoy | no: fixed instruments at set depths | no |
| PFL profiling float | yes: profiles up and down | yes |
| DRB drifting buoy | no: thermistor chain hangs below | yes |

WOD keeps moored records as daily averages: records submitted at 15-minute intervals were
"re-converted as daily averages" in 2021 (p.14, note 6). `wod-etl` builds one profile a day,
each instrument's daily mean at its depth ([`staging/moorings.py`](../wod_etl/staging/moorings.py)).
Each instrument's depth comes from:

- the pressure it records;
- a depth its file or file name states; or
- the mooring's description, whose positions along the line the pressure instruments turn into
  depths ([`staging/mooring_layout.py`](../wod_etl/staging/mooring_layout.py)).

An instrument none of these places is left out, not guessed.

### Discrete samples are OSD even when collected at the surface

A lab-analysed bottle sample is OSD; SUR is for continuous underway sensor streams. Table 3a
stores some variables (δ¹⁸O and the transient tracers) in OSD only, so routing discrete surface
chemistry to SUR discards them.

### Plankton is OSD, but not through the profile path

Table 2 lists plankton under OSD, but not as profiles: plankton lives in the Biological Header
and Taxa/Biomass records, where a measurement has an upper and lower depth. See
[`plankton_biomass.md`](plankton_biomass.md), [`cast_structure.md`](cast_structure.md) for
where it sits in a cast, and *What a profile cannot carry* in
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md).

## What counts as a cruise, a cast, a profile, a station

Manual p.17.

- Cruise: ISO 3166-1 country code + a cruise number unique only within that country code. The
  country is usually the ship's flag; for a float, buoy or glider, the operating institute's
  country. No country information → code 99. No identifiable cruise → cruise number 0.
- Profile: measurements of one variable at discrete depths as an instrument moves vertically.
  For surface-only data the path is horizontal. For moored and drifting buoys nothing moves,
  and a profile is a set of concurrent readings.
- Cast: one lowering. Station: one or more casts at one geographic location.

Consequences:

- One cruise number covers the whole life of a profiling float, moored buoy or drifting buoy.
  A glider deployment is one cruise.
- SUR is one cast per cruise: a cast and a cruise are the same thing (bar 27 historical cruises
  split for size). Separate underway instrument streams sampling the same clock belong in one
  cast.
