"""`wod-etl web`: the browser page drives the same run and confirm, one command at a time."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

from wod_etl.web import server as web

ACC = "0999998"
CAST = ("# LAT: 21.0\n# LON: 142.3\n# DATE: 2015-11-09\n"
        "Depth (m),Temperature (deg C),Salinity (PSU)\n"
        + "".join(f"{z},{25 - z / 10:.2f},34.5\n" for z in range(0, 20)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The web server, data folders under tmp_path, and an accession of one CTD cast."""
    from wod_etl import config

    for name in ("RAW_DIR", "STAGING_DIR", "PROCESSED_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
    acc = tmp_path / "src" / ACC
    (acc / "data" / "ctd").mkdir(parents=True)
    (acc / "NCEI-Readme.txt").write_text("This is the standard bundle for an NCEI AIP.\n")
    (acc / "data" / "ctd" / "cast_1.csv").write_text(CAST)
    monkeypatch.setattr(web, "_job", {})
    return TestClient(web.app), acc


def _wait(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/status/{ACC}").json()
        if not (s["job"] or {}).get("running"):
            return s
        time.sleep(0.05)
    raise AssertionError("the command did not finish")


class TestRunReviewConfirm:
    def test_the_two_steps_and_the_review_between_them(self, client):
        c, acc = client
        assert c.post("/api/run", json={"source": str(acc)}).json()["accession"] == ACC
        s = _wait(c)
        assert (s["last_command"], s["last_exit_code"]) == ("run", 0)
        assert [st["status"] for st in s["stages"][:4]] == ["done"] * 4
        assert "review them, then confirm" in s["outcome"]

        files = {sec["section"]: sec["files"] for sec in c.get(f"/api/files/{ACC}").json()[
            "sections"]}
        [cast] = files["in"]
        assert (cast["path"], cast["include"], cast["dataset"], cast["decided_by"]) == (
            "data/ctd/cast_1.csv", True, "CTD", "data/ctd/cast_1.csv")
        assert [f["name"] for f in files["docs"]] == ["NCEI-Readme.txt"]

        d = c.get(f"/api/decisions/{ACC}").json()
        assert d["exists"] and '[files."data/ctd/cast_1.csv"]' in d["text"]
        broken = d["text"].replace("include = true", "include = yes", 1)
        bad = c.put(f"/api/decisions/{ACC}", json={"text": broken, "mtime": d["mtime"]})
        assert bad.status_code == 422 and "(at line " in bad.json()["detail"]
        assert c.get(f"/api/decisions/{ACC}").json()["text"] == d["text"]   # nothing saved
        stale = c.put(f"/api/decisions/{ACC}", json={"text": d["text"], "mtime": 1})
        assert stale.status_code == 409
        edited = d["text"].replace("[metadata]", '[metadata]\ncruise = "X1"', 1)
        assert c.put(f"/api/decisions/{ACC}", json={"text": edited, "mtime": d["mtime"]}
                     ).json()["saved"]

        assert c.post(f"/api/confirm/{ACC}").status_code == 200
        s = _wait(c)
        assert (s["last_command"], s["last_exit_code"]) == ("confirm", 0), s["problems"]
        assert {st["name"]: st["status"] for st in s["stages"]} == dict.fromkeys(
            ["resolve", "fetch", "triage", "decide", "check", "stage", "process", "verify",
             "report"], "done")
        out = c.get(f"/api/outputs/{ACC}").json()
        assert out["verified"] is True
        [f] = out["files"]
        assert (f["name"], f["dataset"], f["year"], f["casts"]) == (
            f"wod_ctd_2015_{ACC}.nc", "CTD", 2015, 1)
        got = c.get(f"/api/outputs/{ACC}/{f['name']}")
        assert got.status_code == 200 and got.content[:4] == b"\x89HDF"
        assert "## WOD files written" in c.get(f"/api/report/{ACC}").text
        assert "cli confirm" in c.get(f"/api/log/{ACC}").text
        assert c.get("/api/accessions").json()[0]["accession"] == ACC


class TestOneCommandAtATime:
    def test_a_second_command_and_a_save_wait_for_the_first(self, client, monkeypatch,
                                                              tmp_path):
        c, acc = client
        signal = tmp_path / "release"
        # A stand-in for the command: says one line, then waits to be released.
        slow = ("import os, sys, time\nprint('[1/5] resolve  ...', flush=True)\n"
                "while not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n")
        monkeypatch.setattr(web, "_command_line",
                            lambda *a: [sys.executable, "-c", slow, str(signal)])
        assert c.post("/api/run", json={"source": str(acc)}).status_code == 200
        again = c.post("/api/run", json={"source": str(acc)})
        assert again.status_code == 409 and "one command at a time" in again.json()["detail"]
        assert c.put(f"/api/decisions/{ACC}", json={"text": ""}).status_code == 409
        for _ in range(200):                          # wait for its first output line
            s = c.get(f"/api/status/{ACC}").json()
            if s["job"]["lines"]:
                break
            time.sleep(0.02)
        assert s["job"]["running"] and s["job"]["lines"] == ["[1/5] resolve  ..."]
        signal.write_text("go")
        assert _wait(c)["job"]["exit_code"] == 0


class TestWhatItRefuses:
    def test_bad_input_is_said_not_run(self, client):
        c, acc = client
        assert c.post("/api/run", json={"source": "  "}).status_code == 400
        no_acc = c.post("/api/run", json={"source": str(acc.parent)})
        assert no_acc.status_code == 400 and "accession id" in no_acc.json()["detail"]
        assert c.post(f"/api/confirm/{ACC}").status_code == 404          # not run yet
        assert c.get("/api/status/not!an:id").status_code == 400
        assert c.get(f"/api/outputs/{ACC}/notes.nc").status_code == 404
        assert c.get(f"/api/outputs/{ACC}/wod_ctd_2015_{ACC}.nc").status_code == 404
