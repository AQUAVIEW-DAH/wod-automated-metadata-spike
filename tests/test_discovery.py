"""Phase 1: directory listing, peeking, profiling, loading, cast splitting."""
import functools
import http.server
import socketserver
import threading

import pytest

from wod_etl.discovery import (
    fetch_and_peek_file,
    list_remote_directory,
    load_tabular,
    profile_raw_data,
    read_any,
    split_casts,
)

APACHE_INDEX = """<html><body><pre><a href="?C=N;O=D">Name</a>
<hr><a href="/data/oceans/archive/arc0222">Parent Directory</a>          -
<a href="../">Up one level</a>                                          -
<a href="5-XCTD/">5-XCTD/</a>            12-Apr-2023 14:22    -
<a href="surface_data.csv">surface_data.csv</a>   12-Apr-2023 14:25  1.0M
<a href="0287423.1.1.xml">0287423.1.1.xml</a>    03-Mar-2023 09:01  14K
<hr></pre></body></html>"""

DEEP_PATH = "data/oceans/archive/arc0222/0287423"


@pytest.fixture
def apache_server(tmp_path):
    """Serve an Apache-style index at a realistic deep path."""
    deep = tmp_path / DEEP_PATH
    deep.mkdir(parents=True)
    (deep / "index.html").write_text(APACHE_INDEX)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(tmp_path))

    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}/{DEEP_PATH}/"
        httpd.shutdown()


class TestListRemoteDirectory:
    def test_parent_directory_link_is_not_listed_as_a_child(self, apache_server):
        """NCEI writes the parent link as an absolute path, not '../'."""
        res = list_remote_directory(apache_server)
        assert "arc0222" not in [i["name"] for i in res["items"]]
        assert res["skipped_navigation_links"] >= 2

    def test_sizes_and_dates_are_parsed(self, apache_server):
        items = {i["name"]: i for i in list_remote_directory(apache_server)["items"]}
        assert items["surface_data.csv"]["size_bytes"] == 1024 ** 2
        assert items["0287423.1.1.xml"]["size_bytes"] == 14 * 1024
        assert items["surface_data.csv"]["last_modified"].startswith("2023-04-12T14:25")

    def test_directories_are_flagged(self, apache_server):
        items = {i["name"]: i for i in list_remote_directory(apache_server)["items"]}
        assert items["5-XCTD"]["is_dir"] is True
        assert items["surface_data.csv"]["is_dir"] is False

    def test_extension_filter(self, apache_server):
        res = list_remote_directory(apache_server, filter_extension=".csv")
        assert "0287423.1.1.xml" not in [i["name"] for i in res["items"]]

    def test_unreachable_host_returns_error_and_does_not_raise(self):
        res = list_remote_directory("http://127.0.0.1:9/nope/")
        assert res["total_items"] == 0
        assert "error" in res

    def test_local_directory(self, tmp_path):
        (tmp_path / "a.csv").write_text("x")
        (tmp_path / "sub").mkdir()
        res = list_remote_directory(str(tmp_path))
        names = {i["name"] for i in res["items"]}
        assert {"a.csv", "sub"} <= names


class TestFetchAndPeek:
    def test_reads_only_the_requested_prefix(self, tmp_path):
        p = tmp_path / "big.txt"
        p.write_text("A" * 10_000)
        peek = fetch_and_peek_file(str(p), num_bytes=100)
        assert peek["peek_size_bytes"] == 100
        assert len(peek["preview_text"]) == 100

    def test_missing_file_returns_error(self):
        assert "error" in fetch_and_peek_file("/nonexistent/nope.csv")


class TestProfileRawData:
    def test_separates_header_from_data(self):
        prof = profile_raw_data(
            "# CRUISE: X\nLAT: 61.9 LON: -172.2\nDepth, Temp_C\n0.0, 9.1\n10.0, 8.9\n")
        assert prof["header_comment_lines"] == 2
        assert prof["columns"] == ["Depth", "Temp_C"]

    def test_empty_input_returns_error(self):
        assert "error" in profile_raw_data("")


