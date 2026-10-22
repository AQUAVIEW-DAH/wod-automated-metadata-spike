"""A mooring's instruments become WOD MRB profiles: one a day, each instrument's daily mean at
its depth (manual pp.14, 17)."""
import datetime as dt

import netCDF4
import numpy as np
import pytest

from wod_etl.staging import (deployment, depth_from_name, group_moorings, mooring_days,
                             mooring_profiles, place_instruments, pressure_to_depth, read_layout,
                             read_instrument, serial_key, wet_window)
from wod_etl.staging.mooring_layout import file_serial

T0 = dt.datetime(2018, 9, 3, 6, 0, tzinfo=dt.timezone.utc).timestamp()   # 06:00 UTC


def write_instrument(path, *, hours=78.0, step=600, temperature=None, pressure=None,
                     absolute=False, salinity=False, depth_attr=None, lat=56.84, lon=-132.36,
                     extra=None, serial="100020"):
    """Write an RBR/Sea-Bird-style instrument record; `temperature` and `pressure` are
    functions of hours since T0."""
    t = T0 + np.arange(0, hours * 3600 + 1, step)
    h = (t - T0) / 3600
    with netCDF4.Dataset(path, "w") as ds:
        ds.model = "RBRsolo"
        ds.serialNumber = serial
        if absolute:
            ds.pressureType, ds.pressureAtmospheric = "absolute", "10.1325"
        if depth_attr is not None:
            ds.instrument_depth = depth_attr
        ds.createDimension("time", t.size)
        for name, value in (("lat", lat), ("lon", lon)):
            v = ds.createVariable(name, "f8")
            v.units = "degree_north" if name == "lat" else "degree_east"
            v.standard_name = "latitude" if name == "lat" else "longitude"
            v.assignValue(value)
        tv = ds.createVariable("time", "f8", ("time",))
        tv.units = "seconds since 1970-01-01 00:00:00"
        tv[:] = t
        columns = [("t", "Temperature", "degree_C", "sea_water_temperature",
                    temperature or (lambda h: 5.0 + 0 * h))]
        if pressure is not None:
            columns.append(("p", "Pressure", "dbar", "sea_water_pressure", pressure))
        if salinity:
            columns.append(("SP", "Practical salinity", "", "sea_water_practical_salinity",
                            lambda h: 30.0 + 0 * h))
        for name, long_name, units, std, f in columns + list(extra or []):
            v = ds.createVariable(name, "f8", ("time",), fill_value=np.nan)
            v.long_name, v.standard_name = long_name, std
            if units:
                v.units = units
            v[:] = f(h)
    return path


class TestReadingAnInstrument:
    def test_what_it_records_and_what_mrb_does_not_store(self, tmp_path):
        p = write_instrument(tmp_path / "sbe.nc", pressure=lambda h: 35.0 + 0 * h, salinity=True,
                             extra=[("C", "Conductivity", "S m-1",
                                     "sea_water_electrical_conductivity", lambda h: 3 + 0 * h)])
        info = read_instrument(p)
        assert [v["code"] for v in info["variables"]] == [1, 25, 2]
        assert info["pressure"] == "p" and info["not_stored"] == ["Conductivity (S m-1)"]
        assert (info["latitude"], info["longitude"]) == pytest.approx((56.84, -132.36))
        assert info["instrument"] == "RBRsolo SN 100020" and info["error"] is None

    def test_a_stated_depth_and_absolute_pressure_are_read(self, tmp_path):
        info = read_instrument(write_instrument(tmp_path / "a.nc", depth_attr=12.5,
                                                pressure=lambda h: 22.6 + 0 * h, absolute=True))
        [p] = [v for v in info["variables"] if v["code"] == 25]
        assert info["depth"] == 12.5 and p["offset"] == pytest.approx(10.1325)

    def test_flags_are_not_measurements(self, tmp_path):
        info = read_instrument(write_instrument(tmp_path / "f.nc", extra=[
            ("t_qc", "Temperature quality flag", "", "", lambda h: 1 + 0 * h)]))
        assert [v["name"] for v in info["variables"]] == ["t"]

    def test_a_file_of_several_depths_is_said_not_read(self, tmp_path):
        p = tmp_path / "chain.nc"
        with netCDF4.Dataset(p, "w") as ds:
            ds.createDimension("time", 50)
            ds.createDimension("depth", 3)
            tv = ds.createVariable("time", "f8", ("time",))
            tv.units = "hours since 2018-09-01"
            tv[:] = np.arange(50)
            v = ds.createVariable("T", "f8", ("time", "depth"))
            v.long_name, v.units = "Temperature", "degree_C"
            v[:] = 5.0
        assert "several depths" in read_instrument(p)["error"]


