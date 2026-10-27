"""Station lists, long-format tables, count matrices, reconciliation and PDF tables."""
import datetime as dt

import pytest

from wod_etl.discovery import sniff_format
from wod_etl.staging import (join_station_list, melt_matrix, normalise_station,
                             parse_coordinate, parse_timestamp, pivot_long,
                             read_station_list)

STATION_LIST = (
    "﻿,,,,,,\n"
    "Cruise,Station No.,Date,Year,Long,Lat,Observation items\n"
    "SHU1505,1,Nov. 09,2015,142°18’E,21°00’N,\"CTD, trawl, NORPAC, Gillnet\"\n"
    "SHU1505,2,Nov. 10,2015,142°24’E,20°30’N,\"CTD, NORPAC, PL\"\n"
    "SHU1505,3,Nov. 10,2015,142°30’E,20°00’N,CTD\n"
    ",,,,,,\n"
    ",,,,,,\"*NORPAC samples were investigated only in station No.1, 3\"\n"
)


class TestCoordinateMarks:
    @pytest.mark.parametrize("value,expected", [
        ("142°18’E", 142.3),               # degree sign + right single quote
        ("21°00’N", 21.0),
        ("142°18'30\"E", 142.308333),           # ASCII minute and second marks
        ("16°30.5′S", -16.508333),        # prime
        ("N 21°00.5'", 21.008333),              # hemisphere written first
        ("21 deg 00.5 min N", 21.008333),
    ])
    def test_marks_are_separators(self, value, expected):
        assert parse_coordinate(value) == pytest.approx(expected, abs=1e-6)

    def test_marks_do_not_rescue_text(self):
        assert parse_coordinate("N/A") is None
        assert parse_coordinate("°'") is None


class TestMonthNameDates:
    def test_year_in_its_own_column(self):
        assert parse_timestamp("Nov. 09", year=2015) == dt.datetime(2015, 11, 9)
        assert parse_timestamp("Nov. 09", year="2015") == dt.datetime(2015, 11, 9)

    @pytest.mark.parametrize("value,expected", [
        ("Nov. 09, 2015", dt.datetime(2015, 11, 9)),
        ("Sept. 3 2015", dt.datetime(2015, 9, 3)),
        ("12-Nov-2015", dt.datetime(2015, 11, 12)),
    ])
    def test_forms(self, value, expected):
        assert parse_timestamp(value) == expected

    def test_a_year_is_not_read_as_a_clock_time(self):
        """'nov 09 2015' ends in four digits; it must not become 20:15 on an unknown year."""
        assert parse_timestamp("Nov 09 2015") == dt.datetime(2015, 11, 9)
        assert parse_timestamp("Nov 09 2015", "1442") == dt.datetime(2015, 11, 9, 14, 42)

    def test_a_year_argument_does_not_override_a_written_year(self):
        assert parse_timestamp("2015-11-09", year=1999) == dt.datetime(2015, 11, 9)


class TestStationListIsATable:
    def test_a_quoted_remark_column_does_not_make_it_prose(self, tmp_path):
        """Raw comma counts differ per row inside the quotes; csv-aware counts do not."""
        p = tmp_path / "stations.csv"
        p.write_text(STATION_LIST, encoding="utf-8")
        fmt = sniff_format(str(p))
        assert fmt["readable"] and fmt["reader"] == "delimited"


class TestReadStationList:
    @pytest.fixture
    def path(self, tmp_path):
        p = tmp_path / "SHU1505_StationInfo.csv"
        p.write_text(STATION_LIST, encoding="utf-8")
        return p

    def test_roles_positions_and_dates(self, path):
        stations, info = read_station_list(path)
        assert info["columns_used"]["station"] == "Station No."
        assert info["columns_used"]["year"] == "Year"
        assert list(stations) == ["1", "2", "3"]
        assert stations["1"]["latitude"] == pytest.approx(21.0)
        assert stations["1"]["longitude"] == pytest.approx(142.3)
        assert stations["2"]["date"] == "2015-11-10"
        assert stations["2"]["time"] is None and not stations["2"]["time_known"]

    def test_footer_remarks_are_dropped_by_name_not_silently(self, path):
        _, info = read_station_list(path)
        assert len(info["dropped"]) == 1
        assert "NORPAC samples" in info["dropped"][0]["text"]

    def test_a_reoccupied_station_is_kept_and_reported(self, tmp_path):
        p = tmp_path / "s.csv"
        p.write_text("Station,Lat,Lon,Date\nA1,10,20,2020-01-01\nA1,10.1,20,2020-01-05\n")
        stations, info = read_station_list(p)
        assert set(stations) == {"a1", "a1#2"}
        assert info["duplicates"] and "station and date" in info["note"]

    def test_a_zone_in_the_header_is_recorded(self, tmp_path):
        p = tmp_path / "s.csv"
        p.write_text("Stn,Lat,Lon,Date,Time (UTC)\n1,10,20,2020-01-01,0312\n")
        stations, info = read_station_list(p)
        assert info["time_zone"] == "UTC"
        assert stations["1"]["time"] == "03:12" and stations["1"]["time_known"]

    def test_missing_roles_are_an_error_not_an_empty_success(self, tmp_path):
        p = tmp_path / "s.csv"
        p.write_text("Name,Value\nx,1\n")
        stations, info = read_station_list(p)
        assert not stations and "no column found" in info["error"]


