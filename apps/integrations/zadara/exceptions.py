"""Domain exceptions for the Zadara integration (spec §5.4)."""


class ZadaraError(Exception):
    """
    Raised for any Zadara integration failure. `code` is a stable, human-mappable
    string surfaced to the API layer; technical details never leak to the client.
    """

    def __init__(self, code: str, message: str, status: int | None = None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


# Stable codes (mirror the frontend client codes)
INVALID_CREDENTIALS = 'invalid_credentials'
ACCOUNT_NOT_FOUND = 'account_not_found'
NO_PROJECT_ACCESS = 'no_project_access'
FORBIDDEN = 'forbidden'
NETWORK_ERROR = 'network_error'
TIMEOUT = 'timeout'
RATE_LIMITED = 'rate_limited'
UPSTREAM_ERROR = 'upstream_error'
UNEXPECTED = 'unexpected'
