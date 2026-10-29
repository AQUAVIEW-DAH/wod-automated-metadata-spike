"""Phase 2 and 3: WOD spec lookup, mapping contract, conversions and QC flags."""
import numpy as np
import pandas as pd
import pytest

from wod_etl.mapping import generate_field_mapping, query_wod_spec
from wod_etl.mapping.query_wod_spec import (
    DEPTH_SENTINEL_CODE,
    extract_unit,
    match_variable,
    normalize_name,
    resolve_dataset,
    wod_datasets,
    wod_variables,
)
from wod_etl.qc_transforms import apply_qc_auditing, convert_units_and_types
from wod_etl.qc_transforms.apply_qc_auditing import gradient_limits, range_at_depth


class TestReferenceTables:
    def test_exactly_eleven_datasets(self):
        """Manual Table 2 p.17 defines eleven; XCTD is not one of them."""
        d = wod_datasets()
        assert len(d) == 11
        assert "XCTD" not in d
        assert {"OSD", "CTD", "MBT", "XBT", "SUR", "APB", "MRB", "PFL", "DRB", "UOR", "GLD"} == set(d)

    def test_table_3a_variable_codes_and_units(self):
        v = wod_variables()
        assert v[1]["name"] == "Temperature"
        assert v[3]["standard_unit"] == "Micromole kilogram-1"
        assert v[17]["standard_unit"] == "Millimole liter-1"   # per-volume, not per-mass
        assert {30, 31, 32} <= set(v)                          # lat/lon/julian-day as variables


class TestDatasetResolution:
    def test_xctd_high_resolution_routes_to_ctd(self):
        assert resolve_dataset("XCTD", depth_increment_m=0.5)["wod_dataset_code"] == "CTD"

    def test_xctd_low_resolution_routes_to_osd(self):
        """Manual p.19: routing is by resolution, not instrument."""
        assert resolve_dataset("XCTD", depth_increment_m=5.0)["wod_dataset_code"] == "OSD"

    def test_xctd_without_resolution_is_unresolved_not_guessed(self):
        r = resolve_dataset("XCTD")
        assert r["wod_dataset_code"] is None
        assert r["resolved_by"] == "unresolved"

    def test_unknown_label_is_rejected(self):
        assert resolve_dataset("BANANA")["wod_dataset_code"] is None


class TestColumnMatching:
    @pytest.mark.parametrize("raw,expected", [
        ("Temp (C)", 1), ("Temperature", 1), ("temp_c", 1),
        ("Salinity (PSU)", 2), ("Sal_PSU", 2), ("PSAL", 2),
        ("Oxygen (umol/kg)", 3), ("DOXY", 3),
        ("Depth (m)", DEPTH_SENTINEL_CODE), ("DEPTH", DEPTH_SENTINEL_CODE),
        ("Pressure (dbar)", 25),
    ])
    def test_unit_suffixed_names_match(self, raw, expected):
        assert match_variable(raw) == expected

    def test_air_temperature_is_not_captured_by_temp(self):
        assert match_variable("Air_Temperature") == 26

    def test_unknown_column_returns_none(self):
        assert match_variable("Conductivity_ratio_xyz") is None

    def test_normalization_strips_units(self):
        assert normalize_name("Temp (deg C)") == "temp"
        assert normalize_name("Depth, m") == "depth"


class TestMappingContract:
    def _contract(self, columns, header=""):
        profile = {"source": "t", "columns": columns, "raw_header_block": header}
        spec = query_wod_spec("XCTD", columns, depth_increment_m=0.5)
        return generate_field_mapping(profile, spec)

    def test_real_world_columns_produce_a_non_empty_mapping(self):
        c = self._contract(["Depth (m)", "Temp (C)", "Salinity (PSU)"],
                           "LAT: 61.9 LON: -172.2\nDATE: 1985-06-12")
        assert len(c["column_mapping"]) == 3
        assert c["unmapped_columns"] == []
        assert c["ready_for_transform"] is True

    def test_unit_conversion_is_detected_from_the_column_name(self):
        c = self._contract(["Depth (ft)", "Temp (deg F)", "Oxygen (ml/l)"],
                           "LAT: 61.9 LON: -172.2\nDATE: 1985-06-12")
        conv = {m["raw_column"]: m["conversion_needed"] for m in c["column_mapping"]}
        assert conv["Depth (ft)"] == "feet_to_meters"
        assert conv["Temp (deg F)"] == "fahrenheit_to_celsius"
        assert conv["Oxygen (ml/l)"] == "ml_per_l_to_umol_per_kg"

    def test_per_volume_variables_are_never_molar_converted(self):
        """Alkalinity and DIC are per-volume in Table 3a."""
        c = self._contract(["Depth (m)", "Alkalinity (mmol/l)", "DIC (mmol/l)"],
                           "LAT: 61.9 LON: -172.2\nDATE: 1985-06-12")
        for m in c["column_mapping"]:
            assert m["conversion_needed"] != "molar_rescale"

    def test_integer_latitude_is_parsed(self):
        c = self._contract(["Depth (m)", "Temp (C)"], "LAT: 61 LON: -172\nDATE: 1985-06-12")
        assert c["parsed_coordinates"]["latitude"] == 61.0
        assert c["parsed_coordinates"]["longitude"] == -172.0

    def test_hemisphere_suffix_is_applied(self):
        c = self._contract(["Depth (m)"], "LAT: 33.5 S LON: 18.4 E\nDATE: 1985-06-12")
        assert c["parsed_coordinates"]["latitude"] == -33.5
        assert c["parsed_coordinates"]["longitude"] == 18.4

    def test_missing_position_is_reported(self):
        c = self._contract(["Depth (m)", "Temp (C)"], "CRUISE: X")
        assert "latitude" in c["missing_required_coordinates"]
        assert c["ready_for_transform"] is False

    def test_accession_keeps_its_leading_zero(self):
        c = self._contract(["Depth (m)"], "ACCESSION: 0287423\nLAT: 1 LON: 1\nDATE: 1985-06-12")
        assert c["extracted_header_metadata"]["1"]["value"] == "0287423"


