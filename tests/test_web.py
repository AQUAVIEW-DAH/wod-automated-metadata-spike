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
    monkeypatch.setattr(web, "_jobs", {})
    monkeypatch.setattr(web, "_queue", [])
    return TestClient(web.app), acc


def _wait(c, acc=ACC, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/status/{acc}").json()
        if not (s["job"] or {}).get("running") and not s["queued"]:
            return s
        time.sleep(0.05)
    raise AssertionError("the command did not finish")


class TestRunReviewConfirm:
    def test_the_two_steps_and_the_review_between_them(self, client):
        c, acc = client
        assert c.post("/api/run", json={"source": str(acc)}).json()["accessions"] == [ACC]
        s = _wait(c)
        assert (s["last_command"], s["last_exit_code"]) == ("run", 0)
        assert [st["status"] for st in s["stages"][:4]] == ["done"] * 4
        assert "review them, then confirm" in s["outcome"]
        assert c.get(f"/api/batch?ids={ACC}").json()[0]["step"] == "review"

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
            assert c.get(f"/api/input-stats/{ACC}/{path}").status_code == 404
        read = c.get(f"/api/input-stats/{ACC}/data/ctd/cast_1.csv").json()
        assert (read["casts"], read["groups"][0]["size"]) == (1, 20)
        depth, temperature, _ = read["groups"][0]["variables"]
        assert (depth["name"], depth["min"], depth["max"]) == ("Depth (m)", 0, 19)
        assert (temperature["mean"], temperature["missing"]) == (pytest.approx(24.05), 0)

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
        assert c.get(f"/api/batch?ids={ACC}").json()[0]["step"] == "done"
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
        groups = c.get(f"/api/stats/{ACC}/{f['name']}").json()["groups"]
        assert [g["dimension"] for g in groups][:2] == ["z_obs", "casts"]
        z = next(v for v in groups[0]["variables"] if v["name"] == "z")
        assert (z["n"], z["max"], z["units"]) == (20, 19, "m")
        assert "## WOD files written" in c.get(f"/api/report/{ACC}").text
        assert "cli confirm" in c.get(f"/api/log/{ACC}").text
        assert c.get("/api/accessions").json()[0]["accession"] == ACC


class TestOneCommandAtATime:
    def test_later_commands_wait_their_turn(self, client, monkeypatch, tmp_path):
        c, acc = client
        other = acc.parent / "0999997"
        other.mkdir()
        signal = tmp_path / "release"
        # A stand-in for the command: says one line, then waits to be released.
        slow = ("import os, sys, time\nprint('[1/5] resolve  ...', flush=True)\n"
                "while not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n")
        monkeypatch.setattr(web, "_command_line",
                            lambda *a: [sys.executable, "-c", slow, str(signal)])
        r = c.post("/api/run", json={"source": f"{acc}, {acc} ,{other}"}).json()
        assert (r["accessions"], r["queued"]) == ([ACC, "0999997"], [ACC, "0999997"])
        again = c.post("/api/run", json={"source": str(acc)})
        assert again.status_code == 409 and "already running" in again.json()["detail"]
        assert c.put(f"/api/decisions/{ACC}", json={"text": ""}).status_code == 409
        for _ in range(200):
            s = c.get(f"/api/status/{ACC}").json()
            if s["job"]["lines"]:
                break
            time.sleep(0.02)
        assert s["job"]["running"] and s["job"]["lines"] == ["[1/5] resolve  ..."]
        assert c.get("/api/status/0999997").json()["queued"] == {"position": 1,
                                                                  "command": "run"}
        steps = {b["accession"]: (b["step"], b["command"], b["position"])
                 for b in c.get(f"/api/batch?ids={ACC},0999997").json()}
        assert steps == {ACC: ("running", "run", None), "0999997": ("queued", "run", 1)}

        assert c.delete("/api/queue/0999997").json()["removed"] == "run"
        assert c.delete("/api/queue/0999997").status_code == 404
        assert c.delete(f"/api/queue/{ACC}").status_code == 404     # running, not waiting
        assert c.post("/api/run", json={"source": str(other)}).json()["queued"] == ["0999997"]
        signal.write_text("go")
        assert _wait(c)["job"]["exit_code"] == 0
        assert _wait(c, "0999997")["job"]["exit_code"] == 0


class TestWhatItRefuses:
    def test_bad_input_is_said_not_run(self, client):
        c, acc = client
        assert c.post("/api/run", json={"source": "  "}).status_code == 400
        no_acc = c.post("/api/run", json={"source": str(acc.parent)})
        assert no_acc.status_code == 400 and "accession id" in no_acc.json()["detail"]
        some = c.post("/api/run", json={"source": f"{acc},{acc.parent}"})
        assert some.status_code == 400 and repr(str(acc.parent)) in some.json()["detail"]
        assert c.get(f"/api/status/{ACC}").json()["job"] is None          # none of them run
        named = c.post("/api/run", json={"source": f"{acc},{acc}", "accession_id": "1"})
        assert named.status_code == 400
        assert c.post(f"/api/confirm/{ACC}").status_code == 404          # not run yet
        assert c.get("/api/status/not!an:id").status_code == 400
        assert c.get(f"/api/outputs/{ACC}/notes.nc").status_code == 404
        assert c.get(f"/api/outputs/{ACC}/wod_ctd_2015_{ACC}.nc").status_code == 404
        assert c.get(f"/api/preview/{ACC}/notes.nc").status_code == 404
        assert c.get(f"/api/preview/{ACC}/wod_ctd_2015_{ACC}.nc/casts/0").status_code == 404
        assert c.get(f"/api/stats/{ACC}/wod_ctd_2015_{ACC}.nc").status_code == 404


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


class TestStats:
    """Summaries of numbers, flags, text and times."""

    def test_numbers(self):
        import pandas as pd

        from wod_etl.web.stats import BINS, summarize

        s = summarize(pd.Series([1.0, 2.0, 3.0, 4.0, None]))
        assert (s["kind"], s["n"], s["missing"]) == ("number", 4, 1)
        assert (s["min"], s["p25"], s["median"], s["p75"], s["max"]) == (1, 1.75, 2.5, 3.25, 4)
        assert s["mean"] == 2.5 and s["std"] == pytest.approx(1.2910, abs=1e-4)
        assert len(s["histogram"]) == BINS and sum(s["histogram"]) == 4
        assert summarize(pd.Series([5.0, 5.0]))["histogram"] == [2]
        assert summarize(pd.Series([None, None], dtype=float))["kind"] == "empty"

    def test_text_in_a_numeric_column_is_counted_apart(self):
        import pandas as pd

        from wod_etl.web.stats import summarize

        s = summarize(pd.Series(["0.5", "ND", "1.5", "ND", "2.5", None], dtype=object))
        assert (s["kind"], s["n"], s["missing"], s["median"]) == ("number", 3, 1, 1.5)
        assert s["text"] == {"count": 2, "values": [{"value": "ND", "count": 2,
                                                     "meaning": None}]}

    def test_flags_text_and_times(self):
        import pandas as pd

        from wod_etl.web.stats import summarize

        flags = summarize(pd.Series([4.0, 0.0, 0.0, None]), flag=True,
                          meanings={0.0: "accepted", 4.0: "bad"})
        assert flags["values"] == [{"value": 0, "count": 2, "meaning": "accepted"},
                                   {"value": 4, "count": 1, "meaning": "bad"}]
        text = summarize(pd.Series(["A01", "A01", "B02", ""], dtype=object))
        assert (text["kind"], text["distinct"], text["missing"]) == ("text", 2, 1)
        times = summarize(pd.Series(["2023/6/16 12:14", "2023/6/25 16:55", None], dtype=object))
        assert (times["kind"], times["first"], times["last"], times["missing"]) == (
            "time", "2023-06-16 12:14", "2023-06-25 16:55", 1)

    def test_a_written_file_by_dimension(self, tmp_path):
        from helpers import make_cast
        from wod_etl.web.stats import output_stats
        from wod_etl.writers import write_ragged_profiles

        p = tmp_path / "out.nc"
        write_ragged_profiles([make_cast(dic=True), make_cast(lat=62.4, rows=2)], str(p))
        r = output_stats(str(p))
        levels, casts = r["groups"][:2]
        assert (levels["dimension"], levels["size"], casts["dimension"]) == ("z_obs", 5, "casts")
        by_name = {v["name"]: v for g in r["groups"] for v in g["variables"]}
        assert "z_row_size" not in by_name                         # a count, not data
        dic = by_name["dissolved_inorganic_carbon"]
        assert (dic["n"], dic["missing"], dic["min"], dic["max"]) == (3, 2, 2100, 2110)
        assert by_name["temperature_qc"]["kind"] == "flag"
        assert (by_name["time"]["first"], by_name["lat"]["max"]) == ("1985-06-12", 62.4)

    def test_a_netcdf_input_is_described_as_stored(self, tmp_path):
        import netCDF4
        from helpers import write_mooring_nc
        from wod_etl.web.stats import input_stats

        p = tmp_path / "t.nc"
        write_mooring_nc(p, days=1)
        with netCDF4.Dataset(p, "a") as ds:
            ds["T_20"][3, 0, 0, 0] = 1e35                       # EPIC's undeclared fill
        r = input_stats(str(p))
        record, clock, single = r["groups"]
        assert r["stored"] and record["dimension"] == "time, depth, lat, lon"
        [t] = record["variables"]
        assert (t["n"], t["missing"], t["min"], t["max"]) == (24, 1, 5, 5)
        [time] = clock["variables"]
        assert (time["kind"], time["first"], time["last"]) == (
            "time", "2017-09-24 00:00", "2017-09-25 00:00")
        assert [v["name"] for v in single["variables"]] == ["depth", "lat", "lon"]
        assert single["size"] is None

    def test_an_input_table_as_the_pipeline_s_reader_splits_it(self, tmp_path):
        from wod_etl.web.stats import input_stats

        csv = tmp_path / "bottle.csv"
        csv.write_text("Station,Depth (m),Temp,Temp_QC\n"
                       + "".join(f"A01,{z},{5 - z / 100},2\n" for z in range(0, 50, 10)))
        r = input_stats(str(csv))
        station, depth, temp, flag = r["groups"][0]["variables"]
        assert (station["kind"], depth["max"], temp["min"]) == ("text", 40, 4.6)
        assert flag["kind"] == "flag" and flag["values"][0]["count"] == 5
        readme = tmp_path / "readme.txt"
        readme.write_text("This accession holds CTD casts from 2015. Contact the PI.\n" * 3)
        assert input_stats(str(readme))["error"]
