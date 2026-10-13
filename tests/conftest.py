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


@pytest.fixture(autouse=True)
def offline_code_lists(tmp_path, monkeypatch):
    """No test reaches NCEI for a code list, and none reads or writes the real cache.

    Lookups stay ON, so the code paths run; with an empty cache and fetching off, every
    text value comes back `unavailable`. A test that wants a list writes one into
    `config.CODE_LIST_CACHE_DIR` (see tests/helpers.py: `cache_code_list`).
    """
    from wod_etl import config
    from wod_etl.reference import code_lists

    cache = tmp_path / "code_lists"
    cache.mkdir()
    monkeypatch.setattr(config, "CODE_LIST_CACHE_DIR", cache)
    monkeypatch.setattr(config, "CODE_LIST_FETCH", False)
    monkeypatch.setattr(config, "CODE_LOOKUP", True)
    code_lists.clear_cache_memo()
    yield cache
    code_lists.clear_cache_memo()


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """Retries still happen in tests; the waits between them do not."""
    from wod_etl import http_util

    monkeypatch.setattr(http_util, "HTTP_BACKOFF_SECONDS", 0.0)