class TestConversions:
    def _run(self, df, mapping):
        return convert_units_and_types(df, {"column_mapping": mapping})

    def test_feet_and_fahrenheit(self):
        df, _ = self._run(
            pd.DataFrame({"D": [32.8], "T": [69.8]}),
            [{"raw_column": "D", "wod_code": 0, "conversion_needed": "feet_to_meters"},
             {"raw_column": "T", "wod_code": 1, "conversion_needed": "fahrenheit_to_celsius"}])
        assert df["D"].iloc[0] == pytest.approx(9.99744)
        assert df["T"].iloc[0] == pytest.approx(21.0)

    def test_ml_per_l_to_umol_per_kg_uses_the_table_3b_constant(self):
        df, log = self._run(
            pd.DataFrame({"O": [4.70]}),
            [{"raw_column": "O", "wod_code": 3, "conversion_needed": "ml_per_l_to_umol_per_kg"}])
        # 4.70 ml/l / 22.3916 l/mol * 1000 / 1.025 kg/l
        assert df["O"].iloc[0] == pytest.approx(4.70 / 22.3916 * 1000 / 1.025, rel=1e-6)
        assert log["transformations_applied"][0]["constant"] == pytest.approx(22.3916)

    def test_density_assumption_is_recorded(self):
        _, log = self._run(
            pd.DataFrame({"P": [2.0]}),
            [{"raw_column": "P", "wod_code": 4, "conversion_needed": "molar_rescale",
              "conversion_from": "umol/l", "conversion_to": "umol/kg"}])
        assert log["seawater_density_kg_m3"] == 1025.0
        assert "manual p.15" in log["seawater_density_provenance"]

    def test_a_rescale_without_endpoints_is_refused_not_guessed(self):
        """Without from/to units the direction is unknown, so values are left alone."""
        df, log = self._run(
            pd.DataFrame({"A": [2.3]}),
            [{"raw_column": "A", "wod_code": 17, "conversion_needed": "molar_rescale"}])
        assert df["A"].iloc[0] == 2.3
        assert any("Cannot rescale" in w for w in log["warnings"])


