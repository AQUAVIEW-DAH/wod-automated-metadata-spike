# Reference

Loads WOD codes, units, flags and thresholds from the extracted manual tables. The data lives
in [`knowledge/`](../../knowledge/) at the repo root: tables, prose references and the manual.

```python
from wod_etl.reference import wod_variables, observed_level_flags, code_table_link
```

Every WOD code, unit, flag meaning and threshold comes from
[`knowledge/wod_code_tables/`](../../knowledge/wod_code_tables/), extracted from
`knowledge/wod_readme.pdf` with page citations. Do not hardcode WOD values; look them up here
and cite the page.

[`knowledge/README.md`](../../knowledge/README.md) says which file answers which question,
and which pages still need the PDF.

---

## The loaders

Pipeline code calls these rather than reading the JSON:

| Function | Returns |
| --- | --- |
| `wod_datasets()` | `{"OSD": "Ocean Station Data...", ...}` — 11 entries |
| `wod_variables()` | `{1: {"name": "Temperature", "standard_unit": ...}, ...}` — 29 entries |
| `wod_secondary_headers()` | `{1: "Accession Number", ...}` — 67 entries |
| `variable_specific_headers()` | `{id: {name, appendix, field, description}}` — 19 entries |
| `biological_headers()` | `{id: {name, unit, appendix, description}}` — 25 entries |
| `taxa_biomass_variables()` | `{id: {name, unit, appendix, description}}` — 28 entries |
| `taxa_code_ranges()` / `classify_taxa_code(code)` | Table 8 ranges; which one a variable number falls in |
| `plankton_grouping_code()` | the PGC and its multiply-and-truncate usage |
| `qc_flag_scales()` | all five scales from Table 12 |
| `observed_level_flags()` / `depth_flags()` | one scale as `{int: meaning}` |
| `gradient_factors()` | MGV/MIV/ZSI by variable code |
| `range_levels()` | `{code: [(depth_m, min, max), ...]}` |
| `unit_constants()` | Table 3b |
| `range_check_variables()` | which variables get range-checked |
| `country_codes()` / `resolve_country(value)` | Appendix 1 country codes; a code or country name as its WOD code |
| `code_table_link(namespace, code)` | the published code list for one metadata field (below) |
| `code_table_hub(name)` | a named hub URL: `all_wod_code_tables`, `country_codes`, `originator_flag_sets`, … |
| `load_table(name)` / `available_tables()` | raw JSON, and what exists |

### Code list links

The manual defines the metadata fields (Tables 4a, 5a, 6a, 7a) and links each coded one to its
list on the WOD website as PDF annotations, which text extraction loses.
`knowledge/wod_code_tables/code_table_links.json` holds them: 62 code tables across the four
namespaces, each with its HTML page, CSV where one exists, and the manual pages that link it.
Two lists the text cites without a link are added as `primary_header` 6 (countries) and
`character_data` 3 (principal investigators).

```python
code_table_link("secondary_header", 29)
# {"field": "s_29_probe_type", "name": "Probe Type",
#  "html": "https://.../CODES/s_29_probe_type.html",
#  "csv":  "https://.../CODES/CSV/s_29_probe_type.csv", "manual_pages": [25, 69]}

code_table_link("secondary_header", 10)     # -> None
```

`None` means the field has no code list: it holds a plain value or free text (bottom depth is
metres).

`map_metadata` attaches the link to every coded field, and the writer puts it in the NetCDF.

### Looking a value up: `code_lists.lookup_code(namespace, field_id, value)`

Resolves an originator's name against the field's published CSV (derived from the link when
the manual gives only a PDF). A value becomes a code only when exactly one entry matches.

```python
from wod_etl.reference.code_lists import lookup_code, describe_lookup

lookup_code("variable_specific_header", 3, "ITS-90")     # matched 103 'Temperature: ITS-90'
lookup_code("variable_specific_header", 5, "SBE 911plus")  # matched 411
lookup_code("secondary_header", 3, "Shunyo-Maru")        # ambiguous: 4959, 10793
lookup_code("taxa_variable", 1, "Acartia danae")         # matched 86086, PGC 4212010
```