class TestLoadTabular:
    def test_reads_full_file_with_unit_suffixed_columns(self, tmp_path):
        p = tmp_path / "cast.txt"
        p.write_text("# CRUISE: X\nLAT: 61.9 LON: -172.2\n"
                     "Depth (m), Temp (C)\n0.0, 9.13\n10.0, 8.96\n25.0, 6.75\n")
        df, info = load_tabular(str(p), {"detected_delimiter": "','"})
        assert df is not None and len(df) == 3
        assert list(df.columns) == ["Depth (m)", "Temp (C)"]
        assert info["header_lines_skipped"] == 2

    def test_missing_value_sentinels_become_nan(self, tmp_path):
        p = tmp_path / "c.csv"
        p.write_text("Depth, Temp\n0.0, 9.1\n10.0, -999\n")
        df, _ = load_tabular(str(p), {"detected_delimiter": "','"})
        assert df["Temp"].isna().sum() == 1

    def test_whitespace_delimited(self, tmp_path):
        p = tmp_path / "c.txt"
        p.write_text("Depth  Temp\n0.0  9.1\n10.0  8.9\n")
        df, _ = load_tabular(str(p), {"detected_delimiter": "whitespace"})
        assert df is not None and list(df.columns) == ["Depth", "Temp"]

    def test_missing_file_reports_error_without_raising(self):
        df, info = load_tabular("/nonexistent/x.csv")
        assert df is None and "error" in info

    def test_a_malformed_row_is_reported_not_just_dropped(self, tmp_path):
        """pandas only warns on stderr; the dropped row must reach the caller."""
        p = tmp_path / "c.csv"
        p.write_text("Depth,Temp\n0.0,9.1\nLEGEND,a,b,c,d\n10.0,8.9\n")
        df, info = load_tabular(str(p), delimiter=",")
        assert len(df) == 2
        assert info["malformed_rows_dropped"] == 1
        assert any("malformed" in w and "3" in w for w in info["warnings"])

    def test_a_clean_file_reports_no_malformed_rows(self, tmp_path):
        p = tmp_path / "c.csv"
        p.write_text("Depth,Temp\n0.0,9.1\n10.0,8.9\n")
        _, info = load_tabular(str(p), delimiter=",")
        assert "malformed_rows_dropped" not in info


class TestSplitCasts:
    def test_single_cast_file_yields_one_cast(self):
        res = split_casts("# CRUISE: X\nDepth, Temp\n0.0, 9.1\n10.0, 8.9\n")
        assert res["cast_count"] == 1
        assert res["multi_cast"] is False

    def test_repeated_header_block_starts_a_new_cast(self):
        res = split_casts(
            "# CAST: 1\nLAT: 61.9 LON: -172.2\nDepth, Temp\n0.0, 9.1\n10.0, 8.9\n"
            "# CAST: 2\nLAT: 62.4 LON: -173.0\nDepth, Temp\n0.0, 8.1\n12.0, 7.8\n")
        assert res["cast_count"] == 2
        assert res["multi_cast"] is True
        assert "61.9" in res["casts"][0]["header_block"]
        assert "62.4" in res["casts"][1]["header_block"]

    def test_row_counts_exclude_the_column_header(self):
        res = split_casts("# CAST: 1\nDepth, Temp\n0.0, 9.1\n10.0, 8.9\n20.0, 8.0\n")
        assert res["casts"][0]["data_row_count"] == 3

    def test_empty_input_reports_error(self):
        assert split_casts("   ")["cast_count"] == 0