class TestQCAuditing:
    CONTRACT = {"wod_dataset_code": "OSD", "column_mapping": [
        {"raw_column": "Depth", "wod_code": 0},
        {"raw_column": "Temp", "wod_code": 1}]}

    def test_depth_flags_use_the_depth_scale(self):
        """Table 12 (2)(a): 1 means duplicate/inversion on the depth scale."""
        df = pd.DataFrame({"Depth": [0., 10., 5., 20., 20.], "Temp": [9.]*5})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert list(out["Depth_qc_flag"]) == [0, 0, 1, 0, 1]

    def test_depth_errors_propagate_to_variable_flags(self):
        df = pd.DataFrame({"Depth": [0., 10., 5.], "Temp": [9.0, 8.9, 8.8]})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert out["Temp_qc_flag"].iloc[2] != 0

    def test_three_successive_shallower_depths_flag_the_remainder(self):
        """Manual p.52 rule (b): the signature of two concatenated profiles."""
        df = pd.DataFrame({"Depth": [100., 1., 2., 3., 4.], "Temp": [9.]*5})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert list(out["Depth_qc_flag"])[1:] == [1, 1, 1, 1]

    def test_range_check_is_depth_dependent(self):
        df = pd.DataFrame({"Depth": [0., 5000.], "Temp": [25.0, 25.0]})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert out["Temp_qc_flag"].iloc[0] == 0
        assert out["Temp_qc_flag"].iloc[1] == 1

    def test_flag_5_is_reachable(self):
        """Flag 5 is a combined gradient and inversion failure."""
        seen = set()
        rng = np.random.default_rng(0)
        for _ in range(800):
            df = pd.DataFrame({"Depth": np.sort(rng.uniform(0, 800, 6)),
                               "Temp": rng.uniform(-40, 80, 6)})
            out, _ = apply_qc_auditing(df, self.CONTRACT)
            seen.update(int(x) for x in out["Temp_qc_flag"])
        assert 5 in seen

    def test_mgv_and_miv_are_different_thresholds(self):
        """Table 13: temperature MGV 0.700 but MIV 0.300."""
        lim = gradient_limits(1, 100.0)
        assert lim["MGV"] == pytest.approx(0.700)
        assert lim["MIV"] == pytest.approx(0.300)

    def test_gradient_limits_tighten_below_400m(self):
        assert gradient_limits(2, 100.0)["MGV"] == pytest.approx(9.0)
        assert gradient_limits(2, 1000.0)["MGV"] == pytest.approx(0.05)

    def test_range_lookup_uses_nearest_shallower_level_on_a_tie(self):
        assert range_at_depth(1, 5.0) == range_at_depth(1, 0.0)

    def test_flags_stay_within_the_table_12_range(self):
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"Depth": np.sort(rng.uniform(0, 6000, 40)),
                           "Temp": rng.uniform(-50, 90, 40)})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert out["Temp_qc_flag"].between(0, 9).all()

    def test_no_depth_column_is_reported_not_crashed(self):
        df = pd.DataFrame({"Temp": [9.0, 8.9]})
        out, summary = apply_qc_auditing(df, {"column_mapping": [{"raw_column": "Temp", "wod_code": 1}]})
        assert "Temp_qc_flag" in out
        assert any("No depth column" in n for n in summary["notes"])


class TestInstrumentExportNames:
    """Sea-Bird exports name their columns by sensor, not by variable."""

    @pytest.mark.parametrize("column,code", [
        ("PrDM", 25), ("PrdM", 25),            # pressure, digiquartz
        ("T090C", 1), ("Tv290C", 1),           # primary / SBE-19 temperature
        ("Sal00", 2), ("Sal11", 2),            # primary / secondary salinity
        ("Sbeox0ML/L", 3),                     # SBE-43 oxygen
        ("FlSP", 11),                          # Seapoint fluorometer
    ])
    def test_sea_bird_names_map(self, column, code):
        assert match_variable(column) == code

    @pytest.mark.parametrize("column", ["DepSM", "DepSM (m)", "DepFM"])
    def test_sea_bird_depth_names_are_the_depth_axis(self, column):
        """Unmapped, these leave an all-fill depth axis that still validates."""
        assert match_variable(column) == DEPTH_SENTINEL_CODE

    @pytest.mark.parametrize("column", ["C0S/m", "Sigma-t00", "SvDM", "Potemp090C"])
    def test_columns_with_no_wod_code_stay_unmapped(self, column):
        """No Table 3a code; potential temperature must not map onto Temperature."""
        assert match_variable(column) is None

    @pytest.mark.parametrize("column,code,scale", [
        ("T068", 1, "IPTS-68"), ("T168", 1, "IPTS-68"), ("t068C", 1, "IPTS-68"),
        ("T090", 1, "ITS-90"), ("OxML/L", 3, None), ("OxMg/L", 3, None),
    ])
    def test_old_seasoft_names_map_with_their_scale(self, column, code, scale):
        """Accession 0054244's ASCII exports name columns 'Pr T068 Sal00 OxML/L'."""
        from wod_etl.mapping.query_wod_spec import extract_scale

        assert match_variable(column) == code
        assert extract_scale(column) == scale

    @pytest.mark.parametrize("column,conversion", [
        ("OxML/L", "ml_per_l_to_umol_per_kg"), ("Sbeox0Mg/L", "mg_per_l_to_umol_per_kg"),
        ("Sbeox0Mm/Kg", "none"),                 # Sea-Bird 'Mm' is micromoles
        ("Sbeox0Mm/L", "molar_rescale"),          # per volume to per mass
    ])
    def test_a_sea_bird_oxygen_name_carries_its_unit(self, column, conversion):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion

        assert match_variable(column) == 3
        assert _detect_unit_conversion(column, 3) == conversion
        assert extract_unit(column) == column[-5:].lstrip("0x")

    @pytest.mark.parametrize("column,refused", [
        ("OxC", None), ("OxTC", None), ("sbeox0V", None),      # sensor current, temperature, volts
        ("t090F", None),                                         # Fahrenheit: not converted
        ("Xmiss", None), ("WetChConc", None),
        ("sbeox0PS", "oxygen_saturation"), ("oxsatML/L", "oxygen_saturation"),
        ("Potemp068", "potential_temperature"), ("C1mS/cm", "conductivity"),
    ])
    def test_raw_and_derived_seasoft_channels_are_not_written(self, column, refused):
        from wod_etl.mapping.query_wod_spec import explain_column

        assert match_variable(column) is None
        assert (explain_column(column) or {}).get("variable") == refused

    @pytest.mark.parametrize("column,code", [
        ("TA (µmol kg−1)", 17), ("SiO2 (µmol L−1)", 6), ("δ18O(‰)", 43),
    ])
    def test_chemistry_spellings(self, column, code):
        assert match_variable(column) == code

    @pytest.mark.parametrize("column", ["NO2−  (µmol L−1)", "NH4+ (µmol L−1)",
                                        "CDOM (m−1)", "Turbidity (FTU)"])
    def test_variables_wod_does_not_store(self, column):
        """Nitrite is excluded from the online database (manual p.49); the rest have no code."""
        assert match_variable(column) is None


