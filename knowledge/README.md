# Knowledge base — where to look, and when to open the PDF

**Answer from this directory. Open `wod_readme.pdf` only for the short list at the bottom.**

The manual is 131 pages and re-reading it mid-task is slow and error-prone — the tables are
easy to confuse and page-scraped text drops characters. Everything routinely needed has been
extracted, cited by page, and put here.

---

## The four tiers, in order

### 1. Code tables — load them, never read them

```python
from wod_etl.reference import (wod_datasets, wod_variables, wod_secondary_headers,
                               unit_constants, observed_level_flags, depth_flags,
                               gradient_factors, range_levels, range_check_variables,
                               load_table, available_tables)
```

| Table | File | Manual |
| --- | --- | --- |
| The eleven datasets | `wod_code_tables/table_2_datasets.json` | p.17 |
| Measured variables — code, standard unit, datasets | `wod_code_tables/table_3a_variables.json` | p.18 |
| Unit constants — atomic weights, molar volume of O₂ | `wod_code_tables/table_3b_unit_constants.json` | p.19 |
| Secondary header IDs | `wod_code_tables/table_4a_secondary_headers.json` | pp.23–24 |
| Which variables get range checks | `wod_code_tables/table_11_range_variables.json` | p.54 |
| QC flag scales | `wod_code_tables/table_12_qc_flags.json` | p.55 |
| Gradient factors — MGV / MIV / ZSI | `wod_code_tables/table_13_gradients.json` | p.56 |
| Acceptable ranges by depth | `wod_code_tables/appendix_11_ranges.json` | pp.87–113 |

**Never transcribe a code, unit or threshold from memory or from a PDF read.** The code
loads these files; so should you.

### 2. Prose references — read the relevant one

| Question | File |
| --- | --- |
| Which dataset does this belong to? What is MRB vs DRB vs PFL? | [`datasets.md`](datasets.md) |
| What are the parts of a WOD cast? Which numbering is this code from? | [`cast_structure.md`](cast_structure.md) |
| How is QC done? What does this flag mean? Which unit conversion applies? What can WOD not store? | [`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md) |
| How do submissions actually arrive? What does `49-46.9858N` mean? How do I stage this? | [`accession_field_guide.md`](accession_field_guide.md) |

### 3. `search_manual` — the cached full text

```python
from wod_etl.reference import search_manual
search_manual("Reiniger-Ross")     # -> {"term", "hit_count", "hits": [{page, line, text}], "note"}
```

Substring matching over `wod_readme_db/page_text.json`, so it costs nothing and finds the
page for you. It matches the word you type, not the concept — try a few spellings. Rebuild
the cache with `uv run python -m wod_etl.reference.index_manual`.

**Use this before opening the PDF.** Usually the hit lines are the whole answer.

### 4. The PDF — only for these

Open `wod_readme.pdf` at a page `search_manual` gives you, and only for content that is
genuinely not extracted:

| Content | Where | Why not extracted |
| --- | --- | --- |
| ISO country codes | Appendix 1, p.68 | Long lookup list; also on the WOD website |
| Secondary header **value** codes (weather station, cast direction, water colour, sea state, probe type, recorder, water sampler…) | Appendix 2, pp.69–70 | Many small enumerations; the manual defers most to the website code tables |
| Variable-specific secondary header codes (scale, instrument, method, originator's units, filter type) | Appendix 3, p.71 | Same |
| Biological header codes (tow type, gear, preservative, weight/count method) | Appendix 4, p.71 | Only needed on the plankton path, which this pipeline does not write |
| Taxonomic codes and plankton grouping | Appendices 5–6, pp.71–72 | Same |
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
these files: fix it here so the next run does not repeat the read.

---

## The traps that cause most confusion

Four things in the manual collide by name. All four are covered in
[`cast_structure.md`](cast_structure.md); they are listed here because they are what sends
people back to the PDF.

- **Table 3a codes and Table 4a IDs are different namespaces.** ID 29 is *Probe Type*;
  variable code 29 is *Air pressure*.
- **"Julian year-day" is not the Julian calendar.** It is day-of-year, and only for
  SUR/APB/UOR.
- **Depth is not a variable.** There is no code for it. Code 25 is *Pressure*, a separate
  measurement that may also be present.
- **Table 12 defines several flag scales.** The same integer means different things on the
  depth scale and the observed-level scale.
