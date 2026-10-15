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


# ------------------------------------------------ scored exclusions, per-file triage
class TestExclusionsAreScoredNotOrdered:
    """First-match-wins labelled fish catch as plankton because the plankton rule came first."""

    def test_camel_case_file_names_are_read(self):
        """`\\btrawl\\b` cannot match inside 'TrawlInfo'."""
        v = classify_dataset(path="0-data/SHU1505_TrawlInfo.csv",
                             columns=["Cruise", "Station No.", "Long", "Lat",
                                      "towing depgth (center)", "total weight (kg)"])
        assert v["exclusion"]["reason"] == "fisheries catch data"
        assert v["exclusion"]["belongs_in_wod"] is False

    def test_a_species_column_does_not_make_trawl_catch_plankton(self):
        v = classify_dataset(path="0-data/U2015-015_TrawlSamples.csv",
                             columns=["Cruise", "Station", "Family", "Genus", "Species",
                                      "Number", "Weight (g)"])
        assert v["exclusion"]["reason"] == "fisheries catch data"
        assert v["exclusion"]["alternatives"][0]["reason"] == "plankton biomass or taxonomy"

    def test_fish_lengths_are_catch(self):
        v = classify_dataset(path="x/lengths.csv",
                             columns=["Station", "Species", "Fork Length (mm)"])
        assert v["exclusion"]["reason"] == "fisheries catch data"

    def test_a_species_column_alone_is_ambiguous_not_guessed(self):
        v = classify_dataset(path="0-data/PLsamples.csv",
                             columns=["Station No.", "Date", "Species", "number"])
        assert "ambiguous" in v["exclusion"]["reason"]
        assert v["exclusion"]["belongs_in_wod"] is None
        assert v["exclusion"]["confidence"] == "low"

    @pytest.mark.parametrize("path", ["0-data/U2015-015_2mRingNet.csv",
                                      "0-data/U2015-015_NORPAC.csv",
                                      "5-plankton/biomass.csv"])
    def test_plankton_nets_are_plankton(self, path):
        v = classify_dataset(path=path)
        assert v["exclusion"]["reason"] == "plankton biomass or taxonomy"
        assert v["exclusion"]["belongs_in_wod"] is True
        assert "process_taxa" in v["exclusion"]["route"]


class TestSidecarsAndLongFormat:
    def test_a_station_list_is_a_sidecar(self):
        v = classify_dataset(path="0-data/SHU1505_StationInfo.csv",
                             columns=["Cruise", "Station No.", "Date", "Year", "Long", "Lat",
                                      "Observation items"])
        assert v["exclusion"]["kind"] == "sidecar"
        assert "read_station_list" in v["exclusion"]["route"]

    def test_a_cast_with_coordinates_and_data_is_not_a_sidecar(self):
        v = classify_dataset(path="x/track.csv",
                             columns=["Station", "Lat", "Lon", "Temperature (deg C)"])
        assert v["exclusion"] is None

    def test_long_format_is_detected_and_pivot_named(self):
        v = classify_dataset(path="0-data/U2015-015_CTD_Temp.csv",
                             columns=["Cruise", "StNo", "ObsItem", "parm", "dep", "value"])
        assert v["long_format"]["variable_column"] == "parm"
        assert v["long_format"]["key_columns"] == ["StNo", "dep"]
        assert "pivot_long" in v["note"]

    def test_no_evidence_either_way_is_undecided_not_profile(self):
        assert classify_dataset(path="about/0303928_lonlat.txt",
                                columns=["140", "10"])["is_wod_profile_data"] is None
        assert classify_dataset(path="x/y.csv",
                                columns=["Depth (m)", "A"])["is_wod_profile_data"] is True


