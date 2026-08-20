# GEMINI.md

**The canonical instructions are [`docs/AGENT.md`](docs/AGENT.md). Read that first.**

WOD ETL converts oceanographic data from an NCEI accession into WOD-standardized,
CF-compliant NetCDF profiles — discovery, staging, WOD code mapping, unit conversion, QC
flagging, validation.

Use it when given an accession URL, an NCEI archive path, or a folder of ocean profile
files (CTD, XCTD, XBT, MBT, bottle, buoy, float, glider) and asked to standardize,
convert, quality-control, or turn them into NetCDF.

Setup: `uv sync`, then run everything with `uv run python ...` from the repo root.

Two things that are easy to get wrong, both covered in the canonical doc:

- **An accession is not a dataset.** Triage every folder before processing any of it; most
  folders in a real accession are not WOD profile data.
- **Do not re-read `knowledge/wod_readme.pdf` to settle a question.** Start at
  [`knowledge/README.md`](knowledge/README.md), which routes to the extracted tables and
  prose references and names the short list of things that genuinely need the manual.
