"""`wod-etl`: one accession, in two steps, from a terminal -- no agent required.

    uv run wod-etl run <accession>       # triage it; write decisions.toml for review
    uv run wod-etl confirm <accession>   # write what the reviewed decisions include
    uv run wod-etl status <accession>    # where it stopped, what is open, where the files are

`run` takes a landing-page URL, an accession number, a data URL, a local folder or a .tar.gz.
Exit codes: 0 done · 1 a stage failed (or, for confirm, a file could not be written) ·
2 decisions needed · 3 written, but the output is not verified · 130 interrupted.
"""
import argparse
import sys
import textwrap
from typing import List, Optional

from wod_etl.orchestration.pipeline import (CONFIRM_STAGES, RUN_STAGES, STAGES, Workspace,
                                            confirm_pipeline, run_pipeline, status)

EPILOG = """\
The two steps:
  wod-etl run <input>         resolve, fetch and triage every file, then write
                              data/runs/<accession>/decisions.toml: for each file, what it
                              holds, whether it goes into WOD, as which dataset, and why.
                              Nothing is processed. Review the file; change what you
                              disagree with.
  wod-etl confirm <accession> check the decisions, stage what needs reshaping, write every
                              included file as WOD NetCDF, and verify the output.

exit codes:
  0    run: decisions written for review -- confirm: written and verified, or nothing to
       write
  1    a stage failed, or a file could not be written -- the message says what, why and how
       to fix it
  2    confirm: decisions needed -- edit decisions.toml (or run again), then confirm again
  3    confirm: written, but verification flagged the output (see report.md)
  130  interrupted -- run the same command again to continue

Each command logs to data/runs/<accession>/run.log (and events.jsonl) and summarises the run
in report.md. Running a command again continues from where it stopped.

  wod-etl web                 the same two steps in a browser: the stages, the files,
                              decisions.toml to edit, and the WOD files written"""


MAX_PRINTED = 12


def _print_problems(problems, out=sys.stdout) -> None:
    blocking = [p for p in problems if p.get("blocking")]
    other = [p for p in problems if not p.get("blocking")]
    for n, p in enumerate(blocking[:MAX_PRINTED], 1):
        print(f"\n  {n}. {p['what']}", file=out)
        if p.get("why"):
            print(textwrap.indent(textwrap.fill(f"why: {p['why']}", 92), "     "), file=out)
        print(textwrap.indent(textwrap.fill(f"fix: {p['how_to_fix']}", 92), "     "), file=out)
    if len(blocking) > MAX_PRINTED:
        print(f"\n  ... and {len(blocking) - MAX_PRINTED} more -- all of them are in "
              f"report.md", file=out)
    if other:
        print(f"\n  also ({len(other)} not blocking): "
              + "; ".join(p["what"] for p in other[:6])
              + (" ..." if len(other) > 6 else ""), file=out)


def _confirm_rerun(args, stages) -> bool:
    if args.from_stage and not args.yes and sys.stdin.isatty():
        later = stages[stages.index(args.from_stage):]
        answer = input(f"This reruns {', '.join(later)} and clears their pipeline outputs. "
                       f"Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing done.")
            return False
    return True


def _paths(result) -> None:
    paths = result.get("paths") or {}
    if paths.get("log"):
        print(f"\nLog: {paths['log']}\nReport: {paths.get('report')}")


def _written(result) -> None:
    """The WOD files a confirm wrote, one line each -- not after a confirm that stopped
    before writing, whose process record is an earlier one's."""
    stages = result.get("stages") or {}
    if result.get("exit_code") not in (0, 1, 3) or any(
            (stages.get(s) or {}).get("status") != "done" for s in ("check", "stage", "process")):
        return
    files = ((stages.get("process") or {}).get("outputs") or {}).get("files") or []
    if files:
        print(f"\nOutput: {(result.get('paths') or {}).get('output')}")
        for f in files:
            print(f"  {f['name']}  {f['casts']} {f['dataset']} cast(s) from "
                  f"{len(f.get('sources') or [])} source file(s)")


def cmd_run(args) -> int:
    if not _confirm_rerun(args, RUN_STAGES):
        return 1
    print(f"wod-etl run: {args.source}")
    result = run_pipeline(args.source, accession_id=args.accession_id,
                          from_stage=args.from_stage,
                          progress=lambda line: print(line, flush=True))
    code, acc = result["exit_code"], result["accession"]
    paths = result.get("paths") or {}
    if code == 0:
        plan = ((result.get("stages") or {}).get("decide") or {}).get("outputs") or {}
        print(f"\nDecisions written: {paths.get('decisions')}")
        print(f"  {plan.get('included_files', 0)} file(s) to include, "
              f"{len((plan.get('plan') or {}).get('left_out') or [])} to leave out.")
        print("Review each entry -- what the file holds, include or not, and why -- and change "
              "any you\ndisagree with.")
        blocking = [p for p in result["problems"] if p.get("blocking")]
        if blocking:
            print("\nSettle these before confirming:")
        _print_problems(result["problems"])
        print(f"\nThen:  uv run wod-etl confirm {acc}")
    elif code == 130:
        print("\nInterrupted. Run the same command again to continue.")
    else:
        print("\nFAILED.")
        _print_problems([p for p in result["problems"] if p.get("blocking")])
        print("\nAfter fixing it, run the same command again: it continues from the stage "
              "that failed.")
    _paths(result)
    return code