class TestCommentsBelowTheHeader:
    """Only leading comment lines are skipped; later ones must not shift the header."""

    def _load(self, tmp_path, name, text):
        p = tmp_path / name
        p.write_text(text)
        profile = profile_raw_data(fetch_and_peek_file(str(p))["preview_text"], str(p))
        return profile, load_tabular(str(p), profile)

    def test_a_mid_file_comment_does_not_eat_the_header(self, tmp_path):
        profile, (df, info) = self._load(
            tmp_path, "mid.csv",
            "Depth (m),Temp (deg C)\n0,20\n# instrument recalibrated here\n20,18\n")
        assert profile["header_comment_lines"] == 0
        assert list(df.columns) == ["Depth (m)", "Temp (deg C)"]
        assert len(df) == 2

    def test_a_footer_legend_does_not_eat_the_header(self, tmp_path):
        _, (df, info) = self._load(
            tmp_path, "foot.csv",
            "Depth (m),Temp (deg C)\n0,20\n10,18\n# legend: NS = not sampled\n")
        assert list(df.columns) == ["Depth (m)", "Temp (deg C)"]
        assert len(df) == 2

    def test_a_dropped_comment_row_is_reported_not_silent(self, tmp_path):
        _, (df, info) = self._load(
            tmp_path, "mid2.csv", "Depth (m),T\n0,20\n# note\n20,18\n")
        assert any("interior comment" in w for w in info["warnings"])

    def test_a_leading_block_is_still_skipped(self, tmp_path):
        profile, (df, info) = self._load(
            tmp_path, "lead.csv",
            "# CRUISE: X\n# DATE: 2023-01-01\nDepth (m),Temp (deg C)\n0,20\n10,18\n")
        assert profile["header_comment_lines"] == 2
        assert list(df.columns) == ["Depth (m)", "Temp (deg C)"]

    def test_star_sentinels_are_not_mistaken_for_comments(self, tmp_path):
        """'**NS' and '*DL' begin real cells; only '#' and '//' mark a comment line."""
        _, (df, info) = self._load(
            tmp_path, "sent.csv", "Depth (m),Nitrate (umol/kg)\n0,**NS\n10,1.2\n")
        assert len(df) == 2


class TestRealValuesAreNotSentinels:
    """Values such as -9 or 9999 can be real, so they are not default missing markers."""

    @pytest.mark.parametrize("column,value", [
        ("Air temperature (deg C)", "-9"),
        ("Longitude", "-99"),
        ("Bottom depth (m)", "9999"),
    ])
    def test_a_plausible_value_survives(self, tmp_path, column, value):
        p = tmp_path / "v.csv"
        p.write_text(f"Depth (m),{column}\n0,{value}\n10,1.5\n")
        profile = profile_raw_data(fetch_and_peek_file(str(p))["preview_text"], str(p))
        df, _ = load_tabular(str(p), profile)
        assert df[column].iloc[0] == pytest.approx(float(value))

    def test_unambiguous_sentinels_are_still_missing(self, tmp_path):
        p = tmp_path / "s.csv"
        p.write_text("Depth (m),Temp (deg C)\n0,-999\n10,-9999\n20,1.5\n")
        profile = profile_raw_data(fetch_and_peek_file(str(p))["preview_text"], str(p))
        df, _ = load_tabular(str(p), profile)
        assert df["Temp (deg C)"].isna().sum() == 2


class TestPeekDecoding:
    """A peek cut mid-character must not make a UTF-8 file look latin-1."""

    FULL = "Nitrate (µmol/l),Depth (m)\n1.5,0\n".encode("utf-8")

    @pytest.mark.parametrize("cut", [9, 10, 11, 12])
    def test_a_cut_multibyte_character_does_not_change_the_verdict(self, cut):
        from wod_etl.discovery.fetch_and_peek_file import _decode_peek
        text, encoding = _decode_peek(self.FULL[:cut])
        assert encoding == "utf-8"
        assert "Â" not in text

    def test_a_whole_file_still_decodes(self):
        from wod_etl.discovery.fetch_and_peek_file import _decode_peek
        text, encoding = _decode_peek(self.FULL)
        assert encoding == "utf-8"
        assert "µmol/l" in text

    def test_a_japanese_export_is_detected_rather_than_mojibaked(self):
        from wod_etl.discovery.fetch_and_peek_file import _decode_peek
        text, encoding = _decode_peek("経度,緯度\n1,2\n".encode("cp932"))
        assert encoding == "cp932"
        assert "経度" in text


class TestMissingPathIsAnError:
    def test_a_nonexistent_path_does_not_become_a_phantom_cast(self):
        res = split_casts("/no/such/file.csv", {"detected_delimiter": "','"})
        assert res["casts"] == []
        assert "not found" in res["error"]

    def test_text_passed_directly_still_works(self):
        res = split_casts("Depth (m),Temp (deg C)\n0,20\n10,18\n",
                          {"detected_delimiter": "','"})
        assert res["cast_count"] == 1


class TestSubdirTraversalGuard:
    @pytest.mark.parametrize("subdir,expected", [
        ("run_07/stationA", "run_07/stationA"),
        ("run_07", "run_07"),
        ("run/./x", "run/x"),
    ])
    def test_legitimate_nesting_is_preserved(self, subdir, expected):
        from wod_etl.discovery.download_file import _safe_subpath
        assert str(_safe_subpath(subdir)) == expected

    @pytest.mark.parametrize("subdir", ["../../etc", "a/../../b", "/etc/passwd", "..", "../"])
    def test_nothing_escapes_the_destination_root(self, subdir, tmp_path):
        from wod_etl.discovery.download_file import _safe_subpath
        resolved = (tmp_path / _safe_subpath(subdir)).resolve()
        assert resolved.is_relative_to(tmp_path.resolve())


