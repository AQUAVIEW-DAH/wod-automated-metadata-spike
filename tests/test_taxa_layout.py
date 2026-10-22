"""Reading a plankton table as Taxa-Records: column roles, units, and why a table cannot be
written."""
import pytest

from wod_etl.staging.taxa_layout import describe_taxa_table, unit_parts

BONGO = ("Station,Upper depth (m),Lower depth (m),Taxon,Displacement volume (ml),"
         "Wet weight (g)\n014,0,100,Calanus pacificus,4.20,1.81\n"
         "014,100,500,Calanus pacificus,2.05,0.90\n015,0,100,Calanus pacificus,3.75,1.55\n")


def table(tmp_path, text, name="hauls.csv"):
    p = tmp_path / name
    p.write_text(text)
    return describe_taxa_table(p)


class TestUnits:
    @pytest.mark.parametrize("column, parts", [
        ("Displacement volume (ml)", ("ml", None)),
        ("Wet weight (mg/m3)", ("mg", "m3")),
        ("Abundance (ind m-3)", ("ind", "m3")),
        ("Abundance (ind/m3)", ("ind", "m3")),
        ("Wet weight (g per 1000 m3)", ("g", "1000 m3")),
        ("Abundance (ind. m^-2)", ("ind.", "m2")),
        ("Count", ("", None)),
    ])
    def test_measured_unit_and_per_unit_are_read_from_the_name(self, column, parts):
        assert unit_parts(column) == parts


class TestTheBongoTable:
    def test_each_column_is_a_taxa_record_field(self, tmp_path):
        t = table(tmp_path, BONGO)
        assert t["missing"] == []
        assert t["columns"] == {"taxon": "Taxon", "upper": "Upper depth (m)",
                                "lower": "Lower depth (m)", "station": "Station"}
        assert [(m["field"], m["unit"], m["per_unit"], m["negate"])
                for m in t["measurements"]] == [(12, "ml", "sample", False),
                                                (13, "g", "sample", False)]
        assert t["stations"] == ["14", "15"] and t["rows"] == 3

    def test_the_smaller_unit_is_written_negative(self, tmp_path):
        t = table(tmp_path, "Station,Upper depth,Lower depth,Taxon,Wet weight (mg/m3)\n"
                            "1,0,50,Acartia,12\n")
        (m,) = t["measurements"]
        assert (m["field"], m["unit"], m["per_unit"], m["negate"]) == (13, "mg", "m3", True)

    def test_a_count_per_volume(self, tmp_path):
        t = table(tmp_path, "Station,Upper depth,Lower depth,Taxon,Abundance (ind/m3)\n"
                            "1,0,50,Acartia,120\n")
        (m,) = t["measurements"]
        assert (m["field"], m["per_unit"], m["negate"]) == (10, "m3", False)


class TestWhatCannotBeWrittenSaysWhy:
    @pytest.mark.parametrize("text, reason", [
        ("Station,Taxon,Count\n1,Acartia,3\n", "no upper and lower depth"),
        ("Station,Upper depth,Lower depth,Count\n1,0,50,3\n", "no column names the taxon"),
        ("Station,Upper depth,Lower depth,Taxon,Dry weight (kg)\n1,0,50,Acartia,3\n",
         "WOD cannot state for dry weight"),
        ("Station,Upper depth,Lower depth,Taxon,Wet weight\n1,0,50,Acartia,3\n",
         "states no unit"),
        ("Station,Upper depth,Lower depth,Taxon,Biomass (mg/m3)\n1,0,50,Acartia,3\n",
         "needs a WOD biomass code"),
        ("Station,Upper depth,Lower depth,Taxon,Notes\n1,0,50,Acartia,ok\n",
         "no column is a measurement"),
    ])
    def test_reason(self, tmp_path, text, reason):
        assert any(reason in m for m in table(tmp_path, text)["missing"])