Names are compared upper-cased with punctuation removed (`PSS-78` = `Salinity: PSS78`). A
label also matches its synonyms (runs of spaces in the CSV), the part after a `Category:`
prefix, the text before a parenthesised description, and a plural's singular (`larvae` →
`LARVA`).

When one name has several entries, a call sign, IMO number or brand stated in the value picks
one; with no brand, the unknown-brand entry (`T-7` → `XBT: T7 (UNKNOWN BRAND)`).

Statuses: `matched`, `already_a_code`, `ambiguous`, `no_match` (with `possible` near misses),
`unavailable`, `not_coded`, `disabled`.

Two lookups need more than a name:

- `lookup_unit(unit, wod_code)` matches an originator's unit to `v_8_orig_units` on a
  canonical form (`µmol kg−1` = `umol/kg`), honouring the list's qualifiers (`for alkalinity
  ONLY`, `use only for CFCs`). The variable's own standard unit returns `standard_unit`.
- `lookup_investigator(name)` matches surname and first initial against
  `primary_investigator_list` (`SURNAME; GIVEN`); only a single match is a code.

**Caching.** Lists are fetched on first use, cached under `config.CODE_LIST_CACHE_DIR`
(`data/reference_cache/`) with the retrieval date, and refetched after `CODE_LIST_MAX_AGE_DAYS`
(30). Each result says which copy it used; if a refetch fails, the stale copy is used and
marked `stale`. `WOD_CODE_LOOKUP=0` turns lookups off; `WOD_CODE_LIST_FETCH=0` uses the cache
only (as the test suite does).

Rebuild the links after replacing the PDF with a newer release:

```bash
uv run python -m wod_etl.reference.extract_code_table_links
```

### Five namespaces, all keyed on small integers

Tables 3a, 4a, 5a, 6a and 7a all number from 1. Code `4` is Phosphate, Institution Code,
Institution, Type of tow and Biomass value, one per table. Pick the loader by the table the
number came from. See [`knowledge/cast_structure.md`](../../knowledge/cast_structure.md).

### Flag constants are checked at import

`apply_qc_auditing` defines named constants (`FLAG_RANGE = 1`, …) and calls
`_verify_flag_constants()` at import, so an edit to `table_12_qc_flags.json` that moves a
flag fails the import.

The writer's CF `flag_values` and `flag_meanings` are generated from Table 12 by
`flag_values_attr()` / `flag_meanings_attr()`, so they always match the QC flags.

---

## What's here

```
knowledge/                      (project root, not this package)
├── README.md                   the index: which file answers which question, and when to open the PDF
├── wod_readme.pdf              WOD 2023 User's Manual, 131 pages — ground truth
├── wod_code_tables/            12 JSON tables + standardization_rules.md
├── datasets.md                 which dataset a measurement belongs to
├── cast_structure.md           the parts of a WOD cast, and the colliding namespaces
├── plankton_biomass.md         plankton: why it is not a profile; the Taxa-Record path
├── accession_field_guide.md    how submissions arrive, and how to stage them
└── reading_unknown_files.md    a file no standard reader opens: what it is, and how to stage it
```

A test checks that every JSON file in `wod_code_tables/` is reachable through `TABLE_FILES`.

---

## Limitations

- Range bounds are the union of all 30 ocean basins. Basin assignment needs `range_area.msk`
  and `range_basin_list.msk` from the WOD website, which are not in the PDF.
- Appendix 11 lists 33 depth levels, while ranges are defined on 102 standard levels; the
  manual's nearest-level lookup rule is applied.
- Not extracted to JSON: ocean weather stations, WMO squares, standard depth levels
  (Appendix 9), the Reiniger-Ross depth-difference table (Appendix 10).
  [`knowledge/README.md`](../../knowledge/README.md) gives the PDF pages.
- Plankton code lists are not in the PDF. Tables 6a and 7a are extracted, but Appendices
  4.1–4.9 (tow type, gear, preservative, methods), 5.1–5.11 (life stage, sex, trophic mode,
  size, CBV), 6 (PGC groups) and the ITIS list are online only; `lookup_code` fetches them.
