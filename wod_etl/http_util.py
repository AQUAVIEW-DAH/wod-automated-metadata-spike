"""HTTP GET with retries.

Transient statuses and network errors are retried with backoff; client errors such as 404
are not.
"""
import logging
import time
from typing import Optional

import httpx

from wod_etl.config import HTTP_BACKOFF_SECONDS, HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

# Transient statuses worth retrying.
RETRY_STATUSES = {429, 500, 502, 503, 504}


def get_with_retries(url: str, *, retries: Optional[int] = None,
                     backoff_seconds: Optional[float] = None,
                     timeout: Optional[float] = None,
                     client: Optional[httpx.Client] = None) -> httpx.Response:
    """GET `url`, retrying transient failures. Returns the response or raises the last error.

    The response has passed `raise_for_status()`; `extensions["attempts"]` holds the count.
    """
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
                raise
        raise last if last else RuntimeError(f"no response from {url}")
    finally:
        if own:
            client.close()
