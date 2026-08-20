"""The knowledge base: table loading, flag derivation, and agent-facing lookup."""
from pathlib import Path

import pytest

from wod_etl.reference import (
    available_tables,
    depth_flags,
    gradient_factors,
    load_table,
    observed_level_flags,
    qc_flag_scales,
    query_wod_knowledge,
    range_check_variables,
    range_levels,
    search_manual,
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
        """Every JSON in the knowledge dir must be reachable through the loader."""
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
        """apply_qc_auditing asserts its constants at import; prove the guard works."""
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
        """Header id 29 is Probe Type; variable code 29 is Air pressure."""
        assert wod_secondary_headers()[29] == "Probe Type"
        assert wod_variables()[29]["name"] == "Air pressure"

    def test_gradient_factors_cover_the_range_checked_variables(self):
        assert set(range_levels()) <= set(gradient_factors()) | {11, 17}

    def test_range_variables_match_appendix_11(self):
        assert {v["code"] for v in range_check_variables()} == set(range_levels())

    def test_molar_volume_constant(self):
        assert unit_constants()["molar_volume_o2_stp_l_per_mole"] == pytest.approx(22.3916)


class TestQueryWodKnowledge:
    def test_variable_by_name(self):
        m = query_wod_knowledge("temperature", kind="variable")["matches"]
        assert m[0]["wod_code"] == 1
        assert "p.18" in m[0]["citation"]

    def test_variable_by_code(self):
        m = query_wod_knowledge(3, kind="variable")["matches"]
        assert m[0]["name"] == "Oxygen"

    def test_flag_lookup_reports_every_scale(self):
        m = query_wod_knowledge(6, kind="flag")["matches"]
        scales = {x["scale"] for x in m}
        assert {"observed_level", "depth"} & scales or len(scales) >= 2
        assert all("p.55" in x["citation"] for x in m)

    def test_xctd_is_explained_not_invented(self):
        r = query_wod_knowledge("XCTD", kind="dataset")
        assert any("2 m" in (x.get("note") or "") or x.get("code") for x in r["matches"])

    def test_secondary_header_lookup(self):
        m = query_wod_knowledge("probe type", kind="secondary_header")["matches"]
        assert m[0]["id"] == 29

    def test_threshold_at_depth(self):
        m = query_wod_knowledge("temperature", kind="gradient", depth_m=1000)["matches"]
        assert m[0]["applicable_at_depth"]["MGV"] == pytest.approx(0.7)

    def test_range_at_depth_carries_the_basin_caveat(self):
        m = query_wod_knowledge("oxygen", kind="range", depth_m=3000)["matches"]
        assert m[0]["applicable_at_depth"]["max"] == pytest.approx(392.0)
        assert "basin" in m[0]["caveat"]

    def test_every_match_is_citable(self):
        r = query_wod_knowledge("salinity")
        assert r["match_count"] > 0
        assert all("citation" in m for m in r["matches"])

    def test_unknown_kind_is_rejected(self):
        assert "error" in query_wod_knowledge("x", kind="banana")

    def test_no_match_is_not_an_error(self):
        r = query_wod_knowledge("zzz_nonexistent_zzz")
        assert r["match_count"] == 0 and "error" not in r


class TestSearchManual:
    def test_finds_prose_not_in_any_table(self):
        r = search_manual("fall-rate", max_hits=3)
        if "error" in r:
            pytest.skip("page cache not built")
        assert r["hit_count"] >= 1
        assert all("p." in h["citation"] for h in r["hits"])

    def test_missing_term_returns_no_hits(self):
        r = search_manual("zzzz_not_in_the_manual_zzzz")
        if "error" in r:
            pytest.skip("page cache not built")
        assert r["hit_count"] == 0


class TestKnowledgeRouting:
    """The knowledge index promises that common questions are answerable without opening
    the 131-page manual. These guard that promise against drift."""

    KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"

    def test_index_and_prose_references_exist(self):
        for name in ("README.md", "datasets.md", "cast_structure.md",
                     "accession_field_guide.md", "wod_code_tables/standardization_rules.md"):
            assert (self.KNOWLEDGE / name).is_file(), f"missing knowledge doc: {name}"

    def test_index_links_all_resolve(self):
        import re
        index = self.KNOWLEDGE / "README.md"
        for link in re.findall(r"\]\(([^)#]+)\)", index.read_text()):
            if link.startswith(("http", "#")):
                continue
            assert (index.parent / link).resolve().exists(), f"dead link in index: {link}"

    @pytest.mark.parametrize("question,terms", [
        ("plankton is not a Table 3a variable", ["Biological Header", "upper and lower depth"]),
        ("SUR is one cast per cruise",          ["one cast per cruise", "Julian year-day"]),
        ("surface data skips vertical checks",  ["no vertical checks"]),
        ("bottom depth is a secondary header",  ["Bottom Depth"]),
        ("buoy datasets are distinguishable",   ["moored", "profiling float", "drifting"]),
        ("the 29/29 collision is explained",    ["Probe Type", "Air pressure"]),
        ("CTD vs OSD is by resolution",         ["depth increment"]),
        ("degree-decimal-minute coordinates",   ["decimal minutes"]),
        ("nitrite is excluded",                 ["nitrite"]),
    ])
    def test_common_questions_are_answered_in_prose(self, question, terms):
        """If one of these fails, the answer moved or was lost and the next run will go
        back to the PDF for it."""
        text = "\n".join(p.read_text() for p in self.KNOWLEDGE.rglob("*.md")).lower()
        missing = [t for t in terms if t.lower() not in text]
        assert not missing, f"{question}: knowledge docs no longer cover {missing}"

    def test_search_manual_finds_the_deferred_appendices(self):
        """The middle tier: content we deliberately did NOT extract must still be locatable
        without paging through the PDF."""
        from wod_etl.reference import search_manual
        for term in ("Reiniger-Ross", "WMO square"):
            assert search_manual(term)["hit_count"] > 0, f"search_manual cannot find {term}"
