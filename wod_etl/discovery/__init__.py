"""Phase 1: Ingestion & Inspection skills."""

from .list_remote_directory import (list_remote_directory, accession_number,
                                    resolve_accession_url)
from .fetch_and_peek_file import fetch_and_peek_file
from .sniff_format import sniff_format
from .classify_dataset import classify_dataset, classify_accession, classify_file
from .profile_raw_data import profile_raw_data
from .download_file import download_file
from .load_tabular import load_tabular
from .read_any import read_any
from .split_casts import split_casts, cast_as_profile_input

__all__ = [
    "list_remote_directory",
    "accession_number",
    "resolve_accession_url",
    "fetch_and_peek_file",
    "sniff_format",
    "classify_dataset",
    "classify_accession",
    "classify_file",
    "profile_raw_data",
    "download_file",
    "load_tabular",
    "read_any",
    "split_casts",
    "cast_as_profile_input",
]
