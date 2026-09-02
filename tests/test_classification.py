"""Format sniffing, multi-format reading, and deciding a file's dataset without being told.

`dataset_type` used to be an argument. These tests pin the behaviour that replaced it: a
file is identified from its own path, header, columns and depth spacing, and a file that
cannot become a WOD profile is skipped with a stated reason rather than failing four
phases later or, worse, being written as a plausible wrong profile.
"""
import numpy as np
import pytest

from wod_etl.discovery import (classify_accession, classify_dataset, read_any,
                               sniff_format)
from wod_etl.orchestration import process_accession

netCDF4 = pytest.importorskip("netCDF4")


# --------------------------------------------------------------- fixtures
@pytest.fixture
def mixed(tmp_path):
    """One accession holding four instruments and three things WOD cannot store."""
    acc = tmp_path / "0999999"

    def write(rel, text):
        p = acc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    write("0-readme/about.txt",
          "Accession 0999999. Folder 2 holds CTD, folder 3 XBT, folder 4 ADCP currents.\n")
    write("1-ctd/cast_01.csv",
          "# Latitude: 34.25\n# Longitude: -120.5\n# Date: 2024-03-04\n"
          "Depth (m),Temperature (deg C),Salinity (PSU)\n"
          + "".join(f"{z},{15 - z / 100:.2f},33.4\n" for z in range(0, 200, 1)))
    write("2-xbt/drop_07.csv",
          "# Latitude: 10.0\n# Longitude: -30.0\n# Date: 2024-03-05\n"
          "Depth (m),Temperature (deg C)\n0,28.1\n50,27.0\n100,22.4\n200,14.1\n")
    write("3-adcp/currents.csv",
          "Depth (m),u_vel (m/s),v_vel (m/s)\n10,0.12,-0.04\n20,0.10,-0.06\n")
    write("4-plankton/biomass.csv",
          "Upper depth (m),Lower depth (m),Taxon,Displacement volume (ml)\n"
          "0,100,Calanus finmarchicus,4.2\n0,100,Euphausia pacifica,1.1\n")
    write("5-nutrients/nuts.csv",
          "Station,Depth (m),Nitrate (umol/kg)\n001,0,Not analyzed\n001,50,Not analyzed\n")
    return acc


@pytest.fixture
def argo_file(tmp_path):
    path = tmp_path / "D5904471_001.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("N_PROF", 2)
        ds.createDimension("N_LEVELS", 4)
        ds.title = "Argo float vertical profile"
        ds.time_coverage_start = "20240304T091500Z"
        for name, unit, base in (("PRES", "decibar", 0.0), ("TEMP", "degree_Celsius", 12.0),
                                 ("PSAL", "psu", 34.0)):
            v = ds.createVariable(name, "f4", ("N_PROF", "N_LEVELS"))
            v.units = unit
            v[:] = np.array([[base + i for i in range(4)],
                             [base + 2 * i for i in range(4)]], dtype="f4")
        for name, vals in (("LATITUDE", [34.25, 35.10]), ("LONGITUDE", [-120.5, -121.0])):
            v = ds.createVariable(name, "f8", ("N_PROF",))
            v[:] = vals
    return path


