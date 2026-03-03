import pytest

from shuo.realtime.auth import (
    RealtimeAuthError,
    clear_replay_cache,
    mint_session,
    verify_session_token,
)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("REALTIME_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("REALTIME_TOKEN_TTL_SECONDS", "60")
    clear_replay_cache()


def test_mint_and_verify_roundtrip():
    token, claims = mint_session(origin="http://localhost:3040")

    parsed = verify_session_token(token, origin="http://localhost:3040")

    assert parsed.session_id == claims.session_id
    assert parsed.token_id == claims.token_id
    assert parsed.expires_at == claims.expires_at


def test_verify_rejects_tampered_signature():
    token, _ = mint_session()
    payload, signature = token.split(".", 1)

    tampered = f"{payload}.{'A' + signature[1:]}"

    with pytest.raises(RealtimeAuthError):
        verify_session_token(tampered)


def test_verify_rejects_expired_token():
    token, claims = mint_session(ttl_seconds=5)

    with pytest.raises(RealtimeAuthError):
        verify_session_token(token, now_ts=claims.expires_at + 1)


def test_verify_consume_rejects_replay():
    token, _ = mint_session()

    verify_session_token(token, consume=True)

    with pytest.raises(RealtimeAuthError):
        verify_session_token(token, consume=True)


def test_verify_rejects_origin_mismatch():
    token, _ = mint_session(origin="https://one.example")

    with pytest.raises(RealtimeAuthError):
        verify_session_token(token, origin="https://two.example")
