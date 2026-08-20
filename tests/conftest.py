"""Keep the test suite out of the working data directories.

Without this, every run leaves manifests in data/staging/manifests/ and downloaded copies
in data/raw/, which makes a real run's ledger hard to read.
"""
import pytest

from wod_etl.orchestration import run_manifest


@pytest.fixture(autouse=True)
def isolated_manifest_dir(tmp_path, monkeypatch):
    """Point the manifest ledger at a per-test temporary directory."""
    d = tmp_path / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(run_manifest, "MANIFEST_DIR", d)
    return d
