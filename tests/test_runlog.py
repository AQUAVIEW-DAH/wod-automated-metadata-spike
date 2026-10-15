"""The per-accession run log: written by any run that touches an accession, person or agent."""
import json
import logging

import pytest

from wod_etl import runlog
from wod_etl.orchestration import process_accession
from wod_etl.staging import write_staged_cast

CAST = ("# LAT: 61.9\n# LON: -172.2\n# DATE: 2023-06-20\n"
        "Depth (m),Temp (deg C)\n0,20\n10,18\n")


def _events(runs, acc):
    path = runs / acc / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def acc(tmp_path):
    d = tmp_path / "survey"
    d.mkdir()
    (d / "cast_01.csv").write_text(CAST)
    (d / "broken.csv").write_text("no tabular data here at all\n")
    return d


class TestAgentRunsAreLogged:
    def test_process_accession_writes_both_logs(self, acc, tmp_path, isolated_runs_dir):
        process_accession(str(acc), dataset_type="CTD", output_dir=str(tmp_path / "o"),
                          raw_dir=str(tmp_path / "r"), run_id="t_log", resume=False,
                          accession="0303928")
        text = (isolated_runs_dir / "0303928" / "run.log").read_text()
        assert "library run" in text and "end of run" in text
        assert "done cast_01.csv" in text
        assert "FAILED broken.csv at sniff_format" in text
        kinds = [e.get("fields", {}).get("kind") for e in _events(isolated_runs_dir, "0303928")]
        assert kinds[0] == "start" and kinds[-1] == "end"
        assert "run_end" in kinds

    def test_the_accession_is_worked_out_from_the_path(self, tmp_path, isolated_runs_dir):
        d = tmp_path / "staging" / "0303928" / "CTD"
        d.mkdir(parents=True)
        (d / "cast_01.csv").write_text(CAST)
        process_accession(str(d), dataset_type="CTD", output_dir=str(tmp_path / "o"),
                          raw_dir=str(tmp_path / "r"), run_id="t_log2", resume=False)
        assert (isolated_runs_dir / "0303928" / "run.log").exists()

    def test_no_accession_means_no_log(self, acc, tmp_path, isolated_runs_dir):
        process_accession(str(acc), dataset_type="CTD", output_dir=str(tmp_path / "o"),
                          raw_dir=str(tmp_path / "r"), run_id="t_nolog", resume=False)
        assert not any(isolated_runs_dir.iterdir())

    def test_a_staged_cast_is_logged_without_a_banner(self, tmp_path, isolated_runs_dir):
        write_staged_cast(tmp_path / "staging" / "0303928" / "c.csv", ["Depth (m)"], [[0]],
                          latitude=1, longitude=2, date="2020-01-01", notes=["from the list"])
        events = _events(isolated_runs_dir, "0303928")
        assert len(events) == 1
        assert events[0]["fields"]["kind"] == "staged_cast"
        assert events[0]["fields"]["notes"] == ["from the list"]


class TestTheLogItself:
    def test_nesting_writes_one_banner_and_no_duplicate_lines(self, acc, tmp_path,
                                                              isolated_runs_dir):
        with runlog.accession_log("0303928", actor="cli") as ctx:
            process_accession(str(acc), dataset_type="CTD", output_dir=str(tmp_path / "o"),
                              raw_dir=str(tmp_path / "r"), run_id="t_nest", resume=False,
                              accession="0303928")
            ctx["status"] = "finished"
        events = _events(isolated_runs_dir, "0303928")
        starts = [e for e in events if e.get("fields", {}).get("kind") == "start"]
        assert len(starts) == 1 and starts[0]["actor"] == "cli"
        done = [e for e in events if e["message"].startswith("done cast_01.csv")]
        assert len(done) == 1
        assert "end of run" in events[-1]["message"] and "finished" in events[-1]["message"]

    def test_handlers_and_level_are_restored(self, isolated_runs_dir):
        logger = logging.getLogger("wod_etl")
        before = (list(logger.handlers), logger.level)
        with runlog.accession_log("0303928"):
            assert runlog.active()["accession"] == "0303928"
        assert (list(logger.handlers), logger.level) == before
        assert runlog.active() is None

    def test_an_exception_is_logged_and_still_raised(self, isolated_runs_dir):
        with pytest.raises(ValueError):
            with runlog.accession_log("0303928"):
                raise ValueError("bad input")
        text = (isolated_runs_dir / "0303928" / "run.log").read_text()
        assert "run raised ValueError: bad input" in text

    def test_a_run_folder_that_cannot_be_written_does_not_stop_the_work(self, tmp_path,
                                                                        monkeypatch, capsys):
        from wod_etl import config

        in_the_way = tmp_path / "a_file"
        in_the_way.write_text("")
        monkeypatch.setattr(config, "RUNS_DIR", in_the_way / "runs")
        with runlog.accession_log("0303928") as ctx:
            logging.getLogger("wod_etl.x").info("carrying on")
        assert ctx["dir"] is None and "no run log for accession 0303928" in capsys.readouterr().err

    def test_under_a_parent_run_the_log_goes_to_stderr(self, tmp_path, monkeypatch, capsys,
                                                       isolated_runs_dir):
        monkeypatch.setenv(runlog.TO_STDERR_ENV, "1")
        write_staged_cast(tmp_path / "staging" / "0303928" / "c.csv", ["Depth (m)"], [[0]],
                          latitude=1, longitude=2, date="2020-01-01")
        assert not (isolated_runs_dir / "0303928").exists()
        err = capsys.readouterr().err
        assert "INFO wod_etl.staging.write_cast: staged" in err and "=====" not in err

    def test_reruns_append(self, isolated_runs_dir):
        for _ in range(2):
            with runlog.accession_log("0303928"):
                pass
        text = (isolated_runs_dir / "0303928" / "run.log").read_text()
        assert text.count("agent run ") == 2 and text.count("end of run ") == 2
