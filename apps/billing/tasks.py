"""Background work for billing. Scheduled by Celery beat (see settings)."""

import logging

from celery import shared_task

from .collector import capture

logger = logging.getLogger('billing')


@shared_task(name='billing.capture_usage_snapshots')
def capture_usage_snapshots() -> dict:
    """Measure every project once a day.

    A day that is missed cannot be reconstructed — the cloud keeps no inventory
    history we can read — so this task is the only source of billable history.
    """
    result = capture()
    logger.info('captured usage: %s', result)

    return result
