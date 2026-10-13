"""Phase 0 — Staging: build pipeline-ready copies of files that cannot be read as they are.

Discovery onward assumes a file carries its own coordinates and a parseable table. Real
accessions frequently break that assumption: positions live in a sibling station list, the
header spans two rows, the depth axis has to be derived from pressure, or the whole cruise
sits in one table that has to become one file per station.

These are primitives, not a universal converter. Each accession's sidecar is shaped
differently and a declarative joiner would be a liability; compose these instead. (The one
exception is deliberate and small: `wod-etl run` stages the commonest shapes from
decisions.toml, through wod_etl/orchestration/recipes.py, on these same functions.)
"""
from wod_etl.staging.parse import parse_coordinate, parse_timestamp, pressure_to_depth
from wod_etl.staging.tables import (read_table, clean_sentinels, group_rows,
                                    fold_unit_row, combine_levels, combine_on_key)
from wod_etl.staging.columns import rename_columns, suggest_column_names
from wod_etl.staging.write_cast import write_staged_cast, write_staged_table
from wod_etl.staging.pdf_tables import pdf_page_text, extract_pdf_rows
from wod_etl.staging.station_list import (read_station_list, join_station_list,
                                          normalise_station)
from wod_etl.staging.reshape import pivot_long, melt_matrix
from wod_etl.staging.reconcile import reconcile_stations, check_track, distance_km
from wod_etl.staging.taxa_haul import (write_staged_haul, read_staged_haul, is_staged_haul,
                                       biological_field, taxa_record_field, HAUL_MARKER)

__all__ = [
    "parse_coordinate", "parse_timestamp", "pressure_to_depth",
    "read_table", "clean_sentinels", "group_rows",
    "fold_unit_row", "combine_levels", "combine_on_key",
    "rename_columns", "suggest_column_names",
    "write_staged_cast", "write_staged_table",
    "pdf_page_text", "extract_pdf_rows",
    "read_station_list", "join_station_list", "normalise_station",
    "pivot_long", "melt_matrix",
    "reconcile_stations", "check_track", "distance_km",
    "write_staged_haul", "read_staged_haul", "is_staged_haul",
    "biological_field", "taxa_record_field", "HAUL_MARKER",
]
