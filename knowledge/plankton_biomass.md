# Plankton biomass and taxonomy in WOD

Plankton belongs in WOD, but not in a profile. This file describes its record model.

`wod-etl` writes plankton as Taxa-Records in four steps:

1. stage each haul (`staging.write_staged_haul`);
2. map it (`mapping.map_haul`);
3. write it (`writers.write_taxa_netcdf`);
4. validate it (`verify.validate_taxa_netcdf`).

What it cannot supply, such as volume filtered or preservative, it reports as an unfinished
item ([operating principle 7](../README.md#operating-principles)).

## Why it cannot go through the profile path

A profile is a value at a level: one depth, one measurement, one QC flag. A net haul samples a
depth range. Manual p.32:

> "The typical plankton cast … contains a taxonomic description, depth range (the upper and
> lower depth) of observation, the original measurements (e.g., abundance, biomass or
> volume), and all provided qualifiers…"

So there is no `z` to write it at, and there is no Table 3a plankton code for
`generate_field_mapping` to map to. Forcing one is how a zooplankton wet weight ends up stored
as chlorophyll.

Table 2 lists "Plankton data" under OSD: true of the dataset, misleading about the record
structure. See [`datasets.md`](datasets.md) → *Plankton is OSD, but not through the profile
path*.

The exception: chlorophyll at discrete depths is Table 3a code `11` and a genuine profile. A
phytoplankton pigment dataset takes the normal path; a zooplankton biomass dataset does not.
Decide from the columns, not the folder name.

## The three namespaces it uses instead

All three are extracted and loaded like every other WOD table, and none overlaps Table 3a.
Small integers collide across all five namespaces; see [`cast_structure.md`](cast_structure.md).

```python
from wod_etl.reference import (biological_headers,        # Table 6a/6b, pp.30-31
                               taxa_biomass_variables,    # Table 7a/7b, pp.33-35
                               taxa_code_ranges,          # Table 8,     p.35
                               plankton_grouping_code,    # Table 9,     pp.35-36
                               classify_taxa_code)
```

| Table | What it holds | File |
| --- | --- | --- |
| 6a / 6b | Biological header: how the sample was collected (gear, tow type, mesh size, water volume filtered, preservative, counting method). One per cast. | `wod_code_tables/table_6a_biological_headers.json` |
| 7a / 7b | Taxa-Record fields: the observation itself (variable number, upper/lower depth, biomass value, life stage, sex, counts, weights, sizes). | `wod_code_tables/table_7a_taxa_biomass.json` |
| 8 | What a Taxa-Record's variable number means, by value range. | same file, `taxa_code_ranges` |
| 9 | Plankton Grouping Code: the tiered index for querying "all copepods". | same file, `plankton_grouping_code` |

The code lists these tables point at (Appendices 4.1–4.9, 5.1–5.11, 6) are not copied into
this repository; the manual defers them to the WOD online code tables. What is extracted is the
field definitions, the units, and which appendix holds each code list.

## The Taxa-Record model

One Taxa-Record per unique combination of taxonomic description × depth range × measurement
type:

| Situation | Taxa-Records |
| --- | --- |
| Displacement volume over 0–100 m and 200–500 m | 2, one per depth range |
| Displacement volume and wet weight over 0–250 m | 2, one per measurement type |
| One species counted at five bottle depths | 5 |
| Ten species counted at five bottle depths | 50 |

Taxa differing only in life stage or sex are different taxonomic descriptions: *Calanus* eggs,
juveniles, adult males and adult females are four records, not one with four qualifiers.

### Two traps in the field values

1. Sign selects the unit. Fields 11, 12, 13, 14, 15 and 25 switch unit on the sign of the
   value: taxon wet weight is `g per UNIT` when positive and `mg per UNIT` when negative. A
   negative value is not missing data. In field 18 a negative value is a size code (−1 small,
   −2 medium, −3 large, −4 very small), not a measurement.
2. "UNIT" is the originator's unit, field 20, Appendix 3.4. Every per-unit field is
   meaningless without it.

### Reading a variable number

```python
classify_taxa_code(-402)    # -> Biomass code, is_biomass=True,  Table 8 p.35
classify_taxa_code(-5002)   # -> Non-taxonomic group code (e.g. "gelatinous organisms")
classify_taxa_code(85257)   # -> Official ITIS code
```

Positive is ITIS. Negative is one of six WOD ranges; only two of them (−400…−405, −500…−503)
mean the record is biomass rather than a taxon.

Separately, 14, −5002 and −5006 are principal-investigator assignment codes for "all
plankton", "zooplankton" and "phytoplankton" (manual p.30). They are not Table 3a codes, and
they are not used the way Table 8 ranges are.

## What to do when an accession holds plankton

`wod-etl run` reads a plankton table's columns (`staging.taxa_layout`) to find the taxon, the
upper and lower depth, the station, and each measurement with its unit. Each haul's position
comes from the station list beside it, or from the table's own latitude, longitude and date
columns. The file is then proposed:

- IN, as OSD Taxa-Records, when all of that is found;
- OUT, with the reason, when it is not: for example, no station list names its stations,
  there is no depth range, or the unit is one WOD cannot state.

`wod-etl confirm` writes one Taxa-Record per taxon, depth range and measurement, and one haul
per station and date. Each year goes in its own file, `wod_osd_<year>_<accession>_taxa.nc`,
because WOD's ragged NetCDF has no layout for Taxa-Records. Values in mg are written negative.
UNIT (field 20) is what a column is per, or `sample` if it does not say.

Other layouts, such as a count matrix with taxa down and samples across, need a custom staging
script built from `melt_matrix` and `write_staged_haul`. Either way, `report.md` lists what was
written and what was missing, for example:

> `5-plankton/`: 340 records in 24 hauls. 12 names not in WOD's list (kept by name); 3
> resolved one rank up. Not stated: volume filtered, preservative, count method, so the
> counts stay per sample.

Ask the submitter for the Table 6a fields while the accession is open: gear and tow type, mesh
size, volume filtered or tow distance, preservative, and counting method. They are rarely in
the data file.

## What the Taxa-Record path uses, and what it still lacks

`wod_etl.reference.code_lists` fetches the code lists the manual defers to the WOD website and
caches them with their retrieval date:

- Appendix 4.x: gear (`b_7_gear_and_flowmeter`), tow type (`b_4_type_tow`) and the other coded
  collection fields. Some publish no CSV; a lookup there is `unavailable` and the originator's
  text is kept.
- Appendix 5.x: life stage (`t_5_taxon_lifestage`), sex (`t_6_taxon_sex`).
- `t_1_taxa_list`: WOD's ~5000 taxa with ITIS numbers and Plankton Grouping Codes. A name not
  in it falls back one rank (genus, or the capitalised taxon in "Zoea of Decapoda") and says
  so; anything else keeps only its name. ITIS itself is not queried.

The record model is `wod_etl.writers.write_taxa_netcdf`: one Biological Header per haul, and
Taxa-Records with an upper/lower depth per observation.

Still missing:

- automatic biomass coding (a Table 8 biomass code carries its unit, so a biomass record must
  be staged with its code as the taxon);
- count matrices (staged by a script);
- a way to give Table 6a values in `decisions.toml`;
- every Table 6a value only the submitter has.

**See also:** [`cast_structure.md`](cast_structure.md) for where sections 5 and 6 sit in a WOD
cast · [`datasets.md`](datasets.md) for why plankton is OSD ·
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md) for the
full list of what the profile path cannot carry.