def _profiles(paths, depths, window=None, **kw):
    instruments = [read_instrument(p) for p in paths]
    return mooring_profiles(instruments, mooring_days(paths, instruments, window), depths,
                            latitude=56.84, **kw)


def _deployed(paths):
    return deployment([wet_window(p, read_instrument(p)) for p in paths])


class TestDailyMeans:
    def test_one_profile_a_day_of_daily_means_and_part_days_left_out(self, tmp_path):
        # 06:00 on 3 Sept to 12:00 on 6 Sept: the first and last days are 75% and 50% recorded
        p = write_instrument(tmp_path / "t.nc", temperature=lambda h: 5.0 + np.floor((h + 6) / 24))
        built = _profiles([p], {"t.nc": 10.0})
        assert [x["day"] for x in built["profiles"]] == ["2018-09-04", "2018-09-05"]
        assert [x["levels"][0]["means"][1] for x in built["profiles"]] == pytest.approx([6.0, 7.0])
        assert {g["day"] for g in built["left_out"]} == {"2018-09-03", "2018-09-06"}
        loose = _profiles([p], {"t.nc": 10.0}, min_day_fraction=0.7)
        assert [x["day"] for x in loose["profiles"]][0] == "2018-09-03"

    def test_instruments_are_levels_placed_by_pressure_or_their_stated_depth(self, tmp_path):
        a = write_instrument(tmp_path / "rbr.nc", temperature=lambda h: 8.0 + 0 * h)
        b = write_instrument(tmp_path / "sbe.nc", temperature=lambda h: 6.0 + 0 * h,
                             pressure=lambda h: 35.0 + 0 * h, salinity=True)
        built = _profiles([a, b], {"rbr.nc": 10.0, "sbe.nc": "pressure"})
        day = built["profiles"][0]
        assert [lv["file"] for lv in day["levels"]] == ["rbr.nc", "sbe.nc"]     # shallowest first
        assert day["levels"][1]["depth"] == pytest.approx(pressure_to_depth(35.0, 56.84), abs=0.01)
        assert day["levels"][1]["means"] == pytest.approx({1: 6.0, 25: 35.0, 2: 30.0})

    def test_air_before_deployment_and_after_recovery_is_not_averaged(self, tmp_path):
        # In air for the first and last 6 h (0 dbar, 20 C); in the water 40 dbar and 5 C.
        wet = lambda h: (h >= 6) & (h <= 72)
        a = write_instrument(tmp_path / "sbe.nc", pressure=lambda h: np.where(wet(h), 40.0, 0.0),
                             temperature=lambda h: np.where(wet(h), 5.0, 20.0))
        b = write_instrument(tmp_path / "rbr.nc",
                             temperature=lambda h: np.where(wet(h), 4.0, 20.0))
        found = _deployed([a, b])
        assert found["how"] == "pressure"
        built = _profiles([a, b], {"sbe.nc": "pressure", "rbr.nc": 30.0},
                          window=found["window"], min_day_fraction=0.1)
        means = {(x["day"], lv["file"]): lv["means"][1] for x in built["profiles"]
                 for lv in x["levels"]}
        assert set(means.values()) == {5.0, 4.0}               # no air temperature anywhere
        assert built["window"] == ["2018-09-03T12:00:00Z", "2018-09-06T06:00:00Z"]

    def test_absolute_pressure_has_the_atmosphere_taken_off(self, tmp_path):
        p = write_instrument(tmp_path / "abs.nc", pressure=lambda h: 45.1325 + 0 * h,
                             absolute=True)
        built = _profiles([p], {"abs.nc": "pressure"})
        assert built["profiles"][0]["levels"][0]["means"][25] == pytest.approx(35.0)

    def test_an_instrument_with_no_depth_is_not_placed(self, tmp_path):
        p = write_instrument(tmp_path / "t.nc")
        assert _profiles([p], {"t.nc": "?"})["profiles"] == []


