"""Staging: the parsing and reshaping that lets an unreadable file be read."""
import datetime as dt

import pytest

from wod_etl.staging import (clean_sentinels, combine_levels, combine_on_key,
                             fold_unit_row, group_rows, parse_coordinate,
                             parse_timestamp, pressure_to_depth, read_table,
                             write_staged_cast)


class TestParseCoordinate:
    @pytest.mark.parametrize("value,expected", [
        (62.16, 62.16),
        ("62.16", 62.16),
        (-170.5, -170.5),
        ("33.5 S", -33.5),
        ("170 30.01 W", -170.5001666),
        ("62 9.6 N", 62.16),
        ("64-05.8454N", 64.0974233),
        ("62 09 36 N", 62.16),
        ("168-29.29 W", -168.4881666),
    ])
    def test_forms(self, value, expected):
        assert parse_coordinate(value) == pytest.approx(expected, abs=1e-6)

    def test_separate_columns(self):
        assert parse_coordinate(62, 9.6, "N") == pytest.approx(62.16)
        assert parse_coordinate(170, 30.01, "W") == pytest.approx(-170.5001666, abs=1e-6)

    def test_hemisphere_letter_beats_minus_sign(self):
        """Station lists write both; the letter is what the originator meant."""
        assert parse_coordinate("-170 30.01 W") == pytest.approx(-170.5001666, abs=1e-6)
        assert parse_coordinate("-62 9.6 N") == pytest.approx(62.16)

    @pytest.mark.parametrize("bad", [None, "", "   ", "not a position", "N/A"])
    def test_unreadable_returns_none(self, bad):
        assert parse_coordinate(bad) is None


class TestParseTimestamp:
    @pytest.mark.parametrize("value,expected", [
        ("2023/6/21", dt.datetime(2023, 6, 21)),
        ("2023-06-21 14:55", dt.datetime(2023, 6, 21, 14, 55)),
        ("20230624", dt.datetime(2023, 6, 24)),
    ])
    def test_forms(self, value, expected):
        assert parse_timestamp(value) == expected

    def test_separate_date_and_time_columns(self):
        assert parse_timestamp("20230624", "031903") == dt.datetime(2023, 6, 24, 3, 19, 3)

    def test_unreadable_returns_none(self):
        assert parse_timestamp("last Tuesday") is None


class TestPressureToDepth:
    def test_shallow_cast_is_close_to_pressure(self):
        assert pressure_to_depth(100.0, 62.16) == pytest.approx(99.02, abs=0.05)

    def test_latitude_dependent(self):
        """Gravity varies with latitude, so the same pressure is a different depth."""
        assert pressure_to_depth(1000.0, 0.0) != pressure_to_depth(1000.0, 70.0)

    def test_zero_pressure_is_zero_depth(self):
        assert pressure_to_depth(0.0, 45.0) == pytest.approx(0.0)


class TestReadTable:
    def test_drops_interleaved_blank_rows(self, tmp_path):
        """A file written with \\r\\r\\n has an empty row between every real one."""
        p = tmp_path / "double.csv"
        p.write_text("Title,,\n\nStation,Depth,Temp\n\nA01,0,3.2\n\nA01,10,3.0\n")
        cols, rows, info = read_table(p, header_row=1)
        assert cols == ["Station", "Depth", "Temp"]
        assert len(rows) == 2
        assert info["blank_rows_dropped"] == 3

    def test_falls_back_through_encodings(self, tmp_path):
        p = tmp_path / "cp932.csv"
        p.write_bytes("Date,経度,緯度\n2023/6/16,168.0,51.1\n".encode("cp932"))
        cols, rows, info = read_table(p)
        assert info["encoding"] == "cp932"
        assert len(cols) == 3 and len(rows) == 1

    def test_reports_duplicate_columns(self, tmp_path):
        p = tmp_path / "dupe.csv"
        p.write_text("Depth,Temp,Temp\n0,3.2,3.3\n")
        _, _, info = read_table(p)
        assert info["duplicate_columns"] == ["Temp"]

    def test_header_row_past_end_is_an_error_not_a_crash(self, tmp_path):
        p = tmp_path / "short.csv"
        p.write_text("a,b\n")
        cols, rows, info = read_table(p, header_row=9)
        assert cols == [] and "error" in info


