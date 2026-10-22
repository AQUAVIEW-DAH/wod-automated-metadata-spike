"""Phase 4: file generation -- WOD ragged-array NetCDF, profiles and Taxa-Records.

Validation is in `wod_etl.verify`.
"""

from .generate_netcdf_ragged import (
    generate_netcdf_ragged,
    write_ragged_profiles,
    cf_safe_name,
)
from .generate_netcdf_taxa import write_taxa_netcdf
from .wod_files import (group_by_file, is_output_file_name, is_taxa_file_name,
                        is_wod_file_name, taxa_file_name, wod_file_name)

__all__ = [
    "generate_netcdf_ragged",
    "write_ragged_profiles",
    "cf_safe_name",
    "write_taxa_netcdf",
    "wod_file_name",
    "is_wod_file_name",
    "taxa_file_name",
    "is_taxa_file_name",
    "is_output_file_name",
    "group_by_file",
]
