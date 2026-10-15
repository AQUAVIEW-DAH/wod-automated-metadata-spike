"""What a cast IS beyond its numbers: when it was taken, what it is called, which codes
describe it, and what the stager recorded about it -- all of it reaching the NetCDF."""
from pathlib import Path

import numpy as np
import pytest

from helpers import cache_code_list, make_cast
from wod_etl.mapping import map_metadata
from wod_etl.orchestration import process_accession, process_one_file
from wod_etl.reference.code_lists import lookup_code, parse_code_list
from wod_etl.staging import write_staged_cast
from wod_etl.writers import write_ragged_profiles
from wod_etl.writers.generate_netcdf_ragged import utc_offset_hours

nc = pytest.importorskip("netCDF4")

PLATFORMS = ("code_name,Platform\nunits,OCL code\n"
             "4959,SHUNYO MARU\n"
             "10793,SHUNYO MARU (R/V;c.s.8JIF;b.03.1973;renamed Kaiko Maru No.12)A\n"
             "5423,KAHAROA (R/V; call sign ZM7552)\n"
             "9000,ALPHA HELIX\n9001,ALPHA HELIX\n")
SCALES = ("code_name,scale\nunits,WOD code\n102,Temperature: T68 (IPTS-68)\n"
          "103,Temperature: ITS-90\n202,Salinity: PSS78\n")
UNITS = ("code_name,Original units\nunits,NODC code\n"
         "036,ug/l               mg/m3    ppb     g/1000m3\n"
         "136,ug/l    Alternate Nutrient Conversion (use instead of #36)\n082,degrees Fahrenheit\n")


def _stage(tmp_path, name="cast.csv", **kw):
    args = dict(columns=["Depth (m)", "Temperature [ITS-90, deg C]"],
                rows=[[0, 20.0], [10, 19.5], [20, 18.0]],
                latitude=21.0, longitude=142.3, date="2015-11-09",
                metadata={"Accession": "0303928", "Platform": "Shunyo-Maru",
                          "Cruise": "SHU1505", "Station": "1", "Cast": "SHU1505_st01",
                          "Instrument": "SBE 911plus"},
                notes=["Position from the station list, row 1."],
                sources=["data/raw/0303928/data/0-data/U2015-015_CTD_Temp.csv"])
    args.update(kw)
    p = tmp_path / name
    write_staged_cast(p, **args)
    return p


def _process(p, tmp_path, tag="t"):
    res = process_one_file(str(p), "CTD", str(tmp_path / f"out_{tag}"), tag,
                           raw_root=str(tmp_path / f"raw_{tag}"))
    assert res["ok"], res
    return res, nc.Dataset(res["output_path"])


