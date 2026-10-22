"""WOD reference tables, loaded from knowledge/wod_code_tables/*.json.

The tables were extracted from the WOD manual with page citations; codes are not hard-coded.
"""

from .load_tables import (
    load_table,
    available_tables,
    wod_datasets,
    wod_variables,
    wod_secondary_headers,
    variable_specific_headers,
    biological_headers,
    missing_biological_fields,
    taxa_biomass_variables,
    taxa_code_ranges,
    plankton_grouping_code,
    classify_taxa_code,
    code_table_links,
    code_table_link,
    code_table_hub,
    qc_flag_scales,
    observed_level_flags,
    depth_flags,
    gradient_factors,
    range_levels,
    unit_constants,
    range_check_variables,
)

__all__ = [
    "load_table",
    "available_tables",
    "wod_datasets",
    "wod_variables",
    "wod_secondary_headers",
    "variable_specific_headers",
    "biological_headers",
    "missing_biological_fields",
    "taxa_biomass_variables",
    "taxa_code_ranges",
    "plankton_grouping_code",
    "classify_taxa_code",
    "code_table_links",
    "code_table_link",
    "code_table_hub",
    "qc_flag_scales",
    "observed_level_flags",
    "depth_flags",
    "gradient_factors",
    "range_levels",
    "unit_constants",
    "range_check_variables",
]
