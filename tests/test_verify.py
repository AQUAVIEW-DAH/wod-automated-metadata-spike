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