# ---------------------------------------------------------------- time of day
class TestTimeOfDay:
    def test_a_date_without_a_time_is_not_written_as_midnight(self, tmp_path):
        _, ds = _process(_stage(tmp_path), tmp_path)
        assert ds["date"][0] == 20151109
        assert np.ma.is_masked(ds["GMT_time"][0])
        assert "Do not read it as midnight" in ds["time"].comment
        ds.close()

    def test_a_known_time_fills_gmt_time(self, tmp_path):
        _, ds = _process(_stage(tmp_path, time="14:42"), tmp_path)
        assert ds["GMT_time"][0] == pytest.approx(14.7)
        assert "comment" not in ds["time"].ncattrs()
        ds.close()

    def test_a_numeric_zone_with_a_time_is_converted_to_utc(self, tmp_path):
        _, ds = _process(_stage(tmp_path, time="05:30", time_zone="UTC+10"), tmp_path)
        assert ds["date"][0] == 20151108                  # local 05:30 is 19:30 the day before
        assert ds["GMT_time"][0] == pytest.approx(19.5)
        assert ds.getncattr("time_zone_of_source") == "UTC+10"
        ds.close()

    def test_a_local_date_is_recorded_not_shifted(self, tmp_path):
        _, ds = _process(_stage(tmp_path, time_zone="local"), tmp_path)
        assert ds["date"][0] == 20151109
        assert "NOT converted" in ds.getncattr("time_zone_note")
        ds.close()

    @pytest.mark.parametrize("zone,hours", [("UTC", 0.0), ("UTC+10", 10.0),
                                            ("GMT-3:30", -3.5), ("+0900", 9.0),
                                            ("JST", None), ("local", None)])
    def test_offsets(self, zone, hours):
        assert utc_offset_hours(zone) == hours

    def test_the_run_reports_date_only_casts(self, tmp_path):
        acc = tmp_path / "acc"
        acc.mkdir()
        _stage(acc, "a.csv")
        _stage(acc, "b.csv", time="10:00")
        res = process_accession(str(acc), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_tod", resume=False)
        assert res["outputs_without_time_of_day"] == ["a.csv"]
        assert res["outputs_without_time"] is None


# ------------------------------------------------------------------- cast ids
class TestCastIds:
    def test_one_cast_files_no_longer_share_an_id(self, tmp_path):
        ids = []
        for i in (1, 2):
            p = _stage(tmp_path, f"c{i}.csv",
                       metadata={"Accession": "0303928", "Cruise": "SHU1505",
                                 "Station": str(i), "Cast": f"SHU1505_st{i:02d}"})
            _, ds = _process(p, tmp_path, tag=f"id{i}")
            ids.append(ds["cast_id"][0])
            ds.close()
        assert ids == ["0303928_SHU1505-st01_20151109", "0303928_SHU1505-st02_20151109"]

    def test_repeats_within_a_file_are_suffixed(self, tmp_path):
        out = tmp_path / "two.nc"
        write_ragged_profiles([make_cast(), make_cast()], str(out))
        with nc.Dataset(out) as ds:
            assert list(ds["cast_id"][:]) == ["STOCS85A_test_19850612",
                                              "STOCS85A_test_19850612_02"]

    def test_with_no_cast_or_station_the_file_name_keeps_drops_apart(self, tmp_path):
        a, b = make_cast(), make_cast()
        a["contract"]["source"] = "data/raw/acc/drop_0012.edf"
        b["contract"]["source"] = "data/raw/acc/drop_0013.edf"
        b["contract"]["parsed_coordinates"].update(hour=14, minute=42)
        out = tmp_path / "drops.nc"
        write_ragged_profiles([a, b], str(out))
        with nc.Dataset(out) as ds:
            assert list(ds["cast_id"][:]) == ["STOCS85A_drop-0012_19850612",
                                              "STOCS85A_drop-0013_19850612T1442"]


# ---------------------------------------------------------- header lines
class TestHeaderLinesAreNeverSilent:
    HEADER = ("# Accession: 0303928\n# Platform: Shunyo-Maru\n# Cruise: SHU1505\n"
              "# Station: 11\n# Instrument: SBE CTD/Rosette system\n"
              "# Weather observer: K. Tanaka\n# Note: a note, not a key\n")
    COLS = ["Depth (m)", "Temperature (deg C)", "Salinity (PSU)"]

    def test_a_cast_level_instrument_reaches_every_measured_variable(self):
        meta = map_metadata(self.HEADER, self.COLS)
        for col in ("Temperature (deg C)", "Salinity (PSU)"):
            h = meta["variable_specific_headers"][col]["headers"]["5"]
            assert h["value"] == "SBE CTD/Rosette system"
        assert "5" not in meta["variable_specific_headers"]["Depth (m)"]["headers"]

    def test_the_cruise_is_character_data(self):
        meta = map_metadata(self.HEADER, self.COLS)
        assert meta["character_data"]["originator_cruise_id"]["value"] == "SHU1505"
        assert "3" in meta["secondary_headers"]             # the platform line, not cruise

    def test_an_unknown_key_is_unresolved_not_dropped(self):
        meta = map_metadata(self.HEADER, self.COLS)
        keys = {u["key"] for u in meta["unresolved"]}
        assert "Weather observer" in keys
        assert "Note" not in keys

    def test_a_second_key_for_a_held_header_is_recorded(self):
        meta = map_metadata("# Platform: Shunyo-Maru\n# Vessel: Shunyo Maru II\n", self.COLS)
        assert meta["secondary_headers"]["3"]["value"] == "Shunyo-Maru"
        assert any(u["key"].lower() == "vessel" for u in meta["unresolved"])

    def test_cast_duration_is_not_read_as_a_cast_number(self):
        meta = map_metadata("# Cast duration: 30\n", self.COLS)
        assert "5" not in meta["secondary_headers"]
        assert meta["secondary_headers"]["11"]["value"] == 30.0


# ------------------------------------------------------------------ code lists
class TestCodeLists:
    def test_parse_both_layouts(self):
        entries = parse_code_list(UNITS)
        assert entries[0]["code"] == "036" and "MGM3" in entries[0]["names"]
        taxa = parse_code_list("ITIS TSN,PGC code,Rank,Taxonomic Name -[ Author ]-\n"
                               "86086,4212010,species,Acartia danae    -[  Giesbrecht; 1889 ]-\n")
        assert taxa[0]["code"] == "86086" and taxa[0]["PGC code"] == "4212010"
        assert "ACARTIADANAE" in taxa[0]["names"]

    def test_exact_name_matches_one_code(self, offline_code_lists):
        cache_code_list(offline_code_lists, "s_3_platform", PLATFORMS)
        r = lookup_code("secondary_header", 3, "R/V Kaharoa")
        assert r["status"] == "matched" and r["code"] == "5423"

    def test_two_vessels_of_one_name_are_ambiguous_whatever_follows_the_name(
            self, offline_code_lists):
        """10793 is a 1973 SHUNYO MARU, later renamed. A name cannot tell them apart."""
        cache_code_list(offline_code_lists, "s_3_platform", PLATFORMS)
        r = lookup_code("secondary_header", 3, "Shunyo-Maru")
        assert r["status"] == "ambiguous"
        assert {c["code"] for c in r["candidates"]} == {"4959", "10793"}

    def test_plurals_and_label_heads(self, offline_code_lists):
        cache_code_list(offline_code_lists, "t_5_taxon_lifestage",
                        "code_name,Taxon lifestage\nunits,WOD code table\n6,LARVA\n8,ADULT\n"
                        "10,C5: COPEPODITE V\n"
                        "29,COPEPODITE(S) sum of various (unspecified) copepodite stages\n")
        assert lookup_code("taxa_variable", 5, "larvae")["code"] == "6"
        assert lookup_code("taxa_variable", 5, "copepodite")["code"] == "29"

    def test_two_entries_with_one_name_are_ambiguous(self, offline_code_lists):
        cache_code_list(offline_code_lists, "s_3_platform", PLATFORMS)
        r = lookup_code("secondary_header", 3, "Alpha Helix")
        assert r["status"] == "ambiguous" and {c["code"] for c in r["candidates"]} == {"9000",
                                                                                      "9001"}

    def test_a_category_prefix_and_squashed_spelling_still_match(self, offline_code_lists):
        cache_code_list(offline_code_lists, "v_3_scale", SCALES)
        assert lookup_code("variable_specific_header", 3, "ITS-90")["code"] == "103"
        assert lookup_code("variable_specific_header", 3, "PSS-78")["code"] == "202"

    def test_no_list_means_unavailable_not_a_guess(self):
        r = lookup_code("secondary_header", 3, "Shunyo-Maru")
        assert r["status"] == "unavailable"

    def test_a_number_is_already_a_code(self):
        assert lookup_code("secondary_header", 29, "464.")["status"] == "already_a_code"

    def test_a_field_with_no_list_is_not_coded(self):
        assert lookup_code("secondary_header", 7, "11")["status"] == "not_coded"


class TestCodedFieldsInTheOutput:
    def test_a_matched_platform_is_written_as_its_code(self, tmp_path, offline_code_lists):
        cache_code_list(offline_code_lists, "s_3_platform", PLATFORMS)
        cache_code_list(offline_code_lists, "v_3_scale", SCALES)
        p = _stage(tmp_path, metadata={"Accession": "0146739", "Platform": "Kaharoa"})
        _, ds = _process(p, tmp_path)
        g = {a: ds.getncattr(a) for a in ds.ncattrs()}
        assert g["secondary_header_3_platform_code"] == "5423"
        assert g["secondary_header_3_platform_code_originator_text"] == "Kaharoa"
        assert "matched 5423" in g["secondary_header_3_platform_code_code_lookup"]
        t = ds["temperature"]
        assert t.getncattr("wod_variable_specific_header_3") == "103"
        assert t.getncattr("measurement_scale") == "ITS-90"
        ds.close()

    def test_unresolved_text_goes_beside_the_field_never_in_it(self, tmp_path,
                                                              offline_code_lists):
        cache_code_list(offline_code_lists, "s_3_platform", PLATFORMS)
        p = _stage(tmp_path, metadata={"Accession": "0303928", "Platform": "Alpha Helix"})
        res, ds = _process(p, tmp_path)
        g = {a: ds.getncattr(a) for a in ds.ncattrs()}
        assert "secondary_header_3_platform_code" not in g
        assert g["secondary_header_3_platform_code_originator_text"] == "Alpha Helix"
        assert "ambiguous" in g["secondary_header_3_platform_code_code_lookup"]
        assert any("Alpha Helix" in str(u["value"])
                   for u in res["coded_headers_not_resolved"])
        ds.close()


# ------------------------------------------------------------------ provenance
class TestProvenanceReachesTheOutput:
    def test_notes_sources_and_the_real_path(self, tmp_path):
        _, ds = _process(_stage(tmp_path), tmp_path)
        g = {a: ds.getncattr(a) for a in ds.ncattrs()}
        assert g["comment"] == "Position from the station list, row 1."
        assert g["source_files_raw"].endswith("U2015-015_CTD_Temp.csv")
        assert Path(g["source_file"]).name == "cast.csv"
        assert g["source_format"].startswith("delimited_text")
        assert "wod-agent-etl" in g["history"]
        assert g["originator_cruise_id"] == "SHU1505"
        ds.close()

    def test_unplaced_keys_are_kept_in_the_file(self, tmp_path):
        p = _stage(tmp_path, metadata={"Accession": "0303928", "Weather observer": "K."})
        _, ds = _process(p, tmp_path)
        assert "Weather observer" in ds.getncattr("metadata_not_placed")
        ds.close()

    def test_per_cast_notes_that_differ_go_to_a_variable(self, tmp_path):
        a, b = make_cast(), make_cast(lat=62.4)
        a["contract"]["provenance_notes"] = ["shared", "only a"]
        b["contract"]["provenance_notes"] = ["shared"]
        out = tmp_path / "n.nc"
        write_ragged_profiles([a, b], str(out))
        with nc.Dataset(out) as ds:
            assert ds.getncattr("comment") == "shared"
            assert list(ds["cast_notes"][:]) == ["only a", ""]


class TestDepthFromPressure:
    """WOD computes the depths of casts measured on pressure surfaces and keeps the pressure
    as a variable (WOD manual, cast structure)."""

    def _run(self, tmp_path, column):
        d = tmp_path / "in"
        d.mkdir()
        (d / "cast.csv").write_text(
            "# LAT: 30.0\n# LON: -140.0\n# DATE: 2015-11-09\n"
            f"{column},Temperature (deg C)\n"
            + "".join(f"{p},{20 - p / 100:.2f}\n" for p in (0, 10, 500, 1000)))
        return process_accession(str(d), output_dir=str(tmp_path / "o"),
                                 raw_dir=str(tmp_path / "r"), run_id="t_pressure",
                                 resume=False, verify=False,
                                 dataset_overrides={"cast.csv": "CTD"})

    def test_decibars_become_depth_with_the_derivation_said(self, tmp_path):
        import netCDF4 as nc

        res = self._run(tmp_path, "Pressure (dbar)")
        assert res["counts"].get("done") == 1, res
        from wod_etl.staging import pressure_to_depth

        with nc.Dataset(res["outputs"][0]) as ds:
            z = ds["z"][:]
            # UNESCO 1983 at 30 N: 1000 dbar is about 991 m, not 1000 m.
            assert abs(float(z[-1]) - pressure_to_depth(1000.0, 30.0)) < 0.01
            assert float(z[-1]) < 995 and float(z[0]) == 0.0
            assert "depth computed from Pressure (dbar)" in ds.getncattr("comment")

    def test_pressure_in_another_unit_is_refused_with_why(self, tmp_path):
        res = self._run(tmp_path, "Pressure (kPa)")
        assert not res["counts"].get("done")
        reasons = " ".join(f["error"] for f in res["failures"]) + " ".join(res["skipped_reasons"])
        assert "not in decibars" in reasons


class TestCastsAFileLeavesOutAreReported:
    def test_a_profile_with_no_position_is_counted_not_lost(self, tmp_path):
        import netCDF4
        import numpy as np

        d = tmp_path / "in"
        d.mkdir()
        with netCDF4.Dataset(d / "casts.nc", "w") as ds:
            ds.createDimension("profile", 2)
            ds.createDimension("z", 4)
            for name, units, values in (("time", "seconds since 1970-01-01", [1.47e9, 1.48e9]),
                                        ("lat", "degrees_north", [56.8, np.nan]),
                                        ("lon", "degrees_east", [-132.4, np.nan])):
                v = ds.createVariable(name, "f8", ("profile",))
                v.units = units
                v[:] = values
            zv = ds.createVariable("z", "f8", ("z",))
            zv.units, zv.standard_name = "m", "depth"
            zv[:] = [1.0, 2.0, 3.0, 4.0]
            t = ds.createVariable("temperature", "f4", ("profile", "z"))
            t.units = "degrees C"
            t[:] = np.full((2, 4), 5.0)
        res = process_accession(str(d), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_noposition",
                                resume=False, verify=False, dataset_overrides={"": "CTD"})
        assert res["counts"].get("done") == 1 and res["dataset_types"] == {"CTD": 1}
        (why, n), = res["casts_not_written"].items()
        assert n == 1 and "missing required coordinate(s): latitude, longitude" in why


class TestADecidedCTDStillFollowsTheResolutionRule:
    def test_a_coarse_cast_decided_as_ctd_is_written_as_osd(self, tmp_path):
        """Low-resolution CTD belongs in OSD (manual p.19), whoever named the dataset."""
        d = tmp_path / "in"
        d.mkdir()
        (d / "coarse.csv").write_text(
            "# LAT: 30.0\n# LON: -140.0\n# DATE: 2015-11-09\n"
            "Depth (m),Temperature (deg C)\n" + "".join(f"{z},{20 - z / 100:.2f}\n"
                                                      for z in range(0, 100, 10)))
        res = process_accession(str(d), output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), run_id="t_coarse", resume=False,
                                verify=False, dataset_overrides={"coarse.csv": "CTD"})
        assert res["dataset_types"] == {"OSD": 1}


