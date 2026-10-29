"""Verification: check that what was written is right.

`validate_intermediate_netcdf` and `validate_taxa_netcdf` check file structure;
`verify_output` checks values against the manual's depth-dependent ranges (Appendix 11).
"""

from .validate_intermediate_netcdf import validate_intermediate_netcdf
from .verify_output import verify_output
from .validate_taxa_netcdf import validate_taxa_netcdf

__all__ = [
    "validate_intermediate_netcdf",
    "verify_output",
    "validate_taxa_netcdf",
]
