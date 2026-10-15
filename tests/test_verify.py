"""Verification: the structural check, and the audit that catches what it cannot.

`validate_intermediate_netcdf` proves a file obeys CF and the ragged-array contract. That
is necessary and not sufficient -- a wrong unit, a dropped column and an impossible value
all validate cleanly. These tests pin the cases where the validator must fail, especially
the empty-array ones, which once let an all-NaN depth axis through as a PASS.
"""
import netCDF4 as nc
import numpy as np
import pytest

from wod_etl.verify import validate_intermediate_netcdf
from wod_etl.writers import write_ragged_profiles
from helpers import make_cast


class TestValidator:
    def test_well_formed_file_passes(self, tmp_path):
        p = tmp_path / "good.nc"
        write_ragged_profiles([make_cast(dic=True)], str(p))
        report = validate_intermediate_netcdf(str(p))
        assert report["passed"], report["errors"]

    def test_rejects_per_variable_observation_dimensions(self, tmp_path):
        """The old layout used to pass validation."""
        p = tmp_path / "old.nc"
        with nc.Dataset(p, "w", format="NETCDF4") as ds:
            ds.Conventions = "CF-1.8"; ds.featureType = "profile"
            ds.wod_dataset_type = "CTD"; ds.WOD_version = "WOD23"
            ds.createDimension("casts", 1); ds.createDimension("z_obs", 3)
            ds.createDimension("Temperature_obs", 3)
            for n in ("lat", "lon"):
                ds.createVariable(n, "f4", ("casts",))[0] = 10.0
            z = ds.createVariable("z", "f4", ("z_obs",)); z.positive = "down"; z[:] = [0, 1, 2]
            r = ds.createVariable("z_row_size", "i4", ("casts",)); r.sample_dimension = "z_obs"; r[0] = 3
            t = ds.createVariable("Temperature", "f4", ("Temperature_obs",))
            t.wod_variable_code = 1; t[:] = [1, 2, 3]
        report = validate_intermediate_netcdf(str(p))
        assert not report["passed"]
        assert any("shared observation dimension" in e for e in report["errors"])

    def test_rejects_non_cf_variable_names(self, tmp_path):
        p = tmp_path / "spacey.nc"
        with nc.Dataset(p, "w", format="NETCDF4") as ds:
            ds.Conventions = "CF-1.8"; ds.featureType = "profile"
            ds.wod_dataset_type = "CTD"; ds.WOD_version = "WOD23"
            ds.createDimension("casts", 1); ds.createDimension("z_obs", 2)
            for n in ("lat", "lon"):
                ds.createVariable(n, "f4", ("casts",))[0] = 10.0
            z = ds.createVariable("z", "f4", ("z_obs",)); z.positive = "down"; z[:] = [0, 1]
            r = ds.createVariable("z_row_size", "i4", ("casts",)); r.sample_dimension = "z_obs"; r[0] = 2
            d = ds.createVariable("Dissolved Inorganic Carbon", "f4", ("z_obs",))
            d.wod_variable_code = 21; d[:] = [1, 2]
        report = validate_intermediate_netcdf(str(p))
        assert not report["passed"]
        assert any("not a valid CF" in e for e in report["errors"])

    def test_missing_coordinates_fail(self, tmp_path):
        cast = make_cast()
        cast["contract"]["parsed_coordinates"] = {}
        p = tmp_path / "nocoord.nc"
        write_ragged_profiles([cast], str(p))
        report = validate_intermediate_netcdf(str(p))
        assert not report["passed"]
        assert any("entirely missing" in e for e in report["errors"])

    def test_unreadable_file_reports_fatal_error(self, tmp_path):
        bad = tmp_path / "notnc.nc"
        bad.write_text("definitely not netcdf")
        assert validate_intermediate_netcdf(str(bad))["status"] == "FAIL"


