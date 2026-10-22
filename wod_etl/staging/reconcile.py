"""Check station positions and dates against every other source in the accession.

    from wod_etl.staging import reconcile_stations, check_track

    report = reconcile_stations({
        "stations.csv": stations,                        # from read_station_list
        "trawls.csv": trawls,
        "cruise report p.4 Table 1": table1,             # from extract_pdf_rows
    }, position_tolerance_km=10)
    report["conflicts"]

    track = check_track(stations)                        # in occupation order
    track["flags"]

Neither changes anything; both report disagreements and which source said what.
"""
import datetime as dt
import math
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from wod_etl.staging.station_list import normalise_station

EARTH_RADIUS_KM = 6371.0088


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine), kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def reconcile_stations(
    sources: Dict[str, Dict[Any, Dict[str, Any]]],
    *,
    position_tolerance_km: float = 5.0,
    compare: Sequence[str] = ("position", "date"),
) -> Dict[str, Any]:
    """Compare every station's position and date across sources.

    `sources` maps a source name (e.g. `"report p.4 Table 1"`) to `{station: {"latitude",
    "longitude", "date"}}`, as `read_station_list` returns. Station keys are compared through
    `normalise_station`. `position_tolerance_km` is how far apart two positions may be and
    still agree; allow more for sources sampled apart, such as a trawl and a CTD station.

    Returns `conflicts` (field, each source's value, spread), `single_source` (stations only
    one source states) and per-station detail.
    """
    by_station: Dict[str, Dict[str, Dict[str, Any]]] = {}
    labels: Dict[str, str] = {}
    for src, stations in sources.items():
        for key, entry in (stations or {}).items():
            raw = entry.get("station", key)
            norm = normalise_station(raw)
            if "#" in str(key):
                norm = f"{norm}{str(key)[str(key).index('#'):]}"
            labels.setdefault(norm, str(raw))
            by_station.setdefault(norm, {})[src] = entry

    conflicts: List[Dict[str, Any]] = []
    detail: Dict[str, Any] = {}
    single = []
    for station, per_src in by_station.items():
        row: Dict[str, Any] = {"sources": {}}
        for src, e in per_src.items():
            row["sources"][src] = {"latitude": _num(e.get("latitude")),
                                   "longitude": _num(e.get("longitude")),
                                   "date": e.get("date")}
        if len(per_src) < 2:
            single.append(labels[station])
            detail[labels[station]] = row
            continue

        if "position" in compare:
            pts = [(s, v["latitude"], v["longitude"]) for s, v in row["sources"].items()
                   if v["latitude"] is not None and v["longitude"] is not None]
            spread, pair = 0.0, None
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    d = distance_km(pts[i][1], pts[i][2], pts[j][1], pts[j][2])
                    if d > spread:
                        spread, pair = d, (pts[i][0], pts[j][0])
            row["position_spread_km"] = round(spread, 2)
            if spread > position_tolerance_km:
                conflicts.append({
                    "station": labels[station], "field": "position",
                    "spread_km": round(spread, 2), "between": pair,
                    "values": {s: (v["latitude"], v["longitude"])
                               for s, v in row["sources"].items()},
                    "note": f"{spread:.1f} km apart, beyond the {position_tolerance_km} km "
                            f"tolerance. One of these is not where the station was."})

        if "date" in compare:
            dates = {s: v["date"] for s, v in row["sources"].items() if v["date"]}
            row["dates"] = dates
            if len(set(dates.values())) > 1:
                conflicts.append({
                    "station": labels[station], "field": "date", "values": dates,
                    "note": "the sources give different dates. Look for a third source -- a "
                            "fishing log, a time-stamped event -- before choosing one."})
        detail[labels[station]] = row

    return {
        "sources": list(sources),
        "stations_checked": sum(1 for v in by_station.values() if len(v) > 1),
        "position_tolerance_km": position_tolerance_km,
        "conflicts": conflicts,
        "single_source": single,
        "stations": detail,
        "note": ("Every conflict is a value to confirm with the submitter or settle from a "
                 "third source. Record the decision in the staged notes either way."
                 if conflicts else None),
    }