class TestTheOutputIsCF:
    def test_units_are_udunits_and_table_3a_wording_is_kept(self, tmp_path):
        import netCDF4 as nc

        out = tmp_path / "cf.nc"
        write_ragged_profiles([make_cast()], str(out))
        with nc.Dataset(out) as ds:
            t = ds["temperature"]
            assert t.units == "degree_C" and t.wod_units == "Degrees Celsius"
            assert t.standard_name == "sea_water_temperature" and ds["z"].units == "m"

    def test_missing_values_are_the_fill_value_not_nan(self, tmp_path):
        import netCDF4 as nc

        cast = make_cast(rows=3)
        cast["df"].loc[1, "Temp_C"] = np.nan
        out = tmp_path / "fill.nc"
        write_ragged_profiles([cast], str(out))
        with nc.Dataset(out) as ds:
            ds.set_auto_mask(False)
            raw = ds["temperature"][:]
            assert raw[1] == ds["temperature"]._FillValue and not np.isnan(raw).any()


def _write(tmp_path, name, text):
    d = tmp_path / "in"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text)
    return d


def _run_one(d, tmp_path, **kw):
    return process_accession(str(d), output_dir=str(tmp_path / "o"), raw_dir=str(tmp_path / "r"),
                             run_id=f"t_{tmp_path.name}", resume=False, verify=False, **kw)


