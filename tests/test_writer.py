"""Phase 4 and orchestration: NetCDF writing, manifest, end-to-end run.

Validation lives in test_verify.py, with the audit it belongs with."""
from pathlib import Path

import netCDF4 as nc
import numpy as np
import pytest

from wod_etl.orchestration import run_manifest as mf
from wod_etl.orchestration import process_accession
from wod_etl.verify import validate_intermediate_netcdf
from wod_etl.writers import cf_safe_name, write_ragged_profiles
from helpers import make_cast


class TestCFNames:
    @pytest.mark.parametrize("raw,expected", [
        ("Dissolved inorganic carbon", "dissolved_inorganic_carbon"),
        ("Nitrate and Nitrate + Nitrite", "nitrate_and_nitrate_nitrite"),
        ("pH", "ph"),
        ("Temperature", "temperature"),
    ])
    def test_names_become_cf_identifiers(self, raw, expected):
        assert cf_safe_name(raw) == expected

    def test_leading_digit_is_prefixed(self):
        assert cf_safe_name("14C").startswith("v_")


class TestRaggedWriter:
    def test_single_shared_observation_dimension(self, tmp_path):
        """Per-variable obs dims broke the coordinate linkage."""
        p = tmp_path / "out.nc"
        write_ragged_profiles([make_cast(dic=True)], str(p))
        with nc.Dataset(p) as ds:
            assert set(ds.dimensions) == {"casts", "z_obs"}
            for v in ("z", "temperature", "dissolved_inorganic_carbon"):
                assert ds.variables[v].dimensions == ("z_obs",)

    def test_row_sizes_sum_to_the_observation_dimension(self, tmp_path):
        p = tmp_path / "multi.nc"
        res = write_ragged_profiles(
            [make_cast(rows=3), make_cast(lat=62.4, rows=2)], str(p))
        assert res["row_sizes"] == [3, 2]
        with nc.Dataset(p) as ds:
            assert int(ds.variables["z_row_size"][:].sum()) == len(ds.dimensions["z_obs"])

    def test_multiple_casts_keep_their_own_positions(self, tmp_path):
        p = tmp_path / "multi.nc"
        write_ragged_profiles([make_cast(lat=61.93), make_cast(lat=62.4)], str(p))
        with nc.Dataset(p) as ds:
            assert list(np.round(ds.variables["lat"][:], 2)) == [61.93, 62.4]

    def test_cast_ids_are_not_truncated(self, tmp_path):
        p = tmp_path / "ids.nc"
        write_ragged_profiles([make_cast()], str(p))
        with nc.Dataset(p) as ds:
            assert ds.variables["cast_id"][0].startswith("STOCS85A")

    def test_qc_arrays_accompany_every_measurement(self, tmp_path):
        p = tmp_path / "qc.nc"
        write_ragged_profiles([make_cast(dic=True)], str(p))
        with nc.Dataset(p) as ds:
            for v in ("temperature", "dissolved_inorganic_carbon"):
                assert f"{v}_qc" in ds.variables
                assert ds.variables[f"{v}_qc"].flag_meanings

    def test_empty_input_is_an_error_not_a_crash(self, tmp_path):
        res = write_ragged_profiles([], str(tmp_path / "x.nc"))
        assert res["status"] == "error"


