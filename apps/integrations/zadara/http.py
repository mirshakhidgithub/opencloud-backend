"""
Low-level HTTP toward the Zadara zCompute API (spec §5.3).

Centralizes base URL, headers, timeout, a light retry/backoff for idempotent
GETs, 429 handling, and sensitive-data masking in logs. All higher-level modules
(auth, client) go through here.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings

from .exceptions import ZadaraError

logger = logging.getLogger('zadara')

_RETRYABLE_STATUS = {502, 503, 504}
_MAX_RETRIES = 2
_BACKOFF_BASE = 0.5

# How many requests this process may have in flight toward the cloud at once.
#
# Views fetch their independent sources concurrently, which is a large win per
# request — but it multiplies just as fast across requests: 24 concurrent page
# loads at eight sources each is ~190 open sockets, and the cloud starts timing
# out (observed). A ceiling here keeps one page fast without letting twenty
# pages overwhelm the thing they all depend on.
#
# Acquired around the HTTP call only, never held while a caller waits on nested
# work, so a `gather` inside a `gather` cannot deadlock on it.
_gate = threading.BoundedSemaphore(getattr(settings, 'ZADARA_MAX_CONCURRENT_REQUESTS', 12))


def _retry_after(resp: requests.Response, default: float) -> float:
    """Seconds to wait, from a Retry-After header.

    The header is allowed to be an HTTP-date rather than a number of seconds
    (RFC 9110), and float() on a date raises — turning a rate-limit answer into
    a 500 from our own client. Anything unparseable falls back to the backoff.
    """
    raw = (resp.headers.get('Retry-After') or '').strip()
    if not raw:
        return default

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default

    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _base_url() -> str:
    base = settings.ZADARA_API_URL
    if not base:
        raise ZadaraError('unexpected', 'ZADARA_API_URL is not configured')
    return base.rstrip('/')


def _mask(token: str | None) -> str:
    if not token:
        return '-'
    return f'{token[:6]}…{token[-4:]}' if len(token) > 12 else '***'


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    allow_redirects: bool = True,
) -> requests.Response:
    """Perform an HTTP request to Zadara, normalizing transport errors."""
    url = f'{_base_url()}{path}'
    request_headers = {'Content-Type': 'application/json'}
    if token:
        request_headers['X-Auth-Token'] = token
    if headers:
        request_headers.update(headers)

    timeout = settings.ZADARA_HTTP_TIMEOUT
    attempt = 0

    while True:
        try:
            with _gate:
                resp = requests.request(
                    method, url, headers=request_headers, json=json, timeout=timeout, allow_redirects=allow_redirects
                )
        except requests.Timeout:
            raise ZadaraError('timeout', 'Zadara did not respond in time')
        except requests.RequestException as exc:
            raise ZadaraError('network_error', 'Failed to reach Zadara API', details={'reason': str(exc)})

        # Retry idempotent GETs on transient upstream errors.
        if method.upper() == 'GET' and resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            time.sleep(_BACKOFF_BASE * (2**attempt))
            attempt += 1
            continue

        # Respect rate limiting.
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            time.sleep(min(_retry_after(resp, _BACKOFF_BASE * (2**attempt)), 5))
            attempt += 1
            continue

        logger.info('zadara %s %s -> %s (token=%s)', method, path, resp.status_code, _mask(token))
        return resp
