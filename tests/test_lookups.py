"""Country (Appendix 1), PI, unit, platform and instrument lookups, and metadata key precedence."""
import netCDF4
import pytest

from helpers import cache_code_list, make_cast
from wod_etl.mapping.map_metadata import map_metadata
from wod_etl.reference import country_codes, resolve_country
from wod_etl.reference.code_lists import lookup_investigator
from wod_etl.writers.generate_netcdf_ragged import write_ragged_profiles


class TestCountry:
    def test_appendix_1_and_the_online_additions(self):
        codes = country_codes()
        assert codes["99"] == "Unknown" and codes["NZ"] == "New Zealand"
        assert {"LR", "LB", "FO", "NA"} <= set(codes)

    @pytest.mark.parametrize("value, code, status", [
        ("NZ", "NZ", "already_a_code"), ("jp", "JP", "already_a_code"),
        ("Canada", "CA", "matched"), ("NEW ZEALAND", "NZ", "matched"),
        ("Republic of Korea", "KR", "matched"), ("United Kingdom", "GB", "matched"),
        ("USA", "US", "matched"), ("Atlantis", None, "no_match"), ("CAN", None, "no_match")])
    def test_resolve(self, value, code, status):
        r = resolve_country(value)
        assert (r["code"], r["status"]) == (code, status)

    def test_a_stated_country_outranks_a_creator_country(self):
        meta = map_metadata("creator_country: Germany\ncountry: Canada\n", [])
        assert meta["primary_header"]["country"]["value"] == "CA"

    def test_an_unknown_country_is_reported_not_guessed(self):
        meta = map_metadata("country: Atlantis\n", [])
        assert "country" not in meta["primary_header"]
        assert any(u["key"] == "country" for u in meta["unresolved"])

    def test_the_writer_writes_the_code_and_name(self, tmp_path):
        cast = make_cast()
        cast["contract"]["primary_header"] = {"country": {"value": "NZ", "label": "New Zealand"}}
        write_ragged_profiles([cast], str(tmp_path / "f.nc"))
        with netCDF4.Dataset(tmp_path / "f.nc") as ds:
            assert (ds.country, ds.country_name) == ("NZ", "New Zealand")


class TestPlatformAndCruise:
    def test_a_platform_name_outranks_a_platform_type(self):
        meta = map_metadata("platform: research vessel\nplatform_name: Hudson\n", [])
        assert meta["secondary_headers"]["3"]["originator_key"] == "platform_name"
        assert any(u["key"] == "platform" for u in meta["unresolved"])

    def test_a_cruise_id_outranks_a_cruise_name(self):
        meta = map_metadata("cruise_name: Fall survey 2016\ncruise_number: HUD2016027\n", [])
        assert meta["character_data"]["originator_cruise_id"]["value"] == "HUD2016027"


class TestPrincipalInvestigator:
    LIST = "WOD code,PI Name\n302,AAGAARD; DR. KNUT\n4091,POLYAKOV; DR. IGOR\n" \
           "10,SMITH; JOHN\n11,SMITH; JANE\n"

    @pytest.fixture(autouse=True)
    def pi_list(self, offline_code_lists):
        cache_code_list(offline_code_lists, "primary_investigator_list", self.LIST)

    @pytest.mark.parametrize("name, status, code", [
        ("Igor Polyakov", "matched", "4091"), ("Aagaard, Knut", "matched", "302"),
        ("J. Smith", "ambiguous", None), ("Ann Nobody", "no_match", None),
        ("Smith and Jones", "no_match", None)])
    def test_lookup(self, name, status, code):
        r = lookup_investigator(name)
        assert (r["status"], r.get("code")) == (status, code)

    def test_the_code_is_character_data(self):
        meta = map_metadata("chief_scientist: Igor Polyakov\n", [])
        assert meta["character_data"]["principal_investigator_code"]["value"] == "4091"


class TestOriginatorUnits:
    LIST = ("code_name,Original units\nunits,NODC code\n"
            "007,ug-at/l            mmol/m3  umol/l  uM  umol/dm3\n029,umol/kg\n"
            "032,mg/l               ppm      ug/g    ug/ml       ul/l    g/m3\n"
            "034,ueq/kg             umol/kg (for alkalinity ONLY)\n088,ml/l\n"
            "107,umol/l  Alternate Oxygen Conversion (use instead of #7)\n"
            "215,umol/l (use only for CFCs)\n")

    @pytest.fixture(autouse=True)
    def units_list(self, offline_code_lists):
        cache_code_list(offline_code_lists, "v_8_orig_units", self.LIST)

    @pytest.mark.parametrize("unit, code, status, expect", [
        ("µmol L−1", 4, "matched", "007"), ("umol/kg", 17, "matched", "034"),
        ("umol/l", 41, "matched", "215"), ("mg/L", 3, "matched", "032"),
        ("ml/l", 3, "matched", "088"), ("µmol kg−1", 4, "standard_unit", None),
        ("deg C", 1, "standard_unit", None), ("furlongs", 1, "no_match", None)])
    def test_lookup(self, unit, code, status, expect):
        from wod_etl.reference.code_lists import lookup_unit

        r = lookup_unit(unit, code)
        assert (r["status"], r.get("code")) == (status, expect)


class TestIdentifierTieBreak:
    LIST = ("code_name,Platform\nunits,OCL code\n"
            "4913,OSHORO MARU (F/V; call sign JDVA;built 01.1984; IMO8401365)\n"
            "11031,OSHORO MARU (R/V;call sign 7JRM;built 07.2014;IMO9699983)\n")

    @pytest.fixture(autouse=True)
    def platforms(self, offline_code_lists):
        cache_code_list(offline_code_lists, "s_3_platform", self.LIST)

    @pytest.mark.parametrize("value, status, code", [
        ("Oshoro Maru (call sign 7JRM)", "matched", "11031"),
        ("Oshoro Maru, IMO 8401365", "matched", "4913"),
        ("Oshoro Maru", "ambiguous", None)])
    def test_platform(self, value, status, code):
        from wod_etl.reference.code_lists import lookup_code

        r = lookup_code("secondary_header", 3, value)
        assert (r["status"], r.get("code")) == (status, code)


class TestInstrumentMaker:
    LIST = ("code_name,Instrument\nunits,OCL code\n201,XBT: T7 (UNKNOWN BRAND)\n"
            "207,XBT: T7 (SIPPICAN)\n")

    @pytest.fixture(autouse=True)
    def instruments(self, offline_code_lists):
        cache_code_list(offline_code_lists, "v_5_instrument", self.LIST)

    @pytest.mark.parametrize("value, code", [("T-7", "201"), ("T-7 (Sippican)", "207")])
    def test_maker(self, value, code):
        from wod_etl.reference.code_lists import lookup_code

        assert lookup_code("variable_specific_header", 5, value)["code"] == code
