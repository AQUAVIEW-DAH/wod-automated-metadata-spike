"""decisions.toml: written by `wod-etl run`, reviewed by a person, never overwritten."""
import re
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
        text = dec.section(dec.item_header("files", "data/0-data/x y.csv"), {"include": False})
        assert tomllib.loads(text)["files"]["data/0-data/x y.csv"]["include"] is False


class TestAppendOnly:
    def test_the_persons_edits_and_comments_survive_an_append(self, tmp_path):
        p = tmp_path / "decisions.toml"
        dec.append(p, [dec.section('files."a.csv"', {"include": True})], "0303928", "run 1")
        p.write_text(p.read_text().replace("include = true",
                                           "include = false   # a calibration cast"))
        dec.append(p, [dec.section('conflicts."date:6"', {"resolution": ""})], "0303928",
                   "run 2")
        text = p.read_text()
        assert "include = false   # a calibration cast" in text
        assert "# ---- added by run 2 ----" in text and "uv run wod-etl confirm 0303928" in text
        data = tomllib.loads(text)
        assert data["files"]["a.csv"]["include"] is False and "date:6" in data["conflicts"]

    def test_nothing_to_add_leaves_the_file_alone(self, tmp_path):
        p = tmp_path / "decisions.toml"
        dec.append(p, [], "0303928", "run 1")
        before = p.read_text()
        assert dec.append(p, [], "0303928", "run 2") == 0 and p.read_text() == before

    def test_a_syntax_error_is_reported_with_where(self, tmp_path):
        p = tmp_path / "decisions.toml"
        p.write_text("accession = \"0303928\"\n[files\n")
        data, err = dec.load(p)
        assert data == {} and "not valid TOML" in err and "line" in err


def _file(path, action="process", include=None, dataset="CTD", data="CTD profiles: "
          "temperature", why="CTD by its path", **extra):
    include = action in ("process", "stage") if include is None else include
    return {"path": path, "action": action, "include": include,
            "dataset": dataset if include else None, "data": data, "why": why,
            "format": "csv", "reader": "delimited", "dataset_type": dataset if include else None,
            "reason": why, **extra}


def _doc(path, data="NCEI readme"):
    return _file(path, action="document", data=data, why="documentation, not data")


def _triage(*files):
    return {"root": "/nowhere", "files": list(files)}


def _entries(triage, decisions=None):
    return tomllib.loads("\n".join(dec.propose(triage, decisions or {}))).get("files") or {}


