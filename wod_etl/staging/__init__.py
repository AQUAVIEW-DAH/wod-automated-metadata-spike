"""Phase 0 — Staging: build pipeline-ready copies of files that cannot be read as they are.

Discovery onward assumes a file carries its own coordinates and a parseable table. Real
accessions frequently break that assumption: positions live in a sibling station list, the
header spans two rows, the depth axis has to be derived from pressure, or the whole cruise
sits in one table that has to become one file per station.

These are primitives, not a universal converter. Each accession's sidecar is shaped
differently and a declarative joiner would be a liability; compose these instead.
"""
from wod_etl.staging.parse import parse_coordinate, parse_timestamp, pressure_to_depth
from wod_etl.staging.tables import (read_table, clean_sentinels, group_rows,
                                    fold_unit_row, combine_levels, combine_on_key)
from wod_etl.staging.write_cast import write_staged_cast

__all__ = [
    "parse_coordinate", "parse_timestamp", "pressure_to_depth",
    "read_table", "clean_sentinels", "group_rows",
    "fold_unit_row", "combine_levels", "combine_on_key",
    "write_staged_cast",
]