class TestUnitTraps:
    def test_mg_per_litre_oxygen_is_converted(self):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("DO[mg/L]", 3) == "mg_per_l_to_umol_per_kg"

        df = pd.DataFrame({"DO[mg/L]": [10.4545]})
        contract = {"column_mapping": [{"raw_column": "DO[mg/L]", "wod_code": 3,
                                        "conversion_needed": "mg_per_l_to_umol_per_kg"}]}
        out, log = convert_units_and_types(df, contract)
        # 10.4545 mg/l / 31.9988 g/mol -> 326.7 umol/l -> /1.025 -> 318.7 umol/kg
        assert out["DO[mg/L]"].iloc[0] == pytest.approx(318.7, abs=0.5)

    @pytest.mark.parametrize("col", ["Chlorophyll (mg/l)", "Chlorophyll a [mg/L]",
                                     "Chl (mg l-1)"])
    def test_mg_per_litre_chlorophyll_is_rescaled_to_ug_per_litre(self, col):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion(col, 11) == "mg_per_l_to_ug_per_l"

    def test_mg_per_cubic_metre_is_not_read_as_mg_per_litre(self):
        """'mg m-3' is the chlorophyll unit and is identical to the ug/l standard."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("Chlorophyll a (mg m-3)", 11) == "none"

    @pytest.mark.parametrize("code", [17, 21])
    def test_per_mass_source_still_converts_for_per_volume_variables(self, code):
        """Alkalinity and DIC are stored in mmol/l, so a umol/kg source is rescaled."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion, molar_units
        assert _detect_unit_conversion("DIC (µmol kg−1)", code) == "molar_rescale"
        assert molar_units("DIC (µmol kg−1)", code) == (("umol", "mass"), ("mmol", "volume"))

        df = pd.DataFrame({"DIC (µmol kg−1)": [2088.8]})
        contract = {"column_mapping": [{"raw_column": "DIC (µmol kg−1)", "wod_code": code,
                                        "conversion_needed": "molar_rescale",
                                        "conversion_from": "umol/kg",
                                        "conversion_to": "mmol/l"}]}
        out, _ = convert_units_and_types(df, contract)
        assert out["DIC (µmol kg−1)"].iloc[0] == pytest.approx(2.141, abs=0.001)

    def test_oxygen_in_umol_per_kg_needs_no_conversion(self):
        """Code 3's standard unit is umol/kg."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("Oxygen (µmol kg-1)", 3) == "none"


class TestNonProfileQC:
    """Surface-only data get range checks only, not vertical checks (manual p.37, p.49)."""

    @staticmethod
    def _surface_track():
        df = pd.DataFrame({"Depth (m)": [0.0] * 5,
                           "water temperature (deg C)": [5.1, 5.2, 5.0, 5.3, 5.1],
                           "latitude": [62.1, 62.2, 62.3, 62.4, 62.5]})
        contract = {"wod_dataset_code": "SUR", "column_mapping": [
            {"raw_column": "Depth (m)", "wod_code": DEPTH_SENTINEL_CODE},
            {"raw_column": "water temperature (deg C)", "wod_code": 1},
            {"raw_column": "latitude", "wod_code": 30}]}
        return df, contract

    def test_constant_surface_depth_is_not_a_depth_error(self):
        df, contract = self._surface_track()
        out, summary = apply_qc_auditing(df, contract)
        assert set(out["Depth (m)_qc_flag"]) == {0}
        assert summary["vertical_checks_applied"] is False
        assert "depth_inversion_duplication" not in summary["checks_applied"]

    def test_variables_are_not_flagged_as_inversions(self):
        df, contract = self._surface_track()
        out, _ = apply_qc_auditing(df, contract)
        assert set(out["water temperature (deg C)_qc_flag"]) == {0}

    def test_range_checks_still_run_on_surface_data(self):
        """Range is the one check the manual keeps for these datasets."""
        df, contract = self._surface_track()
        df.loc[2, "water temperature (deg C)"] = 900.0
        out, summary = apply_qc_auditing(df, contract)
        assert out["water temperature (deg C)_qc_flag"].iloc[2] != 0
        assert "broad_range_by_depth" in summary["checks_applied"]

    def test_profile_datasets_keep_every_vertical_check(self):
        df = pd.DataFrame({"Depth (m)": [0.0, 10.0, 10.0], "Temp": [5.0, 4.9, 4.8]})
        contract = {"wod_dataset_code": "CTD", "column_mapping": [
            {"raw_column": "Depth (m)", "wod_code": DEPTH_SENTINEL_CODE},
            {"raw_column": "Temp", "wod_code": 1}]}
        out, summary = apply_qc_auditing(df, contract)
        assert summary["vertical_checks_applied"] is True
        assert "depth_inversion_duplication" in summary["checks_applied"]
        assert out["Depth (m)_qc_flag"].iloc[2] != 0     # genuine duplicate depth


class TestHeaderMiningIsNotProseMining:
    @staticmethod
    def _headers(header_text):
        from wod_etl.mapping.generate_field_mapping import _parse_header_block
        return _parse_header_block(header_text)

    def test_cast_in_a_sentence_is_not_a_cast_number(self):
        text = ("# Note: verified that the two clocks agree (cast positions fall on the "
                "SADCP track to 0.4 km)\n# Station: BT-01\n")
        got = self._headers(text)["secondary_headers"]
        assert "5" not in got
        assert got["7"]["value"] == "BT-01"

    def test_a_quoted_source_header_in_a_note_does_not_set_the_date(self):
        text = ("# Note: position and time from the source header: 20230303 2102 "
                "21-31.16N 144-01.84E\n"
                "LAT: 18.096167\nLON: 147.101667\nDATE: 2023-03-18\nTIME: 02:30\n")
        coords = self._headers(text)["coordinates"]
        assert coords["latitude"] == pytest.approx(18.096167)
        assert coords["longitude"] == pytest.approx(147.101667)
        assert (coords["year"], coords["month"], coords["day"]) == (2023, 3, 18)
        assert (coords["hour"], coords["minute"]) == (2, 30)

    def test_real_key_value_lines_still_resolve(self):
        text = ("# Accession: 0283273\n# Platform: Kaimei\n# Institute: JAMSTEC\n"
                "# Station: BT-202303180229\n# Bottom Depth: 1409.3\n")
        got = self._headers(text)["secondary_headers"]
        assert got["1"]["value"] == "0283273"
        assert got["3"]["value"] == "Kaimei"
        assert got["4"]["value"] == "JAMSTEC"
        assert got["10"]["value"] == pytest.approx(1409.3)

    def test_keys_separated_by_a_column_gap_still_resolve(self):
        got = self._headers("STATION 12    BOTTOM DEPTH 2500\n")["secondary_headers"]
        assert got["7"]["value"] == "12"
        assert got["10"]["value"] == pytest.approx(2500.0)

    def test_bottom_hit_is_a_recognised_secondary_header(self):
        """Table 4a id 34: an XBT that stopped at the sea floor."""
        got = self._headers("# Bottom Hit: yes\n")["secondary_headers"]
        assert got["34"]["name"].lower().startswith("bottom hit")
        assert got["34"]["value"] == "yes"


class TestShortAliasesDoNotMatchMidName:
    @pytest.mark.parametrize("column", ["Sigma-t (kg/m3)", "sigma_t", "pot_t", "cond_t"])
    def test_a_t_inside_a_name_is_not_temperature(self, column):
        assert match_variable(column) != 1

    @pytest.mark.parametrize("column,code", [
        ("T (deg C)", 1), ("t", 1), ("T090C", 1), ("Temperature", 1),
        ("DO (mg/L)", 3), ("TA (umol/kg)", 17), ("pH", 9), ("Si (umol/kg)", 6),
    ])
    def test_a_column_spelt_as_a_short_alias_still_resolves(self, column, code):
        assert match_variable(column) == code

    @pytest.mark.parametrize("column,code", [("o2_umol", 3), ("do_mg_l", 3), ("ta_umol_kg", 17)])
    def test_two_character_symbols_still_match_as_tokens(self, column, code):
        """Chemistry symbols do not occur as incidental tokens, and 'o2_umol' is real."""
        assert match_variable(column) == code

    def test_density_does_not_displace_temperature_in_a_full_contract(self):
        profile = {"columns": ["PrDM", "T090C", "Sigma-t (kg/m3)", "Sal00"],
                   "raw_header_block": "LAT: 61.9\nLON: -172.2\nDATE: 2023-06-20"}
        spec = query_wod_spec("CTD", profile["columns"], 1.0)
        contract = generate_field_mapping(profile, spec)
        by_code = {m["wod_code"]: m["raw_column"] for m in contract["column_mapping"]}
        assert by_code[1] == "T090C"
        assert "Sigma-t (kg/m3)" in contract["unmapped_columns"]


class TestMolarPrefixIsNotConflated:
    @staticmethod
    def _conv(col, code):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        return _detect_unit_conversion(col, code)

    @pytest.mark.parametrize("code", [17, 21])
    def test_mmol_per_l_is_already_standard_for_alkalinity_and_dic(self, code):
        assert self._conv("Alkalinity (mmol/l)", code) == "none"

    def test_mmol_and_umol_are_told_apart_for_oxygen(self):
        from wod_etl.mapping.generate_field_mapping import molar_units
        assert molar_units("Oxygen (mmol/l)", 3)[0] == ("mmol", "volume")
        assert molar_units("Oxygen (umol/l)", 3)[0] == ("umol", "volume")

    def test_nanomolar_noble_gases_are_not_treated_as_micromolar(self):
        """Helium, neon and argon are stored in nmol/kg, not umol/kg."""
        from wod_etl.mapping.generate_field_mapping import molar_units
        assert self._conv("Helium (nmol/kg)", 34) == "none"
        assert molar_units("Helium (nmol/l)", 34) == (("nmol", "volume"), ("nmol", "mass"))

    def test_molar_chlorophyll_is_refused_not_written(self):
        """Chlorophyll is stored in ug/l, and the manual gives no molar mass to convert with."""
        profile = {"columns": ["Depth (m)", "Chlorophyll (µmol/l)"],
                   "raw_header_block": "LAT: 1.0\nLON: 2.0\nDATE: 2023-01-01"}
        contract = generate_field_mapping(profile, query_wod_spec("OSD", profile["columns"], 10.0))
        assert 11 not in {m["wod_code"] for m in contract["column_mapping"]}
        [d] = [d for d in contract["dropped_columns"] if d["column"] == "Chlorophyll (µmol/l)"]
        assert d["kind"] == "unit_not_convertible" and "molar-to-mass" in d["reason"]
        assert any("Chlorophyll (µmol/l)" in w for w in contract["warnings"])

    def test_a_mass_unit_column_beside_a_molar_one_is_written(self):
        profile = {"columns": ["Depth (m)", "Chlorophyll (umol/l)", "Chlorophyll (ug/l)"],
                   "raw_header_block": "LAT: 1.0\nLON: 2.0\nDATE: 2023-01-01"}
        contract = generate_field_mapping(profile, query_wod_spec("OSD", profile["columns"], 10.0))
        assert [m["raw_column"] for m in contract["column_mapping"]
                if m["wod_code"] == 11] == ["Chlorophyll (ug/l)"]

    @pytest.mark.parametrize("col,code,refused", [
        ("Chlorophyll (nmol/l)", 11, True), ("Chlorophyll (mg m-3)", 11, False),
        ("Chlorophyll (ug/l)", 11, False), ("Oxygen (umol/l)", 3, False),
        ("CFC-11 (pmol/kg)", 40, False), ("Helium (nmol/kg)", 34, False)])
    def test_only_a_molar_unit_on_a_mass_standard_is_refused(self, col, code, refused):
        from wod_etl.mapping.generate_field_mapping import molar_to_mass
        assert molar_to_mass(col, code) is refused

    @pytest.mark.parametrize("col,code,value,expected", [
        ("Alkalinity (mmol/l)", 17, 2.3, 2.3),           # already standard
        ("DIC (µmol/kg)", 21, 2100.0, 2.1525),           # umol/kg -> mmol/l
        ("Oxygen (mmol/l)", 3, 0.28, 273.1707),          # mmol/l -> umol/kg
        ("Nitrate (umol/l)", 8, 25.0, 24.3902),          # umol/l -> umol/kg
        ("Chlorophyll (mg/l)", 11, 0.0018, 1.8),         # mg/l -> ug/l
        ("Chlorophyll (mg m-3)", 11, 1.8, 1.8),          # mg/m3 is ug/l
    ])
    def test_the_arithmetic_lands_on_the_table_3a_unit(self, col, code, value, expected):
        profile = {"columns": ["Depth (m)", col],
                   "raw_header_block": "LAT: 1.0\nLON: 2.0\nDATE: 2023-01-01"}
        contract = generate_field_mapping(profile, query_wod_spec("OSD", profile["columns"], 10.0))
        out, _ = convert_units_and_types(pd.DataFrame({"Depth (m)": [0.0], col: [value]}), contract)
        assert out[col].iloc[0] == pytest.approx(expected, rel=1e-4)


class TestEachCoordinateIsCheckedOnItsOwnCode:
    def test_a_latitude_column_does_not_satisfy_longitude(self):
        profile = {"columns": ["Depth (m)", "Latitude", "Temp (deg C)"], "raw_header_block": ""}
        contract = generate_field_mapping(
            profile, query_wod_spec("OSD", profile["columns"], 10.0))
        assert "longitude" in contract["missing_required_coordinates"]
        assert "latitude" not in contract["missing_required_coordinates"]
        assert contract["ready_for_transform"] is False


class TestDeclaredIdentity:
    """A NetCDF variable's CF standard_name or EPIC code outranks its name."""

    @pytest.mark.parametrize("attrs,label", [
        ({"standard_name": "sea_water_temperature"}, "Temperature"),
        ({"standard_name": "sea_water_potential_temperature"}, "Potential temperature"),
        ({"standard_name": "moles_of_nitrate_and_nitrite_per_unit_mass_in_sea_water"},
         "Nitrate + Nitrite"),
        ({"epic_code": 28}, "Temperature"), ({"epic_code": "41"}, "Salinity"),
        ({"epic_key": "OST_62"}, "Oxygen saturation"),
        ({"standard_name": "sea_water_temperature status_flag"}, None),
        ({"epic_code": 99999}, None), ({}, None)])
    def test_declared_label(self, attrs, label):
        from wod_etl.mapping.query_wod_spec import declared_label
        assert declared_label(attrs) == label

    @pytest.mark.parametrize("label,attrs,units,code", [
        ("TEMP (degC)", {"standard_name": "sea_water_potential_temperature"}, "degC", None),
        ("X_1 (C)", {"epic_code": 20}, "C", 1),
        ("Oxygen (%)", {"standard_name": "fractional_saturation_of_oxygen_in_sea_water"},
         "%", None),
        ("FLUOR (mg m-3)",
         {"standard_name": "mass_concentration_of_chlorophyll_a_in_sea_water"}, "mg m-3", 11)])
    def test_the_declaration_decides_the_code(self, label, attrs, units, code):
        from wod_etl.mapping.query_wod_spec import prefer_declared
        assert match_variable(prefer_declared(label, attrs, units)) == code

    def test_a_name_that_agrees_keeps_the_originators_name(self):
        from wod_etl.mapping.query_wod_spec import prefer_declared
        assert prefer_declared("PSAL", {"standard_name": "sea_water_practical_salinity"}) \
            == "PSAL"

    @pytest.mark.parametrize("col,code,refused", [
        ("Partial pressure of carbon dioxide (uatm)", 20, None),
        ("Absolute Salinity (g/kg)", None, "teos10_salinity"),
        ("Conservative Temperature (degC)", None, "conservative_temperature")])
    def test_names_that_resolved_to_the_wrong_variable(self, col, code, refused):
        from wod_etl.mapping import explain_column
        assert match_variable(col) == code
        assert (explain_column(col) or {}).get("variable") == refused