class TestReaderWarningsReachTheCaller:
    def test_a_dropped_row_is_visible_through_read_any(self, tmp_path):
        from wod_etl.discovery import read_any, sniff_format
        p = tmp_path / "cast.csv"
        p.write_text("Depth,Temp\n0,9.1\n5,9.0\nLEGEND,a,b,c,d\n10,8.9\n20,8.4\n")
        casts, info = read_any(str(p), sniff_format(str(p)))
        assert len(casts[0]["df"]) == 4
        assert any("malformed" in w for w in info["warnings"])


class TestALocalCopyIsOnlyReusedWhileCurrent:
    def test_an_unchanged_source_is_cached(self, tmp_path):
        from wod_etl.discovery import download_file

        src = tmp_path / "cast.csv"
        src.write_text("Depth (m),Temp (deg C)\n0,20\n")
        first = download_file(str(src), dest_dir=str(tmp_path / "raw"))
        again = download_file(str(src), dest_dir=str(tmp_path / "raw"))
        assert first["status"] == "copied" and again["status"] == "cached"

    def test_a_changed_source_is_copied_again(self, tmp_path):
        import os

        from wod_etl.discovery import download_file

        src = tmp_path / "cast.csv"
        src.write_text("Depth (m),Temp (deg C)\n0,20\n")
        download_file(str(src), dest_dir=str(tmp_path / "raw"))
        src.write_text("Depth (m),Temp (deg C)\n0,20\n10,19\n")
        later = os.stat(src).st_mtime + 5
        os.utime(src, (later, later))
        res = download_file(str(src), dest_dir=str(tmp_path / "raw"))
        assert res["status"] == "copied" and "changed" in res["note"]
        assert (tmp_path / "raw" / "cast.csv").read_text().endswith("10,19\n")


# Time units and first-cast time from a real CTD file.
LECONTE_UNITS = "seconds since 1970-01-01 00:00:00 0:00"
T0 = 1470773808.0                                           # 2016-08-09 20:16:48 UTC