class TestValidatorCatchesEmptyArrays:
    """A file can satisfy every structural rule and still carry no usable data.

    Both of these were produced against a real accession -- once when a Sea-Bird DepSM
    column failed to map, once when a surface dataset had no depth column at all -- and
    both passed validation. They were caught only by reading the values by hand.
    """

    def test_all_fill_depth_axis_is_an_error(self, tmp_path):
        p = tmp_path / "no_depth.nc"
        write_ragged_profiles([make_cast()], str(p))
        with nc.Dataset(p, "a") as ds:
            ds.variables["z"][:] = np.nan
        report = validate_intermediate_netcdf(str(p))
        assert not report["passed"]
        assert any("no usable depth axis" in e for e in report["errors"])

    def test_partially_missing_depth_is_only_a_warning(self, tmp_path):
        p = tmp_path / "some_depth.nc"
        write_ragged_profiles([make_cast(rows=3)], str(p))
        with nc.Dataset(p, "a") as ds:
            ds.variables["z"][1] = np.nan
        report = validate_intermediate_netcdf(str(p))
        assert report["passed"]
        assert any("'z' is missing for 1" in w for w in report["warnings"])

    def test_all_fill_measured_variable_warns(self, tmp_path):
        """An entirely unanalysed source column is honest output, but must not pass
        unremarked -- it is indistinguishable from a mapping failure otherwise."""
        p = tmp_path / "empty_var.nc"
        write_ragged_profiles([make_cast(dic=True)], str(p))
        with nc.Dataset(p, "a") as ds:
            name = next(n for n, v in ds.variables.items()
                        if getattr(v, "wod_variable_code", None) is not None)
            ds.variables[name][:] = np.nan
        report = validate_intermediate_netcdf(str(p))
        assert report["passed"]
        assert any("is entirely missing" in w for w in report["warnings"])


class TestAuditSurvivesTextMetadata:
    """The writer emits vlen-string secondary_header_* variables whenever a header differs
    between casts -- the normal multi-cast case, e.g. per-cast station numbers. The audit
    coerced every non-cast_id variable to float and died on the first one."""

    def _dataset(self, tmp_path, headers_per_cast):
        casts = []
        for i, headers in enumerate(headers_per_cast):
            c = make_cast(lat=61.0 + i)
            c["contract"]["extracted_header_metadata"] = headers
            casts.append(c)
        proc = tmp_path / "proc"; proc.mkdir()
        (tmp_path / "staged").mkdir()
        write_ragged_profiles(casts, str(proc / "multi.nc"))
        from wod_etl.verify import audit_dataset
        return audit_dataset(str(tmp_path / "staged"), "CTD", str(proc))

    def test_per_cast_station_numbers_do_not_abort_the_audit(self, tmp_path):
        report = self._dataset(tmp_path, [
            {"7": {"name": "Originator's station number", "value": "A01"}},
            {"7": {"name": "Originator's station number", "value": "A02"}}])
        assert report["casts"] == 2
        assert report["value_ranges"]["temperature"]["n"] > 0

    def test_the_text_metadata_is_reported_not_silently_skipped(self, tmp_path):
        report = self._dataset(tmp_path, [
            {"7": {"name": "Originator's station number", "value": "A01"}},
            {"7": {"name": "Originator's station number", "value": "A02"}}])
        station = report["text_variables"]["secondary_header_7_originator_s_station_number"]
        assert station == ["A01", "A02"]
        assert "A01" in report["text"]

    def test_a_numeric_per_cast_header_is_still_a_value_range(self, tmp_path):
        """Bottom depth varies per cast too, but it is a number and must be ranged."""
        report = self._dataset(tmp_path, [
            {"10": {"name": "Bottom Depth (meters)", "value": 3212.4}},
            {"10": {"name": "Bottom Depth (meters)", "value": 1409.3}}])
        r = report["value_ranges"]["secondary_header_10_bottom_depth_meters"]
        assert (r["min"], r["max"]) == pytest.approx((1409.3, 3212.4))


