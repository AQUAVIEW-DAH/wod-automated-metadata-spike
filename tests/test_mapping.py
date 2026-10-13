"""Phase 2 and 3: WOD spec lookup, mapping contract, conversions, QC flags.

Several tests pin defects that were fixed; the docstring says which, so a regression is
recognisable rather than just a red test.
"""
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
        """Used to require an exact alias, so 'Temp (C)' matched nothing."""
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
        """_detect_unit_conversion used to be unreachable and always returned 'none'."""
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
        """The regex used to require a decimal point, so 'LAT: 61' was dropped."""
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
        assert "not specified by WOD" in log["seawater_density_provenance"]

    def test_a_rescale_without_endpoints_is_refused_not_guessed(self):
        """`molar_rescale` is parameterised. A contract that names the label but not the
        endpoints must leave the values alone and say so, never assume a direction."""
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
        """Table 12 (2)(a): 1 means duplicate/inversion on the DEPTH scale."""
        df = pd.DataFrame({"Depth": [0., 10., 5., 20., 20.], "Temp": [9.]*5})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert list(out["Depth_qc_flag"]) == [0, 0, 1, 0, 1]

    def test_depth_errors_propagate_to_variable_flags(self):
        """Levels at a bad depth used to pass as 0 = accepted."""
        df = pd.DataFrame({"Depth": [0., 10., 5.], "Temp": [9.0, 8.9, 8.8]})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert out["Temp_qc_flag"].iloc[2] != 0

    def test_three_successive_shallower_depths_flag_the_remainder(self):
        """Manual p.52 rule (b): the signature of two concatenated profiles."""
        df = pd.DataFrame({"Depth": [100., 1., 2., 3., 4.], "Temp": [9.]*5})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert list(out["Depth_qc_flag"])[1:] == [1, 1, 1, 1]

    def test_range_check_is_depth_dependent(self):
        """A single global min/max used to be applied at every depth."""
        df = pd.DataFrame({"Depth": [0., 5000.], "Temp": [25.0, 25.0]})
        out, _ = apply_qc_auditing(df, self.CONTRACT)
        assert out["Temp_qc_flag"].iloc[0] == 0
        assert out["Temp_qc_flag"].iloc[1] == 1

    def test_flag_5_is_reachable(self):
        """Combined gradient and inversion was unreachable: the branches were exclusive."""
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
    """Sea-Bird exports name their columns by sensor, not by variable.

    Every one of these was unmapped before, so a whole CTD cast reached the writer with
    only the columns that happened to be spelled in English.
    """

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
        """DepSM unmapped produced a file with an all-fill depth axis that still validated."""
        assert match_variable(column) == DEPTH_SENTINEL_CODE

    @pytest.mark.parametrize("column", ["C0S/m", "Sigma-t00", "SvDM", "Potemp090C"])
    def test_columns_with_no_wod_code_stay_unmapped(self, column):
        """Conductivity, density, sound velocity and potential temperature have no Table 3a
        code. Mapping potential temperature onto Temperature would duplicate the variable."""
        assert match_variable(column) is None

    @pytest.mark.parametrize("column,code", [
        ("TA (µmol kg−1)", 17), ("SiO2 (µmol L−1)", 6), ("δ18O(‰)", 43),
    ])
    def test_chemistry_spellings(self, column, code):
        assert match_variable(column) == code

    @pytest.mark.parametrize("column", ["NO2−  (µmol L−1)", "NH4+ (µmol L−1)",
                                        "CDOM (m−1)", "Turbidity (FTU)"])
    def test_variables_wod_does_not_store(self, column):
        """Nitrite was excluded from the online database (manual p.49); ammonium, CDOM and
        turbidity have no Table 3a code at all."""
        assert match_variable(column) is None


class TestUnitTraps:
    """Unit errors that passed silently because the pattern was missing."""

    def test_mg_per_litre_oxygen_is_converted(self):
        """Undetected, a mg/L oxygen column was written as if it were already umol/kg."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("DO[mg/L]", 3) == "mg_per_l_to_umol_per_kg"

        df = pd.DataFrame({"DO[mg/L]": [10.4545]})
        contract = {"column_mapping": [{"raw_column": "DO[mg/L]", "wod_code": 3,
                                        "conversion_needed": "mg_per_l_to_umol_per_kg"}]}
        out, log = convert_units_and_types(df, contract)
        # 10.4545 mg/l / 31.9988 g/mol -> 326.7 umol/l -> /1.025 -> 318.7 umol/kg
        assert out["DO[mg/L]"].iloc[0] == pytest.approx(318.7, abs=0.5)

    def test_mg_per_cubic_metre_is_not_read_as_mg_per_litre(self):
        """'mg m-3' is the chlorophyll unit and is identical to the ug/l standard."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("Chlorophyll a (mg m-3)", 11) == "none"

    @pytest.mark.parametrize("code", [17, 21])
    def test_per_mass_source_still_converts_for_per_volume_variables(self, code):
        """Alkalinity and DIC are stored in mmol/l, so a umol/kg source is NOT standard.
        Treating it as already-correct mislabelled both by a factor of ~1000."""
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
        """Code 3's standard unit IS umol/kg -- the per-volume rule must not overreach."""
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        assert _detect_unit_conversion("Oxygen (µmol kg-1)", 3) == "none"


