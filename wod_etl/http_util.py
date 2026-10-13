"""One GET with retries, for every page this package reads over HTTP.

NCEI answers 503 and times out often enough that a single attempt is a coin toss: a whole
session's triage once stalled on a landing page that was simply busy. Transient statuses and
network errors are retried with backoff; a 404 or any other client error is not, because
retrying it only delays the real answer.
"""
import time
from typing import Optional

import httpx

from wod_etl.config import HTTP_BACKOFF_SECONDS, HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT

# Statuses that mean "try again shortly", not "this URL is wrong".
RETRY_STATUSES = {429, 500, 502, 503, 504}


def get_with_retries(url: str, *, retries: Optional[int] = None,
                     backoff_seconds: Optional[float] = None,
                     timeout: Optional[float] = None,
                     client: Optional[httpx.Client] = None) -> httpx.Response:
    """GET `url`, retrying transient failures. Returns the response or raises the last error.

    The response is returned with `raise_for_status()` already applied, so a caller sees a
    success or an exception -- with `attempts` recorded on the response's `extensions`.
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
                    last = httpx.HTTPStatusError(f"{resp.status_code} on attempt {attempt}",
                                                 request=resp.request, response=resp)
                    time.sleep(backoff * attempt)
                    continue
                resp.raise_for_status()
                resp.extensions["attempts"] = attempt
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                if attempt < attempts:
                    time.sleep(backoff * attempt)
                    continue
                raise
        raise last if last else RuntimeError(f"no response from {url}")
    finally:
        if own:
            client.close()
