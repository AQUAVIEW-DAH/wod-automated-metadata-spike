"""WOD reference knowledge: the manual's tables, loaded rather than remembered.

Every WOD code, unit, flag meaning and threshold used anywhere in this project comes from
knowledge/wod_code_tables/*.json, which was extracted from knowledge/wod_readme.pdf with
page citations. Nothing is transcribed from memory into code.
"""

from .load_tables import (
    load_table,
    available_tables,
    wod_datasets,
    wod_variables,
    wod_secondary_headers,
    variable_specific_headers,
    biological_headers,
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
from .query_wod_knowledge import query_wod_knowledge, search_manual

__all__ = [
    "query_wod_knowledge",
    "search_manual",
    "load_table",
    "available_tables",
    "wod_datasets",
    "wod_variables",
    "wod_secondary_headers",
    "variable_specific_headers",
    "biological_headers",
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