class TestCleanSentinels:
    def test_replaces_and_counts(self):
        cols = ["Depth", "NO3", "CDOM"]
        rows = [["0", "*DL", "***NA"], ["10", "2.8", "***NA"]]
        out, counts = clean_sentinels(rows, cols)
        assert out == [["0", "", ""], ["10", "2.8", ""]]
        assert counts["NO3 :: *DL"] == 1
        assert counts["CDOM :: ***NA"] == 2

    def test_below_detection_is_counted_not_substituted(self):
        """WOD has no below-detection flag, so the value is blanked and counted."""
        out, counts = clean_sentinels([["*DL"]], ["NO3"])
        assert out == [[""]]
        assert counts["NO3 :: *DL"] == 1

    def test_extra_codes(self):
        out, counts = clean_sentinels([["<LOD"]], ["NH4"], extra={"<LOD": "below detection"})
        assert out == [[""]] and counts["NH4 :: <LOD"] == 1


class TestGroupRows:
    def test_groups_in_file_order(self):
        cols = ["Station", "Depth"]
        rows = [["A01", "0"], ["A01", "10"], ["A02", "0"]]
        g = group_rows(rows, cols, ["Station"])
        assert list(g) == [("A01",), ("A02",)]
        assert len(g[("A01",)]) == 2

    def test_composite_key_separates_repeat_occupations(self):
        cols = ["Station", "Date", "Depth"]
        rows = [["a03", "2023/6/21", "0"], ["a03", "2023/6/22", "0"]]
        assert len(group_rows(rows, cols, ["Station", "Date"])) == 2

    def test_blank_key_collects_under_empty_tuple(self):
        g = group_rows([["", "x"]], ["Station", "V"], ["Station"])
        assert () in g

    def test_missing_key_column_raises(self):
        with pytest.raises(KeyError):
            group_rows([["a"]], ["Station"], ["Cast"])


