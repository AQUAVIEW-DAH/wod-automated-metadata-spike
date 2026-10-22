"""The two fixture accessions: every triage outcome, and files whose extension misleads.

Each test works on a copy in tmp_path. Known gaps are `xfail(strict=True)`.
"""
import shutil
from pathlib import Path

import pytest

from wod_etl.discovery import classify_accession, sniff_format
from wod_etl.orchestration import process_accession
from wod_etl.staging.parse import parse_coordinate
from wod_etl.staging.station_list import read_station_list

FIXTURES = Path(__file__).parent / "fixtures" / "accessions"


@pytest.fixture
def mini(tmp_path):
    return Path(shutil.copytree(FIXTURES / "mini-accession", tmp_path / "mini-accession"))


@pytest.fixture
def mixed(tmp_path):
    return Path(shutil.copytree(FIXTURES / "mixed-formats", tmp_path / "mixed-formats"))


def by_path(triage):
    return {f["path"]: f for f in triage["files"]}


class TestMiniAccessionTriage:
    def test_every_folder_gets_a_verdict(self, mini):
        folders = classify_accession(str(mini))["folders"]
        assert set(folders) == {"0-about", "1-ctd", "2-adcp", "3-ctd-sidecar", "4-nutrients"}
        assert all(f["verdict"]["action"] for f in folders.values())

    def test_ctd_folders_are_profile_data(self, mini):
        folders = classify_accession(str(mini))["folders"]
        assert folders["1-ctd"]["verdict"]["action"] == "process"
        assert folders["1-ctd"]["verdict"]["dataset_types"] == {"CTD": 2}
        assert folders["3-ctd-sidecar"]["verdict"]["dominant"] == "CTD"

    def test_currents_are_excluded_for_having_no_table_3a_code(self, mini):
        f = by_path(classify_accession(str(mini)))["2-adcp/currents.csv"]
        assert f["action"] == "exclude"
        assert "Table 3a" in f["why"]

    def test_unanalysed_nutrients_are_empty_not_unsupported(self, mini):
        verdict = classify_accession(str(mini))["folders"]["4-nutrients"]["verdict"]
        assert verdict["action"] == "exclude"
        assert verdict["empty_files"] == ["4-nutrients/nutrients.csv"]

    def test_the_station_list_is_a_sidecar(self, mini):
        files = by_path(classify_accession(str(mini)))
        assert files["3-ctd-sidecar/stations.csv"]["action"] == "sidecar"

    def test_casts_located_by_a_station_list_are_staged(self, mini):
        f = by_path(classify_accession(str(mini)))["3-ctd-sidecar/casts.csv"]
        assert f["action"] == "stage" and f["include"]
        assert f["located_by"]["kind"] == "by_station"

    def test_conductivity_is_named_as_not_written(self, mini):
        f = by_path(classify_accession(str(mini)))["1-ctd/cast_01.csv"]
        assert "conductivity" in f["why"]


class TestMiniAccessionProcessing:
    @pytest.fixture
    def result(self, mini, tmp_path):
        return process_accession(str(mini), output_dir=str(tmp_path / "out"),
                                 raw_dir=str(tmp_path / "raw"), run_id="fixture",
                                 resume=False)

    def test_datasets_are_decided_per_file_by_resolution(self, result):
        # 25 m spacing is low resolution, so OSD whatever the instrument.
        assert result["dataset_types"] == {"OSD": 2}

    def test_conductivity_is_reported_dropped(self, result):
        assert any("Conductivity" in c for c in result["unstorable_columns_dropped"])

    def test_currents_are_skipped_not_failed(self, result):
        assert any("current velocity" in r for r in result["skipped_reasons"])
        assert not any(f["key"] == "2-adcp/currents.csv" for f in result["failures"])

    def test_no_measurements_is_distinguished_from_a_parse_failure(self, result):
        assert any(r.startswith("no measurements") for r in result["skipped_reasons"])
        assert not any(f["key"] == "4-nutrients/nutrients.csv" for f in result["failures"])

    def test_what_was_written_is_verified(self, result):
        assert len(result["outputs"]) == 1
        assert result["output_verified"] is True


class TestStationList:
    def test_degree_decimal_minute_coordinates(self):
        assert parse_coordinate("49-46.9858S") == pytest.approx(-49.7831, abs=1e-4)
        assert parse_coordinate("178-12.3300E") == pytest.approx(178.2055, abs=1e-4)

    def test_positions_are_read_from_the_list(self):
        stations, _ = read_station_list(
            FIXTURES / "mini-accession" / "3-ctd-sidecar" / "stations.csv")
        assert stations["7"]["latitude"] == pytest.approx(-49.7831, abs=1e-4)
        assert stations["8"]["longitude"] == pytest.approx(178.7650, abs=1e-4)

    def test_dates_with_a_space_separated_time_are_parsed(self):
        stations, _ = read_station_list(
            FIXTURES / "mini-accession" / "3-ctd-sidecar" / "stations.csv")
        assert (stations["7"]["date"], stations["7"]["time"]) == ("2024-03-11", "08:30")

    def test_a_zone_named_in_the_header_is_recorded(self):
        _, info = read_station_list(
            FIXTURES / "mini-accession" / "3-ctd-sidecar" / "stations.csv")
        assert info["time_zone"] == "NZST"


class TestMixedFormats:
    @pytest.mark.parametrize("path, fmt", [
        ("1-argo/R5904471_042", "netcdf4_hdf5"),
        ("2-seabird/cast_014.txt", "sea_bird_cnv"),
        ("3-old-ascii/drop_001.dat", "fixed_width_text"),
    ])
    def test_formats_are_read_from_the_bytes_not_the_name(self, mixed, path, fmt):
        assert sniff_format(str(mixed / path))["format_family"] == fmt

    def test_datasets_come_from_the_files_own_evidence(self, mixed):
        files = by_path(classify_accession(str(mixed)))
        assert files["1-argo/R5904471_042"]["dataset"] == "PFL"
        assert files["2-seabird/cast_014.txt"]["dataset"] == "CTD"

    def test_an_unnamed_fixed_width_file_is_left_for_a_person(self, mixed):
        files = by_path(classify_accession(str(mixed)))
        assert files["3-old-ascii/drop_001.dat"]["action"] == "inspect"

    def test_the_readme_is_documentation(self, mixed):
        files = by_path(classify_accession(str(mixed)))
        assert files["0-docs/readme.txt"]["action"] == "document"

    def test_plankton_with_nothing_to_locate_its_hauls_is_proposed_out_saying_why(self, mixed):
        # WOD takes it as Taxa-Records, but nothing here gives the hauls' positions.
        f = by_path(classify_accession(str(mixed)))["4-plankton/bongo_hauls.csv"]
        assert (f["action"], f["dataset"], f["include"]) == ("stage", "OSD", False)
        assert "cannot be located" in f["why"]