class TestManifest:
    def test_records_outcomes_and_computes_failure_rate(self):
        m = mf.create_manifest("t_unit_1", "src", [{"key": "a"}, {"key": "b"}, {"key": "c"}])
        mf.mark(m, "a", mf.DONE, output_path="a.nc")
        mf.mark(m, "b", mf.FAILED, stage="load_tabular", error="boom")
        mf.mark(m, "c", mf.SKIPPED, reason="xml")
        s = mf.summarize(m)
        assert s["counts"] == {"done": 1, "failed": 1, "skipped": 1}
        assert s["failure_rate"] == pytest.approx(0.5)
        assert s["failures"][0]["stage"] == "load_tabular"

    def test_resume_skips_completed_items(self):
        items = [{"key": "a"}, {"key": "b"}]
        m = mf.create_manifest("t_unit_2", "src", items)
        mf.mark(m, "a", mf.DONE)
        again = mf.resume_or_create("t_unit_2", "src", items)
        assert [i["key"] for i in mf.pending_items(again)] == ["b"]

    def test_interrupted_running_item_becomes_pending_again(self):
        items = [{"key": "a"}]
        m = mf.create_manifest("t_unit_3", "src", items)
        mf.mark(m, "a", mf.RUNNING)
        again = mf.resume_or_create("t_unit_3", "src", items)
        assert [i["key"] for i in mf.pending_items(again)] == ["a"]

    def test_failed_items_are_retryable_only_on_request(self):
        m = mf.create_manifest("t_unit_4", "src", [{"key": "a"}])
        mf.mark(m, "a", mf.FAILED, stage="x", error="y")
        loaded = mf.load_manifest("t_unit_4")
        assert mf.pending_items(loaded) == []
        assert [i["key"] for i in mf.pending_items(loaded, retry_failed=True)] == ["a"]