class TestNetCDFCastsAreDatedAndPlacedByTheirOwnVariables:
    @staticmethod
    def _write(path, times, units=LECONTE_UNITS, fill=None, **attrs):
        """Casts along `profile` with a time, lat and lon each, on a shared depth axis."""
        import netCDF4

        n = len(times)
        with netCDF4.Dataset(path, "w") as ds:
            ds.setncatts(attrs)
            ds.createDimension("profile", n)
            ds.createDimension("z", 3)
            t = ds.createVariable("time", "f8", ("profile",), fill_value=fill)
            t.units, t.calendar = units, "calendar"      # placeholder, as in the source file
            t[:] = times
            for name, unit, start, step in (("lat", "degrees_north", 56.82, 0.01),
                                            ("lon", "degrees_east", -132.36, -0.01)):
                v = ds.createVariable(name, "f4", ("profile",))
                v.units = unit
                v[:] = [start + i * step for i in range(n)]
            z = ds.createVariable("z", "f4", ("z",))
            z.units = "m"
            z[:] = [1, 2, 3]
            temp = ds.createVariable("temperature", "f4", ("profile", "z"))
            temp.units = "degrees C"
            temp[:] = [[5.0, 4.9, 4.8]] * n
        return str(path)

    @staticmethod
    def _coords(cast):
        """(year, month, day, hour, minute) and (latitude, longitude), as the mapper reads them."""
        from wod_etl.mapping.generate_field_mapping import parse_header_block

        c = parse_header_block(cast["header_block"])["coordinates"]
        return (tuple(c.get(k) for k in ("year", "month", "day", "hour", "minute")),
                (c.get("latitude"), c.get("longitude")))

    def test_each_cast_has_its_own_date_time_and_position(self, tmp_path):
        times = [T0, T0 + 86400 + 3600, T0 + 2 * 86400 + 7800]
        casts, info = read_any(self._write(tmp_path / "ctd.nc", times))
        assert info.get("error") is None
        got = [self._coords(c) for c in casts]
        assert [date for date, _ in got] == [(2016, 8, 9, 20, 16), (2016, 8, 10, 21, 16),
                                             (2016, 8, 11, 22, 26)]
        assert got[2][1] == pytest.approx((56.84, -132.38), abs=1e-4)
        assert "time: 2016-08-09T20:16:48Z" in casts[0]["header_block"]

    def test_the_files_own_dates_do_not_override_the_casts(self, tmp_path):
        path = self._write(tmp_path / "ctd.nc", [T0, T0 + 86400],
                           date_created="2019-05-28", time_coverage_start="2016-08-01")
        casts, _ = read_any(path)
        header = casts[1]["header_block"]
        assert "date_created: 2019-05-28" in header          # kept, but never as a Date
        assert "Date: 2019-05-28" not in header and "Date: 2016-08-01" not in header
        assert self._coords(casts[1])[0] == (2016, 8, 10, 20, 16)

    def test_a_single_profile_with_no_time_variable_still_uses_the_attributes(self, tmp_path):
        import netCDF4

        path = tmp_path / "one.nc"
        with netCDF4.Dataset(path, "w") as ds:
            ds.setncatts({"date_created": "2019-05-28", "time_coverage_start": "20240304T091500Z",
                          "geospatial_lat_min": 34.25, "geospatial_lon_min": -120.5})
            ds.createDimension("z", 3)
            for name, unit in (("depth", "m"), ("temperature", "degC")):
                v = ds.createVariable(name, "f4", ("z",))
                v.units = unit
                v[:] = [1, 2, 3]
        casts, _ = read_any(str(path))
        date, position = self._coords(casts[0])
        assert date[:3] == (2024, 3, 4)
        assert position == pytest.approx((34.25, -120.5))

    @pytest.mark.parametrize("units", [LECONTE_UNITS, "seconds since 1970-01-01T00:00:00Z",
                                       "seconds since 1970-01-01"])
    def test_an_epoch_with_a_utc_offset_decodes(self, tmp_path, units):
        casts, _ = read_any(self._write(tmp_path / "ctd.nc", [T0], units=units))
        assert self._coords(casts[0])[0] == (2016, 8, 9, 20, 16)

    def test_a_mistyped_epoch_leaves_the_cast_undated_and_says_so(self, tmp_path):
        """An epoch typo (1070 for 1970) would put a 2018 cast in 1118."""
        units = "seconds since 1070-01-01 00:00:00 0:00"
        path = self._write(tmp_path / "keel.nc", [1536014356.5], units=units,
                           time_coverage_start="2018-09-03")
        casts, info = read_any(path)
        assert "Date:" not in casts[0]["header_block"]
        assert self._coords(casts[0])[0] == (None,) * 5
        warning = next(w for w in info["warnings"] if "epoch looks wrong" in w)
        assert "'time'" in warning and units in warning and "1118-09-03" in warning

    def test_a_missing_time_dates_nothing(self, tmp_path):
        """A fill or NaN time gives no date, and the file's date does not stand in for it."""
        path = self._write(tmp_path / "ctd.nc", [T0, -9999.0, float("nan")], fill=-9999.0,
                           time_coverage_start="2016-08-01")
        casts, _ = read_any(path)
        assert self._coords(casts[0])[0] == (2016, 8, 9, 20, 16)
        for cast in casts[1:]:
            assert "Date:" not in cast["header_block"]
            assert self._coords(cast)[0] == (None,) * 5

    def test_a_bounding_box_does_not_override_the_cast_position(self, tmp_path):
        path = self._write(tmp_path / "ctd.nc", [T0, T0 + 600],
                           geospatial_lat_min=56.0, geospatial_lon_min=-133.0)
        casts, _ = read_any(path)
        assert "geospatial_lat_min: 56.0" in casts[1]["header_block"]
        assert "(from global attribute geospatial_lat_min)" not in casts[1]["header_block"]
        assert self._coords(casts[1])[1] == pytest.approx((56.83, -132.37), abs=1e-4)

    def test_a_lone_cast_timed_per_scan_is_dated_by_its_first_scan(self, tmp_path):
        import netCDF4

        path = tmp_path / "scans.nc"
        with netCDF4.Dataset(path, "w") as ds:
            ds.time_coverage_start = "2016-08-01"
            ds.createDimension("obs", 3)
            t = ds.createVariable("time", "f8", ("obs",), fill_value=-9999.0)
            t.units = LECONTE_UNITS
            t[:] = [-9999.0, T0, T0 + 30]
            for name, unit in (("depth", "m"), ("temperature", "degC")):
                v = ds.createVariable(name, "f4", ("obs",))
                v.units = unit
                v[:] = [1, 2, 3]
        casts, _ = read_any(str(path))
        assert self._coords(casts[0])[0] == (2016, 8, 9, 20, 16)


