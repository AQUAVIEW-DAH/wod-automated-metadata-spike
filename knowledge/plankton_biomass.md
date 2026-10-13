# Plankton biomass and taxonomy in WOD

**Plankton belongs in WOD. It does not belong in a profile.** Those two facts are both true
and they are what makes this confusing. This file is the reference for the record model.

`process_accession` identifies plankton and skips it with a stated reason. It then goes
through the same phase packages a cast does, in its own record shape: stage each haul
(`wod_etl.staging.write_staged_haul`), map it -- taxa, life stages and coded collection
fields resolved against WOD's own code lists (`wod_etl.mapping.map_haul`) -- write
Biological Headers and Taxa-Records (`wod_etl.writers.write_taxa_netcdf`) and validate them
(`wod_etl.verify.validate_taxa_netcdf`). `wod_etl.orchestration.process_taxa` runs all four;
each phase README documents its part. What it cannot supply -- volume filtered, preservative,
counting method -- it reports, and that part stays an **unfinished item** to name. Rule 7 of
the `wod-etl` skill.

---

## Why it cannot go through the profile path

A profile is a **value at a level**: one depth, one measurement, one QC flag. A plankton
observation is a **value over a depth range**, because that is what a net haul physically
samples. Manual p.32:

> "The typical plankton cast … contains a taxonomic description, **depth range (the upper and
> lower depth) of observation**, the original measurements (e.g., abundance, biomass or
> volume), and all provided qualifiers…"

There is no `z` to write it at. There is also **no Table 3a variable code for plankton at
all** — so there is nothing for `generate_field_mapping` to map to, and forcing one is how a
zooplankton wet weight ends up stored as chlorophyll.

Table 2 lists "Plankton data" under **OSD**, which is true of the *dataset* and misleading
about the *record structure*. See [`datasets.md`](datasets.md) → *Plankton is OSD, but not
through the profile path*.

**The exception worth knowing.** Chlorophyll measured at discrete depths *is* Table 3a code
`11` and *is* a genuine profile. A phytoplankton pigment dataset goes through the normal
path; a zooplankton biomass dataset does not. Read the columns, not the folder name.

---

## The three namespaces it uses instead

All three are extracted, loaded the same way as every other WOD table, and none of them
overlaps Table 3a. Small integers collide across all five namespaces — see
[`cast_structure.md`](cast_structure.md).

```python
from wod_etl.reference import (biological_headers,        # Table 6a/6b, pp.30-31
                               taxa_biomass_variables,    # Table 7a/7b, pp.33-35
                               taxa_code_ranges,          # Table 8,     p.35
                               plankton_grouping_code,    # Table 9,     pp.35-36
                               classify_taxa_code)
```

| Table | What it holds | File |
| --- | --- | --- |
| **6a / 6b** | Biological header — how the sample was *collected*: gear, tow type, mesh size, water volume filtered, preservative, counting method. One per cast. | `wod_code_tables/table_6a_biological_headers.json` |
| **7a / 7b** | Taxa-Record fields — the observation itself: variable number, upper/lower depth, biomass value, life stage, sex, counts, weights, sizes. | `wod_code_tables/table_7a_taxa_biomass.json` |
| **8** | What a Taxa-Record's *variable number* means, by value range. | same file, `taxa_code_ranges` |
| **9** | Plankton Grouping Code — the tiered index for querying "all copepods". | same file, `plankton_grouping_code` |

The **code lists** those tables point at (Appendices 4.1–4.9, 5.1–5.11, 6) are **not** in this
repository. The manual defers them to the WOD online code tables, and so does this. What is
extracted is the field definitions, the units, and which appendix each code list lives in.

---

## The Taxa-Record model

One Taxa-Record per unique combination of **taxonomic description × depth range × measurement
type**. This multiplies fast, and it is the shape any future writer has to produce:

| Situation | Taxa-Records |
| --- | --- |
| Displacement volume over 0–100 m and 200–500 m | 2 — one per depth range |
| Displacement volume *and* wet weight over 0–250 m | 2 — one per measurement type |
| One species counted at five bottle depths | 5 |
| Ten species counted at five bottle depths | 50 |