class TestTheRunVerifiesItsOwnOutput:
    """A wrong unit passes structural validation. The numbers have to be judged too."""

    def _accession(self, tmp_path, rows, columns="Depth (m),Temperature (deg C),Salinity (PSU)"):
        acc = tmp_path / "acc" / "1-ctd"
        acc.mkdir(parents=True)
        (acc / "cast.csv").write_text(
            "# Latitude: 34.25\n# Longitude: -120.5\n# Date: 2024-03-04\n"
            f"{columns}\n{rows}\n")
        return acc.parent

    def _run(self, src, tmp_path, name):
        from wod_etl.orchestration import process_accession
        return process_accession(str(src), output_dir=str(tmp_path / f"out-{name}"),
                                 raw_dir=str(tmp_path / f"raw-{name}"),
                                 run_id=f"t_vo_{name}", resume=False)

    def test_plausible_values_verify(self, tmp_path):
        src = self._accession(tmp_path, "0,15.1,33.4\n1,14.8,33.5\n2,13.9,33.6")
        res = self._run(src, tmp_path, "ok")
        assert res["output_verified"] is True
        assert res["output_verification"]["finding_counts"]["high"] == 0

    def test_a_whole_variable_out_of_range_fails_verification(self, tmp_path):
        """Fahrenheit in a column labelled Celsius: nothing converts it, and it validates."""
        src = self._accession(tmp_path, "0,60.5,33.4\n1,60.1,33.5\n2,59.4,33.6")
        res = self._run(src, tmp_path, "degf")
        # The run itself is clean -- that is the whole point of checking separately.
        assert res["status"] == "complete"
        assert res["output_verified"] is False
        finding = res["output_verification"]["findings"][0]
        assert finding["finding"] == "systematically_out_of_range"
        assert finding["variable"] == "temperature"
        assert "unit or a conversion" in finding["action"]

    def test_a_flat_depth_axis_fails_verification(self, tmp_path):
        """Every level at one depth is not a profile, however well-formed the file is."""
        src = self._accession(tmp_path, "10,15.1,33.4\n10,14.8,33.5\n10,13.9,33.6")
        res = self._run(src, tmp_path, "flat")
        assert res["output_verified"] is False
        kinds = {f["finding"] for f in res["output_verification"]["findings"]}
        assert "flat_depth_axis" in kinds

    def test_verification_can_be_turned_off(self, tmp_path):
        src = self._accession(tmp_path, "0,15.1,33.4\n1,14.8,33.5\n2,13.9,33.6")
        from wod_etl.orchestration import process_accession
        res = process_accession(str(src), output_dir=str(tmp_path / "off"),
                                raw_dir=str(tmp_path / "off-raw"), run_id="t_vo_off",
                                resume=False, verify=False)
        assert res["output_verified"] is None
        assert res["output_verification"]["skipped"] == "verify=False"

    def test_no_output_is_not_a_verification_failure(self, tmp_path):
        """A run that wrote nothing has nothing to verify, which is not the same as bad."""
        acc = tmp_path / "empty-acc" / "0-docs"
        acc.mkdir(parents=True)
        (acc / "readme.txt").write_text("Nothing but prose in this accession, no data.\n")
        res = self._run(acc.parent, tmp_path, "none")
        assert res["output_verified"] is None
        assert "no output" in res["output_verification"]["skipped"]


class TestVerifyOutputDirectly:
    def test_an_empty_directory_passes_with_a_note(self, tmp_path):
        from wod_etl.verify import verify_output
        report = verify_output(str(tmp_path))
        assert report["passed"] is True and report["netcdf_files"] == 0
        assert report["note"] == "no output to verify"

    def test_every_finding_carries_evidence_and_an_action(self, tmp_path):
        """A finding nobody can act on is noise."""
        from wod_etl.orchestration import process_accession
        from wod_etl.verify import verify_output

        acc = tmp_path / "a" / "1-ctd"
        acc.mkdir(parents=True)
        (acc / "cast.csv").write_text(
            "# Latitude: 34.25\n# Longitude: -120.5\n# Date: 2024-03-04\n"
            "Depth (m),Temperature (deg C)\n0,60.5\n1,60.1\n2,59.4\n")
        out = tmp_path / "out"
        process_accession(str(acc.parent), output_dir=str(out),
                          raw_dir=str(tmp_path / "r"), run_id="t_vo_ev", resume=False)
        report = verify_output(str(out))
        assert report["findings"]
        for f in report["findings"]:
            assert f["evidence"] and f["action"]
            assert f["severity"] in ("high", "medium", "low")

    def test_the_basin_union_caveat_is_stated(self):
        """The bounds are more permissive than a basin-aware check, and saying so matters:
        an unflagged value can still be wrong."""
        from wod_etl.verify.verify_output import render
        text = render({"processed_dir": "x", "netcdf_files": 0, "casts": 0,
                       "observations": 0, "findings": [], "variables": {},
                       "flag_distribution": {}})
        assert "union across all 30 ocean basins" in text