# ----------------------------------------------------------- sniff_format
class TestSniffReadsBytesNotExtensions:
    def test_netcdf_is_recognised_without_an_extension(self, argo_file, tmp_path):
        renamed = tmp_path / "cast_007"
        renamed.write_bytes(argo_file.read_bytes())
        assert sniff_format(str(renamed))["reader"] == "netcdf"

    def test_csv_named_dat_is_still_delimited(self, tmp_path):
        p = tmp_path / "station.dat"
        p.write_text("Depth,Temp\n0,15.1\n10,14.8\n25,13.9\n")
        result = sniff_format(str(p))
        assert result["reader"] == "delimited"
        assert result["delimiter"] == ","

    def test_prose_with_numbers_is_not_a_table(self, tmp_path):
        p = tmp_path / "readme.txt"
        p.write_text("Cruise EX24-01 sailed 4 March 2024 with 12 stations.\n"
                     "Station 7 was aborted after 3 casts because of weather.\n"
                     "See folder 2 for the 48 CTD downcasts collected on leg 1.\n")
        result = sniff_format(str(p))
        assert result["reader"] == "none"
        assert not result["readable"]

    def test_a_pdf_is_a_document_but_an_odd_text_file_is_not(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        assert sniff_format(str(pdf))["is_document"]

        junk = tmp_path / "cast.dat"
        junk.write_text("this file should have been a cast and is not\n")
        # Unreadable but NOT a document: the caller must fail rather than skip it, or a
        # real cast in an unrecognised layout vanishes from a run without a word.
        assert not sniff_format(str(junk))["is_document"]

    def test_fixed_width_columns_are_found(self, tmp_path):
        p = tmp_path / "old.dat"
        p.write_text("   0.0   11.42   34.51\n  10.0   11.38   34.52\n"
                     "  25.0   11.02   34.55\n  50.0   10.31   34.58\n")
        result = sniff_format(str(p))
        assert result["reader"] == "fixed_width"
        assert len(result["colspecs"]) == 3


# --------------------------------------------------------------- read_any
class TestReadAnyProducesTheSameShapeForEveryFormat:
    def test_argo_profile_x_level_becomes_one_cast_per_profile(self, argo_file):
        casts, info = read_any(str(argo_file))
        assert info.get("error") is None
        assert len(casts) == 2
        assert casts[0]["df"].shape == (4, 3)

    def test_units_are_folded_into_the_column_name(self, argo_file):
        """Unit detection reads the raw column name and nothing else, so it must be there."""
        casts, _ = read_any(str(argo_file))
        assert "TEMP (degree_Celsius)" in casts[0]["df"].columns

    def test_netcdf_coordinates_reach_the_header_block(self, argo_file):
        casts, _ = read_any(str(argo_file))
        assert "Latitude: 34.25" in casts[0]["header_block"]
        assert "Longitude: -120.5" in casts[0]["header_block"]
        assert "Date: 2024-03-04" in casts[0]["header_block"]

    def test_ragged_array_splits_on_row_size(self, tmp_path):
        path = tmp_path / "ragged.nc"
        with netCDF4.Dataset(path, "w") as ds:
            ds.createDimension("profile", 2)
            ds.createDimension("obs", 7)
            rs = ds.createVariable("row_size", "i4", ("profile",))
            rs.sample_dimension = "obs"
            rs[:] = [3, 4]
            z = ds.createVariable("depth", "f4", ("obs",))
            z.units = "m"
            z[:] = [0, 10, 20, 0, 25, 50, 75]
            t = ds.createVariable("temperature", "f4", ("obs",))
            t.units = "degC"
            t[:] = [11.4, 11.2, 10.8, 9.9, 9.5, 8.2, 7.1]
        casts, _ = read_any(str(path))
        assert [len(c["df"]) for c in casts] == [3, 4]

    def test_sea_bird_header_names_the_columns(self, tmp_path):
        p = tmp_path / "cast.cnv"
        p.write_text("* Sea-Bird SBE 9 Data File:\n"
                     "* NMEA Latitude = 34 15.00 N\n"
                     "# name 0 = prDM: Pressure, Digiquartz [db]\n"
                     "# name 1 = t090C: Temperature [ITS-90, deg C]\n"
                     "*END*\n    1.000   11.4231\n    2.000   11.4118\n")
        casts, info = read_any(str(p))
        assert info.get("error") is None
        assert casts[0]["df"].columns[0].startswith("prDM")
        assert "Latitude" in casts[0]["header_block"]

    def test_headerless_fixed_width_does_not_eat_its_first_level(self, tmp_path):
        p = tmp_path / "old.dat"
        p.write_text("   0.0   11.42   34.51\n  10.0   11.38   34.52\n"
                     "  25.0   11.02   34.55\n  50.0   10.31   34.58\n")
        casts, info = read_any(str(p))
        assert len(casts[0]["df"]) == 4          # not 3
        assert list(casts[0]["df"].columns) == ["col_0", "col_1", "col_2"]
        assert any("col_0" in w for w in info["warnings"])


# -------------------------------------------------------- classify_dataset
class TestClassifyDataset:
    @pytest.mark.parametrize("path,expected", [
        ("2-CTD/cast_01.cnv", "CTD"),
        ("cruise/xbt-drops/t7_0012.edf", "XBT"),
        ("arc0222/argo/D5904471_001.nc", "PFL"),
        ("data/glider_slocum/unit_286.nc", "GLD"),
        ("noaa/moored-buoy-tao/t0n165e.csv", "MRB"),
        ("sub/seal_tag_meop/ct148.nc", "APB"),
        ("leg1/underway-tsg/track.csv", "SUR"),
    ])
    def test_instrument_is_read_from_the_path(self, path, expected):
        assert classify_dataset(path=path)["dataset_type"] == expected

    def test_underscores_and_hyphens_do_not_hide_a_word(self):
        """'\\bglider\\b' does not match 'glider_slocum': _ is a word character to re."""
        assert classify_dataset(path="a/glider_slocum/x.nc")["dataset_type"] == "GLD"
        assert classify_dataset(path="a/02-CTD-downcast/x.csv")["dataset_type"] == "CTD"

    def test_nothing_identifiable_is_not_guessed(self):
        result = classify_dataset(path="misc/data.txt", columns=["A", "B"])
        assert result["dataset_type"] is None
        assert result["verdict"] == "unidentified"
        assert "note" in result

    def test_currents_and_plankton_are_excluded_with_a_route(self):
        adcp = classify_dataset(path="4-adcp/currents.csv")
        assert not adcp["is_wod_profile_data"]
        assert "Table 3a" in adcp["exclusion"]["route"]

        plankton = classify_dataset(path="5-plankton/biomass.csv")
        assert not plankton["is_wod_profile_data"]
        assert plankton["exclusion"]["belongs_in_wod"] is True
        assert "Table 7a" in plankton["exclusion"]["route"]

    def test_a_conductivity_column_does_not_exclude_a_ctd_cast(self):
        """The column is dropped and reported. Excluding the file loses temperature too."""
        result = classify_dataset(
            path="1-ctd/cast_01.csv",
            columns=["Depth (m)", "Temperature (deg C)", "Salinity (PSU)",
                     "Conductivity (S/m)"])
        assert result["is_wod_profile_data"]
        assert result["dataset_type"] == "CTD"
        assert result["unstorable_columns"][0]["column"] == "Conductivity (S/m)"

    def test_prose_mentioning_adcp_does_not_exclude_the_ctd_beside_it(self):
        result = classify_dataset(
            path="1-ctd/cast_01.csv",
            columns=["Depth (m)", "Temperature (deg C)"],
            header_text="Cruise report: see folder 4 for ADCP current velocities.")
        assert result["is_wod_profile_data"]
        assert any("check it during triage" in f for f in result["possible_exclusions"])

    def test_depth_spacing_separates_high_from_low_resolution(self):
        assert any("high-resolution" in e for e in
                   classify_dataset(path="x/y.csv", depth_increment_m=0.5)["evidence"])
        low = classify_dataset(path="x/y.csv", depth_increment_m=25.0)
        assert low["dataset_type"] == "OSD"


# ------------------------------------------------------ classify_accession
class TestClassifyAccession:
    def test_each_folder_gets_its_own_verdict(self, mixed):
        report = classify_accession(str(mixed))
        assert set(report["process"]) == {"1-ctd", "2-xbt"}
        assert {"3-adcp", "4-plankton", "5-nutrients", "0-readme"} <= set(report["exclude"])

    def test_an_all_sentinel_file_is_reported_as_holding_no_data(self, mixed):
        report = classify_accession(str(mixed))
        verdict = report["folders"]["5-nutrients"]["verdict"]
        assert verdict["action"] == "exclude"
        assert "no measurements" in verdict["reason"]

    def test_the_dataset_mix_is_named_per_folder(self, mixed):
        report = classify_accession(str(mixed))
        assert report["folders"]["2-xbt"]["verdict"]["dataset_types"] == {"XBT": 1}


# ----------------------------------------------------- end-to-end routing
class TestProcessAccessionWithoutBeingToldTheDataset:
    def test_a_mixed_accession_routes_each_folder_to_its_own_dataset(self, mixed, tmp_path):
        res = process_accession(str(mixed), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_1",
                                resume=False)
        assert res["dataset_types"], "no file was classified"
        # XBT is a WOD dataset in its own right (Table 2), so it stays XBT. Only XCTD/STD
        # route by resolution.
        assert set(res["dataset_types"]) == {"CTD", "XBT"}
        assert len(res["outputs"]) == 2

    def test_non_profile_folders_are_skipped_not_failed(self, mixed, tmp_path):
        res = process_accession(str(mixed), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_2",
                                resume=False)
        reasons = " ".join(res["skipped_reasons"])
        assert "current velocity" in reasons
        assert "plankton" in reasons
        assert "no measurements" in reasons

    def test_dropped_columns_are_reported_not_silent(self, tmp_path):
        acc = tmp_path / "acc"
        acc.mkdir()
        (acc / "1-ctd_cast.csv").write_text(
            "# Latitude: 34.25\n# Longitude: -120.5\n# Date: 2024-03-04\n"
            "Depth (m),Temperature (deg C),Conductivity (S/m)\n"
            "0,15.1,4.41\n1,15.0,4.40\n2,14.9,4.39\n")
        res = process_accession(str(acc), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_3",
                                resume=False)
        assert any("Conductivity" in k for k in res["unstorable_columns_dropped"])

    def test_an_explicit_dataset_type_still_wins(self, mixed, tmp_path):
        res = process_accession(str(mixed), dataset_type="XBT",
                                output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_4",
                                resume=False)
        assert set(res["dataset_types"]) == {"XBT"}

    def test_overrides_apply_per_folder(self, mixed, tmp_path):
        res = process_accession(str(mixed), dataset_overrides={"2-xbt": "XBT"},
                                output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_5",
                                resume=False)
        assert res["dataset_types"].get("XBT") == 1
        assert res["dataset_types"].get("CTD") == 1

    def test_dry_run_says_what_each_file_would_be_processed_as(self, mixed, tmp_path):
        res = process_accession(str(mixed), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_auto_6",
                                resume=False, dry_run=True)
        decided = {item["key"]: item["dataset_type"] for item in res["would_process"]}
        assert decided["1-ctd/cast_01.csv"] == "CTD"
        assert decided["2-xbt/drop_07.csv"] == "XBT"
        assert any(s["key"].startswith(("3-adcp", "4-plankton")) for s in res["would_skip"])


class TestTheInputIsDiagnosedWhenItIsNotADirectory:
    """A metadata landing page is not an accession. Crawled, it is a valid empty page."""

    LANDING = ("https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso"
               "?id=gov.noaa.nodc:146739")

    def test_accession_number_is_recovered_from_every_form(self):
        from wod_etl.discovery import accession_number

        assert accession_number("0146739") == "146739"
        assert accession_number(self.LANDING) == "146739"
        assert accession_number("https://www.ncei.noaa.gov/archive/accession/0146739") == "146739"
        assert accession_number("data/raw/some-folder") is None

    def test_a_landing_page_is_reported_as_the_wrong_kind_of_url(self):
        """Previously: status 'empty', 'no files found; check the URL' -- a typo hunt."""
        from wod_etl.discovery.list_remote_directory import _diagnose_empty

        res = _diagnose_empty(self.LANDING)
        assert "not an accession data directory" in res["error"]
        assert res["accession_number"] == "146739"
        assert "archive/accession/download/146739" in res["hint"]

    def test_an_ordinary_empty_directory_is_not_misdiagnosed(self):
        from wod_etl.discovery.list_remote_directory import _diagnose_empty

        res = _diagnose_empty("https://example.org/data/cruise/")
        assert "error" not in res
        assert "hint" in res


class TestCrossFileShapeIsDetected:
    """One cast split across files is invisible in any single file."""

    def _acc(self, tmp_path, layout):
        acc = tmp_path / "acc"
        for folder, files in layout.items():
            d = acc / folder
            d.mkdir(parents=True)
            for name, text in files.items():
                (d / name).write_text(text)
        return acc

    def test_one_file_per_depth_level(self, tmp_path):
        acc = self._acc(tmp_path, {"1-levels": {
            f"lvl_{i}.csv": f"Depth (m),Temperature (deg C),Salinity (PSU)\n{z},14.0,33.4\n"
            for i, z in enumerate([0, 10, 25, 50, 100])}})
        shapes = classify_accession(str(acc))["cross_file_shapes"]
        assert shapes["1-levels"]["shape"] == "one file per depth level"
        assert "combine_levels" in shapes["1-levels"]["action"]

    def test_one_file_per_variable_names_the_join_key(self, tmp_path):
        acc = self._acc(tmp_path, {"2-vars": {
            "temp.csv": "Depth (m),Temperature (deg C)\n0,15.1\n10,14.8\n25,13.9\n",
            "oxy.csv": "Depth (m),Oxygen (ml/l)\n0,5.8\n10,5.7\n25,5.5\n",
            "nut.csv": "Depth (m),Nitrate (umol/kg)\n0,0.2\n10,0.4\n25,1.1\n"}})
        shape = classify_accession(str(acc))["cross_file_shapes"]["2-vars"]
        assert shape["shape"] == "one file per variable"
        assert shape["join_key"] == "Depth (m)"
        assert "per_table" in shape["action"]      # the overlap check must be named

    def test_one_cast_per_file_is_left_alone(self, tmp_path):
        rows = "\n".join(f"{z},14.0,33.4" for z in (0, 10, 25, 50))
        acc = self._acc(tmp_path, {"3-casts": {
            f"cast_{i}.csv": f"Depth (m),Temperature (deg C),Salinity (PSU)\n{rows}\n"
            for i in range(3)}})
        assert not classify_accession(str(acc))["cross_file_shapes"]

    def test_a_single_level_file_is_readable_at_all(self, tmp_path):
        """It sniffed as prose, so it never reached the shape check."""
        p = tmp_path / "lvl.csv"
        p.write_text("Depth (m),Temperature (deg C)\n0,15.1\n")
        result = sniff_format(str(p))
        assert result["readable"] and result["reader"] == "delimited"
        assert result["single_data_row"] is True
