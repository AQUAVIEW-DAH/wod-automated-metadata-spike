"""Plankton end to end: triage, review, and confirm writing one Taxa-Record file per year,
from the terminal and the browser."""
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from helpers import cache_code_list
from wod_etl.discovery import classify_accession
from wod_etl.orchestration import decisions as dec
from wod_etl.orchestration.pipeline import (EXIT_OK, Workspace, confirm_pipeline,
                                            run_pipeline)
from wod_etl.orchestration.process_taxa import assemble_taxa
from wod_etl.orchestration.recipes import check_taxa_recipe, run_taxa_recipe

nc = pytest.importorskip("netCDF4")

FIXTURE = Path(__file__).parent / "fixtures" / "accessions" / "mixed-formats" / "4-plankton"
ACC = "0999997"
STATIONS = ("Station,Latitude,Longitude,Date,Time\n"
            "014,34.250,-120.500,2024-03-04,10:15\n"
            "015,34.410,-120.770,2024-03-04,15:40\n")
TAXA = ("ITIS TSN,PGC code,Rank,Taxonomic Name -[ Author or comments ]-\n"
        "85309,4212040,species,Calanus pacificus\n"
        "95500,4290000,genus,Euphausia\n")
BONGO = "4-plankton/bongo_hauls.csv"


def accession(root: Path, stations: bool = True) -> Path:
    """The plankton folder of the mixed-formats fixture, with (or without) a station list."""
    d = root / "4-plankton"
    d.mkdir(parents=True)
    shutil.copy(FIXTURE / "bongo_hauls.csv", d)
    if stations:
        (d / "stations.csv").write_text(STATIONS)
    return root


def files_of(root):
    return {f["path"]: f for f in classify_accession(str(root))["files"]}


@pytest.fixture
def taxa_list(offline_code_lists):
    cache_code_list(offline_code_lists, "t_1_taxa_list", TAXA)
    return offline_code_lists