class TestFoldUnitRow:
    """Fold a units row into the column names, where unit detection looks for it."""

    def test_folds_units_into_names(self):
        cols = ["time (UTC)", "latitude", "water temperature", "chlorophyll"]
        units = ["", "degree N", "degree C", "ppb"]
        assert fold_unit_row(cols, units) == [
            "time (UTC)", "latitude (degree N)",
            "water temperature (degree C)", "chlorophyll (ppb)"]

    def test_makes_a_conversion_detectable(self):
        """Before folding, the mg/L unit is invisible."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("DO", 3) == "none"
        folded = fold_unit_row(["DO"], ["mg/L"])[0]
        assert _detect_unit_conversion(folded, 3) == "mg_per_l_to_umol_per_kg"

    def test_leaves_existing_bracketed_units_alone(self):
        assert fold_unit_row(["Depth (m)"], ["metres"]) == ["Depth (m)"]

    def test_blank_unit_and_blank_name_are_skipped(self):
        assert fold_unit_row(["Depth", ""], ["", "x"]) == ["Depth", ""]

    def test_short_units_row_does_not_raise(self):
        assert fold_unit_row(["a", "b", "c"], ["m"]) == ["a (m)", "b", "c"]


class TestCombineLevels:
    """A variable archived one file per depth is levels of one cast, not many casts."""

    def test_concatenates_same_schema_tables(self):
        cols, rows, info = combine_levels([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["Depth (m)", "Temp"], [["10", "3.0"]]),
        ])
        assert cols == ["Depth (m)", "Temp"]
        assert rows == [["0", "3.2"], ["10", "3.0"]]
        assert info["tables_combined"] == 2

    def test_schema_mismatch_is_reported_not_misaligned(self):
        cols, rows, info = combine_levels([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["Depth (m)", "Salinity"], [["10", "32.0"]]),
        ])
        assert len(rows) == 1
        assert info["schema_mismatches"][0]["columns"] == ["Depth (m)", "Salinity"]

    def test_no_tables_is_an_error_not_a_crash(self):
        assert "error" in combine_levels([])[2]


class TestCombineOnKey:
    """One cast's variables split across files must be joined before staging."""

    def test_outer_joins_on_depth(self):
        cols, rows, info = combine_on_key([
            (["Depth (m)", "Temp"], [["0", "3.2"], ["10", "3.0"]]),
            (["Depth (m)", "Oxygen"], [["0", "320"], ["20", "290"]]),
        ], key="Depth (m)")
        assert cols == ["Depth (m)", "Temp", "Oxygen"]
        assert rows == [["0", "3.2", "320"], ["10", "3.0", ""], ["20", "", "290"]]
        assert info["keys"] == 3

    def test_key_aliases_let_files_name_the_key_differently(self):
        cols, rows, _ = combine_on_key([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["DEPTH", "Oxygen"], [["0", "320"]]),
        ], key="Depth (m)", key_aliases=["DEPTH"])
        assert rows == [["0", "3.2", "320"]]

    def test_disjoint_keys_are_visible_in_the_report(self):
        _, rows, info = combine_on_key([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["Depth (m)", "Oxygen"], [["999", "320"]]),
        ], key="Depth (m)")
        assert len(rows) == 2
        assert info["per_table"][1]["matched_existing_keys"] == 0

    def test_missing_key_column_is_reported(self):
        _, _, info = combine_on_key([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["Pressure", "Oxygen"], [["0", "320"]]),
        ], key="Depth (m)")
        assert info["per_table"][1]["key_column"] is None

    def test_colliding_variable_names_are_named(self):
        _, _, info = combine_on_key([
            (["Depth (m)", "Temp"], [["0", "3.2"]]),
            (["Depth (m)", "Temp"], [["0", "9.9"]]),
        ], key="Depth (m)")
        assert info["colliding_columns"] == ["Temp"]


class TestWriteStagedCast:
    def test_round_trips_through_discovery(self, tmp_path):
        """Staged output must parse as a positioned cast."""
        from wod_etl.discovery import profile_raw_data
        from wod_etl.mapping import generate_field_mapping, query_wod_spec

        out = tmp_path / "C145-001.csv"
        write_staged_cast(
            out,
            columns=["Depth (m)", "Temp (deg C)", "Salinity"],
            rows=[[0, 3.2, 31.9], [10, 3.0, 32.0]],
            latitude=62.16, longitude=-170.500167, date="2023-06-20", time="14:55",
            metadata={"Accession": "0287423", "Platform": "Oshoro maru", "Station": "a01"},
            notes=["Depth computed from PrDM by UNESCO 1983"])

        profile = profile_raw_data(out.read_text(), str(out))
        contract = generate_field_mapping(
            {**profile, "columns": ["Depth (m)", "Temp (deg C)", "Salinity"]},
            query_wod_spec("CTD", ["Depth (m)", "Temp (deg C)", "Salinity"], 10.0))

        assert contract["missing_required_coordinates"] == []
        assert contract["parsed_coordinates"]["latitude"] == pytest.approx(62.16)
        assert contract["parsed_coordinates"]["longitude"] == pytest.approx(-170.500167)
        assert contract["ready_for_transform"] is True

    def test_reports_what_it_wrote(self, tmp_path):
        res = write_staged_cast(tmp_path / "c.csv", ["Depth (m)"], [[0], [1], [2]],
                                latitude=1.0, longitude=2.0, date="2023-01-01")
        assert res["data_rows"] == 3
        assert res["header_lines"] == 3          # LAT, LON, DATE with no metadata


