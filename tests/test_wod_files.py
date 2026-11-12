"""WOD's file layout: one file per dataset and year, `wod_ctd_2008_<accession>.nc`.

A file holds casts from many sources, so anything that differs between sources is per cast."""
import copy
from pathlib import Path

import netCDF4 as nc
import pytest

from wod_etl.orchestration import process_accession, process_one_file
from wod_etl.orchestration.process_accession import CACHE_SUFFIX, assemble_profiles
from wod_etl.verify import validate_intermediate_netcdf, verify_output
from wod_etl.writers import (group_by_file, is_wod_file_name, wod_file_name,
                             write_ragged_profiles)
from wod_etl.writers.generate_netcdf_ragged import cast_year
from helpers import make_cast

ACC = "0287423"


def _cast_block(n, date, lat, levels, step=1.0):
    rows = "".join(f"{z * step:g},{25 - z / 10:.2f}\n" for z in range(levels))
    return (f"# CAST: {n}\n# DATE: {date}\nLAT: {lat} LON: 142.3\n"
            f"Depth (m),Temperature (deg C)\n{rows}")


def _run(src, tmp_path, **kw):
    kw.setdefault("dataset_type", "CTD")
    return process_accession(str(src), output_dir=str(tmp_path / "out"),
                             raw_dir=str(tmp_path / "raw"), run_id=kw.pop("run_id", "t_wod"),
                             resume=kw.pop("resume", False), **kw)


def _casts(path):
    with nc.Dataset(path) as ds:
        n = len(ds.dimensions["casts"])
        sources = ([str(s) for s in ds["source_file"][:]] if "source_file" in ds.variables
                   else [ds.source_file] * n)
        return {"dataset": ds.wod_dataset_type,
                "per_cast": [str(x) for x in ds["dataset"][:]],
                "sources": [Path(s).name for s in sources],
                "dates": [int(d) for d in ds["date"][:]]}


class TestNames:
    def test_the_name_is_wods_with_the_accession(self):
        assert wod_file_name("CTD", 2008, "0062621") == "wod_ctd_2008_0062621.nc"
        assert wod_file_name("MBT", 1905) == "wod_mbt_1905.nc"
        assert wod_file_name("OSD", None, "0062621") == "wod_osd_undated_0062621.nc"

    def test_only_this_accessions_names_are_its_own(self):
        assert is_wod_file_name("wod_ctd_2008_0062621.nc", "0062621")
        assert is_wod_file_name("wod_xbt_undated_0062621.nc", "0062621")
        assert not is_wod_file_name("wod_ctd_2008_0062622.nc", "0062621")
        assert not is_wod_file_name("wod_ctd_2008.nc", "0062621")
        assert not is_wod_file_name("cast_01.nc", "0062621")
        assert not is_wod_file_name("wod_ctd_2008_0062621.nc")

    def test_a_cast_is_filed_under_its_utc_year(self):
        cast = make_cast()
        cast["contract"]["parsed_coordinates"].update(
            year=2008, month=12, day=31, hour=23, minute=0, time_zone="UTC-5")
        assert cast_year(cast) == 2009                 # 04:00 UTC on 1 January
        undated = make_cast()
        undated["contract"]["parsed_coordinates"].pop("year")
        assert cast_year(undated) is None


class TestGrouping:
    def _cast(self, dataset, year, month=6, source="s"):
        c = make_cast()
        c["contract"]["wod_dataset_code"] = dataset
        c["contract"]["parsed_coordinates"].update(year=year, month=month)
        c["contract"]["source"] = source
        return c

    def test_one_file_per_dataset_and_year_sources_in_time_order(self):
        sources = [
            {"key": "b.csv", "casts": [self._cast("CTD", 2017, 7), self._cast("CTD", 2017, 8)]},
            {"key": "a.csv", "casts": [self._cast("CTD", 2017, 9), self._cast("OSD", 2017, 9)]},
            {"key": "c.csv", "casts": [self._cast("CTD", 2017, 5), self._cast("CTD", 2018, 1)]},
        ]
        files = group_by_file(sources, ACC)
        assert [f["name"] for f in files] == [f"wod_ctd_2017_{ACC}.nc",
                                              f"wod_ctd_2018_{ACC}.nc",
                                              f"wod_osd_2017_{ACC}.nc"]
        ctd17 = files[0]
        # a source's casts stay together, in their own order; sources by their first cast
        assert [s["key"] for s in ctd17["sources"]] == ["c.csv", "b.csv", "a.csv"]
        assert [s["casts"] for s in ctd17["sources"]] == [1, 2, 1]
        assert [c["contract"]["parsed_coordinates"]["month"] for c in ctd17["casts"]] == \
            [5, 7, 8, 9]


