# Reference

**Everything WOD says, loaded from the manual rather than remembered.**

This package is the *reader*. The knowledge itself lives in
[`knowledge/`](../../knowledge/) at the repo root — tables, prose references and the manual.
Two different things with one name was a standing source of confusion, hence the split.

```python
from wod_etl.reference import query_wod_knowledge, search_manual
```

Every WOD code, unit, flag meaning and threshold this project uses comes from
[`knowledge/wod_code_tables/`](../../knowledge/wod_code_tables/), extracted from
`knowledge/wod_readme.pdf` with page citations. **Never hardcode a WOD value from memory** —
if it isn't in a table, look it up here and cite the page.

**Before reaching for the PDF, use the `wod-reference` skill**
([`.claude/skills/wod-reference/SKILL.md`](../../.claude/skills/wod-reference/SKILL.md)).
It routes each kind of question to the table or prose file that answers it, and names the
short list of appendices that genuinely require opening the manual. `search_manual` below is the tier in between: it
searches a cached copy of the full text, so it costs nothing and hands you the page.

---

## `query_wod_knowledge(query, kind=None, depth_m=None)`

One lookup for "what does WOD say about X". Answers carry their citation.

```python
query_wod_knowledge("temperature")            # → code 1, °C, datasets, p.18
query_wod_knowledge(6, kind="flag")           # → what flag 6 means, on each scale
query_wod_knowledge("probe type")             # → secondary header 29, p.23
query_wod_knowledge("oxygen", kind="range", depth_m=3000)
```

```json
{"query": "temperature", "match_count": 1,
 "matches": [{"kind": "variable", "wod_code": 1, "name": "Temperature",
              "standard_unit": "Degrees Celsius", "unit_abbrev": "°C",
              "datasets": ["OSD", "CTD", ...],
              "citation": "Table 3a, WOD 2023 User's Manual p.18"}],
 "sources": ["Table 3a, WOD 2023 User's Manual p.18"]}
```

`kind` narrows the search; omit it to search everything:

| `kind` | Answers | Source |
| --- | --- | --- |
| `variable` | code, name, standard unit, which datasets store it | Table 3a p.18 |
| `dataset` | the eleven dataset names; explains XCTD rather than inventing it | Table 2 p.17 |
| `secondary_header` | cast metadata code ids and CSV field names | Table 4a p.23 |
| `variable_specific_header` | per-variable metadata: scale, instrument, method, originator's units, Argo mode | Table 5a/5b pp.28–29 |
| `biological_header` | how a plankton sample was collected: gear, tow, mesh, preservative | Table 6a/6b pp.30–31 |
| `taxa_variable` | Taxa-Record fields, and what a negative variable number means | Table 7a/7b pp.33–35, Table 8 p.35 |
| `flag` | what a flag value means, **on which scale** | Table 12 p.55 |
| `gradient` | MGV / MIV / ZSI, optionally resolved at `depth_m` | Table 13 p.56 |
| `range` | acceptable bounds, optionally resolved at `depth_m` | Appendix 11 pp.87–113 |
| `constant` | molar volume of O₂, atomic weights | Table 3b p.19 |

**Numeric queries match codes exactly; text queries match words.** `query_wod_knowledge(6,
kind="flag")` returns flag 6 on *every* scale that defines it, each labelled — because 6
means different things on the cast, observed-level and standard-level scales.

`match_count: 0` is not an error. An unknown `kind` is.

---

## `search_manual(term, max_hits=8)`

Keyword search over the manual's prose, for the ~120 pages that are not tables: dataset
descriptions, ASCII format specs (Table 10a–c), WMO and taxonomic codes, the interpolation
method.

```python
search_manual("fall-rate")
# {"hit_count": 1, "hits": [{"page": 20, "excerpt": "...", "citation": "... p.20"}]}
```

Returns page numbers and excerpts, not answers. Read the page properly once you know where
to look — open `knowledge/wod_readme.pdf` at that page range.

**Requires the page cache.** Build it once:

```bash
uv run python -m wod_etl.reference.index_manual
```

Without it, `search_manual` returns an `error` telling you the same thing. The cache is a
plain JSON list of page text — no embeddings, no vector store, no extra dependency.

---

## Direct table access

For pipeline code that needs a whole table rather than a lookup:

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
| `code_table_link(namespace, code)` | the published code list for one metadata field — see below |
| `code_table_hub(name)` | a named hub URL: `all_wod_code_tables`, `country_codes`, `originator_flag_sets`, … |
| `load_table(name)` / `available_tables()` | raw JSON, and what exists |

### The value code lists live online, and the links are in the PDF

The manual defines the metadata **fields** (Tables 4a, 5a, 6a, 7a) and publishes the
**value** code lists on the WOD website, as a hyperlink beside each entry. Those links are
PDF *annotations*: they survive neither a text extraction nor a copy-paste, which is why the
"App" column reads like a dead end in every extracted copy of the manual.

They are pulled out of the PDF's annotations into
`knowledge/wod_code_tables/code_table_links.json` — 62 code tables across the four
namespaces, each with its HTML page, its CSV download where one exists, and the manual pages
it is linked from.

```python
code_table_link("secondary_header", 29)
# {"field": "s_29_probe_type", "name": "Probe Type",
#  "html": "https://.../CODES/s_29_probe_type.html",
#  "csv":  "https://.../CODES/CSV/s_29_probe_type.csv", "manual_pages": [25, 69]}

code_table_link("secondary_header", 10)     # -> None
```