MICRO = "\u00b5"      # micro sign, common in CSVs
GREEK_MU = "\u03bc"   # Greek small letter mu, from Word and PDFs
MINUS = "\u2212"      # minus sign, in pasted exponents


class TestTypographicUnitCharacters:
    """Headers pasted from Word or journals use non-ASCII look-alike characters."""

    @pytest.mark.parametrize("mu", [MICRO, GREEK_MU])
    @pytest.mark.parametrize("tail", ["/kg", " kg-1", f" kg{MINUS}1"])
    def test_micromolar_per_mass_is_detected_however_it_is_spelt(self, mu, tail):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        # DIC is stored in mmol/l, so a micromolar source must rescale.
        assert _detect_unit_conversion(f"DIC ({mu}mol{tail})", 21) == "molar_rescale"

    @pytest.mark.parametrize("mu", [MICRO, GREEK_MU])
    @pytest.mark.parametrize("tail", ["/L", " L-1", f" L{MINUS}1"])
    def test_micromolar_per_volume_is_detected_however_it_is_spelt(self, mu, tail):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion(f"NO3 ({mu}mol{tail})", 8) == "molar_rescale"

    @pytest.mark.parametrize("name", [f"DIC ({MICRO}mol kg{MINUS}1)",
                                      f"DIC ({GREEK_MU}mol/kg)",
                                      f"NO3 ({MICRO}mol L{MINUS}1)"])
    def test_the_unit_is_recorded_verbatim(self, name):
        """Header 8 is the originator's own text, so the fold is for matching only."""
        unit = extract_unit(name)
        assert unit is not None and unit == name[name.index("(") + 1:-1]

    def test_a_symbol_only_unit_is_still_a_unit(self):
        """Per mille is the Table 3a standard unit for Oxygen-18 and has no letters."""
        assert extract_unit("d18O (\u2030)") == "\u2030"

    def test_a_qualifier_is_still_not_a_unit(self):
        assert extract_unit("Temperature (secondary sensor)") is None