@pytest.fixture
def env(tmp_path, monkeypatch, taxa_list):
    from wod_etl import config

    for name in ("RAW_DIR", "STAGING_DIR", "PROCESSED_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
    return accession(tmp_path / "src" / ACC)


class TestTriageWorksItOut:
    def test_a_located_plankton_table_is_staged_as_osd_taxa_records(self, tmp_path):
        f = files_of(accession(tmp_path))[BONGO]
        assert (f["action"], f["include"], f["dataset"]) == ("stage", True, "OSD")
        assert f["taxa"]["station_list"] == "4-plankton/stations.csv"
        assert "located by stations.csv" in f["why"]

    def test_the_station_list_beside_it_is_a_station_list_not_plankton(self, tmp_path):
        assert files_of(accession(tmp_path))["4-plankton/stations.csv"]["action"] == "sidecar"

    def test_a_table_with_its_own_positions_is_plankton_not_a_station_list(self, tmp_path):
        d = tmp_path / "4-plankton"
        d.mkdir()
        (d / "hauls.csv").write_text(
            "Station,Latitude,Longitude,Date,Upper depth (m),Lower depth (m),Taxon,"
            "Wet weight (g)\n1,34.2,-120.5,2024-03-04,0,100,Calanus pacificus,1.8\n")
        f = files_of(tmp_path)["4-plankton/hauls.csv"]
        assert (f["action"], f["include"]) == ("stage", True)
        assert "its own latitude, longitude and date" in f["why"]

    def test_one_that_cannot_be_located_is_proposed_out_saying_why(self, tmp_path):
        f = files_of(accession(tmp_path, stations=False))[BONGO]
        assert (f["action"], f["include"]) == ("stage", False)
        assert "cannot be located" in f["why"]
        assert dec.section_of(f) == "later"


class TestTheReviewDecidesOnlyWhetherItGoesIn:
    def plan(self, root, include=True):
        triage = classify_accession(str(root))
        decisions = {"files": {f["path"]: {"include": include if f["path"] == BONGO
                                           else f["include"]}
                               for f in triage["files"]}}
        return dec.build_plan(decisions, triage, []), decisions, triage

    def test_the_recipe_is_worked_out_and_its_files_are_consumed(self, tmp_path):
        plan, _, _ = self.plan(accession(tmp_path))
        (recipe,) = plan["taxa"].values()
        assert recipe["files"] == [BONGO] and recipe["station_list"].endswith("stations.csv")
        assert BONGO in plan["consumed"] and BONGO not in plan["direct"]

    def test_an_unlocatable_file_included_anyway_is_not_written_with_why(self, tmp_path):
        plan, _, _ = self.plan(accession(tmp_path, stations=False))
        assert plan["taxa"] == {}
        assert [w["key"] for w in plan["not_written"]] == [BONGO]
        assert "cannot be located" in plan["not_written"][0]["why"]

    def test_what_nobody_stated_is_a_note_not_a_question(self, tmp_path):
        root = accession(tmp_path)
        plan, decisions, triage = self.plan(root)
        checks = {n: check_taxa_recipe(r, root) for n, r in plan["taxa"].items()}
        items, _ = dec.validate(decisions, triage, {}, [], checks)
        taxa_items = [i for i in items if i["id"].startswith("taxa.")]
        assert taxa_items and not any(i["blocking"] for i in taxa_items)
        assert any("Water volume filtered" in i["what"] for i in taxa_items)


class TestStagingAndWriting:
    def recipe(self, root):
        triage = classify_accession(str(root))
        decisions = {"files": {f["path"]: {"include": f["include"]} for f in triage["files"]}}
        (recipe,) = dec.build_plan(decisions, triage, [])["taxa"].values()
        return recipe

    def test_one_record_per_taxon_depth_range_and_measurement(self, tmp_path, taxa_list):
        root = accession(tmp_path / "acc")
        res = run_taxa_recipe("bongo", self.recipe(root), root, tmp_path / "staged", ACC, {})
        assert len(res["written"]) == 2 and res["skipped"] == []
        out = assemble_taxa(tmp_path / "staged", tmp_path / "out", ACC)
        (f,) = out["files"]
        assert (f["name"], f["hauls"], f["records"]) == (f"wod_osd_2024_{ACC}_taxa.nc", 2, 8)
        assert (f["resolved"], f["higher_rank"], f["by_name"]) == (6, 2, 0)
        with nc.Dataset(tmp_path / "out" / f["name"]) as ds:
            volume = np.isfinite(np.ma.filled(ds["t_12_taxon_volume"][:], np.nan))
            weight = np.isfinite(np.ma.filled(ds["t_13_taxon_wet_weight"][:], np.nan))
            # Never both in one record: one measurement each.
            assert all(volume ^ weight) and volume.sum() == weight.sum() == 4
            assert set(ds["t_20_originator_s_unit"][:]) == {"sample"}
            assert list(ds["taxa_row_size"][:]) == [6, 2]

    def test_a_haul_its_station_list_does_not_name_is_left_out_with_why(self, tmp_path):
        root = accession(tmp_path / "acc")
        (root / "4-plankton" / "stations.csv").write_text(STATIONS.splitlines()[0] + "\n"
                                                         + STATIONS.splitlines()[1] + "\n")
        recipe = self.recipe(root)
        assert check_taxa_recipe(recipe, root)["casts_without_station"] == ["015"]
        res = run_taxa_recipe("bongo", recipe, root, tmp_path / "staged", ACC, {})
        assert len(res["written"]) == 1 and "no entry in" in res["skipped"][0]["reason"]

    def test_one_station_spelled_two_ways_is_one_haul(self, tmp_path):
        root = accession(tmp_path / "acc")
        (root / "4-plankton" / "bongo_hauls.csv").write_text(
            "Station,Upper depth (m),Lower depth (m),Taxon,Wet weight (g)\n"
            "014,0,100,Calanus pacificus,1.8\n14,100,200,Calanus pacificus,0.9\n")
        res = run_taxa_recipe("bongo", self.recipe(root), root, tmp_path / "staged", ACC, {})
        (path,) = res["written"]
        assert open(path).read().count("Calanus pacificus") == 2

    def test_a_negative_value_is_a_missing_value_code_not_the_smaller_unit(self, tmp_path):
        root = accession(tmp_path / "acc")
        (root / "4-plankton" / "bongo_hauls.csv").write_text(
            "Station,Upper depth (m),Lower depth (m),Taxon,Wet weight (g)\n"
            "014,0,100,Calanus pacificus,-999\n014,100,200,Calanus pacificus,0.9\n")
        recipe = self.recipe(root)
        assert check_taxa_recipe(recipe, root)["negative_values"] == {"Wet weight (g)": 1}
        res = run_taxa_recipe("bongo", recipe, root, tmp_path / "staged", ACC, {})
        text = open(res["written"][0]).read()
        assert "999" not in text.split("Taxon,", 1)[1] and "not written" in text

    def test_a_year_no_longer_written_is_removed(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / f"wod_osd_1999_{ACC}_taxa.nc").write_text("stale")
        (out / f"wod_osd_1999_{ACC}.nc").write_text("a profile file: not ours to remove")
        res = assemble_taxa(None, out, ACC)
        assert res["removed"] == [f"wod_osd_1999_{ACC}_taxa.nc"]
        assert (out / f"wod_osd_1999_{ACC}.nc").exists()


class TestConfirmWritesIt:
    def test_a_plankton_only_accession_is_written_and_verified(self, env):
        assert run_pipeline(str(env), accession_id=ACC)["exit_code"] == EXIT_OK
        ws = Workspace(ACC)
        assert '[files."4-plankton/bongo_hauls.csv"]\ninclude = true\ndataset = "OSD"' in \
            ws.decisions_path.read_text()
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK, r["problems"]
        name = f"wod_osd_2024_{ACC}_taxa.nc"
        assert (ws.processed_dir / name).is_file()
        verify = r["stages"]["verify"]["outputs"]
        assert verify["output_verified"] and verify["taxa_verified"]
        assert "## Taxa-Record files written" in ws.report_md.read_text()

    def test_confirming_again_with_nothing_changed_rewrites_nothing(self, env):
        run_pipeline(str(env), accession_id=ACC)
        confirm_pipeline(ACC)
        path = Workspace(ACC).processed_dir / f"wod_osd_2024_{ACC}_taxa.nc"
        before = path.stat().st_mtime_ns
        r = confirm_pipeline(ACC)
        assert r["exit_code"] == EXIT_OK and path.stat().st_mtime_ns == before

    def test_output_deleted_by_hand_is_rebuilt(self, env):
        run_pipeline(str(env), accession_id=ACC)
        confirm_pipeline(ACC)
        path = Workspace(ACC).processed_dir / f"wod_osd_2024_{ACC}_taxa.nc"
        path.unlink()
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK and path.is_file()

    def test_leaving_it_out_removes_its_file(self, env):
        run_pipeline(str(env), accession_id=ACC)
        confirm_pipeline(ACC)
        ws = Workspace(ACC)
        text = ws.decisions_path.read_text()
        ws.decisions_path.write_text(text.replace(
            '[files."4-plankton/bongo_hauls.csv"]\ninclude = true',
            '[files."4-plankton/bongo_hauls.csv"]\ninclude = false'))
        assert confirm_pipeline(ACC)["exit_code"] == EXIT_OK
        assert not (ws.processed_dir / f"wod_osd_2024_{ACC}_taxa.nc").exists()


class TestInTheBrowser:
    def test_run_confirm_and_download_the_taxa_file(self, env, monkeypatch):
        from fastapi.testclient import TestClient

        from wod_etl.web import server as web

        monkeypatch.setattr(web, "_job", {})
        c = TestClient(web.app)

        def wait():
            end = time.time() + 60
            while time.time() < end:
                if not (c.get(f"/api/status/{ACC}").json()["job"] or {}).get("running"):
                    return
                time.sleep(0.05)
            raise AssertionError("the command did not finish")

        assert c.post("/api/run", json={"source": str(env), "accession_id": ACC}).status_code \
            == 200
        wait()
        files = {f["path"]: f for s in c.get(f"/api/files/{ACC}").json()["sections"]
                 for f in s["files"]}
        assert files[BONGO]["taxa"] and files[BONGO]["include"]
        assert c.post(f"/api/confirm/{ACC}").status_code == 200
        wait()
        out = c.get(f"/api/outputs/{ACC}").json()
        (taxa,) = [f for f in out["files"] if f["kind"] == "taxa"]
        assert (taxa["hauls"], taxa["records"], taxa["resolved"]) == (2, 8, 6)
        assert out["verified"] is True
        assert c.get(f"/api/outputs/{ACC}/{taxa['name']}").status_code == 200
        assert c.get(f"/api/outputs/{ACC}/wod_osd_2024_0000001_taxa.nc").status_code == 404
