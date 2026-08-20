"""Phase 4: File Generation & Handoff skills.

Validation moved to `wod_etl.verify`, alongside the dataset audit it belongs with.
"""

from .generate_netcdf_ragged import (
    generate_netcdf_ragged,
    write_ragged_profiles,
    cf_safe_name,
)

__all__ = [
    "generate_netcdf_ragged",
    "write_ragged_profiles",
    "cf_safe_name",
]
