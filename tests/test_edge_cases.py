"""Layouts seen in real accessions, each reproduced as a small synthetic file."""
import numpy as np
import pandas as pd
import pytest

from wod_etl.discovery import classify_accession, read_any, sniff_format
from wod_etl.mapping.generate_field_mapping import parse_header_block
from wod_etl.mapping.query_wod_spec import explain_column, match_variable


def read(path):
    casts, info = read_any(str(path), sniff_format(str(path)))
    return casts, info


class TestCsvLayouts:
    def test_title_row_and_key_value_rows_above_the_table(self, tmp_path):
        for sep in ("\n\n", "\n"):
            p = tmp_path / "t.csv"
            p.write_text("Cruise,KH-23-1\nLatitude,49-46.9858N\nLongitude,165-51.5694E\n"
                         "Date,2023-07-02" + sep + "Depth (m),Temperature (deg C)\n0,10.1\n10,9.8\n")
            (cast,), _ = read(p)
            assert list(cast["df"].columns) == ["Depth (m)", "Temperature (deg C)"]
            assert len(cast["df"]) == 2
            assert "Latitude: 49-46.9858N" in cast["header_block"]

    def test_a_units_row_is_folded_into_the_names(self, tmp_path):
        p = tmp_path / "u.csv"
        p.write_text("# LAT: 49.5\nDepth,Temperature,Oxygen\nm,degC,umol/kg\n0,10.1,250\n10,9.8,248\n")
        (cast,), info = read(p)
        assert list(cast["df"].columns) == ["Depth (m)", "Temperature (degC)", "Oxygen (umol/kg)"]
        assert cast["df"]["Oxygen (umol/kg)"].tolist() == [250, 248]

    def test_a_group_label_and_sentinel_heavy_rows(self, tmp_path):
        p = tmp_path / "g.csv"
        p.write_text("Phyto_C145,,,,Chlorophyll a (mg m-3),\n"
                     "Station,Date,Latitude,Longitude,20 µm,Total\n"
                     "A01,2023/6/21,62.16,-170.5,**NS,1.21\nA01,2023/6/21,62.16,-170.5,0.5,2.3\n")
        (cast,), _ = read(p)
        cols = list(cast["df"].columns)
        assert cols[-1] == "Total Chlorophyll a (mg m-3)" and match_variable(cols[-1]) == 11
        assert match_variable(cols[-2]) is None

    def test_a_cruise_wide_table_is_split_into_a_cast_per_station(self, tmp_path):
        p = tmp_path / "bottle.csv"
        p.write_text("Station,Date,Latitude,Longitude,Depth (m),Temperature (°C)\n"
                     "A01,2023/6/21,62.16,-170.5,0,1.0\nA01,2023/6/21,62.16,-170.5,10,0.5\n"
                     "A02,2023/6/22,62.58,-171.3,0,1.2\n")
        casts, _ = read(p)
        assert [len(c["df"]) for c in casts] == [2, 1]
        assert "Latitude: 62.58" in casts[1]["header_block"]
        assert "Station" not in casts[0]["df"].columns


class TestPositionsAndNames:
    @pytest.mark.parametrize("text, lat, lon", [
        ("Latitude: 49-46.9858N\nLongitude: 165-51.5694E", 49.7831, 165.8595),
        ("Latitude: 49°46.9858'S\nLongitude: 165°51.5694'W", -49.7831, -165.8595),
        ("LAT: 61 55.8 N\nLON: 172 16.2 W", 61.93, -172.27)])
    def test_degree_minute_positions_in_a_header(self, text, lat, lon):
        c = parse_header_block(text)["coordinates"]
        assert c["latitude"] == pytest.approx(lat, abs=1e-4)
        assert c["longitude"] == pytest.approx(lon, abs=1e-4)

    @pytest.mark.parametrize("name, code", [("経度(度表記)", 31), ("緯度(度表記)", 30),
                                            ("水温(℃)", 1), ("塩分", 2), ("水深(m)", 0)])
    def test_japanese_column_names(self, name, code):
        assert match_variable(name) == code

    def test_size_fractions_of_chlorophyll_are_not_chlorophyll(self):
        assert match_variable("20 µm Chlorophyll a") is None
        assert explain_column("20 µm Chlorophyll a")["variable"] == "size_fractionated_chlorophyll"

    def test_a_station_list_with_a_title_and_split_coordinates(self, tmp_path):
        from wod_etl.staging.station_list import read_station_list

        p = tmp_path / "latlon.csv"
        p.write_text('Cruise station list,,,,,,,,\n'
                     ',Station,"Latitude (deg, min, N/S)",,,"Longitude (deg, min, E/W)",,,'
                     'Date time (UT)\n1,a01,62,9.6,N,170,30.01,W,2023/6/21\n')
        st, info = read_station_list(p)
        assert st["a01"]["latitude"] == pytest.approx(62.16)
        assert st["a01"]["longitude"] == pytest.approx(-170.50017, abs=1e-5)
        assert info["time_zone"] == "UT"


