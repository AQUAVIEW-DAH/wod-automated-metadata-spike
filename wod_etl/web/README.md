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

The app is [`server.py`](server.py) (FastAPI) plus the static front end in
[`static/`](static/).

- **Child process.** Run and Confirm start `wod-etl` as a child process whose output becomes
  the activity log. HDF5 stays on a main thread, and a crash in a data library does not take
  the page down.
- **One command at a time.** A second command, or a save while a command runs, gets a 409.
- **Safe saving.** A save is parsed as TOML first and never overwrites a file that changed on
  disk. Run and Confirm save unsaved edits first.
- **Local only.** Binds to 127.0.0.1, has no login, and serves only the accession's own
  output files for download.

API:

- `POST /api/run {"source", "accession_id"?}`
- `POST /api/confirm/<acc>`
- `GET /api/status|files|decisions|outputs|report|log/<acc>`
- `PUT /api/decisions/<acc> {"text", "mtime"}`
- `GET /api/outputs/<acc>/<file>`
- `GET /api/accessions`
