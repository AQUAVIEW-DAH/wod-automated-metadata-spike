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

**Before reaching for the PDF, read
[`knowledge/README.md`](../../knowledge/README.md).** It routes each kind of question
to the table or prose file that answers it, and names the short list of appendices that
genuinely require opening the manual. `search_manual` below is the tier in between: it
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
| `secondary_header` | metadata code ids and CSV field names | Table 4a p.23 |
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
| `qc_flag_scales()` | all five scales from Table 12 |
| `observed_level_flags()` / `depth_flags()` | one scale as `{int: meaning}` |
| `gradient_factors()` | MGV/MIV/ZSI by variable code |
| `range_levels()` | `{code: [(depth_m, min, max), ...]}` |
| `unit_constants()` | Table 3b |
| `range_check_variables()` | which variables get range-checked |
| `load_table(name)` / `available_tables()` | raw JSON, and what exists |

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
├── wod_code_tables/            8 JSON tables + standardization_rules.md
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
- Appendices not extracted to JSON: ocean weather stations, WMO squares, taxonomic codes,
  standard depth levels (Appendix 9), the Reiniger-Ross depth-difference table
  (Appendix 10). Reach them with `search_manual`, then open the PDF at that page.