Taxa differing only in life stage or sex are **different taxonomic descriptions**: *Calanus*
eggs, juveniles, adult male and adult female are four records, not one with four qualifiers.

### Two traps in the field values

1. **Sign selects the unit.** Fields 11, 12, 13, 14, 15 and 25 switch unit on the sign of the
   value — taxon wet weight is `g per UNIT` when positive and `mg per UNIT` when negative. A
   negative value is *not* missing data. Field 18 goes further: negative is a size *code*
   (−1 small, −2 medium, −3 large, −4 very small), not a measurement.
2. **"UNIT" is the originator's unit**, field 20, Appendix 3.4. Every per-unit field is
   meaningless without it.

### Reading a variable number

```python
classify_taxa_code(-402)    # -> Biomass code, is_biomass=True,  Table 8 p.35
classify_taxa_code(-5002)   # -> Non-taxonomic group code (e.g. "gelatinous organisms")
classify_taxa_code(85257)   # -> Official ITIS code
```

Positive is ITIS. Negative is one of six WOD ranges, and only two of them (−400…−405,
−500…−503) mean the record is **biomass** rather than a taxon.

Separately, and confusingly: **14, −5002 and −5006 are principal-investigator assignment
codes** for "all plankton", "zooplankton" and "phytoplankton" (manual p.30). They are not
Table 3a codes and they are not Table 8 ranges used the same way.

---

## What to do when an accession holds plankton

`classify_dataset` already recognises it and refuses to route it into the profile path:

```python
from wod_etl.discovery import classify_dataset
v = classify_dataset(path="5-plankton/bongo_biomass.csv")
v["is_wod_profile_data"]              # False
v["exclusion"]["belongs_in_wod"]      # True  -- unlike currents or eDNA
v["exclusion"]["route"]               # names Tables 6a and 7a
```

`process_accession` skips those files and records the reason, so they appear in
`skipped_reasons` rather than vanishing. **That is not the end of the job.** Reshape the
sheet (`melt_matrix` for a count matrix), stage one file per haul with
`write_staged_haul`, and run `process_taxa`. Then report its four lists:

> `5-plankton/` — 340 zooplankton records in 24 hauls, written as Taxa-Records. 12 names did
> not resolve to a WOD variable number (kept by name); 3 resolved one rank up. Not stated
> anywhere: volume filtered, preservative, count method -- counts stay per sample.

Capture, while you have the accession open, what only the submitter can tell you: **gear and
tow type, mesh size, water volume filtered or tow distance, preservative, counting and
weighing method, and whether large plankters were removed.** These are Table 6a fields, they
are rarely in the data file, and they are usually recoverable only from the cruise report or
the submitter. `missing_biological_fields` names the ones a haul lacks.

---

## What the Taxa-Record path uses, and what it still lacks

The code lists the manual defers to the WOD website are **fetched and cached with their
retrieval date** by `wod_etl.reference.code_lists`, not copied into this repository:

- **Appendix 4.x** — gear (`b_7_gear_and_flowmeter`), tow type (`b_4_type_tow`) and the other
  coded collection fields. Some of them publish no CSV; a lookup there is `unavailable` and
  the originator's text is kept.
- **Appendix 5.x** — life stage (`t_5_taxon_lifestage`), sex (`t_6_taxon_sex`).
- **`t_1_taxa_list`** — WOD's ~5000 taxa with ITIS numbers and Plankton Grouping Codes. A name
  not in it falls back one rank (genus, or the capitalised taxon in "Zoea of Decapoda") and
  says so; anything else keeps only its name. ITIS itself is not queried.
- **The record model** — `wod_etl.writers.write_taxa_netcdf`: one Biological Header per
  haul, Taxa-Records with upper/lower depth per observation.

Still missing: **automatic biomass coding** (a Table 8 biomass code carries its unit, so a
biomass record must be staged with its code as the taxon), and every Table 6a value only the
submitter has.

---

**See also:** [`cast_structure.md`](cast_structure.md) for where sections 5 and 6 sit in a WOD
cast · [`datasets.md`](datasets.md) for why plankton is OSD ·
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md) for the
full list of what the profile path cannot carry.