class TestJoinStationList:
    def test_spellings_meet_and_both_sides_are_reported(self):
        stations = {"1": {"station": "1"}, "2": {"station": "2"}, "9": {"station": "9"}}
        matched, rep = join_station_list(["St-01", "stn 2", "X"], stations)
        assert set(matched) == {"St-01", "stn 2"}
        assert rep["casts_without_station"] == ["X"]
        assert rep["stations_without_cast"] == ["9"]

    @pytest.mark.parametrize("raw,norm", [("St-01", "1"), ("Station 1", "1"), ("1.0", "1"),
                                          ("A01", "a01"), ("stn_12", "12")])
    def test_normalise_station(self, raw, norm):
        assert normalise_station(raw) == norm


class TestPivotLong:
    TEMP = (["Cruise", "StNo", "parm", "dep", "value"],
            [["C", "1", "Temperature", "5.0", "28.5"], ["C", "1", "Temperature", "6", "28.4"]])
    SAL = (["Cruise", "StNo", "parm", "depth", "value"],
           [["C", "1", "Salinity", "5", "34.1"], ["C", "1", "Salinity", "6.0", "34.2"]])

    def test_one_file_per_variable_joins_on_numeric_keys(self):
        cols, rows, info = pivot_long([self.TEMP, self.SAL], keys=["StNo", "depth"],
                                      variable="parm", value="value",
                                      key_aliases={"depth": ["dep"]},
                                      columns={"Temperature": "Temperature (deg C)"})
        assert cols == ["StNo", "depth", "Temperature (deg C)", "Salinity"]
        assert rows == [["1", "5.0", "28.5", "34.1"], ["1", "6", "28.4", "34.2"]]
        assert info["levels"] == 2 and not info["levels_missing_variable"]
        assert info["per_table"][0]["key_columns"] == ["StNo", "dep"]

    def test_a_single_table_is_accepted(self):
        cols, rows, _ = pivot_long(self.TEMP, keys=["StNo", "dep"], variable="parm",
                                   value="value")
        assert cols == ["StNo", "dep", "Temperature"] and len(rows) == 2

    def test_conflicting_values_are_listed_not_resolved(self):
        bad = (self.TEMP[0], self.TEMP[1] + [["C", "1", "Temperature", "5", "99"]])
        _, rows, info = pivot_long(bad, keys=["StNo", "dep"], variable="parm", value="value")
        assert rows[0][2] == "28.5"
        assert info["conflicts"][0]["also"] == "99" and info["note"]

    def test_a_level_missing_a_variable_is_counted(self):
        sal = (self.SAL[0], self.SAL[1][:1])
        _, rows, info = pivot_long([self.TEMP, sal], keys=["StNo", "depth"], variable="parm",
                                   value="value", key_aliases={"depth": ["dep"]})
        assert info["levels_missing_variable"] == {"Salinity": 1}
        assert rows[1][3] == ""


class TestMeltMatrix:
    ROWS = [
        ["", "", "", "Station", "1", "", "3", ""],
        ["Order", "Family", "Taxa", "mesh size", "0,1mm", "0.335mm", "0,1mm", "0.335mm"],
        ["Calanoida", "Acartiidae", "Acartia danae", "adult", "", "8", "", ""],
        ["Calanoida", "", "Acartia spp.", "copepodite", "1,700", "", "100", "12"],
    ]

    def test_cells_become_rows_with_headers_filled_across(self):
        cols, rows, info = melt_matrix(self.ROWS, header_rows=2,
                                       label_columns=["Order", "Family", "Taxon", "Stage"],
                                       header_names=["Station", "Mesh"], fill_down=["Family"])
        assert cols == ["Order", "Family", "Taxon", "Stage", "Station", "Mesh", "value"]
        assert ["Calanoida", "Acartiidae", "Acartia danae", "adult", "1", "0.335mm", "8"] in rows
        # thousands separator stripped; Family filled down
        assert ["Calanoida", "Acartiidae", "Acartia spp.", "copepodite", "1", "0,1mm",
                "1700"] in rows
        assert info["cells_written"] == 4 and info["blank_cells"] == 4
        assert {"Station": "3", "Mesh": "0.335mm"} in info["samples"]

    def test_blank_as_zero_is_an_explicit_choice(self):
        _, rows, info = melt_matrix(self.ROWS, header_rows=2,
                                    label_columns=["Order", "Family", "Taxon", "Stage"],
                                    header_names=["Station", "Mesh"], blank="zero")
        assert len(rows) == 8 and info["blank_policy"] == "zero"


from wod_etl.staging import check_track, distance_km, reconcile_stations  # noqa: E402

