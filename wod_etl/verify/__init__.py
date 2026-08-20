"""Verification: checking that what was written is actually right.

Two complementary checks, and the second is the one that catches real corruption.

`validate_intermediate_netcdf` is structural -- does this file obey CF and the ragged-array
contract. A file can pass it and still be wrong: a variable in the wrong unit, a column
silently dropped at mapping, a physically impossible value.

`audit_dataset` is what closes that gap. It reads the whole dataset back -- every mapped
and dropped column, every variable's value range, the QC flag distribution -- so a 30x unit
error or a missing column shows up as a number you can look at.

Run both. Structural validation gates the output; the audit is what you report.
"""

from .validate_intermediate_netcdf import validate_intermediate_netcdf
from .audit_dataset import audit_dataset, contract_for

__all__ = [
    "validate_intermediate_netcdf",
    "audit_dataset",
    "contract_for",
]
