# Plankton biomass and taxonomy in WOD

**Plankton belongs in WOD. It does not belong in a profile.** Those two facts are both true
and they are what makes this confusing. This file is the reference for the second one, and
the preparation for a future tool that handles the first.

Nothing in `wod_etl/` writes a plankton record today. When an accession holds plankton, the
job is to identify it, exclude it from the profile run with a stated reason, and report it as
an **unfinished item** — not a success, not a failure. Rule 7 of the `wod-etl` skill.

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

## What to do today, when an accession holds plankton

`classify_dataset` already recognises it and refuses to route it into the profile path:

```python
from wod_etl.discovery import classify_dataset
v = classify_dataset(path="5-plankton/bongo_biomass.csv")
v["is_wod_profile_data"]              # False
v["exclusion"]["belongs_in_wod"]      # True  -- unlike currents or eDNA
v["exclusion"]["route"]               # names Tables 6a and 7a
```

`process_accession` skips those files and records the reason, so they appear in
`skipped_reasons` rather than vanishing. **That is not the end of the job.** Report it:

> `5-plankton/` — 340 zooplankton biomass records, 12 taxa, hauls 0–100 m and 100–500 m.
> Belongs in WOD via the Biological Header (Table 6a) and Taxa-Records (Table 7a). Not
> written: this pipeline has no plankton writer. Unfinished item.

Also capture, while you have the accession open, what a future writer will need and what only
the submitter can tell you: **gear and tow type, mesh size, water volume filtered or tow
distance, preservative, counting and weighing method, and whether large plankters were
removed.** These are Table 6a fields, they are rarely in the data file, and they are usually
recoverable only from the cruise report or the submitter.

---

## What a plankton tool would still need

Everything below is missing from this repository, and all of it is on the WOD website rather
than in the manual PDF:

- **Appendix 4.1–4.9** — tow type, gear, preservative, weight/count/volume/depth methods,
  flowmeter calibration.
- **Appendix 5.1–5.11** — life stage, sex, presence, trophic mode, realm, feature, modifier,
  size, count method, CBV and its calculation method.
- **Appendix 6** — the PGC group list.
- **The ITIS taxonomic serial numbers** (`t_1_taxa_list`), and the WOD ancillary code lists
  for descriptions ITIS cannot represent.
- **A record model that is not a profile**: an output format with upper/lower depth per
  observation, repeated per taxon, with the biological header attached to the cast.

Until those exist, a plankton dataset is an item to name in a report, not an item to process.

---

**See also:** [`cast_structure.md`](cast_structure.md) for where sections 5 and 6 sit in a WOD
cast · [`datasets.md`](datasets.md) for why plankton is OSD ·
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md) for the
full list of what the profile path cannot carry.