class TestWhatTheReviewFound:
    """Cases that must not silently give a wrong mooring profile."""

    def test_a_logger_stopping_early_does_not_cut_the_others_days(self, tmp_path):
        a = write_instrument(tmp_path / "a.nc", hours=240, pressure=lambda h: 40.0 + 0 * h)
        b = write_instrument(tmp_path / "b.nc", hours=240,
                             pressure=lambda h: np.where(h < 60, 50.0, np.nan))
        found = _deployed([a, b])
        assert found["how"] == "pressure"
        assert found["window"][1] - found["window"][0] == pytest.approx(240 * 3600)

    def test_two_deployments_are_not_one(self, tmp_path):
        a = write_instrument(tmp_path / "a.nc", hours=240,
                             pressure=lambda h: np.where(h < 100, 40.0, 0.0))
        b = write_instrument(tmp_path / "b.nc", hours=240,
                             pressure=lambda h: np.where(h > 140, 40.0, 0.0))
        assert _deployed([a, b]) == {"window": None, "how": "apart"}

    def test_without_pressure_nothing_is_guessed(self, tmp_path):
        a = write_instrument(tmp_path / "a.nc")
        assert _deployed([a]) == {"window": None, "how": "none"}

    def test_the_instruments_own_temperature_and_the_air_are_not_the_water(self, tmp_path):
        p = write_instrument(tmp_path / "x.nc", extra=[
            ("tp", "Pressure sensor temperature", "degree_C", "", lambda h: 30 + 0 * h),
            ("Patm", "Atmospheric pressure", "dbar", "", lambda h: 10 + 0 * h),
            ("osat", "Oxygen saturation", "%", "", lambda h: 98 + 0 * h)])
        info = read_instrument(p)
        assert [(v["name"], v["code"]) for v in info["variables"]] == [("t", 1)]
        assert sum("not a measurement of the water" in x for x in info["not_stored"]) == 3

    def test_a_standard_name_beats_a_name_and_sea_pressure_beats_absolute(self, tmp_path):
        p = write_instrument(tmp_path / "rbr.nc", absolute=True, extra=[
            ("sea_pressure", "Sea pressure", "dbar", "sea_water_pressure_due_to_sea_water",
             lambda h: 35.0 + 0 * h),
            ("pressure", "Pressure", "dbar", "sea_water_pressure", lambda h: 45.1 + 0 * h)])
        [p25] = [v for v in read_instrument(p)["variables"] if v["code"] == 25]
        assert (p25["name"], p25["offset"]) == ("sea_pressure", 0.0)   # gauge: nothing off

    def test_pressure_in_another_unit_is_converted_and_an_unknown_one_refused(self, tmp_path):
        kpa = read_instrument(write_instrument(tmp_path / "k.nc", extra=[
            ("p", "Pressure", "kPa", "sea_water_pressure", lambda h: 350 + 0 * h)]))
        assert [v["factor"] for v in kpa["variables"] if v["code"] == 25] == [0.1]
        odd = read_instrument(write_instrument(tmp_path / "o.nc", extra=[
            ("p", "Pressure", "furlongs", "sea_water_pressure", lambda h: 1 + 0 * h)]))
        assert odd["pressure"] is None and "not a unit read as decibars" in odd["not_stored"][0]

    def test_values_the_originator_flags_bad_are_not_averaged(self, tmp_path):
        p = write_instrument(tmp_path / "f.nc", hours=48, step=600,
                             temperature=lambda h: np.where(np.isclose(h % 24, 12), 30.0, 7.0))
        with netCDF4.Dataset(p, "a") as ds:
            q = ds.createVariable("t_qc", "i1", ("time",))
            q.flag_values = np.array([1, 3, 4], dtype="i1")
            q.flag_meanings = "pass suspect fail"
            q[:] = np.where(np.isclose((ds["time"][:] - T0) / 3600 % 24, 12), 4, 1)
            ds["t"].ancillary_variables = "t_qc"
        built = _profiles([p], {"f.nc": 10.0})
        assert {x["levels"][0]["means"][1] for x in built["profiles"]} == {7.0}

    def test_a_variable_must_cover_the_day_not_only_its_timestamps(self, tmp_path):
        p = write_instrument(tmp_path / "s.nc", hours=72, salinity=True, extra=[])
        with netCDF4.Dataset(p, "a") as ds:          # salinity only for one hour a day
            h = (ds["time"][:] - T0) / 3600
            ds["SP"][:] = np.where((h % 24) < 1, 30.0, np.nan)
        day = _profiles([p], {"s.nc": 10.0})["profiles"][1]["levels"][0]["means"]
        assert 1 in day and 2 not in day

    def test_a_change_of_sampling_rate_is_not_a_gap(self, tmp_path):
        p = write_instrument(tmp_path / "r.nc", hours=24 * 6, step=60)
        with netCDF4.Dataset(p, "a") as ds:          # 1-minute, then 10-minute samples
            t = ds["time"][:]
            keep = (t - T0 < 3 * 86400) | (((t - T0) / 60) % 10 == 0)
            ds["t"][~keep] = np.nan
        built = _profiles([p], {"r.nc": 10.0})
        assert len(built["profiles"]) == 5        # every whole UTC day of the six

    def test_positions_west_south_and_in_words(self, tmp_path):
        p = write_instrument(tmp_path / "w.nc", lat=56.84, lon=132.36)
        with netCDF4.Dataset(p, "a") as ds:
            ds["lon"].units, ds["lon"].standard_name = "degree_W", ""
            h = ds.createVariable("heading", "f8")
            h.units = "degrees"
            h.assignValue(271.0)
        info = read_instrument(p)
        assert (info["latitude"], info["longitude"]) == pytest.approx((56.84, -132.36))

    def test_time_on_another_calendar_is_refused_and_a_missing_time_is_skipped(self, tmp_path):
        p = write_instrument(tmp_path / "c.nc")
        with netCDF4.Dataset(p, "a") as ds:
            ds["time"].calendar = "noleap"
        assert "noleap" in read_instrument(p)["error"]
        q = write_instrument(tmp_path / "n.nc", pressure=lambda h: 40 + 0 * h)
        with netCDF4.Dataset(q, "a") as ds:
            ds["time"][0] = np.nan
        assert wet_window(q, read_instrument(q)) is not None


