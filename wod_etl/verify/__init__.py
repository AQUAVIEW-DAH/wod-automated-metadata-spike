"""Verification: checking that what was written is actually right.

Two complementary checks, and the second is the one that catches real corruption.

`validate_intermediate_netcdf` is structural -- does this file obey CF and the ragged-array
contract. A file can pass it and still be wrong: a variable in the wrong unit, a column
silently dropped at mapping, a physically impossible value.

`verify_output` closes that gap without being asked. It reads every written NetCDF back and
compares each observation against the manual's acceptable range for that variable AT THAT
DEPTH (Appendix 11), then reports FINDINGS with a severity rather than numbers to interpret.
`process_accession` runs it at the end of every run, so a unit error fails the run instead
of waiting for someone to look.

`audit_dataset` is the wider report: every mapped and dropped column across a dataset, plus
the ranges and the flag distribution. It needs the staged directory as well as the output,
which is why it stays a separate call.

Structural validation gates each file; verify_output judges the numbers; the audit is what
you report.
"""

from .validate_intermediate_netcdf import validate_intermediate_netcdf
from .audit_dataset import audit_dataset, contract_for
from .verify_output import verify_output

__all__ = [
    "validate_intermediate_netcdf",
    "verify_output",
    "audit_dataset",
    "contract_for",
]