class TestPerFileTriage:
    @pytest.fixture
    def acc(self, tmp_path):
        """One NCEI 0-data folder holding everything, as NCEI archives a submission."""
        root = tmp_path / "0303928"
        (root / "about").mkdir(parents=True)
        (root / "data" / "0-data").mkdir(parents=True)
        (root / "NCEI-Readme.txt").write_text("This is the standard bundle for an NCEI AIP.\n")
        (root / "about" / "0303928_lonlat.txt").write_text("140 10\n150 10\n150 25\n140 25\n")
        d = root / "data" / "0-data"
        for var, name in (("Temperature", "Temp"), ("Salinity", "Salinity")):
            rows = "\n".join(f"SHU1505,1,CTD,{var},{z}.0,{20 - z / 10}" for z in range(5, 25))
            (d / f"U2015-015_CTD_{name}.csv").write_text(
                f"Cruise,StNo,ObsItem,parm,depth,value\n{rows}\n")
        (d / "SHU1505_StationInfo.csv").write_text(
            "Cruise,Station No.,Date,Year,Long,Lat,Observation items\n"
            "SHU1505,1,Nov. 09,2015,142°18’E,21°00’N,\"CTD, trawl\"\n"
            "SHU1505,2,Nov. 10,2015,142°24’E,20°30’N,\"CTD, NORPAC, PL\"\n")
        (d / "SHU1505_TrawlInfo.csv").write_text(
            "Cruise,Station No.,Date,Long,Lat,total weight (kg)\n"
            "SHU1505,1,Nov. 09,142.3,21.0,1.7\nSHU1505,3,Nov. 10,142.5,20.0,1.3\n")
        return root

    def test_every_file_gets_its_own_action(self, acc):
        r = classify_accession(str(acc))
        action = {f["path"]: f["action"] for f in r["files"]}
        assert action["NCEI-Readme.txt"] == "document"
        assert action["about/0303928_lonlat.txt"] == "document"
        assert action["data/0-data/SHU1505_StationInfo.csv"] == "sidecar"
        assert action["data/0-data/SHU1505_TrawlInfo.csv"] == "exclude"
        assert action["data/0-data/U2015-015_CTD_Temp.csv"] == "stage"
        assert r["stage_files"] == ["data/0-data/U2015-015_CTD_Salinity.csv",
                                    "data/0-data/U2015-015_CTD_Temp.csv"]

    def test_long_format_one_file_per_variable_is_a_shape(self, acc):
        shape = classify_accession(str(acc))["cross_file_shapes"]["data/0-data"]
        assert shape["shape"] == "long format, one file per variable"
        assert shape["key_columns"] == ["StNo", "depth"]
        assert "pivot_long" in shape["action"]

    def test_the_folder_summary_says_it_is_mixed(self, acc):
        v = classify_accession(str(acc))["folders"]["data/0-data"]["verdict"]
        assert v["action"] == "stage"
        assert "per-file verdicts" in v["reason"]

    def test_a_long_format_file_fails_with_the_fix_not_a_sidecar_skip(self, acc, tmp_path):
        from wod_etl.orchestration import process_accession

        res = process_accession(str(acc / "data" / "0-data"), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_long", resume=False)
        failed = {f["key"]: f for f in res["failures"]}
        assert "pivot_long" in failed["U2015-015_CTD_Temp.csv"]["error"]


class TestBinaryFilesAreOpenedNotAskedAbout:
    """A binary file has no text head. Triage must look inside it, or it asks a person what
    every NetCDF file and every picture in the accession is."""

    @staticmethod
    def _triage(root):
        return {f["path"]: f for f in classify_accession(str(root))["files"]}

    def test_a_moored_instruments_record_is_mrb_data_not_a_question(self, tmp_path):
        from helpers import write_mooring_nc

        write_mooring_nc(tmp_path / "18bs8a_s39_0051m.cf.nc")
        # An ADCP's own thermistor beside its roll and heading: plain "degrees" is not a
        # position, so the record does not "move".
        write_mooring_nc(tmp_path / "18bsp8a_wcp_scal.cf.nc", instrument="RDI ADCP",
                         variables=(("T_20", "TEMPERATURE (C)", "C"),
                                    ("Hdg_1215", "INST Heading", "degrees")))
        # A level of the mooring's MRB profiles, combined with its other instruments by a
        # mooring recipe: staged, not left out.
        for name, f in self._triage(tmp_path).items():
            assert f["action"] == "stage", (name, f["reason"])
            assert (f["dataset"], f["include"]) == ("MRB", True)
            assert f["mooring"]["variables"] == ["TEMPERATURE (C)"]   # not the heading
        f = self._triage(tmp_path)["18bs8a_s39_0051m.cf.nc"]
        assert f["time_series"]["instrument"] == "Seabird SBE-39" and f["time_series"]["days"] == 30.0
        m = f["mooring"]
        assert (m["platform"], m["pressure"]) == ("18bs8a", False)
        assert m["depth"] == pytest.approx(19.8, abs=0.1)              # its nominal 20 dbar
        assert (m["latitude"], m["longitude"]) == pytest.approx((62.19, -174.69))
        assert "one a day of daily means" in f["why"]

    def test_an_adcp_velocity_file_is_excluded_as_currents(self, tmp_path):
        from helpers import write_mooring_nc

        write_mooring_nc(tmp_path / "vel.nc", levels=13, instrument="RDI ADCP",
                         variables=(("u_1205", "Eastward Velocity", "cm/s"),
                                    ("v_1206", "Northward Velocity", "cm/s")))
        f = self._triage(tmp_path)["vel.nc"]
        assert f["action"] == "exclude" and f["exclusion"] == "current velocity"

    def test_a_cast_recorded_against_time_is_not_a_mooring(self, tmp_path):
        from helpers import write_mooring_nc

        write_mooring_nc(tmp_path / "cast.nc", days=0.5, mooring="")
        f = self._triage(tmp_path)["cast.nc"]
        assert f["time_series"] is None and f["exclusion"] != "moored instrument time series"

    @staticmethod
    def _track(path, *columns):
        n = 48
        with netCDF4.Dataset(path, "w") as ds:
            ds.createDimension("time", n)
            for name, units, values in (("time", "hours since 2020-01-01", np.arange(n)),
                                        ("lat", "degrees_north", np.linspace(60, 61, n)),
                                        ("lon", "degrees_east", np.linspace(-170, -169, n)),
                                        ("TEMP", "degC", np.full(n, 5.0))) + columns:
                v = ds.createVariable(name, "f8", ("time",))
                v.units = units
                v[:] = values

    def test_a_track_is_an_underway_record_not_a_mooring(self, tmp_path):
        self._track(tmp_path / "track.nc")
        f = self._triage(tmp_path)["track.nc"]
        assert f["time_series"]["moving"] is True and f["action"] == "exclude"
        assert f["exclusion"] == "underway record along a track" and "SUR" in f["why"]

    def test_a_glider_is_not_an_underway_record(self, tmp_path):
        """Moving, with its depth changing along the path: its dives are profiles."""
        self._track(tmp_path / "glider.nc", ("PRES", "dbar", np.abs(np.sin(np.arange(48))) * 500))
        f = self._triage(tmp_path)["glider.nc"]
        assert f["time_series"] is None and f["exclusion"] != "underway record along a track"

    def test_pictures_and_ncei_maps_are_documents(self, tmp_path):
        from helpers import JPEG_HEAD

        (tmp_path / "about").mkdir()
        (tmp_path / "about" / "0302602_map.jpg").write_bytes(JPEG_HEAD + b"\x00" * 64)
        (tmp_path / "gear.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        assert sniff_format(str(tmp_path / "gear.png"))["format_family"] == "image"
        files = self._triage(tmp_path)
        assert {p: f["action"] for p, f in files.items()} == {
            "about/0302602_map.jpg": "document", "gear.png": "document"}
        assert files["about/0302602_map.jpg"]["ncei_bundle"].startswith("NCEI's map")


class TestDryRunReadsLocalFiles:
    """It classified by file NAME, so a staged folder of CTD casts came back dataset null."""

    def test_a_staged_cast_is_classified_from_its_header(self, tmp_path):
        d = tmp_path / "staged"
        d.mkdir()
        (d / "SHU1505_st01.csv").write_text(
            "# Instrument: SBE CTD/Rosette system\nLAT: 21.0\nLON: 142.3\nDATE: 2015-11-09\n"
            "Depth (m),Temperature (deg C)\n" + "\n".join(f"{z},{20 - z / 100}"
                                                          for z in range(5, 60)) + "\n")
        res = process_accession(str(d), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_dry_local",
                                resume=False, dry_run=True)
        (item,) = res["would_process"]
        assert item["dataset_type"] == "CTD"
        assert item["decided_by"].startswith("file head")

    def test_long_format_and_ncei_files_are_named_in_the_dry_run(self, tmp_path):
        d = tmp_path / "acc"
        (d / "about").mkdir(parents=True)
        (d / "NCEI-Readme.txt").write_text("This is the standard bundle.\n")
        (d / "about" / "0303928_lonlat.txt").write_text("140 10\n150 10\n150 25\n140 25\n")
        rows = "\n".join(f"C,1,Temperature,{z},{20 - z / 10}" for z in range(5, 30))
        (d / "ctd_long.csv").write_text(f"Cruise,StNo,parm,depth,value\n{rows}\n")
        res = process_accession(str(d), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_dry_long",
                                resume=False, dry_run=True)
        assert [s["key"] for s in res["needs_staging"]] == ["ctd_long.csv"]
        skipped = {s["key"]: s["reason"] for s in res["would_skip"]}
        assert "NCEI bundle" in skipped["NCEI-Readme.txt"]
        assert "NCEI bundle" in skipped["about/0303928_lonlat.txt"]


class TestTriageSaysWhatEachFileIsAndWhy:
    """What a reviewer reads in decisions.toml: data, include, dataset, why."""

    @staticmethod
    def _triage(root):
        return {f["path"]: f for f in classify_accession(str(root))["files"]}

    @staticmethod
    def _nc(path, globals_=None, container=None, variables=(), dims=(("profile", 1),
                                                                     ("z", 12))):
        import netCDF4

        with netCDF4.Dataset(path, "w") as ds:
            for k, v in (globals_ or {}).items():
                ds.setncattr(k, v)
            for name, size in dims:
                ds.createDimension(name, size)
            if container:
                c = ds.createVariable("instrument", "i4", ())
                for k, v in container.items():
                    c.setncattr(k, v)
            for name, var_dims, attrs, values in variables:
                v = ds.createVariable(name, "f8", var_dims)
                for k, a in attrs.items():
                    v.setncattr(k, a)
                v[:] = values

    def _profile_vars(self, salinity=None):
        z = np.arange(12, dtype=float)
        t = (20 - z / 10).reshape(1, 12)
        s = (34 + z / 100).reshape(1, 12) if salinity is None else salinity
        return [("time", ("profile",), {"units": "seconds since 1970-01-01"}, [1.7e9]),
                ("lat", ("profile",), {"units": "degrees_north"}, [27.7]),
                ("lon", ("profile",), {"units": "degrees_east"}, [-96.1]),
                ("depth", ("profile", "z"), {"units": "m", "standard_name": "depth"},
                 z.reshape(1, 12)),
                ("temperature", ("profile", "z"), {"units": "degree_C",
                                                   "long_name": "temperature"}, t),
                ("salinity", ("profile", "z"), {"units": "1e-3",
                                                "long_name": "salinity"}, s)]

    def test_a_sound_velocity_and_temperature_probe_is_mbt_and_svp_is_no_drifter(self,
                                                                              tmp_path):
        self._nc(tmp_path / "20250201043830_BL.nc", {"title": "SVP_MVP profile"},
                 {"long_name": "SVP", "make_model": "MVP",
                  "serial_number": "AML Oceanographic MVP30-350 with SVP&T"},
                 self._profile_vars())
        f = self._triage(tmp_path)["20250201043830_BL.nc"]
        assert (f["include"], f["dataset"]) == (True, "MBT")
        assert f["data"] == "MBT profiles: temperature"
        assert "salinity is computed from sound speed" in f["why"] and f["drop"] == ["salinity"]

    def test_a_variable_that_copies_another_is_proposed_as_dropped(self, tmp_path):
        z = np.arange(12, dtype=float)
        self._nc(tmp_path / "ctd_ship.nc", {"title": "CTD profiles"}, None,
                 self._profile_vars(salinity=(20 - z / 10).reshape(1, 12)))
        f = self._triage(tmp_path)["ctd_ship.nc"]
        assert f["drop"] == ["salinity"] and f["data"] == "CTD profiles: temperature"
        assert "a copy, not a measurement" in f["why"]

    def test_seabed_sediment_is_left_out_as_not_water_column(self, tmp_path):
        self._nc(tmp_path / "LeConte_2017_seabedsedimentsizes.nc",
                 {"title": "seabed sediment-size distributions"}, None,
                 [("lat", ("n",), {"units": "degrees_north"}, [56.8, 56.9]),
                  ("lon", ("n",), {"units": "degrees_east"}, [-132.4, -132.3]),
                  ("d50", ("n",), {"long_name": "median grain size", "units": "phi"},
                   [1.0, 2.0])], dims=(("n", 2),))
        f = self._triage(tmp_path)["LeConte_2017_seabedsedimentsizes.nc"]
        assert (f["action"], f["exclusion"]) == ("exclude", "seabed sediment")
        assert "WOD stores no seabed or sediment property" in f["why"]

    def test_an_adcp_named_by_its_attribute_is_currents_though_it_logs_temperature(self,
                                                                                  tmp_path):
        n = 48
        self._nc(tmp_path / "workhorse300_upward_SN15645.nc",
                 {"instrumentType": "adcp", "manufacturer": "teledyne rdi"}, None,
                 [("time", ("time",), {"units": "hours since 2018-09-01"}, np.arange(n)),
                  ("distance", ("distance",), {"units": "m"}, np.arange(5.0)),
                  ("u", ("distance", "time"), {"long_name": "Eastward velocity",
                                              "units": "m s-1"}, np.zeros((5, n))),
                  ("t", ("time",), {"long_name": "Temperature", "units": "degree_C"},
                   np.full(n, 5.0))], dims=(("time", n), ("distance", 5)))
        f = self._triage(tmp_path)["workhorse300_upward_SN15645.nc"]
        assert (f["action"], f["exclusion"]) == ("exclude", "current velocity")

    def test_prose_is_documentation_and_a_data_file_under_a_long_header_is_not(self,
                                                                                tmp_path):
        (tmp_path / "mooring_description.txt").write_text(
            "# Downstream deep mooring\n\n## Instrument layout\n\nAlso see the diagram.\n"
            "| Type | Pos (m) | Serial |\n| RBR Solo | 45 | 100028 |\n")
        long_header = "".join(f"# comment line {i} about the cruise and its people\n"
                              for i in range(400))
        (tmp_path / "cast.txt").write_text(
            long_header + "Depth Temperature Salinity\n"
            + "".join(f"{z} {20 - z / 10:.2f} 34.5\n" for z in range(300)))
        files = self._triage(tmp_path)
        assert files["mooring_description.txt"]["action"] == "document"
        assert files["cast.txt"]["action"] != "document"

    def test_an_ncei_submission_report_is_a_bundle_file(self, tmp_path):
        (tmp_path / "H1CMAX-report.txt").write_text(
            "The submitter provided the following profile information:\nName: E. Eidam\n")
        f = self._triage(tmp_path)["H1CMAX-report.txt"]
        assert f["action"] == "document" and f["data"] == "NCEI submission report"
