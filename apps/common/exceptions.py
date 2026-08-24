"""
Domain exceptions and the API error envelope (spec §7.1).

Error shape: { "error": { "code": str, "message": str, "details": {...} } }
"""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class AppError(APIException):
    """Base application error carrying a stable string code."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'error'
    default_detail = 'An error occurred.'

    def __init__(self, message=None, code=None, details=None, status_code=None):
        self.code = code or self.default_code
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message or self.default_detail)


class NotAuthenticatedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = 'unauthenticated'
    default_detail = 'Authentication required.'


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'forbidden'
    default_detail = 'You do not have permission to perform this action.'


class UpstreamError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = 'upstream_error'
    default_detail = 'Upstream service error.'


def _code_for(exc, response):
    if isinstance(exc, AppError):
        return exc.code
    mapping = {
        401: 'unauthenticated',
        403: 'forbidden',
        404: 'not_found',
        405: 'method_not_allowed',
        429: 'rate_limited',
    }
    return mapping.get(response.status_code, 'error')


def envelope_exception_handler(exc, context):
    """Convert any raised exception into the { error: {...} } envelope."""
    response = drf_exception_handler(exc, context)

    if response is None:
        return None

    detail = response.data
    details = {}
    message = None

    if isinstance(detail, dict):
        message = detail.get('detail')
        if message is None:
            details = detail
            message = 'Validation failed.'
    elif isinstance(detail, list):
        details = {'errors': detail}
        message = 'Validation failed.'
    else:
        message = str(detail)

    if isinstance(exc, AppError) and exc.details:
        details = exc.details

    response.data = {
        'error': {
            'code': _code_for(exc, response),
            'message': str(message),
            'details': details,
        }
    }

    return response


def handler404(request, exception=None):
    """Project-wide 404 → JSON envelope (used when DEBUG=False)."""
    return JsonResponse(
        {'error': {'code': 'not_found', 'message': 'Resource not found.', 'details': {}}},
        status=404,
    )


def handler500(request):
    """Project-wide 500 → JSON envelope (used when DEBUG=False)."""
    return JsonResponse(
        {'error': {'code': 'server_error', 'message': 'Internal server error.', 'details': {}}},
        status=500,
    )
