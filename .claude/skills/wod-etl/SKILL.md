---
name: wod-etl
description: Convert oceanographic data from an NCEI accession URL or local directory into WOD-standardized, CF-compliant NetCDF profiles. Use when given an accession link, an NCEI archive path, or a folder of ocean profile files (CTD, XCTD, XBT, MBT, bottle, buoy, float, glider) and asked to standardize, convert, quality-control, or turn them into NetCDF. Handles discovery, download, schema mapping to WOD codes, unit conversion, QC flagging, and validation end to end.
---

# WOD ETL

**The canonical instructions are [`docs/AGENT.md`](../../../docs/AGENT.md) at the repo root.
Read that file now — it is the whole procedure.**

This skill is a thin pointer so that `/wod-etl` keeps working in Claude Code. The substance
lives outside `.claude/` on purpose: the same instructions are used by other agent runtimes
through `CLAUDE.md`, `GEMINI.md` and `AGENTS.md`, which are also pointers to `docs/AGENT.md`.
Keeping one copy is what stops the four from drifting.

Setup: `uv sync`, then run everything with `uv run python ...` from the repo root.

Two things that are easy to get wrong, both covered in the canonical doc:

- **An accession is not a dataset.** Triage every folder before processing any of it; most
  folders in a real accession are not WOD profile data.
- **Do not re-read `knowledge/wod_readme.pdf` to settle a question.** Start at
  [`knowledge/README.md`](../../../knowledge/README.md), which routes to the extracted
  tables and prose references and names the short list that genuinely needs the manual.

| Where | What |
| --- | --- |
| [`docs/AGENT.md`](../../../docs/AGENT.md) | The procedure: triage, stage, process, verify |
| [`knowledge/README.md`](../../../knowledge/README.md) | Which tier answers which WOD question |
| `wod_etl/<phase>/README.md` | Per-phase function reference |
| [`README.md`](../../../README.md) | Project overview, layout, known limitations |
