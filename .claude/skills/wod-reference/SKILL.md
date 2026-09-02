---
name: wod-reference
description: Look up World Ocean Database specifications — variable codes, dataset types, units and conversions, QC flag scales, acceptable ranges, gradient thresholds, cast structure, the four metadata namespaces (secondary headers, variable-specific, biological, taxa and plankton biomass), and the published online code list for any coded field. Routes each question to the extracted table or prose file that answers it, so the 131-page manual stays closed.
when_to_use: WOD terminology and specification questions with no files to process — what a code, flag value, dataset abbreviation, unit or threshold means, whether WOD stores a given variable, where a value code list lives, or where the manual documents something. To convert or quality-control actual data files, use wod-etl instead.
---

# WOD reference — where to look, and when to open the PDF

**Answer from `knowledge/`. Open `wod_readme.pdf` only for the short list at the bottom.**

The manual is 131 pages and re-reading it mid-task is slow and error-prone — the tables are
easy to confuse and page-scraped text drops characters. Everything routinely needed has been
extracted, cited by page, and put in `knowledge/`.

---

## The four tiers, in order

### 1. Code tables — load them, never read them

```python
from wod_etl.reference import (wod_datasets, wod_variables, wod_secondary_headers,
                               variable_specific_headers, biological_headers,
                               taxa_biomass_variables, taxa_code_ranges,
                               plankton_grouping_code, classify_taxa_code,
                               unit_constants, observed_level_flags, depth_flags,
                               gradient_factors, range_levels, range_check_variables,
                               load_table, available_tables)
```

| Table | File | Manual |
| --- | --- | --- |
| The eleven WOD dataset names | `knowledge/wod_code_tables/table_2_datasets.json` | p.17 |
| 29 measured variables — code, standard unit, datasets | `knowledge/wod_code_tables/table_3a_variables.json` | p.18 |
| Unit constants — atomic weights, molar volume of O₂ | `knowledge/wod_code_tables/table_3b_unit_constants.json` | p.19 |
| 67 secondary header IDs — metadata about the **cast** | `knowledge/wod_code_tables/table_4a_secondary_headers.json` | pp.23–24 |
| 19 variable specific second headers — metadata about **one variable** | `knowledge/wod_code_tables/table_5a_variable_specific_headers.json` | pp.28–29 |
| 25 biological headers — how a plankton sample was **collected** | `knowledge/wod_code_tables/table_6a_biological_headers.json` | pp.30–31 |
| 28 Taxa-Record fields, the Table 8 code ranges, the Table 9 PGC | `knowledge/wod_code_tables/table_7a_taxa_biomass.json` | pp.32–36 |
| Which variables get range checks | `knowledge/wod_code_tables/table_11_range_variables.json` | p.54 |
| All five QC flag scales | `knowledge/wod_code_tables/table_12_qc_flags.json` | p.55 |
| Gradient factors — MGV / MIV / ZSI | `knowledge/wod_code_tables/table_13_gradients.json` | p.56 |
| Acceptable ranges by depth, 33 levels | `knowledge/wod_code_tables/appendix_11_ranges.json` | pp.87–113 |

**Never transcribe a code, unit or threshold from memory or from a PDF read.** The code
loads these files; so should you. See
[`wod_etl/reference/README.md`](../../../wod_etl/reference/README.md) for each loader's
return shape.

### 1b. The value code lists — a link, not a lookup

Every table above defines metadata **fields**. The **values** those fields take are code
lists WOD publishes online, linked from beside each entry in the manual. Those links are PDF
annotations: they survive neither a text extraction nor a copy-paste, which is why the "App"
column reads like a dead end. All 62 of them are extracted.

```python
from wod_etl.reference import code_table_link, code_table_hub

code_table_link("secondary_header", 29)          # probe type
# {"field": "s_29_probe_type", "html": "https://.../s_29_probe_type.html",
#  "csv": "https://.../CSV/s_29_probe_type.csv", "manual_pages": [25, 69]}

code_table_link("variable_specific_header", 8)   # originator's units
code_table_link("biological_header", 7)          # plankton gear
code_table_link("taxa_variable", 5)              # taxon life stage
code_table_hub("originator_flag_sets")           # the s_96 flag-set list
```

Namespaces: `secondary_header` (4a), `variable_specific_header` (5a), `biological_header`
(6a), `taxa_variable` (7a).

**`None` is an answer, not a gap.** A field with no code list carries a plain measurement or
free text — bottom depth is metres. **Prefer the CSV** to the HTML: same list, parseable.

Nothing in this project fetches these. They are external, they change between WOD releases,
and a cached copy would go stale without saying so — so the answer to "what does probe type
7 mean" is the URL plus, if the user wants the value, a fetch they can see. Rebuild the link
file after a new WOD release with
`uv run python scripts/extract_code_table_links.py`.

### 2. Prose references — read the relevant one

| Question | File |
| --- | --- |
| Which dataset does this belong to? What is MRB vs DRB vs PFL? | [`knowledge/datasets.md`](../../../knowledge/datasets.md) |
| What are the parts of a WOD cast? Which numbering is this code from? | [`knowledge/cast_structure.md`](../../../knowledge/cast_structure.md) |
| How is QC done? What does this flag mean? Which unit conversion applies? What can WOD not store? | [`knowledge/wod_code_tables/standardization_rules.md`](../../../knowledge/wod_code_tables/standardization_rules.md) |
| How do submissions actually arrive? What does `49-46.9858N` mean? How do I stage this? | [`knowledge/accession_field_guide.md`](../../../knowledge/accession_field_guide.md) |
| How does WOD store plankton? What is a Taxa-Record? Why can't this pipeline write one? | [`knowledge/plankton_biomass.md`](../../../knowledge/plankton_biomass.md) |