# Positions are along the line from a reference point; a blank reference means the block's.
# The second block is a frame listed without positions.
LAYOUT = """# Downstream deep mooring

   |-----------------+--------------------|
   | Reference Point | Description        |
   |-----------------+--------------------|
   |               1 | Bottom of 50m line |
   |-----------------+--------------------|

   |----------+---------+----------+--------|
   | Type     | Pos (m) | Ref. Pt. | Serial |
   |----------+---------+----------+--------|
   | SBE37SM  |      48 |          |  10552 |
   | RBR Solo |      45 |        1 | 100028 |
   | RBR Solo |      10 |        1 | 100020 |
   |----------+---------+----------+--------|
   | SBE37SM  |         |          |  10551 |
   | RBR Solo |         |          | 100320 |
   |----------+---------+----------+--------|
"""


def _layout(tmp_path, text=LAYOUT):
    p = tmp_path / "mooring_description.txt"
    p.write_text(text)
    return read_layout(p)


def _member(name, serial, depth=None):
    return {"name": name, "serial": serial, "depth": depth, "by_pressure": depth is not None}


class TestPlacingLoggersByTheMooringsDescription:
    def test_the_table_is_read_with_its_reference_points_and_blocks(self, tmp_path):
        lay = _layout(tmp_path)
        assert lay["references"] == {"1": "Bottom of 50m line"} and lay["blocks"] == 2
        assert [(r["serial"], r["position"], r["ref"]) for r in lay["rows"]] == [
            ("10552", 48.0, None), ("100028", 45.0, "1"), ("100020", 10.0, "1"),
            ("10551", None, None), ("100320", None, None)]

    def test_positions_become_depths_fixed_by_a_pressure_instrument_on_the_line(self, tmp_path):
        res = place_instruments(_layout(tmp_path), [
            _member("sbe_top", "10552", 34.7), _member("sbe_frame", "10551", 86.4),
            _member("rbr_45", "100028"), _member("rbr_10", "100020"),
            _member("rbr_frame", "100320")])
        assert {k: v["depth"] for k, v in res["placed"].items()} == {
            "rbr_45": 37.7, "rbr_10": 72.7, "rbr_frame": 86.4}
        assert res["placed"]["rbr_45"]["how"] == "position"
        assert "Bottom of 50m line" in res["placed"]["rbr_45"]["detail"]
        assert res["placed"]["rbr_frame"]["how"] == "beside" and not res["unplaced"]

    def test_a_position_no_pressure_instrument_fixes_places_nothing(self, tmp_path):
        res = place_instruments(_layout(tmp_path), [
            _member("sbe_frame", "10551", 86.4), _member("rbr_45", "100028")])
        assert not res["placed"] and "records pressure" in res["unplaced"]["rbr_45"]

    def _between(self, tmp_path, depth):
        """A second pressure instrument listed between the positions 45 and 10."""
        text = LAYOUT.replace("| RBR Solo |      10 |        1 | 100020 |",
                              "| SBE37SM  |         |        1 |   7819 |\n"
                              "   | RBR Solo |      10 |        1 | 100020 |")
        return place_instruments(_layout(tmp_path, text), [
            _member("sbe_top", "10552", 34.7), _member("sbe_mid", "7819", depth),
            _member("rbr_45", "100028"), _member("rbr_10", "100020")])

    def test_a_pressure_instrument_between_two_positions_moves_the_line_to_agree(self,
                                                                                 tmp_path):
        res = self._between(tmp_path, 74.0)     # puts the reference point 84-119 m deep
        assert {k: v["depth"] for k, v in res["placed"].items()} == {"rbr_45": 39.0,
                                                                     "rbr_10": 74.0}
        assert "moved 1.3 m to agree with sbe_mid" in res["placed"]["rbr_45"]["detail"]

    def test_a_table_the_pressure_records_disagree_with_places_nothing(self, tmp_path):
        res = self._between(tmp_path, 10.0)     # 27.7 m from where the table lists it
        assert not res["placed"] and res["problems"]
        assert "27.7 m from where mooring_description.txt lists it" in res["unplaced"]["rbr_45"]

    def test_a_stated_depth_is_used_as_it_stands(self, tmp_path):
        lay = _layout(tmp_path, "| Serial | Depth (m) |\n|---|---|\n| 100020 | 25 |\n"
                                "| SN 100021 | 50 |\n")
        res = place_instruments(lay, [_member("a", "100020"), _member("b", "100021")])
        assert {k: (v["depth"], v["how"]) for k, v in res["placed"].items()} == {
            "a": (25.0, "stated"), "b": (50.0, "stated")}

    def test_serial_numbers_and_depths_in_file_names(self):
        assert serial_key("07819") == serial_key("37SM56061-7819") == "7819"
        assert file_serial("RBRsolo_SN100020.nc") == "100020"
        assert file_serial("workhorse300_upward.nc") is None
        assert depth_from_name("T_45m.nc") == 45.0
        assert depth_from_name("SBE56-12.5m_SN1.csv") == 12.5
        assert depth_from_name("LeConte_Sept2018_kayak300kHz_ADCP.nc") is None