**`None` is an answer, not a gap.** A field with no code list carries a plain measurement or
free text: bottom depth is metres.

`map_metadata` attaches the link to every coded field it finds, and the writer puts it in
the NetCDF, so a reader of the output alone can resolve a code.

### Looking a value up: `code_lists.lookup_code(namespace, field_id, value)`

A coded field holds a **code**, and an originator writes a name. `lookup_code` resolves the
name against the field's published CSV (derived from the link when the manual gives only a
PDF), and treats a value as a code only when **exactly one** entry is named that:

```python
from wod_etl.reference.code_lists import lookup_code, describe_lookup

lookup_code("variable_specific_header", 3, "ITS-90")     # matched 103 'Temperature: ITS-90'
lookup_code("variable_specific_header", 5, "SBE 911plus")  # matched 411
lookup_code("secondary_header", 3, "Shunyo-Maru")        # ambiguous: 4959, 10793
lookup_code("taxa_variable", 1, "Acartia danae")         # matched 86086, PGC 4212010
```

Names are compared upper-cased with punctuation removed, so `Shunyo-Maru` meets
`SHUNYO MARU` and `PSS-78` meets `Salinity: PSS78`; each label also answers to its
synonyms (runs of spaces in the CSV), the part after a `Category:` prefix, the text before a
parenthesised description, and a plural's singular (`larvae` → `LARVA`). Statuses:
`matched`, `already_a_code`, `ambiguous`, `no_match` (with `possible` near misses),
`unavailable`, `not_coded`, `disabled`.

**The lists are external and change between releases**, so they are fetched on first use
with `get_with_retries`, cached under `config.CODE_LIST_CACHE_DIR` (`data/reference_cache/`)
**with the date retrieved**, refetched after `CODE_LIST_MAX_AGE_DAYS` (30), and every result
says which copy it was checked against. A refetch that fails falls back to the stale copy
and says `stale`. `WOD_CODE_LOOKUP=0` turns lookups off; `WOD_CODE_LIST_FETCH=0` keeps them
to the cache (the test suite runs that way).

Rebuild after replacing the PDF with a newer release:

```bash
uv run python -m wod_etl.reference.extract_code_table_links
```

### Five namespaces, all keyed on small integers

Tables 3a, 4a, 5a, 6a and 7a all number from 1 and mean entirely different things. Code `4`
is Phosphate, Institution Code, Institution, Type of tow and Biomass value — one per table.

`query_wod_knowledge(4)` deliberately searches **all** of them and returns every match, so an
agent sees the collision instead of silently picking a namespace it never chose. Pass `kind=`
once you know which table the number came from.

### Flag constants are checked, not trusted

`apply_qc_auditing` defines named constants (`FLAG_RANGE = 1`, …) for readability, then
calls `_verify_flag_constants()` **at import**. If an edit to
`table_12_qc_flags.json` moves a flag, the import fails loudly rather than silently
mislabelling data.

The NetCDF writer's CF `flag_values` and `flag_meanings` attributes are *generated* from
Table 12 by `flag_values_attr()` / `flag_meanings_attr()`, so a written file's flag
vocabulary cannot disagree with the flags the QC phase assigned.

---

## What's here

```
knowledge/                      (project root, not this package)
├── wod_readme.pdf              WOD 2023 User's Manual, 131 pages — ground truth
├── wod_code_tables/            12 JSON tables + standardization_rules.md
├── datasets.md                 which dataset a measurement belongs to
├── cast_structure.md           the parts of a WOD cast, and the colliding namespaces
├── plankton_biomass.md         plankton: why it is not a profile; the Taxa-Record path
├── accession_field_guide.md    how submissions actually arrive, and how to stage them
└── wod_readme_db/
    └── page_text.json          page cache for search_manual (gitignored, rebuildable)
```

A test asserts that **every JSON file in `wod_code_tables/` is reachable** through
`TABLE_FILES`, so a table can't be added and then quietly go unused — which is what had
happened to Table 11 and Table 12 before this package existed.

---

## Limitations

- **Range bounds are the union across all 30 ocean basins.** Basin assignment needs
  `range_area.msk` and `range_basin_list.msk` from the WOD website, which are not in the
  PDF. Answers carry this as a `caveat`.
- **`search_manual` is substring matching**, not semantic search. It finds the word you
  type, not the concept.
- **Appendix 11 lists 33 depth levels**; the manual says ranges are defined on 102 standard
  levels. The nearest-level lookup rule the manual prescribes is applied.
- Appendices not extracted to JSON: ocean weather stations, WMO squares, standard depth
  levels (Appendix 9), the Reiniger-Ross depth-difference table (Appendix 10). Reach them
  with `search_manual`, then open the PDF at that page.
- **The plankton code lists are not here, and are not in the PDF either.** Tables 6a and 7a
  are extracted — the field definitions, units and which appendix holds each code list — but
  Appendices 4.1–4.9 (tow type, gear, preservative, methods), 5.1–5.11 (life stage, sex,
  trophic mode, size, CBV) and 6 (PGC groups) are deferred by the manual to the WOD online
  code tables. So is the ITIS taxonomic list. A plankton writer needs all of them; see
  [`knowledge/plankton_biomass.md`](../../knowledge/plankton_biomass.md).