class TestNonProfileQC:
    """Manual p.37: surface-only data are not profile data. p.49: the identifying
    variables get range checks only. Running the vertical checks on a surface track
    flagged 85-100% of every variable, because a constant 0 m depth reads as a duplicate
    depth at every level and therefore an inversion in every variable."""

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
    """The header scanner reads every line of the comment block. Staged files put free-text
    notes above the coordinate lines, so prose that happens to contain 'cast', 'station' or a
    quoted source header must not become a secondary header or a coordinate."""

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
        """Table 4a id 34. An XBT that stops at the sea floor rather than at the probe's
        terminal depth is a different cast, and WOD has a header for saying so."""
        got = self._headers("# Bottom Hit: yes\n")["secondary_headers"]
        assert got["34"]["name"].lower().startswith("bottom hit")
        assert got["34"]["value"] == "yes"


class TestShortAliasesDoNotMatchMidName:
    """A one-character alias matched as a whole word inside a longer name.

    'Sigma-t (kg/m3)' normalises to 'sigma_t', where 't' matched and claimed WOD code 1.
    With a real CTD column set it claimed code 1 first and T090C was then dropped as a
    duplicate, so the NetCDF `temperature` variable held density.
    """

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
    """`[µumn]mol/l` matched mmol/l too, so alkalinity and DIC -- whose Table 3a standard
    unit IS mmol/l -- were read as micromolar and divided by 1000."""

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

    def test_a_molar_source_on_a_non_molar_variable_is_left_alone(self):
        """Chlorophyll's standard unit is ug/l. Rescaling a molar source onto it is
        meaningless, so the values must not be touched."""
        assert self._conv("Chlorophyll (µmol/l)", 11) == "none"

    @pytest.mark.parametrize("col,code,value,expected", [
        ("Alkalinity (mmol/l)", 17, 2.3, 2.3),           # already standard
        ("DIC (µmol/kg)", 21, 2100.0, 2.1525),           # umol/kg -> mmol/l
        ("Oxygen (mmol/l)", 3, 0.28, 273.1707),          # mmol/l -> umol/kg
        ("Nitrate (umol/l)", 8, 25.0, 24.3902),          # umol/l -> umol/kg
    ])
    def test_the_arithmetic_lands_on_the_table_3a_unit(self, col, code, value, expected):
        profile = {"columns": ["Depth (m)", col],
                   "raw_header_block": "LAT: 1.0\nLON: 2.0\nDATE: 2023-01-01"}
        contract = generate_field_mapping(profile, query_wod_spec("OSD", profile["columns"], 10.0))
        out, _ = convert_units_and_types(pd.DataFrame({"Depth (m)": [0.0], col: [value]}), contract)
        assert out[col].iloc[0] == pytest.approx(expected, rel=1e-4)


class TestEachCoordinateIsCheckedOnItsOwnCode:
    def test_a_latitude_column_does_not_satisfy_longitude(self):
        """`in_columns` tested membership of (30, 31) rather than the code being asked
        about, so a file with latitude and no longitude reported ready_for_transform."""
        profile = {"columns": ["Depth (m)", "Latitude", "Temp (deg C)"], "raw_header_block": ""}
        contract = generate_field_mapping(
            profile, query_wod_spec("OSD", profile["columns"], 10.0))
        assert "longitude" in contract["missing_required_coordinates"]
        assert "latitude" not in contract["missing_required_coordinates"]
        assert contract["ready_for_transform"] is False


MICRO = "\u00b5"      # U+00B5 MICRO SIGN -- what a keyboard and most CSVs produce
GREEK_MU = "\u03bc"   # U+03BC GREEK SMALL LETTER MU -- what Word and journal PDFs produce
MINUS = "\u2212"      # U+2212 MINUS SIGN -- pasted exponents, e.g. kg-1


class TestTypographicUnitCharacters:
    """Headers pasted from Word and journals use characters that look ASCII and are not.

    Both failures below were silent: the column mapped to the right WOD code, so nothing
    looked wrong, but the unit was lost and with it the conversion.
    """

    @pytest.mark.parametrize("mu", [MICRO, GREEK_MU])
    @pytest.mark.parametrize("tail", ["/kg", " kg-1", f" kg{MINUS}1"])
    def test_micromolar_per_mass_is_detected_however_it_is_spelt(self, mu, tail):
        from wod_etl.mapping.generate_field_mapping import _detect_unit_conversion
        # DIC is stored in mmol/l, so a micromolar source must rescale. Undetected, the
        # values were written as if already standard -- a factor of ~1000.
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
    """It used to depend on a hand rename to 'Chlorophyll' during staging."""

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
