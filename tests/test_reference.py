"""The knowledge base: table loading, flag derivation, and the knowledge index."""

import pytest

from wod_etl.reference import (
    available_tables,
    depth_flags,
    gradient_factors,
    load_table,
    observed_level_flags,
    qc_flag_scales,
    range_check_variables,
    range_levels,
    unit_constants,
    wod_datasets,
    wod_secondary_headers,
    wod_variables,
)
from wod_etl.reference.load_tables import TABLE_FILES, flag_meanings_attr, flag_values_attr


class TestTableLoading:
    def test_every_declared_table_loads(self):
        for name in TABLE_FILES:
            assert load_table(name), name

    def test_no_table_is_orphaned(self):
        from wod_etl.config import CODE_TABLES_DIR
        on_disk = {p.name for p in CODE_TABLES_DIR.glob("*.json")}
        declared = set(TABLE_FILES.values())
        assert on_disk == declared, f"unreachable: {on_disk - declared}"

    def test_available_tables_carries_page_citations(self):
        for t in available_tables():
            assert t["pages"] is not None, t["name"]

    def test_missing_table_raises_a_helpful_error(self):
        with pytest.raises(FileNotFoundError, match="wod_readme.pdf"):
            load_table("table_99_nonexistent")


class TestFlagsComeFromTable12:
    def test_five_scales_are_defined(self):
        assert set(qc_flag_scales()) == {"entire_cast", "depth", "observed_level",
                                         "standard_level", "biological"}

    def test_depth_and_observed_scales_differ_at_flag_1(self):
        """1 means inversion on the depth scale but range outlier on the observed scale."""
        assert "inversion" in depth_flags()[1].lower()
        assert "range" in observed_level_flags()[1].lower()

    def test_cf_attributes_align_one_for_one(self):
        for scale in ("depth", "observed_level"):
            assert len(flag_values_attr(scale)) == len(flag_meanings_attr(scale).split())

    def test_cf_tokens_are_valid_and_unique(self):
        for scale in ("depth", "observed_level"):
            tokens = flag_meanings_attr(scale).split()
            assert len(tokens) == len(set(tokens))
            assert all(t.replace("_", "").isalnum() for t in tokens)

    def test_module_constants_are_verified_against_the_table(self):
        """apply_qc_auditing checks its flag constants against the table at import."""
        # importlib, not `from ... import`: the package __init__ binds that name to the
        # function, which shadows the submodule.
        import importlib
        m = importlib.import_module("wod_etl.qc_transforms.apply_qc_auditing")
        assert "range" in observed_level_flags()[m.FLAG_RANGE].lower()
        assert "gradient" in observed_level_flags()[m.FLAG_GRADIENT].lower()
        assert "inversion" in depth_flags()[m.DEPTH_FLAG_INVERSION].lower()
        m._verify_flag_constants()   # raises if the table and the constants disagree


class TestReferenceContent:
    def test_eleven_datasets(self):
        assert len(wod_datasets()) == 11

    def test_29_variables_with_units(self):
        v = wod_variables()
        assert len(v) == 29
        assert all(x["standard_unit"] for x in v.values())

    def test_secondary_headers_are_a_separate_namespace(self):
        assert wod_secondary_headers()[29] == "Probe Type"
        assert wod_variables()[29]["name"] == "Air pressure"

    def test_gradient_factors_cover_the_range_checked_variables(self):
        assert set(range_levels()) <= set(gradient_factors()) | {11, 17}

    def test_range_variables_match_appendix_11(self):
        assert {v["code"] for v in range_check_variables()} == set(range_levels())

    def test_molar_volume_constant(self):
        assert unit_constants()["molar_volume_o2_stp_l_per_mole"] == pytest.approx(22.3916)
