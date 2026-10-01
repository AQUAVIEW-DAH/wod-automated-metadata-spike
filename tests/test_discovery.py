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
    """The bridge that used to be a manual step between Phase 2 and Phase 3."""

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
        """pandas drops a row whose field count is wrong and warns to stderr, which an
        unattended run never sees. The loss has to reach the caller."""
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
    """`header_comment_lines` counted comments ANYWHERE in the 8 KB preview and was then
    used as a skip count, so one mid-file annotation skipped the real column-header row
    and promoted the first data row to be the header."""

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
    """'-9', '-99', '9999' and '99999' were in DEFAULT_NA_VALUES. pandas expands them to
    their float forms, so a real measurement equal to one silently became NaN."""

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
    """A peek cuts at a fixed byte count. Landing mid-character was read as evidence the
    file was latin-1, and load_tabular then decoded the WHOLE file that way -- turning
    'µmol/l' into 'Âµmol/l' in a column name and defeating unit detection."""

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
        """_read returned the path string as its own content, so split_casts produced a
        one-row cast whose only datum was the path."""
        res = split_casts("/no/such/file.csv", {"detected_delimiter": "','"})
        assert res["casts"] == []
        assert "not found" in res["error"]

    def test_text_passed_directly_still_works(self):
        res = split_casts("Depth (m),Temp (deg C)\n0,20\n10,18\n",
                          {"detected_delimiter": "','"})
        assert res["cast_count"] == 1


class TestSubdirTraversalGuard:
    """A nested subdir keeps its nesting, but nothing may escape the destination root."""

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
    """load_tabular's warnings used to stop at read_any, which is what the run calls."""

    def test_a_dropped_row_is_visible_through_read_any(self, tmp_path):
        from wod_etl.discovery import read_any, sniff_format
        p = tmp_path / "cast.csv"
        p.write_text("Depth,Temp\n0,9.1\n5,9.0\nLEGEND,a,b,c,d\n10,8.9\n20,8.4\n")
        casts, info = read_any(str(p), sniff_format(str(p)))
        assert len(casts[0]["df"]) == 4
        assert any("malformed" in w for w in info["warnings"])