class TestEndToEnd:
    @pytest.fixture
    def accession(self, tmp_path):
        acc = tmp_path / "0287423"
        acc.mkdir()
        (acc / "cast_01.csv").write_text(
            "# CRUISE: STOCS85A\n# DATE: 1985-06-12\nLAT: 61.930 LON: -172.270\n"
            "Depth (ft), Temp (deg F), Salinity (PSU)\n"
            "0.0, 48.4, 32.94\n32.8, 48.1, 32.91\n65.6, 47.5, 32.92\n")
        (acc / "cast_02.csv").write_text(
            "# CAST: 1\nLAT: 62.100 LON: -172.500\nDepth, Temp_C\n0.0, 8.70\n10.0, 8.40\n"
            "# CAST: 2\nLAT: 62.400 LON: -173.000\nDepth, Temp_C\n0.0, 8.10\n12.0, 7.80\n")
        (acc / "broken.csv").write_text("no tabular data here at all\n")
        (acc / "meta.xml").write_text("<xml/>")
        return acc

    def test_run_completes_and_isolates_the_bad_file(self, accession, tmp_path):
        res = process_accession(str(accession), dataset_type="XCTD",
                                output_dir=str(tmp_path / "out"), raw_dir=str(tmp_path / "raw"),
                                run_id="t_e2e_1", resume=False)
        assert res["counts"]["done"] == 2
        assert res["counts"]["failed"] == 1
        assert res["counts"]["skipped"] == 1          # the .xml
        assert res["failures"][0]["key"] == "broken.csv"

    def test_outputs_validate_and_conversions_were_applied(self, accession, tmp_path):
        out = tmp_path / "out"
        res = process_accession(str(accession), dataset_type="XCTD",
                                output_dir=str(out), raw_dir=str(tmp_path / "raw"), run_id="t_e2e_2", resume=False)
        for path in res["outputs"]:
            assert validate_intermediate_netcdf(path)["passed"]
        target = next(p for p in res["outputs"] if "cast_01" in p)
        with nc.Dataset(target) as ds:
            assert ds.variables["z"][1] == pytest.approx(9.99744, rel=1e-4)   # 32.8 ft
            assert ds.variables["temperature"][0] == pytest.approx(9.111, rel=1e-3)  # 48.4 F

    def test_multi_cast_file_produces_multiple_casts(self, accession, tmp_path):
        res = process_accession(str(accession), dataset_type="XCTD",
                                output_dir=str(tmp_path / "out"), raw_dir=str(tmp_path / "raw"),
                                run_id="t_e2e_3", resume=False)
        target = next(p for p in res["outputs"] if "cast_02" in p)
        with nc.Dataset(target) as ds:
            assert len(ds.dimensions["casts"]) == 2
            assert list(ds.variables["z_row_size"][:]) == [2, 2]

    def test_dry_run_writes_nothing(self, accession, tmp_path):
        out = tmp_path / "out"
        res = process_accession(str(accession), dataset_type="XCTD", output_dir=str(out), raw_dir=str(tmp_path / "raw"),
                                run_id="t_e2e_4", resume=False, dry_run=True)
        assert res["status"] == "dry_run"
        assert not list(out.glob("*.nc")) if out.exists() else True

    def test_resume_does_not_redo_completed_files(self, accession, tmp_path):
        out = tmp_path / "out"
        first = process_accession(str(accession), dataset_type="XCTD", output_dir=str(out),
                                  raw_dir=str(tmp_path / "raw"), run_id="t_e2e_5", resume=False)
        again = process_accession(str(accession), dataset_type="XCTD", output_dir=str(out),
                                  raw_dir=str(tmp_path / "raw"), run_id="t_e2e_5", resume=True, dry_run=True)
        assert set(again["already_done"]) >= {"cast_01.csv", "cast_02.csv", "meta.xml"}
        assert first["counts"]["done"] == 2

    def test_empty_directory_is_reported(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        res = process_accession(str(empty), raw_dir=str(tmp_path / "raw"), run_id="t_e2e_6", resume=False)
        assert res["status"] == "empty"


class TestSecondaryHeadersInMultiCastFiles:
    """Per-cast Table 4a headers must not be written as file-global attributes.

    The ragged array exists to hold many casts in one file. Taking the first cast's headers
    as globals relabels every other cast with cast 0's bottom depth and station number, and
    silently drops any header cast 0 does not carry -- corruption that still validates.
    """

    @staticmethod
    def _with_headers(headers, **kw):
        cast = make_cast(**kw)
        cast["contract"]["extracted_header_metadata"] = headers
        return cast

    def _write(self, casts, tmp_path):
        out = tmp_path / "multi.nc"
        res = write_ragged_profiles(casts, str(out))
        assert res["status"] == "success"
        return nc.Dataset(str(out))

    def test_a_header_that_varies_becomes_a_per_cast_variable(self, tmp_path):
        casts = [
            self._with_headers({"3": {"name": "Platform Code", "value": "Kaimei"},
                                "10": {"name": "Bottom Depth (meters)", "value": 3212.4}}),
            self._with_headers({"3": {"name": "Platform Code", "value": "Kaimei"},
                                "10": {"name": "Bottom Depth (meters)", "value": 1409.3}},
                               lat=18.1, lon=147.1),
        ]
        ds = self._write(casts, tmp_path)
        depth = "secondary_header_10_bottom_depth_meters"
        assert depth not in ds.ncattrs(), "a varying header must not be a global attribute"
        assert list(ds[depth][:]) == pytest.approx([3212.4, 1409.3])
        assert ds[depth].wod_secondary_header_id == "10"
        # what every cast agrees on stays global
        assert ds.getncattr("secondary_header_3_platform_code") == "Kaimei"
        ds.close()

    def test_a_header_only_one_cast_carries_is_not_dropped(self, tmp_path):
        casts = [
            self._with_headers({"3": {"name": "Platform Code", "value": "Kaimei"}}),
            self._with_headers({"3": {"name": "Platform Code", "value": "Kaimei"},
                                "34": {"name": "Bottom Hit", "value": "yes"}},
                               lat=18.1, lon=147.1),
        ]
        ds = self._write(casts, tmp_path)
        hit = ds["secondary_header_34_bottom_hit"][:]
        assert list(hit) == ["", "yes"]
        ds.close()

    def test_a_single_cast_file_keeps_every_header_global(self, tmp_path):
        ds = self._write([self._with_headers(
            {"3": {"name": "Platform Code", "value": "Kaimei"},
             "10": {"name": "Bottom Depth (meters)", "value": 3212.4}})], tmp_path)
        assert ds.getncattr("secondary_header_10_bottom_depth_meters") == "3212.4"
        assert "secondary_header_10_bottom_depth_meters" not in ds.variables
        ds.close()


class TestFilesAreIdentifiedNotJustNamed:
    """The manifest key, the raw copy and the output name were all the bare basename, so
    with recursive=True (the default) every subdirectory's data.csv collapsed onto one
    entry: one manifest item, one cached download, one output file overwritten in turn."""

    CAST = ("# LAT: 61.9\n# LON: -172.2\n# DATE: 2023-06-20\n"
            "Depth (m),Temp (deg C)\n0,20\n10,18\n")

    @pytest.fixture
    def nested(self, tmp_path):
        for station in ("stationA", "stationB"):
            d = tmp_path / "acc" / station
            d.mkdir(parents=True)
            (d / "data.csv").write_text(self.CAST)
        return tmp_path

    def _run(self, nested, **kw):
        return process_accession(
            str(nested / "acc"), dataset_type="CTD", output_dir=str(nested / "out"),
            raw_dir=str(nested / "raw"), run_id="t_collide", resume=False, **kw)

    def test_both_files_are_processed(self, nested):
        res = self._run(nested)
        assert res["counts"]["done"] == 2
        assert len(res["outputs"]) == 2

    def test_manifest_keys_are_distinct(self, nested):
        import json
        res = self._run(nested)
        keys = sorted(json.loads(Path(res["manifest_path"]).read_text())["items"])
        assert keys == ["stationA/data.csv", "stationB/data.csv"]

    def test_outputs_do_not_overwrite_each_other(self, nested):
        self._run(nested)
        assert sorted(p.name for p in (nested / "out").glob("*.nc")) == [
            "stationA__data.nc", "stationB__data.nc"]

    def test_raw_copies_keep_their_provenance_path(self, nested):
        self._run(nested)
        got = sorted(str(p.relative_to(nested / "raw" / "t_collide"))
                     for p in (nested / "raw").rglob("*.csv"))
        assert got == ["stationA/data.csv", "stationB/data.csv"]

    def test_a_flat_accession_keeps_plain_names(self, tmp_path):
        d = tmp_path / "acc"; d.mkdir()
        (d / "cast_01.csv").write_text(self.CAST)
        res = process_accession(str(d), dataset_type="CTD", output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_flat", resume=False)
        assert res["counts"]["done"] == 1
        assert [p.name for p in (tmp_path / "out").glob("*.nc")] == ["cast_01.nc"]


class TestGlobalAttributes:
    """CF: `institution` is whose data this is, not what wrote the file."""

    def _globals(self, tmp_path, casts, name):
        out = tmp_path / name
        write_ragged_profiles(casts, str(out))
        with nc.Dataset(out) as ds:
            return {a: ds.getncattr(a) for a in ds.ncattrs()}

    def test_the_originators_institution_is_used_when_stated(self, tmp_path):
        cast = make_cast()
        cast["contract"]["extracted_header_metadata"]["4"] = {
            "name": "Institution", "value": "Japan Fisheries Research and Education Agency"}
        g = self._globals(tmp_path, [cast], "inst.nc")
        assert g["institution"] == "Japan Fisheries Research and Education Agency"
        assert g["source"] == "generated by wod-agent-etl"

    def test_no_institution_is_claimed_when_the_originator_gave_none(self, tmp_path):
        g = self._globals(tmp_path, [make_cast()], "noinst.nc")
        assert "institution" not in g
        assert g["source"] == "generated by wod-agent-etl"

    def test_casts_that_disagree_do_not_get_one_institution(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        a["contract"]["extracted_header_metadata"]["4"] = {"name": "Institution", "value": "A"}
        b["contract"]["extracted_header_metadata"]["4"] = {"name": "Institution", "value": "B"}
        assert "institution" not in self._globals(tmp_path, [a, b], "two.nc")


class TestProcessOneFileAcceptsAStringPath:
    def test_out_dir_may_be_a_string(self, tmp_path):
        """The documented single-file entry point; callers pass a string far more often
        than a Path, and it used to raise TypeError on the join."""
        src = tmp_path / "cast.csv"
        src.write_text("# LAT: 61.9\n# LON: -172.2\n# DATE: 1985-06-12\n"
                       "Depth (m),Temperature (deg C)\n0,9.1\n10,8.9\n20,8.4\n")
        from wod_etl.orchestration import process_one_file
        res = process_one_file(str(src), "CTD", str(tmp_path / "out"), "strtest",
                               raw_root=str(tmp_path / "raw"))
        assert res["ok"], res
        assert Path(res["output_path"]).is_file()
