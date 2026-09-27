"""One error type for the whole partner API.

Every error leaves the API as {"error": {"code": "...", "message": "..."}}.
"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def invalid_key(msg: str = "Missing, malformed or revoked API key.") -> ApiError:
    return ApiError(401, "invalid_key", msg, {"WWW-Authenticate": "Bearer"})


def insufficient_scope(scope: str) -> ApiError:
    return ApiError(403, "insufficient_scope", f"This key does not have the '{scope}' scope.")


def invalid_item(msg: str) -> ApiError:
    return ApiError(422, "invalid_item", msg)


def not_food() -> ApiError:
    return ApiError(422, "not_food", "The input does not appear to be food.")


def rate_limited(retry_after_s: int) -> ApiError:
    return ApiError(429, "rate_limited", "Rate limit exceeded; retry after the given number of seconds.",
                    {"Retry-After": str(max(1, retry_after_s))})


def ai_unavailable(msg: str = "The analysis service is unavailable or timed out. Safe to retry.") -> ApiError:
    return ApiError(503, "ai_unavailable", msg, {"Retry-After": "5"})


def capacity_reached() -> ApiError:
    return ApiError(503, "capacity_reached",
                    "Daily analysis capacity has been reached. Cached menu items still work; retry after midnight UTC.",
                    {"Retry-After": "3600"})
