"""`wod-etl`: run one accession end to end from a terminal, no agent required.

    uv run wod-etl run <accession>        # landing-page URL, number, data URL, folder, .tar.gz
    uv run wod-etl status <accession>     # where it stopped, what is open, where the files are

Exit codes: 0 finished and verified · 1 a stage failed · 2 decisions needed ·
3 finished but the output is not verified · 130 interrupted.
"""
import argparse
import sys
import textwrap
from typing import List, Optional

from wod_etl.orchestration.pipeline import STAGES, Workspace, run_pipeline, status

EPILOG = """\
exit codes:
  0    finished, output verified
  1    a stage failed -- the message says what, why and how to fix it
  2    paused: edit data/runs/<accession>/decisions.toml, then run again
  3    finished, but verification flagged the output (see report.md)
  130  interrupted -- run again to continue

Every run is logged to data/runs/<accession>/run.log and summarised in report.md.
Running again continues from where the last run stopped."""


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
              f"report.md and decisions.toml", file=out)
    if other:
        print(f"\n  also ({len(other)} not blocking): "
              + "; ".join(p["what"] for p in other[:6])
              + (" ..." if len(other) > 6 else ""), file=out)


def cmd_run(args) -> int:
    if args.from_stage and not args.yes and sys.stdin.isatty():
        later = STAGES[STAGES.index(args.from_stage):]
        answer = input(f"This reruns {', '.join(later)} and clears their pipeline outputs. "
                       f"Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing done.")
            return 1
    print(f"wod-etl: {args.source}")
    result = run_pipeline(args.source, accession_id=args.accession_id,
                          from_stage=getattr(args, "from_stage", None),
                          progress=lambda line: print(line, flush=True))
    code = result["exit_code"]
    paths = result.get("paths") or {}
    if code == 0:
        print("\nFinished. Output verified.")
    elif code == 2:
        print("\nPaused: decisions needed.")
        _print_problems([p for p in result["problems"] if p.get("kind") in ("decision",
                                                                          "failure")
                         or p.get("blocking")])
        acc = result["accession"]
        print(f"\nEdit {paths.get('decisions')}, then run again:  "
              f"uv run wod-etl run {args.source}"
              + (f" --accession-id {acc}" if args.accession_id else ""))
    elif code == 3:
        print("\nFinished, but verification flagged the output:")
        _print_problems([p for p in result["problems"] if p.get("stage") == "verify"])
    elif code == 130:
        print("\nInterrupted. Run the same command again to continue.")
    else:
        print("\nFAILED.")
        _print_problems([p for p in result["problems"] if p.get("blocking")])
        print("\nAfter fixing it, run the same command again: it continues from the stage "
              "that failed.")
    if paths.get("log"):
        print(f"\nLog: {paths['log']}\nReport: {paths.get('report')}")
    return code


def cmd_status(args) -> int:
    from wod_etl.discovery import accession_for

    acc = args.accession_id or accession_for(args.accession) or args.accession
    s = status(acc)
    if not s["exists"]:
        print(f"No run recorded for accession {acc} (looked in {Workspace(acc).run_dir}).")
        return 1
    print(f"Accession {acc}  input: {s['input']}  last exit code: {s['last_exit_code']}\n")
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
        prog="wod-etl", description="Turn an NCEI accession into WOD NetCDF, resumably.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run (or continue) every stage for one accession",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("source", help="NCEI landing-page URL, accession number, data-directory "
                                  "URL, local folder, or .tar.gz/.zip")
    r.add_argument("--accession-id", help="name the accession when the input does not say "
                                          "(e.g. a local folder named 'survey_2024')")
    r.add_argument("--from", dest="from_stage", choices=STAGES,
                   help="rerun from this stage even if it is done (clears its outputs)")
    r.add_argument("--yes", action="store_true",
                   help="do not ask before --from clears outputs")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("status", help="show where an accession's run stopped")
    s.add_argument("accession", help="accession number, or the input you ran it with")
    s.add_argument("--accession-id", help="the name given with --accession-id")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
