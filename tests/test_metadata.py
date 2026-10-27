"""Everything in a file that is not a measurement value: headers, units, scales and flags."""
import netCDF4 as nc
import numpy as np
import pandas as pd
import pytest

from wod_etl.mapping import generate_field_mapping, map_metadata, query_wod_spec
from wod_etl.mapping.query_wod_spec import (explain_column, extract_scale, extract_unit,
                                            match_variable)
from wod_etl.orchestration import process_accession
from wod_etl.reference import code_table_hub, code_table_link, code_table_links


class TestNamesThatLookLikeVariablesAndAreNot:
    @pytest.mark.parametrize("column", [
        "Potential Temperature (deg C)", "pot_temp", "Theta", "PTMP", "potential_temp",
        "pot_temperature",
    ])
    def test_potential_temperature_is_never_temperature(self, column):
        """Theta is derived; mapping it to code 1 would report the wrong quantity."""
        assert match_variable(column) is None
        assert explain_column(column)["variable"] == "potential_temperature"

    def test_the_refusal_says_what_to_do_instead(self):
        route = explain_column("Potential Temperature")["route"]
        assert "pressure" in route.lower() and "salinity" in route.lower()

    @pytest.mark.parametrize("column,expected", [
        ("Conductivity (S/m)", "conductivity"),
        ("Sigma-t (kg/m3)", "density"),
        ("Sound Velocity (m/s)", "sound_velocity"),
        ("Nitrite (umol/kg)", "nitrite"),
        ("Ammonium", "ammonium"),
        ("Turbidity (NTU)", "turbidity"),
    ])
    def test_unstorable_variables_are_refused_with_a_reason(self, column, expected):
        assert match_variable(column) is None
        assert explain_column(column)["variable"] == expected

    def test_nitrate_plus_nitrite_still_maps(self):
        """Nitrite alone is excluded; nitrate+nitrite is code 8 with header 17."""
        assert match_variable("Nitrate + Nitrite (umol/kg)") == 8
        assert explain_column("Nitrate + Nitrite (umol/kg)") is None

    @pytest.mark.parametrize("column", [
        "Bottom Depth (m)", "Water Depth", "Sounding (m)", "Station Depth",
        "Max Depth (m)", "Sensor Depth", "Depth to bottom",
    ])
    def test_bottom_depth_is_not_the_depth_axis(self, column):
        assert match_variable(column) is None

    def test_the_real_depth_axis_still_resolves(self):
        for column in ("Depth (m)", "depth", "DEPSM", "Depth, m", "z"):
            assert match_variable(column) == 0

    def test_air_temperature_is_not_water_temperature(self):
        assert match_variable("Air Temp (deg C)") == 26
        assert match_variable("Dry Bulb (C)") == 26


class TestTheUnitIsKeptNotJustUsed:
    @pytest.mark.parametrize("column,unit", [
        ("Temperature (deg C)", "deg C"),
        ("DO [ml/l]", "ml/l"),
        ("Depth, m", "m"),
        ("PRES (decibar)", "decibar"),
        ("t090C: Temperature [ITS-90, deg C]", "deg C"),
    ])
    def test_extract_unit_recovers_what_normalisation_drops(self, column, unit):
        assert extract_unit(column) == unit

    @pytest.mark.parametrize("column", [
        "Temperature (secondary sensor)", "Salinity (bottle)", "Temperature (corrected)",
        "Temperature",
    ])
    def test_a_qualifier_in_brackets_is_not_a_unit(self, column):
        assert extract_unit(column) is None

    @pytest.mark.parametrize("column,scale", [
        ("t090C: Temperature [ITS-90, deg C]", "ITS-90"),
        ("Salinity (PSS-78)", "PSS-78"),
        ("Temperature (IPTS-68)", "IPTS-68"),
    ])
    def test_a_scale_is_recognised_and_is_not_a_unit(self, column, scale):
        """A scale is variable-specific header 3, not a unit."""
        assert extract_scale(column) == scale
        assert extract_unit(column) != scale


@pytest.fixture
def frame():
    return pd.DataFrame({
        "Depth (m)": [0, 10, 25],
        "t090C: Temperature [ITS-90, deg C]": [15.8, 15.4, 14.1],
        "Temperature_QC": [1, 1, 3],
        "Nitrate + Nitrite (umol/kg)": [0.2, 0.4, 1.1],
        "Bottom Depth (m)": [2500, 2500, 2500],
    })


HEADER = ("CRUISE: STOCS85A\nSTATION: 001\nPROBE TYPE: T-7\nWIND SPEED: 12.5\n"
          "WATER SAMPLER: Niskin\nACCESSION: 0287423\n"
          "INSTITUTION: University of Alaska Fairbanks")


