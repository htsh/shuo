"""Ephemeral token auth for realtime websocket sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


class RealtimeAuthError(ValueError):
    """Raised when a realtime session token is invalid."""


@dataclass(frozen=True)
class SessionClaims:
    """Decoded claims in a verified realtime session token."""

    session_id: str
    issued_at: int
    expires_at: int
    token_id: str
    origin: str = ""


_used_token_ids: Dict[str, int] = {}
_lock = threading.Lock()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _secret() -> str:
    value = os.getenv("REALTIME_SESSION_SECRET", "").strip()
    if not value:
        raise RuntimeError("REALTIME_SESSION_SECRET is not configured")
    return value


def _default_ttl() -> int:
    raw = os.getenv("REALTIME_TOKEN_TTL_SECONDS", "60").strip()
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise RuntimeError("REALTIME_TOKEN_TTL_SECONDS must be an integer") from exc
    return max(5, min(ttl, 600))


def clear_replay_cache() -> None:
    """Clear consumed-token cache (used in tests)."""
    with _lock:
        _used_token_ids.clear()


def _cleanup_expired(now_ts: int) -> None:
    expired = [tid for tid, exp in _used_token_ids.items() if exp <= now_ts]
    for token_id in expired:
        _used_token_ids.pop(token_id, None)


def mint_session(origin: Optional[str] = None, ttl_seconds: Optional[int] = None) -> Tuple[str, SessionClaims]:
    """Create a short-lived signed token for one websocket session."""
    now_ts = int(time.time())
    ttl = _default_ttl() if ttl_seconds is None else int(ttl_seconds)
    ttl = max(5, min(ttl, 600))
    claims = {
        "sid": secrets.token_urlsafe(12),
        "iat": now_ts,
        "exp": now_ts + ttl,
        "jti": secrets.token_urlsafe(10),
        "ori": (origin or "").strip(),
    }

    payload_bytes = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = _b64url_encode(payload_bytes)

    signature = hmac.new(
        _secret().encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    token = f"{payload}.{_b64url_encode(signature)}"

    return token, SessionClaims(
        session_id=claims["sid"],
        issued_at=claims["iat"],
        expires_at=claims["exp"],
        token_id=claims["jti"],
        origin=claims["ori"],
    )


def verify_session_token(
    token: str,
    *,
    origin: Optional[str] = None,
    consume: bool = False,
    now_ts: Optional[int] = None,
) -> SessionClaims:
    """Verify and decode a realtime token; optionally mark it as consumed."""
    if not token or "." not in token:
        raise RealtimeAuthError("invalid token format")

    now_val = int(time.time()) if now_ts is None else int(now_ts)

    try:
        payload_b64, signature_b64 = token.split(".", 1)
        expected_sig = hmac.new(
            _secret().encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        actual_sig = _b64url_decode(signature_b64)
    except Exception as exc:
        raise RealtimeAuthError("invalid token encoding") from exc

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise RealtimeAuthError("invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        claims = SessionClaims(
            session_id=str(payload["sid"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            token_id=str(payload["jti"]),
            origin=str(payload.get("ori", "") or ""),
        )
    except Exception as exc:
        raise RealtimeAuthError("invalid token payload") from exc

    if claims.expires_at <= now_val:
        raise RealtimeAuthError("token expired")

    requested_origin = (origin or "").strip()
    if claims.origin and requested_origin and claims.origin != requested_origin:
        raise RealtimeAuthError("origin mismatch")

    if consume:
        with _lock:
            _cleanup_expired(now_val)
            if claims.token_id in _used_token_ids:
                raise RealtimeAuthError("token already used")
            _used_token_ids[claims.token_id] = claims.expires_at

    return claims
