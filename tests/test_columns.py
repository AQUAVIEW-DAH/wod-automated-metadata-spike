"""Resolving and renaming columns.

A column name carries the WOD variable code, unit and scale; a rename must keep all three.
"""
import pytest

from wod_etl.mapping import generate_field_mapping, resolve_column
from wod_etl.staging import rename_columns, suggest_column_names


class TestResolveColumn:
    """Find a column from a variable name without knowing the file's spelling."""

    def test_a_wod_variable_name_finds_the_instrument_spelling(self):
        r = resolve_column("latitude", ["LAT", "LON", "Depth (m)"])
        assert r["column"] == "LAT"
        assert r["matched_by"] == "wod_code"
        assert r["wod_code"] == 30

    def test_an_exact_name_wins_without_needing_a_code(self):
        r = resolve_column("Station", ["Station", "Depth (m)"])
        assert (r["column"], r["matched_by"]) == ("Station", "exact")

    def test_normalization_handles_case_and_bracketed_units(self):
        r = resolve_column("latitude", ["Latitude (degree N)", "Depth"])
        assert r["column"] == "Latitude (degree N)"
        assert r["matched_by"] == "normalized"

    @pytest.mark.parametrize("query,columns,code", [
        ("temperature", ["T090C", "Depth (m)"], 1),
        ("oxygen", ["Sbeox0ML/L", "Depth (m)"], 3),
        ("salinity", ["Sal00", "Depth (m)"], 2),
    ])
    def test_finds_sea_bird_names_through_the_code(self, query, columns, code):
        r = resolve_column(query, columns)
        assert r["column"] == columns[0] and r["wod_code"] == code

    def test_two_sensors_on_one_code_is_ambiguous_not_a_guess(self):
        """Primary and secondary sensors share code 1; picking one would drop data."""
        r = resolve_column("temperature", ["t090C", "t190C", "sal00"])
        assert r["column"] is None
        assert r["ambiguous"] is True
        assert set(r["candidates"]) == {"t090C", "t190C"}
        assert "primary and secondary" in r["note"]

    def test_a_variable_wod_does_not_store_is_refused_with_its_reason(self):
        """Potential temperature is not temperature and has no Table 3a code."""
        r = resolve_column("temperature", ["Potential Temperature (deg C)", "Depth (m)"])
        assert r["column"] is None
        assert "does not store" in r["note"]

    def test_no_match_names_the_columns_it_saw(self):
        r = resolve_column("salinity", ["Depth (m)", "Temp"])
        assert r["column"] is None and r["matched_by"] is None
        assert "Depth (m)" in r["note"]

    def test_empty_inputs_do_not_raise(self):
        assert resolve_column("", ["A"])["column"] is None
        assert resolve_column("latitude", [])["column"] is None


class TestRenameAppliesTheObviousCase:
    def test_the_user_request(self):
        cols, audit = rename_columns(["LAT", "LON", "Depth (m)"], {"latitude": "Latitude"})
        assert cols == ["Latitude", "LON", "Depth (m)"]
        assert audit["applied"] == {"LAT": "Latitude"}
        assert audit["renames"][0]["resolved_by"] == "wod_code"

    def test_naming_a_headerless_fixed_width_column_needs_no_escape_hatch(self):
        """Going from no code to code 0 is an improvement, so it needs no strict=False."""
        cols, audit = rename_columns(["col_0", "col_1"],
                                     {"col_0": "Depth (m)", "col_1": "Temp (deg C)"})
        assert cols == ["Depth (m)", "Temp (deg C)"]
        assert audit["applied_with_problems"] is False

    def test_folding_a_unit_into_the_name_is_allowed(self):
        cols, _ = rename_columns(["DO", "Depth (m)"], {"DO": "DO (mg/L)"})
        assert cols == ["DO (mg/L)", "Depth (m)"]


class TestRenameRefusesToChangeMeaning:
    """Each rename would still validate but lose the code, unit or scale."""

    def test_dropping_the_unit_is_refused_and_the_fix_is_printed(self):
        cols, audit = rename_columns(["Temp (deg F)", "Depth (m)"],
                                     {"Temp (deg F)": "Temperature"})
        assert cols == ["Temp (deg F)", "Depth (m)"], "nothing may be applied"
        assert "'Temperature (deg F)'" in audit["problems"][0]

    def test_dropping_the_scale_is_refused(self):
        old = "Temperature [ITS-90, deg C]"
        cols, audit = rename_columns([old], {old: "Temperature (deg C)"})
        assert cols == [old]
        assert "ITS-90" in audit["problems"][0]
        assert "second header 3" in audit["problems"][0]

    def test_changing_the_variable_is_refused(self):
        cols, audit = rename_columns(["T090C", "Depth (m)"], {"T090C": "Theta"})
        assert cols == ["T090C", "Depth (m)"]
        assert "WOD code 1" in audit["problems"][0]
        assert "VARIABLE_ALIASES" in audit["problems"][0]

    def test_renaming_the_depth_axis_onto_bottom_depth_is_refused(self):
        """`depth` inside `bottom_depth` must not match the depth axis."""
        cols, audit = rename_columns(["Depth (m)", "Temp (deg C)"],
                                     {"Depth (m)": "Bottom Depth (m)"})
        assert cols == ["Depth (m)", "Temp (deg C)"]
        assert audit["problems"]

    def test_require_unit_false_permits_a_deliberate_unit_drop(self):
        cols, audit = rename_columns(["Temp (deg C)"], {"Temp (deg C)": "Temperature"},
                                     require_unit=False)
        assert cols == ["Temperature"]
        assert audit["applied_with_problems"] is False