def _entry(path, **mooring):
    return {"path": path, "mooring": {"platform": None, "latitude": None, "longitude": None,
                                      "record": [0.0, 3 * 86400.0], **mooring}}


class TestMooringsAreGrouped:
    def test_by_their_id_or_their_place_and_each_label_names_one_mooring(self):
        groups = dict(group_moorings([
            _entry("a/x.nc", platform="M1"), _entry("b/y.nc", platform="M1"),
            _entry("c/p.nc", latitude=56.0, longitude=-132.0),
            _entry("c/q.nc", latitude=57.0, longitude=-132.0),
            _entry("d/z.nc", platform="moored buoy")]))           # a kind, not an id
        assert sorted(f["path"] for f in groups["M1"]) == ["a/x.nc", "b/y.nc"]
        assert {tuple(f["path"] for f in groups[k]) for k in ("c", "c_2")} == {("c/p.nc",),
                                                                               ("c/q.nc",)}
        assert [f["path"] for f in groups["d"]] == ["d/z.nc"]

    def test_records_that_do_not_overlap_in_time_are_two_deployments(self):
        groups = group_moorings([_entry("m/a.nc", record=[0.0, 86400.0]),
                                 _entry("m/b.nc", record=[10 * 86400.0, 12 * 86400.0])])
        assert [label for label, _ in groups] == ["m_1970-01-01", "m_1970-01-11"]


