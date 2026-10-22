"""Phase 0 — Staging: build pipeline-ready copies of files that cannot be read as they are.

Primitives for joining station lists, folding header rows, deriving depth and splitting
tables per station. Compose them per accession; `wod-etl run` uses them for common shapes.
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
from wod_etl.staging.moorings import (read_instrument, wet_window, in_water, deployment,
                                      daily_means, mooring_days, mooring_profiles,
                                      group_moorings)
from wod_etl.staging.mooring_layout import (read_layout, find_layout, place_instruments,
                                            depth_from_name, serial_key)

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
    "read_instrument", "wet_window", "in_water", "deployment", "daily_means", "mooring_days",
    "mooring_profiles", "group_moorings",
    "read_layout", "find_layout", "place_instruments", "depth_from_name", "serial_key",
]
