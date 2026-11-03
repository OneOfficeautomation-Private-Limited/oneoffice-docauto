"""Standard error model shared by all engine options."""
from __future__ import annotations


class DocAutoError(Exception):
    code = "PROVIDER_ERROR"
    retryable = False

    def __init__(self, message: str, provider_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "providerId": self.provider_id,
                "retryable": self.retryable}


class InvalidInput(DocAutoError):
    code = "INVALID_INPUT"


class UnsupportedCapability(DocAutoError):
    code = "UNSUPPORTED_CAPABILITY"


class PolicyDenied(DocAutoError):
    code = "POLICY_DENIED"


class AuthFailed(DocAutoError):
    code = "AUTH_FAILED"


class QuotaExceeded(DocAutoError):
    code = "QUOTA_EXCEEDED"


class RateLimited(DocAutoError):
    code = "RATE_LIMITED"
    retryable = True


class ProviderTimeout(DocAutoError):
    code = "TIMEOUT"
    retryable = True


class ProviderError(DocAutoError):
    code = "PROVIDER_ERROR"
    retryable = True


class ProviderUnavailable(DocAutoError):
    """Engine not installed / not configured (e.g. missing SDK or credentials)."""
    code = "PROVIDER_UNAVAILABLE"