class TestCompactTimeFields:
    """A four-digit time is HHMM (2102 is 21:02), not HHMMSS."""

    def test_hhmm_in_a_separate_column(self):
        assert parse_timestamp("20230303", "2102") == dt.datetime(2023, 3, 3, 21, 2)
        assert parse_timestamp("20230318", "0230") == dt.datetime(2023, 3, 18, 2, 30)
        assert parse_timestamp("20230101", "2359") == dt.datetime(2023, 1, 1, 23, 59)

    def test_hhmm_glued_to_the_date(self):
        assert parse_timestamp("20230303 2102") == dt.datetime(2023, 3, 3, 21, 2)

    def test_hhmmss_still_parses(self):
        assert parse_timestamp("20230624", "031903") == dt.datetime(2023, 6, 24, 3, 19, 3)
        assert parse_timestamp("20230624 031903") == dt.datetime(2023, 6, 24, 3, 19, 3)

    def test_punctuated_forms_are_untouched(self):
        assert parse_timestamp("2023-03-03", "21:02") == dt.datetime(2023, 3, 3, 21, 2)
        assert parse_timestamp("2023/6/1") == dt.datetime(2023, 6, 1)


class TestReadTableDelimiters:
    def test_comma_is_still_the_default(self, tmp_path):
        p = tmp_path / "c.csv"
        p.write_text("Depth (m),Temp (deg C)\n0,12.1\n10,11.4\n")
        cols, rows, info = read_table(p)
        assert cols == ["Depth (m)", "Temp (deg C)"]
        assert info["delimiter"] == ","

    @pytest.mark.parametrize("sep,name", [("\t", "tab"), (";", "semicolon"), ("|", "pipe")])
    def test_sniffs_the_delimiter_when_asked(self, tmp_path, sep, name):
        p = tmp_path / "c.dat"
        p.write_text(f"Depth (m){sep}Temp (deg C)\n0{sep}12.1\n10{sep}11.4\n")
        cols, rows, info = read_table(p, delimiter=None)
        assert cols == ["Depth (m)", "Temp (deg C)"], f"{name} not split"
        assert info["delimiter"] == sep
        assert rows == [["0", "12.1"], ["10", "11.4"]]

    def test_a_tab_file_read_as_csv_collapses_to_one_column(self, tmp_path):
        """Pins the comma default."""
        p = tmp_path / "c.dat"
        p.write_text("Depth (m)\tTemp (deg C)\n0\t12.1\n10\t11.4\n")
        cols, _, _ = read_table(p)
        assert len(cols) == 1


class TestCombineOnKeyReportsRowsItOverwrites:
    """A key repeated within one table counts as a duplicate, not as a match."""

    def test_a_within_table_duplicate_is_counted_separately(self):
        t1 = (["Depth", "Temp"], [["0", "12.1"], ["0", "12.4"], ["10", "11.0"]])
        t2 = (["Depth", "Sal"], [["0", "34.1"], ["10", "34.2"]])
        _, rows, info = combine_on_key([t1, t2], key="Depth")
        first = info["per_table"][0]
        assert first["duplicate_keys_within_table"] == 1
        assert first["matched_existing_keys"] == 0      # nothing preceded it
        assert first["distinct_keys"] == 2
        assert len(rows) == 2                           # the overwrite is real

    def test_a_genuine_cross_table_match_still_counts(self):
        t1 = (["Depth", "Temp"], [["0", "12.1"], ["10", "11.0"]])
        t2 = (["Depth", "Sal"], [["0", "34.1"], ["10", "34.2"]])
        _, _, info = combine_on_key([t1, t2], key="Depth")
        assert info["per_table"][1]["matched_existing_keys"] == 2
        assert info["per_table"][1]["duplicate_keys_within_table"] == 0

    def test_overlap_against_distinct_keys_not_rows(self):
        """Rows overstate overlap when a table overwrites itself."""
        t1 = (["Depth", "Temp"], [["0", "1"], ["0", "2"], ["0", "3"]])
        t2 = (["Depth", "Sal"], [["0", "34.1"]])
        _, _, info = combine_on_key([t1, t2], key="Depth")
        assert info["per_table"][0]["distinct_keys"] == 1
        assert info["per_table"][0]["duplicate_keys_within_table"] == 2