HEADER = "# LAT: 30.0\n# LON: -140.0\n# DATE: 2015-11-09\n# TIME: 04:38:30\n"


class TestWhatIsWrittenIsWhatWasMeasured:
    def test_a_level_with_nothing_measured_is_not_written(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C)\n"
                   + "".join(f"{z},{'' if z > 5 else 20 - z / 10}\n" for z in range(10)))
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"})
        assert res["levels_without_data"] == 4
        with nc.Dataset(res["outputs"][0]) as ds:
            assert ds.dimensions["z_obs"].size == 6

    def test_the_time_keeps_its_seconds(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C)\n0,20\n1,19.9\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"})
        with nc.Dataset(res["outputs"][0]) as ds:
            assert abs(float(ds["GMT_time"][0]) - (4 + 38 / 60 + 30 / 3600)) < 1e-4

    def test_the_accession_is_written_into_every_cast(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C)\n0,20\n1,19.9\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"}, accession="0189574")
        with nc.Dataset(res["outputs"][0]) as ds:
            text = " ".join(str(ds.getncattr(a)) for a in ds.ncattrs())
            text += " ".join(str(v[:]) for n, v in ds.variables.items() if "secondary" in n)
            assert "0189574" in text

    def test_a_dropped_variable_and_review_metadata_are_applied(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C),Salinity (PSU)\n"
                   "0,20,34.5\n1,19.9,34.6\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"},
                       file_options={"ctd.csv": {"drop": ["salinity"],
                                                 "metadata": {"Platform": "Kapitan Dranitsyn"}}})
        with nc.Dataset(res["outputs"][0]) as ds:
            assert "salinity" not in ds.variables and "temperature" in ds.variables
            assert "Salinity (PSU) (Salinity, left out by decisions.toml)" in ds.getncattr("comment")
            assert "Kapitan Dranitsyn" in " ".join(str(ds.getncattr(a)) for a in ds.ncattrs())
        assert any("left out by decisions.toml" in k for k in res["unstorable_columns_dropped"])

    def test_a_flag_column_naming_no_variable_is_every_variables_originator_flag(self,
                                                                                tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C),flag\n"
                   "0,20,0\n1,19.9,1\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"})
        with nc.Dataset(res["outputs"][0]) as ds:
            assert list(ds["temperature_qc_originator"][:]) == [0.0, 1.0]