class TestFluorescenceIsDecidedByItsUnit:
    @pytest.mark.parametrize("col", ["Fluorescence (ug/l)", "fluorescence (mg/m^3)",
                                     "Fluor [ppb]", "Fl (µg/L)", "FlSP", "flECO-AFL"])
    def test_a_concentration_is_chlorophyll(self, col):
        assert match_variable(col) == 11

    @pytest.mark.parametrize("col", ["Fluorescence (RFU)", "chl_fluorescence (V)",
                                     "Fluorometer (volts)", "fluorescence"])
    def test_a_raw_signal_or_no_unit_is_dropped_with_a_reason(self, col):
        from wod_etl.mapping import explain_column

        assert match_variable(col) is None
        why = explain_column(col)
        assert why["variable"] == "fluorescence" and why["do_not_alias"]

    def test_fluoride_is_not_fluorescence(self):
        assert match_variable("Fluoride (umol/kg)") is None

    def test_the_contract_says_it_is_fluorometer_derived(self):
        profile = {"source": "x", "raw_header_block": "LAT: 21\nLON: 142\nDATE: 2015-11-09",
                   "columns": ["Depth (m)", "Fluorescence (ug/l)"]}
        spec = query_wod_spec("CTD", profile["columns"], depth_increment_m=1.0)
        contract = generate_field_mapping(profile, spec)
        chl = next(m for m in contract["column_mapping"] if m["wod_code"] == 11)
        assert "in-situ fluorometer" in chl["comment"]
        assert any("not extracted chlorophyll" in w for w in contract["warnings"])


