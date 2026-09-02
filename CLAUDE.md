# CLAUDE.md

**The canonical instructions are [`.claude/skills/wod-etl/SKILL.md`](.claude/skills/wod-etl/SKILL.md).
Read that first.** In Claude Code it loads itself — the skill is named `wod-etl`.

WOD ETL converts oceanographic data from an NCEI accession into WOD-standardized,
CF-compliant NetCDF profiles — discovery, staging, WOD code mapping, unit conversion, QC
flagging, validation.

Use it when given an accession URL, an NCEI archive path, or a folder of ocean profile
files (CTD, XCTD, XBT, MBT, bottle, buoy, float, glider) and asked to standardize,
convert, quality-control, or turn them into NetCDF.

Setup: `uv sync`, then run everything with `uv run python ...` from the repo root.

Three things that are easy to get wrong, all covered in the canonical doc:

- **An accession is not a dataset.** Triage every folder before processing any of it with
  `classify_accession`; most folders in a real accession are not WOD profile data.
- **Do not pass `dataset_type` to `process_accession`.** It is optional. An accession is
  rarely one instrument, so each file is classified from its own path, header, columns and
  depth spacing, and files that cannot become a profile are skipped with a stated reason.
- **Do not re-read `knowledge/wod_readme.pdf` to settle a question.** The second skill,
  [`.claude/skills/wod-reference/SKILL.md`](.claude/skills/wod-reference/SKILL.md), routes
  every WOD code, flag, unit and dataset question to the extracted table or prose file that
  answers it, and names the short list that genuinely needs the manual.
