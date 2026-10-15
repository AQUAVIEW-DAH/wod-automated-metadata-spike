"""`wod-etl run` then `wod-etl confirm`: one accession in two reviewed steps, resumable."""
import importlib
import json
import os
import re
import shutil
import tomllib

import pytest

from wod_etl.orchestration.cli import main
from wod_etl.orchestration.pipeline import (EXIT_DECISIONS, EXIT_FAILED, EXIT_INTERRUPTED,
                                            EXIT_OK, Workspace, confirm_pipeline,
                                            run_pipeline)

pa = importlib.import_module("wod_etl.orchestration.process_accession")

ACC = "0999999"
STATIONS = ("Cruise,Station No.,Date,Year,Long,Lat\n"
            "X1,1,Nov. 09,2015,142°18’E,21°00’N\n"
            "X1,2,Nov. 10,2015,142°24’E,20°30’N\n"
            "X1,3,Nov. 10,2015,142°30’E,20°00’N\n")
DIRECT = ("# LAT: 21.0\n# LON: 142.3\n# DATE: 2015-11-09\n"
          "Depth (m),Temperature (deg C),Salinity (PSU)\n"
          + "".join(f"{z},{25 - z / 10:.2f},34.5\n" for z in range(0, 20)))


def _long(var):
    value = (lambda st, z: 20 - z / 10 + st) if var == "Temperature" else \
        (lambda st, z: 34 + z / 100 + st / 10)
    rows = [f"X1,{st},CTD,{var},{z}.0,{value(st, z):.3f}"
            for st in (1, 2, 3) for z in range(1, 30)]
    return "Cruise,StNo,ObsItem,parm,depth,value\n" + "\n".join(rows) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Every pipeline folder under tmp_path, and a small accession: one cast processed as it
    stands, two long-format variables for three stations, and their station list."""
    from wod_etl import config

    for name in ("RAW_DIR", "STAGING_DIR", "PROCESSED_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
    acc = tmp_path / "src" / ACC
    data = acc / "data" / "0-data"
    data.mkdir(parents=True)
    (acc / "NCEI-Readme.txt").write_text("This is the standard bundle for an NCEI AIP.\n")
    (data / "X1_StationInfo.csv").write_text(STATIONS)
    (data / "X1_CTD_Temp.csv").write_text(_long("Temperature"))
    (data / "X1_CTD_Salinity.csv").write_text(_long("Salinity"))
    ctd = acc / "data" / "ctd"
    ctd.mkdir()
    (ctd / "cast_direct.csv").write_text(DIRECT)
    return acc


def _fill(ws: Workspace, **extra):
    """What the reviewer does here: state the units the long-format recipe asks for."""
    p = ws.decisions_path
    s = p.read_text()
    s = s.replace("Temperature (UNIT?)", "Temperature (deg C)").replace(
        "Salinity (UNIT?)", "Salinity (PSU)")
    for old, new in extra.items():
        s = s.replace(old, new)
    p.write_text(s)


def _set(ws: Workspace, key: str, **fields):
    """Change fields of one [files] entry, as a reviewer edits it."""
    s = ws.decisions_path.read_text()
    head = f'[files."{key}"]'
    i = s.index(head)
    block = s[i:].split("\n\n")[0]
    new = block
    for field, value in fields.items():
        line = f"{field} = {json.dumps(value)}"
        new = re.sub(rf"^{field} = .*$", line, new, flags=re.M) if re.search(
            rf"^{field} = ", new, flags=re.M) else new + "\n" + line
    ws.decisions_path.write_text(s.replace(block, new))


def _decisions(ws: Workspace):
    return tomllib.loads(ws.decisions_path.read_text())


def _lines():
    out = []
    return out, out.append


def _outputs(ws: Workspace):
    """The WOD files written: one per dataset and year."""
    return sorted(p.name for p in ws.processed_dir.glob("wod_*.nc"))


def _sources(ws: Workspace):
    """The source file of every cast written, as its folder and name."""
    import netCDF4 as nc

    out = []
    for p in sorted(ws.processed_dir.glob("wod_*.nc")):
        with nc.Dataset(p) as ds:
            n = len(ds.dimensions["casts"])
            out += ([str(x) for x in ds["source_file"][:]] if "source_file" in ds.variables
                    else [ds.source_file] * n)
    return sorted("/".join(s.split("/")[-2:]) for s in out)


class TestRunWritesDecisionsForReview:
    def test_every_file_gets_what_it_holds_whether_it_goes_in_and_why(self, env):
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_OK, r["problems"]
        ws = Workspace(ACC)
        files = _decisions(ws)["files"]
        assert files["data/ctd/cast_direct.csv"]["include"] is True
        assert files["data/ctd/cast_direct.csv"]["dataset"] == "CTD"
        assert files["data/ctd/cast_direct.csv"]["data"] == "CTD profiles: temperature, salinity"
        long_entry = files["data/0-data/X1_CTD_*.csv"]
        assert long_entry["include"] is True and "[profiles.x1_ctd]" in long_entry["why"]
        assert files["data/0-data/X1_StationInfo.csv"]["include"] is False
        # documentation: its section heading says why, so the entry does not repeat it
        assert files["NCEI-Readme.txt"] == {"data": "NCEI readme", "include": False}
        assert all(e.get("why") for k, e in files.items() if k != "NCEI-Readme.txt")
        text = ws.decisions_path.read_text()
        assert text.index("[metadata]") < text.index("# ==== IN:") < text.index(
            "# ==== DOCUMENTATION:")
        assert "#   IN       3 files  as CTD (3)" in text

    def test_nothing_is_processed_and_what_is_still_open_is_said(self, env):
        r = run_pipeline(str(env))
        ws = Workspace(ACC)
        assert _outputs(ws) == []
        assert "UNIT?" in ws.decisions_path.read_text()
        assert any("unit of" in p["what"] for p in r["problems"] if p["blocking"])
        report = ws.report_md.read_text()
        assert "decisions written: review them, then confirm" in report
        assert "## Before confirming" in report and "| `data/ctd/cast_direct.csv` |" in report

    def test_running_again_keeps_the_reviewers_edits_and_adds_new_files(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _set(ws, "data/ctd/cast_direct.csv", include=False, why="a test cast, not data")
        (env / "data" / "ctd" / "cast_two.csv").write_text(DIRECT)
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK
        files = _decisions(ws)["files"]
        assert files["data/ctd/cast_direct.csv"]["why"] == "a test cast, not data"
        assert files["data/ctd/cast_two.csv"]["include"] is True
        assert ws.decisions_path.read_text().count("# ---- added by run ") == 1


class TestConfirmWritesWhatIsIncluded:
    def test_open_decisions_refuse_then_confirm_writes_everything_included(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS
        assert any("unit of" in p["what"] for p in r["problems"] if p["blocking"])
        assert _outputs(ws) == []

        _fill(ws)
        lines, say = _lines()
        r = confirm_pipeline(ACC, progress=say)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        # WOD's layout: one file per dataset and year, with the accession
        assert _outputs(ws) == [f"wod_ctd_2015_{ACC}.nc"]
        assert _sources(ws) == ["ctd/cast_direct.csv", "x1_ctd/1.csv", "x1_ctd/2.csv",
                                "x1_ctd/3.csv"]
        assert r["stages"]["verify"]["outputs"]["output_verified"] is True
        log = ws.log_path.read_text()
        assert log.count("cli run ") == 1 and log.count("cli confirm ") == 2
        report = ws.report_md.read_text()
        assert "finished: output written and verified" in report
        assert "## Verification" in report and "| temperature | 1 | degree_C |" in report

    def test_a_file_left_out_is_not_written(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        _set(ws, "data/ctd/cast_direct.csv", include=False)
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert _sources(ws) == ["x1_ctd/1.csv", "x1_ctd/2.csv", "x1_ctd/3.csv"]
        _set(ws, "data/ctd/cast_direct.csv", include=True)
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert "ctd/cast_direct.csv" in _sources(ws)

    def test_a_dataset_changed_by_the_reviewer_overrides_the_classification(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        _set(ws, "data/ctd/cast_direct.csv", dataset="OSD")
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        plan = r["stages"]["check"]["outputs"]["plan"]
        assert plan["dataset_overrides"] == {"data/ctd/cast_direct.csv": "OSD"}
        assert r["stages"]["process"]["outputs"]["runs"]["direct"]["dataset_types"] == {"OSD": 1}

    def test_confirming_again_with_nothing_changed_redoes_nothing(self, env):
        run_pipeline(str(env))
        _fill(Workspace(ACC))
        confirm_pipeline(ACC)
        before = json.loads(Workspace(ACC).state_path.read_text())["stages"]
        lines, say = _lines()
        assert confirm_pipeline(ACC, progress=say)["exit_code"] == EXIT_OK
        after = json.loads(Workspace(ACC).state_path.read_text())["stages"]
        assert sum("(unchanged)" in l for l in lines) == 4
        assert after["process"]["finished_at"] == before["process"]["finished_at"]

    def test_an_edit_to_another_files_entry_does_not_rewrite_the_output(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        confirm_pipeline(ACC)
        before = json.loads(ws.state_path.read_text())["stages"]["process"]["finished_at"]
        _set(ws, "NCEI-Readme.txt", why="read it: the layout of the bundle")
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        after = json.loads(ws.state_path.read_text())["stages"]
        assert after["process"]["finished_at"] == before and after["check"]["status"] == "done"

    def test_changing_a_unit_restages_and_reprocesses(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        confirm_pipeline(ACC)
        ws.decisions_path.write_text(ws.decisions_path.read_text().replace(
            "Salinity (PSU)", "Salinity (psu)"))
        lines, say = _lines()
        assert confirm_pipeline(ACC, progress=say)["exit_code"] == EXIT_OK
        assert not any("(unchanged)" in l for l in lines
                       if re.search(r"\] (stage|process)\s", l)), lines
        staged = (ws.staging_dir / "profiles" / "x1_ctd" / "1.csv").read_text()
        assert "Salinity (psu)" in staged

    def test_output_deleted_by_hand_is_rebuilt_not_verified_as_empty(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        written = _outputs(ws)
        (ws.processed_dir / written[0]).unlink()
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert _outputs(ws) == written
        shutil.rmtree(ws.staging_dir)
        shutil.rmtree(ws.processed_dir)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK and _outputs(ws) == written
        assert r["stages"]["verify"]["outputs"]["output_verified"] is True


class TestConfirmOnlyWhatWasReviewed:
    def test_confirm_before_run_says_to_run_first(self, env):
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS and "wod-etl run" in r["problems"][0]["how_to_fix"]

    def test_files_that_changed_since_the_run_must_be_reviewed_again(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        (env / "data" / "ctd" / "cast_two.csv").write_text(DIRECT)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS
        assert "changed since the decisions were written" in r["problems"][0]["what"]
        assert _outputs(ws) == []
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert "ctd/cast_two.csv" in _sources(ws)

    def test_a_file_without_a_decision_is_not_processed(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        s = ws.decisions_path.read_text()
        block = s[s.index('[files."data/ctd/cast_direct.csv"]'):].split("\n\n")[0]
        ws.decisions_path.write_text(s.replace(block, ""))
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS
        assert any("have no entry" in p["what"] for p in r["problems"])

    def test_include_must_be_true_or_false(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        _set(ws, "data/ctd/cast_direct.csv", include="yes")
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS
        assert any("include = 'yes'" in p["what"] for p in r["problems"])

    def test_a_typo_in_decisions_stops_at_check_with_the_line(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        ws.decisions_path.write_text(ws.decisions_path.read_text() + "\nbroken = [\n")
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_FAILED
        p = r["problems"][0]
        assert p["stage"] == "check" and "(at " in p["why"]       # tomllib names where


class TestFailuresSayWhatToDo:
    def test_a_file_that_cannot_be_written_is_reported_and_the_rest_are_written(self, env):
        (env / "data" / "ctd" / "broken.csv").write_text(
            "# LAT: 21.0\n# LON: 142.3\n# DATE: 2015-11-09\n"
            "Depth (m),Temperature (deg C)\n10,20\n")
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        real = pa.process_one_file

        def failing(url, *a, **k):
            if url.endswith("broken.csv"):
                return {"ok": False, "stage": "mapping", "error": "no depth column mapped"}
            return real(url, *a, **k)
        import unittest.mock as um
        with um.patch.object(pa, "process_one_file", failing):
            r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_FAILED
        failed = [p for p in r["problems"] if p.get("file") == "data/ctd/broken.csv"]
        assert failed and "include = false" in failed[0]["how_to_fix"]
        assert "ctd/cast_direct.csv" in _sources(ws)        # the rest were written
        assert r["stages"]["verify"]["status"] == "done"
        _set(ws, "data/ctd/broken.csv", include=False)
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK

    def test_a_transient_failure_fails_the_stage_and_confirming_again_resumes(self, env,
                                                                                monkeypatch):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        real = pa.process_one_file
        calls = []

        def flaky(url, *a, **k):
            calls.append(url)
            if url.endswith("2.csv"):
                return {"ok": False, "stage": "download_file", "error": "503 Service Unavailable"}
            return real(url, *a, **k)
        monkeypatch.setattr(pa, "process_one_file", flaky)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_FAILED
        assert any("retry" in p["how_to_fix"].lower() for p in r["problems"])
        calls.clear()
        monkeypatch.setattr(pa, "process_one_file", lambda url, *a, **k: (calls.append(url),
                                                                          real(url, *a, **k))[1])
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert [os.path.basename(c) for c in calls] == ["2.csv"]      # only the failure

    def test_ctrl_c_stops_cleanly_and_confirming_again_continues(self, env, monkeypatch):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        real = pa.process_one_file

        def interrupt_on_second(url, *a, **k):
            if url.endswith("2.csv"):
                raise KeyboardInterrupt
            return real(url, *a, **k)
        monkeypatch.setattr(pa, "process_one_file", interrupt_on_second)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_INTERRUPTED
        state = json.loads(ws.state_path.read_text())
        assert state["stages"]["process"]["status"] == "pending"
        monkeypatch.setattr(pa, "process_one_file", real)
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert not ws.lock_path.exists()

    def test_a_second_run_is_refused_while_one_is_running(self, env):
        ws = Workspace(ACC)
        ws.run_dir.mkdir(parents=True)
        ws.lock_path.write_text(str(os.getppid()))            # a live process that is not us
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_FAILED
        assert "already being run" in r["problems"][0]["what"]

    def test_a_stale_lock_is_taken_over(self, env):
        ws = Workspace(ACC)
        ws.run_dir.mkdir(parents=True)
        ws.lock_path.write_text("999999")                       # no such process
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK

    def test_a_lock_from_another_machine_is_left_alone(self, env):
        ws = Workspace(ACC)
        ws.run_dir.mkdir(parents=True)
        ws.lock_path.write_text("999999 some-other-host")      # its pid means nothing here
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_FAILED and "some-other-host" in r["problems"][0]["why"]

    def test_an_input_folder_holding_the_workspace_is_refused(self, env, tmp_path):
        r = run_pipeline(str(tmp_path), accession_id=ACC)      # holds runs/, staging/, ...
        assert r["exit_code"] == EXIT_FAILED
        assert "pipeline's own folders" in r["problems"][0]["what"]

    def test_an_input_with_no_accession_number_asks_for_one(self, env, tmp_path):
        d = tmp_path / "survey_2024"
        d.mkdir()
        r = run_pipeline(str(d))
        assert r["exit_code"] == EXIT_FAILED and "--accession-id" in r["problems"][0]["how_to_fix"]


class TestCommandLine:
    def test_run_confirm_and_status(self, env, capsys):
        assert main(["run", str(env)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "Decisions written:" in out and f"uv run wod-etl confirm {ACC}" in out
        assert "Fill these in before confirming" in out
        _fill(Workspace(ACC))
        assert main(["confirm", ACC]) == EXIT_OK
        out = capsys.readouterr().out
        assert "4 cast(s) in 1 file(s) written and verified" in out
        assert f"wod_ctd_2015_{ACC}.nc  4 CTD cast(s) from 4 source file(s)" in out
        assert main(["status", ACC]) == 0
        out = capsys.readouterr().out
        assert "last command: confirm" in out and re.search(r"process\s+done", out)

    def test_a_refused_confirm_does_not_list_earlier_output_as_written(self, env, capsys):
        assert main(["run", str(env)]) == EXIT_OK
        _fill(Workspace(ACC))
        assert main(["confirm", ACC]) == EXIT_OK
        assert "Output:" in capsys.readouterr().out
        ws = Workspace(ACC)
        ws.decisions_path.write_text(ws.decisions_path.read_text().replace(
            "Salinity (PSU)", "Salinity (UNIT?)"))
        assert main(["confirm", ACC]) == EXIT_DECISIONS
        assert "Output:" not in capsys.readouterr().out

    def test_a_confirm_that_stopped_before_writing_lists_no_output(self, monkeypatch, capsys):
        from wod_etl.orchestration import cli

        done = {"status": "done", "outputs": {"files": [
            {"name": "wod_ctd_2015_x.nc", "casts": 1, "dataset": "CTD", "sources": []}]}}
        monkeypatch.setattr(cli, "confirm_pipeline", lambda *a, **k: {
            "accession": ACC, "exit_code": EXIT_FAILED, "problems": [], "paths": {},
            "stages": {"check": {"status": "done"}, "stage": {"status": "failed"},
                       "process": done}})
        assert main(["confirm", ACC]) == EXIT_FAILED
        assert "Output:" not in capsys.readouterr().out

    def test_nothing_to_write_is_not_called_verified(self, monkeypatch, capsys):
        from wod_etl.orchestration import cli

        monkeypatch.setattr(cli, "confirm_pipeline", lambda *a, **k: {
            "accession": ACC, "exit_code": EXIT_OK, "problems": [], "paths": {},
            "stages": {"verify": {"outputs": {"output_verified": None}}}})
        assert main(["confirm", ACC]) == EXIT_OK
        out = capsys.readouterr().out
        assert "Nothing was written" in out and "verified" not in out.split("Nothing")[0]

    def test_from_redoes_a_finished_stage(self, env):
        run_pipeline(str(env))
        _fill(Workspace(ACC))
        confirm_pipeline(ACC)
        lines, say = _lines()
        assert confirm_pipeline(ACC, from_stage="process", progress=say)["exit_code"] == EXIT_OK
        assert "(unchanged)" not in next(l for l in lines if re.search(r"\] process\s", l))
        with pytest.raises(SystemExit):
            main(["confirm", ACC, "--from", "triage"])          # triage is run's, not confirm's


class TestOtherRecipeKinds:
    def test_per_file_recipe(self, env):
        d = env / "data" / "casts"
        d.mkdir()
        for st in (1, 2):
            (d / f"stn{st}.csv").write_text("Depth (m),Temperature (deg C)\n"
                                           + "".join(f"{z},{20 - z / 10}\n" for z in range(15)))
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        # Nothing in the casts names an instrument or locates them: the reviewer includes
        # them (triage's guess from the depth spacing) and says how they are located.
        _set(ws, "data/casts/*", include=True, dataset="CTD")
        with open(ws.decisions_path, "a") as fh:
            fh.write('\n[profiles.casts]\nkind = "per_file"\n'
                     'files = ["data/casts/stn1.csv", "data/casts/stn2.csv"]\n'
                     "station_pattern = '(\\d+)$'\n"
                     'station_list = "data/0-data/X1_StationInfo.csv"\n')
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert {"casts/1.csv", "casts/2.csv"} <= set(_sources(ws))

    def test_script_recipe_is_the_escape_hatch(self, env, tmp_path):
        from wod_etl import config

        repo_runs = config.ROOT / "data" / "runs" / ACC
        was_there = repo_runs.exists()
        script = tmp_path / "stage_custom.py"
        script.write_text(
            "import os, sys\nfrom wod_etl.staging import write_staged_cast\n"
            "out = os.environ['WOD_STAGING_OUT']\n"
            "write_staged_cast(out + '/a.csv', ['Depth (m)', 'Temperature (deg C)'],\n"
            "    [[z, 20 - z / 10] for z in range(15)], latitude=21, longitude=142,\n"
            "    date='2015-11-09', metadata={'Accession': os.environ['WOD_ACCESSION']})\n"
            "print('root is', os.environ['WOD_PROJECT_ROOT'], flush=True)\n"
            "sys.stdout.buffer.write(b'caf\\xe9 in latin-1\\n')\n")
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        with open(ws.decisions_path, "a") as fh:
            fh.write(f'\n[profiles.custom]\nkind = "script"\nscript = \'{script}\'\n')
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert "custom/a.csv" in _sources(ws)
        # What the script printed, in any encoding, and what its wod_etl calls logged, are
        # in this run's log; the script did not open a run folder of its own.
        log = ws.log_path.read_text()
        assert f"root is {config.ROOT}" in log and "caf� in latin-1" in log
        assert re.search(r"stderr: INFO wod_etl\.staging\.write_cast: staged \S+a\.csv", log)
        events = (ws.run_dir / "events.jsonl").read_text().splitlines()
        assert {json.loads(e)["actor"] for e in events} == {"cli"}
        assert was_there or not repo_runs.exists()
        script.write_text("raise SystemExit('cannot find the readme')\n")
        r = confirm_pipeline(ACC)                        # the script changed: rerun, and fail
        assert r["exit_code"] == EXIT_FAILED
        assert "cannot find the readme" in r["problems"][0]["why"]


class TestWhatThePipelineCannotWriteYet:
    def test_mooring_records_are_left_out_with_why_and_nothing_is_written(self, env, tmp_path):
        from helpers import JPEG_HEAD, write_mooring_nc

        acc = tmp_path / "src" / "0999998"
        (acc / "about").mkdir(parents=True)
        (acc / "data" / "0-data").mkdir(parents=True)
        (acc / "about" / "0999998_map.jpg").write_bytes(JPEG_HEAD + b"\x00" * 64)
        write_mooring_nc(acc / "data" / "0-data" / "18bs8a_s39_0051m.cf.nc")
        assert run_pipeline(str(acc))["exit_code"] == EXIT_OK
        ws = Workspace("0999998")
        entry = _decisions(ws)["files"]["data/0-data/18bs8a_s39_0051m.cf.nc"]
        assert entry["include"] is False and "MRB" in entry["why"]
        r = confirm_pipeline("0999998")
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert not r["problems"]
        report = ws.report_md.read_text()
        assert "finished: nothing to write" in report and "Plankton" not in report
        assert "moored instrument record: Seabird SBE-39 at one place" in report


class TestACodeChangeDoesNotSendTheReviewBack:
    def test_confirm_goes_ahead_when_the_new_code_triages_the_files_the_same(self, env,
                                                                                monkeypatch):
        from wod_etl.orchestration import pipeline

        run_pipeline(str(env))
        _fill(Workspace(ACC))
        real = pipeline._code_hash
        monkeypatch.setattr(pipeline, "_code_hash",
                            lambda parts: "changed" if parts == pipeline.TRIAGE_CODE
                            else real(parts))
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert "triages the files as reviewed" in Workspace(ACC).log_path.read_text()

    def test_a_refused_confirm_reports_only_why_it_was_refused(self, env, monkeypatch):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        real = pa.process_one_file
        monkeypatch.setattr(pa, "process_one_file", lambda url, *a, **k: (
            {"ok": False, "stage": "mapping", "error": "boom"} if url.endswith("2.csv")
            else real(url, *a, **k)))
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_FAILED
        (env / "data" / "ctd" / "cast_two.csv").write_text(DIRECT)
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_DECISIONS
        assert [p["stage"] for p in r["problems"]] == ["check"]
