# What a WOD cast is made of

Manual §I.E, pp.21–43. A cast carries seven metadata descriptors. Five describe the cast; the
last two identify the measurements themselves.

| # | Section | Holds | Codes live in |
| --- | --- | --- | --- |
| 1 | Primary header | Date, time, position, ISO country code, cruise code, unique cast number, byte count, number and type of variables | fixed fields, p.22 |
| 2 | Secondary header | Meteorology, sea-floor depth, instrument, platform, institute, project | Table 4a, pp.23–24 |
| 3 | Variable-specific secondary header | Per-variable originator's units, scale, instrument, method | Tables 5a/5b; values in Appendix 3 |
| 4 | Character data + PI code | Originator's cruise and cast codes, Principal Investigator | p.30 |
| 5 | Biological header | How biological data were sampled: tow type, gear, preservative, weight/count method | Tables 6a/6b; values in Appendix 4 |
| 6 | Taxa-specific and biomass data | Plankton weights, volumes, concentrations, whole-sample or per taxon | Appendices 5–6 |
| 7 | Measured variables | Temperature, salinity, oxygen, nutrients, tracers, pressure vs depth | Table 3a, p.18 |

The appendices in the last column are pages of links, not tables: a field's values are a code
list published online, linked through PDF annotations that a text extraction loses. All 62 are
extracted into `wod_code_tables/code_table_links.json`; reach one with
`code_table_link("secondary_header", 29)`.

## Five numbering systems, and how to tell them apart

The manual reuses small integers across unrelated tables, a common source of error. The
appendices' prefixes tell them apart:

| Prefix | Section | Example |
| --- | --- | --- |
| `s_<id>_<name>` | secondary header | `s_9_weather_station`, `s_29_probe_type` |
| `v_<id>_<name>` | variable-specific secondary header | `v_3_scale`, `v_5_instrument` |
| `b_<id>_<name>` | biological header | `b_7_gear_and_flowmeter_codes` |
| `t_<id>_<name>` | taxonomic | `t_5_taxon_lifestage` |

Measured variables (Table 3a) have no prefix: they are the plain codes 1–43.

Collisions to watch for:

| Number | As a Table 3a variable | As a Table 4a secondary header |
| --- | --- | --- |
| 10 | *(unused)* | Bottom Depth (metres) |
| 24 | Transmissivity / beam attenuation | Dry Bulb Temperature |
| 25 | Pressure | Wet Bulb Temperature |
| 26 | Air temperature | Weather Conditions |
| 29 | Air pressure | Probe Type |

"Code 29" means nothing without its table. Keep separate lookups (`wod_variables()` and
`wod_secondary_headers()`) and never index one with the other's number.

## Where a given piece of information belongs

A column that is not a Table 3a variable often belongs in a header:

| Information | Goes to |
| --- | --- |
| Sea-floor / sounding depth | secondary header 10, per cast; not a measured variable |
| Wind speed/direction, barometric pressure, dry and wet bulb, cloud, sea state | secondary headers 16–28, per cast |
| Ship, institute, project, accession number | secondary headers 1–4 |
| Originator's station and cast identifiers | secondary headers 5, 7 and the character-data section |
| Probe type, recorder, calibration | secondary headers 29–32 |
| Originator's units and instrument for one variable | variable-specific secondary header |
| Net gear, mesh, tow type, preservative | biological header |

A secondary header is one value for the whole cast; a Table 3a variable is one value per
level. Per-observation wind cannot be a secondary header and Table 3a has no wind code, so an
underway wind series has nowhere to go. The exception: air temperature (26) and air pressure
(29) are per-level Table 3a variables, but only in SUR.

## The biological path is a different record shape

Sections 5 and 6 exist because plankton does not fit the profile model. Manual p.32:

> "The typical plankton cast … contains a taxonomic description, depth range (the upper and
> lower depth) of observation, the original measurements (e.g., abundance, biomass or volume),
> and all provided qualifiers."

A net haul integrates over a depth range, so a biomass value has an upper and a lower depth
instead of a level. One sample can produce several Taxa-Records: one per biomass type, depth
range and taxon.

For PI assignment, plankton is variable 14 (all plankton), −5002 (zooplankton) and −5006
(phytoplankton), p.30. These are not Table 3a codes; Table 3a has no plankton variable.

The profile path writes Table 3a profiles only and never forces plankton onto a level.
Plankton has its own path into the Biological Header and Taxa-Records, using the extracted
Tables 6a/6b and 7a/7b. [`plankton_biomass.md`](plankton_biomass.md) covers the Taxa-Record
model, the Table 8 code ranges, the Plankton Grouping Code, and what that path does and does
not do.

## Primary header notes

- Time and position each carry significant digits, total digits and the originator's
  precision. WOD preserves how precisely a value was reported, so do not silently round.
- A day of zero is legal. WOD keeps it in case better information appears (p.22). Do not
  "fix" it to day 1.
- The cast number is unique across the database. The cruise identifier is country code +
  cruise number (see [`datasets.md`](datasets.md)).

## How this maps onto the intermediate NetCDF

| WOD section | In the output file |
| --- | --- |
| Primary header | `lat`, `lon`, `time`, `date` and `GMT_time` (fill when no time of day) on the `casts` dimension; `cast_id` with `cf_role = profile_id` |
| Secondary header | `secondary_header_<id>_<name>` global attributes (per-cast variables when casts differ), keyed by Table 4a ID; a coded header holds the code, or the originator's text in `_originator_text` with the lookup result |
| Character data | `originator_cruise_id`, `principal_investigator` |
| Measured variables | one variable per WOD code on `z_obs`, each with `wod_variable_code` and the Table 3a standard unit |
| Depth | `z` on `z_obs`, `positive = down`, metres; the axis, not a variable |
| QC | `z_qc` and `<var>_qc`, Table 12 values with `flag_values`/`flag_meanings` |
| Variable-specific secondary header | `wod_variable_specific_header_<id>` attributes on each variable (originator's units, scale, instrument...), coded where the list has one match; plus `originator_units` and `measurement_scale` |
| Biological header, taxa/biomass | Not in a profile file. Written by `wod_etl.writers.write_taxa_netcdf` as `b_<id>_<name>` per haul and `t_<id>_<name>` per Taxa-Record; see [`plankton_biomass.md`](plankton_biomass.md) |

The full contract is in *The intermediate format* in
[`accession_field_guide.md`](accession_field_guide.md).