class TestEveryFileGetsAnEntry:
    def test_alike_files_share_one_entry_under_a_pattern(self):
        t = _triage(*[_file(f"data/par/PAR_202409{d:02d}.txt") for d in range(1, 31)],
                    _file("data/nav/track.dat", action="inspect", data="unidentified"))
        entries = _entries(t)
        assert set(entries) == {"data/par/*", "data/nav/track.dat"}
        assert entries["data/par/*"]["include"] is True
        assert entries["data/nav/track.dat"]["include"] is False

    def test_a_pattern_never_takes_in_a_file_decided_differently(self):
        t = _triage(*[_file(f"d/RBRsolo_SN10{i}.nc", action="exclude", data="moored record",
                            why="MRB") for i in range(5)],
                    *[_file(f"d/SBE37SM_SN0{i}.nc", action="exclude", data="moored record",
                            why="MRB") for i in range(2)],
                    _file("d/workhorse300_up.nc", action="exclude", data="current velocity",
                          why="no velocity"))
        keys = set(_entries(t))
        assert keys == {"d/RBRsolo_*.nc", "d/SBE37SM_*.nc", "d/workhorse300_up.nc"}

    def test_a_shared_part_of_the_name_makes_the_pattern_when_the_ends_do_not(self):
        names = ["LeConte_Aug2016_CTD_data.nc", "LeConte_May2017_CTD_data_withTurbidity.nc",
                 "LeConte_Sept2018_CTD_kayak_data.nc"]
        t = _triage(*[_file(f"d/{n}") for n in names],
                    _file("d/LeConte_Sept2018_keelCTD_kayak_data.nc", action="exclude",
                          data="underway record", why="SUR"),
                    _file("d/LeConte_Aug2016_ADCP_data.nc", action="exclude",
                          data="current velocity", why="no velocity"))
        assert "d/LeConte_*_CTD_*.nc" in _entries(t)

    def test_a_star_does_not_reach_into_a_subfolder(self):
        assert dec.matches("data/0-data/*.nc", "data/0-data/a.nc")
        assert not dec.matches("data/0-data/*.nc", "data/0-data/moorings/a.nc")

    def test_documentation_in_a_folder_is_one_entry_whatever_it_is(self):
        t = _triage(_doc("about/journal.txt", "NCEI accession journal"),
                    _doc("about/0303928_map.jpg", "NCEI map of the sample locations"))
        entries = _entries(t)
        assert list(entries) == ["about/*"] and entries["about/*"]["include"] is False
        assert "journal" in entries["about/*"]["data"] and "map" in entries["about/*"]["data"]

    def test_what_is_written_comes_first_then_what_is_left_out_then_documentation(self):
        t = _triage(_doc("NCEI-Readme.txt"),
                    _file("a/sed.nc", action="exclude", data="sediment", why="no sediment"),
                    _file("b/ctd.csv"))
        assert list(_entries(t)) == ["b/ctd.csv", "a/sed.nc", "NCEI-Readme.txt"]

    def test_a_file_already_decided_is_not_proposed_again(self):
        t = _triage(_file("data/par/a.txt"), _file("data/par/b.txt"))
        assert not _entries(t, {"files": {"data/par/*": {"include": False}}})

    def test_one_files_own_entry_overrides_its_groups_pattern(self):
        decisions = {"files": {"d/*.csv": {"include": True}, "d/b.csv": {"include": False}}}
        assert dec.file_decision(decisions, "d/a.csv")["include"] is True
        assert dec.file_decision(decisions, "d/b.csv")["include"] is False
        assert dec.decision_key(decisions, "d/b.csv") == "d/b.csv"

    def test_a_long_format_group_is_reshaped_by_a_recipe_nobody_fills_in(self):
        lf = lambda keys, var: {"variable_column": "parm", "value_column": "value",
                                "key_columns": keys, "variables_sampled": [var],
                                "station_list": "d/X1_Stations.csv"}
        t = _triage(_file("d/X1_CTD_Temp.csv", action="stage",
                          long_format=lf(["StNo", "dep"], "Temperature")),
                    _file("d/X1_CTD_Sal.csv", action="stage",
                          long_format=lf(["StNo", "depth"], "Salinity")),
                    _file("d/X1_Stations.csv", action="sidecar", data="station list",
                          why="locates casts", columns=["Station", "Lat", "Lon"]))
        parsed = tomllib.loads("\n".join(dec.propose(t, {})))
        # The person decides include or not, and nothing else: no recipe, no unit, no
        # station check to fill in.
        assert set(parsed) == {"files", "metadata"}
        plan = dec.build_plan(parsed, t, [])
        recipe = plan["profiles"]["x1_ctd"]
        assert (recipe["kind"], recipe["station_list"]) == ("long", "d/X1_Stations.csv")
        assert recipe["cast_key"] == "StNo" and recipe.get("columns") is None
        assert recipe["key_aliases"] in ({"depth": ["dep"]}, {"dep": ["depth"]})
        assert set(plan["consumed"]) == {"d/X1_CTD_Temp.csv", "d/X1_CTD_Sal.csv",
                                         "d/X1_Stations.csv"}

    def test_long_format_files_nothing_locates_are_not_written_and_nothing_is_asked(self):
        t = _triage(_file("d/X1_CTD_Temp.csv", action="stage", include=False,
                          why="... its casts cannot be located",
                          long_format={"variable_column": "parm", "value_column": "value",
                                       "key_columns": ["StNo", "depth"],
                                       "variables_sampled": ["Temperature"],
                                       "station_list": None}))
        entry = _entries(t)["d/X1_CTD_Temp.csv"]
        assert entry["include"] is False and "cannot be located" in entry["why"]
        # Included anyway: not written, and said -- there is nothing to fill in instead.
        items, plan = dec.validate({"files": {"d/X1_CTD_Temp.csv": {"include": True}}}, t, {},
                                   [])
        assert not plan["profiles"] and not plan["direct"]
        assert [w["key"] for w in plan["not_written"]] == ["d/X1_CTD_Temp.csv"]
        assert not [i for i in items if i["blocking"]]
        assert any("cannot be written" in i["what"] for i in items)

    def test_without_a_recipe_there_are_no_station_checks_but_metadata_is_offered(self):
        parsed = tomllib.loads("\n".join(dec.propose(_triage(_file("a.csv")), {})))
        assert "track" not in parsed and "cross_check" not in parsed
        assert parsed["metadata"] == {}                     # every key commented, to fill in


