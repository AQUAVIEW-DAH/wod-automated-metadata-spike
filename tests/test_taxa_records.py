"""Plankton hauls as a Biological Header plus Taxa-Records: staged, mapped, written, validated."""
import numpy as np
import pytest

from helpers import cache_code_list
from wod_etl.discovery import classify_dataset
from wod_etl.discovery.classify_dataset import STAGED_HAUL_MARKER
from wod_etl.mapping import resolve_taxon
from wod_etl.orchestration import process_taxa
from wod_etl.staging import HAUL_MARKER, read_staged_haul, write_staged_haul
from wod_etl.verify import validate_taxa_netcdf

nc = pytest.importorskip("netCDF4")

TAXA = ("ITIS TSN,PGC code,Rank,Taxonomic Name -[ Author or comments ]-\n"
        "86084,4212010,genus,Acartia    -[  Dana;1846 ]-\n"
        "86086,4212010,species,Acartia danae    -[  Giesbrecht; 1889 ]-\n"
        "172418,5000000,genus,Thunnus\n"
        "95599,4300000,order,Decapoda\n"
        "82406,4267550,genus,Mysis\n"
        "-5004,5010000,<WOD group>,Fish -> pisces\n")
STAGES = ("code_name,Taxon lifestage\nunits,WOD code table\n6,LARVA\n7,JUVENILE\n8,ADULT\n"
          "29,COPEPODITE(S) sum of various (unspecified) copepodite stages\n")
GEAR = "code_name,Gear\nunits,WOD code\n101,NORPAC Net\n107,Ring Net\n"
TOW = "code_name,Type of tow\nunits,WOD code\n1,HORIZONTAL TOW\n2,VERTICAL TOW\n"


@pytest.fixture
def lists(offline_code_lists):
    for field, text in (("t_1_taxa_list", TAXA), ("t_5_taxon_lifestage", STAGES),
                        ("b_7_gear_and_flowmeter", GEAR), ("b_4_type_tow", TOW)):
        cache_code_list(offline_code_lists, field, text)
    return offline_code_lists


def _haul(path, **kw):
    args = dict(
        records=[{"Taxon": "Acartia danae", "Life stage": "adult", "Count": 8,
                  "Unit": "number per sample"},
                 {"Taxon": "Acartia spp.", "Life stage": "copepodite", "Count": 1700,
                  "Unit": "number per sample"},
                 {"Taxon": "Thunnus obesus", "Life stage": "larvae", "Count": 1,
                  "Unit": "number per sample"},
                 {"Taxon": "Mysis of Decapoda", "Count": 4, "Unit": "number per sample"},
                 {"Taxon": "Unheardofia nova", "Count": 2, "Unit": "number per sample"}],
        latitude=21.0, longitude=142.3, date="2015-11-09", upper_depth=0, lower_depth=150,
        biological_header={"Gear code": "NORPAC Net", "Mesh size": 100,
                           "Type of tow": "vertical tow", "Bucket colour": "red"},
        metadata={"Accession": "0303928", "Cruise": "SHU1505", "Station": "1",
                  "Cast": "SHU1505_st01_norpac_100um"},
        notes=["Counts per sample; no volume filtered."],
        sources=["data/raw/0303928/data/0-data/U2015-015_NORPAC.csv"])
    args.update(kw)
    return write_staged_haul(path, **args)


class TestStagedHaul:
    def test_the_haul_is_written_with_its_depth_range_and_header(self, tmp_path):
        res = _haul(tmp_path / "h.csv")
        assert res["records"] == 5
        text = (tmp_path / "h.csv").read_text()
        assert text.startswith("# Record type: WOD taxa-records")
        assert "# Bio Gear code: NORPAC Net" in text and "# Bio Mesh size: 100" in text
        assert "Upper depth (m)" in text and "Lower depth (m)" in text

    def test_an_unknown_collection_field_is_a_note_not_a_header(self, tmp_path):
        res = _haul(tmp_path / "h.csv")
        assert res["unknown_biological_fields"] == ["Bucket colour"]
        assert "no Table 6a home: red" in (tmp_path / "h.csv").read_text()

    def test_what_a_reader_needs_and_is_missing_is_named(self, tmp_path):
        res = _haul(tmp_path / "h.csv")
        assert set(res["missing_biological_fields"]) == {
            "1 Water volume filtered", "14 Tow distance", "10 Preservative", "13 Count method"}

    def test_read_back(self, tmp_path):
        _haul(tmp_path / "h.csv")
        staged = read_staged_haul(tmp_path / "h.csv")
        assert staged["biological_header"][7]["value"] == "NORPAC Net"
        assert staged["biological_header"][3]["value"] == "100"
        assert staged["fields"] == [1, 5, 10, 20, 2, 3]
        assert "LAT: 21.000000" in staged["header_text"]