class TestTable3aDecidesWhatEachDatasetStores:
    def test_a_variable_the_dataset_does_not_store_is_left_out_and_said(self):
        """MBT stores temperature only: an SV&T probe's computed salinity is not written."""
        columns = ["Depth (m)", "Temp (C)", "Salinity (PSU)"]
        profile = {"source": "t", "columns": columns,
                   "raw_header_block": "LAT: 27.7 LON: -96.1\nDATE: 2025-02-01"}
        c = generate_field_mapping(profile, query_wod_spec("MBT", columns))
        assert [m["raw_column"] for m in c["column_mapping"]] == ["Depth (m)", "Temp (C)"]
        dropped = [d for d in c["dropped_columns"] if d.get("kind") == "not_in_dataset"]
        assert dropped[0]["column"] == "Salinity (PSU)"
        assert "not in MBT (Table 3a)" in dropped[0]["reason"]

    def test_a_cast_date_labelled_time_is_read(self):
        """Expedition formats write the cast's date as `Time: 2009. 8.18`."""
        from wod_etl.mapping.generate_field_mapping import parse_header_block

        coords = parse_header_block("Stn: KD0309 ;  Lat: 81.671N;  Time: 2009. 8.18;")[
            "coordinates"]
        assert (coords["year"], coords["month"], coords["day"]) == (2009, 8, 18)
        assert "hour" not in coords


