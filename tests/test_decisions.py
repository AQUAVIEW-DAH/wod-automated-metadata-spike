"""decisions.toml: written by the script, edited by a person, never overwritten."""
import tomllib

import pytest

from wod_etl.orchestration import decisions as dec


class TestTomlWriter:
    @pytest.mark.parametrize("value", [
        "plain", 'has "quotes"', "back\\slash", "tab\tand\nnewline", "142°18’E",
        3, 2.5, 150.0, True, [1, "a"], {"Mesh size": 100, "Gear code": "NORPAC Net"},
        {"0,1mm": {"Mesh size": 100}}, [140.0, 150.0, 10.0, 25.0],
    ])
    def test_round_trip(self, value):
        text = dec.section("x", {"v": value})
        assert tomllib.loads(text)["x"]["v"] == value

    def test_a_regex_survives_as_a_literal_string(self):
        pattern = r"(\d+) (Nov\. \d{2}, \d{4}) (\S+E) (\S+N)"
        text = dec.section("cross", {"row_pattern": pattern})
        assert "'" in text and tomllib.loads(text)["cross"]["row_pattern"] == pattern

    def test_odd_keys_are_quoted(self):
        text = dec.section(dec.item_header("files", "data/0-data/x y.csv"), {"action": ""})
        assert tomllib.loads(text)["files"]["data/0-data/x y.csv"]["action"] == ""


class TestAppendOnly:
    def test_the_persons_edits_and_comments_survive_an_append(self, tmp_path):
        p = tmp_path / "decisions.toml"
        dec.append(p, [dec.section("profiles.ctd", {"confirmed": False})], "0303928", "run 1")
        p.write_text(p.read_text().replace("confirmed = false",
                                           "confirmed = true   # checked against the readme"))
        dec.append(p, [dec.section('conflicts."date:6"', {"resolution": ""})], "0303928",
                   "run 2")
        text = p.read_text()
        assert "confirmed = true   # checked against the readme" in text
        assert "# ---- added by run 2 ----" in text
        data = tomllib.loads(text)
        assert data["profiles"]["ctd"]["confirmed"] is True and "date:6" in data["conflicts"]

    def test_nothing_to_add_leaves_the_file_alone(self, tmp_path):
        p = tmp_path / "decisions.toml"
        dec.append(p, [], "0303928", "run 1")
        before = p.read_text()
        assert dec.append(p, [], "0303928", "run 2") == 0 and p.read_text() == before

    def test_a_syntax_error_is_reported_with_where(self, tmp_path):
        p = tmp_path / "decisions.toml"
        p.write_text("accession = \"0303928\"\n[metadata\n")
        data, err = dec.load(p)
        assert data == {} and "not valid TOML" in err and "line" in err


def _triage(files):
    return {"root": "/nowhere", "files": [
        {"path": p, "action": a, "reason": "nothing names an instrument", "format": "csv",
         "dataset_type": None, **extra} for p, a, extra in files]}


class TestProposals:
    def test_look_alike_files_are_one_question(self):
        t = _triage([(f"data/par/PAR_202409{d:02d}.txt", "inspect", {}) for d in range(1, 31)]
                    + [("data/nav/track.dat", "inspect", {})])
        text = "\n".join(dec.propose(t, {}))
        assert '[files."data/par/*.txt"]' in text and "Applies to 30 files" in text
        assert '[files."data/nav/track.dat"]' in text

    def test_an_item_already_decided_is_not_proposed_again(self):
        t = _triage([("data/par/a.txt", "inspect", {}), ("data/par/b.txt", "inspect", {})])
        decisions = {"files": {"data/par/*.txt": {"action": "skip"}}}
        assert not any("[files." in s for s in dec.propose(t, decisions))

    def test_a_long_format_group_gets_a_recipe_with_unit_placeholders(self):
        lf = lambda keys, var: {"variable_column": "parm", "value_column": "value",
                                "key_columns": keys, "variables_sampled": [var]}
        t = _triage([("d/X1_CTD_Temp.csv", "stage", {"long_format": lf(["StNo", "dep"],
                                                                          "Temperature")}),
                     ("d/X1_CTD_Sal.csv", "stage", {"long_format": lf(["StNo", "depth"],
                                                                         "Salinity")}),
                     ("d/X1_Stations.csv", "sidecar", {})])
        recipe = tomllib.loads("\n".join(dec.propose(t, {})))["profiles"]["x1_ctd"]
        assert recipe["columns"]["Temperature"] == "Temperature (UNIT?)"
        assert recipe["station_list"] == "d/X1_Stations.csv"
        assert recipe["cast_key"] == "StNo" and recipe["confirmed"] is False
        assert recipe["key_aliases"] in ({"depth": ["dep"]}, {"dep": ["depth"]})


class TestValidation:
    def test_a_pattern_settles_every_file_it_matches(self):
        t = _triage([("data/par/a.txt", "inspect", {}), ("data/par/b.txt", "inspect", {})])
        items, plan = dec.validate({"files": {"data/par/*.txt": {"action": "skip"}}}, t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert {s["key"] for s in plan["skipped"]} == {"data/par/a.txt", "data/par/b.txt"}

    def test_process_needs_a_real_dataset(self):
        t = _triage([("x.csv", "inspect", {})])
        items, _ = dec.validate({"files": {"x.csv": {"action": "process",
                                                     "process_as": "BOTTLE"}}}, t, {}, [])
        assert any("not a WOD dataset" in i["what"] for i in items if i["blocking"])
        items, plan = dec.validate({"files": {"x.csv": {"action": "process",
                                                        "process_as": "OSD"}}}, t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert plan["direct"] == ["x.csv"] and plan["dataset_overrides"] == {"x.csv": "OSD"}

    def test_an_unconfirmed_taxa_recipe_does_not_block(self):
        items, _ = dec.validate({"taxa": {"norpac": {"confirmed": False, "file": "n.csv"}}},
                                _triage([]), {}, [])
        assert items and not any(i["blocking"] for i in items)

    def test_a_settled_conflict_reopens_when_its_values_change(self):
        conflict = {"id": "date:6", "station": "6", "field": "date", "what": "dates differ",
                    "how_to_fix": "...", "values": {"list": "2015-11-12", "report": "2015-11-11"},
                    "values_hash": "new"}
        decided = {"conflicts": {"date:6": {"resolution": "keep", "values_hash": "old"}}}
        items, _ = dec.validate(decided, _triage([]), {}, [conflict])
        assert any("values changed" in i["what"] for i in items)

    @pytest.mark.parametrize("resolution,expected", [
        ("report", {"date": "2015-11-11"}),
        ({"date": "2015-11-13"}, {"date": "2015-11-13"}),
        ("keep", {}),
    ])
    def test_a_resolution_becomes_a_station_override_with_a_note(self, resolution, expected):
        conflict = {"id": "date:6", "station": "6", "field": "date", "what": "dates differ",
                    "how_to_fix": "...", "values": {"list": "2015-11-12", "report": "2015-11-11"},
                    "values_hash": "h"}
        decided = {"conflicts": {"date:6": {"resolution": resolution, "values_hash": "h",
                                            "note": "fishing log"}}}
        items, plan = dec.validate(decided, _triage([]), {}, [conflict])
        assert not [i for i in items if i["blocking"]]
        o = plan["station_overrides"]["6"]
        assert {k: v for k, v in o.items() if k != "notes"} == expected
        assert "fishing log" in o["notes"][0]
