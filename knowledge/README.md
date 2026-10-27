# WOD reference: where each answer lives

These files hold what this project needs from the WOD 2023 manual, with page citations. Only
the items in tier 3 need [`wod_readme.pdf`](wod_readme.pdf). Page numbers are the same in print
and in the PDF.

## Tier 1. Code tables (read by the code at runtime)

The loaders are described in [`wod_etl/reference/README.md`](../wod_etl/reference/README.md).

| Table | File | Manual |
| --- | --- | --- |
| The 11 datasets | [`table_2_datasets.json`](wod_code_tables/table_2_datasets.json) | p.17 |
| 29 measured variables: code, unit, datasets | [`table_3a_variables.json`](wod_code_tables/table_3a_variables.json) | p.18 |
| Unit constants | [`table_3b_unit_constants.json`](wod_code_tables/table_3b_unit_constants.json) | p.19 |
| Secondary headers: metadata about the cast | [`table_4a_secondary_headers.json`](wod_code_tables/table_4a_secondary_headers.json) | pp.23–24 |
| Variable-specific headers: metadata about one variable | [`table_5a_variable_specific_headers.json`](wod_code_tables/table_5a_variable_specific_headers.json) | pp.28–29 |
| Biological headers: how a plankton sample was collected | [`table_6a_biological_headers.json`](wod_code_tables/table_6a_biological_headers.json) | pp.30–31 |
| Taxa-Record fields, Table 8 ranges, Table 9 PGC | [`table_7a_taxa_biomass.json`](wod_code_tables/table_7a_taxa_biomass.json) | pp.32–36 |
| Variables that get range checks | [`table_11_range_variables.json`](wod_code_tables/table_11_range_variables.json) | p.54 |
| The five QC flag scales | [`table_12_qc_flags.json`](wod_code_tables/table_12_qc_flags.json) | p.55 |
| Gradient factors | [`table_13_gradients.json`](wod_code_tables/table_13_gradients.json) | p.56 |
| Acceptable ranges by depth | [`appendix_11_ranges.json`](wod_code_tables/appendix_11_ranges.json) | pp.86–113 |
| Country codes | [`appendix_1_country_codes.json`](wod_code_tables/appendix_1_country_codes.json) | Appendix 1, p.68 |
| Links to the online code lists | [`code_table_links.json`](wod_code_tables/code_table_links.json) | link annotations |

**Code lists.** The tables define metadata fields; their values (platform, probe type, gear…)
are code lists WOD publishes online, linked from the manual through PDF annotations that a text
copy loses. All 62 links are extracted, plus the two lists the text cites without a link:
countries and principal investigators. `code_table_link("secondary_header", 29)` returns the
probe-type list's URLs; `None` means the field is not coded. The pipeline looks names up in
these lists and caches them for 30 days.

**Not extracted**, because nothing here uses them: Tables 1a/1b (volumes, cast counts), 4c (WMO
code table names), 10a–c (WOD's native ASCII layout), and Appendices 6, 7, 9, 10 and 13 (below).

## Tier 2. Prose references

| Question | File |
| --- | --- |
| Which dataset? MRB vs DRB vs PFL? CTD vs OSD? | [`datasets.md`](datasets.md) |
| What are the parts of a cast? Which table is a code from? | [`cast_structure.md`](cast_structure.md) |
| QC, flags, unit conversions, what WOD cannot store | [`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md) |
| How submissions arrive; coordinates like `49-46.9858N`; station lists | [`accession_field_guide.md`](accession_field_guide.md) |
| Plankton and Taxa-Records | [`plankton_biomass.md`](plankton_biomass.md) |
| A file that does not open, or has no header or units | [`reading_unknown_files.md`](reading_unknown_files.md) |

## Tier 3. The PDF, only for these

| Content | Where | Note |
| --- | --- | --- |
| Secondary header value codes | Appendix 2, pp.69–70 | Only links; use `code_table_link("secondary_header", id)` |
| Variable-specific header value codes | Appendix 3, p.71 | Only links; use `code_table_link("variable_specific_header", id)` |
| Biological header value codes | Appendix 4, p.71 | Only links; use `code_table_link("biological_header", id)` |
| Taxa-Record value codes, PGC groups | Appendices 5–6, pp.71–72 | Only links; use `code_table_link("taxa_variable", id)` |
| WMO squares | Appendix 7, p.74 | |
| Standard levels | Appendix 9, p.81 | Not used: no standard-level interpolation here |
| Reiniger-Ross coefficients | Appendix 10, p.82 | Not used |
| WOD's own NetCDF format | Appendix 12, p.114 | |
| Ocean basins | Appendix 13, p.116 | Needs basin masks from the WOD website |
| Submitting data to NCEI | Appendix 14, p.117 | |
| Glossary | Appendix 15, p.128 | Acronyms are in §VI, p.62 |
| XBT fall-rate equation | §III.C, p.61 | |
| Standard-level QC | §III.B, p.57 | Downstream of this project |

Anything else read from the PDF belongs in tier 1 or 2.

## Common traps

- Five tables number from 1. Code `4` is Phosphate (3a), Institution Code (4a), Institution
  (5a), Type of tow (6a) and Biomass value (7a). Table 4a describes the cast; 5a describes one
  variable.
- "Julian year-day" is day-of-year, not the Julian calendar.
- Depth is not a variable; code 25 is Pressure. A plankton measurement has an upper and lower
  depth instead of one depth, so it is a Taxa-Record, not a profile.
- A negative number in a Taxa-Record is not missing data. For some fields the sign selects the
  smaller unit (mg instead of g). A negative variable number is a Table 8 code.
- Table 12 has several flag scales. The same integer means different things on each.
- Potential temperature is not code 1. It is derived, and WOD has no code for it.
  `explain_column` lists every variable WOD does not store.
- The originator's quality flags are kept verbatim, never translated: their `1` may not mean
  WOD's `1`. Which flag set was used belongs in secondary header 96.
