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

        text = c.get(f"/api/input-preview/{ACC}/data/ctd/cast_1.csv").json()
        assert text["kind"] == "text" and text["text"].startswith("# LAT: 21.0")
        raw = c.get(f"/api/input-file/{ACC}/data/ctd/cast_1.csv")
        assert raw.status_code == 200 and raw.text == CAST
        for path in ("data/ctd/missing.csv", "%2E%2E/%2E%2E/etc/passwd"):
            assert c.get(f"/api/input-preview/{ACC}/{path}").status_code == 404

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

        about = c.get(f"/api/preview/{ACC}/{f['name']}").json()
        assert about["attributes"]["featureType"] == "profile"
        assert about["level_dimension"] == "z_obs"
        [cast] = about["casts"]
        assert (cast["time"], cast["lat"], cast["lon"], cast["size"]) == (
            "2015-11-09", 21.0, 142.3, 20)
        one = c.get(f"/api/preview/{ACC}/{f['name']}/casts/0").json()
        [levels] = one["tables"]
        z = next(col for col in levels["columns"] if col["name"] == "z")
        temperature = next(col for col in levels["columns"] if col["name"] == "temperature")
        assert z["down"] and z["values"][:3] == [0.0, 1.0, 2.0]
        assert temperature["values"][:3] == [25.0, 24.9, 24.8]
        assert c.get(f"/api/preview/{ACC}/{f['name']}/casts/1").status_code == 404
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
        for _ in range(200):
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
        assert c.get(f"/api/preview/{ACC}/notes.nc").status_code == 404
        assert c.get(f"/api/preview/{ACC}/wod_ctd_2015_{ACC}.nc/casts/0").status_code == 404


class TestPreview:
    """The preview reads a file by its ragged counts, not by the writer's variable names."""

    def test_each_cast_gets_its_own_levels_and_fields(self, tmp_path):
        from helpers import make_cast
        from wod_etl.web import preview
        from wod_etl.writers import write_ragged_profiles

        p = tmp_path / "out.nc"
        write_ragged_profiles([make_cast(dic=True), make_cast(lat=62.4, rows=2)], str(p))
        about = preview.describe(str(p))
        assert [(c["lat"], c["size"]) for c in about["casts"]] == [(61.93, 3), (62.4, 2)]
        assert about["casts"][0]["time"] == "1985-06-12"

        second = preview.cast(str(p), 1)
        fields = {f["name"]: f["value"] for f in second["fields"]}
        assert fields["wod_variable_codes"] == [1]
        assert fields["wod_unique_cast"] is None
        [levels] = second["tables"]
        assert levels["rows"] == 2
        assert "dissolved_inorganic_carbon" in levels["empty"]
        assert [c["values"] for c in levels["columns"] if c["name"] == "temperature"] == [
            [20.0, 18.0]]
        with pytest.raises(IndexError):
            preview.cast(str(p), 2)


class TestInputPreview:
    """An input file is shown as its originator sent it."""

    def test_a_workbook_shows_each_sheet_s_first_rows(self, tmp_path):
        import pandas as pd

        from wod_etl.web.inputs import preview_input

        p = tmp_path / "ctd.xlsx"
        with pd.ExcelWriter(p) as w:
            pd.DataFrame({"Depth (m)": [0, 10], "Temp": [5.1, 4.2]}).to_excel(
                w, sheet_name="cast 1", index=False)
        r = preview_input(p)
        assert r["kind"] == "sheets"
        [sheet] = r["sheets"]
        assert sheet["name"] == "cast 1" and sheet["rows"][:2] == [["Depth (m)", "Temp"],
                                                                   ["0", "5.1"]]

    def test_a_netcdf_shows_its_variables_and_their_first_values(self, tmp_path):
        from helpers import write_mooring_nc
        from wod_etl.web.inputs import preview_input
        from wod_etl.web.preview import describe_input

        p = tmp_path / "t.nc"
        write_mooring_nc(p, days=1)
        assert preview_input(p)["kind"] == "netcdf"
        r = describe_input(str(p))
        t = next(v for v in r["variables"] if v["name"] == "T_20")
        assert t["dimensions"] == ["time", "depth", "lat", "lon"] and t["head"][:2] == [5.0, 5.0]
        assert r["dimensions"]["time"] == 25

    def test_text_is_laid_out_to_be_read_and_kept_as_stored(self, tmp_path):
        from wod_etl.web.inputs import preview_input

        csv = tmp_path / "bottle.csv"
        csv.write_text("Cruise C145 bottle data,,\n\nStation,Depth (m),Temp\n"
                       + "".join(f"A01,{z},{5 - z / 100}\n" for z in range(0, 50, 10)))
        r = preview_input(csv)
        f = r["formatted"]
        assert r["text"] == csv.read_text()                     # the file as stored
        assert (f["layout"], f["columns"], f["rows"][0]) == (
            "table", ["Station", "Depth (m)", "Temp"], ["A01", "0", "5.0"])
        assert f["above"][0] == "Cruise C145 bottle data,,"

        dat = tmp_path / "c01.dat"
        dat.write_text("".join(f"   {27.7 - z / 100:.4f}    {z:.4f}\n" for z in range(1, 40)))
        f = preview_input(dat)["formatted"]
        assert not f["named"] and f["columns"] == ["column 1", "column 2"]

        xml = tmp_path / "iso.xml"
        xml.write_text('<?xml version="1.0"?><a><b>x</b></a>')
        assert preview_input(xml)["formatted"]["text"].splitlines()[1:] == ["<a>", "  <b>x</b>",
                                                                            "</a>"]
        readme = tmp_path / "readme.txt"
        readme.write_text("This accession holds CTD casts from 2015.\nContact the PI.\n" * 3)
        assert preview_input(readme)["formatted"] == {"layout": "prose"}