class TestTriagePlacesAMooring:
    def test_loggers_are_placed_by_the_description_and_one_it_misses_is_proposed_out(
            self, tmp_path):
        from wod_etl.discovery import classify_accession

        d = tmp_path / "data" / "0-data" / "deep"
        d.mkdir(parents=True)
        write_instrument(d / "SBE37SM_SN10552.nc", serial="10552", pressure=lambda h: 35 + 0 * h)
        write_instrument(d / "SBE37SM_SN10551.nc", serial="10551",
                         pressure=lambda h: 87.2 + 0 * h)
        for serial in ("100028", "100320", "100999"):           # the last is not listed
            write_instrument(d / f"RBRsolo_SN{serial}.nc", serial=serial)
        (d / "mooring_description.txt").write_text(LAYOUT)
        files = {f["path"].rsplit("/", 1)[-1]: f
                 for f in classify_accession(str(tmp_path))["files"]}
        rbr = files["RBRsolo_SN100028.nc"]
        assert rbr["include"] is True and "mooring_description.txt" in rbr["why"]
        assert rbr["mooring"]["depth"] == pytest.approx(37.7, abs=0.1)
        assert rbr["mooring"]["deployed"] is not None and rbr["mooring"]["group"] == "deep"
        assert files["RBRsolo_SN100320.nc"]["mooring"]["depth"] == pytest.approx(86.4, abs=0.1)
        assert files["SBE37SM_SN10552.nc"]["mooring"]["placed_by"] == "pressure"
        lost = files["RBRsolo_SN100999.nc"]
        assert lost["include"] is False and "its depth is not known" in lost["why"]
        assert "no description in the accession lists its serial number" in lost["why"]

    def test_a_mooring_with_no_pressure_drops_its_first_and_last_day(self, tmp_path):
        from wod_etl.discovery import classify_accession

        write_instrument(tmp_path / "T_20m.nc", hours=24 * 5)       # its depth in its name
        [f] = classify_accession(str(tmp_path))["files"]
        m = f["mooring"]
        assert (f["include"], m["depth"], m["placed_by"]) == (True, 20.0, "name")
        assert m["deployment"] == "record" and "first and last day" in f["why"]
        first, last = m["deployed"]
        assert first == (T0 // 86400 + 1) * 86400 and last < (T0 // 86400 + 5) * 86400
