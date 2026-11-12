"""HTTP GET with retries.

Transient statuses and network errors are retried with backoff; client errors such as 404
are not. A service that times out or cannot be reached on every attempt is not tried again
in this process, so a run with NCEI down does not wait on each request in turn.
"""
import logging
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

import httpx

from wod_etl.config import HTTP_BACKOFF_SECONDS, HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

_unreachable: Dict[str, str] = {}      # service -> why it is not tried again


class Unreachable(httpx.TransportError):
    """A request not made: its service did not answer earlier in this process."""


def service(url: str) -> str:
    """The service a URL belongs to: host plus the first two path segments. NCEI serves its
    archive, landing pages and metadata catalogue under one host, and one can be down while
    another answers."""
    parts = urlsplit(url)
    return parts.netloc.lower() + "".join(f"/{s}" for s in
                                          [s for s in parts.path.split("/") if s][:2])


def unreachable(url: str) -> Optional[str]:
    """Why `url` is not tried, if its service did not answer earlier in this process."""
    return _unreachable.get(service(url))


def mark_unreachable(url: str, error: Exception) -> None:
    key = service(url)
    if key not in _unreachable:
        log.warning("%s did not answer (%s); not tried again in this run", key,
                    type(error).__name__)
        _unreachable[key] = f"{key} did not answer earlier in this run ({type(error).__name__})"


def forget_unreachable() -> None:
    """Try every service again (tests, or a long-running process)."""
    _unreachable.clear()


def get_with_retries(url: str, *, retries: Optional[int] = None,
                     backoff_seconds: Optional[float] = None,
                     timeout: Optional[float] = None,
                     client: Optional[httpx.Client] = None) -> httpx.Response:
    """GET `url`, retrying transient failures. Returns the response or raises the last error.

    The response has passed `raise_for_status()`; `extensions["attempts"]` holds the count.
    """
    if unreachable(url):
        raise Unreachable(unreachable(url))
    attempts = max(1, HTTP_RETRIES if retries is None else retries)
    backoff = HTTP_BACKOFF_SECONDS if backoff_seconds is None else backoff_seconds
    own = client is None
    client = client or httpx.Client(timeout=timeout or HTTP_TIMEOUT, follow_redirects=True)
    last: Optional[Exception] = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                resp = client.get(url, headers={"User-Agent": USER_AGENT})
                if resp.status_code in RETRY_STATUSES and attempt < attempts:
                    log.warning("HTTP %s from %s; retrying (%d/%d)", resp.status_code, url,
                                attempt, attempts)
                    last = httpx.HTTPStatusError(f"{resp.status_code} on attempt {attempt}",
                                                 request=resp.request, response=resp)
                    time.sleep(backoff * attempt)
                    continue
                resp.raise_for_status()
                resp.extensions["attempts"] = attempt
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                log.warning("%s from %s (attempt %d/%d)", type(e).__name__, url, attempt,
                            attempts)
                if attempt < attempts:
                    time.sleep(backoff * attempt)
                    continue
                mark_unreachable(url, e)
                raise
        raise last if last else RuntimeError(f"no response from {url}")
    finally:
        if own:
            client.close()
