"""WOD's file layout: one dataset and one year per file, named the way WOD names its own.

WOD distributes `wod_<dataset>_<year>.nc` (manual pp.44, 46; names from NCEI's listing).
Output adds the accession (`wod_ctd_2008_0062621.nc`) so it is never taken for WOD's own.
"""
import hashlib
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wod_etl.writers.generate_netcdf_ragged import cast_time, cast_year

# The year of a cast with no usable date. WOD has none such; an intermediate file may.
UNDATED = "undated"


def wod_file_name(dataset_code: Optional[str], year: Optional[int],
                  accession: Optional[str] = None) -> str:
    """`wod_ctd_2008_0062621.nc`; `wod_ctd_2008.nc` with no accession to name."""
    dataset = re.sub(r"[^a-z0-9]+", "", str(dataset_code or "").lower()) or "unresolved"
    when = f"{int(year):04d}" if year is not None else UNDATED
    tail = _accession_token(accession)
    return f"wod_{dataset}_{when}" + (f"_{tail}" if tail else "") + ".nc"


def is_wod_file_name(name: str, accession: Optional[str] = None) -> bool:
    """Whether `wod_file_name` writes `name` for this accession (a rerun touches only these)."""
    tail = _accession_token(accession)
    pattern = (rf"wod_[a-z0-9]+_(?:\d{{4}}|{UNDATED})"
               + (rf"_{re.escape(tail)}" if tail else "") + r"\.nc")
    return re.fullmatch(pattern, name) is not None


# Taxa-Records go in their own file per year: WOD's ragged NetCDF (Appendix 12) has no layout
# for them. Accession tokens never contain '_', so the suffix cannot clash with a profile name.
TAXA_SUFFIX = "_taxa.nc"


def taxa_file_name(year: Optional[int], accession: Optional[str] = None) -> str:
    """`wod_osd_2015_0303928_taxa.nc`."""
    return wod_file_name("OSD", year, accession)[:-len(".nc")] + TAXA_SUFFIX


def is_taxa_file_name(name: str, accession: Optional[str] = None) -> bool:
    """Whether `name` is a file `taxa_file_name` writes for this accession."""
    return (name.startswith("wod_osd_") and name.endswith(TAXA_SUFFIX)
            and is_wod_file_name(name[:-len(TAXA_SUFFIX)] + ".nc", accession))


def is_output_file_name(name: str, accession: Optional[str] = None) -> bool:
    """Any file the pipeline writes for this accession."""
    return is_wod_file_name(name, accession) or is_taxa_file_name(name, accession)


def folder_scope(path_or_url: str) -> str:
    """The scope for input that is not an accession: its folder's name (`my_cruise` for
    data/staging/my_cruise or a file in it). Local paths are resolved first."""
    text = str(path_or_url or "")
    if "://" not in text and text:
        p = os.path.realpath(text)
        text = os.path.dirname(p) if os.path.isfile(p) else p
    else:
        text = text.split("?", 1)[0]
    parts = [p for p in re.split(r"[\\/]+", text) if p not in ("", ".", "..")]
    return parts[-1] if parts else "local"


def accession_subpath(root: str, accession: Optional[str]) -> str:
    """Where a local input folder sits inside its accession's folder: `ship/` for
    `.../0287423/ship`, '' for that folder itself or one outside it. Source files are recorded
    from the accession's folder down (`ship/cast.csv`), never as local paths."""
    if not accession or "://" in str(root):
        return ""
    parts = Path(os.path.realpath(root)).parts
    for i in range(len(parts) - 1, -1, -1):
        same = parts[i] == accession or (parts[i].isdigit() and str(accession).isdigit()
                                         and int(parts[i]) == int(accession))
        if same:
            return "".join(f"{p}/" for p in parts[i + 1:])
    return ""


def _accession_token(accession: Optional[str]) -> str:
    """The scope as it goes in a file name. A scope with no usable characters becomes a
    digest, since an empty one would give WOD's own bare name."""
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", str(accession or "")).strip("-")
    if accession and not token:
        token = "scope-" + hashlib.sha1(str(accession).encode("utf-8")).hexdigest()[:8]
    return token


def group_by_file(sources: List[Dict[str, Any]],
                  accession: Optional[str] = None) -> List[Dict[str, Any]]:
    """Casts from many source files, as the WOD files they belong in.

    `sources` is `[{"key": str, "casts": [cast, ...]}, ...]`, each cast as
    `write_ragged_profiles` takes it. Returns one entry per dataset and year, in name order:
    `{"name", "dataset", "year", "casts", "sources": [{"key", "casts"}]}`. Within a file a
    source's casts stay together in order, and sources are ordered by first cast time.
    """
    groups: Dict[tuple, Dict[str, List[Any]]] = {}
    for src in sources:
        for cast in src.get("casts") or []:
            dataset = str(cast["contract"].get("wod_dataset_code") or "unresolved")
            year = cast_year(cast)
            groups.setdefault((dataset, year), {}).setdefault(src["key"], []).append(cast)

    files = []
    for (dataset, year), by_source in groups.items():
        order = sorted(by_source, key=lambda key: (_first_time(by_source[key]), key))
        files.append({"name": wod_file_name(dataset, year, accession), "dataset": dataset,
                      "year": year,
                      "casts": [c for key in order for c in by_source[key]],
                      "sources": [{"key": key, "casts": len(by_source[key])}
                                  for key in order]})
    return sorted(files, key=lambda f: f["name"])


def _first_time(casts) -> float:
    times = [t for t in map(cast_time, casts) if not math.isnan(t)]
    return min(times) if times else math.inf