class TestFormatsWithTheirOwnReader:
    def test_wod_csv(self, tmp_path):
        p = tmp_path / "x.CTD.csv"
        p.write_text("#----,\nCAST ,, ,,\nLatitude ,, -41.035,decimal degrees,,\n"
                     "Longitude ,, 173.249,decimal degrees,,\nYear ,, 2011,,,\nMonth ,, 3,,,\n"
                     "Day ,, 26,,,\nTime ,, 1320,hours minutes (NZST),,\n"
                     "Platform ,, 5423.,OCL code,KAHAROA,\nMETADATA,\n"
                     "VARIABLES ,Depth ,F,O,Temperatur,F,O,,\nUNITS ,m , , ,degrees C , , ,,\n"
                     "Prof-Flag , ,0, , ,0, ,,\n 1, 1,0, , 18.76,0, ,\n 2, 2,0, , 18.75,0, ,\n"
                     "END OF VARIABLES SECTION,\n")
        (cast,), info = read(p)
        assert list(cast["df"].columns) == ["Depth (m)", "Temperature (degrees C)"]
        assert {"Platform: 5423", "TIME_ZONE: NZST", "Date: 2011-03-26", "Time: 13:20"} \
            <= set(cast["header_block"].splitlines())

    def test_an_xctd_dat_with_unlabelled_columns(self, tmp_path):
        p = tmp_path / "XCTD1.dat"
        p.write_text("# XCTD YK17-10 20170516 1251 17-00.14N 143-29.97E 3\n"
                     "  0.0  -5.00  -5.000  0NN\n  1.0  28.09  34.941  000\n  2.0  28.08  34.95  000\n")
        (cast,), _ = read(p)
        assert cast["df"]["Temperature (deg C)"].isna().tolist() == [True, False, False]
        assert "Latitude: 17.002333333333333" in cast["header_block"]

    def test_a_tsurumi_seiki_xctd(self, tmp_path):
        p = tmp_path / "C145-X001"
        p.write_text("19082655 ,0001  ,20230624,031903,64-05.8454N,165-29.6438W,CT4,   2,1m ,"
                     + " " * 40 + ",74549,E,\r\n   0.00, 12.304,21.561,17.569,1477.16,1013.05,\r\n"
                     "   1.00,  8.755,22.571,20.402,1467.34,1015.71,\r\n")
        (cast,), _ = read(p)
        assert match_variable(cast["df"].columns[1]) == 1 and len(cast["df"]) == 2
        assert "TIME_ZONE: UTC" in cast["header_block"]

    def test_a_ruskin_workbook_gives_a_cast_per_downcast(self, tmp_path):
        p = tmp_path / "ctd.xlsx"
        with pd.ExcelWriter(p) as xw:
            pd.DataFrame([["Serial", "Model"], [232508, "RBRconcerto"]]).to_excel(
                xw, sheet_name="Metadata", header=False, index=False)
            pd.DataFrame([["Profile annotation", None, None, None],
                          ["Time 1", "Time 2", "Label", "Type"],
                          ["2024-09-11 04:10:00", "2024-09-11 04:10:02", None, "DOWN"],
                          ["2024-09-11 04:10:02", "2024-09-11 04:10:04", None, "UP"]]).to_excel(
                xw, sheet_name="Profile annotation", header=False, index=False)
            pd.DataFrame([["Data"] + [None] * 5,
                          ["Time", "Latitude", "Longitude", "Temperature", "Sea pressure", "Depth"],
                          ["2024-09-11 04:10:00", "84°12'25.39''", "173°33'42.74'' W", -1.5, 1.0, 1.0],
                          ["2024-09-11 04:10:01", "84°12'25.39''", "173°33'42.74'' W", -1.6, 2.0, 2.0],
                          ["2024-09-11 04:10:03", "84°12'25.39''", "173°33'42.74'' W", -1.6, 1.5, 1.5]]
                         ).to_excel(xw, sheet_name="St. 1", header=False, index=False)
        (cast,), _ = read(p)
        assert len(cast["df"]) == 2                      # the upcast sample is not in it
        assert "Latitude: 84.20705277777778" in cast["header_block"]
        assert "Sea pressure (dbar)" in cast["df"].columns


