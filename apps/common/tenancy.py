"""
The account boundary, in one place.

Several apps now need the same rule — an administrator administers their OWN
account and nothing else — and it is the rule a bug already broke once (roles
were matched as substrings, so a tenant admin saw all 21 accounts on the
cluster). It lives here so there is exactly one definition to audit.
"""

from apps.common.exceptions import AppError
from apps.integrations.zadara import service as zadara_service
from apps.integrations.zadara.exceptions import ZadaraError


def account_domain(request) -> tuple[str, str]:
    """The caller's account name and its cloud id."""
    account = (request.user.account or '').strip()
    if not account:
        raise AppError(
            message='Your session has no account, please sign in again.',
            code='session_expired',
            status_code=401,
        )

    try:
        domain_id = zadara_service.resolve_domain_id(account)
    except ZadaraError as err:
        raise AppError(message='Failed to reach the cloud', code=err.code, status_code=502)

    if not domain_id:
        raise AppError(message='Account not found in the cloud.', code='account_not_found', status_code=404)

    return account, domain_id