### 3. `search_manual` — the cached full text

```python
from wod_etl.reference import search_manual
search_manual("Reiniger-Ross")     # -> {"term", "hit_count", "hits": [{page, line, text}], "note"}
```

Substring matching over `knowledge/wod_readme_db/page_text.json`, so it costs nothing and
finds the page for you. It matches the word you type, not the concept — try a few spellings.
Rebuild the cache with `uv run python -m wod_etl.reference.index_manual`.

**Use this before opening the PDF.** Usually the hit lines are the whole answer.

### 4. The PDF — only for these

Open `knowledge/wod_readme.pdf` at a page `search_manual` gives you, and only for content
that is genuinely not extracted:

| Content | Where | Why not extracted |
| --- | --- | --- |
| ISO country codes | Appendix 1, p.68 | Long lookup list; also on the WOD website |
| Secondary header **value** codes (weather station, cast direction, water colour, sea state, probe type, recorder, water sampler…) | Appendix 2, pp.69–70 | **Do not open it — it is a page of links.** Use `code_table_link("secondary_header", id)` |
| Variable-specific second header **value** codes | Appendix 3, p.71 | **Do not open it — it is a page of links.** Use `code_table_link("variable_specific_header", id)` |
| Biological header **value** codes | Appendix 4, p.71 | Same — `code_table_link("biological_header", id)` |
| Taxa-Record **value** codes and the PGC group list | Appendices 5–6, pp.71–72 | Same — `code_table_link("taxa_variable", id)`. The ITIS list is `code_table_hub("taxa_list_itis")` |
| WMO squares | Appendix 7, p.73 | Large grid table |
| Standard vertical levels and depths | Appendix 9, p.81 | Needed only for standard-level interpolation, not implemented |
| Reiniger-Ross interpolation coefficients | Appendix 10, p.82 | Same |
| WOD ragged-array NetCDF format description | Appendix 12, p.114 | Describes WOD's own distribution format, not this intermediate one |
| Ocean basin definitions | Appendix 13 | Needs `range_area.msk` from the website to be usable anyway |
| Glossary, submission guidance, acronyms | Appendices 14–15, §IV–VI | Reference reading |
| XBT depth-time (fall-rate) equation | §III.C, p.58 | Only for XBT depth correction |
| Standard-level QC — density check, statistics, objective analysis | §III.B, p.57 | Downstream of this pipeline |

Everything else — datasets, cast structure, variables, units, QC of observed levels, flags —
is in tiers 1 and 2. If you find yourself opening the PDF for one of those, the gap is in
these files: fix it there so the next run does not repeat the read.

---

## The traps that cause most confusion

Four things in the manual collide by name. All four are covered in
[`knowledge/cast_structure.md`](../../../knowledge/cast_structure.md); they are listed here
because they are what sends people back to the PDF.

- **Five tables number from 1 and mean entirely different things.** Code `4` is *Phosphate*
  (Table 3a), *Institution Code* (4a), *Institution* (5a), *Type of tow* (6a) and *Biomass
  value* (7a). `query_wod_knowledge(4)` searches all five on purpose and returns every
  match, so the collision is visible rather than silently resolved; pass `kind=` once you
  know which table the number came from. The scopes differ too: **4a describes the cast, 5a
  describes one variable within it** and repeats per variable.
- **"Julian year-day" is not the Julian calendar.** It is day-of-year, and only for
  SUR/APB/UOR.
- **Depth is not a variable.** There is no code for it. Code 25 is *Pressure*, a separate
  measurement that may also be present. And a **plankton** measurement has no depth at all —
  it has an upper and a lower depth, which is why it cannot be a profile.
- **A negative number in a Taxa-Record is not missing data.** Several Table 7a fields switch
  unit on the sign (taxon wet weight is `g per UNIT` positive, `mg per UNIT` negative), and
  a negative *variable number* selects a Table 8 range: `classify_taxa_code(-402)` is a
  biomass code, `-5002` a non-taxonomic group.
- **Table 12 defines several flag scales.** The same integer means different things on the
  depth scale and the observed-level scale — `z_qc` 2 is a density inversion, while
  `<var>_qc` 2 is an inversion on the observed-level scale.

---

## Two answers that are easy to get backwards

**"Does WOD store potential temperature?" — No, and it is not code 1.** Theta is derived:
in-situ temperature adjusted for adiabatic compression. Table 3a code 1 is in-situ
temperature and there is no code for theta. Treating them as the same variable makes a cast
claim Temperature twice when both are present, and report the wrong quantity when only theta
is. It can be converted back to in-situ if pressure and salinity are known — that is a
computation, not a rename. `explain_column("potential temperature")` in `wod_etl.mapping`
gives the same answer with its route, and is the single registry for every variable WOD does
not store.

**"How do I record the originator's quality flags?" — Keep them, do not translate them.**
WOD Table 12 is WOD's scale; a submitter's is the submitter's, and an originator `1` may
mean *good* where WOD's `1` means a failed range check. WOD's field for *which* flag set was
used is **secondary header 96**, whose code list is `code_table_hub("originator_flag_sets")`.
Without knowing the set there is no mapping to be made, so the values are carried verbatim
and the set is recorded as unknown.

---

To convert, quality-control or write actual data files, use the `wod-etl` skill.