class TestTheRangeLookupIsTheManualsRuleForAnyNumberOfDepths:
    def test_the_vectorised_lookup_agrees_with_the_scalar_one(self):
        """Nearest listed level, the shallower on a tie, the deepest for all below it."""
        from wod_etl.qc_transforms.apply_qc_auditing import range_at_depth, ranges_at_depths

        z = np.array([0, 2.4, 5, 7.5, 12.5, 999, 5500, 9000, np.nan])
        lo, hi = ranges_at_depths(1, z)
        assert list(zip(lo, hi)) == [range_at_depth(1, float(d)) for d in z]
        assert ranges_at_depths(25, z) is None or len(ranges_at_depths(25, z)[0]) == z.size


class TestNamesFromAccession0303809:
    @pytest.mark.parametrize("column, code", [
        ("DPTH (meters)", 0), ("OXY (ml/l)", 3), ("PRES (decibar = 10000 pascals)", 25)])
    def test_they_map(self, column, code):
        from wod_etl.mapping.query_wod_spec import match_variable

        assert match_variable(column) == code

    @pytest.mark.parametrize("column", ["Oxygen saturation (%)", "OXY_SAT (%)", "O2sat"])
    def test_oxygen_saturation_is_not_oxygen(self, column):
        from wod_etl.mapping.query_wod_spec import explain_column, match_variable

        assert match_variable(column) is None
        assert explain_column(column)["variable"] == "oxygen_saturation"

    def test_pressure_with_an_explained_decibar_unit_becomes_depth(self):
        import pandas as pd

        from wod_etl.orchestration.process_accession import _depth_from_pressure

        df = pd.DataFrame({"PRES (decibar = 10000 pascals)": [0.0, 100.0],
                           "TEMP (degC)": [20.0, 10.0]})
        out, note = _depth_from_pressure(df, "Latitude: 8.5")
        assert note and len(out.columns) == 3
