"""`wod-etl run`: the whole workflow for one accession, resumable, run by a person."""
import importlib
import json
import os
import re

import pytest

from wod_etl.orchestration.cli import main
from wod_etl.orchestration.pipeline import (EXIT_DECISIONS, EXIT_FAILED, EXIT_INTERRUPTED,
                                            EXIT_OK, Workspace, run_pipeline)

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
    rows = [f"X1,{st},CTD,{var},{z}.0,{20 - z / 10 + st:.3f}"
            for st in (1, 2, 3) for z in range(1, 30)]
    return "Cruise,StNo,ObsItem,parm,depth,value\n" + "\n".join(rows) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Every pipeline folder under tmp_path, and a small accession: one direct cast, two
    long-format variables for three stations, and their station list."""
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
    """What the person does: state units, confirm the recipe."""
    p = ws.decisions_path
    s = p.read_text()
    s = s.replace("Temperature (UNIT?)", "Temperature (deg C)").replace(
        "Salinity (UNIT?)", "Salinity (PSU)")
    s = s.replace("confirmed = false", "confirmed = true", 1)
    for old, new in extra.items():
        s = s.replace(old, new)
    p.write_text(s)


def _lines():
    out = []
    return out, out.append


class TestFirstRunPausesThenFinishes:
    def test_pause_fill_finish(self, env):
        lines, say = _lines()
        r = run_pipeline(str(env), progress=say)
        assert r["exit_code"] == EXIT_DECISIONS
        ws = Workspace(ACC)
        text = ws.decisions_path.read_text()
        assert "[profiles.x1_ctd]" in text and "UNIT?" in text
        assert 'station_list = "data/0-data/X1_StationInfo.csv"' in text
        assert any("unit of" in p["what"] for p in r["problems"])
        assert ws.report_md.exists() and "PAUSED" in ws.report_md.read_text()

        _fill(ws)
        lines, say = _lines()
        r = run_pipeline(str(env), progress=say)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert all("(unchanged)" in l for l in lines[:3])
        outputs = sorted(p.name for p in (ws.processed_dir / "profiles").glob("*.nc"))
        assert outputs == ["data__ctd__cast_direct.nc", "x1_ctd__1.nc", "x1_ctd__2.nc",
                           "x1_ctd__3.nc"]
        log = ws.log_path.read_text()
        assert log.count("cli run ") == 2 and "exit 2" in log and "exit 0" in log

    def test_a_rerun_with_nothing_changed_redoes_nothing(self, env):
        run_pipeline(str(env))
        _fill(Workspace(ACC))
        run_pipeline(str(env))
        before = json.loads(Workspace(ACC).state_path.read_text())["stages"]
        lines, say = _lines()
        assert run_pipeline(str(env), progress=say)["exit_code"] == EXIT_OK
        after = json.loads(Workspace(ACC).state_path.read_text())["stages"]
        assert sum("(unchanged)" in l for l in lines) == 8
        assert after["process"]["finished_at"] == before["process"]["finished_at"]

    def test_a_decision_about_an_unrelated_file_does_not_rerun_processing(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        run_pipeline(str(env))
        before = json.loads(ws.state_path.read_text())["stages"]["process"]["finished_at"]
        with open(ws.decisions_path, "a") as fh:
            fh.write('\n[files."NCEI-Readme.txt"]\naction = "skip"\n')
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK
        after = json.loads(ws.state_path.read_text())["stages"]
        assert after["process"]["finished_at"] == before
        assert after["decide"]["status"] == "done"

    def test_changing_a_unit_restages_and_reprocesses(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        run_pipeline(str(env))
        s = ws.decisions_path.read_text().replace("Salinity (PSU)", "Salinity (psu)")
        ws.decisions_path.write_text(s)
        lines, say = _lines()
        assert run_pipeline(str(env), progress=say)["exit_code"] == EXIT_OK
        assert not any("(unchanged)" in l for l in lines
                       if re.search(r"\] (stage|process)\s", l)), lines
        staged = (ws.staging_dir / "profiles" / "x1_ctd" / "1.csv").read_text()
        assert "Salinity (psu)" in staged


class TestFailuresSayWhatToDo:
    def test_a_file_that_cannot_map_becomes_a_decision_not_a_retry_loop(self, env):
        (env / "data" / "ctd" / "no_depth.csv").write_text(
            "# LAT: 21.0\n# LON: 142.3\n# DATE: 2015-11-09\nTemperature (deg C)\n20\n19\n")
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_DECISIONS
        failed = [p for p in r["problems"] if p.get("file") == "data/ctd/no_depth.csv"]
        assert failed and "skip" in failed[0]["how_to_fix"]
        assert '[files."data/ctd/no_depth.csv"]' in ws.decisions_path.read_text()
        s = ws.decisions_path.read_text()
        i = s.index('[files."data/ctd/no_depth.csv"]')
        ws.decisions_path.write_text(s[:i] + s[i:].replace('action = ""', 'action = "skip"', 1))
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK

    def test_a_transient_failure_fails_the_stage_and_a_rerun_resumes(self, env, monkeypatch):
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
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_FAILED
        assert any("rerun" in p["how_to_fix"].lower() for p in r["problems"])
        monkeypatch.setattr(pa, "process_one_file", real)
        calls.clear()
        monkeypatch.setattr(pa, "process_one_file", lambda url, *a, **k: (calls.append(url),
                                                                          real(url, *a, **k))[1])
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK
        assert [os.path.basename(c) for c in calls] == ["2.csv"]      # only the failure

    def test_ctrl_c_stops_cleanly_and_the_rerun_continues(self, env, monkeypatch):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        real = pa.process_one_file
        done = []

        def interrupt_on_second(url, *a, **k):
            if url.endswith("2.csv"):
                raise KeyboardInterrupt
            done.append(url)
            return real(url, *a, **k)
        monkeypatch.setattr(pa, "process_one_file", interrupt_on_second)
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_INTERRUPTED
        state = json.loads(ws.state_path.read_text())
        assert state["stages"]["process"]["status"] == "pending"
        monkeypatch.setattr(pa, "process_one_file", real)
        assert run_pipeline(str(env))["exit_code"] == EXIT_OK
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
        assert run_pipeline(str(env))["exit_code"] == EXIT_DECISIONS

    def test_an_input_with_no_accession_number_asks_for_one(self, env, tmp_path):
        d = tmp_path / "survey_2024"
        d.mkdir()
        r = run_pipeline(str(d))
        assert r["exit_code"] == EXIT_FAILED and "--accession-id" in r["problems"][0]["how_to_fix"]

    def test_a_typo_in_decisions_stops_at_decide_with_the_line(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        ws.decisions_path.write_text(ws.decisions_path.read_text() + "\nbroken = [\n")
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_FAILED
        p = r["problems"][0]
        assert p["stage"] == "decide" and "(at " in p["why"]       # tomllib names where


class TestCommandLine:
    def test_run_and_status(self, env, capsys):
        assert main(["run", str(env)]) == EXIT_DECISIONS
        out = capsys.readouterr().out
        assert "Paused: decisions needed." in out and "decisions.toml" in out
        assert main(["status", ACC]) == 0
        out = capsys.readouterr().out
        assert "decide" in out and "blocked" in out

    def test_from_reruns_a_finished_stage(self, env):
        run_pipeline(str(env))
        _fill(Workspace(ACC))
        run_pipeline(str(env))
        lines, say = _lines()
        assert run_pipeline(str(env), from_stage="process", progress=say)["exit_code"] == EXIT_OK
        assert "(unchanged)" not in next(l for l in lines if re.search(r"\] process\s", l))


class TestOtherRecipeKinds:
    def _base(self, env):
        run_pipeline(str(env))
        ws = Workspace(ACC)
        _fill(ws)
        return ws

    def test_per_file_recipe(self, env):
        d = env / "data" / "casts"
        d.mkdir()
        for st in (1, 2):
            (d / f"stn{st}.csv").write_text("Depth (m),Temperature (deg C)\n"
                                           + "".join(f"{z},{20 - z / 10}\n" for z in range(15)))
        ws = self._base(env)
        with open(ws.decisions_path, "a") as fh:
            fh.write('\n[profiles.casts]\nkind = "per_file"\nconfirmed = true\n'
                     'files = ["data/casts/stn1.csv", "data/casts/stn2.csv"]\n'
                     "station_pattern = '(\\d+)$'\n"
                     'station_list = "data/0-data/X1_StationInfo.csv"\n')
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert (ws.processed_dir / "profiles" / "casts__1.nc").exists()

    def test_script_recipe_is_the_escape_hatch(self, env, tmp_path):
        script = tmp_path / "stage_custom.py"
        script.write_text(
            "import os\nfrom wod_etl.staging import write_staged_cast\n"
            "out = os.environ['WOD_STAGING_OUT']\n"
            "write_staged_cast(out + '/a.csv', ['Depth (m)', 'Temperature (deg C)'],\n"
            "    [[z, 20 - z / 10] for z in range(15)], latitude=21, longitude=142,\n"
            "    date='2015-11-09', metadata={'Accession': os.environ['WOD_ACCESSION']})\n")
        ws = self._base(env)
        with open(ws.decisions_path, "a") as fh:
            fh.write(f'\n[profiles.custom]\nkind = "script"\nconfirmed = true\n'
                     f"script = '{script}'\n")
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert (ws.processed_dir / "profiles" / "custom__a.nc").exists()
        script.write_text("raise SystemExit('cannot find the readme')\n")
        r = run_pipeline(str(env))                       # the script changed: rerun, and fail
        assert r["exit_code"] == EXIT_FAILED
        assert "cannot find the readme" in r["problems"][0]["why"]

    def test_taxa_recipe_writes_hauls_and_pending_ones_do_not_block(self, env):
        (env / "data" / "0-data" / "X1_ringnet.csv").write_text(
            "Cruise,StNo,genus,species,stage,value\n"
            "X1,1,Scomber,japonicus,larvae,2\nX1,2,Auxis,na,larvae,1\n")
        ws = self._base(env)
        r = run_pipeline(str(env))
        assert r["exit_code"] == EXIT_OK                 # the taxa skeleton is pending only
        assert "[taxa.x1_ringnet]" in ws.decisions_path.read_text()
        s = ws.decisions_path.read_text()
        i = s.index("[taxa.x1_ringnet]")
        block = s[i:].split("\n\n")[0]
        filled = ('[taxa.x1_ringnet]\nkind = "long"\nconfirmed = true\n'
                  'file = "data/0-data/X1_ringnet.csv"\n'
                  'station_list = "data/0-data/X1_StationInfo.csv"\ncast_key = "StNo"\n'
                  'taxon_columns = ["genus", "species"]\nstage_column = "stage"\n'
                  'count_column = "value"\nunit = "number per sample"\nupper_depth = 0\n'
                  'lower_depth = 0\nbiological_header = { "Gear code" = "Ring Net" }')
        ws.decisions_path.write_text(s.replace(block, filled))
        lines, say = _lines()
        r = run_pipeline(str(env), progress=say)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        assert sorted(p.name for p in (ws.processed_dir / "taxa").glob("*.nc")) == [
            "x1_ringnet__1.nc", "x1_ringnet__2.nc"]
        assert "(unchanged)" in next(l for l in lines if re.search(r"\] process\s", l))
