# Web

`wod-etl web` is a browser page over the same two commands as the terminal, `wod-etl run`
and `wod-etl confirm`, for one person on their own machine. Both use
`data/runs/<accession>/`, so a run can start in one and finish in the other.

```bash
uv run wod-etl web                 # http://127.0.0.1:8000  (--host, --port)
```

```
[ accession: URL, number or folder ............ ] [Run] [Confirm] [Open…]
 resolve ✓  fetch ✓  triage ✓  decide ✓    check  stage  process  verify    report
 problems: what, why, how to fix
+------------------+--------------------------------------+--------------------+
| Input files      | decisions.toml            [Reload]   | Output files       |
|  IN / OUT / docs |                           [Save]     |  name · download   |
+------------------+--------------------------------------+--------------------+
 Activity · report.md · run.log
```

Clicking an output file replaces the three panels with its preview (Close or Esc returns):

```
+ wod_ctd_2017_0302596.nc  17 casts ----------------------- [Download] [Close] +
| [filter casts]   | > File: attributes · variables · dimensions              |
|  cast id         | cast id · time · position · dataset · levels             |
|  time · position | [x var] against [z]   # | z | temperature | ..._qc ...   |
|  ...             |   profile plot          1 | 0 | 28.09       | 0          |
|                  | > Cast fields: lat, lon, time, secondary headers, ...    |
+------------------+-----------------------------------------------------------+
```

The app is [`server.py`](server.py) (FastAPI) plus the static front end in
[`static/`](static/).

- **Child process.** Run and Confirm start `wod-etl` as a child process whose output becomes
  the activity log. HDF5 stays on a main thread, and a crash in a data library does not take
  the page down.
- **One command at a time.** A second command, or a save while a command runs, gets a 409.
- **Safe saving.** A save is parsed as TOML first and never overwrites a file that changed on
  disk. Run and Confirm save unsaved edits first.
- **Local only.** Binds to 127.0.0.1, has no login, and serves only the accession's own
  output files for download and preview.
- **Preview.** [`preview.py`](preview.py) reads a file by its CF ragged counts
  (`sample_dimension`), not by the writer's variable names, so new fields show up without
  changes. Reads run in a worker process, like the commands keep HDF5 off the server's
  threads. A cast's levels load in one request and the table shows them 500 at a time; the
  plot uses level number when depth does not vary (a surface track). Plankton files show
  each haul's Taxa-Records, with no plot.
- **Input preview.** Each input file's "preview" link shows it beside its decision: text (the
  first 64 KB), a workbook's sheets (the first 50 rows each), a NetCDF's attributes and
  variables with their first values, or a PDF or image in the browser. Text is laid out to be
  read, and "As stored" shows it unchanged: a table (delimited, fixed-width, Sea-Bird `.cnv`,
  WOD's CSV export, XBT/XCTD `.dat`) as its header lines and columns, split as the pipeline
  splits it; XML and JSON indented; prose wrapped. Files are only read, and only files the
  accession's triage lists, inside its data folder, are served.

API:

- `POST /api/run {"source", "accession_id"?}`
- `POST /api/confirm/<acc>`
- `GET /api/status|files|decisions|outputs|report|log/<acc>`
- `PUT /api/decisions/<acc> {"text", "mtime"}`
- `GET /api/outputs/<acc>/<file>`
- `GET /api/preview/<acc>/<file>` (attributes, dimensions, variables, cast index)
- `GET /api/preview/<acc>/<file>/casts/<i>` (one cast's fields and levels)
- `GET /api/input-preview/<acc>/<path>` (an input file's text, sheets or NetCDF variables)
- `GET /api/input-file/<acc>/<path>` (an input file as it is: shown in the page or downloaded)
- `GET /api/accessions`