class TestValidation:
    def test_include_must_be_true_or_false(self):
        t = _triage(_file("x.csv"))
        items, _ = dec.validate({"files": {"x.csv": {"include": "yes"}}}, t, {}, [])
        assert [i for i in items if i["blocking"] and "include = 'yes'" in i["what"]]

    def test_a_file_with_no_entry_blocks(self):
        items, _ = dec.validate({"files": {}}, _triage(_file("x.csv")), {}, [])
        assert any("have no entry" in i["what"] for i in items if i["blocking"])

    def test_an_included_file_with_no_dataset_is_classified_when_processed(self):
        t = _triage(_file("x.csv", action="inspect", include=False))
        items, plan = dec.validate({"files": {"x.csv": {"include": True}}}, t, {}, [])
        assert not [i for i in items if i["blocking"]] and plan["direct"] == ["x.csv"]
        assert any("classified from what it holds" in i["what"] for i in items)
        items, _ = dec.validate({"files": {"x.csv": {"include": True, "dataset": "BOTTLE"}}},
                                t, {}, [])
        assert any("not a WOD dataset" in i["what"] for i in items if i["blocking"])
        items, plan = dec.validate({"files": {"x.csv": {"include": True, "dataset": "OSD"}}},
                                   t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert plan["direct"] == ["x.csv"] and plan["dataset_overrides"] == {"x.csv": "OSD"}

    def test_the_reviewed_dataset_is_what_the_file_is_written_as(self):
        """Processing does not classify again from less evidence: it sees no NetCDF
        instrument attribute, and an SV&T probe's casts came out CTD instead of MBT."""
        t = _triage(_file("ctd.csv"), _file("mvp.nc", dataset="MBT"))
        _, plan = dec.validate({"files": {"ctd.csv": {"include": True, "dataset": "CTD"},
                                          "mvp.nc": {"include": True, "dataset": "MBT"}}},
                               t, {}, [])
        assert plan["dataset_overrides"] == {"ctd.csv": "CTD", "mvp.nc": "MBT"}

    def test_left_out_files_are_listed_with_why(self):
        t = _triage(_file("a.csv"), _file("b.nc", action="exclude", data="currents",
                                          why="no velocity code"))
        decisions = {"files": {"a.csv": {"include": True},
                               "b.nc": {"include": False, "why": "no velocity code"}}}
        _, plan = dec.validate(decisions, t, {}, [])
        assert plan["left_out"] == [{"key": "b.nc", "data": None, "why": "no velocity code"}]
        assert [e["key"] for e in plan["entries"]] == ["a.csv", "b.nc"]

    def test_a_recipe_written_into_the_file_is_no_longer_read(self):
        t = _triage(_file("d/l.csv", action="stage"))
        decisions = {"files": {"d/l.csv": {"include": False}},
                     "profiles": {"l": {"kind": "long", "files": ["d/l.csv"],
                                        "columns": {"T": "T (UNIT?)"}}}}
        items, plan = dec.validate(decisions, t, {}, [])
        assert not [i for i in items if i["blocking"]] and not plan["profiles"]
        assert any("[profiles.l] is no longer read" in i["what"] for i in items)

    def test_a_station_conflict_is_reported_and_the_station_list_kept(self):
        conflict = {"id": "date:6", "station": "6", "field": "date", "what": "dates differ",
                    "values": {"list": "2015-11-12", "report": "2015-11-11"},
                    "values_hash": "h"}
        items, plan = dec.validate({}, _triage(), {}, [conflict])
        assert not [i for i in items if i["blocking"]]
        assert any("the station list is kept" in i["what"] for i in items)
        assert plan["station_overrides"]["6"] == {
            "notes": ["dates differ -- the station list was kept"]}

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
        items, plan = dec.validate(decided, _triage(), {}, [conflict])
        assert not [i for i in items if i["blocking"]]
        o = plan["station_overrides"]["6"]
        assert {k: v for k, v in o.items() if k != "notes"} == expected
        assert "fishing log" in o["notes"][0]


class TestHowAnIncludedFileIsWritten:
    def test_drop_and_metadata_reach_the_plan_with_the_accessions_metadata(self):
        t = _triage(_file("a.nc"), _file("b.nc"))
        decisions = {"metadata": {"Platform": "R/V Pelican"},
                     "files": {"a.nc": {"include": True, "dataset": "CTD",
                                        "drop": ["salinity"],
                                        "metadata": {"Cruise": "NABOS-08"}},
                               "b.nc": {"include": True, "dataset": "CTD"}}}
        items, plan = dec.validate(decisions, t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert plan["file_options"] == {
            "a.nc": {"drop": ["salinity"],
                     "metadata": {"Platform": "R/V Pelican", "Cruise": "NABOS-08"}},
            "b.nc": {"drop": [], "metadata": {"Platform": "R/V Pelican"}}}

    def test_a_malformed_drop_or_metadata_blocks(self):
        t = _triage(_file("a.nc"))
        for bad in ({"drop": "salinity"}, {"metadata": "NABOS"}):
            items, _ = dec.validate({"files": {"a.nc": {"include": True, "dataset": "CTD",
                                                        **bad}}}, t, {}, [])
            assert [i for i in items if i["blocking"]], bad

    def test_a_proposed_drop_is_written_into_the_entry(self):
        t = _triage(_file("ship.nc", data="CTD profiles: temperature", drop=["salinity"]))
        assert _entries(t)["ship.nc"]["drop"] == ["salinity"]


class TestAFileWithItsOwnEntryDoesNotBreakItsNeighboursGroup:
    def test_alike_files_keep_their_pattern_when_it_also_takes_in_a_file_decided_alone(self):
        names = ["LeConte_Aug2016_CTD_data.nc", "LeConte_Jul2017_CTD_data.nc",
                 "LeConte_Sept2018_CTD_kayak_data.nc"]
        t = _triage(*[_file(f"d/{n}") for n in names],
                    _file("d/LeConte_Sept2018_CTD_ship_data.nc", data="CTD profiles: temperature",
                          drop=["salinity"]),
                    _file("d/LeConte_Aug2016_ADCP_data.nc", action="exclude",
                          data="current velocity", why="no velocity"))
        decided = tomllib.loads("\n".join(dec.propose(t, {})))
        keys = {dec.decision_key(decided, f"d/{n}") for n in names}
        assert len(keys) == 1 and dec.is_pattern(keys.pop())          # one shared entry
        assert dec.file_decision(decided, "d/LeConte_Sept2018_CTD_ship_data.nc")["drop"] == [
            "salinity"]
        assert dec.file_decision(decided, "d/LeConte_Aug2016_ADCP_data.nc")["include"] is False


class TestTheMetadataTemplateIsValidOnceUncommented:
    def test_every_proposed_key_parses_when_the_reviewer_uncomments_it(self):
        text = "\n".join(dec.propose(_triage(_file("a.csv")), {}))
        uncommented = re.sub(r"^# ([a-z_]+ = )", r"\1", text, flags=re.M)
        meta = tomllib.loads(uncommented)["metadata"]
        assert {"platform", "institution", "project", "cruise", "principal_investigator",
                "probe_type", "instrument"} <= set(meta)


ISO = """<gmi:MI_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd"
 xmlns:gco="http://www.isotc211.org/2005/gco" xmlns:gmi="http://www.isotc211.org/2005/gmi">
 <gmd:identificationInfo><gmd:MD_DataIdentification>
  <gmd:pointOfContact><gmd:CI_ResponsibleParty>
   <gmd:individualName><gco:CharacterString>David Sutherland</gco:CharacterString></gmd:individualName>
   <gmd:role><gmd:CI_RoleCode codeListValue="principalInvestigator"/></gmd:role>
  </gmd:CI_ResponsibleParty></gmd:pointOfContact>
  <gmd:descriptiveKeywords><gmd:MD_Keywords>
   <gmd:keyword><gco:CharacterString>LeConte Glacier Ice-Ocean Interactions</gco:CharacterString></gmd:keyword>
   <gmd:type><gmd:MD_KeywordTypeCode codeListValue="project"/></gmd:type>
  </gmd:MD_Keywords></gmd:descriptiveKeywords>
 </gmd:MD_DataIdentification></gmd:identificationInfo>
 <gmi:acquisitionInformation><gmi:MI_AcquisitionInformation>
  <gmi:platform><gmi:MI_Platform><gmi:identifier><gmd:MD_Identifier><gmd:code>
   <gco:CharacterString>MV Steller</gco:CharacterString></gmd:code></gmd:MD_Identifier>
  </gmi:identifier></gmi:MI_Platform></gmi:platform>
  <gmi:platform><gmi:MI_Platform><gmi:identifier><gmd:MD_Identifier><gmd:code>
   <gco:CharacterString>MV Pelican</gco:CharacterString></gmd:code></gmd:MD_Identifier>
  </gmi:identifier></gmi:MI_Platform></gmi:platform>
 </gmi:MI_AcquisitionInformation></gmi:acquisitionInformation>
</gmi:MI_Metadata>"""


class TestMetadataTheAccessionStatesIsProposed:
    def test_one_value_is_set_and_several_are_offered_to_choose_from(self, tmp_path):
        (tmp_path / "WL01P3-ISO-19115-2.xml").write_text(ISO)
        t = {"root": str(tmp_path), "files": [_doc("WL01P3-ISO-19115-2.xml", "ISO 19115 metadata"),
                                              _file("a.nc")]}
        text = "\n".join(dec.propose(t, {}))
        meta = tomllib.loads(text)["metadata"]
        assert meta == {"project": "LeConte Glacier Ice-Ocean Interactions",
                        "principal_investigator": "David Sutherland"}
        assert '# platform = "MV Steller"\n#   or: MV Pelican' in text


class TestTheFileReadsByOutcome:
    def _write(self, tmp_path, triage):
        p = tmp_path / "decisions.toml"
        dec.append(p, dec.propose(triage, {}), "0303928", "run 1", triage=triage)
        return p.read_text()

    def test_a_summary_then_metadata_then_one_section_per_outcome(self, tmp_path):
        t = _triage(_file("data/a.csv"), _file("data/b.csv"),
                    _file("data/adcp.nc", action="exclude", data="current velocity",
                          why="WOD stores no current velocity", exclusion="current velocity"),
                    _file("data/m.nc", action="exclude", data="moored record", why="MRB",
                          exclusion="moored instrument time series", belongs_in_wod=True),
                    _doc("NCEI-Readme.txt"))
        text = self._write(tmp_path, t)
        assert "#   IN       2 files  as CTD (2)" in text
        assert "#   OUT      1 file   WOD takes them, but wod-etl cannot write them" in text
        order = ["[metadata]", "# ==== IN:", "# ==== OUT: WOD takes these",
                 "# ==== OUT: not something WOD stores", "# ==== DOCUMENTATION:"]
        assert [text.index(h) for h in order] == sorted(text.index(h) for h in order)
        entry = text[text.index('[files."data/'):]
        assert entry.index("include =") < entry.index("data =") < entry.index("why =")
        assert tomllib.loads(text)["files"]["NCEI-Readme.txt"] == {
            "include": False, "data": "NCEI readme"}               # its heading says why
        assert max(len(line) for line in text.splitlines() if line.startswith("#")) <= 100


class TestAnExistingFileIsReformattedKeepingItsDecisions:
    OLD = '''accession = "0303928"

[files."data/*.csv"]
data = "CTD profiles: temperature"
include = true
dataset = "CTD"
why = "CTD by its path ('CTD') and depth increment 1.00 m is high-resolution (< 2.0 m)"

[metadata]
Platform = "Shunyo-Maru"
"Principal investigator" = "A. Person"

[files."data/a.csv"]
data = "CTD profiles: temperature"
include = true
dataset = "CTD"
why = "the cast the PI asked for"
metadata = { Cruise = "SHU1505" }

[files."data/b.csv"]
data = "CTD profiles: temperature"
include = false
dataset = "CTD"
why = "CTD by its path ('CTD') and depth increment 1.00 m is high-resolution (< 2.0 m)"
'''

    def test_values_are_kept_and_only_untouched_reasons_and_dead_entries_change(self,
                                                                                tmp_path):
        p = tmp_path / "decisions.toml"
        p.write_text(self.OLD)
        old_why = "CTD by its path ('CTD') and depth increment 1.00 m is high-resolution (< 2.0 m)"
        new_why = "CTD by its path ('CTD') and levels 1.00 m apart (< 2.0 m)"
        old = _triage(_file("data/a.csv", why=old_why), _file("data/b.csv", why=old_why))
        new = _triage(_file("data/a.csv", why=new_why), _file("data/b.csv", why=new_why))
        res = dec.reformat(p, new, old)
        assert res["written"] and res["dropped"] == ["data/*.csv"]   # both files have their own
        d = tomllib.loads(p.read_text())
        assert d["files"]["data/a.csv"]["why"] == "the cast the PI asked for"   # the review's
        assert d["files"]["data/a.csv"]["metadata"] == {"cruise": "SHU1505"}
        assert d["files"]["data/b.csv"] == {"include": False, "dataset": "CTD",
                                            "data": "CTD profiles: temperature",
                                            "why": new_why}
        assert d["metadata"] == {"platform": "Shunyo-Maru",
                                 "principal_investigator": "A. Person"}
        assert (tmp_path / "decisions.toml.bak").read_text() == self.OLD
        text = p.read_text()
        assert text.index("[metadata]") < text.index("# ==== IN:") < text.index(
            "# ==== OUT: left out by the review")

    def test_a_file_that_does_not_parse_is_left_alone(self, tmp_path):
        p = tmp_path / "decisions.toml"
        p.write_text("broken = [\n")
        res = dec.reformat(p, _triage(_file("data/a.csv")))
        assert not res["written"] and p.read_text() == "broken = [\n"


class TestAMooringIsWorkedOutNotAsked:
    """A mooring's recipe is wod-etl's: its instruments' depths and its deployment come from
    the files (triage places them), and the person only says which instruments go in."""

    @staticmethod
    def _instrument(path, *, group="deep", placed_by="pressure", depth=None, include=True,
                    why="a mooring's instrument"):
        return _file(path, action="stage", dataset="MRB", data="moored instrument record",
                     why=why, include=include, mooring={
                         "platform": None, "instrument": "RBRsolo", "days": 13.6,
                         "variables": ["Temperature (degree_C)"], "group": group,
                         "pressure": placed_by == "pressure", "placed_by": placed_by,
                         "depth": depth, "depth_note": f"{depth} m, somehow",
                         "depth_short": "somehow" if placed_by else None,
                         "deployed": [1535941021.0, 1537114213.0], "deployment": "pressure"})

    def test_the_file_asks_only_include_and_the_plan_has_one_recipe_per_mooring(self):
        t = _triage(self._instrument("data/0-data/deep/rbr_1.nc", placed_by="description:position",
                                     depth=10.0),
                    self._instrument("data/0-data/deep/rbr_2.nc", placed_by="file", depth=12.5),
                    self._instrument("data/0-data/deep/sbe_1.nc", depth=34.7),
                    self._instrument("data/0-data/m2.nc", group="18bs8a", placed_by="file",
                                     depth=20.0),
                    _file("data/0-data/deep/adcp.nc", action="exclude", data="velocity",
                          why="no WOD code"))
        text = "\n".join(dec.propose(t, {}))
        d = tomllib.loads(text)
        assert set(d) == {"files", "metadata"} and "[profiles" not in text
        assert "Depths, as wod-etl places them:" in text and "rbr_1.nc    10.0 m  somehow" in text
        plan = dec.build_plan(d, t, [])
        assert set(plan["profiles"]) == {"deep", "18bs8a"}
        deep = plan["profiles"]["deep"]
        assert (deep["kind"], deep["dataset_type"]) == ("mooring", "MRB")
        assert deep["files"] == ["data/0-data/deep/rbr_1.nc", "data/0-data/deep/rbr_2.nc",
                                 "data/0-data/deep/sbe_1.nc"]
        assert deep["depths"] == {"rbr_1.nc": 10.0, "rbr_2.nc": 12.5, "sbe_1.nc": "pressure"}
        assert deep["deployed"] == ["2018-09-03T02:17:01Z", "2018-09-16T16:10:13Z"]
        assert deep["metadata"] == {"cruise": "deep"}
        assert plan["profiles"]["18bs8a"]["metadata"] == {"cruise": "18bs8a"}

    def test_an_instrument_with_no_depth_is_proposed_out_and_never_asked_for(self):
        t = _triage(self._instrument("deep/rbr_1.nc", placed_by=None, include=False,
                                     why="a mooring's instrument, but its depth is not known"),
                    self._instrument("deep/sbe_1.nc", depth=50.0))
        d = tomllib.loads("\n".join(dec.propose(t, {})))
        assert d["files"]["deep/rbr_1.nc"]["include"] is False
        items, plan = dec.validate(d, t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert plan["profiles"]["deep"]["files"] == ["deep/sbe_1.nc"]
        # Included anyway: it cannot be placed, so it is not written -- and that is said.
        d["files"]["deep/rbr_1.nc"]["include"] = True
        items, plan = dec.validate(d, t, {}, [])
        assert not [i for i in items if i["blocking"]]
        assert [w["key"] for w in plan["not_written"]] == ["deep/rbr_1.nc"]
        assert plan["consumed"] == ["deep/rbr_1.nc", "deep/sbe_1.nc"] and not plan["direct"]

    def test_instruments_that_cannot_be_staged_together_say_which_to_leave_out(self):
        t = _triage(self._instrument("deep/a.nc", depth=5.0),
                    self._instrument("deep/b.nc", depth=9.0))
        d = tomllib.loads("\n".join(dec.propose(t, {})))
        problem = "b.nc states 'Temperature (K)' in 'K' where a.nc states 'degree_C'"
        items, _ = dec.validate(d, t, {"deep": {"mooring_problems": [problem]}}, [])
        blocking = [i for i in items if i["blocking"]]
        assert len(blocking) == 1 and problem in blocking[0]["what"]
        assert "include = false" in blocking[0]["how_to_fix"]

    def test_two_moorings_of_one_name_are_two_recipes(self):
        t = _triage(self._instrument("c/p.nc", group="c", depth=5.0),
                    self._instrument("c/q.nc", group="c_2", depth=5.0))
        plan = dec.build_plan(tomllib.loads("\n".join(dec.propose(t, {}))), t, [])
        assert {tuple(r["files"]) for r in plan["profiles"].values()} == {("c/p.nc",),
                                                                         ("c/q.nc",)}