def cmd_confirm(args) -> int:
    from wod_etl.discovery import accession_for

    if not _confirm_rerun(args, CONFIRM_STAGES):
        return 1
    acc = args.accession_id or accession_for(args.accession) or args.accession
    print(f"wod-etl confirm: {acc}")
    result = confirm_pipeline(acc, from_stage=args.from_stage,
                              progress=lambda line: print(line, flush=True))
    code = result["exit_code"]
    stages = result.get("stages") or {}
    if code == 0:
        verify = (stages.get("verify") or {}).get("outputs") or {}
        print(f"\nFinished. {verify.get('casts')} cast(s) in {verify.get('netcdf_files')} "
              f"file(s) written and verified." if verify.get("output_verified") else
              "\nFinished. Nothing was written: the decisions include no file that holds a "
              "profile (report.md says what was left out and why).")
        _print_problems(result["problems"])           # the notes that did not stop it
    elif code == 2:
        print("\nDecisions needed before anything is written:")
        _print_problems([p for p in result["problems"] if p.get("blocking")])
        print(f"\nThen:  uv run wod-etl confirm {acc}")
    elif code == 3:
        print("\nWritten, but verification flagged the output:")
        _print_problems([p for p in result["problems"] if p.get("stage") == "verify"])
    elif code == 130:
        print("\nInterrupted. Run the same command again to continue.")
    else:
        failed_files = [p for p in result["problems"]
                        if p.get("stage") == "process" and p.get("file")]
        process = stages.get("process") or {}
        if failed_files and process.get("status") == "done":
            print(f"\nFinished, but {len(failed_files)} file(s) could not be written "
                  f"(the rest were written and verified):")
        else:
            print("\nFAILED.")
        _print_problems([p for p in result["problems"] if p.get("blocking")])
        print("\nAfter fixing it, run the same command again: it continues from where it "
              "stopped.")
    _written(result)
    _paths(result)
    return code


def cmd_status(args) -> int:
    from wod_etl.discovery import accession_for

    acc = args.accession_id or accession_for(args.accession) or args.accession
    s = status(acc)
    if not s["exists"]:
        print(f"No run recorded for accession {acc} (looked in {Workspace(acc).run_dir}).")
        return 1
    print(f"Accession {acc}  input: {s['input']}\nlast command: {s['last_command']} "
          f"(exit code {s['last_exit_code']})\n")
    for name in STAGES:
        st = s["stages"].get(name)
        if st:
            print(f"  {name:<8} {st.get('status') or '-':<8} {st.get('summary') or ''}")
    open_items = [p for p in s["problems"] if p.get("blocking")]
    if open_items:
        print("\nOpen:")
        _print_problems(open_items)
    print(f"\nLog: {s['paths']['log']}\nDecisions: {s['paths']['decisions']}\n"
          f"Report: {s['paths']['report']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wod-etl", description="Turn an NCEI accession into WOD NetCDF, in two reviewed "
                                    "steps.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="triage an accession and write its decisions for review",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("source", help="NCEI landing-page URL, accession number, data-directory "
                                  "URL, local folder, or .tar.gz/.zip")
    r.add_argument("--accession-id", help="name the accession when the input does not say "
                                          "(e.g. a local folder named 'survey_2024')")
    r.add_argument("--from", dest="from_stage", choices=RUN_STAGES,
                   help="redo from this stage even if it is done (clears its outputs)")
    r.add_argument("--yes", action="store_true",
                   help="do not ask before --from clears outputs")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("confirm", help="write what the reviewed decisions include",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_argument("accession", help="accession number, or the input you ran it with")
    c.add_argument("--accession-id", help="the name given to `run` with --accession-id")
    c.add_argument("--from", dest="from_stage", choices=CONFIRM_STAGES,
                   help="redo from this stage even if it is done (clears its outputs)")
    c.add_argument("--yes", action="store_true",
                   help="do not ask before --from clears outputs")
    c.set_defaults(func=cmd_confirm)
    s = sub.add_parser("status", help="show where an accession's run stopped")
    s.add_argument("accession", help="accession number, or the input you ran it with")
    s.add_argument("--accession-id", help="the name given with --accession-id")
    s.set_defaults(func=cmd_status)
    w = sub.add_parser("web", help="run and confirm accessions in a browser")
    w.add_argument("--host", default="127.0.0.1",
                   help="address to serve on (default 127.0.0.1: this machine only -- there "
                        "is no login)")
    w.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    w.set_defaults(func=cmd_web)
    return p


def cmd_web(args) -> int:
    from wod_etl.orchestration.web import serve

    serve(args.host, args.port)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
