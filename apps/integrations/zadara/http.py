"""
Low-level HTTP toward the Zadara zCompute API (spec §5.3).

Centralizes base URL, headers, timeout, a light retry/backoff for idempotent
GETs, 429 handling, and sensitive-data masking in logs. All higher-level modules
(auth, client) go through here.
"""

import logging
import time

import requests
from django.conf import settings

from .exceptions import ZadaraError

logger = logging.getLogger('zadara')

_RETRYABLE_STATUS = {502, 503, 504}
_MAX_RETRIES = 2
_BACKOFF_BASE = 0.5


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
            retry_after = float(resp.headers.get('Retry-After', _BACKOFF_BASE * (2**attempt)))
            time.sleep(min(retry_after, 5))
            attempt += 1
            continue

        logger.info('zadara %s %s -> %s (token=%s)', method, path, resp.status_code, _mask(token))
        return resp