class TestProcessAccessionWritesWodFiles:
    def test_two_sources_of_one_dataset_and_year_are_one_file(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        (src / "b.csv").write_text(_cast_block(1, "2015-11-10", 21.5, 20))
        (src / "a.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        res = _run(src, tmp_path)
        assert [Path(p).name for p in res["outputs"]] == [f"wod_ctd_2015_{ACC}.nc"]
        assert sorted(Path(p).name for p in res["per_file_outputs"]) == ["a.csv.nc", "b.csv.nc"]
        got = _casts(res["outputs"][0])
        assert got["sources"] == ["a.csv", "b.csv"] and got["per_cast"] == ["CTD", "CTD"]
        assert res["output_verified"] is True
        with nc.Dataset(res["outputs"][0]) as ds:
            assert "source_file" not in ds.ncattrs()     # it differs, so it is per cast
            assert "2 source files" in ds.history

    def test_a_single_level_cast_goes_to_its_own_osd_file_not_labelled_ctd(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        (src / "mixed.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20)
                                       + _cast_block(2, "2015-11-10", 21.5, 1))
        res = _run(src, tmp_path)
        names = sorted(Path(p).name for p in res["outputs"])
        assert names == [f"wod_ctd_2015_{ACC}.nc", f"wod_osd_2015_{ACC}.nc"]
        for path in res["outputs"]:
            got = _casts(path)
            assert set(got["per_cast"]) == {got["dataset"]}
            assert validate_intermediate_netcdf(path)["passed"]
        assert res["dataset_types"] == {"CTD": 1, "OSD": 1}

    def test_a_source_spanning_new_year_is_split_by_year(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        (src / "cruise.csv").write_text(_cast_block(1, "2008-12-31", 70.0, 20)
                                        + _cast_block(2, "2009-01-01", 70.5, 20))
        res = _run(src, tmp_path)
        assert sorted(Path(p).name for p in res["outputs"]) == [
            f"wod_ctd_2008_{ACC}.nc", f"wod_ctd_2009_{ACC}.nc"]

    def test_a_resumed_run_redoes_a_file_whose_result_is_gone(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        for name, day in (("a.csv", "09"), ("b.csv", "10")):
            (src / name).write_text(_cast_block(1, f"2015-11-{day}", 21.0, 20))
        _run(src, tmp_path, run_id="t_resume")
        (tmp_path / "out" / "per_file" / "t_resume" / f"a.csv{CACHE_SUFFIX}").unlink()
        res = _run(src, tmp_path, run_id="t_resume", resume=True)
        assert _casts(res["outputs"][0])["sources"] == ["a.csv", "b.csv"]

    def test_a_file_that_fails_now_is_not_written_from_its_last_result(self, tmp_path):
        good = tmp_path / "cast.csv"
        good.write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        out = tmp_path / "per_file"
        assert process_one_file(str(good), "CTD", str(out), "t_stale",
                                raw_root=str(tmp_path / "raw"))["ok"]
        assert (out / f"cast.csv{CACHE_SUFFIX}").is_file()
        good.write_text("nothing tabular in here\n")
        assert not process_one_file(str(good), "CTD", str(out), "t_stale",
                                    raw_root=str(tmp_path / "raw"))["ok"]
        assert not (out / f"cast.csv{CACHE_SUFFIX}").exists()
        assert not (out / "cast.csv.nc").exists()

    def test_earlier_output_is_replaced_and_nothing_else_is_touched(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        cast = src / "cast_01.csv"
        cast.write_text(_cast_block(1, "1999-11-09", 21.0, 20))
        out = tmp_path / "out"
        _run(src, tmp_path, run_id="first")
        for name in ("wod_ctd_1999_9999999.nc", "notes.nc"):
            (out / name).write_bytes(b"not this run's")
        cast.write_text(_cast_block(1, "2015-11-09", 21.0, 20))   # the date was corrected
        res = _run(src, tmp_path, run_id="second")
        left = sorted(p.name for p in out.iterdir() if p.is_file())
        assert left == ["notes.nc", "wod_ctd_1999_9999999.nc", f"wod_ctd_2015_{ACC}.nc"]
        assert res["assembly"]["removed"] == [f"wod_ctd_1999_{ACC}.nc"]

    def test_a_file_holding_another_runs_sources_is_neither_replaced_nor_removed(self,
                                                                                  tmp_path):
        root = tmp_path / ACC
        for folder, day in (("ship", "09"), ("kayak", "10")):
            (root / folder).mkdir(parents=True)
            (root / folder / "cast.csv").write_text(_cast_block(1, f"2015-11-{day}", 21.0, 20))
        first = _run(root / "ship", tmp_path, run_id="ship", accession=ACC)
        assert _casts(first["outputs"][0])["sources"] == ["cast.csv"]
        second = _run(root / "kayak", tmp_path, run_id="kayak", accession=ACC)
        assert second["status"] == "incomplete" and second["outputs"] == []
        [problem] = second["assembly"]["problems"]
        assert problem["file"] == f"wod_ctd_2015_{ACC}.nc" and "left as it is" in problem["error"]
        with nc.Dataset(first["outputs"][0]) as ds:          # still the first run's casts
            assert ds.source_file == "ship/cast.csv"          # from the accession's folder

    def test_files_of_input_with_no_accession_are_named_for_its_folder(self, tmp_path):
        src = tmp_path / "my_cruise"
        src.mkdir()
        (src / "a.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        out = tmp_path / "out"
        out.mkdir()
        (out / "wod_ctd_2015.nc").write_bytes(b"WOD's own yearly file")
        res = _run(src, tmp_path)
        assert [Path(p).name for p in res["outputs"]] == ["wod_ctd_2015_my-cruise.nc"]
        assert (out / "wod_ctd_2015.nc").read_bytes() == b"WOD's own yearly file"

    def test_only_the_files_this_run_covers_are_assembled(self, tmp_path):
        """Two accessions into one output_dir (the default is data/processed for all)."""
        for acc, day in (("0287423", "09"), ("0303928", "12")):
            src = tmp_path / acc
            src.mkdir()
            (src / f"{acc}.csv").write_text(_cast_block(1, f"2015-11-{day}", 21.0, 20))
            res = _run(src, tmp_path, run_id=acc)
            assert _casts(res["outputs"][0])["sources"] == [f"{acc}.csv"]
        out = tmp_path / "out"
        assert sorted(p.name for p in out.glob("wod_*.nc")) == [
            "wod_ctd_2015_0287423.nc", "wod_ctd_2015_0303928.nc"]

    def test_assembly_says_when_a_result_cannot_be_read_back(self, tmp_path):
        bad = tmp_path / f"x{CACHE_SUFFIX}"
        bad.write_bytes(b"not a pickle")
        res = assemble_profiles([str(bad)], tmp_path / "out", ACC)
        assert res["files"] == [] and res["problems"][0]["source"] == "x"
        assert "cannot be read back" in res["problems"][0]["error"]

    def test_assembly_needs_a_scope_or_its_files_would_be_named_as_wods_own(self, tmp_path):
        with pytest.raises(ValueError):
            assemble_profiles([], tmp_path / "out", "")


class TestWhatDiffersBetweenSourcesIsPerCast:
    def _write(self, casts, tmp_path):
        out = tmp_path / "f.nc"
        assert write_ragged_profiles(casts, str(out))["status"] == "success"
        assert validate_intermediate_netcdf(str(out))["passed"]
        return nc.Dataset(str(out))

    def test_a_file_of_two_datasets_says_mixed_and_which_cast_is_which(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        b["contract"]["wod_dataset_code"] = "OSD"
        with self._write([a, b], tmp_path) as ds:
            assert ds.wod_dataset_type == "mixed"
            assert [str(x) for x in ds["dataset"][:]] == ["CTD", "OSD"]
        report = validate_intermediate_netcdf(str(tmp_path / "f.nc"))
        assert any("mixed" in w for w in report["warnings"])

    def test_a_column_named_differently_by_two_sources_is_named_per_cast(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        for c, unit in ((a, "deg C"), (b, "degC")):
            c["contract"]["column_mapping"][1]["originator_unit"] = unit
        b["df"] = b["df"].rename(columns={"Temp_C": "T", "Temp_C_qc_flag": "T_qc_flag"})
        b["contract"]["column_mapping"][1]["raw_column"] = "T"
        with self._write([a, b], tmp_path) as ds:
            v = ds["temperature"]
            assert "originator_column_name" not in v.ncattrs()
            assert [str(x) for x in ds["temperature_originator_column_name"][:]] == ["Temp_C", "T"]
            assert [str(x) for x in ds["temperature_originator_units"][:]] == ["deg C", "degC"]
            assert "temperature_originator_units" in v.per_cast_attributes
        with self._write([make_cast(), make_cast(lat=62.4)], tmp_path) as ds:
            assert ds["temperature"].originator_column_name == "Temp_C"   # shared: attribute
            assert "temperature_originator_column_name" not in ds.variables

    def test_two_sources_flag_schemes_are_kept_apart(self, tmp_path):
        def flagged(scheme, lat=61.9):
            c = make_cast(lat=lat)
            c["df"]["Tflag"] = [1, 1, 4]
            c["contract"]["column_mapping"][1]["originator_flag_column"] = "Tflag"
            c["column_attributes"] = {"Tflag": scheme}
            return c
        one = {"flag_values": [1, 4], "flag_meanings": "good bad"}
        other = {"flag_values": [0, 4], "flag_meanings": "unchecked bad"}
        with self._write([flagged(one), flagged(other, 62.4)], tmp_path) as ds:
            o = ds["temperature_qc_originator"]
            assert "flag_meanings" not in o.ncattrs()
            assert [str(x) for x in ds["temperature_qc_originator_flag_meanings"][:]] == [
                "good bad", "unchecked bad"]
        with self._write([flagged(one), flagged(copy.deepcopy(one), 62.4)], tmp_path) as ds:
            assert ds["temperature_qc_originator"].flag_meanings == "good bad"

    def test_unplaced_header_keys_shared_are_global_the_rest_per_cast(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        a["contract"]["unresolved_metadata"] = [{"key": "Area", "value": "Arctic"},
                                                {"key": "title", "value": "May"}]
        b["contract"]["unresolved_metadata"] = [{"key": "Area", "value": "Arctic"},
                                                {"key": "title", "value": "July"}]
        with self._write([a, b], tmp_path) as ds:
            assert "Area='Arctic'" in ds.metadata_not_placed
            assert "title" not in ds.metadata_not_placed
            assert [str(x) for x in ds["cast_metadata_not_placed"][:]] == [
                "title='May'", "title='July'"]

    def test_a_conversion_is_described_once_not_per_cast(self, tmp_path):
        log = {"transformations_applied": [{"column": "Depth", "from": "ft", "to": "m"}]}
        casts = [make_cast(), make_cast(lat=62.4)]
        for c in casts:
            c["transform_log"] = copy.deepcopy(log)
        with self._write(casts, tmp_path) as ds:
            assert ds.unit_conversions == "Depth: ft -> m"


class TestVerifyJudgesEachSourceOnItsOwn:
    def _file(self, tmp_path, casts, name="f.nc"):
        out = tmp_path / name
        assert write_ragged_profiles(casts, str(out))["status"] == "success"
        return out

    def _with_salinity(self, source, copy_of_temperature, lat=61.9, rows=12):
        c = make_cast(lat=lat, rows=rows)
        c["df"]["Sal"] = c["df"]["Temp_C"] if copy_of_temperature else 34.0 + c["df"].index / 100
        c["contract"]["column_mapping"].append(
            {"raw_column": "Sal", "wod_code": 2, "wod_name": "Salinity",
             "target_unit": "Dimensionless"})
        c["contract"]["source"] = source
        return c

    def test_a_copied_variable_in_one_source_is_found_among_others(self, tmp_path):
        path = self._file(tmp_path, [self._with_salinity("ship.nc", True),
                                     self._with_salinity("kayak.nc", False, lat=62.4)])
        report = verify_output(str(tmp_path))
        copies = [f for f in report["findings"] if f["finding"] == "identical_variables"]
        assert len(copies) == 1 and "ship.nc" in copies[0]["file"]
        assert path.name in copies[0]["file"]

    def test_a_flat_depth_axis_in_one_source_is_found_among_others(self, tmp_path):
        flat = make_cast(rows=5)
        flat["df"]["Depth"] = 7.0
        flat["contract"]["source"] = "station_depth.csv"
        good = make_cast(lat=62.4, rows=5)
        good["contract"]["source"] = "good.csv"
        self._file(tmp_path, [flat, good])
        report = verify_output(str(tmp_path))
        found = [f for f in report["findings"] if f["finding"] == "flat_depth_axis"]
        assert len(found) == 1 and "station_depth.csv" in found[0]["file"]

    def test_paths_checks_exactly_the_files_named(self, tmp_path):
        mine = self._file(tmp_path, [make_cast()], "mine.nc")
        self._file(tmp_path, [make_cast()], "other.nc")
        report = verify_output(str(tmp_path), paths=[str(mine)])
        assert report["netcdf_files"] == 1 and report["casts"] == 1



class TestWhatOneFileOfManySourcesMustStillSay:
    def test_one_cast_in_two_source_files_is_still_a_duplicate_id(self, tmp_path):
        """Suffixing ids to make them unique in a file must not hide a duplicate."""
        a, b = make_cast(), make_cast()
        for c, source in ((a, "export_1.csv"), (b, "export_2.csv")):
            c["contract"]["source"] = source
            c["contract"]["extracted_header_metadata"]["5"] = {"name": "Cast", "value": "5"}
        write_ragged_profiles([a, b], str(tmp_path / "f.nc"))
        with nc.Dataset(tmp_path / "f.nc") as ds:
            assert [str(x) for x in ds["cast_id"][:]] == ["STOCS85A_5_19850612",
                                                          "STOCS85A_5_19850612_02"]
        report = verify_output(str(tmp_path))
        assert any(f["finding"] == "duplicate_cast_id" for f in report["findings"])
        assert report["passed"] is False

    def test_two_casts_of_one_source_with_one_id_are_not_a_duplicate(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        for c in (a, b):
            c["contract"]["extracted_header_metadata"]["5"] = {"name": "Cast", "value": "5"}
        write_ragged_profiles([a, b], str(tmp_path / "f.nc"))
        report = verify_output(str(tmp_path))
        assert not any(f["finding"] == "duplicate_cast_id" for f in report["findings"])

    def test_conversions_and_density_are_said_for_the_casts_they_apply_to(self, tmp_path):
        converted, plain = make_cast(), make_cast(lat=62.4)
        converted["transform_log"] = {
            "transformations_applied": [{"column": "Temp_C", "from": "degF", "to": "degC"}],
            "seawater_density_kg_m3": 1025.0, "seawater_density_provenance": "assumed"}
        out = tmp_path / "f.nc"
        write_ragged_profiles([converted, plain], str(out))
        with nc.Dataset(out) as ds:
            assert "unit_conversions" not in ds.ncattrs()
            assert [str(x) for x in ds["unit_conversions"][:]] == ["Temp_C: degF -> degC", ""]
            # used by one cast only: said for that cast, not claimed for the file
            assert "seawater_density_kg_m3" not in ds.ncattrs()
            assert ds["seawater_density_kg_m3"][:].mask.tolist() == [False, True]
        plain["transform_log"] = copy.deepcopy(converted["transform_log"])
        write_ragged_profiles([converted, plain], str(out))
        with nc.Dataset(out) as ds:                  # the same for every cast: the file's
            assert ds.seawater_density_kg_m3 == 1025.0
            assert ds.unit_conversions == "Temp_C: degF -> degC"
        plain["transform_log"] = {"seawater_density_kg_m3": 1027.0,
                                  "seawater_density_provenance": "from the header"}
        write_ragged_profiles([converted, plain], str(out))
        with nc.Dataset(out) as ds:
            assert list(ds["seawater_density_kg_m3"][:]) == [1025.0, 1027.0]
            assert [str(x) for x in ds["seawater_density_provenance"][:]] == [
                "assumed", "from the header"]


class TestAFileIsDoneOnlyWhenItsResultIsSaved:
    def test_a_failure_after_the_write_leaves_no_result_to_assemble(self, tmp_path, monkeypatch):
        import importlib

        pa = importlib.import_module("wod_etl.orchestration.process_accession")
        src = tmp_path / ACC
        src.mkdir()
        (src / "a.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))

        def boom(built):
            raise RuntimeError("late failure")
        monkeypatch.setattr(pa, "_coded_header_report", boom)
        res = _run(src, tmp_path)
        assert res["counts"].get("failed") == 1
        assert res["per_file_casts"] == [] and res["outputs"] == []


class TestARunWritesOnlyWhatItAnswersFor:
    def test_two_files_of_one_name_and_different_extensions_are_both_written(self, tmp_path):
        """e.g. Sea-Bird's x.cnv beside x.btl."""
        src = tmp_path / ACC
        src.mkdir()
        (src / "x.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        (src / "x.txt").write_text(_cast_block(2, "2015-11-10", 21.5, 20))
        res = _run(src, tmp_path)
        assert len(set(res["per_file_casts"])) == 2
        assert sorted(_casts(res["outputs"][0])["sources"]) == ["x.csv", "x.txt"]

    def test_an_include_batch_does_not_replace_another_batchs_casts(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        for name in ("a.csv", "b.csv"):
            (src / name).write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        first = _run(src, tmp_path, run_id="one", include=["a.csv"])
        second = _run(src, tmp_path, run_id="two", include=["b.csv"])
        assert second["outputs"] == [] and "left as it is" in \
            second["assembly"]["problems"][0]["error"]
        assert _casts(first["outputs"][0])["sources"] == ["a.csv"]

    def test_a_renamed_source_is_replaced_not_kept_beside_its_new_name(self, tmp_path):
        src = tmp_path / ACC
        src.mkdir()
        (src / "old_name.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        _run(src, tmp_path, run_id="before")
        (src / "old_name.csv").rename(src / "new_name.csv")
        res = _run(src, tmp_path, run_id="after")
        assert _casts(res["outputs"][0])["sources"] == ["new_name.csv"]

    def test_a_run_id_reused_on_another_folder_processes_that_folders_file(self, tmp_path):
        for folder, lat in (("cruiseA", 21.0), ("cruiseB", 30.0)):
            (tmp_path / folder).mkdir()
            (tmp_path / folder / "cast.csv").write_text(_cast_block(1, "2015-11-09", lat, 20))
        _run(tmp_path / "cruiseA", tmp_path, run_id="same", resume=True)
        res = _run(tmp_path / "cruiseB", tmp_path, run_id="same", resume=True)
        [out] = res["outputs"]
        assert Path(out).name == "wod_ctd_2015_cruiseB.nc"
        with nc.Dataset(out) as ds:
            assert float(ds["lat"][0]) == pytest.approx(30.0)
            assert ds.source_file == "cast.csv"                # not a local path

    def test_a_folder_name_with_no_usable_character_still_scopes_the_names(self, tmp_path):
        src = tmp_path / "航次"
        src.mkdir()
        (src / "a.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        [out] = _run(src, tmp_path)["outputs"]
        assert Path(out).name.startswith("wod_ctd_2015_scope-")

    def test_a_date_named_folder_is_not_taken_for_an_accession(self, tmp_path):
        src = tmp_path / "cruise" / "201511"
        src.mkdir(parents=True)
        (src / "a.csv").write_text(_cast_block(1, "2015-11-09", 21.0, 20))
        [out] = _run(src, tmp_path)["outputs"]
        assert Path(out).name == "wod_ctd_2015_201511.nc"

    def test_ids_that_only_look_suffixed_are_not_duplicates(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        for c, src in ((a, "stn12.csv"), (b, "stn12_02.csv")):
            c["contract"]["source"] = src
            c["contract"]["parsed_coordinates"].pop("year")          # undated: id ends in name
        write_ragged_profiles([a, b], str(tmp_path / "f.nc"))
        report = verify_output(str(tmp_path))
        assert not any(f["finding"] == "duplicate_cast_id" for f in report["findings"])


class TestTaxaRecordFilesSitBesideTheProfiles:
    """Taxa-Record files are named so profile assembly never touches them."""

    def test_the_name(self):
        from wod_etl.writers import is_output_file_name, is_taxa_file_name, taxa_file_name

        name = taxa_file_name(2015, ACC)
        assert name == f"wod_osd_2015_{ACC}_taxa.nc"
        assert is_taxa_file_name(name, ACC) and is_output_file_name(name, ACC)
        assert not is_wod_file_name(name, ACC)
        assert not is_taxa_file_name(name, "0000001")
        assert not is_taxa_file_name(f"wod_ctd_2015_{ACC}_taxa.nc", ACC)
        assert taxa_file_name(None, ACC) == f"wod_osd_undated_{ACC}_taxa.nc"

    def test_profile_assembly_leaves_a_taxa_file_alone(self, tmp_path):
        from wod_etl.writers import taxa_file_name

        out = tmp_path / "out"
        out.mkdir()
        taxa = out / taxa_file_name(2015, ACC)
        taxa.write_text("taxa")
        assemble_profiles([], out, ACC)
        assert taxa.exists()