class TestTheSourceIsReadAsItsOriginatorMeantIt:
    def test_a_duplicate_cast_is_written_once(self, tmp_path):
        import netCDF4

        d = tmp_path / "in"
        d.mkdir()
        with netCDF4.Dataset(d / "casts.nc", "w") as ds:
            ds.createDimension("profile", 3)
            ds.createDimension("z", 4)
            for name, units, values in (("time", "seconds since 1970-01-01",
                                         [1.47e9, 1.47e9, 1.48e9]),
                                        ("lat", "degrees_north", [56.8, 56.8, 56.9]),
                                        ("lon", "degrees_east", [-132.4, -132.4, -132.3])):
                v = ds.createVariable(name, "f8", ("profile",))
                v.units = units
                v[:] = values
            zv = ds.createVariable("z", "f8", ("z",))
            zv.units, zv.standard_name = "m", "depth"
            zv[:] = [1.0, 2.0, 3.0, 4.0]
            t = ds.createVariable("temperature", "f4", ("profile", "z"))
            t.units = "degrees C"
            t[:] = np.array([[5, 5.1, 5.2, 5.3], [5, 5.1, 5.2, 5.3], [6, 6.1, 6.2, 6.3]])
        res = _run_one(d, tmp_path, dataset_overrides={"casts.nc": "CTD"})
        assert res["dataset_types"] == {"CTD": 2}
        (why, n), = res["casts_not_written"].items()
        assert n == 1 and why.startswith("an exact duplicate of an earlier cast")

    def test_a_float_fill_is_missing_not_a_value(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "Depth (m),Temperature (deg C)\n"
                   "0,20\n1,-9.99e-29\n2,19.8\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"})
        with nc.Dataset(res["outputs"][0]) as ds:
            assert ds.dimensions["z_obs"].size == 2                # the level held nothing

    def test_the_depth_on_a_station_line_is_the_bottom_depth_and_minus_999_is_none(self):
        from wod_etl.mapping.generate_field_mapping import parse_header_block

        line = "Stn: KD5408 ;  Lat: 81.529N;  Lon:  30.857E;  Time: 2008.10.27;  Depth:  900m"
        assert parse_header_block(line)["secondary_headers"]["10"]["value"] == 900.0
        assert "10" not in parse_header_block(line.replace("900m", "-999m"))[
            "secondary_headers"]

    def test_a_source_line_naming_a_project_is_not_a_source_file(self):
        from wod_etl.mapping.generate_field_mapping import parse_header_block

        h = parse_header_block("Source:      NABOS             Comment: I/B Kapitan Dranitsyn")
        assert h["sources"] == []
        assert parse_header_block("# Source: data/raw/x/cast.csv")["sources"] == [
            "data/raw/x/cast.csv"]

    def test_a_key_value_line_with_commas_is_not_split_into_pairs(self):
        from wod_etl.mapping import map_metadata

        md = map_metadata("title: CTD profiles from LeConte Bay, Alaska, August 2016", [],
                          None, "CTD")
        kept = " ".join(str(v) for v in (md.get("unplaced") or md.get("metadata_not_placed")
                                         or {}).values()) if isinstance(
            md.get("unplaced") or md.get("metadata_not_placed"), dict) else str(md)
        assert "='August 2016'" not in kept and "LeConte Bay='" not in kept