class TestRenameNeverCorrupts:
    """strict=False accepts a judgement call. It never accepts a structural one."""

    def test_a_collision_is_blocked_even_with_strict_false(self):
        cols, audit = rename_columns(["LAT", "Latitude"], {"LAT": "Latitude"}, strict=False)
        assert cols == ["LAT", "Latitude"], "a duplicate header loses a column silently"
        assert audit["blocked"] == ["LAT"]

    def test_two_renames_onto_one_name_are_blocked(self):
        cols, audit = rename_columns(["a", "b"], {"a": "Depth (m)", "b": "Depth (m)"},
                                     strict=False)
        assert cols.count("Depth (m)") <= 1

    def test_an_unresolvable_query_is_blocked_not_invented(self):
        cols, audit = rename_columns(["Depth (m)"], {"oxygen": "Oxygen"}, strict=False)
        assert cols == ["Depth (m)"]
        assert audit["blocked"] == ["oxygen"]

    def test_strict_false_applies_a_judgement_call_and_says_so(self):
        cols, audit = rename_columns(["Temp (deg F)"], {"Temp (deg F)": "Temperature"},
                                     strict=False)
        assert cols == ["Temperature"]
        assert audit["applied_with_problems"] is True
        assert audit["problems"]


class TestRenamePreservesTheContract:
    """A rename must not change the mapping contract generated for the file."""

    @pytest.mark.parametrize("columns,renames", [
        (["LAT", "LON", "Depth (m)", "T090C"], {"latitude": "Latitude"}),
        (["LAT", "LON", "Depth (m)", "T090C"], {"temperature": "Temperature (deg C)"}),
        (["LAT", "LON", "DepSM", "Sal00"], {"depth": "Depth (m)"}),
    ])
    def test_codes_and_conversions_survive(self, columns, renames):
        before = _contract(columns)
        renamed, audit = rename_columns(columns, renames)
        assert renamed != columns, "the test is pointless if nothing was applied"
        after = _contract(renamed)
        assert _codes(before) == _codes(after)
        assert _conversions(before) == _conversions(after)


class TestSuggestColumnNames:
    def test_it_refuses_to_canonicalize_a_colliding_sensor_pair(self):
        s = suggest_column_names(["t090C", "t190C", "Depth (m)"])
        assert s["collisions"] == {"1": ["t090C", "t190C"]}
        assert all(c["suggested"] is None for c in s["columns"])

    def test_it_suggests_the_one_rename_that_changes_behaviour(self):
        s = suggest_column_names(["DO", "Depth (m)"], units=["mg/L", "m"])
        suggested = {c["column"]: c["suggested"] for c in s["columns"]}
        assert suggested["DO"] == "DO (mg/L)"

    def test_a_suggestion_makes_the_conversion_detectable(self):
        """Folding the unit into the name lets the mapping flag a needed conversion."""
        s = suggest_column_names(["DO"], units=["mg/L"])
        new = s["columns"][0]["suggested"]
        contract = _contract(["LAT", "LON", "Depth (m)", new])
        oxygen = [m for m in contract["column_mapping"] if m["wod_code"] == 3]
        assert oxygen and oxygen[0].get("conversion_needed")

    def test_it_names_what_has_no_code(self):
        s = suggest_column_names(["SIGMA-T", "Depth (m)"])
        assert s["unmapped"] == ["SIGMA-T"]

    def test_it_applies_nothing(self):
        cols = ["DO", "Depth (m)"]
        suggest_column_names(cols, units=["mg/L", "m"])
        assert cols == ["DO", "Depth (m)"]

    def test_a_blank_column_does_not_shift_the_units_onto_their_neighbours(self):
        """A units row aligns with the original header, blank columns included."""
        s = suggest_column_names(["", "Depth", "Temp", "Sal"],
                                 units=["", "m", "deg C", "PSU"])
        unit = {c["column"]: c["unit"] for c in s["columns"]}
        assert unit == {"Depth": "m", "Temp": "deg C", "Sal": "PSU"}

    def test_a_trailing_blank_column_is_still_skipped(self):
        s = suggest_column_names(["Depth", "Temp", ""], units=["m", "deg C", ""])
        assert [c["column"] for c in s["columns"]] == ["Depth", "Temp"]


# --------------------------------------------------------------------------- helpers
def _contract(columns):
    return generate_field_mapping({"columns": list(columns), "raw_header_block": ""},
                                  {"dataset": {"code": "CTD"}})


def _codes(contract):
    return sorted(m["wod_code"] for m in contract["column_mapping"])


def _conversions(contract):
    return sorted((m["wod_code"], bool(m.get("conversion_needed")))
                  for m in contract["column_mapping"])