def _stamp(entry: Dict[str, Any]) -> Tuple[Optional[dt.datetime], bool]:
    """(datetime or None, whether the time of day is known)."""
    d = entry.get("date")
    if not d:
        return None, False
    t = entry.get("time")
    try:
        if t:
            return dt.datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M"), True
        return dt.datetime.strptime(str(d), "%Y-%m-%d"), False
    except ValueError:
        return None, False


def check_track(
    stations,
    *,
    outlier_factor: float = 1.75,
    max_speed_knots: float = 20.0,
    bounding_box: Optional[Tuple[float, float, float, float]] = None,
) -> Dict[str, Any]:
    """Flag stations along the cruise track that are not where their neighbours suggest.

    `stations` is in occupation order: the dict `read_station_list` returns, or a list of
    entries with `latitude`, `longitude` and optionally `date`/`time`. Flags:

    * outlying step -- a hop over `outlier_factor` times the median hop
    * time running backwards -- a station dated before the previous one
    * impossible speed -- above `max_speed_knots`, when both stations have a time

    `bounding_box` is `(lon_min, lon_max, lat_min, lat_max)`; stations outside it are flagged.
    """
    entries = list(stations.values()) if isinstance(stations, dict) else list(stations)
    name = lambda e, i: str(e.get("station", i))
    steps, flags = [], []

    for i in range(1, len(entries)):
        a, b = entries[i - 1], entries[i]
        la, oa, lb, ob = (_num(a.get("latitude")), _num(a.get("longitude")),
                          _num(b.get("latitude")), _num(b.get("longitude")))
        if None in (la, oa, lb, ob):
            continue
        km = distance_km(la, oa, lb, ob)
        step = {"from": name(a, i - 1), "to": name(b, i), "km": round(km, 2)}
        (ta, ka), (tb, kb) = _stamp(a), _stamp(b)
        if ta and tb:
            # A date-only stamp sits at 00:00, so compare dates unless both have times.
            backwards = tb < ta if (ka and kb) else tb.date() < ta.date()
            if backwards:
                flags.append({"check": "time_backwards", "from": step["from"], "to": step["to"],
                              "evidence": f"{step['to']} ({b.get('date')}) is dated before "
                                          f"{step['from']} ({a.get('date')})"})
            if ka and kb and tb > ta:
                hours = (tb - ta).total_seconds() / 3600.0
                knots = km / 1.852 / hours
                step.update(hours=round(hours, 2), knots=round(knots, 1))
                if knots > max_speed_knots:
                    flags.append({"check": "impossible_speed", "from": step["from"],
                                  "to": step["to"],
                                  "evidence": f"{km:.1f} km in {hours:.1f} h is {knots:.1f} "
                                              f"knots (limit {max_speed_knots})"})
        steps.append(step)

    median = statistics.median(s["km"] for s in steps) if steps else None
    if median and median > 0:
        for s in steps:
            if s["km"] > outlier_factor * median:
                flags.append({"check": "outlying_step", "from": s["from"], "to": s["to"],
                              "evidence": f"{s['km']:.1f} km, {s['km'] / median:.2f}x the "
                                          f"median hop of {median:.1f} km",
                              "note": "Check this station's position against every other "
                                      "source: a mistyped degree or minute looks like this."})

    if bounding_box:
        lon_min, lon_max, lat_min, lat_max = bounding_box
        for i, e in enumerate(entries):
            lat, lon = _num(e.get("latitude")), _num(e.get("longitude"))
            if lat is None or lon is None:
                continue
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                flags.append({"check": "outside_bounding_box", "station": name(e, i),
                              "evidence": f"({lat}, {lon}) is outside {bounding_box}"})

    return {"steps": steps, "median_step_km": round(median, 2) if median else None,
            "flags": flags, "outlier_factor": outlier_factor}