class TestVerifierJudgesWhatAReaderWouldMisread:
    def _write(self, tmp_path, name, casts):
        write_ragged_profiles(casts, str(tmp_path / name))

    def test_a_small_negative_near_zero_is_an_offset_not_impossible(self, tmp_path):
        from wod_etl.verify import verify_output

        cast = make_cast(rows=40)
        cast["df"]["Chl"] = np.where(np.arange(40) % 3 == 0, -0.02, 0.4)
        cast["contract"]["column_mapping"].append(
            {"raw_column": "Chl", "wod_code": 11, "wod_name": "Chlorophyll",
             "target_unit": "Microgram liter-1"})
        self._write(tmp_path, "a.nc", [cast])
        r = verify_output(str(tmp_path))
        kinds = {(f["finding"], f["severity"]) for f in r["findings"]}
        assert ("near_zero_offset", "low") in kinds
        assert not any(f["finding"] == "out_of_range_levels" for f in r["findings"])
        assert r["variables"]["chlorophyll"]["out_of_range"]["near_zero_offset_levels"] == 14

    def test_a_large_negative_is_still_out_of_range(self, tmp_path):
        from wod_etl.verify import verify_output

        cast = make_cast(rows=40)
        cast["df"]["Chl"] = np.where(np.arange(40) % 3 == 0, -5.0, 0.4)
        cast["contract"]["column_mapping"].append(
            {"raw_column": "Chl", "wod_code": 11, "wod_name": "Chlorophyll",
             "target_unit": "Microgram liter-1"})
        self._write(tmp_path, "a.nc", [cast])
        r = verify_output(str(tmp_path))
        assert any(f["finding"] == "out_of_range_levels" for f in r["findings"])
        assert not any(f["finding"] == "near_zero_offset" for f in r["findings"])

    def test_a_cast_id_repeated_across_files_fails_verification(self, tmp_path):
        from wod_etl.verify import verify_output

        self._write(tmp_path, "a.nc", [make_cast()])
        self._write(tmp_path, "b.nc", [make_cast(lat=62.4)])
        r = verify_output(str(tmp_path))
        assert not r["passed"]
        dup = next(f for f in r["findings"] if f["finding"] == "duplicate_cast_id")
        assert dup["severity"] == "high" and "a.nc" in dup["evidence"]


class TestACopiedVariableIsNotVerified:
    def test_salinity_equal_to_temperature_at_every_level_is_a_high_finding(self, tmp_path):
        from helpers import make_cast

        from wod_etl.verify import verify_output
        from wod_etl.writers import write_ragged_profiles

        cast = make_cast(rows=12)
        cast["df"]["Sal"] = cast["df"]["Temp_C"]
        cast["df"]["Sal_qc_flag"] = 0
        cast["contract"]["column_mapping"].append(
            {"raw_column": "Sal", "wod_code": 2, "wod_name": "Salinity",
             "target_unit": "Dimensionless"})
        write_ragged_profiles([cast], str(tmp_path / "copy.nc"))
        report = verify_output(str(tmp_path))
        assert not report["passed"]
        f = next(f for f in report["findings"] if f["finding"] == "identical_variables")
        assert f["variable"] == "salinity" and "drop" in f["action"]