class TestTheReviewHasTheLastWord:
    def test_review_metadata_overrides_what_the_file_states(self, tmp_path):
        d = _write(tmp_path, "ctd.csv", HEADER + "# Instrument: CTD\n# Project: wrong\n"
                   "Depth (m),Temperature (deg C)\n0,20\n1,19.9\n")
        res = _run_one(d, tmp_path, dataset_overrides={"ctd.csv": "CTD"},
                       file_options={"ctd.csv": {"drop": [],
                                                 "metadata": {"Project": "NABOS"}}})
        with nc.Dataset(res["outputs"][0]) as ds:
            attrs = " ".join(f"{a}={ds.getncattr(a)}" for a in ds.ncattrs())
            assert "NABOS" in attrs and "wrong" not in attrs.split("metadata_not_placed")[0]

    def test_a_single_level_ctd_cast_is_low_resolution_and_goes_to_osd(self, tmp_path):
        d = _write(tmp_path, "one.csv", HEADER + "Depth (m),Temperature (deg C)\n2.5,20\n")
        res = _run_one(d, tmp_path, dataset_overrides={"one.csv": "CTD"})
        assert res["dataset_types"] == {"OSD": 1}


class TestCodeListsAreReadAsWODMeansThem:
    def test_an_entry_the_list_retires_is_no_match(self):
        entries = parse_code_list("code_name,platform\n10125,KAPITAN DRANITSYN (DO NOT USE)\n"
                                  "8553,KAPITAN DRANITSYN (I/B)\n")
        from wod_etl.reference.code_lists import normalise_label

        named = [e for e in entries if normalise_label("Kapitan Dranitsyn") in e["names"]]
        assert [e["code"] for e in named] == ["8553"]

    def test_a_generic_value_is_told_to_name_the_model_not_to_pick_a_candidate(self):
        from wod_etl.reference.code_lists import describe_lookup

        text = describe_lookup({"status": "ambiguous", "value": "CTD", "field": "v_5_instrument",
                                "candidates": [{"code": "407", "label": "CTD: (MHI)"},
                                               {"code": "408", "label": "CTD: (IO RAS)"}]})
        assert "too general" in text and "407" not in text


class TestKeyValueHeadersKeepTheirCommas:
    def test_a_title_with_commas_is_one_value_and_two_pairs_on_a_line_are_two(self):
        from wod_etl.mapping import map_metadata

        md = map_metadata("title: CTD profiles from LeConte Bay, Alaska, August 2016\n"
                          "Weather observer: K. Smith, deck 2", [], None, "CTD")
        values = {u["key"]: u["value"] for u in md["unresolved"]}
        assert values.get("title") == "CTD profiles from LeConte Bay, Alaska, August 2016"
        assert values.get("Weather observer") == "K. Smith, deck 2"
