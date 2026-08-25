"""
The HTTP client's own behaviour toward the cloud.

Small things here fail loudly in the wrong place: a header we cannot parse
becomes a 500 from our own code, and the caller is told the cabinet is broken
when the cloud merely asked it to wait.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from apps.integrations.zadara.http import _retry_after


class Resp:
    def __init__(self, value=None):
        self.headers = {'Retry-After': value} if value is not None else {}


def test_seconds_are_taken_as_seconds():
    assert _retry_after(Resp('3'), 0.5) == 3


def test_a_missing_header_falls_back_to_the_backoff():
    assert _retry_after(Resp(), 0.75) == 0.75


def test_an_http_date_is_parsed_rather_than_crashing():
    """RFC 9110 allows a date here, and float() on a date raises — which turned a
    rate-limit answer into a 500 from our own client."""
    when = datetime.now(timezone.utc) + timedelta(seconds=30)

    assert 25 <= _retry_after(Resp(format_datetime(when)), 0.5) <= 31


def test_a_date_in_the_past_waits_no_time_at_all():
    when = datetime.now(timezone.utc) - timedelta(minutes=5)

    assert _retry_after(Resp(format_datetime(when)), 0.5) == 0


def test_nonsense_falls_back_instead_of_raising():
    assert _retry_after(Resp('soon'), 1.25) == 1.25
