"""Autouse fixtures that keep tests out of the real data, cache and runs directories."""
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
    """Use an empty per-test code-list cache with fetching off; see `helpers.cache_code_list`."""
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
    """Keep retries but skip the waits between them."""
    from wod_etl import http_util

    monkeypatch.setattr(http_util, "HTTP_BACKOFF_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def isolated_runs_dir(tmp_path, monkeypatch):
    """Write run output to a per-test folder and remove log handlers added by the test."""
    import logging

    from wod_etl import config

    d = tmp_path / "runs"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RUNS_DIR", d)
    logger = logging.getLogger("wod_etl")
    before, level = list(logger.handlers), logger.level
    yield d
    for h in list(logger.handlers):
        if h not in before:
            logger.removeHandler(h)
            h.close()
    logger.setLevel(level)
