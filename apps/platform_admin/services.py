"""Recording operator actions, and the `me` payload."""

import logging

from .models import AdminAction, PlatformAdmin

logger = logging.getLogger(__name__)


def client_ip(request) -> str | None:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record(
    request,
    action: str,
    *,
    actor: PlatformAdmin | None = None,
    target_account: str = '',
    target_type: str = '',
    target_id: str = '',
    target_name: str = '',
    outcome: str = AdminAction.SUCCESS,
    error_code: str = '',
    detail: dict | None = None,
) -> None:
    """Write one row. Never raises — a failed log must not undo the action.

    `actor` is passed explicitly for sign-in attempts, where the request is not
    authenticated yet but the row still has to name who tried.
    """
    who = actor if actor is not None else (request.user if isinstance(request.user, PlatformAdmin) else None)

    try:
        AdminAction.objects.create(
            actor=who,
            actor_email=getattr(who, 'email', '') or 'anonymous',
            action=action,
            target_account=target_account,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            outcome=outcome,
            error_code=error_code,
            detail=detail or {},
            ip_address=client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:512],
        )
    except Exception:  # noqa: BLE001 - auditing is best-effort by design
        logger.exception('failed to write admin action %s', action)


def me_payload(admin: PlatformAdmin) -> dict:
    return {
        'id': admin.pk,
        'email': admin.email,
        'name': admin.name,
        'role': admin.role,
        'canWrite': admin.can_write,
        'lastLoginAt': admin.last_login_at.isoformat() if admin.last_login_at else None,
    }