class TestCastMetadata:
    def test_header_block_keys_become_secondary_headers(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        got = {k: v["value"] for k, v in meta["secondary_headers"].items()}
        # A cruise code names a voyage, not a vessel: character data, never header 3.
        assert "3" not in got
        assert meta["character_data"]["originator_cruise_id"]["value"] == "STOCS85A"
        assert got["7"] == "001"               # originator's station number
        assert got["29"] == "T-7"              # probe type
        assert got["22"] == 12.5               # wind speed, coerced
        assert got["97"] == "Niskin"           # water sampler
        assert got["4"] == "University of Alaska Fairbanks"

    def test_an_identifier_is_not_coerced_to_a_number(self, frame):
        """'0287423' must stay a string to keep its leading zero."""
        meta = map_metadata(HEADER, list(frame.columns), frame)
        assert meta["secondary_headers"]["1"]["value"] == "0287423"

    def test_a_metadata_column_becomes_a_secondary_header(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        assert meta["secondary_headers"]["10"]["value"] == 2500.0
        assert meta["metadata_columns"][0]["column"] == "Bottom Depth (m)"

    def test_a_varying_metadata_column_records_no_single_value(self):
        """A secondary header describes the whole cast, so a varying column gives no value."""
        df = pd.DataFrame({"Depth (m)": [0, 10], "Bottom Depth (m)": [2500, 2650]})
        meta = map_metadata("", list(df.columns), df)
        entry = meta["secondary_headers"]["10"]
        assert entry["value"] is None
        assert "varies down the cast" in entry["note"]

    def test_every_coded_header_carries_its_published_code_list(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        assert meta["secondary_headers"]["29"]["code_table"]["csv"].endswith(
            "s_29_probe_type.csv")
        # Bottom depth is in metres, not a code.
        assert meta["secondary_headers"]["10"]["is_coded"] is False


class TestPerVariableMetadata:
    def test_originator_unit_and_scale_attach_to_the_variable(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        headers = meta["variable_specific_headers"][
            "t090C: Temperature [ITS-90, deg C]"]["headers"]
        assert headers["3"]["value"] == "ITS-90"      # scale
        assert "8" not in headers                     # deg C is the standard unit

    def test_a_converted_unit_is_the_originator_unit(self):
        frame = pd.DataFrame({"Depth (m)": [0, 10], "Oxygen (ml/l)": [6.1, 5.9]})
        meta = map_metadata("", list(frame.columns), frame)
        headers = meta["variable_specific_headers"]["Oxygen (ml/l)"]["headers"]
        assert headers["8"]["value"] == "ml/l"

    def test_nitrate_nitrite_sets_header_17(self, frame):
        """Header 17 is how WOD marks the combined measurement."""
        meta = map_metadata(HEADER, list(frame.columns), frame)
        headers = meta["variable_specific_headers"]["Nitrate + Nitrite (umol/kg)"]["headers"]
        assert headers["17"]["value"] == 1


class TestOriginatorFlags:
    def test_a_flag_column_is_paired_with_its_variable(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        flag = meta["originator_flags"][0]
        assert flag["flag_column"] == "Temperature_QC"
        assert flag["flags_column"] == "t090C: Temperature [ITS-90, deg C]"
        assert flag["flags_wod_code"] == 1

    @pytest.mark.parametrize("name", [
        "Temperature_QC", "QC_Temperature", "Temperature quality flag", "TEMP_QF",
        "flag_temperature",
    ])
    def test_flag_naming_conventions_are_recognised(self, name):
        meta = map_metadata("", ["Temperature", name], None)
        assert meta["originator_flags"][0]["flags_column"] == "Temperature"

    def test_flag_columns_are_not_reported_as_dropped(self, frame):
        spec = query_wod_spec("CTD", list(frame.columns))
        contract = generate_field_mapping(
            {"columns": list(frame.columns), "raw_header_block": HEADER}, spec, df=frame)
        assert "Temperature_QC" not in contract["unmapped_columns"]
        assert "Bottom Depth (m)" not in contract["unmapped_columns"]

    def test_flags_present_implies_secondary_header_96(self, frame):
        meta = map_metadata(HEADER, list(frame.columns), frame)
        assert "96" in meta["secondary_headers"]
        assert meta["secondary_headers"]["96"]["value"] is None      # the flag set is unknown
        assert "not WOD Table 12" in meta["secondary_headers"]["96"]["note"]


class TestCodeTableLinks:
    def test_links_come_from_the_manuals_own_hyperlinks(self):
        links = code_table_links()
        assert len(links["links"]["secondary_header"]) >= 30
        assert links["links"]["secondary_header"]["29"]["csv"].startswith("https://")

    def test_a_field_with_no_code_list_returns_none(self):
        """Bottom depth is in metres, not a code."""
        assert code_table_link("secondary_header", 10) is None

    def test_hubs_include_the_originator_flag_sets(self):
        assert "s_96_origflagset" in code_table_hub("originator_flag_sets")


@pytest.fixture
def cast_file(tmp_path):
    acc = tmp_path / "acc" / "1-ctd"
    acc.mkdir(parents=True)
    (acc / "cast_01.csv").write_text(
        "# Latitude: 34.250\n# Longitude: -120.500\n# Date: 2024-03-04\n"
        "# Cruise: EX24-01\n# Station: 007\n# Probe type: T-7\n# Bottom depth: 2500\n"
        'Depth (m),"t090C: Temperature [ITS-90, deg C]",Temperature_QC,'
        "Salinity (PSS-78),Conductivity (S/m),Potential Temperature (deg C)\n"
        "0,15.82,1,33.41,4.412,15.82\n"
        "1,15.44,1,33.45,4.379,15.43\n"
        "2,14.10,3,33.62,4.271,14.08\n")
    return acc.parent


class TestMetadataReachesTheOutput:
    @pytest.fixture
    def output(self, cast_file, tmp_path):
        res = process_accession(str(cast_file), output_dir=str(tmp_path / "out"),
                                raw_dir=str(tmp_path / "raw"), run_id="t_meta_1",
                                resume=False)
        assert res["counts"].get("done") == 1, res.get("failures")
        return res

    def test_secondary_headers_are_written(self, output):
        with nc.Dataset(output["outputs"][0]) as ds:
            attrs = {a: ds.getncattr(a) for a in ds.ncattrs()}
            assert attrs["originator_cruise_id"] == "EX24-01"
            assert "secondary_header_3_platform_code" not in attrs
            # 'T-7' was not looked up offline, so it is kept as text beside the coded field.
            assert "secondary_header_29_probe_type" not in attrs
            assert attrs["secondary_header_29_probe_type_originator_text"] == "T-7"
            assert "not looked up" in attrs["secondary_header_29_probe_type_code_lookup"]
            assert float(attrs["secondary_header_10_bottom_depth_meters"]) == 2500.0

    def test_a_coded_header_carries_its_code_table_url(self, output):
        with nc.Dataset(output["outputs"][0]) as ds:
            url = ds.getncattr("secondary_header_29_probe_type_code_table")
            assert url.endswith("s_29_probe_type.csv")

    def test_per_variable_metadata_is_written(self, output):
        with nc.Dataset(output["outputs"][0]) as ds:
            t = ds.variables["temperature"]
            assert t.getncattr("measurement_scale") == "ITS-90"
            assert t.getncattr("originator_units") == "deg C"
            assert "t090C" in t.getncattr("originator_column_name")

    def test_originator_flags_are_carried_verbatim(self, output):
        """Originator flag scales differ from WOD's, so values are not translated."""
        with nc.Dataset(output["outputs"][0]) as ds:
            flags = ds.variables["temperature_qc_originator"]
            assert [int(x) for x in flags[:]] == [1, 1, 3]
            assert "not WOD Table 12" in flags.comment
            assert flags.source_column == "Temperature_QC"

    def test_wod_qc_is_off_by_default(self, output):
        """WOD QC is applied downstream."""
        with nc.Dataset(output["outputs"][0]) as ds:
            assert "temperature_qc" not in ds.variables

    def test_wod_qc_can_be_turned_back_on(self, cast_file, tmp_path):
        res = process_accession(str(cast_file), output_dir=str(tmp_path / "out2"),
                                raw_dir=str(tmp_path / "raw2"), run_id="t_meta_2",
                                resume=False, run_qc=True)
        with nc.Dataset(res["outputs"][0]) as ds:
            assert "temperature_qc" in ds.variables
            assert "temperature_qc_originator" in ds.variables

    def test_unstorable_columns_are_dropped_and_named(self, output):
        dropped = " ".join(output["unstorable_columns_dropped"])
        assert "Conductivity" in dropped
        assert "Potential Temperature" in dropped


class TestExcludedFoldersStillCarryMetadata:
    def test_an_adcp_folder_says_what_to_take_from_it(self):
        from wod_etl.discovery import classify_dataset

        verdict = classify_dataset(path="4-adcp/currents.csv")
        assert verdict["is_wod_profile_data"] is False
        harvest = verdict["exclusion"]["harvest"]
        assert "bottom depth" in harvest.lower()
        assert "navigation" in harvest.lower()

    def test_a_bathymetry_folder_points_at_secondary_header_10(self):
        from wod_etl.discovery import classify_dataset

        verdict = classify_dataset(path="6-multibeam/swath.csv")
        assert "secondary header 10" in verdict["exclusion"]["harvest"].lower()


class TestDelimitedHeaderBlock:
    """WOD's own CSV export header: `key,variable,value,code-system,description`."""

    BLOCK = (
        "CAST                        ,,               ,,\n"
        "Originators Station ID      ,,              1,,,integer,\n"
        "Originators Cruise ID       ,,        kah1104,,,\n"
        "Latitude                    ,,        -41.035,decimal degrees,,\n"
        "METADATA,\n"
        "Country                     ,,             NZ,NODC code,NEW ZEALAND\n"
        "Accession Number            ,,               ,NODC code,,\n"
        "Platform                    ,,          5423.,OCL code,KAHAROA (R/V; call sign ZM7552),\n"
        "Institute                   ,,          1344.,NODC code,NIWA; AUCKLAND,\n"
        "Bottom depth                ,,             36,meters,,\n"
        "probe_type                  ,,           464.,WOD code,CTD: SBE 37-IM MicroCAT,\n"
        "Instrument       ,Temperature,           464.,WOD code,CTD: SBE 37-IM MicroCAT,\n"
        "uncalibrated     ,Temperature,             1.,,,\n"
    )

    @pytest.fixture
    def meta(self):
        return map_metadata(header_text=self.BLOCK, columns=[], df=None)

    def test_the_value_is_the_value_not_a_piece_of_the_label(self, meta):
        assert meta["secondary_headers"]["3"]["value"] == "5423."
        assert meta["secondary_headers"]["4"]["value"] == "1344."
        assert meta["secondary_headers"]["29"]["value"] == "464."
        assert meta["secondary_headers"]["10"]["value"] == 36.0

    def test_a_published_code_is_flagged_as_one(self, meta):
        """'464.' is already a WOD code, not a name."""
        probe = meta["secondary_headers"]["29"]
        assert probe["value_is_already_a_code"] is True
        assert "WOD code" in probe["code_system"]
        assert "MicroCAT" in probe["description"]

    def test_column_one_names_the_variable_a_table_5a_header_belongs_to(self, meta):
        """Empty means the key describes the cast; named means it describes one variable."""
        headers = meta["variable_specific_headers"]["Temperature"]["headers"]
        assert headers["5"]["value"] == "464."      # instrument
        assert headers["16"]["value"] == "1."       # uncalibrated

    def test_an_empty_value_column_yields_no_header(self, meta):
        """`Accession Number ,, ,NODC code,,` has no accession number in it."""
        assert "1" not in meta["secondary_headers"]
        blank = [u for u in meta["unresolved"] if u["key"] == "Accession Number"]
        assert blank and blank[0]["value"] is None

    def test_the_country_is_the_primary_header_code(self, meta):
        assert meta["primary_header"]["country"]["value"] == "NZ"

    def test_unplaced_keys_are_never_silent(self):
        meta = map_metadata("Ice ,,0%,,\nSurvey area ,,Arctic Ocean,,\n", [])
        assert {u["key"] for u in meta["unresolved"]} == {"Ice", "Survey area"}
        assert all(u["note"] for u in meta["unresolved"])

    def test_the_cruise_id_is_character_data_not_a_rival_platform(self, meta):
        assert meta["secondary_headers"]["3"]["value"] == "5423."
        assert meta["character_data"]["originator_cruise_id"]["value"] == "kah1104"
        assert not [u for u in meta["unresolved"] if u["key"] == "Originators Cruise ID"]

    def test_a_collision_keeps_both_and_says_which_won(self):
        """Two keys for one header: the coded value wins and the other is recorded."""
        block = ("Ship name                   ,,         Kaharoa,,,\n"
                 "Platform                    ,,          5423.,OCL code,KAHAROA,\n")
        meta = map_metadata(header_text=block, columns=[], df=None)
        assert meta["secondary_headers"]["3"]["value"] == "5423."
        loser = [u for u in meta["unresolved"] if u["key"] == "Ship name"]
        assert loser and "secondary header 3" in loser[0]["matches"]
        assert "published code" in loser[0]["note"]

    def test_a_column_header_row_is_not_read_as_metadata(self):
        """`Depth,Temp,Sal` has the shape of a key/value line and is not one."""
        meta = map_metadata(header_text="Depth,Temp,Sal\n", columns=[], df=None)
        assert not meta["secondary_headers"]
        assert not meta["unresolved"]