class TestTriage:
    def test_a_file_of_sound_speed_only_is_left_out(self, tmp_path):
        import netCDF4

        acc = tmp_path / "acc"
        acc.mkdir()
        with netCDF4.Dataset(acc / "svp.nc", "w") as ds:
            ds.createDimension("profile", 1)
            ds.createDimension("z", 5)
            for name, dims, units in (("time", ("profile",), "seconds since 1970-01-01"),
                                      ("lat", ("profile",), "degrees_north"),
                                      ("lon", ("profile",), "degrees_east"),
                                      ("depth", ("profile", "z"), "m"),
                                      ("speed_of_sound_in_sea_water", ("profile", "z"), "m/s")):
                v = ds.createVariable(name, "f8", dims)
                v.units = units
            ds["depth"][:] = np.arange(5.0).reshape(1, 5)
            ds["speed_of_sound_in_sea_water"][:] = np.full((1, 5), 1500.0)
        (f,) = classify_accession(str(acc))["files"]
        assert f["action"] == "exclude" and "sound velocity" in f["why"]

    def test_a_cast_exported_binned_and_unbinned_is_written_once(self, tmp_path):
        acc = tmp_path / "acc"
        acc.mkdir()
        line = "19082655 ,0001  ,20230624,031903,64-05.8454N,165-29.6438W,CT4,   1,1m ,"
        for name in ("C145-X001", "C145-X001all"):
            (acc / name).write_text(line + " " * 40 + ",\n   0.00, 12.3,21.5,17.5,\n")
        files = {f["path"]: f for f in classify_accession(str(acc))["files"]}
        assert files["C145-X001"]["include"] and not files["C145-X001all"]["include"]

    def test_a_word_document_is_documentation(self, tmp_path):
        import zipfile

        with zipfile.ZipFile(tmp_path / "notes.docx", "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
        assert sniff_format(str(tmp_path / "notes.docx"))["format_family"] == "word_document"


class TestPlanktonBiomass:
    def test_zooplankton_wet_weight_from_a_vertical_net(self, tmp_path):
        from wod_etl.staging.taxa_layout import describe_taxa_table

        p = tmp_path / "16-NORPAC" / "data-NORPAC.csv"
        p.parent.mkdir()
        p.write_text("Title,,,,,\nStation,Date,Latitude,Longitude,Depth (m),"
                     "Zooplankton wet weight (mg WW m-3)\nA01,2023/6/21,62.16,-170.5,42,674\n")
        t = describe_taxa_table(p)
        assert t["missing"] == []
        (m,) = t["measurements"]
        assert (m["biomass"], m["unit"], m["per_unit"]) == (-403, "mg", "m3")
        assert t["columns"]["upper_zero"] and t["columns"]["lower"] == "Depth (m)"

    def test_one_depth_without_a_vertical_net_is_not_a_range(self, tmp_path):
        from wod_etl.staging.taxa_layout import describe_taxa_table

        p = tmp_path / "hauls.csv"
        p.write_text("Station,Depth (m),Zooplankton wet weight (mg m-3)\nA01,42,674\n")
        assert any("upper and lower depth" in m for m in describe_taxa_table(p)["missing"])


class TestWhatTriageProposes:
    def classify(self, tmp_path, files):
        acc = tmp_path / "acc"
        acc.mkdir()
        for name, text in files.items():
            (acc / name).write_text(text)
        return {f["path"]: f for f in classify_accession(str(acc))["files"]}

    def test_a_readme_table_is_documentation(self, tmp_path):
        f = self.classify(tmp_path, {"CTD_readme.csv": "Column,Meaning\nTemp,temperature\n"})
        assert f["CTD_readme.csv"]["action"] == "document"

    def test_bottle_chemistry_without_a_depth_axis_is_osd(self, tmp_path):
        f = self.classify(tmp_path, {"surface.csv":
                                     "Sample,Latitude,Longitude,Nitrate (umol/l),Phosphate (umol/l)\n"
                                     "S1,60.1,-170.1,3.1,0.5\nS2,60.4,-170.3,2.9,0.4\n"})
        assert f["surface.csv"]["dataset"] == "OSD"

    def test_a_table_that_gives_no_position_is_proposed_out(self, tmp_path):
        f = self.classify(tmp_path, {"cast.csv": "Depth (m),Temperature (deg C)\n"
                                                 + "".join(f"{d},{10 - d / 10}\n" for d in range(20))})
        assert not f["cast.csv"]["include"] and "no latitude and longitude" in f["cast.csv"]["why"]

    def test_a_position_in_a_csv_preamble_locates_it(self, tmp_path):
        f = self.classify(tmp_path, {"cast.csv": "Latitude,49.5\nLongitude,-130.2\n\n"
                                                 "Depth (m),Temperature (deg C)\n"
                                                 + "".join(f"{d},{10 - d / 10}\n" for d in range(20))})
        assert f["cast.csv"]["include"]

    def test_a_file_with_nothing_wod_stores_is_proposed_out(self, tmp_path):
        f = self.classify(tmp_path, {"met.csv": "Latitude,Longitude,Wind speed (m/s),Wind direction\n"
                                                "60.1,-170.1,5.2,180\n60.2,-170.2,5.0,175\n"})
        assert not f["met.csv"]["include"]


class TestSurfaceData:
    def test_a_track_is_one_sur_cast_at_0_m_with_a_year_day(self):
        from wod_etl.orchestration.process_accession import _at_the_surface

        df = pd.DataFrame({"Time (UTC)": ["2023-06-11 12:00:00", "2023-06-11 18:00:00"],
                           "Latitude": [60.0, 60.5], "Longitude": [-170.0, -170.2],
                           "Temperature (deg C)": [5.0, 5.2]})
        out, header, note = _at_the_surface(df, "", "SUR", "leg1")
        assert out["Depth (m)"].tolist() == [0.0, 0.0]
        assert out["Julian year-day"].tolist() == [161.5, 161.75]
        assert "Time (UTC)" not in out.columns and out["Latitude"].tolist() == [60.0, 60.5]
        assert header.startswith("Latitude: 60.0") and "TIME_ZONE: UTC" in header
        assert "manual p.20" in note

    def test_one_underway_sample_is_at_0_m(self):
        from wod_etl.orchestration.process_accession import _at_the_surface

        out, header, _ = _at_the_surface(pd.DataFrame({"Nitrate (umol/l)": [3.0]}), "Station: A1",
                                         "OSD", "underway_sample_3")
        assert out["Depth (m)"].tolist() == [0.0] and header.startswith("Station: A1\nNote:")

    def test_a_profile_is_left_alone(self):
        from wod_etl.orchestration.process_accession import _at_the_surface

        df = pd.DataFrame({"Depth (m)": [0.0, 5.0], "Temperature (deg C)": [1.0, 2.0]})
        assert _at_the_surface(df, "h", "OSD", "x")[2] is None

    def test_surface_samples_split_by_sample_and_a_track_does_not(self, tmp_path):
        p = tmp_path / "samples.csv"
        p.write_text("Sample,Latitude,Longitude,Nitrate (umol/l)\nS1,60.1,-170.1,3.1\n"
                     "S2,60.4,-170.3,2.9\n")
        assert len(read(p)[0]) == 2
        p.write_text("Time,Latitude,Longitude,Temperature (deg C)\n2023-06-11 12:00,60.1,-170.1,3.1\n"
                     "2023-06-11 12:01,60.4,-170.3,2.9\n")
        assert len(read(p)[0]) == 1


class TestTablesAcrossFiles:
    def test_a_flattened_netcdf_is_a_cast_per_profile_and_upcasts_are_left_out(self, tmp_path):
        import netCDF4

        p = tmp_path / "erddap.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.cdm_data_type = "Profile"
            ds.cdm_profile_variables = "id,profile_direction,latitude,longitude"
            ds.createDimension("row", 6)
            ds.createDimension("id_strlen", 4)
            ds.createDimension("dir_strlen", 8)
            ids = ds.createVariable("id", "S1", ("row", "id_strlen"))
            ids[:] = np.array([list(x) for x in ["c1dn"] * 2 + ["c1up"] * 2 + ["c2dn"] * 2],
                              dtype="S1")
            dirs = ds.createVariable("profile_direction", "S1", ("row", "dir_strlen"))
            dirs[:] = np.array([list(x.ljust(8)) for x in ["downward"] * 2 + ["upward"] * 2
                                + ["downward"] * 2], dtype="S1")
            for name, units, values in (("latitude", "degrees_north", [44.0] * 4 + [45.0] * 2),
                                        ("longitude", "degrees_east", [-63.0] * 6),
                                        ("depth", "m", [1, 2, 2, 1, 1, 2]),
                                        ("TEMPS901", "degree Celsius", [5, 4, 4, 5, 6, 5])):
                v = ds.createVariable(name, "f8", ("row",))
                v.units = units
                v[:] = values
        casts, info = read(p)
        assert len(casts) == 2 and [len(c["df"]) for c in casts] == [2, 2]
        assert "latitude: 45.0" in casts[1]["header_block"]
        assert any("upcast" in w for w in info["warnings"])

    def test_two_names_for_one_variable_are_not_a_copy(self, tmp_path):
        import netCDF4

        acc = tmp_path / "acc"
        acc.mkdir()
        with netCDF4.Dataset(acc / "ctd.nc", "w") as ds:
            ds.createDimension("z", 12)
            for name, long_name, units in (("depth", "depth", "m"),
                                           ("TEMPP901", "Temperature of the water body", "degC"),
                                           ("TEMPS901", "Temperature of the water body", "degC")):
                v = ds.createVariable(name, "f8", ("z",))
                v.long_name, v.units = long_name, units
                v[:] = np.arange(12.0)
        (f,) = classify_accession(str(acc))["files"]
        assert not f.get("drop") and "temperature" in f["data"]

    def test_a_units_row_with_blanks_and_values_not_analyzed(self, tmp_path):
        acc = tmp_path / "acc"
        acc.mkdir()
        (acc / "h3.csv").write_text(
            "Table 1. Tritium in seawater,,,,,\nNo.,Sample ID,Depth,Latitude(N),Longitude (W),Tritium\n"
            ",,(m),,,Bq/L\n" + "".join(f"{i},a0{i}-0,{i * 10},62-09.60,170-30.01,Not analyzed\n"
                                       for i in range(1, 6)))
        (f,) = classify_accession(str(acc))["files"]
        assert f["columns"][2] == "Depth (m)" and f["columns"][5] == "Tritium (Bq/L)"
        assert not f["include"] and "Not analyzed" in f["why"]

    def test_casts_named_in_a_station_list_are_staged_with_its_positions(self, tmp_path):
        from wod_etl.orchestration.recipes import run_profile_recipe

        acc = tmp_path / "acc"
        (acc / "data").mkdir(parents=True)
        (acc / "CTD_readme.csv").write_text(
            "Cruise C145 CTD station list,,,,,,,,,,\n"
            ",Station,File name,\"Latitude (deg, min, N/S)\",,,\"Longitude (deg, min, E/W)\",,,"
            "Date time (UTC),SST (deg. C)\n"
            "1,a01,C145-001,62,9.6,N,170,30.01,W,2023/6/20 14:55,3.7\n"
            "2,a01,C145-002,62,34.8,N,171,20.46,W,2023/6/20 20:46,3.2\n")
        for n in (1, 2):
            (acc / "data" / f"C145-00{n}.asc").write_text(
                "       PrDM      T090C      Sal00\n"
                + "".join(f"{z:11.3f}{3 - z / 100:11.4f}{31.9:11.4f}\n" for z in range(2, 30)))
        files = {f["path"]: f for f in classify_accession(str(acc))["files"]}
        f = files["data/C145-002.asc"]
        assert files["CTD_readme.csv"]["action"] == "sidecar"
        assert f["action"] == "stage" and f["located_by"]["key"] == "File name"
        recipe = {"kind": f["located_by"]["kind"], "files": ["data/C145-002.asc"],
                  "station_list": "CTD_readme.csv", "station_column": "File name"}
        r = run_profile_recipe("ctd", recipe, acc, tmp_path / "out", "0000001", {})
        (staged,) = r["written"]
        text = open(staged).read()
        assert "62.58" in text and "2023-06-20" in text and "20:46" in text

    def test_two_tables_of_the_same_casts_are_combined(self, tmp_path):
        from wod_etl.staging.combine import combine_files

        acc = tmp_path / "acc"
        acc.mkdir()
        (acc / "bottle.csv").write_text(
            "Station,Date,Latitude,Longitude,Depth (m),Nitrate (umol/l)\n"
            "A01,2023/6/21,62.16,-170.5,0,1.0\nA01,2023/6/21,62.16,-170.5,10,2.0\n"
            "A02,2023/6/22,62.58,-171.3,0,1.5\nA02,2023/6/22,62.58,-171.3,10,2.5\n")
        (acc / "Data_Phytoplankton.csv").write_text(
            "Station,Date,Latitude,Longitude,Depth (m),Chlorophyll a (mg m-3)\n"
            "A01,2023/6/21,62.16,-170.5,0,0.9\nA01,2023/6/21,62.16,-170.5,13,2.3\n"
            "A02,2023/6/22,62.58,-171.3,0,0.4\n")
        files = {f["path"]: f for f in classify_accession(str(acc))["files"]}
        assert files["bottle.csv"]["combine"] == files["Data_Phytoplankton.csv"]["combine"]
        casts, _ = combine_files([acc / "bottle.csv", acc / "Data_Phytoplankton.csv"])
        df = casts[0]["df"]
        assert df["Depth (m)"].tolist() == [0, 10, 13]
        assert df["Chlorophyll a (mg m-3)"].tolist()[0] == 0.9

    def test_a_unit_folds_after_a_description_in_brackets(self, tmp_path):
        p = tmp_path / "met.csv"
        p.write_text("time (UTC),latitude,longitude,air temperature (instantaneous value)\n"
                     ",degree N,degree E,degree C\n2023/6/11 1:40,40.26,145.5,12.1\n"
                     "2023/6/11 1:41,40.27,145.6,12.0\n", encoding="utf-8")
        (cast,), _ = read(p)
        assert list(cast["df"].columns)[-1] == "air temperature (instantaneous value) (degree C)"
        assert len(cast["df"]) == 2 and match_variable(cast["df"].columns[-1]) == 26


class TestInstrumentFilesAndOutput:
    def test_a_sea_bird_ascii_export_has_a_header_row_and_an_upload_time(self, tmp_path):
        p = tmp_path / "017.cnv"
        p.write_text("* Sea-Bird SBE 9 Raw Data File:\n* NMEA Latitude = 37 59.49 NN\n"
                     "* NMEA Longitude = 146 48.69 E\n* System UpLoad Time = Feb 1 2002  12:21:49\n"
                     "*END*\n         Pr       T068      Sal00      Sal00\n"
                     + "".join(f"{z:11.3f}{11 - z / 100:11.4f}{33.1:11.4f}{33.1:11.4f}\n"
                               for z in range(1, 12)))
        (cast,), _ = read(p)
        assert list(cast["df"].columns) == ["Pr", "T068", "Sal00", "Sal00_2"]
        coords = parse_header_block(cast["header_block"])["coordinates"]
        assert (round(coords["latitude"], 4), coords["year"], coords["hour"]) == (37.9915, 2002, 12)
        assert "TIME_ZONE" not in cast["header_block"]          # a PC clock: zone not stated

    def test_a_mooring_file_with_fill_metadata_joins_the_mooring_its_name_starts_with(self):
        from wod_etl.staging.moorings import group_moorings

        entry = lambda path, platform: {"path": path, "mooring": {
            "platform": platform, "latitude": None if not platform else 57.89,
            "longitude": None if not platform else -168.87, "record": [0.0, 86400.0 * 30]}}
        groups = dict(group_moorings([entry("d/15bs4b_s37_0018m.cf.nc", "15bs4b"),
                                      entry("d/15bs4b_s56_0040m.cf.nc", None)]))
        assert [len(g) for g in groups.values()] == [2]

    def test_flags_named_in_ancillary_variables_pair_and_bad_levels_are_not_range_checked(
            self, tmp_path):
        import netCDF4

        from wod_etl.verify.verify_output import _flagged_bad

        p = tmp_path / "ph.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("z", 4)
            d = ds.createVariable("depth", "f8", ("z",))
            d.units, d[:] = "m", [1, 2, 3, 4]
            v = ds.createVariable("PHXXZZ01", "f8", ("z",))
            v.long_name, v.units, v.ancillary_variables = "pH of the water body", "1", "QPH"
            v[:] = [8.0, 8.1, -3.0, -3.1]
            q = ds.createVariable("QPH", "i4", ("z",))
            q.flag_values, q.flag_meanings = np.array([1, 4]), "correct erroneous"
            q[:] = [1, 1, 4, 4]
        (cast,), _ = read(p)
        assert "pH of the water body_QC" in cast["df"].columns
        with netCDF4.Dataset(tmp_path / "out.nc", "w") as ds:
            ds.createDimension("z_obs", 4)
            o = ds.createVariable("ph_qc_originator", "f4", ("z_obs",))
            o.flag_values, o.flag_meanings = np.array([1, 4], "f4"), "correct erroneous"
            o[:] = [1, 1, 4, 4]
            assert _flagged_bad(ds, "ph", 4).tolist() == [False, False, True, True]

    def test_a_long_cruise_name_never_pushes_the_date_out_of_a_cast_id(self):
        from wod_etl.writers.generate_netcdf_ragged import _cast_id

        cast = {"contract": {"character_data": {"originator_cruise_id": {
                    "value": "Atlantic Zone Monitoring Program Fall 2016"}},
                "extracted_header_metadata": {"1": {"value": "0304487"},
                                              "7": {"value": "HL07"}},
                "parsed_coordinates": {"year": 2016, "month": 9, "day": 16, "hour": 19,
                                       "minute": 56}}}
        cid = _cast_id(cast, 0)
        assert len(cid) <= 60 and cid.endswith("_stHL07_20160916T1956")

    def test_single_level_surface_samples_are_not_a_flat_depth_axis(self, tmp_path):
        from wod_etl.verify.verify_output import _check_depth_axis

        findings = []
        _check_depth_axis("f.nc", np.zeros(77), findings, "OSD", one_level_casts=True)
        assert findings == []
        _check_depth_axis("f.nc", np.zeros(77), findings, "OSD")
        assert [f["finding"] for f in findings] == ["flat_depth_axis"]


class TestSplittingAndReadingEdgeCases:
    def test_a_position_or_clock_on_every_row_is_one_cast(self, tmp_path):
        p = tmp_path / "ctd.csv"
        p.write_text("Latitude,Longitude,Depth (m),Temperature (C)\n"
                     + "".join(f"{41 + i / 1000},{141 + i / 1000},{i},{10 - i / 10}\n"
                               for i in range(5)))
        assert len(read(p)[0]) == 1
        p.write_text("Station,Date/Time (UTC),Latitude,Longitude,Depth (m),Temperature (C)\n"
                     + "".join(f"A1,2023-07-02 10:0{i},41,141,{i},10\n" for i in range(4)))
        assert len(read(p)[0]) == 1

    def test_each_station_keeps_its_own_position_over_the_preamble(self, tmp_path):
        p = tmp_path / "t.csv"
        p.write_text("Date,2023-07-01\nLatitude,40.0\nLongitude,140.0\n\n"
                     "Station,Date,Latitude,Longitude,Depth (m),Temperature (C)\n"
                     "A1,2023-07-02,41,141,0,10\nA1,2023-07-02,41,141,10,9\n"
                     "A2,2023-07-03,42,142,0,11\nA2,2023-07-03,42,142,10,10\n")
        a2 = parse_header_block(read(p)[0][1]["header_block"])["coordinates"]
        assert (a2["latitude"], a2["day"]) == (42.0, 3)

    def test_fill_down_rows_stay_with_their_station(self, tmp_path):
        p = tmp_path / "t.csv"
        p.write_text("Station,Latitude,Longitude,Depth (m),Temperature (C)\n"
                     "A1,41,141,0,10.1\n,,,10,9.8\n,,,20,9.5\nA2,42,142,0,11\n,,,10,10.5\n")
        assert [len(c["df"]) for c in read(p)[0]] == [3, 2]

    def test_a_sheet_title_above_the_header(self, tmp_path):
        p = tmp_path / "t.xlsx"
        pd.DataFrame([["CTD data cruise KH-23", None, None],
                      ["Station", "Depth (m)", "Temperature (C)"],
                      ["A1", 0, 10.1], ["A1", 10, 9.8]]).to_excel(p, header=False, index=False)
        (cast,), _ = read(p)
        assert list(cast["df"].columns) == ["Station", "Depth (m)", "Temperature (C)"]

    def test_netcdf_strings_and_integer_fills(self, tmp_path):
        import netCDF4

        p = tmp_path / "t.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("z", 3)
            ds.createVariable("name", str, ("z",))[:] = np.array(["a", "b", "c"], dtype=object)
            d = ds.createVariable("depth", "f8", ("z",))
            d.units, d[:] = "m", [0, 10, 20]
            q = ds.createVariable("count", "i4", ("z",))
            q.FillValue = -999
            q[:2] = [5, -999]                                  # q[2] left unwritten
        (cast,), info = read(p)
        assert cast["df"]["count"].isna().tolist() == [False, True, True]

    @pytest.mark.parametrize("lat, lon", [("lat_deg", "lon_deg"), ("GPS_Lat", "GPS_Lon")])
    def test_lat_lon_columns_with_suffixes_locate_a_table(self, lat, lon):
        from wod_etl.discovery.classify_dataset import _locates_itself

        assert _locates_itself([lat, lon, "Depth (m)"], "")

    def test_a_station_list_with_a_blank_row_under_its_title(self, tmp_path):
        from wod_etl.staging import read_station_list

        p = tmp_path / "s.csv"
        p.write_text("C145 station list,,,\n,,,\nStation,Latitude,Longitude,Bottom depth\n"
                     "1,41.5,140.1,3000\n")
        stations, info = read_station_list(p)
        assert not info.get("error") and stations["1"]["longitude"] == 140.1

    def test_a_track_stamped_with_z_is_converted_to_utc(self):
        from wod_etl.orchestration.process_accession import _at_the_surface

        df = pd.DataFrame({"Time": ["2019-07-01T10:00:00+09:00", "2019-07-01T16:00:00+09:00"],
                           "Latitude": [60.0, 60.5], "Longitude": [-170.0, -170.2],
                           "Temperature (deg C)": [5.0, 5.2]})
        _, header, _ = _at_the_surface(df, "", "SUR", "k")
        assert "Time: 01:00:00" in header and "TIME_ZONE: UTC" in header

    def test_depthless_samples_not_named_surface_are_proposed_out(self, tmp_path):
        acc = tmp_path / "acc"
        acc.mkdir()
        (acc / "nutrients.csv").write_text("Station,Latitude,Longitude,Nitrate (umol/l)\n"
                                           "S1,60.1,-170.1,3.1\nS2,60.4,-170.3,2.9\n")
        (f,) = classify_accession(str(acc))["files"]
        assert f["dataset"] == "OSD" and not f["include"] and "sample's depth" in f["why"]

    def test_triage_fingerprint_covers_what_triage_imports(self):
        from wod_etl.orchestration.pipeline import TRIAGE_CODE

        for module in ("wod_etl/terms.py", "wod_etl/time_zones.py", "wod_etl/staging/combine.py",
                       "wod_etl/staging/parse.py"):
            assert any(module == c or module.startswith(c + "/") for c in TRIAGE_CODE)

    def test_wod_csv_decimal_hours_never_make_minute_60(self, tmp_path):
        p = tmp_path / "w.csv"
        p.write_text("#----\nCAST,,1,,\nLatitude,,-44.0,decimal degrees,\n"
                     "Longitude,,172.0,decimal degrees,\n"
                     "Year,,2012,,\nMonth,,6,,\nDay,,5,,\nTime,,19.995,decimal hours (UT),\n"
                     "VARIABLES ,Depth     ,F,O,Temperatur,F,O,\nUNITS     ,m         , , ,degrees C , , ,\n"
                     "1,0,0,,10.0,0,,\nEND OF VARIABLES SECTION,\n")
        (cast,), _ = read(p)
        assert "Time: 19:59:42" in cast["header_block"]

    def test_key_value_rows_are_not_group_labels(self, tmp_path):
        p = tmp_path / "t.csv"
        p.write_text("Cruise,,KH-23-1\nShip,,Hakuho maru\n"
                     "Depth (m),Temperature (C),Salinity,Oxygen (umol/kg)\n0,10,34,250\n10,9,34,248\n")
        (cast,), _ = read(p)
        assert list(cast["df"].columns) == ["Depth (m)", "Temperature (C)", "Salinity",
                                            "Oxygen (umol/kg)"]


class TestManualConventions:
    def test_a_cast_recorded_deepest_first_is_reversed(self):
        from wod_etl.orchestration.process_accession import _shallowest_first

        df = pd.DataFrame({"Depth (m)": [30.0, 20.0, 10.0, 0.0], "Temperature (C)": [4, 5, 6, 7]})
        out, note = _shallowest_first(df)
        assert out["Depth (m)"].tolist() == [0.0, 10.0, 20.0, 30.0] and "p.52" in note
        both_ways = pd.DataFrame({"Depth (m)": [0.0, 10, 20, 10, 0], "T": [1, 2, 3, 2, 1]})
        assert _shallowest_first(both_ways)[1] is None

    def test_a_height_axis_measured_upward_is_depth(self, tmp_path):
        import netCDF4

        p = tmp_path / "up.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("z", 5)
            z = ds.createVariable("z", "f8", ("z",))
            z.units, z.positive, z.standard_name = "m", "up", "height"
            z[:] = -np.arange(1.0, 6.0)
            t = ds.createVariable("TEMP", "f8", ("z",))
            t.units, t[:] = "degree_Celsius", np.linspace(10, 5, 5)
        (cast,), info = read(p)
        assert cast["df"]["z (m)"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_a_longitude_beyond_180_is_brought_into_range(self):
        coords = parse_header_block("Latitude: 41.5\nLongitude: 200.5\n")["coordinates"]
        assert coords["longitude"] == -159.5

    def test_an_xbt_probe_model_and_fall_rate(self, offline_code_lists):
        from helpers import cache_code_list
        from wod_etl.mapping.map_metadata import map_metadata

        cache_code_list(offline_code_lists, "s_29_probe_type",
                        "code_name,probe_type\nunit,OCL_code\n2,XBT\n4,CTD\n")
        cache_code_list(offline_code_lists, "v_5_instrument",
                        "code_name,Instrument\nunits,OCL code\n201,XBT: T7 (UNKNOWN BRAND)\n"
                        "207,XBT: T7 (SIPPICAN)\n")
        df = pd.DataFrame({"Depth (m)": [0.0, 1.0], "Temperature (deg C)": [20.0, 19.0]})
        meta = map_metadata("Probe type: T-7\nFall rate equation: Hanawa et al. (1995)\n",
                            list(df.columns), df)
        assert meta["secondary_headers"]["29"]["value"] == "2"
        assert meta["secondary_headers"]["33"]["value"] == 1
        temp = meta["variable_specific_headers"]["Temperature (deg C)"]["headers"]
        assert temp["5"]["value"] == "201"

    def test_a_depth_unit_other_than_metres_is_secondary_header_95(self, offline_code_lists):
        from helpers import cache_code_list
        from wod_etl.mapping.map_metadata import map_metadata

        cache_code_list(offline_code_lists, "s_95_depth_unit",
                        "code_name,Depth unit\nunits,NODC code\n083,foot\n086,fathom\n")
        df = pd.DataFrame({"Depth (ft)": [0.0, 10.0], "Temperature (deg C)": [20.0, 19.0]})
        meta = map_metadata("", list(df.columns), df)
        assert meta["secondary_headers"]["95"]["value"] == "083"
        assert "8" not in meta["variable_specific_headers"]["Depth (ft)"]["headers"]

    @pytest.mark.parametrize("calendar, days, date", [
        ("proleptic_gregorian", 8582.25, "2023-07-01"), ("noleap", 365 * 23 + 180.5, "2023-06-30")])
    def test_netcdf_calendars(self, tmp_path, calendar, days, date):
        import netCDF4

        p = tmp_path / "cal.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("depth", 3)
            d = ds.createVariable("depth", "f8", ("depth",))
            d.units, d[:] = "m", [0.0, 1.0, 2.0]
            ds.createVariable("temperature", "f8", ("depth",))[:] = [5.0, 4.9, 4.8]
            t = ds.createVariable("time", "f8")
            t.units, t.calendar = "days since 2000-01-01", calendar
            t[...] = days
        (cast,), _ = read(p)
        assert f"Date: {date}" in cast["header_block"]