class TestResolveTaxon:
    def test_species_genus_and_spp(self, lists):
        assert resolve_taxon("Acartia danae")["code"] == 86086
        spp = resolve_taxon("Acartia spp.")
        assert spp["status"] == "matched" and spp["code"] == 86084 and "genus" in spp["note"]

    def test_a_missing_species_falls_back_to_its_genus_and_says_so(self, lists):
        r = resolve_taxon("Thunnus obesus")
        assert r["status"] == "matched_higher_rank" and r["code"] == 172418
        assert "originator's name is kept" in r["note"]

    def test_a_stage_of_a_taxon_is_that_taxon_not_a_genus_named_like_the_stage(self, lists):
        """'Mysis of Decapoda' is a decapod larva, not the mysid genus Mysis."""
        r = resolve_taxon("Mysis of Decapoda")
        assert r["code"] == 95599 and r["query"] == "Decapoda"

    def test_unknown_and_blank_names(self, lists):
        assert resolve_taxon("Unheardofia nova")["code"] is None
        assert resolve_taxon("na")["status"] == "no_name"

    def test_without_the_list_nothing_is_guessed(self):
        assert resolve_taxon("Acartia danae")["status"] == "unavailable"


class TestProcessTaxa:
    def test_end_to_end(self, lists, tmp_path):
        _haul(tmp_path / "staged" / "h1.csv")
        res = process_taxa(tmp_path / "staged", tmp_path / "out")
        assert res["status"] == "complete", res["failures"]
        assert res["taxa_records"] == 5
        assert res["unresolved_taxa"] == {"Unheardofia nova": 1}
        assert set(res["taxa_resolved_at_higher_rank"]) == {"Thunnus obesus",
                                                            "Mysis of Decapoda"}
        assert "1 Water volume filtered" in res["missing_biological_fields"]

        with nc.Dataset(res["outputs"][0]) as ds:
            assert list(ds["t_1_variable_number"][:4]) == [86086, 86084, 172418, 95599]
            assert np.ma.is_masked(ds["t_1_variable_number"][4])
            assert ds["taxon_name_originator"][4] == "Unheardofia nova"
            assert list(ds["t_5_taxon_life_stage"][:3]) == [8, 29, 6]
            assert list(ds["t_2_upper_depth"][:]) == [0] * 5
            assert list(ds["t_3_lower_depth"][:]) == [150] * 5
            assert list(ds["t_10_taxon_count"][:]) == [8, 1700, 1, 4, 2]
            assert ds["t_20_originator_s_unit"][0] == "number per sample"
            assert ds["b_7_gear_code"][0] == "101"
            assert ds["b_7_gear_code_originator_text"][0] == "NORPAC Net"
            assert ds["b_4_type_of_tow"][0] == "2"
            assert float(ds["b_3_mesh_size"][0]) == 100.0
            assert ds["taxa_row_size"][0] == 5
            assert ds.getncattr("originator_cruise_id") == "SHU1505"
            assert ds.getncattr("comment").splitlines() == [
                "Counts per sample; no volume filtered.",
                "biological field 'Bucket colour' has no Table 6a home: red"]
            assert ds["cast_id"][0] == "0303928_SHU1505-st01-norpac-100um_20151109"

    def test_a_reversed_depth_range_is_not_output(self, lists, tmp_path):
        _haul(tmp_path / "staged" / "bad.csv", upper_depth=150, lower_depth=0)
        res = process_taxa(tmp_path / "staged", tmp_path / "out")
        assert res["status"] == "incomplete"
        assert "upper depth below" in " ".join(res["failures"][0]["error"])

    def test_validator_on_a_good_file(self, lists, tmp_path):
        _haul(tmp_path / "staged" / "h.csv")
        res = process_taxa(tmp_path / "staged", tmp_path / "out")
        report = validate_taxa_netcdf(res["outputs"][0])
        assert report["passed"]
        assert any("Biological Header fields not stated" in w for w in report["warnings"])
        assert any("did not resolve" in w for w in report["warnings"])


class TestTheProfilePathHandsPlanktonOver:
    def test_a_staged_haul_is_excluded_with_the_route_to_take(self, tmp_path):
        _haul(tmp_path / "h.csv")
        text = (tmp_path / "h.csv").read_text()
        v = classify_dataset(path="staged/h.csv", columns=["Taxon", "Count"],
                             header_text=text[:2000])
        assert v["exclusion"]["kind"] == "staged_haul"
        assert "process_taxa" in v["exclusion"]["matched"]

    def test_discovery_and_staging_agree_on_the_marker(self):
        """Discovery keeps its own copy of the marker text; the two must not drift."""
        assert STAGED_HAUL_MARKER == HAUL_MARKER

    def test_process_accession_skips_it_rather_than_failing(self, tmp_path):
        from wod_etl.orchestration import process_accession

        _haul(tmp_path / "acc" / "h.csv")
        res = process_accession(str(tmp_path / "acc"), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_pl", resume=False)
        assert res["counts"].get("skipped") == 1 and not res["failures"]
        assert any("plankton" in r for r in res["skipped_reasons"])
