from __future__ import annotations

import contextvars
import uuid

from fastapi import Request

_request_context_var: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "current_request", default=None
)

_user_id_context_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_id", default=None
)

_trace_id_context_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_trace_id", default=""
)

# ── Request ────────────────────────────────────────────────────────────────────


def set_current_request(request: Request | None) -> contextvars.Token[Request | None]:
    return _request_context_var.set(request)


def reset_current_request(token: contextvars.Token[Request | None]) -> None:
    _request_context_var.reset(token)


def get_current_request() -> Request | None:
    return _request_context_var.get()


# ── Cookies ────────────────────────────────────────────────────────────────────


def get_current_cookies() -> dict[str, str]:
    req = _request_context_var.get()
    if req is None:
        return {}
    return dict(req.cookies)


# ── Auth token (Bearer token or cookie fallback) ───────────────────────────────


def get_current_auth_token() -> str:
    """Extract the raw auth material from the current request.

    Returns ``Authorization: Bearer <token>`` value, or the first matching
    cookie (``sessionid`` / ``sid`` / ``token``) as a fallback.

    Adapters calling downstream services can forward this token.
    """
    req = _request_context_var.get()
    if req is None:
        return ""
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :]
    for key in ("sessionid", "sid", "token"):
        if key in req.cookies:
            return req.cookies[key]
    return ""


# ── Locale (from Accept-Language) ──────────────────────────────────────────────


def get_current_locale(default: str = "zh") -> str:
    """Parse ``Accept-Language`` from the current request.

    Returns the first language tag (e.g. ``"zh"`` / ``"en"``), or *default*.
    """
    req = _request_context_var.get()
    if req is None:
        return default
    accept_language = req.headers.get("accept-language", "")
    if accept_language:
        lang = accept_language.split(",")[0].split(";")[0].strip().lower()
        if lang in ("zh", "en"):
            return lang
    return default


# ── User ID (set after successful auth, see get_user_id in api/auth.py) ────────


def set_current_user_id(user_id: str | None) -> contextvars.Token[str | None]:
    return _user_id_context_var.set(user_id)


def get_current_user_id() -> str | None:
    return _user_id_context_var.get()


def clear_current_user_id() -> None:
    _user_id_context_var.set(None)


# ── Trace ID (for distributed tracing / logging) ────────────────────────────────


def set_current_trace_id(trace_id: str) -> contextvars.Token[str]:
    return _trace_id_context_var.set(trace_id)


def reset_current_trace_id(token: contextvars.Token[str]) -> None:
    _trace_id_context_var.reset(token)


def get_current_trace_id() -> str:
    return _trace_id_context_var.get()


def ensure_trace_id(request: Request) -> str:
    """Return ``X-Request-Id`` / ``X-Trace-Id`` from *request*, or generate one."""
    trace_id = request.headers.get("X-Request-Id", "") or request.headers.get(
        "X-Trace-Id", ""
    )
    if not trace_id:
        trace_id = uuid.uuid4().hex[:16]
    return trace_id
