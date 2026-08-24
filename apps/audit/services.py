"""Recording helper: one call from a view, no exceptions leaking out."""

import logging

from .models import AuditLog

logger = logging.getLogger(__name__)


def _client_ip(request) -> str | None:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record(
    request,
    action: str,
    *,
    resource_type: str = '',
    resource_id: str = '',
    resource_name: str = '',
    outcome: str = AuditLog.SUCCESS,
    error_code: str = '',
    detail: dict | None = None,
) -> None:
    """Write one audit row. Never raises: auditing must not break the action."""
    user = request.user if request.user.is_authenticated else None
    session = request.session

    try:
        AuditLog.objects.create(
            user=user,
            username=getattr(user, 'username', '') or 'anonymous',
            account=getattr(user, 'account', '') or '',
            project_id=session.get('zadara_project_id') or '',
            project_name=session.get('zadara_project_name') or '',
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            outcome=outcome,
            error_code=error_code,
            detail=detail or {},
            ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:512],
        )
    except Exception:  # noqa: BLE001 - auditing is best-effort by design
        logger.exception('failed to write audit entry for %s', action)