class TestStationBlockFiles:
    """A file header, then per station a `Stn:` line, level count, column names and rows."""

    @staticmethod
    def _text(levels=(12, 12), stated=(12, 14)):
        out = [" Experiment:  NABOS-09          Number of stations:  2",
               " Area:        Arctic Ocean      Missing value:       -9", ""]
        for (stn, lat, lon, day), n, says in zip(
                (("KD0309", "81.671N", "31.307E", "18"), ("KD0409a", "81.771N", "31.029E", "19")),
                levels, stated):
            out += [f"Stn: {stn} ;  Lat: {lat};  Lon:  {lon};  Time: 2009. 8.{day};  "
                    f"Depth: -999m", f"Number of levels: {says};  Ice:   0%      Comment: CTD",
                    "    PR     TEMP  SALNTY"]
            out += [f"{1.5 + i / 2:7.2f}  {-1.3 - i / 100:6.3f}  {32.3 + i / 100:6.3f}"
                    if i != 1 else f"{2.0:7.2f}  -9.000  -9.000" for i in range(n)]
        return "\n".join(out) + "\n"

    def test_every_block_is_a_cast_with_the_file_header(self, tmp_path):
        from wod_etl.mapping.generate_field_mapping import parse_header_block

        p = tmp_path / "2009CTD.txt"
        p.write_text(self._text())
        casts, info = read_any(str(p))
        assert [c["df"].shape for c in casts] == [(12, 3), (12, 3)], info
        assert list(casts[1]["df"].columns) == ["PR", "TEMP", "SALNTY"]
        assert "Missing value:       -9" in casts[1]["header_block"]
        coords = parse_header_block(casts[1]["header_block"])["coordinates"]
        assert (coords["year"], coords["month"], coords["day"]) == (2009, 8, 19)
        assert (coords["latitude"], coords["longitude"]) == (81.771, 31.029)
        assert "hour" not in coords                       # a date, not a time of day

    def test_the_declared_missing_value_is_missing_and_a_short_block_is_reported(self,
                                                                                tmp_path):
        p = tmp_path / "2009CTD.txt"
        p.write_text(self._text())
        casts, info = read_any(str(p))
        assert casts[0]["df"]["TEMP"].isna().tolist() == [False, True] + [False] * 10
        assert any("declared missing value -9" in w for w in info["warnings"])
        assert any("states 14 levels, 12 were read" in w for w in info["warnings"])


class TestNetCDFStringDimensions:
    def test_a_string_length_dimension_is_not_the_profile_dimension(self, tmp_path):
        """`profile_id_length` contains 'profile' but counts id characters, not profiles."""
        import netCDF4
        import numpy as np

        p = tmp_path / "mvp.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("profile", 1)
            ds.createDimension("z", 5)
            ds.createDimension("profile_id_length", 40)
            pid = ds.createVariable("profile", "S1", ("profile", "profile_id_length"))
            pid[0, :] = np.array(list("2025-02-01T04:38:30Z".ljust(40)), dtype="S1")
            for name, units, value in (("time", "seconds since 1970-01-01 00:00:00",
                                        1738384710), ("lat", "degrees_north", 27.71),
                                       ("lon", "degrees_east", -96.12)):
                v = ds.createVariable(name, "f8", ("profile",))
                v.units = units
                v[:] = [value]
            for name, units in (("depth", "m"), ("temperature", "degree_C")):
                v = ds.createVariable(name, "f4", ("profile", "z"))
                v.units = units
                v[:] = np.arange(5, dtype="f4").reshape(1, 5) + 1
        casts, info = read_any(str(p))
        assert len(casts) == 1 and casts[0]["df"].shape == (5, 2), info
        assert "Date: 2025-02-01" in casts[0]["header_block"]