LINE = {str(i): {"station": str(i), "latitude": 21.0 - 0.5 * (i - 1),
                 "longitude": 142.3 + 0.1 * (i - 1), "date": f"2015-11-{9 + (i - 1) // 2:02d}"}
        for i in range(1, 12)}


class TestReconcileStations:
    def test_identical_sources_agree(self):
        r = reconcile_stations({"list": LINE, "report": LINE})
        assert not r["conflicts"] and r["stations_checked"] == 11

    def test_a_date_disagreement_names_both_sources(self):
        report = {k: dict(v) for k, v in LINE.items()}
        report["6"]["date"] = "2015-11-12"                 # the list says the 11th
        r = reconcile_stations({"list": LINE, "report": report})
        (c,) = r["conflicts"]
        assert c["station"] == "6" and c["field"] == "date"
        assert c["values"] == {"list": "2015-11-11", "report": "2015-11-12"}

    def test_a_position_beyond_tolerance_is_a_conflict(self):
        trawl = {"St-11": {"station": "St-11", "latitude": 16.0, "longitude": 143.15}}
        r = reconcile_stations({"list": {"11": dict(LINE["11"], longitude=142.2)},
                                "trawl": trawl}, position_tolerance_km=10)
        (c,) = r["conflicts"]
        assert c["field"] == "position" and c["spread_km"] > 90

    def test_a_station_one_source_states_is_unverified_not_wrong(self):
        r = reconcile_stations({"list": LINE, "trawl": {"1": LINE["1"]}})
        assert "2" in r["single_source"] and not r["conflicts"]


class TestCheckTrack:
    def test_a_mistyped_degree_stands_out_on_an_even_line(self):
        track = {k: dict(v) for k, v in LINE.items()}
        track["11"]["longitude"] -= 1.0                     # 143.3 typed as 142.3
        flags = check_track(track)["flags"]
        assert [(f["check"], f["to"]) for f in flags] == [("outlying_step", "11")]

    def test_an_even_line_raises_nothing(self):
        assert not check_track(LINE)["flags"]

    def test_time_running_backwards(self):
        track = {k: dict(v) for k, v in LINE.items()}
        track["5"]["date"] = "2015-11-01"
        assert any(f["check"] == "time_backwards" for f in check_track(track)["flags"])

    def test_speed_is_only_judged_when_both_times_are_known(self):
        a = {"station": "a", "latitude": 0.0, "longitude": 0.0, "date": "2020-01-01",
             "time": "00:00"}
        b = {"station": "b", "latitude": 0.0, "longitude": 2.0, "date": "2020-01-01",
             "time": "01:00"}
        flags = check_track([a, b])["flags"]
        assert flags[0]["check"] == "impossible_speed"
        assert not check_track([dict(a, time=None), dict(b, time=None)])["flags"]

    def test_bounding_box(self):
        flags = check_track(LINE, bounding_box=(141.5, 143.0, 15.8, 21.5))["flags"]
        assert {f["station"] for f in flags if f["check"] == "outside_bounding_box"} == \
            {"9", "10", "11"}

    def test_distance(self):
        assert distance_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.05)


from pathlib import Path  # noqa: E402

from wod_etl.staging import extract_pdf_rows, write_staged_table  # noqa: E402

MANUAL = Path(__file__).resolve().parents[1] / "knowledge" / "wod_readme.pdf"


@pytest.mark.skipif(not MANUAL.exists(), reason="the WOD manual PDF is not checked out")
class TestExtractPdfRows:
    def test_rows_are_read_with_a_page_citation(self):
        cols, rows, info = extract_pdf_rows(
            MANUAL, page=21, row_pattern=r"(\d+)\. ([A-Za-z ]+?) \((UOR|GLD)\) [Dd]ata",
            columns=["Number", "Name", "Code"])
        assert rows == [["10", "Undulating Oceanographic Recorder", "UOR"],
                        ["11", "Glider", "GLD"]]
        assert info["source"] == "wod_readme.pdf, PDF page 21 (printed page 21)"

    def test_a_pattern_that_matches_nothing_shows_the_text(self):
        _, rows, info = extract_pdf_rows(MANUAL, page=21, row_pattern=r"(NOPE)",
                                         columns=["x"])
        assert not rows and info["matches"] == 0 and info["text_excerpt"]

    def test_groups_must_match_columns(self):
        _, _, info = extract_pdf_rows(MANUAL, page=21, row_pattern=r"(\d+)", columns=["a", "b"])
        assert "group" in info["error"]


class TestWriteStagedTable:
    def test_a_harvested_table_reads_back_as_a_station_list(self, tmp_path):
        p = tmp_path / "report_table2.csv"
        write_staged_table(p, ["Station", "Date", "Long", "Lat"],
                           [["11", "Nov. 14, 2015", "143°09’E", "16°00’N"]],
                           source="Final.Report.pdf, PDF page 8 (printed page 5)",
                           notes=["Trawl positions, Table 2."])
        text = p.read_text()
        assert text.startswith("# Source: Final.Report.pdf, PDF page 8")
        stations, _ = read_station_list(p)
        assert stations["11"]["longitude"] == pytest.approx(143.15)
